"""
Update existing Obsidian notes — เพิ่ม "Exam Context" section จากข้อสอบจริงใน exam_kb
ไม่ใช้ LLM/API — grounded 100% จากข้อมูลจริง

Usage:
  python quiz/update_notes.py                     # อัพเดททุก note ที่มีข้อสอบ
  python quiz/update_notes.py --disease "Stroke"  # โรคเดียว
  python quiz/update_notes.py --dry-run           # ดูตัวอย่างโดยไม่บันทึก
  python quiz/update_notes.py --min-questions 1   # ขั้นต่ำกี่ข้อถึงจะ append
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

VAULT      = Path(os.getenv("OBSIDIAN_VAULT_PATH", r"C:\Users\ASUS\Documents\Obsidian vault"))
KB_PATH    = Path(os.getenv("EXAM_KB_PATH", "./examflow/exam_kb.json"))
SECTION_TAG = "<!-- exam-focus-auto -->"


# ── Load exam_kb ──────────────────────────────────────────────────────────────

def _load_kb() -> list[dict]:
    data = json.loads(KB_PATH.read_text(encoding="utf-8"))
    return data["questions"]


# ── Find questions for a disease ──────────────────────────────────────────────

def _search_questions(kb: list[dict], disease: str) -> list[dict]:
    """Find exam_kb questions related to the disease by full-text search."""
    terms = [t.strip().lower() for t in re.split(r"[\s/&]+", disease) if len(t.strip()) > 2]
    results = []
    seen_texts = set()

    for q in kb:
        text = " ".join([
            str(q.get("question_text", "")),
            str(q.get("answer_explanation", "")),
            str(q.get("disease_tags", "")),
            str(q.get("system_tags", "")),
        ]).lower()

        if any(t in text for t in terms):
            # dedup by first 80 chars of question
            key = str(q.get("question_text", ""))[:80].strip()
            if key and key not in seen_texts:
                seen_texts.add(key)
                results.append(q)

    return results


# ── Format exam section ───────────────────────────────────────────────────────

def _normalize_choices(choices) -> dict:
    """Convert list-format choices to dict {A: text, B: text, ...}."""
    if isinstance(choices, dict):
        return choices
    if isinstance(choices, list):
        letters = "ABCDE"
        return {letters[i]: str(v) for i, v in enumerate(choices) if i < len(letters)}
    return {}


def _letter_to_text(choices, letter: str) -> str:
    if not letter or not choices:
        return ""
    c = _normalize_choices(choices)
    return str(c.get(letter.upper().strip(), "")).strip()


def _format_section(disease: str, questions: list[dict]) -> str:
    lines = [
        f"<!-- exam-focus-auto -->",
        f"## 🎯 Exam Context — จากข้อสอบจริง",
        "",
        f"> ข้อสอบที่เกี่ยวข้อง: **{len(questions)} ข้อ**",
        "",
    ]

    for i, q in enumerate(questions, 1):
        qtext   = str(q.get("question_text", "")).strip()
        choices = q.get("choices") or {}
        answer  = str(q.get("answer_key") or "").strip().upper()
        explain = str(q.get("answer_explanation", "")).strip()
        qid     = q.get("question_id") or ""
        pattern = q.get("question_pattern", "")
        topic   = q.get("topic_type", "")

        if not qtext:
            continue

        id_str = f" `{qid}`" if qid else ""
        lines.append(f"### ข้อ {i}{id_str}")
        if topic or pattern:
            tags = " · ".join(filter(None, [topic, pattern]))
            lines.append(f"*{tags}*")
        lines.append("")

        # Question stem (ย่อถ้ายาวเกิน)
        q_display = qtext if len(qtext) <= 300 else qtext[:300] + "…"
        lines.append(f"> {q_display}")
        lines.append("")

        # Choices
        choices_dict = _normalize_choices(choices)
        if choices_dict:
            for letter in ["A", "B", "C", "D", "E"]:
                val = choices_dict.get(letter, "")
                if not val:
                    continue
                marker = "✅" if letter == answer else "  "
                lines.append(f"{marker} **{letter}.** {val}")
            lines.append("")

        # Answer + Explanation
        if answer:
            ans_text = _letter_to_text(choices, answer)
            lines.append(f"**คำตอบ: {answer}**" + (f" — {ans_text}" if ans_text else ""))

        if explain:
            lines.append("")
            # ตัด explain ถ้ายาวมาก
            exp_display = explain if len(explain) <= 500 else explain[:500] + "…"
            lines.append(f"> [!note] Explanation")
            for part in exp_display.split("\n"):
                if part.strip():
                    lines.append(f"> {part.strip()}")

        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


# ── Note finder ───────────────────────────────────────────────────────────────

def _find_note(disease: str) -> Path | None:
    def norm(s: str) -> str:
        return re.sub(r"[\s_\-]+", " ", s).lower().strip()

    d = norm(disease)
    candidates = [
        f for f in VAULT.rglob("*.md")
        if not re.match(r"disease_.+_\d{8}", f.stem)
        and not f.stem.lower().startswith("scope")
        and not f.stem.startswith("_MOC")
        and (d in norm(f.stem) or norm(f.stem) in d)
    ]
    if not candidates:
        return None
    exact = [f for f in candidates if norm(f.stem) == d]
    return exact[0] if exact else candidates[0]


# ── Scope loader ──────────────────────────────────────────────────────────────

def _load_scope() -> list[str]:
    scope_files = sorted(VAULT.rglob("scope*.md"))
    diseases = []
    for sf in scope_files:
        text = sf.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r"\|\s*\*\*(.+?)\*\*\s*\|", text):
            d = m.group(1).strip()
            if d not in diseases:
                diseases.append(d)
        for m in re.finditer(r"-\s*\*\*(.+?)\*\*", text):
            d = m.group(1).strip()
            if d and d not in diseases:
                diseases.append(d)
    return diseases


# ── Main ──────────────────────────────────────────────────────────────────────

def run(disease_filter: str | None, dry_run: bool, min_questions: int = 1):
    kb       = _load_kb()
    diseases = _load_scope()
    updated = skipped = 0

    for disease in diseases:
        if disease_filter and disease_filter.lower() not in disease.lower():
            continue

        note_path = _find_note(disease)
        if not note_path:
            print(f"  – {disease}: ไม่มี note")
            continue

        existing = note_path.read_text(encoding="utf-8", errors="ignore")

        if SECTION_TAG in existing:
            print(f"  ✓ {disease} — already patched, skip")
            skipped += 1
            continue

        questions = _search_questions(kb, disease)
        if len(questions) < min_questions:
            print(f"  – {disease}: พบ {len(questions)} ข้อ (ต่ำกว่า threshold), skip")
            skipped += 1
            continue

        print(f"\n  {disease} — {len(questions)} ข้อ → {note_path.name}")

        section = _format_section(disease, questions)

        if dry_run:
            print("  [dry-run] preview:")
            print("  " + "\n  ".join(section.splitlines()[:30]))
            print("  ...")
        else:
            with open(note_path, "a", encoding="utf-8") as f:
                f.write(f"\n\n---\n{section}\n")
            print(f"  → appended {len(questions)} questions to {note_path.name}")

        updated += 1

    print(f"\n[update] Done — {updated} updated, {skipped} skipped")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--disease",       help="Filter disease name")
    parser.add_argument("--dry-run",       action="store_true")
    parser.add_argument("--min-questions", type=int, default=1,
                        help="Minimum exam questions required to update (default 1)")
    args = parser.parse_args()
    run(args.disease, args.dry_run, args.min_questions)
