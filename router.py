from typing import Dict, Optional
import os
import concurrent.futures
from contextvars import ContextVar
import anthropic
from google import genai
from google.genai import types
from google.genai.types import GenerateContentConfig, GoogleSearch, Tool, HttpOptions
from dotenv import load_dotenv
import ollama
from tenacity import retry, stop_after_attempt, wait_exponential

load_dotenv()

claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
gemini_client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))

# ── Progress tracking ───────────────────────────────────────────────────────────
_current_session: ContextVar[str] = ContextVar("current_session", default="")
_step_log: dict[str, list] = {}

AGENT_LABELS: Dict[str, str] = {
    "navigator":        "วิเคราะห์ input",
    "pi_checker":       "ตรวจสอบข้อมูลผู้ป่วย",
    "source_finder":    "ค้นหาแหล่งข้อมูล",
    "researcher":       "ค้นหา Web / Guideline",
    "smart_router":     "วิเคราะห์ความซับซ้อน",
    "analyzer":         "วิเคราะห์เคส",
    "challenger":       "ตรวจสอบ DDx",
    "professor":        "Teaching Points",
    "reasoning_gate":   "สังเคราะห์ผล",
    "formatter":        "จัดรูปแบบรายงาน",
    "report_architect": "สร้างโครงร่างรายงาน",
    "symptom_mapper":   "วาง Approach อาการ",
    "round_coach":      "เตรียม Morning Round",
    "interpreter":      "แปลผล Lab/EKG",
    "query_agent":      "ตอบคำถาม",
    "omni_planner":     "วางแผนการทำงาน",
    "revision_router":  "วิเคราะห์ feedback",
    "kb_retrieval":     "ค้น Knowledge Base",
    "drug_agent":       "ตรวจสอบยาและขนาดยา",
    "patho_agent":      "อธิบาย Pathophysiology",
    "score_agent":      "คำนวณ Clinical Scores",
    "qa_agent":         "ตรวจสอบคุณภาพรายงาน",
}

def _log_step(sid: str, agent: str, status: str, detail: str = "") -> None:
    if not sid:
        return
    log = _step_log.setdefault(sid, [])
    if status in ("done", "error") and log and log[-1]["agent"] == agent and log[-1]["status"] == "running":
        log[-1]["status"] = status
        if detail:
            log[-1]["detail"] = detail
    else:
        log.append({"agent": agent, "label": AGENT_LABELS.get(agent, agent), "status": status, "detail": detail})

AGENT_MODELS: Dict[str, str] = {
    "navigator":         "claude",       # [อัปเกรดแล้ว] เข้าใจบริบทว่านี่คือเคส หรือแค่ประวัติสั้นๆ
    "kb_retrieval":      "gemini-pro",   # [UPGRADE] ต้องใช้ความรู้หมอในการสกัด "คำค้นหา" (Query) จากประวัติยาวๆ
    "pi_checker":        "ollama",       # ปล่อยไว้ (งานเซ็นเซอร์ชื่อคนไข้ เป็นงาน Pattern matching ธรรมดา)
    "smart_router":      "claude",       # [UPGRADE] การสับรางย่อยในระบบ Revision ต้องใช้ความเข้าใจเจตนาสูงมาก
    "source_finder":     "gemini-pro",   # [UPGRADE] เป็นตัวตัดสินว่า "เคสนี้ยากจนต้องเปิด Guideline ไหม" ต้องใช้สมองระดับ Pro
    "reasoning_gate":    "claude",        # ย้ายจาก gemini-pro — กรรมการตัดสินตรรกะต้องการ model ที่เสถียร
    "formatter":         "claude",       # [UPGRADE] เพื่อการันตี 100% ว่าเนื้อหา Assessment คมๆ จะไม่ถูกตัดทิ้งตอนจัดหน้า
    "symptom_mapper":    "gemini-pro",   # [อัปเกรดแล้ว] วาง Approach อาการนำ 
    "round_coach":       "claude",       # [UPGRADE] เก็งคำถามอาจารย์หมอ ภาษาของ Claude จะมีความเป็นมนุษย์และคล้ายอาจารย์แพทย์มากกว่า
    "interpreter":       "gemini-pro",   # [อัปเกรดแล้ว] แปลผล Lab/EKG/ABG 
    "researcher":        "gemini-pro",   # [UPGRADE] อ่าน Guideline หรือ Textbook ยาวๆ แล้วสรุปใจความสำคัญ Gemini Pro จะ Context window ใหญ่และแม่นยำ
    "revision_router":   "claude",       # [UPGRADE] ตัวรับคำสั่งแก้รายงาน (เช่น "ไปเพิ่มข้อมูลยาโรคไต") Claude เก่งสุดในการแก้จุดเล็กๆ โดยไม่ทำจุดอื่นพัง
    "analyzer":          "claude", 
    "challenger":        "claude",
    "professor":         "claude",
    "report_architect":  "claude",
    "omni_planner":      "claude",   # Branch U planner — ห้ามใช้ Gemini
    "omni_executor":     "claude",   # Branch U step executor fallback
    "drug_agent":        "claude",   # Drug dosing — ต้องการ precision สูง ห้าม hallucinate dose
    "patho_agent":       "claude",   # Pathophysiology explainer — ต้องการ reasoning ลึก
    "score_agent":       "claude",   # Clinical scoring — ต้องการ precision สูง
    "qa_agent":          "claude",   # QA / hallucination checker — ต้องการ critical thinking
}

