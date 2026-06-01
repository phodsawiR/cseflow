from typing import Dict, Optional
import os
import concurrent.futures
from contextvars import ContextVar
import anthropic
from google import genai
from google.genai import types
from dotenv import load_dotenv
import ollama
from tenacity import retry, stop_after_attempt, wait_exponential

load_dotenv()

claude        = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
gemini_client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))

# ── Model IDs ──────────────────────────────────────────────────────────────────
_CLAUDE_MODEL          = "claude-sonnet-4-6"
_GEMINI_LATEST_MODEL   = "gemini-3.5-flash"   # newest frontier-class (May 2026)
_GEMINI_THINKING_MODEL = "gemini-3.1-pro-preview"  # latest Pro — extended thinking
_GEMINI_FLASH_MODEL    = "gemini-3.5-flash"    # fast structured tasks (same base as latest)

# ── Progress tracking ──────────────────────────────────────────────────────────
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
    "approach_flowchart": "สร้าง Approach Mindmap",
    "revision_router":  "วิเคราะห์ feedback",
    "kb_retrieval":     "ค้น Knowledge Base",
    "drug_agent":       "ตรวจสอบยาและขนาดยา",
    "patho_agent":      "อธิบาย Pathophysiology",
    "score_agent":      "คำนวณ Clinical Scores",
    "qa_agent":         "ตรวจสอบคุณภาพรายงาน",
}

# ── Role assignment ─────────────────────────────────────────────────────────────
#
#  claude              → orchestration + final clinical synthesis (4 agents only)
#  gemini-thinking     → gemini-2.5-pro with extended thinking (deep DDx / QA)
#  gemini-latest       → gemini-3.5-flash frontier (most agents — fast + smart)
#  gemini-latest-grnd  → gemini-3.5-flash + Google Search (needs live data)
#  gemini-flash        → gemini-2.5-flash (fast structured: format / score / route)
#  ollama              → local — patient info never leaves the machine
#
AGENT_MODELS: Dict[str, str] = {
    # ── Claude: dispatch + final synthesis only ───────────────────────────────
    "navigator":         "claude",              # complex intent → branch dispatch
    "omni_planner":      "claude",              # freestyle multi-step planner
    "analyzer":          "claude",              # final clinical reasoning + synthesis
    "reasoning_gate":    "claude",              # DDx arbitration gate

    # ── Gemini 2.5 Pro (thinking): tasks needing deliberate deep reasoning ────
    "challenger":        "gemini-thinking",     # DDx challenge — needs thinking depth
    "qa_agent":          "gemini-thinking",     # hallucination / quality audit
    "source_finder":     "gemini-thinking",     # judge whether guideline lookup needed

    # ── Gemini 3.5 Flash (latest) + Google Search grounding ──────────────────
    "researcher":        "gemini-latest-grnd",  # guideline / web research
    "drug_agent":        "gemini-latest-grnd",  # dosing — grounded to live protocols
    "interpreter":       "gemini-latest-grnd",  # lab/EKG — live reference ranges
    "patho_agent":       "gemini-latest-grnd",  # pathophysiology + recent updates
    "kb_retrieval":      "gemini-latest-grnd",  # RAG → web fallback

    # ── Gemini 3.5 Flash (latest, no grounding): complex tasks ───────────────
    "symptom_mapper":    "gemini-latest",       # systematic symptom approach
    "round_coach":       "gemini-latest",       # morning round coaching
    "professor":         "gemini-latest",       # teaching points + questions
    "query_agent":       "gemini-latest",       # knowledge Q&A (Branch B)
    "approach_flowchart": "gemini-latest",      # Mermaid mindmap generation (Branch C)

    # ── Gemini 2.5 Flash: fast structured tasks ───────────────────────────────
    "smart_router":      "gemini-flash",        # complexity routing decision
    "revision_router":   "gemini-flash",        # route revision feedback
    "formatter":         "gemini-flash",        # format final output
    "report_architect":  "gemini-flash",        # SOAP structure planning
    "score_agent":       "gemini-flash",        # clinical score calculation
    "omni_executor":     "gemini-flash",        # plan step fallback executor

    # ── Local: never leaves machine ───────────────────────────────────────────
    "pi_checker":        "ollama",              # patient info anonymization
}

# ── Helpers ────────────────────────────────────────────────────────────────────

def load_prompt(agent_name: str) -> str:
    path = os.path.join("prompts", f"{agent_name}.md")
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _log_step(sid: str, agent: str, status: str, detail: str = "") -> None:
    if not sid:
        return
    log = _step_log.setdefault(sid, [])
    if (status in ("done", "error") and log
            and log[-1]["agent"] == agent and log[-1]["status"] == "running"):
        log[-1]["status"] = status
        if detail:
            log[-1]["detail"] = detail
    else:
        log.append({
            "agent": agent,
            "label": AGENT_LABELS.get(agent, agent),
            "status": status,
            "detail": detail,
        })


