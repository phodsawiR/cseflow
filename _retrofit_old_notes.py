"""
Append "## 📝 ตัวอย่างข้อสอบจริง" to the END of already-existing OLD-format notes
in 02 - Diseases/ (Hypertension, DM, Stroke, etc. — notes that predate this
session and are NOT being restructured, just getting real exam questions added).
No LLM call — verbatim copy from exam_kb.json. Safe to re-run.
"""
import json
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, "examflow")
from pipeline import _load_exam_kb, _vault_filename_tokens

VAULT_DISEASES_DIR = r"C:\Users\ASUS\Documents\Obsidian vault\02 - Diseases"


def _match_exact_only(disease_names: list[str], vault_files: list[str]) -> dict[str, str | None]:
    """EXACT token match only (no subset fallback) — this script writes disease-
    specific questions into a specific file, so a loose subset match (e.g. generic
    'Hypertension' matching 'Portal_Hypertension.md') would silently write the
    wrong disease's questions into the wrong note. Gap-detection can afford to be
    loose; writing content can't."""
    vault_norm = [(f, _vault_filename_tokens(f)) for f in vault_files]
    coverage: dict[str, str | None] = {}
    for d in disease_names:
        d_tokens = _vault_filename_tokens(d + ".md")
        exact = [f for f, t in vault_norm if t == d_tokens]
        coverage[d] = exact[0] if exact else None
    return coverage


def build_section(questions: list[dict]) -> str:
    blocks = []
    for q in questions[:3]:
        choices = "\n".join(q.get("choices", []))
        ak = q.get("answer_key") or "?"
        ans_text = next((c for c in q.get("choices", []) if c.strip().upper().startswith(ak.upper())), ak)
        explanation = q.get("answer_explanation", "").strip()
        block = f"**{q['id']}:**\n{q['question_text']}\n{choices}\n\n> [!dx]- เฉลย\n> คำตอบ: <span class=\"management\">{ans_text}</span>"
        if explanation:
            block += f"\n> {explanation}"
        blocks.append(block)
    return "\n\n## 📝 ตัวอย่างข้อสอบจริง\n\n" + "\n\n".join(blocks) + "\n"


def main():
    kb = _load_exam_kb()
    di = kb["disease_index"]
    questions_by_id = {q["id"]: q for q in kb["questions"]}

    all_files = []
    for root, _, files in os.walk(VAULT_DISEASES_DIR):
        for f in files:
            if f.endswith(".md"):
                all_files.append(os.path.join(root, f))
    basename_to_path = {}
    for p in all_files:
        basename_to_path.setdefault(os.path.basename(p), []).append(p)

    targets = [(name, data["frequency"]) for name, data in di.items() if data["frequency"] >= 1]
    targets.sort(key=lambda x: -x[1])

    coverage = _match_exact_only([n for n, _ in targets], [os.path.basename(p) for p in all_files])

    patched = skipped_has = skipped_noq = skipped_nofile = 0

    for name, freq in targets:
        matched_fname = coverage.get(name)
        if not matched_fname:
            continue
        paths = basename_to_path.get(matched_fname, [])
        if not paths:
            skipped_nofile += 1
            continue

        qids = di[name].get("question_ids", [])
        questions = [questions_by_id[q] for q in qids if q in questions_by_id]
        if not questions:
            skipped_noq += 1
            continue

        for path in paths:
            with open(path, encoding="utf-8") as f:
                content = f.read()

            if "ตัวอย่างข้อสอบจริง" in content:
                skipped_has += 1
                continue

            section = build_section(questions)
            new_content = content.rstrip("\n") + "\n" + section

            with open(path, "w", encoding="utf-8") as f:
                f.write(new_content)
            patched += 1
            print(f"  [PATCHED] {name} ({freq}Q) -> {os.path.relpath(path, VAULT_DISEASES_DIR)}")

    print(f"\nPatched: {patched} | Already had section: {skipped_has} | No questions: {skipped_noq} | No file: {skipped_nofile}")


if __name__ == "__main__":
    main()
