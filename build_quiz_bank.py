"""
Parse the per-system quiz notes into a practice bank for quiz/practice.html.

Reads `Obsidian vault/05 - Exam Scope/Quiz/quiz_*.md` (built by build_system_quiz.py
and friends) and emits `quiz/data/quiz_bank.js` — a plain JS assignment so the
practice app can be opened straight from file:// without tripping CORS.

Scope for now is MCQ only: "ส่วนที่ 1 — MCQ" plus every 🔄 โจทย์บิด variant found
there. MEQ/อัตนัย (ส่วนที่ 2) and the corrupted originals (ส่วนที่ 3) are skipped —
they need a different answering flow.

Deterministic, no LLM, free to re-run. Card ids are the real exam Q-IDs so the
app's localStorage progress survives a rebuild.

Usage:
    python build_quiz_bank.py
    python build_quiz_bank.py --report      # per-file parse audit
"""
import argparse
import datetime
import json
import os
import re
import sys
from collections import Counter

VAULT = os.getenv("OBSIDIAN_VAULT_PATH", r"C:/Users/ASUS/Documents/Obsidian vault")
QUIZ_DIR = os.path.join(VAULT, "05 - Exam Scope", "Quiz")
OUT_PATH = os.path.join("quiz", "data", "quiz_bank.js")

QID = r"Q\d{4}_\d+"

# --- headers -----------------------------------------------------------------
RE_CARD_SPLIT = re.compile(r"^### ข้อ \d+ — ", re.M)
RE_SECTION = re.compile(r"^## ส่วนที่ (\d)", re.M)
# Source filenames contain their own parentheses ('01.1_MCQs (2).pdf'), so anchor
# on the extension rather than the first ')'.
RE_HEAD_META = re.compile(r"\((MCQ|MEQ), (\d{4}), (.+?\.pdf|.+?)\)(?:\s|$|\s*\+)")
RE_QID = re.compile(QID)

# --- in-card markers ---------------------------------------------------------
# Label may be followed by no space ('- A.AZT') or repeated dots ('- A...text').
# Requiring \S in group 2 keeps pure placeholders ('- A...') out of the bank.
RE_CHOICE = re.compile(r"^- ?([A-Ea-e])[.):]+\s*(\S.*)$")
RE_SPAN = re.compile(r"</?span[^>]*>")
RE_CHOICE_BARE = re.compile(r"^([A-E])\.\s+(.*)$")  # Skin_G4 style
# Dot-less OCR fallback ('- A Mitral Valve Prolapse'); gated in parse_choices().
RE_CHOICE_LOOSE = re.compile(r"^- ([A-Ea-e])\s+(?!\d)(\S.*)$")
RE_ANSWER = re.compile(r"^>\s*คำตอบ:\s*\*\*([A-Ea-e])\*\*")
RE_ANSWER_EN = re.compile(r"^>\s*\*\*Answer:\s*([A-Ea-e])\*\*")
RE_REPEAT_INLINE = re.compile(r"^\*\*ออกซ้ำ (\d+) ครั้ง:\*\*\s*(.*)$", re.M)
RE_REPEAT_CALLOUT = re.compile(
    r"^> \[!tip\] 🔥 \*\*ออกซ้ำ (\d+) ครั้ง.*$\n^> (.*)$", re.M
)
RE_CONFLICT = re.compile(r"^> \[!warning\] ข้อซ้ำให้เฉลยไม่ตรงกัน \(([^)]*)\)", re.M)
RE_OCR_WARN = re.compile(r"^> \[!warning\] ต้นฉบับ OCR เพี้ยน(.*)$", re.M)
RE_MODELED = re.compile(r"^Modeled from:\s*(.*)$", re.M)

CALLOUT_DX = "> [!dx]-"
CALLOUT_TWIST = "> [!question]- 🔄 โจทย์บิด"
CALLOUT_DANGER = "> [!danger] ⚠️ **เฉลยเดิมล้าสมัยแล้ว"

MARK_TRAP = "⚠️ **กับดัก:**"
MARK_USMLE = "🔬 **วิเคราะห์แบบ USMLE**"
MARK_VERIFIED = "✅ **ตรวจกับไกด์ไลน์ปัจจุบันแล้ว"
MARK_GUIDELINE = "เช็ค guideline ล่าสุดก่อนเชื่อ"
MARK_TWIST_WHERE = "🔀 **บิดตรงไหน:**"
MARK_TWIST_WHY = "💡 **ทำไมคำตอบเปลี่ยน:**"
# written by apply_answer_fixes.py — a human overruled the transcribed answer
RE_FIXED = re.compile(r"^✏️ \*\*เฉลยแก้เอง \(([\d-]+)\):\*\*\s*ต้นฉบับตอบ ([A-E]) → ([A-E])(?: — (.*))?$")

