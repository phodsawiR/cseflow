# CaseFlow v2 + ExamFlow — Architecture Reference

## Claude Operating Mode: Pipeline Operator

**Claude acts as an operator — not an implementer.**

When the user asks for medical analysis, exam summaries, reports, or content generation, Claude MUST run the pipeline instead of generating content directly from files.

### Rules

- **DO**: Run `python examflow/query.py "..."` via Bash for any ExamFlow task (scope, summary, vignette, gap analysis, pattern)
- **DO**: Run `python examflow/ingest_exam.py`, `ingest_lecture.py` for ingestion tasks
- **DO**: Use `! python ...` suggestions when the server is needed (CaseFlow branches A–N via web UI or bot)
- **DO NOT**: Read `exam_kb.json` and generate reports directly — always call the pipeline
- **DO NOT**: Write medical summaries, disease notes, or exam analysis from your own knowledge — use agents

### When to break this rule
Only bypass the pipeline for: debugging code, checking file structure, fixing bugs, syntax errors, config changes.

### Command reference
```bash
# ExamFlow queries (Branch U → agents decide)
python examflow/query.py "<question>"

# Force a specific branch
python examflow/query.py "[branch G3] สรุปโรค HIV"

# Ingest
python examflow/ingest_exam.py --pdf inbox/exams/exam.pdf
python ingest_lecture.py --folder inbox/lectures/HIV/

# Dedup
python examflow/ingest_exam.py --dedup
```

## System Overview
Multi-agent clinical reasoning + exam preparation pipeline for Thai medical students (Year 4).

## Branch Map
| Branch | Name | Description |
|---|---|---|
| A | Case Analysis (DDx-directed morning round) | KB sources → Analyzer → [sign_symptom_mapper ∥ researcher ∥ drug_agent ∥ patho_agent] → gap_analyzer → [score_agent ∥ attending_qa ∥ professor] → [formatter_aug ∥ formatter_missing ∥ formatter_qa ∥ formatter_disease] |
| B | Knowledge Query | RAG → NotebookLM → Query Agent → formatter_b |
| C | Symptom Approach | Symptom Mapper → Flowchart → Drug/Patho → Analyzer → formatter_c |
| D | Progress Note | Report Architect → Drug/Score → Analyzer → Challenger → formatter_d |
| E | Morning Round | Round Coach → Drug → Interpreter → Score → Professor → formatter_cef |
| F | Lab/EKG | Interpreter → Analyzer → formatter_cef |
| G | Admission Note | KB → Drug → Interpreter → Patho/Score → Analyzer → Challenger → reasoning_gate → Professor → formatter_a_chula |
| H | Note Blind Spot | blind_spot_checker → Professor → formatter_h |
| G1 | ExamFlow Scope | exam_kb → scope_mapper → grounding_gate |
| G2 | ExamFlow Analysis | exam_kb → pattern_finder ∥ distractor_analyzer → grounding_gate |
| G3 | ExamFlow Disease | exam_kb → disease_architect → grounding_gate → obsidian_formatter → Obsidian |
| G4 | ExamFlow Vignette | exam_kb → vignette_writer → grounding_gate |
| G5 | ExamFlow Gap | exam_kb + vault_files → gap_detector → grounding_gate |
| G6 | ExamFlow Ultra | exam_kb → disease_architect (compact) → grounding_gate |
| G7 | ExamFlow Bulk Scope | exam_kb → scope_disease (per-disease, fanned out) → grounding_gate → Obsidian. Also triggered as a side effect of G8. |
| G8 | Lecture Bridge | lecture slides/PDF → lecture_aligner → examflow_lecture → grounding_gate → Obsidian |
| N | Note Narrative | Interpreter → Report Architect → Drug → formatter_n |
| U | Freestyle / Omni | KB sources → Omni Planner → [agent steps] |

PI Checker has been removed from the pipeline entirely (2026-07-07) — it was never actually reachable in any branch after the Branch A revamp, and the Ollama tier existed only to run it. No branch above uses it anymore.

