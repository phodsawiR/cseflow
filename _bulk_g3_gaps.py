import sys, io, json, contextlib
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, "examflow")
from pipeline import run_g3_disease

BALANCE_KEYWORDS = [
    "credit balance", "insufficient_quota", "insufficient quota",
    "quota exceeded", "billing", "purchase credits",
]

# The 38 diseases from this session's gap list that genuinely have no note yet
# (the other 29 were already generated and retrofitted with sample_questions
# for free via _retrofit_sample_questions.py — no need to touch them again).
TARGETS = [
    ("Ischemic hepatitis", 3), ("Gastroesophageal reflux disease", 3),
    ("End-stage renal disease", 3), ("Renal Artery Stenosis", 3),
    ("IgA nephropathy", 3), ("HIV/AIDS", 3), ("Lung Cancer", 3),
    ("Transient ischemic attack", 3), ("Sheehan's syndrome", 3),
    ("Hepatitis B", 3), ("Hepatitis C", 3), ("Necrotizing fasciitis", 3),
    ("Cholera", 3), ("Infectious mononucleosis", 3), ("Atrial Septal Defect", 3),
    ("Organophosphate poisoning", 3), ("Subarachnoid hemorrhage", 3),
    ("Evans syndrome", 3), ("Thiamine Deficiency", 3), ("Pancreatic carcinoma", 3),
    ("Gastrointestinal Bleeding", 3), ("Trigeminal neuralgia", 3),
    ("Homozygous HbE", 3), ("Beta-thalassemia/HbE disease", 3), ("Syphilis", 3),
    ("Beta-thalassemia trait", 3), ("Hepatocellular Carcinoma", 3),
    ("Renal Tubular Acidosis", 3), ("Thyrotoxicosis", 3), ("Thrombocytopenia", 3),
    ("Alcoholic Liver Cirrhosis", 3), ("Measles", 3), ("Chronic Hepatitis B", 3),
    ("Secondary Syphilis", 3), ("ST-Elevation Myocardial Infarction", 3),
    ("Right Bundle Branch Block", 3), ("Rubella", 3),
    ("Pneumocystis jirovecii pneumonia", 3),
]


def main():
    total = len(TARGETS)
    print(f"[bulk-g3] {total} diseases with no note yet — generating via G3")

    done, failed = [], []
    stopped_for_balance = False

    for i, (name, freq) in enumerate(TARGETS, 1):
        print(f"\n[{i}/{total}] {name} (freq={freq}) ...", flush=True)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                result = run_g3_disease(f"สรุปโรค {name}", compact=False)
        except Exception as e:
            captured = buf.getvalue()
            print(captured, end="")
            print(f"  [EXCEPTION] {e}")
            if any(k in str(e).lower() for k in BALANCE_KEYWORDS):
                print("\n[bulk-g3] ANTHROPIC_BALANCE_EXHAUSTED — stopping")
                stopped_for_balance = True
                break
            failed.append(name)
            continue

        captured = buf.getvalue()
        print(captured, end="")

        low = captured.lower()
        if any(k in low for k in BALANCE_KEYWORDS):
            print("\n[bulk-g3] ANTHROPIC_BALANCE_EXHAUSTED — stopping")
            stopped_for_balance = True
            break

        if isinstance(result, str) and ("[ERROR_AGENT_FAILED" in result or result.startswith("ไม่พบ")):
            print(f"  [FAILED] {result[:200]}")
            failed.append(name)
        else:
            print(f"  [OK] saved")
            done.append(name)

    remaining = [n for n, _ in TARGETS if n not in done and n not in failed]

    summary = {
        "total": total, "done": done, "failed": failed,
        "remaining": remaining, "stopped_for_balance": stopped_for_balance,
    }
    with open("_bulk_g3_result.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print("\n=== SUMMARY ===")
    print(f"Done: {len(done)}/{total}")
    print(f"Failed: {len(failed)}")
    print(f"Remaining (not attempted): {len(remaining)}")
    print(f"Stopped for balance: {stopped_for_balance}")


if __name__ == "__main__":
    main()