# Choices that are pure OCR garbage — never show them as a real option.
RE_GARBLED = re.compile(r"[ก-๙]")  # Thai chars inside a Latin-only drug/term list


def system_from_filename(fname: str) -> str:
    stem = re.sub(r"^quiz_", "", fname)
    stem = re.sub(r"_complete\.md$|\.md$", "", stem)
    stem = re.sub(r"_\d{8}$", "", stem)
    stem = re.sub(r"_G4_vignettes$", " (G4 vignettes)", stem)
    stem = re.sub(r"_random30$", " (random 30)", stem)
    return stem.replace("_", " ")


def unquote(lines):
    """Strip one level of blockquote '>' from callout body lines."""
    out = []
    for ln in lines:
        if ln.startswith("> "):
            out.append(ln[2:])
        elif ln == ">":
            out.append("")
        else:
            out.append(ln)
    return out


def _quoting_resumes(lines, i):
    """True if a '>' line follows position i before the card/paragraph ends.

    Used to repair source notes that dropped the '> ' from a wrapped explanation
    line — that ends the callout early and leaks the answer into the question
    stem (e.g. Q2026_300 in quiz_Dermatology_random30.md).
    """
    for la in lines[i:]:
        if la.strip() in ("", "---") or la.startswith("###"):
            return False
        if la.startswith(">"):
            return True
    return False


def split_callouts(block: str):
    """Return (plain_lines, [callout_bodies]) for a card block.

    A callout is a line starting with '> [!' plus every following '>' line. Blank
    lines continue it only while the quoting picks back up; a fresh '> [!' always
    starts a new callout.
    """
    lines = block.split("\n")
    plain, callouts = [], []
    i = 0
    while i < len(lines):
        if not lines[i].startswith("> [!"):
            plain.append(lines[i])
            i += 1
            continue
        body = [lines[i]]
        i += 1
        while i < len(lines):
            ln = lines[i]
            if ln == "":
                nxt = lines[i + 1] if i + 1 < len(lines) else ""
                if nxt.startswith(">") and not nxt.startswith("> [!"):
                    body.append(">")
                    i += 1
                    continue
                break
            if ln.startswith("> [!"):
                break
            if ln.startswith(">"):
                body.append(ln)
                i += 1
                continue
            if ln.strip() != "---" and not ln.startswith("###") \
                    and _quoting_resumes(lines, i + 1):
                body.append("> " + ln)
                i += 1
                continue
            break
        callouts.append(body)
    return plain, callouts


def _collect(lines, patterns):
    choices, rest = {}, []
    for ln in lines:
        s = ln.strip()
        m = None
        for pat in patterns:
            m = pat.match(s)
            if m:
                break
        if m:
            choices[m.group(1).upper()] = m.group(2).strip()
        else:
            rest.append(ln)
    return choices, rest


def parse_choices(lines):
    """Pull choice lines out of a list; returns (choices, remaining_lines).

    Two passes: the dotted form first ('- A. text'), then a loose dot-less
    fallback ('- A text') only if the strict pass found nothing. The fallback is
    gated because '- A 45-year-old woman...' is a stem, not a choice.
    """
    choices, rest = _collect(lines, (RE_CHOICE, RE_CHOICE_BARE))
    if len(choices) >= 2:
        return choices, rest
    loose, rest2 = _collect(lines, (RE_CHOICE_LOOSE,))
    if len(loose) >= 3:
        return loose, rest2
    positional, rest3 = _positional_choices(lines)
    if positional:
        return positional, rest3
    return choices, rest