## Model Assignment
- **Claude Sonnet 5**: navigator, omni_planner, analyzer, reasoning_gate, attending_qa, examflow_disease, examflow_lecture, xray_vocab_reporter
- **Gemini Pro (thinking)**: challenger, qa_agent, source_finder, blind_spot_checker
- **Gemini Flash (grounded)**: researcher, drug_agent, interpreter, patho_agent, kb_retrieval
- **Gemini 3 Flash (mid tier)**: symptom_mapper, round_coach, professor, query_agent, approach_flowchart, sign_symptom_mapper, gap_analyzer, formatter_aug, formatter_disease
- **Gemini Flash (fast)**: revision_router, formatter, report_architect, score_agent, formatter_missing, formatter_qa, examflow_extraction, examflow_grounding, examflow_obsidian, examflow_scope, examflow_pattern, examflow_distractor, examflow_gap

## ExamFlow — Critical Rules

### ห้ามทำ
- ExamFlow: ห้าม output ข้อมูลโรคโดยไม่มี source citation จาก exam_kb
- ExamFlow: ห้ามสร้าง vignette ที่มี clinical pattern ไม่ตรงกับ exam_samples
- ExamFlow: Grounding Gate ต้องรันทุก G-branch ก่อน output เสมอ
- ExamFlow: ถ้า exam_kb ว่างเปล่า → บอก user ให้ run ingest_exam.py ก่อน
- ExamFlow: ห้ามเพิ่ม model ใหม่หรือเปลี่ยน model ID ที่มีอยู่

### ต้องทำเสมอ
- ExamFlow: Disease Summary ต้องมี [[wikilinks]] ทุก disease name
- ExamFlow: Vignette ต้องมี Distractor Alert section
- ExamFlow: Scope Query ต้องมี "เก็งข้อสอบปีหน้า" section เสมอ
- ExamFlow: output ทุกชิ้นต้องมี Sources: [Q-IDs] ท้ายไฟล์

### G4 Vignette — ข้อยกเว้นเฉพาะ (2026-07-15)
G4 มีไว้สร้างโจทย์ฝึกใหม่ ไม่ใช่ทำ archival record ของข้อสอบจริง จึงอนุญาตให้เติม/แต่ง
choices, answer, explanation, distractor ที่ขาดหายจาก exam_samples ได้ด้วยความรู้ทางคลินิกจริง
(กรณี extraction ตอน ingest ไม่ครบ) — ทุกส่วนที่แต่งเติมต้องครอบด้วย `<span class="reconstructed">`
diagnosis/clinical pattern ของ case ยังต้องตรงกับ exam_samples เสมอ (ห้ามเปลี่ยนโรค/pattern)
**ข้อยกเว้นนี้ใช้เฉพาะ G4 เท่านั้น** — ทุก branch อื่น (G1/G2/G3/G5/G6/G7/G8, disease/lecture notes)
ยังคงห้าม hallucinate เหมือนเดิมทุกประการ ไม่มีการเปลี่ยนแปลง

## ExamFlow — File Paths
```
examflow/
├── exam_kb.json          ← generated by ingest_exam.py
├── exam_kb_schema.json   ← JSON schema validation
├── ingest_exam.py        ← PDF → exam_kb pipeline (run once per batch)
├── anki_export.py        ← reports/ → .apkg Anki deck
├── pipeline.py           ← G1–G7 branch implementations (G8 Lecture Bridge lives in lecture_bridge.py)
├── lecture_bridge.py     ← G8 Lecture Bridge
└── anki/                 ← .apkg output files

prompts/examflow/
├── extraction_agent.md    ← Gemini Flash (offline extraction, ingest_exam.py)
├── grounding_gate.md      ← Gemini Flash (anti-hallucination, every branch incl. G7/G8)
├── scope_mapper.md        ← Gemini Flash (G1)
├── pattern_finder.md      ← Gemini Flash (G2)
├── distractor_analyzer.md ← Gemini Flash (G2)
├── disease_architect.md   ← Claude Sonnet (G3, G6 compact mode)
├── vignette_writer.md     ← Claude Sonnet (G4)
├── gap_detector.md        ← Gemini Flash (G5)
├── obsidian_formatter.md  ← Gemini Flash (G3 Obsidian formatting)
├── scope_disease.md       ← Gemini Flash (G7, per-disease scope note)
├── lecture_extraction.md  ← Gemini Flash (ingest_lecture.py, slide extraction)
├── lecture_aligner.md     ← Claude Sonnet (G8, lecture-to-exam alignment)
└── rare_disease_digest.md ← used by archived one-off digest scripts only

inbox/exams/              ← drop PDF files here before ingesting
reports/
├── scope_YYYYMMDD_HHMM.md
├── analysis_YYYYMMDD_HHMM.md
├── disease_[name]_YYYYMMDD.md  → auto-copied to Obsidian vault/05 - Exam Scope/MCQ/
├── vignette_[topic]_YYYYMMDD.md
└── gaps_YYYYMMDD.md
```

