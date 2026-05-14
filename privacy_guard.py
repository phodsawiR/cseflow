"""
PHI Formatter Agent
-------------------
ใช้ Ollama + qwen2.5:14b เพื่อ:
  1) เซนเซอร์ชื่อคนไข้ -> [PATIENT]
  2) เซนเซอร์ HN     -> [REDACTED]
  3) เรียบเรียง HPI ใหม่ตามลำดับเวลา (Chronological order)
  4) จัด Output เป็น Markdown ที่อ่านง่าย
"""

import ollama
import re

MODEL_NAME = "qwen2.5:14b"

SYSTEM_PROMPT = """You are a 'PHI Formatter Agent' for clinical notes written in Thai/English.
Your ONLY job is to de-identify and restructure raw patient histories.
You must NOT add, infer, or interpret any medical information.

def extract_original_names(raw_note: str) -> list[str]:
    patterns = [
        r'(?:นาย|นาง|นางสาว|คุณ|ด\.ช\.|ด\.ญ\.|Mr\.|Mrs\.|Ms\.)\s*([\u0E00-\u0E7F]+(?:\s+[\u0E00-\u0E7F]+)?)',
        r'(?:นาย|นาง|นางสาว|คุณ)\s*([A-Za-z]+(?:\s+[A-Za-z]+)?)',
    ]
    names = []
    for pattern in patterns:
        matches = re.findall(pattern, raw_note)
        names.extend(matches)
    return list(set(names))

def redact_phi(model_output: str, raw_note: str) -> str:
    result = model_output
    names = extract_original_names(raw_note)
    for name in names:
        result = re.sub(
            rf'(?:นาย|นาง|นางสาว|คุณ|ด\.ช\.|ด\.ญ\.|Mr\.|Mrs\.|Ms\.)?\s*{re.escape(name)}',
            '[PATIENT]',
            result
        )
    result = re.sub(r'(?:นาย|นาง(?!สาว)|นางสาว|ด\.ช\.|ด\.ญ\.)\s+[\u0E00-\u0E7F]{2,}', '[PATIENT]', result)
    result = re.sub(r'\b(?:HN|AN|hn|an)\s*:?\s*\d+', '[HN]', result)
    result = re.sub(r'เลขประจำตัว\s*:?\s*\d+', '[HN]', result)
    result = re.sub(r'\b\d{13}\b', '[REDACTED]', result)
    result = re.sub(r'\b0[689]\d{8}\b', '[REDACTED]', result)
    return result
## Rules (must follow exactly)

1. **De-identification — replace with literal tokens, no exceptions**
   | PHI Type                                                                          | Token       |
   |-----------------------------------------------------------------------------------|-------------|
   | Patient name (นาย/นาง/นางสาว/คุณ/ด.ช./ด.ญ./Mr./Mrs./Ms. + first/last/nickname) | [PATIENT]   |
   | HN / AN / OPD number / เลขประจำตัวผู้ป่วย                                       | [HN]        |
   | เลขบัตรประชาชน (13 digits)                                                        | [REDACTED]  |
   | Date of birth (if stated explicitly)                                              | [REDACTED]  |
   | Phone number / Address                                                            | [REDACTED]  |
   | Relative / Next-of-kin name                                                       | [RELATIVE]  |
   | Physician / Nurse name                                                            | [PHYSICIAN] |

   ⚠️ CRITICAL: Scan EVERY line of the output for patient names — including HPI,
   PE, and Assessment sections. Names can appear mid-sentence in Thai or English.
   A name is any word matching a title prefix OR any word you identified as a name
   in the Demographics section. Replace ALL occurrences with [PATIENT].

   Do NOT redact: age, sex, vital signs, lab values, diagnoses, medications, allergies.

2. **HPI Chronological Reordering**
   - Extract all events from the HPI, identify the time anchor for each.
   - Sort strictly by time: furthest in the past → most recent → today.
   - Correct example order: "3 วันก่อน" → "2 วันก่อน" → "1 วันก่อน" → "วันนี้"
   - Re-number after sorting. Do NOT keep original numbering.
   - Do NOT invent facts. Keep clinical wording faithful to the source.
   - If timeline is unclear, preserve original order and add note: *(timeline unclear)*

   ⚠️ SELF-CHECK before writing HPI: list the time anchor of each event mentally,
   confirm they are ascending (oldest first), then write.

3. **Strict Output Format — Markdown only**
   - Start output IMMEDIATELY with `# Patient Summary`. No preamble, no explanation.
   - Do not wrap in code fences.
   - If a section has no data in the source, write exactly: `- N/A`
   - Copy source text faithfully. Do NOT paraphrase or add clinical judgment.

   # Patient Summary

   ## Demographics
   - **Patient:** [PATIENT]
   - **HN:** [HN]
   - **Age/Sex:** ...

   ## Chief Complaint (CC)
   - ...

   ## History of Present Illness (HPI) — Chronological
   1. [3 วันก่อน / earliest event first]
   2. ...
   3. [วันนี้ / most recent last]

   ## Past History / Underlying Disease
   - ...

   ## Medications & Allergies
   - **Medications:** ...
   - **Allergies:** ...

   ## Physical Examination
   - ...

   ## Investigations (from source only)
   - ...

   ## Provisional Diagnosis (copy from source only — do NOT generate)
   - ...

4. **Language:** preserve original language of each phrase (Thai stays Thai, English stays English).

5. **Output discipline:**
   - Output the Markdown document ONLY.
   - Do NOT write: "Here is...", "Sure!", "The following...", or any commentary.
   - Do NOT add sections not listed above.
   - Do NOT diagnose, suggest treatment, or interpret lab values.
   - ⚠️ Assessment/Plan: copy ONLY if explicitly present in source.
     If source has no Assessment/Plan → write `- N/A`. Never generate your own.
"""
def extract_original_names(raw_note: str) -> list[str]:
    patterns: list[str] = [
        r'(?:นาย|นาง|นางสาว|คุณ|ด\.ช\.|ด\.ญ\.|Mr\.|Mrs\.|Ms\.)\s*([\u0E00-\u0E7F]+(?:\s+[\u0E00-\u0E7F]+)?)',
        r'(?:นาย|นาง|นางสาว|คุณ)\s*([A-Za-z]+(?:\s+[A-Za-z]+)?)',
    ]
    names: list[str] = []
    for pattern in patterns:
        matches = re.findall(pattern, raw_note)
        names.extend(matches)
    return list(set(names))