def _positional_choices(lines):
    """Last resort: OCR dropped the A-E labels entirely, leaving a bare run of
    option lines (seen in quiz_Dermatology_random30.md). Accept a trailing run of
    4-5 short label-less lines and letter them by position.
    """
    # '---' separators are layout, not content — treat them as blank
    norm = ["" if ln.strip() in ("", "---") else ln for ln in lines]
    lines = norm
    idx = [i for i, ln in enumerate(lines) if ln.strip()]
    if len(idx) < 5:
        return None, lines
    # walk back over the trailing contiguous block of non-empty lines
    run = [idx[-1]]
    for i in reversed(idx[:-1]):
        if i == run[0] - 1:
            run.insert(0, i)
        else:
            break
    if not 4 <= len(run) <= 5:
        return None, lines
    if run[0] == 0:  # nothing left over for the question stem
        return None, lines
    opts = [lines[i].strip() for i in run]
    for o in opts:
        if len(o) > 80 or o[0] in "->*#|" or o.endswith(("?", "ใด", "อะไร")):
            return None, lines
    letters = "ABCDE"
    choices = {letters[n]: o for n, o in enumerate(opts)}
    rest = [ln for i, ln in enumerate(lines) if i not in set(run)]
    return choices, rest


def clean_text(s: str) -> str:
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def parse_dx(body):
    """Parse a '[!dx]- เฉลย' callout body into answer / explanation / trap / usmle."""
    body = unquote(body[1:])  # drop the '> [!dx]- ...' header line
    out = {
        "answer": None,
        "explanation": "",
        "trap": "",
        "usmle": "",
        "verified": None,
        "note": "",
        "fixed": None,
    }
    buf_expl, buf_trap, buf_usmle = [], [], []
    target = buf_expl
    for ln in body:
        raw = ln.strip()
        # G4 notes wrap the answer letter in a <span> — strip tags before matching
        bare = RE_SPAN.sub("", raw)
        m = RE_ANSWER.match("> " + bare) or RE_ANSWER_EN.match("> " + bare)
        if m:
            out["answer"] = m.group(1).upper()
            continue
        fm = RE_FIXED.match(bare)
        if fm:
            out["fixed"] = {"at": fm.group(1), "was": fm.group(2),
                            "to": fm.group(3), "reason": fm.group(4) or ""}
            continue
        if raw.startswith(MARK_TRAP):
            target = buf_trap
            raw = raw[len(MARK_TRAP):].strip()
        elif raw.startswith(MARK_USMLE):
            target = buf_usmle
            continue
        elif raw.startswith(MARK_VERIFIED):
            out["verified"] = raw[len(MARK_VERIFIED):].strip(" *()")
            continue
        elif raw.startswith("*(ต้นฉบับ"):
            out["note"] = raw.strip("*()")
            continue
        target.append(raw)
    out["explanation"] = clean_text("\n".join(buf_expl))
    out["trap"] = clean_text("\n".join(buf_trap))
    out["usmle"] = clean_text("\n".join(buf_usmle))
    return out


def parse_twist(body):
    """Parse a '[!question]- 🔄 โจทย์บิด' callout into a standalone MCQ."""
    body = unquote(body[1:])
    choices, rest = parse_choices(body)
    answer, where, why = None, "", ""
    fixed = None
    qlines = []
    for ln in rest:
        raw = RE_SPAN.sub("", ln.strip())
        m = re.match(r"^\*\*เฉลย:\s*([A-Ea-e])\*\*", raw)
        if m:
            answer = m.group(1).upper()
            continue
        fm = RE_FIXED.match(raw)
        if fm:
            fixed = {"at": fm.group(1), "was": fm.group(2),
                     "to": fm.group(3), "reason": fm.group(4) or ""}
            continue
        if raw.startswith("- " + MARK_TWIST_WHERE):
            where = raw[len("- " + MARK_TWIST_WHERE):].strip()
            continue
        if raw.startswith("- " + MARK_TWIST_WHY):
            why = raw[len("- " + MARK_TWIST_WHY):].strip()
            continue
        qlines.append(raw)
    return {
        "question": clean_text("\n".join(qlines)),
        "choices": choices,
        "answer": answer,
        "twist_where": where,
        "twist_why": why,
        "fixed": fixed,
    }


