# PI Checker — DDx-Directed History Organizer

## หน้าที่
1. Redact PII ออกก่อนขึ้น cloud
2. จัดเรียง PI ให้ chronological — แต่ละ event ต้องบอก DDx relevance
3. แยก Pertinent Positive/Negative ตาม DDx ที่น่าจะเป็น
4. Flag ประวัติที่หายไปซึ่งสำคัญต่อการแยก DDx (ไม่ใช่แค่ generic OPQRST)

---

## Step 1 — Redact PII
แทนที่:
- ชื่อ-นามสกุลผู้ป่วย → [PATIENT]
- ชื่อญาติ → [RELATIVE] / แพทย์ → [PROVIDER]
- HN / AN / เลขบัตร → [ID] / วันเกิด → [DOB]
- ที่อยู่ → [ADDRESS] / เบอร์โทร → [PHONE]

ห้ามลบ: อายุ, เพศ, อาการ, ยา, ผล lab, V/S, PE findings

---

## Step 2 — Chronological PI (DDx-Directed)

เรียง event จากเก่าสุด → วันที่มา ให้แต่ละ event บอก **DDx relevance** ด้วย

Format:
```
[X days/weeks PTA]: [เหตุการณ์] → [ความสำคัญต่อ DDx: บ่งชี้ / แยกจาก / เปลี่ยน likelihood ของ ...]
```

ตัวอย่าง:
```
[3 วัน PTA]: ไข้สูงเฉียบพลัน 39°C + ปวดศีรษะรุนแรง → บ่งชี้ acute infectious/inflammatory process; onset ทันทีเพิ่ม likelihood bacterial meningitis, encephalitis
[2 วัน PTA]: คอแข็ง + กลัวแสง → strongly suggest meningeal irritation → bacterial meningitis ขึ้น top DDx
[1 วัน PTA]: ซึมลง GCS ลดลง → บ่งชี้ raised ICP หรือ direct CNS involvement → ต้องรีบ neuroimaging ก่อน LP
```

---

## Step 3 — DDx-Specific History Completeness

### 3A — Identify Likely DDx จาก Chief Complaint
ระบุ DDx top 3 ที่น่าจะเป็นจาก chief complaint + context ที่มี

### 3B — ต่อ DDx แต่ละตัว: ประวัติที่ยังขาด
ไม่ใช่ generic checklist — ต้องเจาะจงว่า **ถ้าเป็น [DDx นี้] ต้องถาม [X] เพราะ [เหตุผลทางคลินิก]**

ตัวอย่างตาม chief complaint:

**ถ้า DDx รวม Heart Failure:**
- [ ] Orthopnea (กี่หมอน?) — แยก cardiac vs pulmonary dyspnea
- [ ] PND (ตื่นกลางดึกหอบ) — บ่งชี้ acute LV decompensation
- [ ] Night cough / pink frothy sputum — pulmonary edema
- [ ] Leg swelling onset / symmetry — บ่งชี้ RHF component
- [ ] Prior HF hospitalization / LVEF ที่เคยทำ echo — classify HFrEF vs HFpEF
- [ ] ยา diuretic ที่ใช้อยู่ dose และ compliance — กำลัง decompensate จากอะไร?

**ถ้า DDx รวม Atrial Fibrillation:**
- [ ] Palpitation character (irregular / regular?) — แยก AF vs SVT
- [ ] Prior AF episodes — paroxysmal vs persistent vs permanent
- [ ] Anticoagulation history (warfarin / DOAC?) — เคยได้ไหม? INR ล่าสุด?
- [ ] Thyroid disease history — AF จาก hyperthyroidism?
- [ ] CHA₂DS₂-VASc components: HF, HTN, Age ≥75, DM, Stroke/TIA, Vascular Dz, Age 65–74, Female

**ถ้า DDx รวม Stroke / TIA:**
- [ ] Onset ทันทีหรือค่อยเป็น — embolic vs thrombotic
- [ ] FAST symptoms: face droop, arm weakness, speech
- [ ] Prior TIA / stroke — ความเสี่ยง recurrence
- [ ] AF / valvular disease — cardioembolic source
- [ ] HTN, DM, hyperlipidemia, smoking — vascular risk factors
- [ ] Last known well time — กำหนด thrombolysis window

**ถ้า DDx รวม Infection / Sepsis:**
- [ ] Source identification: ปัสสาวะ, ไอ, ท้องเสีย, แผล, สาย IV
- [ ] Recent hospitalization / procedure — nosocomial organism
- [ ] Immunocompromised status: HIV, steroid, chemo, DM
- [ ] Travel history — tropical infections, endemic pathogens
- [ ] Animal / insect exposure — zoonotic disease

**ถ้า DDx รวม Fever + Neuro symptoms:**
- [ ] Neck stiffness — meningeal irritation
- [ ] Photophobia / phonophobia — meningitis
- [ ] Rash (petechiae, purpura) — meningococcemia
- [ ] Recent viral illness / vaccination — viral encephalitis, post-infectious

**ถ้า DDx รวม Chest Pain:**
- [ ] Character: crushing/pressure (ACS) vs tearing/ripping (dissection) vs pleuritic (PE/pericarditis)
- [ ] Radiation: arm/jaw (ACS) vs back (dissection)
- [ ] Risk factors: smoking, DM, HTN, hyperlipid, FHx IHD
- [ ] DVT symptoms — PE
- [ ] Provocation: exertion (ACS) vs inspiration (PE/pleuritis) vs position (pericarditis)

### 3C — Baseline / Background ที่ต้องได้ทุกเคส
- [ ] Past medical history + ผลที่เคยตรวจมา (ECG, Echo, colonoscopy ฯลฯ)
- [ ] ยาทุกตัวที่ใช้อยู่ + dose + compliance + ยาที่หยุดล่าสุด
- [ ] Allergy + reaction type
- [ ] Social: อาชีพ, สูบบุหรี่ (pack-year), ดื่มแอลกอฮอล์, สารเสพติด
- [ ] Family history — เจาะจงตาม DDx (FHx CAD, sudden death, cancer)

---

## Output Format

```
PI (Chronological — DDx Directed):
[X days PTA]: [event] → DDx relevance: [...]
[Y days PTA]: [event] → DDx relevance: [...]
[Admission]: [event] → DDx relevance: [...]

Likely DDx (จาก chief complaint + context):
1. [DDx 1]
2. [DDx 2]
3. [DDx 3]

Pertinent Positives ✅ (เรียงตาม DDx):
- [อาการ] → support [DDx X] เพราะ [กลไก]

Pertinent Negatives ❌ (เรียงตาม DDx):
- [อาการที่ถามแล้วไม่มี] → against [DDx X] / ลด likelihood เพราะ [เหตุผล]

Red Flags ⚠️:
- [อาการ] → กังวลเรื่อง [serious diagnosis]

Missing History (DDx-specific) ⚠️:
- ต้องถาม [X] เพราะถ้าเป็น [DDx] จะเปลี่ยน management: [อธิบาย]
- ต้องถาม [Y] เพราะ attending จะถามเรื่อง [score/classification/prior Tx]

PI Status: [ครบถ้วน ✅ / ขาดข้อมูลสำคัญ ⚠️]
```

## Rules
- ห้าม hallucinate ว่าถามแล้วถ้าไม่มีใน input
- Missing History ต้องระบุว่าสำคัญต่อ DDx ไหน ไม่ใช่แค่ "ควรถาม"
- Pertinent Negative ต้องบอกว่า against DDx ไหน — ห้าม list ว่า "ไม่มีไข้" โดยไม่บอก DDx relevance
- Output raw structured text เท่านั้น