## Anti-Hallucination Architecture
```
PDF → ingest_exam.py (Extraction Agent) → exam_kb.json
                                              ↓
User Query → G1–G6 Pipeline Agent → Grounding Gate → Output
                                         ↑
                             exam_kb เป็น single source of truth
```

## MCQ Practice App (quiz/) — offline, no LLM
Random-MCQ drill built from the per-system quiz notes. **MCQ only** — MEQ/OSCE need a
different answering flow and are deliberately not included yet.

```
Obsidian vault/05 - Exam Scope/Quiz/quiz_*.md   ← source of truth
        ↓ build_quiz_bank.py  (deterministic, free, no LLM)
quiz/data/quiz_bank.js        ← generated, gitignored, rebuild anytime
        ↓ <script src>
quiz/practice.html            ← open by double-click, works on file://
```

```bash
python build_quiz_bank.py --report            # rebuild the bank + parse audit
python apply_answer_fixes.py --fixes f.json   # dry-run answer corrections
python apply_answer_fixes.py --fixes f.json --apply
```

### ห้ามทำ
- ห้ามแก้เฉลยใน `quiz_bank.js` โดยตรง — มันเป็นไฟล์ generated จะหายตอน rebuild
- ห้ามแก้เฉลยแค่ไฟล์ระบบเดียว — 343 Q-ID อยู่ในหลายไฟล์พร้อมกัน ต้องแก้ทุกไฟล์
  (ใช้ `apply_answer_fixes.py` ซึ่งจัดการให้แล้ว)

### Answer corrections — notes win, always
The app can only stage a fix in localStorage. The vault note is canonical, so a fix must
travel note-ward: **app ✏️ → export JSON → `apply_answer_fixes.py --apply` → rebuild**.
The script rewrites the answer line in every note holding that Q-ID and leaves an audit
marker `> ✏️ **เฉลยแก้เอง (date):** ต้นฉบับตอบ X → Y — reason`, which the parser reads back
as `q.fixed` + flag `answer_fixed`; the app then drops its local override by itself.
Three answer dialects exist: `คำตอบ: **X**` (main), `**เฉลย: X**` (_twist), `**Answer: X**` (_g4).

Bank facts: 938 card blocks → 800 cards (586 MCQ + 214 โจทย์บิด) after merging Q-IDs tagged
into several systems. Answer letters are skewed (A 272 / B 255 / C 165 / D 67 / E 41), so the
shuffle-choices toggle matters. `meta.built_at` shows in the app header — if it looks stale the
browser cached the bank, hard-refresh with Ctrl+Shift+R.

## Integration Points
| จุดเชื่อม | วิธี |
|---|---|
| Quiz notes → MCQ app | build_quiz_bank.py อ่าน `05 - Exam Scope/Quiz/quiz_*.md` (MCQ section only) |
| MCQ app → Quiz notes | apply_answer_fixes.py เขียนเฉลยที่แก้กลับเข้าโน้ตทุกไฟล์ที่มี Q-ID นั้น |
| G3 Disease → Obsidian | Auto-copy to OBSIDIAN_VAULT_PATH/05 - Exam Scope/MCQ/ (grouped with exam content for now, may split later) |
| G3 Disease → Anki | anki_export.py อ่าน reports/disease_*.md |
| G4 Vignette → Anki | anki_export.py อ่าน reports/vignette_*.md |
| Branch A + ExamFlow | Disease Architect output เสริม Professor agent context (future) |

## Environment Variables
```
# CaseFlow (existing)
ANTHROPIC_API_KEY=...
GOOGLE_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_USER_ID=...

# ExamFlow (added)
OBSIDIAN_VAULT_PATH=C:/Users/ASUS/Documents/Obsidian vault
EXAM_KB_PATH=./examflow/exam_kb.json
ANKI_OUTPUT_PATH=./examflow/anki/
EXAM_INBOX_PATH=./inbox/exams/
```
