"""
vault_linker.py — Scan Obsidian vault notes → เพิ่ม [[wikilinks]] อัตโนมัติ
รองรับ 2 โหมด:
  1. KG-based linking (ใช้ PrimeKG — ฟรี ไม่ต้อง call AI)
  2. AI-based linking (ใช้ call_agent — fallback)
"""
import os
import re
import sys
import json
import time
import argparse
from typing import Dict, List, Tuple

from router import call_agent

# Try to load KG
try:
    from kg_query import ClinicalKG
    _KG_AVAILABLE = True
except (ImportError, FileNotFoundError):
    _KG_AVAILABLE = False


# ─── Utility Functions ──────────────────────────────────────────────────────

def load_all_notes(vault_dir: str) -> Dict[str, dict]:
    """
    Scan ทุก .md file ใน vault
    Returns: { "Note Name": { "path": "...", "content": "..." }, ... }
    """
    all_notes = {}
    for root, dirs, files in os.walk(vault_dir):
        # Skip hidden folders (e.g. .obsidian)
        dirs[:] = [d for d in dirs if not d.startswith(".")]

        for file in files:
            if not file.endswith(".md"):
                continue
            # ชื่อ note = ชื่อไฟล์ ไม่รวม .md, แทน _ ด้วย space
            name = file.replace(".md", "").replace("_", " ")
            path = os.path.join(root, file)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                all_notes[name] = {"path": path, "content": content}
            except Exception as e:
                print(f"  ⚠️ Cannot read {path}: {e}")

    return all_notes


def save_note(path: str, content: str):
    """Save note ทับไฟล์เดิม"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def add_links_to_note(content: str, links: List[str], all_notes: Dict[str, dict]) -> str:
    """
    เพิ่ม [[wikilinks]] ใน body text และ Related section

    - แทนที่ชื่อ note ที่พบใน body ด้วย [[wikilink]]
    - อัปเดต/สร้าง ## Related section
    - ระวังไม่ให้ซ้อน link (ไม่ link ซ้ำถ้ามี [[ ]] อยู่แล้ว)
    """
    for link in links:
        if link not in all_notes:
            continue

        # หลีกเลี่ยงการ link ซ้ำ — ถ้ามี [[link]] อยู่แล้วให้ข้าม
        if f"[[{link}]]" in content:
            continue

        # แทนที่ชื่อ note ใน body (ยกเว้นใน heading # lines และ links ที่มีอยู่แล้ว)
        # ใช้ word boundary เพื่อไม่ให้ replace substring
        pattern = re.compile(
            r'(?<!\[\[)'           # ไม่อยู่หลัง [[
            r'(?<!\#\s)'          # ไม่อยู่หลัง # (heading)
            + re.escape(link) +
            r'(?!\]\])',           # ไม่อยู่ก่อน ]]
            re.IGNORECASE
        )

        # Replace แค่ครั้งแรกเท่านั้น (ไม่ทำทุก occurrence)
        content = pattern.sub(f"[[{link}]]", content, count=1)

    # อัปเดต Related section
    related_lines = [f"- [[{link}]]" for link in links if link in all_notes]

    if not related_lines:
        return content

    related_section = "\n## Related\n" + "\n".join(related_lines) + "\n"

    # แทนที่ Related section เดิม หรือเพิ่มใหม่ (ก่อนบรรทัด ---)
    if "## Related" in content:
        # ตัดตั้งแต่ ## Related ไปจนจบ (หรือจนเจอ ---)
        related_start = content.rfind("## Related")
        # หา --- ที่อยู่หลัง ## Related
        rest = content[related_start:]
        dash_pos = rest.find("\n---")
        if dash_pos != -1:
            after_related = rest[dash_pos:]
            content = content[:related_start] + related_section + after_related
        else:
            content = content[:related_start] + related_section
    else:
        # เพิ่มก่อนบรรทัด --- สุดท้าย
        last_dash = content.rfind("\n---")
        if last_dash != -1:
            content = content[:last_dash] + "\n" + related_section + content[last_dash:]
        else:
            content += "\n" + related_section

    return content


# ─── KG-Based Linking (Primary — ฟรี) ───────────────────────────────────────

def build_links_with_kg(vault_dir: str, kg_path: str = None, target: str = None):
    """
    ใช้ PrimeKG เป็น evidence สำหรับหา connections
    แม่นกว่า AI + ฟรี ไม่ต้องเสีย token
    """
    try:
        kg = ClinicalKG(kg_path) if kg_path else ClinicalKG()
    except FileNotFoundError as e:
        print(f"❌ {e}")
        return

    all_notes = load_all_notes(vault_dir)
    print(f"📂 Found {len(all_notes)} notes in vault")

    stats = {"linked": 0, "skipped": 0, "total_links": 0}

    for note_name, note_data in all_notes.items():
        if target and target not in note_data["path"]:
            continue
        # Query KG สำหรับ note นี้
        connections = kg.get_all_connections(note_name)

        # หา notes ที่มีอยู่จริงใน vault และ match กับ KG results
        valid_links = set()
        for category, entities in connections.items():
            for entity in entities:
                for existing_note in all_notes.keys():
                    # fuzzy match: ถ้า entity name อยู่ใน note name หรือกลับกัน
                    if (entity.lower() in existing_note.lower() or
                            existing_note.lower() in entity.lower()):
                        if existing_note != note_name:  # ไม่ link ตัวเอง
                            valid_links.add(existing_note)

        if not valid_links:
            stats["skipped"] += 1
            continue

        valid_links = list(valid_links)

        # เพิ่ม links ใน note
        updated = add_links_to_note(note_data["content"], valid_links, all_notes)

        if updated != note_data["content"]:
            save_note(note_data["path"], updated)
            stats["linked"] += 1
            stats["total_links"] += len(valid_links)
            print(f"  ✅ {note_name}: +{len(valid_links)} links "
                  f"({', '.join(valid_links[:3])}{'...' if len(valid_links) > 3 else ''})")
        else:
            stats["skipped"] += 1

    print(f"\n{'='*50}")
    print(f"✅ KG linking complete")
    print(f"   Notes linked: {stats['linked']}")
    print(f"   Notes skipped: {stats['skipped']}")
    print(f"   Total links added: {stats['total_links']}")


# ─── AI-Based Linking (Fallback) ────────────────────────────────────────────

def build_links_with_ai(vault_dir: str, delay: float = 1.0, target: str = None):
    """
    ส่งให้ AI วิเคราะห์ connections ระหว่าง notes
    ใช้เมื่อไม่มี PrimeKG หรือต้องการ cross-check
    """
    all_notes = load_all_notes(vault_dir)
    print(f"📂 Found {len(all_notes)} notes in vault")

    note_list = "\n".join([f"- {name}" for name in all_notes.keys()])
    stats = {"linked": 0, "skipped": 0, "errors": 0}

    for note_name, note_data in all_notes.items():
        if target and target not in note_data["path"]:
            continue
        print(f"  🔗 Linking: {note_name}...")

        prompt = f"""Available notes in vault:
{note_list}

