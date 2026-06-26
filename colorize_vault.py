"""
colorize_vault.py — ใส่สีใน Obsidian notes ที่มีอยู่แล้ว ด้วย Local LLM (Ollama)

Usage:
  python colorize_vault.py                          # ทุก folder
  python colorize_vault.py --folder "02 - Diseases" # เฉพาะ folder
  python colorize_vault.py --dry-run                # preview ไม่เขียนทับ
  python colorize_vault.py --model qwen2.5:14b      # ระบุ model
  python colorize_vault.py --workers 2              # จำนวน parallel (default 2)
"""
import argparse
import json
import re
import shutil
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

import os
OBSIDIAN_PATH = Path(os.getenv("OBSIDIAN_VAULT_PATH",
                                r"C:\Users\ASUS\Documents\Obsidian vault"))
OLLAMA_URL    = "http://localhost:11434"
BACKUP_DIR    = OBSIDIAN_PATH / ".colorize_backup"

# ── ไฟล์/folder ที่ข้าม ──────────────────────────────────────────────────────
_SKIP_FOLDERS = {".obsidian", ".colorize_backup", "99 - Templates", "copilot",
                 "PDF", "radiology", "images", "diseases"}
_MIN_SIZE_BYTES = 200   # ข้ามไฟล์เล็กเกิน (template/ว่าง)
_MAX_SIZE_BYTES = 60_000  # ตัดไฟล์ใหญ่มาก ส่งทีละ section แทน

# ── Prompt ───────────────────────────────────────────────────────────────────
_SYSTEM = """คุณเป็น medical note editor สำหรับนิสิตแพทย์ไทยปี 4

งาน: เพิ่ม <span> tag ลงใน Obsidian markdown note เพื่อเน้นข้อมูลสำคัญ

ระบบสี:
- <span class="must-know">ข้อมูล</span>   → ข้อมูล critical / Must Know (แดง)
- <span class="distractor">ข้อมูล</span>  → distractor / สิ่งที่ต้องระวัง (ส้ม)
- <span class="management">ข้อมูล</span>  → การรักษา / drug of choice (เขียว)
- <span class="diagnosis">ข้อมูล</span>   → criteria / investigation หลัก (ฟ้า)
- <span class="threshold">ข้อมูล</span>   → ตัวเลข / dose / cutoff (ม่วง)

กฎสำคัญ:
1. ห้ามเปลี่ยนเนื้อหา — แก้ได้เฉพาะเพิ่ม <span> tag เท่านั้น
2. ใส่สีเฉพาะจุดที่สำคัญจริงๆ ไม่ใส่ทุกคำ (มากเกินทำให้รก)
3. ห้ามแตะ YAML frontmatter (ระหว่าง --- บรรทัดแรก)
4. ห้ามแตะ wikilinks [[...]] และ code blocks ```...```
5. ห้ามเพิ่ม/ลบ heading หรือ bullet
6. ตอบเฉพาะ markdown content ที่แก้แล้ว ไม่มีคำอธิบายเพิ่ม"""

_USER_TMPL = """เพิ่มสีใน note นี้ตามระบบสี ตอบเป็น markdown เท่านั้น:

{content}"""


# ── Ollama helpers ────────────────────────────────────────────────────────────

def _list_models() -> list[str]:
    try:
        req = urllib.request.Request(f"{OLLAMA_URL}/api/tags")
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []


def _ollama_generate(model: str, content: str, timeout: int = 120) -> str:
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user",   "content": _USER_TMPL.format(content=content)},
        ],
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 4096},
    }).encode()

    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    return data["message"]["content"].strip()


# ── Note processing ───────────────────────────────────────────────────────────

def _split_frontmatter(text: str) -> tuple[str, str]:
    """แยก YAML frontmatter ออกจาก body"""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            fm   = text[:end + 4]
            body = text[end + 4:]
            return fm, body
    return "", text


def _already_colorized(text: str) -> bool:
    return 'class="must-know"' in text or 'class="management"' in text


def _split_sections(body: str) -> list[str]:
    """แบ่ง body เป็น sections ตาม heading เพื่อส่งทีละก้อน"""
    parts = re.split(r'(?m)^(#{1,3} .+)$', body)
    sections, cur = [], ""
    for p in parts:
        cur += p
        if len(cur.encode()) > 3000:   # ~3KB ต่อ section
            sections.append(cur)
            cur = ""
    if cur.strip():
        sections.append(cur)
    return sections or [body]


