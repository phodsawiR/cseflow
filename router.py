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

# ── Cost tracking ──────────────────────────────────────────────────────────────
# Pricing per 1M tokens (USD) — update if Google/Anthropic changes rates
_COST_TABLE: dict[str, dict] = {
    "claude":             {"in":  3.00, "out": 15.00, "think": 0.0},   # Sonnet 4.6
    "gemini-thinking":    {"in":  1.25, "out": 10.00, "think": 3.50},  # Gemini Pro thinking
    "gemini-latest":      {"in":  0.15, "out":  0.60, "think": 0.0},   # Gemini 3.5 Flash
    "gemini-latest-grnd": {"in":  0.15, "out":  0.60, "think": 0.0},   # Gemini 3.5 Flash + Search
    "gemini-flash":       {"in": 0.075, "out":  0.30, "think": 0.0},   # Gemini Flash fast
    "ollama":             {"in":  0.0,  "out":  0.0,  "think": 0.0},   # local — free
}
_THB_PER_USD = 36.0
_cost_log: dict[str, dict] = {}  # sid → {model_key: {in, out, think, cost_usd}}


def _log_cost(model_key: str, in_tok: int, out_tok: int, think_tok: int = 0) -> None:
    sid = _current_session.get()
    if not sid:
        return
    p = _COST_TABLE.get(model_key, {"in": 0.0, "out": 0.0, "think": 0.0})
    cost_usd = (
        in_tok    * p["in"]    / 1_000_000 +
        out_tok   * p["out"]   / 1_000_000 +
        think_tok * p["think"] / 1_000_000
    )
    bucket = _cost_log.setdefault(sid, {}).setdefault(
        model_key, {"in": 0, "out": 0, "think": 0, "cost_usd": 0.0}
    )
    bucket["in"]       += in_tok
    bucket["out"]      += out_tok
    bucket["think"]    += think_tok
    bucket["cost_usd"] += cost_usd

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
    "blind_spot_checker":  "ตรวจจุดบอด Progress Note",
    # ── ExamFlow agents ───────────────────────────────────────────────────────
    "examflow_extraction": "ExamFlow: สกัดข้อสอบ",
    "examflow_grounding":  "ExamFlow: ตรวจสอบ Grounding",
    "examflow_obsidian":   "ExamFlow: จัดรูปแบบ Obsidian",
    "examflow_scope":      "ExamFlow: สร้าง Scope Checklist",
    "examflow_pattern":    "ExamFlow: วิเคราะห์ Pattern",
    "examflow_distractor": "ExamFlow: วิเคราะห์ Distractors",
    "examflow_disease":    "ExamFlow: สร้างสรุปโรค / Vignette",
    "examflow_gap":        "ExamFlow: ตรวจ Gap ใน Vault",
    "examflow_lecture":    "ExamFlow: Lecture-Exam Bridge สรุปก่อนสอบ",
    # ── Radiology ─────────────────────────────────────────────────────────────
    "xray_vocab_reporter": "สร้าง CXR Vocabulary Pool Note",
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
    "blind_spot_checker": "gemini-thinking",    # identify clinical gaps — thinking depth needed

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
    "formatter":         "gemini-flash",        # format final output (legacy / non-A branches)
    "report_architect":  "gemini-flash",        # SOAP structure planning
    "score_agent":       "gemini-flash",        # clinical score calculation
    "omni_executor":     "gemini-flash",        # plan step fallback executor
    # ── Branch A: reasoning chain (Steps 2–4) ────────────────────────────────
    "sign_symptom_mapper": "gemini-latest",     # Step 2: DDx → expected signs/symptoms/PE
    "gap_analyzer":        "gemini-latest",     # Step 3: compare documented vs expected
    "attending_qa":        "claude",            # Step 4: generate attending's Q&A
    # ── Branch A split formatters (Step 5) ───────────────────────────────────
    "formatter_aug":     "gemini-latest",       # Section 1: inline augmentation
    "formatter_missing": "gemini-flash",        # Section 3: missing critical list (just present)
    "formatter_qa":      "gemini-flash",        # Section 4: format attending_qa output (just present)
    "formatter_disease": "gemini-latest",       # Section 5: disease quick reference

    # ── Local: never leaves machine ───────────────────────────────────────────
    "pi_checker":        "ollama",              # patient info anonymization

    # ── ExamFlow: offline structured tasks → gemini-flash ─────────────────────
    "examflow_extraction": "gemini-flash",     # PDF question extraction (offline batch)
    "examflow_grounding":  "gemini-flash",     # anti-hallucination gate (every G branch)
    "examflow_obsidian":   "gemini-flash",     # Obsidian markdown formatter (G3 only)
    "examflow_scope":      "gemini-flash",     # scope checklist generator (G1)
    "examflow_pattern":    "gemini-flash",     # pattern / trend analysis (G2)
    "examflow_distractor": "gemini-flash",     # distractor analysis (G2)
    "examflow_gap":        "gemini-flash",     # vault gap detection (G5)
    "examflow_lecture":    "gemini-flash",     # lecture-exam bridge summary (G8)

    # ── ExamFlow: complex synthesis → claude ──────────────────────────────────
    "examflow_disease":    "claude",           # disease architect + vignette writer (G3/G4/G6)

    # ── Radiology ─────────────────────────────────────────────────────────────
    "xray_vocab_reporter": "claude",           # CXR vocab pool note → rich synthesis
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
        max_tokens=16384,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    _log_cost("claude", response.usage.input_tokens, response.usage.output_tokens)
    return response.content[0].text


