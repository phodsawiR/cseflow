"""
Commit answer corrections from quiz/practice.html back into the vault quiz notes.

The notes under `Obsidian vault/05 - Exam Scope/Quiz/` are the source of truth for
the practice bank. The app can only park a correction in localStorage, so this
script takes the app's exported fixes and rewrites the real answer line in every
note that carries the question — 343 Q-IDs appear in more than one system file, so
a fix has to land in all of them or the next rebuild will disagree with itself.

After the answer line it leaves an audit marker that build_quiz_bank.py picks up:

    > ✏️ **เฉลยแก้เอง (2026-07-26):** ต้นฉบับตอบ B → D — <เหตุผล>

Three answer-line dialects are handled:
    > คำตอบ: **B**            main MCQ cards
    > **เฉลย: B**             🔄 โจทย์บิด variants   (id suffix _twist)
    > **Answer: B**           G4 vignette notes      (id suffix _g4)

Dry-run by default — nothing is written until you pass --apply.

Usage:
    python apply_answer_fixes.py --fixes mcq_answer_fixes_20260726.json
    python apply_answer_fixes.py --fixes fixes.json --apply
"""
import argparse
import datetime
import json
import os
import re
import shutil
import sys

VAULT = os.getenv("OBSIDIAN_VAULT_PATH", r"C:/Users/ASUS/Documents/Obsidian vault")
QUIZ_DIR = os.path.join(VAULT, "05 - Exam Scope", "Quiz")

RE_CARD_HEAD = re.compile(r"^### ข้อ \d+ — (.*)$")
RE_QID = re.compile(r"Q\d{4}_\d+")
RE_MODELED = re.compile(r"^Modeled from:\s*(.*)$")
RE_SPAN = re.compile(r"</?span[^>]*>")

RE_ANS_MAIN = re.compile(r"^(>\s*คำตอบ:\s*\*\*)([A-Ea-e])(\*\*.*)$")
RE_ANS_TWIST = re.compile(r"^(>\s*\*\*เฉลย:\s*)([A-Ea-e])(\*\*.*)$")
RE_ANS_G4 = re.compile(r"^(>\s*\*\*Answer:\s*)(.+?)(\*\*.*)$")

MARKER = "✏️ **เฉลยแก้เอง"
RE_MARKER = re.compile(r"^>\s*✏️ \*\*เฉลยแก้เอง")


def load_fixes(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    # accept either the app's full stats export or a bare {qid: {...}} map
    fixes = data.get("fixes", data)
    out = {}
    for qid, v in fixes.items():
        if isinstance(v, str):
            v = {"answer": v}
        ans = (v.get("answer") or "").strip().upper()
        if not re.match(r"^[A-E]$", ans):
            print("  ! skipping %s — bad answer %r" % (qid, v.get("answer")))
            continue
        out[qid] = {"answer": ans, "reason": (v.get("reason") or "").strip(),
                    "was": (v.get("was") or "").strip().upper()}
    return out


def card_ids_for(header, pending_modeled):
    """Ids a card header maps to. G4 notes label cards by disease, not Q-ID."""
    qids = RE_QID.findall(header)
    if qids:
        return qids[0]
    if pending_modeled:
        return pending_modeled + "_g4"
    return None


def process_file(path, fixes, today):
    """Return (new_lines, [(qid, old, new)]) — changes made in this file."""
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().split("\n")

    out = []
    changed = []
    card = None            # current card id (without suffix)
    in_twist = False       # inside a 🔄 โจทย์บิด callout
    i = 0
    while i < len(lines):
        ln = lines[i]

        head = RE_CARD_HEAD.match(ln)
        if head:
            card = card_ids_for(head.group(1), None)
            if card is None:
                # G4 style: the Q-ID lives on the 'Modeled from:' line below
                for la in lines[i + 1:i + 6]:
                    m = RE_MODELED.match(la)
                    if m:
                        ids = RE_QID.findall(m.group(1))
                        if ids:
                            card = ids[0] + "_g4"
                        break
            in_twist = False
            out.append(ln)
            i += 1
            continue

        if ln.startswith("> [!question]- 🔄 โจทย์บิด"):
            in_twist = True
        elif ln.startswith("> [!"):
            in_twist = False          # a different callout took over
        elif in_twist and not ln.startswith(">"):
            in_twist = False          # quoting stopped, so the callout ended

        if card:
            target_id = card + "_twist" if in_twist else card
            # G4 cards already carry the _g4 suffix in `card`
            fix = fixes.get(target_id)
            m = None
            kind = None
            if fix:
                for rx, k in ((RE_ANS_MAIN, "main"), (RE_ANS_TWIST, "twist"), (RE_ANS_G4, "g4")):
                    m = rx.match(ln)
                    if m:
                        kind = k
                        break
                if m and ((kind == "twist") == in_twist):
                    old_raw = m.group(2)
                    old = RE_SPAN.sub("", old_raw).strip().upper()
                    new = fix["answer"]
                    if old != new:
                        out.append(m.group(1) + new + m.group(3))
                        changed.append((target_id, old, new))
                        # replace an existing marker rather than stacking them
                        j = i + 1
                        if j < len(lines) and RE_MARKER.match(lines[j]):
                            i = j
                        note = "> %s (%s):** ต้นฉบับตอบ %s → %s" % (MARKER, today, old, new)
                        if fix["reason"]:
                            note += " — " + fix["reason"]
                        out.append(note)
                        i += 1
                        continue

        out.append(ln)
        i += 1
    return out, changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixes", required=True, help="JSON exported from practice.html")
    ap.add_argument("--quiz-dir", default=QUIZ_DIR)
    ap.add_argument("--apply", action="store_true", help="write the notes (default: dry run)")
    ap.add_argument("--no-backup", action="store_true", help="skip the .bak copy")
    args = ap.parse_args()

    if not os.path.isdir(args.quiz_dir):
        sys.exit("quiz dir not found: %s" % args.quiz_dir)
    fixes = load_fixes(args.fixes)
    if not fixes:
        sys.exit("no usable fixes in %s" % args.fixes)
    print("%d fix(es) to apply: %s" % (len(fixes), ", ".join(sorted(fixes))))

    today = datetime.date.today().isoformat()
    files = sorted(f for f in os.listdir(args.quiz_dir)
                   if f.startswith("quiz_") and f.endswith(".md"))
    total = 0
    hit_ids = set()
    for fname in files:
        path = os.path.join(args.quiz_dir, fname)
        new_lines, changed = process_file(path, fixes, today)
        if not changed:
            continue
        total += len(changed)
        hit_ids.update(qid for qid, _, _ in changed)
        print("\n%s" % fname)
        for qid, old, new in changed:
            print("  %-22s %s → %s" % (qid, old, new))
        if args.apply:
            if not args.no_backup:
                shutil.copy2(path, path + ".bak")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(new_lines))

    missing = sorted(set(fixes) - hit_ids)
    print("\n%d answer line(s) rewritten across %d file(s)" % (total, len(files)))
    if missing:
        print("NOT FOUND (already at the target answer, or unknown id):")
        for qid in missing:
            print("  " + qid)
    if args.apply:
        print("\nnotes updated%s. Now run:  python build_quiz_bank.py"
              % ("" if args.no_backup else " (.bak copies kept alongside)"))
    else:
        print("\nDRY RUN — nothing written. Re-run with --apply to commit.")


if __name__ == "__main__":
    main()
