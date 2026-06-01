import json
import os
from datetime import datetime
from typing import List, Dict, Any, Optional, Union
from router import call_agent, load_prompt
from notebooklm_client import query_from_source_plan
import re
from schemas import (
    NavigatorOutput,
    SourcePlan,
    SmartRouterOutput,
    RevisionRouterOutput,
    Directive,
    parse_llm_json
)

try:
    from context_enricher import enrich as _kg_vault_enrich, get_kg_drug_flags as _kg_drug_flags
    _ENRICHER_AVAILABLE = True
except ImportError:
    _ENRICHER_AVAILABLE = False
    def _kg_vault_enrich(text: str) -> str: return ""
    def _kg_drug_flags(text: str) -> str: return ""

AVAILABLE_AGENTS = [
    "kb_retrieval",
    "researcher",
    "analyzer",
    "challenger",
    "professor",
    "drug_agent",
    "interpreter",
    "patho_agent",
    "score_agent",
    "formatter",
]

BRANCH_LABELS: dict[str, str] = {
    "A": "Case Analysis (Full Pipeline)",
    "B": "Knowledge Query",
    "C": "Symptom Approach",
    "D": "Progress Note (ใบเหลือง)",
    "E": "Morning Round Prep",
    "F": "Lab/EKG Interpreter",
    "G": "Admission Note (ใบขาว)",
    "N": "Note Narrative (แปล Note อ่านง่าย)",
    "U": "Freestyle / Omni",
}

_CONFIRM_WORDS = {"ยืนยัน", "โอเค", "ใช่", "ok", "yes", "ใช้เลย", "confirm"}

_KB_MISS_MARKERS = ("[KB: no results", "[KB error", "[KB: no targets", "[ERROR")

def detect_branch(raw_input: str, default: str = "A") -> str:
    """
    Rule-based branch detection — runs without any LLM call.
    Returns the branch when a keyword/pattern matches; otherwise `default`.
    """
    text = raw_input.lower()

    # 🌟 ด่านที่ 0: ดักจับคำสั่งบังคับ (Manual Override)
    # ถ้าพิมพ์ [Branch X] ระบบจะเคารพคำสั่งนี้ทันที ไม่สนเงื่อนไขอื่น!
    override_match = re.search(r'\[branch\s*([a-gn])\]', text)
    if override_match:
        return override_match.group(1).upper()

    # N: Note Narrative — แปล note ดิบที่มีคำย่อให้อ่านง่าย ตามลำดับเวลา
    if any(k in text for k in [
        "แปล note", "แปลโน้ต", "อ่าน note", "note narrative",
        "แปลคำย่อ", "ขยาย note", "สรุป note", "note จาก",
        "note คนไข้", "ดู note", "note ดิบ"
    ]):
        return "N"

    # G: Admission Note (ใบขาว)
    if any(k in text for k in [
        "เขียนใบขาว", "admission note", "รายงานผู้ป่วย", "เขียนรายงาน", "ทำใบขาว"
    ]):
        return "G"

    # D: Progress Note (ใบเหลือง / SOAP)
    if any(k in text for k in [
        "เขียนใบเหลือง", "progress note", "เขียน note", "soap"
    ]) or (
        "s:" in text and "o:" in text and ("a:" in text or "a/p:" in text)
    ):
        return "D"

    # E: Morning Round Prep
    if any(k in text for k in [
        "เตรียมราวน์", "morning round", "เตรียม round", "สรุปเคสเช้า"
    ]):
        return "E"

    # F: Interpreter — keyword OR raw lab/EKG values without narrative
    if any(k in text for k in [
        "แปลผล", "interpret", "ekg", "abg", "ecg"
    ]) or (
        not any(k in text for k in ["ปี", "มาด้วย", "อาการ"]) and
        any(k in text for k in ["ph ", "pco2", "hb ", "wbc", "plt", "rate "])
    ):
        return "F"

    # B: Knowledge Query — knowledge keywords AND no patient context AND no task verbs
    if any(k in text for k in [
        "อยากรู้", "กลไก", "mechanism", "guideline",
        "คืออะไร", "อธิบาย", "ยา", "dose"
    ]) and not any(k in text for k in [
        "ปี", "มาด้วย", "pe:", "v/s", "vital", "pmh"
    ]) and not any(k in text for k in [
        "สร้าง", "ทำ", "สอน", "คนไข้"
    ]):
        return "B"

    # C: Symptom Approach — subjective only, no objective findings.
    # `pmh`/`edema` are objective signals that distinguish a clinical case (A)
    # from a chief-complaint-only presentation (C).
    if (
        any(k in text for k in ["อาการ", "มาด้วย", "ปี"]) and
        not any(k in text for k in [
            "pe:", "v/s", "vital", "bp ", "hr ", "lab:", "cr ", "hb ",
            "pmh", "edema", "rales", "crackles", "murmur", "wheezing"
        ])
    ):
        return "C"

    # U: Freestyle / Omni — catch-all สำหรับ request ที่ไม่ match branch ไหน
    if any(k in text for k in [
        "สร้าง", "ทำ", "เขียน", "ช่วย", "สรุป", "อธิบาย"
    ]) and not any(k in text for k in [
        "ใบเหลือง", "ใบขาว", "ราวน์", "แปลผล", "approach"
    ]):
        return "U"

    return default