def load_prompt(agent_name: str) -> str:
    path = os.path.join("prompts", f"{agent_name}.md")
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def _call_claude_with_retry(agent_name: str, prompt: str, system: str) -> str:
    print(f"[router] {agent_name} (Claude): Requesting...")
    response = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        system=system or load_prompt(agent_name),
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text

_GROUNDING_MODEL = "gemini-3-flash-preview"

def _call_gemini_researcher(prompt: str, system: str, model_name: str) -> str:
    """Researcher: ลอง grounding 1 ครั้ง ถ้าล้มใช้ no-grounding พร้อม retry"""
    content = f"{system}\n\n{prompt}" if system else prompt
    print(f"[router] researcher (grounding/{_GROUNDING_MODEL}): Requesting...")
    print(f"[DEBUG] 🌐 เปิด Google Search Grounding ให้ researcher...")
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(
                gemini_client.models.generate_content,
                model=_GROUNDING_MODEL,
                contents=content,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    temperature=0.2,
                ),
            )
            response = future.result(timeout=30)
        return response.text
    except Exception as grounding_err:
        print(f"[DEBUG] Grounding failed ({grounding_err!r}), falling back to no-grounding...")

    return _call_gemini_no_grounding_with_retry(prompt, system, model_name)


_FALLBACK_MODEL = "gemini-2.5-pro"

@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=4, max=10))
def _call_gemini_no_grounding_with_retry(prompt: str, system: str, model_name: str) -> str:
    content = f"{system}\n\n{prompt}" if system else prompt
    fallback = _FALLBACK_MODEL if model_name == "gemini-2.5-pro" else model_name
    print(f"[router] researcher (no-grounding fallback) ({fallback}): Requesting...")
    response = gemini_client.models.generate_content(model=fallback, contents=content)
    return response.text


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def _call_gemini_with_retry(agent_name: str, prompt: str, system: str, model_name: str) -> str:
    print(f"[router] {agent_name} ({model_name}): Requesting...")
    content = f"{system}\n\n{prompt}" if system else prompt
    response = gemini_client.models.generate_content(model=model_name, contents=content)
    return response.text

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def _call_ollama_with_retry(agent_name: str, prompt: str, system: str) -> str:
    print(f"[router] {agent_name} (Ollama): Requesting...")
    content = f"{system}\n\n{prompt}" if system else prompt
    response = ollama.chat(
        model="qwen2.5:14b",
        messages=[{"role": "user", "content": content}]
    )
    return response["message"]["content"]

def call_agent(agent_name: str, prompt: str, system: str = "") -> str:
    sid = _current_session.get()
    _log_step(sid, agent_name, "running")
    model = AGENT_MODELS.get(agent_name, "gemini")

    try:
        if model == "claude":
            result = _call_claude_with_retry(agent_name, prompt, system)
        elif model == "gemini":
            result = _call_gemini_with_retry(agent_name, prompt, system, "gemini-2.5-pro")
        elif model == "gemini-pro":
            if agent_name == "researcher":
                result = _call_gemini_researcher(prompt, system, "gemini-2.5-pro")
            else:
                result = _call_gemini_with_retry(agent_name, prompt, system, "gemini-2.5-pro")
        elif model == "ollama":
            try:
                result = _call_ollama_with_retry(agent_name, prompt, system)
            except Exception as e:
                print(f"[router] {agent_name} (Ollama) error after retries: {e}")
                result = prompt
        else:
            _log_step(sid, agent_name, "error", f"model not found: {model}")
            return f"[ERROR_MODEL_NOT_FOUND: {model}]"
    except Exception as e:
        print(f"[router] {agent_name} ({model}) error: {e}")
        _log_step(sid, agent_name, "error", str(e)[:80])
        return f"[ERROR_AGENT_FAILED: {agent_name}]"

    if isinstance(result, str) and result.startswith("[ERROR"):
        _log_step(sid, agent_name, "error", result[:80])
    else:
        _log_step(sid, agent_name, "done")
    return result