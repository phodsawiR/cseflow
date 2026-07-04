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

def _load_kb() -> tuple[list[dict], dict]:
    data = json.loads(KB_PATH.read_text(encoding="utf-8"))
    return data["questions"], data.get("disease_index", {})


# ── Find questions for a disease ──────────────────────────────────────────────

def _norm(s: str) -> str:
    return re.sub(r"[\s_\-]+", " ", s).lower().strip()


def _search_questions(kb: list[dict], disease_index: dict, disease: str) -> list[dict]:
    """Find questions via disease_index (exact then fuzzy match on disease name)."""
    q_map = {q["id"]: q for q in kb}
    d_norm = _norm(disease)

    # exact match first, then case/spacing-insensitive
    entry = disease_index.get(disease)
    if not entry:
        for key in disease_index:
            if _norm(key) == d_norm:
                entry = disease_index[key]
                break

    if not entry:
        return []

    question_ids = entry.get("question_ids", [])
    seen_texts: set[str] = set()
    results = []
    for qid in question_ids:
        q = q_map.get(qid)
        if not q:
            continue
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
        and not f.stem.upper().startswith("MOC")
        and not f.stem.lower().startswith("template")
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

def _strip_exam_section(text: str) -> str:
    """Remove existing exam-focus-auto section (and the --- separator before it)."""
    # remove from the separator+tag onward
    cut = re.search(r"\n\n---\n" + re.escape(SECTION_TAG), text)
    if cut:
        return text[:cut.start()]
    # fallback: remove from tag onward if no separator
    cut = re.search(re.escape(SECTION_TAG), text)
    if cut:
        return text[:cut.start()].rstrip()
    return text


def run(disease_filter: str | None, dry_run: bool, min_questions: int = 1,
        repatch: bool = False):
    kb, disease_index = _load_kb()
    q_map = {q["id"]: q for q in kb}
    diseases = list(disease_index.keys())
    updated = skipped = 0

    # group diseases by resolved note_path to avoid overwriting within one run
    from collections import defaultdict
    note_groups: dict[Path, list[str]] = defaultdict(list)
    for disease in diseases:
        if disease_filter and disease_filter.lower() not in disease.lower():
            continue
        note_path = _find_note(disease)
        if note_path:
            note_groups[note_path].append(disease)
        else:
            print(f"  – {disease}: ไม่มี note")

    for note_path, matched_diseases in note_groups.items():
        existing = note_path.read_text(encoding="utf-8", errors="ignore")

        if SECTION_TAG in existing:
            if not repatch:
                print(f"  ✓ {note_path.name} — already patched, skip")
                skipped += 1
                continue
            existing = _strip_exam_section(existing)

        # merge question_ids from all matched diseases, dedup, preserve order
        seen_ids: set[str] = set()
        merged_questions: list[dict] = []
        seen_texts: set[str] = set()
        for disease in matched_diseases:
            for qid in disease_index.get(disease, {}).get("question_ids", []):
                if qid in seen_ids:
                    continue
                seen_ids.add(qid)
                q = q_map.get(qid)
                if not q:
                    continue
                key = str(q.get("question_text", ""))[:80].strip()
                if key and key not in seen_texts:
                    seen_texts.add(key)
                    merged_questions.append(q)

        if len(merged_questions) < min_questions:
            print(f"  – {note_path.name}: พบ {len(merged_questions)} ข้อ (ต่ำกว่า threshold), skip")
            skipped += 1
            continue

        label = matched_diseases[0] if len(matched_diseases) == 1 else f"{matched_diseases[0]} +{len(matched_diseases)-1}"
        print(f"\n  {label} — {len(merged_questions)} ข้อ → {note_path.name}")

        section = _format_section(matched_diseases[0], merged_questions)

        if dry_run:
            print("  [dry-run] preview:")
            print("  " + "\n  ".join(section.splitlines()[:30]))
            print("  ...")
        else:
            new_content = existing.rstrip() + f"\n\n---\n{section}\n"
            note_path.write_text(new_content, encoding="utf-8")
            action = "repatched" if repatch else "appended"
            print(f"  → {action} {len(merged_questions)} questions to {note_path.name}")

        updated += 1

    print(f"\n[update] Done — {updated} updated, {skipped} skipped")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--disease",       help="Filter disease name")
    parser.add_argument("--dry-run",       action="store_true")
    parser.add_argument("--repatch",       action="store_true",
                        help="Replace existing exam section (use after fixing search logic)")
    parser.add_argument("--min-questions", type=int, default=1,
                        help="Minimum exam questions required to update (default 1)")
    args = parser.parse_args()
    run(args.disease, args.dry_run, args.min_questions, args.repatch)
