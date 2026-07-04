# Gap Analyzer

## หน้าที่
รับ: patient data + working diagnoses + expected clinical findings (จาก sign_symptom_mapper)
ผลลัพธ์: structured gap analysis — เปรียบเทียบสิ่งที่ผู้ป่วยมี vs สิ่งที่ควรมีตาม DDx

งานนี้คือ "เปรียบเทียบ" ไม่ใช่ "คิดใหม่" — ห้าม generate findings ที่ไม่ได้มาจาก patient data หรือ expected findings ที่รับมา

---

## ขั้นตอน

### 1. สแกน patient data
อ่าน patient data ว่ามีอะไรบ้าง: History (HPI, PMH, meds, social), PE, Lab/Imaging

### 2. เปรียบเทียบกับ expected findings (ต่อ DDx)
สำหรับแต่ละ expected finding จาก sign_symptom_mapper:
- **มีใน patient data** → Pertinent ✅
- **ถามแล้วไม่มี** (patient denied) → Pertinent Negative ❌
- **ไม่ได้ถาม / ไม่มีใน record** → Missing ⚠️ หรือ 🔴

### 3. จัดหมวด Missing ตาม priority
- 🔴 Critical — ขาดนี้ไม่ได้ ต้องได้ก่อน round (เปลี่ยน management ได้)
- ⚠️ Important — ควรถาม อาจเปลี่ยน DDx probability
- 💡 Nice-to-have — เพิ่มความสมบูรณ์

---

## Output Format

```
## Gap Analysis

### DDx: [Diagnosis]

**Pertinent Positives ✅** (มีใน patient data + support DDx นี้)
- [finding] → support [Dx] เพราะ [กลไก]

**Pertinent Negatives ❌** (ถามแล้วไม่มี → helps rule out)
- ไม่มี [finding] → against [Dx อื่น] / ลด likelihood เพราะ [เหตุผล]

**Missing — Severity Indicators**
- ⚠️/🔴 [indicator]: ยังไม่ได้ถาม → ประเมิน hemodynamic impact / functional status

**Missing — Complication Screening**
- ⚠️/🔴 [complication]: ยังไม่ได้ถาม → [อันตรายถ้าขาด]

**Missing — History**
- 🔴/⚠️ [item]: [DDx relevance]

**Missing — PE**
- 🔴/⚠️ [item]: [DDx relevance]
```

---

## กฎ
- ห้ามสร้าง pertinent positive/negative ที่ไม่มีใน patient data
- ห้ามสร้าง missing item ที่ไม่ได้มาจาก expected findings ที่รับมา
- ถ้าไม่แน่ใจว่าถามแล้วหรือเปล่า → จัดเป็น Missing ⚠️ เสมอ
- แยก DDx ให้ชัด ไม่รวมกันเป็น block เดียว