# ── Gemini streaming helper ────────────────────────────────────────────────────

_DEFAULT_GEMINI_CONFIG = types.GenerateContentConfig(max_output_tokens=32768)

def _stream_gemini(model: str, content: str, config=None) -> tuple[str, object]:
    """Stream Gemini response — each chunk proves connection is alive.
    Returns (full_text, usage_metadata_from_last_chunk).
    """
    chunks: list[str] = []
    last_meta = None
    stream = gemini_client.models.generate_content_stream(
        model=model,
        contents=content,
        config=config or _DEFAULT_GEMINI_CONFIG,
    )
    for chunk in stream:
        if chunk.text:
            chunks.append(chunk.text)
        if chunk.usage_metadata:
            last_meta = chunk.usage_metadata
    return "".join(chunks), last_meta


# ── Gemini 2.5 Pro — extended thinking ────────────────────────────────────────

_HIGH_THINKING_AGENTS = {"challenger", "qa_agent"}

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def _call_gemini_thinking(agent_name: str, prompt: str, system: str) -> str:
    print(f"[router] {agent_name} (Gemini-thinking/{_GEMINI_THINKING_MODEL}): Requesting...")
    content = f"{system}\n\n{prompt}" if system else prompt
    level   = "high" if agent_name in _HIGH_THINKING_AGENTS else "medium"
    text, um = _stream_gemini(
        _GEMINI_THINKING_MODEL, content,
        config=types.GenerateContentConfig(
            max_output_tokens=32768,
            thinking_config=types.ThinkingConfig(thinking_level=level),
        ),
    )
    _log_cost("gemini-thinking",
              (um.prompt_token_count or 0) if um else 0,
              (um.candidates_token_count or 0) if um else 0,
              (um.thoughts_token_count or 0) if um else 0)
    return text


# ── Gemini 3.5 Flash — latest frontier ────────────────────────────────────────

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def _call_gemini_latest(agent_name: str, prompt: str, system: str) -> str:
    print(f"[router] {agent_name} (Gemini-latest/{_GEMINI_LATEST_MODEL}): Requesting...")
    content = f"{system}\n\n{prompt}" if system else prompt
    text, um = _stream_gemini(_GEMINI_LATEST_MODEL, content)
    _log_cost("gemini-latest",
              (um.prompt_token_count or 0) if um else 0,
              (um.candidates_token_count or 0) if um else 0)
    return text


