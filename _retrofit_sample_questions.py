"""
Retrofit "## 📝 ตัวอย่างข้อสอบจริง" into already-generated G3 notes — no LLM call,
just copies verbatim question_text/choices/answer straight from exam_kb.json.
Free, deterministic, safe to re-run (skips notes that already have the section).
"""
import json
import os
import re

KB_PATH = r"C:\Users\ASUS\Documents\caseflow\examflow\exam_kb.json"
MCQ_DIR = r"C:\Users\ASUS\Documents\Obsidian vault\05 - Exam Scope\MCQ"

TARGETS = [
    "Ascites", "Diabetic Ketoacidosis", "Tension-type headache",
    "Chronic obstructive pulmonary disease", "Iron deficiency anemia",
    "Deep vein thrombosis", "Hemolytic Uremic Syndrome", "Migraine", "Metabolic Acidosis",
    "Graves' disease", "Pyogenic Liver Abscess", "Portal Hypertension", "Liver Cirrhosis",
    "Colorectal cancer", "Goiter", "Urinary Tract Infection", "Mitral valve prolapse",
    "Erysipelas", "Herpes Zoster", "Genital herpes", "Ventricular Fibrillation",
    "Tinea corporis", "Generalized tonic-clonic seizure", "Choledocholithiasis",
    "Congestive Heart Failure", "Leptospirosis", "Hemoglobin H disease", "Reactive arthritis",
    "Temporal lobe epilepsy",
]

def sanitize(name: str) -> str:
    return re.sub(r'[^\w\s-]', '', name).strip().replace(' ', '_')


def build_section(disease_name: str, questions: list[dict]) -> str:
    if not questions:
        return ""
    blocks = []
    for q in questions[:3]:
        choices = "\n".join(q.get("choices", []))
        ak = q.get("answer_key") or "?"
        ans_text = next((c for c in q.get("choices", []) if c.strip().upper().startswith(ak.upper())), ak)
        explanation = q.get("answer_explanation", "").strip()
        block = f"**{q['id']}:**\n{q['question_text']}\n{choices}\n\n> [!dx]- เฉลย\n> คำตอบ: <span class=\"management\">{ans_text}</span> (พบใน {q['id']})"
        if explanation:
            block += f"\n> {explanation}"
        blocks.append(block)
    return "## 📝 ตัวอย่างข้อสอบจริง\n\n" + "\n\n".join(blocks) + "\n\n"


def main():
    with open(KB_PATH, encoding="utf-8") as f:
        kb = json.load(f)
    di = kb["disease_index"]
    questions_by_id = {q["id"]: q for q in kb["questions"]}

    files = os.listdir(MCQ_DIR)
    patched = skipped_has = skipped_notfound = skipped_noq = 0

    for name in TARGETS:
        safe = sanitize(name)
        match = next((f for f in files if f.startswith(f"disease_{safe}_")), None)
        if not match:
            skipped_notfound += 1
            print(f"  [NOT FOUND] {name}")
            continue

        path = os.path.join(MCQ_DIR, match)
        with open(path, encoding="utf-8") as f:
            content = f.read()

        if "ตัวอย่างข้อสอบจริง" in content:
            skipped_has += 1
            continue

        if name not in di:
            skipped_noq += 1
            print(f"  [NO KB ENTRY] {name}")
            continue

        qids = di[name].get("question_ids", [])
        questions = [questions_by_id[q] for q in qids if q in questions_by_id]
        if not questions:
            skipped_noq += 1
            print(f"  [NO QUESTIONS] {name}")
            continue

        section = build_section(name, questions)

        # Insert right before "## 🃏 Anki Cues" if present, else before the closing "---\nSources:" line, else append
        if "## 🃏 Anki Cues" in content:
            new_content = content.replace("## 🃏 Anki Cues", section + "## 🃏 Anki Cues", 1)
        elif re.search(r'\n---\nSources:', content):
            new_content = re.sub(r'\n---\nSources:', "\n" + section + "---\nSources:", content, count=1)
        else:
            new_content = content.rstrip() + "\n\n" + section

        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)
        patched += 1
        print(f"  [PATCHED] {name} -> {match}")

    print(f"\nPatched: {patched} | Already had section: {skipped_has} | Not found: {skipped_notfound} | No questions: {skipped_noq}")


if __name__ == "__main__":
    main()