def redact_phi(model_output: str, raw_note: str) -> str:
    result: str = model_output
    names: list[str] = extract_original_names(raw_note)
    for name in names:
        result = re.sub(
            rf'(?:นาย|นาง|นางสาว|คุณ|ด\.ช\.|ด\.ญ\.|Mr\.|Mrs\.|Ms\.)?\s*{re.escape(name)}',
            '[PATIENT]',
            result
        )
    result = re.sub(r'(?:นาย|นาง(?!สาว)|นางสาว|ด\.ช\.|ด\.ญ\.)\s+[\u0E00-\u0E7F]{2,}', '[PATIENT]', result)
    result = re.sub(r'\b(?:HN|AN|hn|an)\s*:?\s*\d+', '[HN]', result)
    result = re.sub(r'เลขประจำตัว\s*:?\s*\d+', '[HN]', result)
    result = re.sub(r'\b\d{13}\b', '[REDACTED]', result)
    result = re.sub(r'\b0[689]\d{8}\b', '[REDACTED]', result)
    return result

def format_phi(raw_note: str, model: str = MODEL_NAME) -> str:
    """ส่งข้อความประวัติคนไข้ดิบ ๆ เข้าโมเดล แล้วคืนค่า Markdown ที่จัดรูปแล้ว"""
    response: dict = ollama.chat(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Raw patient note:\n\n{raw_note}"},
        ],
        options={"temperature": 0.1},
    )
    
    # ดึงข้อความจาก Qwen
    model_output: str = response["message"]["content"].strip()
    
    # ส่งเข้าฟิลเตอร์ redact_phi ที่คุณเพิ่งวางไว้
    clean_output: str = redact_phi(model_output, raw_note)
    
    return clean_output

DUMMY_NOTE = """
HN: 6700123456
ชื่อ: นายสมชาย ใจดี (เล่นชื่อ ชาย) อายุ 58 ปี เพศชาย

CC: ไอมีเสมหะ เหนื่อยหอบ มา 3 วัน

HPI:
วันนี้ผู้ป่วยมาด้วยอาการเหนื่อยหอบมากขึ้น เดินไปห้องน้ำก็เหนื่อย
3 วันก่อน นายสมชายเริ่มมีไข้ต่ำ ๆ ไอแห้ง ๆ ไม่มีน้ำมูก ซื้อยาพาราทานเอง
เมื่อวานนี้ (2 วันก่อน) ไอเริ่มมีเสมหะสีเหลือง เจ็บหน้าอกตอนไอ
1 วันก่อน คุณสมชายเริ่มเหนื่อยเวลาเดิน หายใจเร็ว นอนราบไม่ได้ต้องหนุนหมอนสูง

PMH: HT, DM type 2, ไขมันในเลือดสูง, สูบบุหรี่ 20 pack-years เลิกแล้ว 5 ปี
ยาประจำ: Metformin 500mg 1x2, Enalapril 5mg 1x1, Simvastatin 20mg 1x1 hs
แพ้ยา: NKDA

PE:
T 38.5C, BP 142/88, HR 110, RR 26, SpO2 90% RA
Lungs: crepitation ปอดขวาล่าง, wheezing ทั้งสองข้างเล็กน้อย
Heart: regular, no murmur

Ix:
CXR: RLL infiltration
CBC: WBC 15,200 (N 85%)
BUN/Cr 22/1.1, Na 138, K 4.0
Influenza/COVID rapid test: negative

A/P:
- Community-acquired pneumonia, RLL
- Admit ward, O2 cannula 3 LPM
- Ceftriaxone 2g IV OD + Azithromycin 500mg PO OD
- Continue home meds, monitor blood sugar
- F/U CXR 48 ชม.
"""


if __name__ == "__main__":
    print("=" * 60)
    print("PHI Formatter Agent  (model:", MODEL_NAME, ")")
    print("=" * 60)
    print("\n--- RAW INPUT ---")
    print(DUMMY_NOTE)
    print("\n--- FORMATTED OUTPUT ---\n")
    try:
        formatted = format_phi(DUMMY_NOTE)
        print(formatted)
    except Exception as e:
        print(f"[ERROR] เรียก Ollama ไม่สำเร็จ: {e}")
        print("ตรวจสอบว่า: 1) รัน `ollama serve` แล้ว  2) ดึงโมเดลด้วย `ollama pull qwen2.5:14b`")