# ── Gemini 3.5 Flash + Google Search grounding ────────────────────────────────

def _call_gemini_latest_grounded(agent_name: str, prompt: str, system: str) -> str:
    """Try grounding first; graceful fallback to no-grounding."""
    content = f"{system}\n\n{prompt}" if system else prompt
    print(f"[router] {agent_name} (Gemini-latest+grounding/{_GEMINI_LATEST_MODEL}): Requesting...")
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(
                _stream_gemini,
                _GEMINI_LATEST_MODEL, content,
                types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                ),
            )
            text, um = future.result(timeout=90)
        _log_cost("gemini-latest-grnd",
                  (um.prompt_token_count or 0) if um else 0,
                  (um.candidates_token_count or 0) if um else 0)
        return text
    except Exception as err:
        print(f"[router] {agent_name} grounding failed ({err!r}) — falling back")
    return _call_gemini_latest_no_grounding(agent_name, prompt, system)


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=4, max=10))
def _call_gemini_latest_no_grounding(agent_name: str, prompt: str, system: str) -> str:
    print(f"[router] {agent_name} (Gemini-latest/no-grounding fallback): Requesting...")
    content = f"{system}\n\n{prompt}" if system else prompt
    text, um = _stream_gemini(_GEMINI_LATEST_MODEL, content)
    _log_cost("gemini-latest",
              (um.prompt_token_count or 0) if um else 0,
              (um.candidates_token_count or 0) if um else 0)
    return text


# ── Gemini 2.5 Flash — fast structured tasks ──────────────────────────────────

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=8))
def _call_gemini_flash(agent_name: str, prompt: str, system: str) -> str:
    print(f"[router] {agent_name} (Gemini-flash/{_GEMINI_FLASH_MODEL}): Requesting...")
    content = f"{system}\n\n{prompt}" if system else prompt
    text, um = _stream_gemini(_GEMINI_FLASH_MODEL, content)
    _log_cost("gemini-flash",
              (um.prompt_token_count or 0) if um else 0,
              (um.candidates_token_count or 0) if um else 0)
    return text


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
    system = system or load_prompt(agent_name)

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


# ── Cost summary (public) ──────────────────────────────────────────────────────

_MODEL_LABELS = {
    "claude":             "Claude Sonnet 4.6",
    "gemini-thinking":    "Gemini Pro (thinking)",
    "gemini-latest":      "Gemini 3.5 Flash",
    "gemini-latest-grnd": "Gemini 3.5 Flash (grounded)",
    "gemini-flash":       "Gemini Flash (fast)",
    "ollama":             "Ollama (local)",
}


def get_session_cost(sid: str) -> dict:
    """Return raw cost accumulator for a session."""
    return _cost_log.get(sid, {})


def format_cost_summary(sid: str) -> str:
    """Return a human-readable cost breakdown for the session."""
    data = _cost_log.get(sid, {})
    if not data:
        return ""
    lines = ["---", "💰 **ค่าใช้จ่าย session นี้**"]
    total_usd = 0.0
    for key, s in data.items():
        label    = _MODEL_LABELS.get(key, key)
        thb      = s["cost_usd"] * _THB_PER_USD
        total_usd += s["cost_usd"]
        think_str = f" + {s['think']:,} think" if s["think"] else ""
        lines.append(f"  {label}: {s['in']:,} in / {s['out']:,} out{think_str} → ฿{thb:.3f}")
    lines.append(f"  {'─'*44}")
    lines.append(f"  รวม: **฿{total_usd * _THB_PER_USD:.3f}** (~${total_usd:.5f})")
    return "\n".join(lines)


def clear_session_cost(sid: str) -> None:
    """Remove cost data for a completed session."""
    _cost_log.pop(sid, None)