Current note: {note_name}
Content (first 2000 chars):
{note_data['content'][:2000]}

หา notes จาก list ข้างบนที่ควร link กับ note นี้
เฉพาะที่เกี่ยวข้องทาง clinical จริงๆ เท่านั้น ไม่ใช่ทุกอัน
ห้าม link ตัวเอง

Output JSON only:
{{"links_to_add": ["Note Name 1", "Note Name 2"], "reason": "เหตุผลสั้นๆ"}}
"""
        try:
            result_raw = call_agent(
                "analyzer",
                prompt=prompt,
                system="Output JSON only. Find clinically relevant links between medical notes."
            )

            # Parse JSON
            result = json.loads(_extract_json(result_raw))
            links = result.get("links_to_add", [])

        except (json.JSONDecodeError, Exception) as e:
            print(f"    ⚠️ Parse error: {e}")
            stats["errors"] += 1
            continue

        if not links:
            stats["skipped"] += 1
            continue

        # Filter เฉพาะ notes ที่มีอยู่จริง
        valid_links = [l for l in links if l in all_notes and l != note_name]

        if not valid_links:
            stats["skipped"] += 1
            continue

        updated = add_links_to_note(note_data["content"], valid_links, all_notes)

        if updated != note_data["content"]:
            save_note(note_data["path"], updated)
            stats["linked"] += 1
            print(f"    ✅ +{len(valid_links)} links")

        time.sleep(delay)

    print(f"\n{'='*50}")
    print(f"✅ AI linking complete")
    print(f"   Notes linked: {stats['linked']}")
    print(f"   Notes skipped: {stats['skipped']}")
    print(f"   Errors: {stats['errors']}")


def _extract_json(text: str) -> str:
    """Extract JSON from LLM output (may be wrapped in markdown code block)"""
    # ลอง parse ตรงๆ ก่อน
    text = text.strip()
    if text.startswith("{"):
        return text

    # หา JSON ใน code block
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        return match.group(1).strip()

    # หา { ... } ใน text
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return match.group(0)

    return text


# ─── CLI ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CaseFlow Vault Linker")
    parser.add_argument(
        "vault_dir",
        nargs="?",
        default=r"C:\Users\USER\Obsidian vault",
        help=r"Path to Obsidian vault directory (default: C:\Users\USER\Obsidian vault)"
    )
    parser.add_argument(
        "--mode", choices=["kg", "ai", "both"], default="kg",
        help="Linking mode: kg (PrimeKG, default), ai (LLM-based), both"
    )
    parser.add_argument(
        "--kg-path", type=str, default=None,
        help="Path to clinical_kg.csv (default: auto-detect)"
    )
    parser.add_argument(
        "--delay", type=float, default=1.0,
        help="Delay between AI calls in seconds (for ai mode)"
    )
    parser.add_argument(
        "--target", type=str, default=None,
        help="Target specific folder or file name to update (e.g., '01 - Active Cases')"
    )
    args = parser.parse_args()

    if not os.path.exists(args.vault_dir):
        print(f"❌ Vault directory not found: {args.vault_dir}")
        sys.exit(1)

    print(f"🔗 CaseFlow Vault Linker")
    print(f"   Vault: {args.vault_dir}")
    print(f"   Mode: {args.mode}")
    if args.target:
        print(f"   Target: {args.target}")
    print()

    if args.mode in ("kg", "both"):
        if _KG_AVAILABLE:
            print("━━━ KG-Based Linking ━━━")
            build_links_with_kg(args.vault_dir, args.kg_path, args.target)
        else:
            print("⚠️ KG not available — skipping KG mode")

    if args.mode in ("ai", "both"):
        print("\n━━━ AI-Based Linking ━━━")
        build_links_with_ai(args.vault_dir, delay=args.delay, target=args.target)

    print("\n🎉 Linking complete!")