class CaseSession:
    """
    Multi-turn session สำหรับ 1 case
    - Turn 1: full pipeline
    - Turn 2+: followup (ใช้ context เดิม ประหยัด token)
    - Revision: แก้เฉพาะส่วน ไม่รัน full pipeline
    """

    def __init__(self, session_id: Optional[str] = None):
        self.patient_data: str = ""
        self.draft: str = ""
        self.qa_review: str = ""
        self.version: int = 0
        self.branch: str = ""
        self.history: List[Dict[str, str]] = []           # Q&A history
        self.feedback_history: List[Dict[str, Any]] = []  # revision history
        self.created_at: str = datetime.now().strftime("%Y%m%d_%H%M")
        self.session_id: str = session_id or f"session_{self.created_at}"
        self._nav_result: Optional[NavigatorOutput] = None
        self._pending_branch: Optional[dict] = None  # waiting for branch confirmation
        self._pending_plan: Optional[dict] = None
        self._pipeline_context: Dict[str, str] = {}  # intermediate results for QA
        
        if session_id:
            self.load_state()

    def save_state(self):
        """บันทึก State ปัจจุบันลงไฟล์ JSON"""
        os.makedirs("sessions", exist_ok=True)
        file_path = os.path.join("sessions", f"{self.session_id}.json")
        state_data = {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "patient_data": self.patient_data,
            "draft": self.draft,
            "qa_review": self.qa_review,
            "version": self.version,
            "branch": self.branch,
            "history": self.history,
            "feedback_history": self.feedback_history,
            "_nav_result": self._nav_result.model_dump() if self._nav_result else {},
            "_pending_branch": self._pending_branch,
            "_pending_plan": self._pending_plan
        }
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(state_data, f, ensure_ascii=False, indent=2)
        print(f"[DEBUG] Session saved: {file_path}")

    def load_state(self):
        """โหลด State จากไฟล์ JSON (ถ้ามี)"""
        file_path = os.path.join("sessions", f"{self.session_id}.json")
        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                state_data = json.load(f)
            self.created_at = state_data.get("created_at", self.created_at)
            self.patient_data = state_data.get("patient_data", "")
            self.draft = state_data.get("draft", "")
            self.version = state_data.get("version", 0)
            self.branch = state_data.get("branch", "")
            self.history = state_data.get("history", [])
            self.feedback_history = state_data.get("feedback_history", [])
            self.qa_review = state_data.get("qa_review", "")
            nav_data = state_data.get("_nav_result", {})
            self._nav_result = NavigatorOutput(**nav_data) if nav_data else None
            self._pending_branch = state_data.get("_pending_branch")
            self._pending_plan = state_data.get("_pending_plan")
            print(f"[DEBUG] Session loaded: {file_path}")
        else:
            print(f"[DEBUG] Session file not found: {file_path}. Starting new session.")

    # ─────────────────────────────────────────
    # TURN 1: Full pipeline
    # ─────────────────────────────────────────
    def start(self, raw_input: str) -> str:
        # Step 1: Navigator parse
        nav_result = self._navigate(raw_input)  # also sets self._nav_result
        self.patient_data = nav_result.get("patient_data") or raw_input
        nav_branch = nav_result.get("branch", "A")
        raw_directives = nav_result.get("directives", [])
        directives = [d if isinstance(d, Directive) else Directive(**d) for d in raw_directives]

        # Rule-based override — falls back to navigator's branch if no rule fires.
        detected_branch = detect_branch(raw_input, default=nav_branch)

        print(f"[DEBUG] branch detected: {detected_branch} — awaiting confirmation")

        # Pause: store pending branch and ask user to confirm before running pipeline
        self._pending_branch = {
            "branch": detected_branch,
            "directives": [d.model_dump() for d in directives],
        }

        label = BRANCH_LABELS.get(detected_branch, detected_branch)
        preview = self.patient_data[:300] + ("..." if len(self.patient_data) > 300 else "")
        self.draft = (
            f"🔍 Navigator ตรวจพบว่าเป็น: **Branch {detected_branch} — {label}**\n\n"
            f"📋 ข้อมูลที่รับ:\n{preview}\n\n"
            f"พิมพ์ **'ยืนยัน'** เพื่อดำเนินการ หรือระบุ branch ที่ต้องการ "
            f"(เช่น 'D', 'Branch G', 'เขียนใบเหลือง')"
            f"(A: case | B: query | C: symptom | D: report | E: round | F: interpret | G: admission | U: freestyle)"
        )
        self.save_state()
        return self.draft

    def confirm_branch(self, user_response: str) -> str:
        """ยืนยัน branch หรือเปลี่ยน branch แล้วรัน pipeline"""
        if not self._pending_branch:
            return "[Error: ไม่มี branch ที่รอ confirm]"

        stripped = user_response.strip().lower()

        if stripped not in _CONFIRM_WORDS:
            # ตรวจ pattern ตัวอักษรตรงๆ: "D", "Branch D", "branch d"
            direct_match = re.match(r'^(?:branch\s*)?([a-gnu])$', stripped)
            if direct_match:
                new_branch = direct_match.group(1).upper()
            else:
                new_branch = detect_branch(user_response, default=self._pending_branch["branch"])

            self._pending_branch["branch"] = new_branch
            label = BRANCH_LABELS.get(new_branch, new_branch)
            self.draft = (
                f"🔄 เปลี่ยนเป็น: **Branch {new_branch} — {label}**\n\n"
                f"พิมพ์ **'ยืนยัน'** เพื่อดำเนินการ"
            )
            self.save_state()
            return self.draft

        # ยืนยันแล้ว → รัน pipeline
        pending = self._pending_branch
        self.branch = pending["branch"]
        directives = [Directive(**d) for d in pending["directives"]]
        self._pending_branch = None

        print(f"[DEBUG] branch confirmed: {self.branch}")

        self.draft = self._run_pipeline(self.patient_data, directives)
        self.version = 1
        self.qa_review = self._run_qa()
        self.save_state()
        return self.draft

    # ─────────────────────────────────────────
    # TURN 2+: Followup
    # ─────────────────────────────────────────
    def followup(self, question: str) -> str:
        context = self._build_context(question)

        answer = call_agent(
            "analyzer",
            system=load_prompt("analyzer"),
            prompt=context
        )

        self.history.append({"q": question, "a": answer})
        self.save_state() # บันทึก State หลังตอบคำถาม
        return answer

    # ─────────────────────────────────────────
    # REVISION: ตีกลับแก้เฉพาะส่วน
    # ─────────────────────────────────────────
    # Branch ที่ใช้ full re-run เมื่อรับ feedback (ไม่มี section structure)
    _FULL_RERUN_BRANCHES = {"B", "C", "E", "F", "N"}

    def revise(self, feedback: str) -> str:
        if self.branch in self._FULL_RERUN_BRANCHES:
            return self._revise_full_rerun(feedback)
        return self._revise_section(feedback)

    def _revise_full_rerun(self, feedback: str) -> str:
        """Branch B/C/E/F — re-run pipeline ทั้งหมดพร้อม feedback"""
        enriched = f"{self.patient_data}\n\n[User feedback]: {feedback}"
        self.draft = self._run_pipeline(enriched, [])
        self.version += 1
        self.qa_review = self._run_qa()
        self.feedback_history.append({
            "version": self.version,
            "feedback": feedback,
            "target_agent": "full_rerun",
            "target_section": "all"
        })
        self.save_state()
        return self.draft

    def _revise_section(self, feedback: str) -> str:
        """Branch A/G/D — section-level revision ผ่าน revision_router"""
        routing_prompt = f"""
Branch: {self.branch}
Draft:
{self.draft}

Feedback: {feedback}

ระบุ:
1. agent ที่ต้องแก้ (analyzer/challenger/professor/formatter/report_architect/pi_checker)
2. section ที่ต้องแก้
3. instruction สำหรับ agent นั้น

output JSON: {{"agent": "...", "section": "...", "instruction": "..."}}
"""
        routing_raw = call_agent(
            "revision_router",
            system=load_prompt("revision_router"),
            prompt=routing_prompt
        )

        try:
            data = parse_llm_json(routing_raw)
            routing = RevisionRouterOutput(**data).model_dump()
        except Exception as e:
            print(f"[DEBUG] Revision Router parse error: {e}")
            routing = {"agent": "analyzer", "section": "assessment", "instruction": feedback}

        revision_prompt = f"""
Patient data: {self.patient_data}

Original section ({routing['section']}):
{self._extract_section(self.draft, routing['section'])}

Feedback: {feedback}
Instruction: {routing['instruction']}

ส่งคืนเฉพาะเนื้อหาของ section นี้เท่านั้น ห้ามใส่หัวข้อ (## ...) ห้ามส่ง section อื่นกลับมา และห้ามแนบ report ทั้งหมด
"""
        revised = call_agent(
            routing["agent"],
            system=load_prompt(routing["agent"]),
            prompt=revision_prompt
        )

        self.draft = self._merge_section(self.draft, routing["section"], revised)
        self.version += 1
        self.qa_review = self._run_qa()
        self.feedback_history.append({
            "version": self.version,
            "feedback": feedback,
            "target_agent": routing["agent"],
            "target_section": routing["section"]
        })
        self.save_state()
        return self.draft

    # ─────────────────────────────────────────
    # APPROVE: บันทึก report สุดท้าย
    # ─────────────────────────────────────────
    def approve(self) -> str:
        os.makedirs("reports", exist_ok=True)
        
        # Obsidian Vault Path
        vault_inbox = r"C:\Users\USER\Obsidian vault\00 - Inbox"
        os.makedirs(vault_inbox, exist_ok=True)

        prefix_map = {
            "A": "case", "B": "query",
            "C": "symptom", "D": "report",
            "E": "round", "F": "interpret",
            "G": "admission", "N": "note_narrative",
            "U": "freestyle"
        }
        prefix = prefix_map.get(self.branch, "case")
        
        # ให้ AI คิดชื่อไฟล์จากเนื้อหา
        try:
            print("🧠 Generating smart filename...")
            title_prompt = f"Extract a concise filename (max 4 words) for this medical report. Use PascalCase or underscores. DO NOT include extensions like .md. DO NOT use spaces or special characters. Focus on the main diagnosis or chief complaint.\n\nReport:\n{self.draft[:1500]}"
            generated_title = call_agent(
                "formatter", 
                system="You are a strict filename generator. Output ONLY the raw filename text, nothing else.",
                prompt=title_prompt
            ).strip()
            
            # Clean up title (remove spaces, special chars, keep Thai/English/Underscores)
            generated_title = re.sub(r'[^\w\s-]', '', generated_title).strip().replace(' ', '_')
            
            if not generated_title or len(generated_title) > 50:
                generated_title = prefix
        except Exception as e:
            print(f"[DEBUG] Filename generation failed: {e}")
            generated_title = prefix
            
        # ตั้งชื่อไฟล์: [AI_Name]_[Date]_v[Version].md
        base_filename = f"{generated_title}_{self.created_at}_v{self.version}.md"
        local_filename = os.path.join("reports", base_filename)
        obsidian_filename = os.path.join(vault_inbox, base_filename)

        content = self.draft
        if self.qa_review:
            content += "\n\n---\n\n## QA Review\n\n" + self.qa_review

        # Save ลง Local (CaseFlow folder)
        with open(local_filename, "w", encoding="utf-8") as f:
            f.write(content)
            
        # Save ลง Obsidian Vault (Inbox folder)
        try:
            with open(obsidian_filename, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"✅ Saved to Obsidian Vault: 00 - Inbox/{base_filename}")
            
            # บันทึก Log ลงใน Timeline Hub.md อัตโนมัติ
            timeline_file = os.path.join(r"C:\Users\USER\Obsidian vault", "Timeline Hub.md")
            log_entry = f"- [[{datetime.now().strftime('%Y-%m-%d')}]] : สร้างไฟล์ [[{base_filename.replace('.md', '')}]] สำเร็จ (Branch: {prefix})\n"
            
            # เช็คว่ามีไฟล์ Timeline Hub หรือยัง ถ้าไม่มีให้สร้างพร้อม Header
            if not os.path.exists(timeline_file):
                with open(timeline_file, "w", encoding="utf-8") as f:
                    f.write("# ⏳ Note Timeline Hub\n\n*บันทึกการสร้างไฟล์อัตโนมัติจาก CaseFlow*\n\n---\n\n")
            
            # Append log
            with open(timeline_file, "a", encoding="utf-8") as f:
                f.write(log_entry)
            print("📝 Updated Timeline Hub.md")
            
        except Exception as e:
            print(f"⚠️ Failed to save to Obsidian Vault: {e}")

        return local_filename

    # ─────────────────────────────────────────
    # BRANCH U: Execute plan after user confirm
    # ─────────────────────────────────────────
    def execute_plan(self, user_response: str) -> str:
        """รัน plan หลังจาก user confirm (Branch U)"""
        if not self._pending_plan:
            return "[Error: ไม่มี plan ที่รอ confirm]"

        plan = self._pending_plan
        CONFIRM_WORDS = {"ยืนยัน", "โอเค", "ใช่", "ok", "yes"}

        # user ต้องการแก้ plan → replan
        if user_response.strip().lower() not in CONFIRM_WORDS:
            revised_raw = call_agent(
                "omni_planner",
                system=load_prompt("omni_planner"),
                prompt=(
                    f"Original plan: {json.dumps(plan, ensure_ascii=False)}\n"
                    f"User feedback: {user_response}\n"
                    f"Revise the plan."
                )
            )
            revised = parse_llm_json(revised_raw)
            self._pending_plan = revised if revised else plan
            self.save_state()
            return (
                f"📋 แผนใหม่:\n{self._pending_plan.get('plan_summary', revised_raw)}"
                f"\n\nพิมพ์ 'ยืนยัน' เพื่อดำเนินการ"
            )

        # Execute steps
        MAX_STEPS = 5
        context: Dict[str, Any] = {"patient_data": self.patient_data}
        output_format = plan.get("output_format", "")
        result = ""

        for step in plan.get("steps", [])[:MAX_STEPS]:
            agent = step.get("agent", "")
            instruction = step.get("instruction", "")
            input_var = step.get("input_var", "")
            output_var = step.get("output_var", "result")

            if agent not in AVAILABLE_AGENTS:
                print(f"[WARNING] Omni: agent '{agent}' not in AVAILABLE_AGENTS, skipping step {step.get('step')}")
                continue

            input_data = context.get(input_var, self.patient_data) if input_var else self.patient_data

            # formatter ใน Branch U ใช้ formatter_u (flexible) แทน formatter.md (Branch A)
            if agent == "formatter":
                system_prompt = load_prompt("formatter_u")
                fmt_instruction = instruction
                if output_format:
                    fmt_instruction = f"Output format: {output_format}\n{instruction}"
            else:
                system_prompt = load_prompt(agent)
                fmt_instruction = instruction

            try:
                step_result = call_agent(
                    agent,
                    system=system_prompt,
                    prompt=f"Instruction: {fmt_instruction}\n\nInput:\n{input_data}"
                )
                if step_result.startswith("[ERROR"):
                    raise RuntimeError(step_result)
            except Exception as e:
                print(f"[WARNING] Omni: step {step.get('step')} failed ({e}), using previous output")
                step_result = result or self.patient_data

            # KB miss fallback — ถ้า kb_retrieval step ไม่เจอข้อมูล ให้ researcher grounding แทน
            if agent == "kb_retrieval" and (
                any(m in step_result for m in _KB_MISS_MARKERS) or not step_result.strip()
            ):
                print("🌐 KB Miss in plan step: กำลังเรียก Researcher (grounding)...")
                step_result = call_agent(
                    "researcher",
                    system=load_prompt("researcher"),
                    prompt=f"Instruction: {instruction}\n\nContext:\n{input_data}\n\nKB Status: {step_result}"
                )

            context[output_var] = step_result
            result = step_result

        self.draft = result
        self.version = 1
        self._pending_plan = None
        self.qa_review = self._run_qa()
        self.save_state()
        return result

    # ─────────────────────────────────────────
    # INTERNAL HELPERS
    # ─────────────────────────────────────────
    def _run_qa(self) -> str:
        """รัน QA agent หลัง pipeline เสร็จ — ไม่ block ถ้า draft ว่าง"""
        if not self.draft or self._pending_plan is not None:
            return ""
        ctx = self._pipeline_context
        context_parts = []
        if ctx.get("source"):
            context_parts.append(f"Evidence / Sources ที่ใช้:\n{ctx['source']}")
        if ctx.get("drug_info"):
            context_parts.append(f"Drug Analysis (จาก drug_agent):\n{ctx['drug_info']}")
        if ctx.get("score_result"):
            context_parts.append(f"Clinical Scores (จาก score_agent):\n{ctx['score_result']}")
        context_block = (
            "\n\n--- ข้อมูลกลางจาก pipeline (ใช้ verify รายงาน) ---\n"
            + "\n\n".join(context_parts)
        ) if context_parts else ""
        try:
            return call_agent(
                "qa_agent",
                prompt=(
                    f"Branch: {self.branch}\n\n"
                    f"Patient Data:\n{self.patient_data}\n\n"
                    f"Generated Report:\n{self.draft}"
                    f"{context_block}"
                )
            )
        except Exception as e:
            print(f"[DEBUG] QA agent failed: {e}")
            return ""

    def _navigate(self, raw_input: str) -> dict:
        result = call_agent(
            "navigator",
            system=load_prompt("navigator"),
            prompt=raw_input
        )
        try:
            data = parse_llm_json(result)
            nav = NavigatorOutput(**data)
            self._nav_result = nav
            d = nav.model_dump()
            d["branch"] = d.get("branch") or "A"
            return d
        except Exception as e:
            print(f"[DEBUG] Navigator JSON parse error: {e}")
            self._nav_result = NavigatorOutput(mode="full", branch="A", patient_data=raw_input, directives=[])
            return {"mode": "full", "branch": "A", "patient_data": raw_input, "directives": [], "parallel_tasks": []}

    def _get_kb_sources(self, pi_checked: str) -> str:
        """Helper: Source Finder -> RAG/researcher fallback + KG + Vault enrichment"""
        source_plan_raw = call_agent(
            "source_finder",
            system=load_prompt("source_finder"),
            prompt=pi_checked
        )

        if source_plan_raw.startswith("[ERROR"):
            source = source_plan_raw
        else:
            try:
                data = parse_llm_json(source_plan_raw)
                source_plan = SourcePlan(**data)
                source = query_from_source_plan(source_plan)
            except Exception as e:
                print(f"[DEBUG] Source Plan parse error: {e}")
                source = source_plan_raw

        if any(m in source for m in _KB_MISS_MARKERS) or not source.strip():
            print("🌐 KB Miss: กำลังเรียกใช้ Researcher ค้นหาข้อมูลเพิ่มจาก Web...")
            source = call_agent(
                "researcher",
                system=load_prompt("researcher"),
                prompt=f"Context: {pi_checked}\n\nKB Status: {source}"
            )

        # Enrich with PrimeKG facts + Obsidian vault context
        enrichment = _kg_vault_enrich(pi_checked)
        if enrichment:
            print("[enricher] KG + Vault context injected")
            source = source + enrichment

        return source

    def _run_pipeline(self, patient_data: str, directives: List[Directive]) -> str:
        """รัน Agent ตาม Branch ที่ Navigator เลือก"""
        
        def get_extras(target: str, catch_all: bool = False) -> str:
            items = []
            for d in directives:
                if d.target == target or (catch_all and d.target in [None, "", "researcher", "parallel_tasks"]):
                    items.append(d.instruction)
            
            if not items:
                return ""
            return "\n\n[USER DIRECTIVES - MUST FOLLOW]:\n" + "\n".join(f"- {i}" for i in items)

        # -----------------------------------------
        # Branch A: Case Analysis (Full Pipeline)
        # -----------------------------------------
        if self.branch == "A":
            pi_checked = call_agent("pi_checker", prompt=patient_data)
            source = self._get_kb_sources(pi_checked)

            # Smart Router
            router_raw = call_agent("smart_router", prompt=f"PI:\n{pi_checked}")
            try:
                router_result = SmartRouterOutput(**parse_llm_json(router_raw))
            except Exception:
                router_result = SmartRouterOutput(complexity="medium", spawn_professor=False)

            # Parallel Tasks — drug_agent + patho_agent + score_agent เสมอ + tasks จาก navigator
            nav_tasks = list(self._nav_result.parallel_tasks) if self._nav_result else []
            always_on = ["drug_agent", "patho_agent", "score_agent"]
            parallel_agents = nav_tasks + [t for t in always_on if t not in nav_tasks]

            parallel_outputs: Dict[str, str] = {}
            for task in parallel_agents:
                base = f"Patient: {patient_data}\nSources: {source}\n{get_extras(task)}" if task == "drug_agent" else f"Patient: {patient_data}\n{get_extras(task)}"
                parallel_outputs[task] = call_agent(task, prompt=base)
            parallel_results = "".join(f"\n\n[{t.upper()}]:\n{r}" for t, r in parallel_outputs.items())

            analysis = call_agent(
                "analyzer",
                prompt=f"PI: {pi_checked}\nSources: {source}{parallel_results}{get_extras('analyzer', True)}"
            )
            kg_flags = _kg_drug_flags(pi_checked)
            challenge = call_agent("challenger", prompt=f"Analysis: {analysis}\nSources: {source}{kg_flags}{get_extras('challenger')}")
            synthesis = call_agent("reasoning_gate", prompt=f"A: {analysis}\nC: {challenge}")
            if synthesis.startswith("[ERROR"):
                print("[DEBUG] reasoning_gate failed, falling back to analyzer output")
                synthesis = analysis

            professor_output = call_agent("professor", prompt=f"Data: {patient_data}\nSynthesis: {synthesis}")

            self._pipeline_context = {
                "source": source,
                "drug_info": parallel_outputs.get("drug_agent", ""),
                "score_result": parallel_outputs.get("score_agent", ""),
            }
            formatter_input = f"{synthesis}\n\nTeaching Points:\n{professor_output}{get_extras('formatter', True)}"
            return call_agent("formatter", prompt=formatter_input)

        # -----------------------------------------
        # Branch D: Progress Note (ใบเหลือง)
        # -----------------------------------------
        elif self.branch == "D":
            pi_checked = call_agent("pi_checker", prompt=patient_data)
            source = self._get_kb_sources(pi_checked)
            report = call_agent("report_architect", prompt=f"Data: {patient_data}\nSources (ใช้ประกอบการวิเคราะห์ DDx และ Plan for diagnosis):\n{source}\n{get_extras('report_architect')}")
            drug_info = call_agent("drug_agent", prompt=f"Patient: {patient_data}\nSources: {source}\nDiscussion draft: {report}\nระบุยาที่เกี่ยวข้องกับแต่ละ problem: dose, renal/hepatic adjustment, monitoring parameters, drug interactions.")
            score_result = call_agent("score_agent", prompt=f"Patient: {patient_data}\nDiscussion draft: {report}\nคำนวณ clinical prediction scores ที่ระบุใน A section เช่น Wells, Geneva, CURB-65, TIMI พร้อม interpretation.")
            analysis = call_agent("analyzer", prompt=f"Draft: {report}\nSources: {source}\nDrug review: {drug_info}\nClinical scores: {score_result}{get_extras('analyzer')}")
            kg_flags = _kg_drug_flags(pi_checked)
            challenge = call_agent("challenger", prompt=f"Draft: {report}\nAnalysis: {analysis}\nSources: {source}{kg_flags}{get_extras('challenger')}")

            self._pipeline_context = {"source": source, "drug_info": drug_info, "score_result": score_result}
            return call_agent(
                "formatter",
                system=load_prompt("formatter_d"),
                prompt=f"Report: {report}\nAnalysis: {analysis}\nDrug review: {drug_info}\nClinical scores: {score_result}\nFlags: {challenge}{get_extras('formatter', True)}"
            )

        # -----------------------------------------
        # Branch C: Symptom Approach (Systematic)
        # -----------------------------------------
        elif self.branch == "C":
            symptom_map = call_agent("symptom_mapper", prompt=f"Data: {patient_data}{get_extras('symptom_mapper', True)}")
            source = self._get_kb_sources(patient_data)

            # Parallel: approach_flowchart + drug_agent + patho_agent
            flowchart = call_agent("approach_flowchart", prompt=f"Chief complaint: {patient_data}\nSymptom map:\n{symptom_map}")
            drug_info = call_agent("drug_agent", prompt=f"Symptom/presentation: {patient_data}\nSources: {source}\nSymptom map: {symptom_map}\nList drugs of choice, dosages, and red-flag medications to avoid.")
            patho = call_agent("patho_agent", prompt=f"Patient presentation: {patient_data}\nSymptom map: {symptom_map}\nExplain pathophysiology of leading diagnosis and key DDx linking to these symptoms.")

            analysis = call_agent("analyzer", prompt=f"Map: {symptom_map}\nSources: {source}\nPathophysiology: {patho}\nDrug options: {drug_info}")

            self._pipeline_context = {"source": source, "drug_info": drug_info}
            return call_agent(
                "formatter",
                system=load_prompt("formatter_c"),
                prompt=f"Branch: C\nMap: {symptom_map}\nMindmap:\n{flowchart}\nAnalysis: {analysis}\nPathophysiology: {patho}\nDrug options: {drug_info}{get_extras('formatter', True)}"
            )

        # -----------------------------------------
        # Branch E: Morning Round Prep
        # -----------------------------------------
        elif self.branch == "E":
            source = self._get_kb_sources(patient_data)
            round_prep = call_agent("round_coach", prompt=f"Data: {patient_data}\nSources: {source}")
            drug_info = call_agent("drug_agent", prompt=f"Patient: {patient_data}\nSources: {source}\nRound prep: {round_prep}\nระบุ: (1) ยาที่ต้องให้ตอนเช้าพร้อม dose และเวลา (2) ยาที่ต้อง monitor parameter วันนี้ (3) ยาที่อาจทำให้เกิด side effect ที่ต้องถามคนไข้ตอนเช้า")
            lab_interp = call_agent("interpreter", prompt=f"Patient: {patient_data}\nInterpret all lab values, ABG, ECG findings in clinical context. Highlight trends and critical values.")
            score_result = call_agent("score_agent", prompt=f"Patient: {patient_data}\nRound prep: {round_prep}\nCalculate severity scores relevant for morning round presentation to attending.")
            prof = call_agent("professor", prompt=f"Data: {patient_data}\nPrep: {round_prep}\nDrug review: {drug_info}\nLabs: {lab_interp}\nScores: {score_result}")

            self._pipeline_context = {"source": source, "drug_info": drug_info, "score_result": score_result}
            return call_agent(
                "formatter",
                system=load_prompt("formatter_cef"),
                prompt=f"Branch: E\nPrep: {round_prep}\nDrug review: {drug_info}\nLab interpretation: {lab_interp}\nClinical scores: {score_result}\nProf: {prof}{get_extras('formatter', True)}"
            )

        # -----------------------------------------
        # Branch F: Interpreter
        # -----------------------------------------
        elif self.branch == "F":
            interp = call_agent("interpreter", prompt=patient_data)
            analysis = call_agent("analyzer", prompt=f"Interp: {interp}\nProvide clinical context.")

            self._pipeline_context = {"drug_info": interp}  # raw interpretation เพื่อ cross-check
            return call_agent(
                "formatter",
                system=load_prompt("formatter_cef"),
                prompt=f"Branch: F\nInterp: {interp}\nAnalysis: {analysis}{get_extras('formatter', True)}"
            )
        
        # -----------------------------------------
        # Branch G: Admission Note (ใบขาว)
        # -----------------------------------------
        elif self.branch == "G":
            pi_checked = call_agent("pi_checker", prompt=patient_data)
            source = self._get_kb_sources(pi_checked)
            drug_info = call_agent("drug_agent", prompt=f"Patient: {patient_data}\nSources: {source}\nList complete medication history, current drugs on admission, new drugs to start, dosage adjustments for renal/hepatic function.")
            lab_interp = call_agent("interpreter", prompt=f"Patient: {patient_data}\nInterpret all baseline lab values, ECG, imaging findings. Classify severity and clinical significance.")
            patho = call_agent("patho_agent", prompt=f"Patient: {patient_data}\nExplain pathophysiology of primary diagnosis and top DDx relevant to this admission.")
            score_result = call_agent("score_agent", prompt=f"Patient: {patient_data}\nCalculate all relevant clinical scores for admission: severity, risk stratification, treatment decisions.")

            analysis = call_agent("analyzer", prompt=f"Data: {patient_data}\nSources: {source}\nDrug history: {drug_info}\nBaseline labs: {lab_interp}\nPathophysiology: {patho}\nClinical scores: {score_result}{get_extras('analyzer', True)}")
            challenge = call_agent("challenger", prompt=f"Analysis: {analysis}\nSources: {source}{get_extras('challenger')}")
            synthesis = call_agent("reasoning_gate", prompt=f"A: {analysis}\nC: {challenge}")
            if synthesis.startswith("[ERROR"):
                print("[DEBUG] reasoning_gate failed, falling back to analyzer output")
                synthesis = analysis
            prof = call_agent("professor", prompt=f"Data: {patient_data}\nSynthesis: {synthesis}")

            self._pipeline_context = {"source": source, "drug_info": drug_info, "score_result": score_result}
            formatter_input = f"PI: {pi_checked}\nSynthesis: {synthesis}\nDrug history: {drug_info}\nLab interpretation: {lab_interp}\nPathophysiology: {patho}\nClinical scores: {score_result}\nProf: {prof}\nFlags: {challenge}{get_extras('formatter', True)}"
            return call_agent("formatter", system=load_prompt("formatter_a_chula"), prompt=formatter_input)
        
        # -----------------------------------------
        # Branch B: Knowledge Query
        # -----------------------------------------
        elif self.branch == "B":
            kb_result = self._get_kb_sources(patient_data)
            answer = call_agent("query_agent", prompt=f"Q: {patient_data}\nSources: {kb_result}{get_extras('query_agent')}")
            self._pipeline_context = {"source": kb_result}
            return call_agent("formatter", system=load_prompt("formatter_b"), prompt=answer)

        # -----------------------------------------
        # Branch N: Note Narrative (แปล Note อ่านง่าย)
        # -----------------------------------------
        elif self.branch == "N":
            pi_checked = call_agent("pi_checker", prompt=patient_data)

            # ขั้น 1: Interpreter ขยายคำย่อ + แปลค่า lab/vital ทั้งหมด
            expanded = call_agent(
                "interpreter",
                prompt=(
                    f"ขยายทุกคำย่อทางการแพทย์ให้เป็นภาษาเต็ม และแปลค่า lab / vital signs "
                    f"ให้เป็นภาษาที่อ่านเข้าใจง่าย โดยระบุ normal range และ clinical significance สั้นๆ\n\n"
                    f"Note:\n{pi_checked}"
                )
            )

            # ขั้น 2: Report Architect จัด SOAP ตามลำดับเวลา แต่ละ problem แยกชัด ไม่ลง DDx ลึก
            report = call_agent(
                "report_architect",
                prompt=(
                    f"Data (original note):\n{patient_data}\n\n"
                    f"Expanded (คำย่อขยายแล้ว + ค่า lab แปลแล้ว):\n{expanded}\n\n"
                    f"จัดโครงสร้าง SOAP ตามลำดับเวลา (visit เก่า → ใหม่) "
                    f"แต่ละช่วงเวลาระบุ Problem list สั้นๆ ชัดเจน "
                    f"ไม่ต้องวิเคราะห์ DDx ลึก เน้นความเปลี่ยนแปลงระหว่างช่วงเวลา"
                    f"{get_extras('report_architect')}"
                )
            )

            # ขั้น 3: Drug agent ดึงยาทั้งหมด + monitoring parameters สั้นๆ
            drug_info = call_agent(
                "drug_agent",
                prompt=(
                    f"Patient note:\n{patient_data}\n\n"
                    f"ระบุยาทุกตัวที่กล่าวถึงใน note พร้อม:\n"
                    f"1. ชื่อยาเต็ม + วัตถุประสงค์\n"
                    f"2. Monitoring parameters ที่สำคัญ (สั้นๆ)\n"
                    f"3. ผลข้างเคียงที่ควรระวัง (ถ้ามี)"
                )
            )

            self._pipeline_context = {"drug_info": drug_info}
            return call_agent(
                "formatter",
                system=load_prompt("formatter_n"),
                prompt=(
                    f"Expanded note:\n{expanded}\n\n"
                    f"SOAP draft (chronological):\n{report}\n\n"
                    f"Drug & monitoring info:\n{drug_info}"
                    f"{get_extras('formatter', True)}"
                )
            )

        # -----------------------------------------
        # Branch U: Freestyle / Omni (Phase 1 — Planning)
        # -----------------------------------------
        elif self.branch == "U":
            source = self._get_kb_sources(patient_data)
            plan_raw = call_agent(
                "omni_planner",
                system=load_prompt("omni_planner"),
                prompt=(
                    f"Request: {patient_data}\n\n"
                    f"Available agents: {AVAILABLE_AGENTS}\n\n"
                    f"KB Sources (ใช้ประกอบการวางแผน):\n{source}"
                )
            )
            plan = parse_llm_json(plan_raw)
            if not plan:
                plan = {"plan_summary": plan_raw, "steps": []}

            self._pending_plan = plan
            return (
                f"📋 แผนการทำงาน:\n{plan.get('plan_summary', plan_raw)}"
                f"\n\nพิมพ์ 'ยืนยัน' เพื่อดำเนินการ หรือบอกสิ่งที่ต้องการเปลี่ยนแปลง"
            )

        else:
            return f"[System: Branch {self.branch} under construction.]"

    def _build_context(self, question: str) -> str:
        history_text = "\n".join([f"Q: {h['q']}\nA: {h['a']}" for h in self.history[-3:]]) if self.history else ""
        return f"Data: {self.patient_data}\n\nDraft: {self.draft}\n\nHistory:\n{history_text}\n\nNew Q: {question}"

    def _extract_section(self, draft: str, section: str) -> str:
        lines = draft.split("\n")
        res = []
        capture = False
        for l in lines:
            if section.lower() in l.lower(): capture = True
            if capture:
                res.append(l)
                if len(res) > 30: break
        return "\n".join(res) if res else draft[:500]

    def _merge_section(self, draft: str, section: str, revised: str) -> str:
        # Strip any leading heading line from revised to prevent duplication
        revised_lines = revised.strip().split("\n")
        if revised_lines and revised_lines[0].startswith("#") and section.lower() in revised_lines[0].lower():
            revised_body = "\n".join(revised_lines[1:]).strip()
        else:
            revised_body = revised.strip()

        lines = draft.split("\n")
        res, skip, inserted = [], False, False
        for l in lines:
            if section.lower() in l.lower() and not inserted:
                res.append(l)  # keep original heading
                res.append(revised_body)
                skip, inserted = True, True
            elif skip and l.startswith("##") and section.lower() not in l.lower():
                skip = False
                res.append(l)
            elif not skip:
                res.append(l)
        if not inserted:
            return revised
        return "\n".join(res)