def colorize_note(path: Path, model: str, dry_run: bool) -> tuple[str, str]:
    """
    Returns (status, message)
    status: "ok" | "skip" | "error"
    """
    try:
        original = path.read_text(encoding="utf-8", errors="replace")

        if _already_colorized(original):
            return "skip", "มีสีอยู่แล้ว"

        if len(original.encode()) < _MIN_SIZE_BYTES:
            return "skip", "ไฟล์เล็กเกิน"

        frontmatter, body = _split_frontmatter(original)

        # ถ้า body ใหญ่ → แบ่ง section
        if len(body.encode()) > _MAX_SIZE_BYTES:
            sections    = _split_sections(body)
            colored_sections = []
            for sec in sections:
                if len(sec.strip()) < 50:
                    colored_sections.append(sec)
                    continue
                result = _ollama_generate(model, sec)
                colored_sections.append(result)
            colored_body = "".join(colored_sections)
        else:
            colored_body = _ollama_generate(model, body)

        result_text = frontmatter + colored_body

        if dry_run:
            # แสดง diff summary
            added = result_text.count('<span class=') - original.count('<span class=')
            return "ok", f"[dry-run] จะเพิ่ม {added} span tags"

        # Backup ก่อนเขียน
        rel = path.relative_to(OBSIDIAN_PATH)
        backup_path = BACKUP_DIR / rel
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup_path)

        path.write_text(result_text, encoding="utf-8")
        added = result_text.count('<span class=') - original.count('<span class=')
        return "ok", f"+{added} spans"

    except urllib.error.URLError as e:
        return "error", f"Ollama ไม่ตอบสนอง: {e}"
    except Exception as e:
        return "error", str(e)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Colorize Obsidian notes with local LLM")
    ap.add_argument("--folder",  default="",             help="เฉพาะ folder นี้ (ใส่ชื่อ subfolder)")
    ap.add_argument("--model",   default="",             help="Ollama model (default: auto)")
    ap.add_argument("--workers", type=int, default=2,    help="Parallel workers (default: 2)")
    ap.add_argument("--dry-run", action="store_true",    help="Preview ไม่เขียนทับ")
    ap.add_argument("--force",   action="store_true",    help="ใส่สีซ้ำแม้มีอยู่แล้ว")
    args = ap.parse_args()

    # ── เช็ค Ollama ──
    models = _list_models()
    if not models:
        print("[ERROR] Ollama ไม่ทำงาน หรือไม่มี model\n  รัน: ollama serve")
        sys.exit(1)

    model = args.model or models[0]
    if model not in models:
        print(f"[ERROR] ไม่พบ model '{model}'\nมี: {models}")
        sys.exit(1)
    print(f"[colorize] model: {model} | workers: {args.workers} | dry-run: {args.dry_run}")

    # ── หา .md files ──
    search_root = OBSIDIAN_PATH / args.folder if args.folder else OBSIDIAN_PATH
    if not search_root.exists():
        print(f"[ERROR] ไม่พบ folder: {search_root}")
        sys.exit(1)

    all_files = [
        p for p in search_root.rglob("*.md")
        if not any(skip in p.parts for skip in _SKIP_FOLDERS)
        and p.stat().st_size >= _MIN_SIZE_BYTES
    ]

    if not args.force:
        # กรองที่มีสีอยู่แล้วออก
        all_files = [p for p in all_files
                     if not _already_colorized(p.read_text(encoding="utf-8", errors="replace"))]

    total = len(all_files)
    if total == 0:
        print("ไม่มีไฟล์ที่ต้องใส่สี (ทุกไฟล์มีสีอยู่แล้วหรือเล็กเกิน)")
        return

    print(f"[colorize] พบ {total} ไฟล์ → เริ่มประมวลผล...\n")
    if not args.dry_run:
        print(f"  [backup] จะ backup ไว้ที่ {BACKUP_DIR}\n")

    # ── Process ──
    done = ok = skipped = errors = 0
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {
            ex.submit(colorize_note, p, model, args.dry_run): p
            for p in all_files
        }
        for fut in as_completed(futures):
            path  = futures[fut]
            done += 1
            rel   = path.relative_to(OBSIDIAN_PATH)
            status, msg = fut.result()

            if status == "ok":
                ok += 1
                print(f"  ✅ [{done}/{total}] {rel} — {msg}")
            elif status == "skip":
                skipped += 1
            else:
                errors += 1
                print(f"  ❌ [{done}/{total}] {rel} — {msg}")

    elapsed = time.time() - t0
    print(f"\n{'─'*50}")
    print(f"เสร็จ: {ok} ✅ | ข้าม: {skipped} | error: {errors} | ใช้เวลา {elapsed:.0f}s")
    if not args.dry_run and ok > 0:
        print(f"Backup อยู่ที่: {BACKUP_DIR}")


if __name__ == "__main__":
    main()
