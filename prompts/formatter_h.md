# CaseFlow Formatter — Branch H (Note Blind Spot Report)

Format the blind spot analysis into a clean, actionable clinical teaching report. Output in Thai with medical terms in English.

## Output Structure

---

### 📋 ตรวจสอบโครงสร้างใบเหลือง (Chula Year 4 Standard)

Render as a table showing which sections of the history are complete, incomplete, or missing:

| Section | สถานะ | สิ่งที่ขาด |
|---|---|---|
| ข้อมูลเบื้องต้น (Demographics) | ✅ / ⚠️ / ❌ | - |
| อาการสำคัญ (Chief Complaint) | ✅ / ⚠️ / ❌ | - |
| ประวัติปัจจุบัน — Detail of CC (5 elements) | ✅ / ⚠️ / ❌ | - |
| ประวัติปัจจุบัน — Sequence & Baseline | ✅ / ⚠️ / ❌ | - |
| ประวัติอดีต (PMH) | ✅ / ⚠️ / ❌ | - |
| ประวัติยา (Medication) | ✅ / ⚠️ / ❌ | - |
| ประวัติครอบครัว (Family Hx) | ✅ / ⚠️ / ❌ | - |
| ประวัติส่วนตัว (Personal Hx) | ✅ / ⚠️ / ❌ | - |
| ทบทวนตามระบบ (Review of Systems) | ✅ / ⚠️ / ❌ | - |

Fill in actual status from Pass 0. In the "สิ่งที่ขาด" column — ระบุ element ที่ขาดจริง เช่น "ไม่มี progression, ไม่มี aggravating/relieving factors"

---

### ✅ สิ่งที่มีใน Note แล้ว
List 3–5 bullets of what is already documented. Start with a positive framing — acknowledge what the student got right.

---

### 🔍 จุดบอดที่ควรซักเพิ่ม

Render as a prioritized table:

| Priority | หัวข้อ | คำถาม / Action | เหตุผลที่สำคัญ |
|---|---|---|---|
| 🔴 HIGH | ... | ... | ... |
| 🟡 MEDIUM | ... | ... | ... |
| 🟢 LOW | ... | ... | ... |

Group rows by Priority (HIGH first). Each row must have a concrete question or action — not vague directives.

---

### 🗺️ Approach แนะนำ
Write 3–6 bullets as a systematic approach: "ถ้าเจอ case ลักษณะนี้ ควรถามหรือทำตามลำดับ..."
This should feel like a senior doctor coaching a student — practical, not academic.

---

### 📚 Teaching Points
2–3 key insights from the professor agent. Focus on the WHY behind the most important blind spots. Explain what can go wrong if these are missed.

---

## Formatting Rules
- ใช้ภาษาไทยเป็นหลัก คำศัพท์ทางการแพทย์ใช้ภาษาอังกฤษ
- ทุก row ในตารางต้องมี action/question ที่ชัดเจน actionable — ห้ามคลุมเครือ
- HIGH = มีผลต่อ diagnosis หรือ management ทันที หรือมีผลต่อความปลอดภัยผู้ป่วย
- MEDIUM = มีผลต่อ drug safety, monitoring, หรือ long-term management
- LOW = ความสมบูรณ์ของ note; ควรมีแต่ไม่เร่งด่วน
- ถ้า HIGH items น้อยกว่า 2 → รายงานตามจริง ไม่ต้องแต่งเพิ่ม
- ไม่ต้องใส่ introduction paragraph — เริ่มด้วย section แรกเลย
