from session import CaseSession

def run_test():
    print("🚀 Starting CaseFlow v2.1 End-to-End Test ...\n")
    
    # จำลองเคสผู้ป่วย (ทดสอบระดับความยาก Medium/High เพื่อดูการทำงานของ Professor)
    mock_case = """Branch A — Case Analysis
  ชาย 58 ปี มาด้วย dyspnea 3 วัน บวมขาทั้ง 2 ข้าง
  PMH: HT, DM type 2, สูบบุหรี่ 30 pack-year
  Medications: amlodipine 5mg, metformin 1000mg
  BP 158/96  HR 112  RR 24  SpO2 88% RA  T 37.2
  PE: JVP elevated, bilateral crackles, pitting edema 2+
  Lab: BNP 1850, Cr 1.8, Na 132, BG 210, Hb 10.2
  CXR: cardiomegaly, bilateral pulmonary congestion
"""
    
    # เริ่มต้นสร้าง Session ใหม่
    session = CaseSession()
    
    # โยนประวัติคนไข้เข้าระบบ (Turn 1)
    print("📥 กำลังส่งประวัติผู้ป่วยเข้าสู่ระบบ CaseFlow...\n")
    final_report = session.start(mock_case)
    
    # แสดงผลลัพธ์
    print("\n" + "="*60)
    print("📄 FINAL REPORT (Template A)")
    print("="*60 + "\n")
    print(final_report)
    
    # บันทึกเป็นไฟล์ Markdown
    saved_file = session.approve()
    print(f"\n💾 บันทึกรายงานสำเร็จที่: {saved_file}")

if __name__ == "__main__":
    run_test()