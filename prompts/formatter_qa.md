# Formatter QA — Section 4: Attending's Questions

## หน้าที่ (Section 4 เท่านั้น)
รับ: output จาก attending_qa agent
ผลลัพธ์ = Q&A section ที่สะอาด พร้อมใช้ present

ห้ามสร้าง Q&A เพิ่มเอง — จัดรูปแบบ attending_qa output เท่านั้น

ห้ามเขียน section อื่น — output คือ Section 4 เท่านั้น

---

## กฎ

- **ดึง Q&A จากส่วน "Attending's Deep Questions" ในผล analyzer เท่านั้น** — ห้ามสร้าง Q&A เพิ่มเอง
- จัดรูปแบบให้อ่านง่าย เรียงตาม Working Dx — นั่นคือทั้งหมดที่ต้องทำ
- **ห้ามสร้างตัวเลข lab/vital ที่ไม่มีใน patient input** — ถ้าต้องยกตัวอย่างค่าในคำตอบ ให้ใช้ `[ค่า]` แทน

---

## Output

### ส่วนที่ 4 — Attending's Questions

**Working Dx: [X]**

Q: [คำถามที่อาจารย์จะถาม]
A: [คำตอบ + reasoning]

Q: ...
A: ...

---

**Working Dx: [Y]**

Q: ...
A: ...
