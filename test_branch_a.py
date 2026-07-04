"""
Full Branch A test — simulates a 4th-year student's rough morning round notes
Runs: start() → confirm_branch("ยืนยัน") → print full pipeline output
"""
from session import CaseSession

# ── โครงซักประวัติแบบนักศึกษาปี 4 จริงๆ ที่เตรียมมาแบบมีข้อมูลบางส่วนขาด ──
ROUGH_STUDENT_NOTES = """
Branch A

ชาย 62 ปี มาด้วยเหนื่อยหอบมากขึ้น 2 วัน ขาบวมทั้งสองข้าง
underlying: HT, DM type2, AF (เคยได้ warfarin แต่หยุดเองเมื่อ 2 เดือนก่อน)

ซักได้ว่า:
- เหนื่อยเวลาเดิน นอนราบแล้วเหนื่อย ต้องหนุนหมอน 3 ใบ
- ขาบวมมา 1 สัปดาห์ บวมขึ้นเรื่อยๆ
- ไม่ได้ถามเรื่อง PND
- ไม่ได้ถามเรื่องไอกลางคืน

PE:
- BP 162/98  HR 88 irreg  RR 22  SpO2 92% RA  T 37.0
- conscious alert
- JVP ประมาณ 4-5 cm
- ขาบวม 2+
- ฟังปอด crackles bilateral lower zones
- ไม่ได้ตรวจ apex beat, S3

Lab/เครื่องมือ:
- BNP 2200
- Cr 1.6  Na 129  K 4.2
- Hb 11.5
- ECG: AF with rate 88, no ST change
- CXR: cardiomegaly, pulmonary congestion bilateral

ยาที่ได้ที่ ward:
- furosemide 40mg IV
- warfarin จะเริ่มใหม่ (ยังไม่ได้ check INR)
"""


def main():
    print("=" * 60)
    print("  Branch A — Full Pipeline Test")
    print("  Input: นักศึกษาปี 4 (โครงประวัติแบบขาดบางส่วน)")
    print("=" * 60 + "\n")

    session = CaseSession()

    # Turn 1 — ส่ง input เข้า navigator
    nav_response = session.start(ROUGH_STUDENT_NOTES)
    print("[NAVIGATOR RESPONSE]")
    print(nav_response[:200])
    print("...\n")

    # Turn 2 — force Branch A (in case navigator/rule-based detected wrong branch)
    switch = session.confirm_branch("A")
    print(f"[SWITCH]: {switch[:80]}\n")

    # Turn 3 — ยืนยัน → pipeline runs
    print("[CONFIRMING BRANCH A — running full pipeline...]\n")
    pipeline_output = session.confirm_branch("ยืนยัน")

    print("\n" + "=" * 60)
    print("  PIPELINE OUTPUT")
    print("=" * 60 + "\n")
    print(pipeline_output)

    # Save
    saved = session.approve()
    print(f"\n[SAVED TO]: {saved}")


if __name__ == "__main__":
    main()