def parse_card(block: str, system: str, fname: str):
    """Return (main_card_or_None, twist_card_or_None, skip_reason_or_None)."""
    header, _, rest = block.partition("\n")
    header = header.strip()

    qids = RE_QID.findall(block)
    head_qids = RE_QID.findall(header)
    modeled = RE_MODELED.search(rest)

    if head_qids:
        card_id = head_qids[0]
    elif modeled:
        m = RE_QID.findall(modeled.group(1))
        card_id = (m[0] + "_g4") if m else None
    else:
        card_id = None
    if not card_id:
        return None, None, "no Q-ID in header"

    meta = RE_HEAD_META.search(header)
    exam_type = meta.group(1) if meta else "MCQ"
    year = int(meta.group(2)) if meta else None
    source = meta.group(3) if meta else ""
    topic = ""
    tm = re.search(r"\[\[([^\]]+)\]\]", header)
    if tm:
        topic = tm.group(1)

    plain, callouts = split_callouts(rest)

    dx = None
    twist_raw = None
    outdated = ""
    for body in callouts:
        head = body[0]
        if head.startswith(CALLOUT_DX):
            dx = parse_dx(body)
        elif head.startswith(CALLOUT_TWIST):
            twist_raw = parse_twist(body)
        elif head.startswith(CALLOUT_DANGER):
            outdated = clean_text("\n".join(unquote(body[1:])))

    conflict = ""
    cm = RE_CONFLICT.search(rest)
    if cm:
        conflict = cm.group(1)
    ocr_warn = bool(RE_OCR_WARN.search(rest))

    # repeat info
    repeat_count, repeat_ids = 1, [card_id]
    rc = RE_REPEAT_CALLOUT.search(rest) or RE_REPEAT_INLINE.search(rest)
    if rc:
        repeat_count = int(rc.group(1))
        repeat_ids = RE_QID.findall(rc.group(2)) or [card_id]

    # question text = plain lines minus choices, minus the inline repeat line,
    # minus 'Modeled from:' / 'Sources:' bookkeeping and section separators
    choices, qlines = parse_choices(plain)
    keep = []
    for ln in qlines:
        s = ln.strip()
        if not s or s == "---":
            keep.append("")
            continue
        if s.startswith("**ออกซ้ำ") or s.startswith("Modeled from:"):
            continue
        if s.startswith("Sources:") or s.startswith("**Sources:"):
            continue
        if s.startswith("**Case:**") or s.startswith("**Question:**"):
            s = s.replace("**Case:**", "").replace("**Question:**", "").strip()
            if not s:
                continue
        keep.append(s)
    question = clean_text("\n".join(keep))

    if not choices:
        return None, None, "no choices"
    if not dx or not dx["answer"]:
        return None, None, "no answer"
    if dx["answer"] not in choices:
        return None, None, "answer %s not in choices" % dx["answer"]

    flags = []
    if repeat_count >= 3:
        flags.append("high_yield")
    if repeat_count >= 2:
        flags.append("repeated")
    if "🆕" in question or any("🆕" in c for c in choices.values()):
        flags.append("reconstructed")
    if outdated:
        flags.append("outdated_answer")
    if conflict:
        flags.append("conflicting_answer")
    if ocr_warn:
        flags.append("ocr_suspect")
    if dx["verified"]:
        flags.append("verified")
    if MARK_GUIDELINE in rest:
        flags.append("guideline_check")
    # Source transcription lost some options — practising these teaches the wrong
    # discrimination, so the app hides them unless asked for.
    if len(choices) < 4:
        flags.append("incomplete_choices")
    if dx["fixed"]:
        flags.append("answer_fixed")

    card = {
        "id": card_id,
        "system": system,
        "type": "mcq",
        "exam_type": exam_type,
        "year": year,
        "source": source,
        "topic": topic,
        "question": question,
        "choices": choices,
        "answer": dx["answer"],
        "explanation": dx["explanation"],
        "trap": dx["trap"],
        "usmle": dx["usmle"],
        "note": dx["note"],
        "verified": dx["verified"] or "",
        "outdated": outdated,
        "conflict": conflict,
        "fixed": dx["fixed"],
        "repeat_count": repeat_count,
        "repeat_ids": repeat_ids,
        "flags": flags,
        "file": fname,
    }

    twist = None
    if twist_raw and twist_raw["choices"] and twist_raw["answer"]:
        if twist_raw["answer"] in twist_raw["choices"]:
            tflags = ["twist", "reconstructed"]
            if "high_yield" in flags:
                tflags.append("high_yield")
            if twist_raw["fixed"]:
                tflags.append("answer_fixed")
            twist = {
                "id": card_id + "_twist",
                "system": system,
                "type": "twist",
                "exam_type": exam_type,
                "year": year,
                "source": source,
                "topic": topic,
                "parent": card_id,
                "question": twist_raw["question"],
                "choices": twist_raw["choices"],
                "answer": twist_raw["answer"],
                "explanation": twist_raw["twist_why"],
                "trap": twist_raw["twist_where"],
                "usmle": "",
                "note": "",
                "verified": "",
                "outdated": "",
                "conflict": "",
                "fixed": twist_raw["fixed"],
                "repeat_count": 1,
                "repeat_ids": [card_id],
                "flags": tflags,
                "file": fname,
            }
    return card, twist, None


