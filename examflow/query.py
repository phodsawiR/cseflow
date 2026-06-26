"""
ExamFlow Query CLI — ถามตรง ไม่ต้องผ่าน server หรือ bot

Usage:
  python examflow/query.py "ข้อสอบออกเรื่องไหนบ่อย"
  python examflow/query.py "สรุปโรค CAP ให้หน่อย"
  python examflow/query.py          ← interactive mode

ใช้ Branch U (Omni) — Planner ตัดสินใจเองว่าจะเรียก agent ไหน
บังคับ branch ได้: python examflow/query.py "[branch G3] สรุป SLE"
"""
import os
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── ensure project root is on sys.path ──────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv()

# ── branch detection — override only, default to U ───────────────────────────

def _detect_branch(text: str) -> str:
    """Detect ExamFlow branch from text. Falls back to G2 (pattern) for exam queries."""
    t = text.lower()
    m = re.search(r'\[branch\s*(g[1-7]|u)\]', t)
    if m:
        return m.group(1).upper()
    if any(k in t for k in ["สอบพรุ่งนี้", "ultra", "สรุปสั้น"]):
        return "G6"
    if any(k in t for k in ["ออกโจทย์", "จำลองข้อสอบ", "vignette", "ฝึกทำ"]):
        return "G4"
    if any(k in t for k in ["ยังขาด", "gap", "อ่านครบ"]):
        return "G5"
    if any(k in t for k in ["สรุปโรค", "สรุปเรื่อง", "ต้องรู้", "disease summary"]):
        return "G3"
    if any(k in t for k in ["scope", "ต้องอ่านอะไร", "ควรอ่าน", "ก่อนสอบ", "ทั้งหมด"]):
        return "G1"
    # default: G2 for pattern/frequency queries
    return "G2"


_BRANCH_LABELS = {
    "U":  "Freestyle / Omni — Planner จัดการเอง",
    "G1": "Scope Query", "G2": "Exam Analysis", "G3": "Disease Summary",
    "G4": "Vignette",    "G5": "Gap Detector",  "G6": "Ultra Summary",
    "G8": "Lecture-Exam Bridge",
}


# ── check KB exists ──────────────────────────────────────────────────────────

def _check_kb() -> bool:
    kb_path = os.getenv("EXAM_KB_PATH", "./examflow/exam_kb.json")
    if not Path(kb_path).exists():
        print("\n[ExamFlow] ❌  ยังไม่มี exam_kb.json")
        print("    วาง PDF ใน inbox/exams/ แล้วรัน:")
        print("    python examflow/ingest_exam.py --pdf inbox/exams/exam.pdf\n")
        return False
    return True


# ── main query ───────────────────────────────────────────────────────────────

def query(user_input: str) -> str:
    if not _check_kb():
        return ""

    branch = _detect_branch(user_input)
    label  = _BRANCH_LABELS.get(branch, branch)
    print(f"\n[ExamFlow] Branch {branch} — {label}")
    print("─" * 50)

    if branch == "U":
        return _run_branch_u(user_input)

    from examflow.pipeline import run_examflow_branch
    return run_examflow_branch(branch, user_input)


def _run_branch_u(user_input: str) -> str:
    """Run Branch U (Omni) from CLI — auto-confirm plan."""
    import sys
    sys.path.insert(0, str(_ROOT))
    from session import CaseSession

    sess = CaseSession()
    sess.branch = "U"
    sess.patient_data = user_input

    print("[Branch U] Phase 1 — omni_planner...")
    plan_response = sess._run_pipeline(user_input, [])

    print(plan_response)
    print("\n[Branch U] Phase 2 — executing plan (auto-confirm)...")
    return sess.execute_plan("ยืนยัน")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) > 1:
        # Single query from args
        q = " ".join(sys.argv[1:])
        result = query(q)
        if result:
            print("\n" + result)
    else:
        # Interactive mode
        print("ExamFlow Query — พิมพ์คำถาม (Ctrl+C หรือพิมพ์ 'exit' เพื่อออก)")
        print("ตัวอย่าง: ข้อสอบออกเรื่องไหนบ่อย / สรุปโรค CAP / สอบพรุ่งนี้ PE\n")
        while True:
            try:
                q = input(">>> ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nbye")
                break
            if not q or q.lower() in ("exit", "quit", "ออก"):
                break
            result = query(q)
            if result:
                print("\n" + result + "\n")


if __name__ == "__main__":
    main()
