"""
sync_review_count.py — นับ checkbox ที่ติ๊กแล้วใน "## 📖 Review Tracker" ของแต่ละ note
แล้วอัปเดต review_count (และ last_reviewed ถ้ามี field นี้) ใน frontmatter ให้ตรงกัน
ไม่ต้อง LLM — รันซ้ำได้เรื่อยๆ ทุกครั้งที่ติ๊ก checkbox ใน Obsidian แล้วอยากให้ frontmatter sync

Usage:
  python sync_review_count.py                          # ทุก folder
  python sync_review_count.py --folder "02 - Diseases" # เฉพาะ folder
  python sync_review_count.py --dry-run                 # preview ไม่เขียน
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

_FM_RE       = re.compile(r'^---\n(.*?)\n---\n', re.DOTALL)
_TRACKER_RE  = re.compile(r'^## 📖 Review Tracker\n((?:- \[.\].*\n?)+)', re.MULTILINE)
_CHECKED_RE  = re.compile(r'^- \[x\]', re.IGNORECASE | re.MULTILINE)
_COUNT_RE    = re.compile(r'^review_count:\s*\d*\s*$', re.MULTILINE)
_REVIEWED_RE = re.compile(r'^last_reviewed:\s*.*$', re.MULTILINE)


def _sync_one(content: str) -> tuple[str, int, bool]:
    """Returns (new_content, checked_count, changed)."""
    tracker_m = _TRACKER_RE.search(content)
    if not tracker_m:
        return content, -1, False

    checked = len(_CHECKED_RE.findall(tracker_m.group(1)))

    fm_m = _FM_RE.match(content)
    if not fm_m:
        return content, checked, False

    fm_body = fm_m.group(1)
    old_count_m = re.search(r'^review_count:\s*(\d*)\s*$', fm_body, re.MULTILINE)
    old_count = int(old_count_m.group(1)) if (old_count_m and old_count_m.group(1)) else 0

    if old_count == checked:
        return content, checked, False

    if _COUNT_RE.search(fm_body):
        fm_body = _COUNT_RE.sub(f"review_count: {checked}", fm_body, count=1)
    else:
        fm_body = fm_body + f"\nreview_count: {checked}"

    if checked > old_count and _REVIEWED_RE.search(fm_body):
        today = datetime.now().strftime("%Y-%m-%d")
        fm_body = _REVIEWED_RE.sub(f"last_reviewed: {today}", fm_body, count=1)

    new_content = f"---\n{fm_body}\n---\n" + content[fm_m.end():]
    return new_content, checked, True


def main():
    ap = argparse.ArgumentParser(description="Sync review_count frontmatter with Review Tracker checkboxes")
    ap.add_argument("--folder",  default="",         help="เฉพาะ subfolder นี้")
    ap.add_argument("--dry-run", action="store_true", help="Preview ไม่เขียน")
    args = ap.parse_args()

    root = OBSIDIAN_PATH / args.folder if args.folder else OBSIDIAN_PATH
    if not root.exists():
        print(f"[ERROR] ไม่พบ: {root}"); sys.exit(1)

    files = [
        p for p in root.rglob("*.md")
        if not any(s in p.parts for s in _SKIP_FOLDERS) and p.stat().st_size > 100
    ]

    updated = unchanged = no_tracker = 0
    print(f"[sync_review_count] พบ {len(files)} ไฟล์ | dry-run: {args.dry_run}\n")

    for path in files:
        content = path.read_text(encoding="utf-8", errors="replace")
        new_content, checked, changed = _sync_one(content)
        rel = path.relative_to(OBSIDIAN_PATH)

        if checked == -1:
            no_tracker += 1
            continue
        if not changed:
            unchanged += 1
            continue

        print(f"  ~ {rel}  → review_count: {checked}")
        if not args.dry_run:
            path.write_text(new_content, encoding="utf-8")
        updated += 1

    print(f"\n{'─'*50}")
    print(f"อัปเดต: {updated} ✅ | ไม่เปลี่ยน: {unchanged} ⏭️ | ไม่มี tracker: {no_tracker} ⏭️")


if __name__ == "__main__":
    main()