# ── Claude ─────────────────────────────────────────────────────────────────────

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def _call_claude(agent_name: str, prompt: str, system: str) -> str:
    print(f"[router] {agent_name} (Claude/{_CLAUDE_MODEL}): Requesting...")
    response = claude.messages.create(
        model=_CLAUDE_MODEL,
        max_tokens=8192,
        system=system or load_prompt(agent_name),
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


# ── Gemini 2.5 Pro — extended thinking ────────────────────────────────────────

_HIGH_THINKING_AGENTS = {"challenger", "qa_agent"}

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def _call_gemini_thinking(agent_name: str, prompt: str, system: str) -> str:
    print(f"[router] {agent_name} (Gemini-thinking/{_GEMINI_THINKING_MODEL}): Requesting...")
    content = f"{system}\n\n{prompt}" if system else prompt
    level   = "high" if agent_name in _HIGH_THINKING_AGENTS else "medium"
    response = gemini_client.models.generate_content(
        model=_GEMINI_THINKING_MODEL,
        contents=content,
        config=types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_level=level),
        ),
    )
    return response.text


# ── Gemini 3.5 Flash — latest frontier ────────────────────────────────────────

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def _call_gemini_latest(agent_name: str, prompt: str, system: str) -> str:
    print(f"[router] {agent_name} (Gemini-latest/{_GEMINI_LATEST_MODEL}): Requesting...")
    content = f"{system}\n\n{prompt}" if system else prompt
    response = gemini_client.models.generate_content(
        model=_GEMINI_LATEST_MODEL,
        contents=content,
    )
    return response.text


# ── Gemini 3.5 Flash + Google Search grounding ────────────────────────────────

def _call_gemini_latest_grounded(agent_name: str, prompt: str, system: str) -> str:
    """Try grounding first; graceful fallback to no-grounding."""
    content = f"{system}\n\n{prompt}" if system else prompt
    print(f"[router] {agent_name} (Gemini-latest+grounding/{_GEMINI_LATEST_MODEL}): Requesting...")
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(
                gemini_client.models.generate_content,
                model=_GEMINI_LATEST_MODEL,
                contents=content,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                ),
            )
            response = future.result(timeout=90)
        return response.text
    except Exception as err:
        print(f"[router] {agent_name} grounding failed ({err!r}) — falling back")
    return _call_gemini_latest_no_grounding(agent_name, prompt, system)


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=4, max=10))
def _call_gemini_latest_no_grounding(agent_name: str, prompt: str, system: str) -> str:
    print(f"[router] {agent_name} (Gemini-latest/no-grounding fallback): Requesting...")
    content = f"{system}\n\n{prompt}" if system else prompt
    response = gemini_client.models.generate_content(
        model=_GEMINI_LATEST_MODEL,
        contents=content,
    )
    return response.text


# ── Gemini 2.5 Flash — fast structured tasks ──────────────────────────────────

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=8))
def _call_gemini_flash(agent_name: str, prompt: str, system: str) -> str:
    print(f"[router] {agent_name} (Gemini-flash/{_GEMINI_FLASH_MODEL}): Requesting...")
    content = f"{system}\n\n{prompt}" if system else prompt
    response = gemini_client.models.generate_content(
        model=_GEMINI_FLASH_MODEL,
        contents=content,
    )
    return response.text


# ── Ollama — local privacy ─────────────────────────────────────────────────────

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def _call_ollama(agent_name: str, prompt: str, system: str) -> str:
    print(f"[router] {agent_name} (Ollama/local): Requesting...")
    content = f"{system}\n\n{prompt}" if system else prompt
    response = ollama.chat(
        model="qwen2.5:14b",
        messages=[{"role": "user", "content": content}],
    )
    return response["message"]["content"]


# ── Main dispatcher ────────────────────────────────────────────────────────────

_DISPATCH = {
    "claude":             _call_claude,
    "gemini-thinking":    _call_gemini_thinking,
    "gemini-latest":      _call_gemini_latest,
    "gemini-latest-grnd": _call_gemini_latest_grounded,
    "gemini-flash":       _call_gemini_flash,
    "ollama":             _call_ollama,
}


def call_agent(agent_name: str, prompt: str, system: str = "") -> str:
    sid   = _current_session.get()
    _log_step(sid, agent_name, "running")
    model = AGENT_MODELS.get(agent_name, "gemini-latest")

    fn = _DISPATCH.get(model)
    if fn is None:
        _log_step(sid, agent_name, "error", f"unknown model: {model}")
        return f"[ERROR_MODEL_NOT_FOUND: {model}]"

    try:
        if model == "ollama":
            try:
                result = fn(agent_name, prompt, system)
            except Exception as e:
                print(f"[router] {agent_name} (Ollama) failed ({e}) — falling back to gemini-flash")
                result = _call_gemini_flash(agent_name, prompt, system)
        else:
            result = fn(agent_name, prompt, system)
    except Exception as e:
        print(f"[router] {agent_name} ({model}) error: {e}")
        _log_step(sid, agent_name, "error", str(e)[:80])
        return f"[ERROR_AGENT_FAILED: {agent_name}]"

    if isinstance(result, str) and result.startswith("[ERROR"):
        _log_step(sid, agent_name, "error", result[:80])
    else:
        _log_step(sid, agent_name, "done")
    return result
