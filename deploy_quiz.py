"""Copy the MCQ practice app into the MedGuideApp2 web repo.

The bank is generated (gitignored here, committed there), so it has to be
pushed across after every rebuild or the website silently serves a stale
`built_at`. Run this right after build_quiz_bank.py.

    python build_quiz_bank.py
    python deploy_quiz.py            # copy + show what changed
    python deploy_quiz.py --commit   # ...and commit in the web repo

Set MEDGUIDE_REPO_PATH in .env to override the default location.
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

SRC = Path("quiz")
DEST_REPO = Path(os.getenv("MEDGUIDE_REPO_PATH", r"C:\Users\ASUS\Documents\MedGuideApp2"))
DEST = DEST_REPO / "public" / "quiz"

FILES = [
    ("practice.html", "practice.html"),
    ("data/quiz_bank.js", "data/quiz_bank.js"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true",
                    help="also git-commit the copied files in the web repo (never pushes)")
    args = ap.parse_args()

    if not DEST_REPO.is_dir():
        print(f"[ERROR] web repo not found: {DEST_REPO}")
        print("        clone it, or set MEDGUIDE_REPO_PATH in .env")
        return 1

    for src_rel, dest_rel in FILES:
        src = SRC / src_rel
        if not src.is_file():
            print(f"[ERROR] missing {src} — run build_quiz_bank.py first")
            return 1
        dest = DEST / dest_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        print(f"[deploy] {src}  ->  {dest}  ({src.stat().st_size / 1048576:.2f} MB)")

    status = subprocess.run(["git", "status", "--short", "public/quiz"],
                            cwd=DEST_REPO, capture_output=True, text=True)
    if not status.stdout.strip():
        print("[deploy] nothing changed — the web repo already has this build")
        return 0
    print("\n[deploy] changed in web repo:")
    print(status.stdout.rstrip())

    if not args.commit:
        print("\n[deploy] not committed (pass --commit). Push is always left to you.")
        return 0

    subprocess.run(["git", "add", "public/quiz"], cwd=DEST_REPO, check=True)
    subprocess.run(["git", "commit", "-m", "Update MCQ practice bank"],
                   cwd=DEST_REPO, check=True)
    print("[deploy] committed in the web repo — review, then push yourself")
    return 0


if __name__ == "__main__":
    sys.exit(main())
