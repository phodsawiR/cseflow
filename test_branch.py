"""
Standalone test for the rule-based branch override.
Does NOT call any LLM — exercises detect_branch() directly.
"""
from session import detect_branch

test_cases = [
    # (input_text, expected_branch)
    ("ชาย 65 ปี dyspnea bilateral edema PMH: HT DM", "A"),
    ("กลไกของ furosemide คืออะไร", "B"),
    ("ผู้ป่วยชาย 68 ปี มาด้วยอาการขาบวม 2 ข้าง", "C"),
    ("เขียนใบเหลือง\nS: เหนื่อยน้อยลง\nO: V/S stable\nA/P: ADHF improving", "D"),
    ("เตรียมราวน์ ผู้ป่วย ADHF Dx HFrEF", "E"),
    ("pH 7.32 pCO2 55 pO2 68 HCO3 28", "F"),
    ("เขียนใบขาว ผู้ป่วยหญิง 79 ปี เหนื่อยมากขึ้น", "G"),
    # Branch U test cases
    ("สร้างตารางสอนคนไข้เรื่อง DM และยาที่ใช้", "U"),
    ("สรุป case HF ที่วิเคราะห์ไปแล้วให้อ่านง่ายขึ้น", "U"),
    ("เขียน discharge summary สำหรับคนไข้ ADHF ที่กลับบ้านได้", "U"),
    ("อธิบายให้คนไข้เข้าใจว่าทำไมต้องกิน furosemide", "U"),
    ("ช่วย outline สำหรับ case conference เรื่อง PE", "U"),
]


def main():
    print("Testing branch detection...\n")
    passed = 0
    for input_text, expected in test_cases:
        actual = detect_branch(input_text, default="A")
        ok = actual == expected
        if ok:
            passed += 1
        status = "PASS" if ok else "FAIL"
        preview = input_text.replace("\n", " ")[:40]
        suffix = "" if ok else f" (expected {expected})"
        print(f"[{status}] Branch {expected}: {preview}... -> {actual}{suffix}")
    print(f"\nResults: {passed}/{len(test_cases)} passed")
    return 0 if passed == len(test_cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
