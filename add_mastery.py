"""
add_mastery.py — เพิ่ม mastery frontmatter ให้ note เก่าใน Obsidian vault
ไม่ต้อง LLM — แค่ text manipulation รันเร็วมาก

Usage:
  python add_mastery.py                          # ทุก folder
  python add_mastery.py --folder "02 - Diseases" # เฉพาะ folder
  python add_mastery.py --dry-run                # preview ไม่เขียน
"""
import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

import os
OBSIDIAN_PATH = Path(os.getenv("OBSIDIAN_VAULT_PATH",
                                r"C:\Users\ASUS\Documents\Obsidian vault"))

_SKIP_FOLDERS = {".obsidian", ".colorize_backup", "99 - Templates",
                 "copilot", "PDF", "radiology", "images"}
_SKIP_FILES   = {"CLAUDE.md", "MOC.md", "Timeline Hub.md", "🏠 Ward Medicine Home.md"}

# ── detect note type from path/content ───────────────────────────────────────

def _detect_type(path: Path, content: str) -> tuple[str, str]:
    """Returns (type, tags_str)"""
    parts = {p.lower() for p in path.parts}
    name  = path.stem.lower()
    if "drug" in parts or "drugs" in parts or name.startswith("drug"):
        return "drug", "drug"
    if "approach" in parts or "approaches" in parts:
        return "approach", "approach"
    if "lab" in parts or "labs" in parts:
        return "lab", "lab"
    if "lecture" in parts or "lecture summaries" in " ".join(path.parts).lower():
        return "lecture", "lecture"
    if "exam scope" in " ".join(path.parts).lower() or "scope" in parts:
        return "scope", "scope, exam"
    if "vignette" in parts or "vignettes" in parts:
        return "vignette", "vignette, exam"
    if "disease" in parts or "diseases" in parts:
        return "disease", "disease"
    # fallback: infer from content
    if "#drug" in content:
        return "drug", "drug"
    if "#approach" in content:
        return "approach", "approach"
    return "note", "note"


# ── frontmatter helpers ───────────────────────────────────────────────────────

_FM_RE = re.compile(r'^---\n(.*?)\n---\n', re.DOTALL)

def _has_mastery(content: str) -> bool:
    m = _FM_RE.match(content)
    if not m:
        return False
    return "mastery:" in m.group(1)

def _inject_mastery(content: str, path: Path) -> str:
    note_type, tags = _detect_type(path, content)
    today = datetime.now().strftime("%Y-%m-%d")

    m = _FM_RE.match(content)
    if m:
        # มี frontmatter อยู่แล้ว — เพิ่ม fields เข้าไป
        fm_body = m.group(1)
        addition = f"mastery: 0\nlast_reviewed:\ncreated: {today}"
        new_fm   = f"---\n{fm_body}\n{addition}\n---\n"
        return new_fm + content[m.end():]
    else:
        # ไม่มี frontmatter — สร้างใหม่
        new_fm = (f"---\ntags: [{tags}]\ntype: {note_type}\n"
                  f"mastery: 0\nlast_reviewed:\ncreated: {today}\n---\n\n")
        return new_fm + content


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Add mastery frontmatter to Obsidian notes")
    ap.add_argument("--folder",  default="",          help="เฉพาะ subfolder นี้")
    ap.add_argument("--dry-run", action="store_true", help="Preview ไม่เขียน")
    args = ap.parse_args()

    root = OBSIDIAN_PATH / args.folder if args.folder else OBSIDIAN_PATH
    if not root.exists():
        print(f"[ERROR] ไม่พบ: {root}"); sys.exit(1)

    files = [
        p for p in root.rglob("*.md")
        if not any(s in p.parts for s in _SKIP_FOLDERS)
        and p.name not in _SKIP_FILES
        and p.stat().st_size > 100
    ]

    total = len(files)
    added = skipped = 0

    print(f"[add_mastery] พบ {total} ไฟล์ | dry-run: {args.dry_run}\n")

    for path in files:
        content = path.read_text(encoding="utf-8", errors="replace")
        if _has_mastery(content):
            skipped += 1
            continue

        new_content = _inject_mastery(content, path)
        rel = path.relative_to(OBSIDIAN_PATH)

        if args.dry_run:
            note_type, _ = _detect_type(path, content)
            print(f"  + {rel}  [{note_type}]")
        else:
            path.write_text(new_content, encoding="utf-8")

        added += 1

    print(f"\n{'─'*50}")
    action = "จะเพิ่ม" if args.dry_run else "เพิ่มแล้ว"
    print(f"{action}: {added} ✅ | มีอยู่แล้ว: {skipped} ⏭️")


if __name__ == "__main__":
    main()
