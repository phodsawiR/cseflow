"""
add_review_tracker.py — เพิ่ม checklist "รอบที่อ่าน" ให้ note ใน Obsidian vault
ไม่ต้อง LLM — แค่ text manipulation รันเร็วมาก คู่กับ sync_review_count.py

Usage:
  python add_review_tracker.py                          # ทุก folder
  python add_review_tracker.py --folder "02 - Diseases" # เฉพาะ folder
  python add_review_tracker.py --rounds 5                # จำนวน checkbox (default 5)
  python add_review_tracker.py --dry-run                 # preview ไม่เขียน
"""
import argparse
import re
import sys
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

_FM_RE      = re.compile(r'^---\n(.*?)\n---\n', re.DOTALL)
_TRACKER_RE = re.compile(r'^## 📖 Review Tracker\n', re.MULTILINE)


def _has_tracker(content: str) -> bool:
    return bool(_TRACKER_RE.search(content))


def _build_tracker(rounds: int) -> str:
    boxes = "\n".join(f"- [ ] รอบที่ {i}" for i in range(1, rounds + 1))
    return f"## 📖 Review Tracker\n{boxes}\n\n"


def _inject(content: str, rounds: int) -> str:
    tracker = _build_tracker(rounds)

    m = _FM_RE.match(content)
    if m:
        fm_body = m.group(1)
        if "review_count:" not in fm_body:
            fm_body = fm_body + "\nreview_count: 0"
        new_fm = f"---\n{fm_body}\n---\n\n"
        rest = content[m.end():].lstrip("\n")
        return new_fm + tracker + rest
    else:
        new_fm = f"---\nreview_count: 0\n---\n\n"
        return new_fm + tracker + content


def main():
    ap = argparse.ArgumentParser(description="Add review-count checklist to Obsidian notes")
    ap.add_argument("--folder",  default="",          help="เฉพาะ subfolder นี้")
    ap.add_argument("--rounds",  type=int, default=5,  help="จำนวนรอบ checkbox (default 5)")
    ap.add_argument("--dry-run", action="store_true",  help="Preview ไม่เขียน")
    args = ap.parse_args()

    root = OBSIDIAN_PATH / args.folder if args.folder else OBSIDIAN_PATH
    if not root.exists():
        print(f"[ERROR] ไม่พบ: {root}"); sys.exit(1)

    files = [
        p for p in root.rglob("*.md")
        if not any(s in p.parts for s in _SKIP_FOLDERS)
        and p.name not in _SKIP_FILES
        and not p.stem.startswith("_MOC")
        and p.stat().st_size > 100
    ]

    total = len(files)
    added = skipped = 0
    print(f"[add_review_tracker] พบ {total} ไฟล์ | rounds={args.rounds} | dry-run: {args.dry_run}\n")

    for path in files:
        content = path.read_text(encoding="utf-8", errors="replace")
        if _has_tracker(content):
            skipped += 1
            continue

        new_content = _inject(content, args.rounds)
        rel = path.relative_to(OBSIDIAN_PATH)

        if args.dry_run:
            print(f"  + {rel}")
        else:
            path.write_text(new_content, encoding="utf-8")

        added += 1

    print(f"\n{'─'*50}")
    action = "จะเพิ่ม" if args.dry_run else "เพิ่มแล้ว"
    print(f"{action}: {added} ✅ | มีอยู่แล้ว: {skipped} ⏭️")


if __name__ == "__main__":
    main()