def mcq_section(text: str) -> str:
    """Slice out the MCQ part. Files without 'ส่วนที่' headers are all-MCQ."""
    marks = list(RE_SECTION.finditer(text))
    if not marks:
        return text
    start = end = None
    for i, m in enumerate(marks):
        if m.group(1) == "1":
            start = m.start()
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            break
    if start is None:
        return ""
    return text[start:end]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiz-dir", default=QUIZ_DIR)
    ap.add_argument("--out", default=OUT_PATH)
    ap.add_argument("--report", action="store_true", help="print per-file audit")
    args = ap.parse_args()

    if not os.path.isdir(args.quiz_dir):
        sys.exit("quiz dir not found: %s" % args.quiz_dir)

    files = sorted(f for f in os.listdir(args.quiz_dir) if f.startswith("quiz_") and f.endswith(".md"))
    if not files:
        sys.exit("no quiz_*.md in %s" % args.quiz_dir)

    cards, skips = [], []
    per_file = []
    for fname in files:
        path = os.path.join(args.quiz_dir, fname)
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        system = system_from_filename(fname)
        section = mcq_section(text)
        blocks = RE_CARD_SPLIT.split(section)[1:]
        n_main = n_twist = 0
        file_skips = []
        for blk in blocks:
            main_card, twist, reason = parse_card(blk, system, fname)
            if reason:
                head = blk.split("\n", 1)[0][:60]
                file_skips.append((head, reason))
                continue
            cards.append(main_card)
            n_main += 1
            if twist:
                cards.append(twist)
                n_twist += 1
        skips.extend((fname, h, r) for h, r in file_skips)
        per_file.append((system, len(blocks), n_main, n_twist, len(file_skips)))

    # de-dup ids (same Q-ID tagged into two systems)
    seen = {}
    unique = []
    for c in cards:
        if c["id"] in seen:
            prev = seen[c["id"]]
            if c["system"] not in prev["systems"]:
                prev["systems"].append(c["system"])
            continue
        c["systems"] = [c["system"]]
        seen[c["id"]] = c
        unique.append(c)
    cards = unique

    systems = sorted({s for c in cards for s in c["systems"]})
    years = sorted({c["year"] for c in cards if c["year"]})
    flag_counts = Counter(f for c in cards for f in c["flags"])

    bank = {
        "meta": {
            # shown in the app so a browser serving a cached bank is obvious
            "built_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "built_from": args.quiz_dir,
            "files": len(files),
            "total": len(cards),
            "mcq": sum(1 for c in cards if c["type"] == "mcq"),
            "twist": sum(1 for c in cards if c["type"] == "twist"),
            "systems": systems,
            "years": years,
            "flags": dict(flag_counts),
        },
        "questions": cards,
    }

    out_dir = os.path.dirname(args.out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    payload = json.dumps(bank, ensure_ascii=False, separators=(",", ":"))
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write("window.QUIZ_BANK = ")
        fh.write(payload)
        fh.write(";\n")

    print("wrote %s  (%.1f MB)" % (args.out, len(payload) / 1e6))
    print("  cards: %d  (mcq %d + twist %d)  systems %d  years %s-%s"
          % (bank["meta"]["total"], bank["meta"]["mcq"], bank["meta"]["twist"],
             len(systems), years[0] if years else "?", years[-1] if years else "?"))
    print("  flags: " + ", ".join("%s=%d" % kv for kv in flag_counts.most_common()))
    print("  skipped %d card blocks" % len(skips))

    if args.report:
        print("\n%-34s %6s %6s %6s %6s" % ("system", "blocks", "mcq", "twist", "skip"))
        for system, nb, nm, nt, ns in per_file:
            print("%-34s %6d %6d %6d %6d" % (system[:34], nb, nm, nt, ns))
        reasons = Counter(r for _, _, r in skips)
        print("\nskip reasons:")
        for r, n in reasons.most_common():
            print("  %-28s %d" % (r, n))
        print("\nfirst 25 skipped blocks:")
        for fname, head, reason in skips[:25]:
            print("  [%s] %s  <- %s" % (reason, head, fname))


if __name__ == "__main__":
    main()
