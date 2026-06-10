"""
ExamFlow Pipeline — Branch G1–G6
All ExamFlow sub-branch pipelines are implemented here.
Called from session.py: run_examflow_branch(branch, user_input, directives)
"""
import json
import os
import re
import glob
from datetime import datetime
from typing import List
import concurrent.futures

from dotenv import load_dotenv

load_dotenv()

EXAM_KB_PATH    = os.getenv("EXAM_KB_PATH", "./examflow/exam_kb.json")
OBSIDIAN_PATH   = os.getenv("OBSIDIAN_VAULT_PATH", r"C:\Users\USER\Obsidian vault")
REPORTS_DIR     = "reports"

_PROMPT_DIR = os.path.join(os.path.dirname(__file__), "..", "prompts", "examflow")


def _load_prompt(name: str) -> str:
    path = os.path.join(_PROMPT_DIR, f"{name}.md")
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read()


def _load_exam_kb() -> dict | None:
    if not os.path.exists(EXAM_KB_PATH):
        return None
    with open(EXAM_KB_PATH, encoding="utf-8") as f:
        return json.load(f)


def _kb_empty_response() -> str:
    return (
        "ไม่พบ exam_kb.json — กรุณา ingest PDF ข้อสอบก่อน\n\n"
        "วิธี:\n"
        "```\n"
        "python examflow/ingest_exam.py --pdf inbox/exams/exam_2023.pdf\n"
        "```"
    )


def _save_report(prefix: str, content: str) -> str:
    os.makedirs(REPORTS_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    filename  = os.path.join(REPORTS_DIR, f"{prefix}_{timestamp}.md")
    with open(filename, "w", encoding="utf-8") as f:
        f.write(content)
    return filename


def _save_to_obsidian(content: str, subfolder: str, filename: str) -> bool:
    try:
        dest_dir = os.path.join(OBSIDIAN_PATH, subfolder)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, filename)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    except Exception as e:
        print(f"[ExamFlow] Obsidian save failed: {e}")
        return False


def _call(agent_name: str, prompt: str, system: str = "") -> str:
    from router import call_agent
    return call_agent(agent_name, prompt=prompt, system=system)


def _get_disease_data(kb: dict, disease_name: str) -> dict:
    """Find disease in disease_index (fuzzy match)."""
    name_lower = disease_name.lower()
    for key, val in kb["disease_index"].items():
        if name_lower in key.lower() or key.lower() in name_lower:
            return {"disease_name": key, **val}
    return {}


def _get_disease_questions(kb: dict, disease_name: str) -> list[dict]:
    """Get all question objects for a disease."""
    data = _get_disease_data(kb, disease_name)
    if not data:
        return []
    ids = set(data.get("question_ids", []))
    return [q for q in kb["questions"] if q["id"] in ids]


def _get_vault_disease_files() -> list[str]:
    """List .md files in Obsidian vault diseases/ folder."""
    diseases_dir = os.path.join(OBSIDIAN_PATH, "diseases")
    if not os.path.exists(diseases_dir):
        diseases_dir = OBSIDIAN_PATH
    pattern = os.path.join(diseases_dir, "**", "*.md")
    files = glob.glob(pattern, recursive=True)
    return [os.path.basename(f) for f in files]


# ─────────────────────────────────────────────────────────
# G1: Scope Query
# ─────────────────────────────────────────────────────────
def run_g1_scope(user_input: str) -> str:
    kb = _load_exam_kb()
    if not kb:
        return _kb_empty_response()

    disease_name = _extract_disease_name(user_input)
    questions = _get_disease_questions(kb, disease_name) if disease_name else kb["questions"]

    scope_data = {
        "disease_index":    kb["disease_index"],
        "system_index":     kb["system_index"],
        "pattern_analysis": kb["pattern_analysis"],
        "questions":        questions,
        "user_query":       user_input,
    }

    scope_raw = _call(
        "examflow_scope",
        system=_load_prompt("scope_mapper"),
        prompt=json.dumps(scope_data, ensure_ascii=False, indent=2)
    )

    grounded = _call(
        "examflow_grounding",
        system=_load_prompt("grounding_gate"),
        prompt=json.dumps({
            "user_query":     user_input,
            "draft_answer":   scope_raw,
            "source_context": json.dumps({
                "questions":     questions,
                "disease_index": kb["disease_index"],
            }, ensure_ascii=False)
        }, ensure_ascii=False)
    )

    saved = _save_report("scope", grounded)
    return f"{grounded}\n\n---\n*บันทึกที่ `{saved}`*"


# ─────────────────────────────────────────────────────────
# G2: Exam Analysis
# ─────────────────────────────────────────────────────────
def run_g2_analysis(user_input: str) -> str:
    kb = _load_exam_kb()
    if not kb:
        return _kb_empty_response()

    disease_name = _extract_disease_name(user_input)
    questions = _get_disease_questions(kb, disease_name) if disease_name else kb["questions"]

    kb_summary = json.dumps({
        "questions":        questions,
        "pattern_analysis": kb["pattern_analysis"],
        "disease_index":    kb["disease_index"],
    }, ensure_ascii=False, indent=2)

    # Parallel: pattern_finder + distractor_analyzer
    pattern_raw    = _call("examflow_pattern",    system=_load_prompt("pattern_finder"),    prompt=kb_summary)
    distractor_raw = _call("examflow_distractor", system=_load_prompt("distractor_analyzer"), prompt=kb_summary)

    merged = f"{pattern_raw}\n\n---\n\n## Distractor Analysis\n\n{distractor_raw}"

    grounded = _call(
        "examflow_grounding",
        system=_load_prompt("grounding_gate"),
        prompt=json.dumps({
            "user_query":    user_input,
            "draft_answer":  merged,
            "source_context": json.dumps(kb["pattern_analysis"], ensure_ascii=False)
        }, ensure_ascii=False)
    )

    saved = _save_report("analysis", grounded)
    return f"{grounded}\n\n---\n*บันทึกที่ `{saved}`*"


# ─────────────────────────────────────────────────────────
# G3: Disease Summary
# ─────────────────────────────────────────────────────────
def run_g3_disease(user_input: str, compact: bool = False) -> str:
    kb = _load_exam_kb()
    if not kb:
        return _kb_empty_response()

    # Extract disease name from user input
    disease_name = _extract_disease_name(user_input)
    disease_data = _get_disease_data(kb, disease_name)

    if not disease_data:
        return (
            f"ไม่พบ **{disease_name}** ในข้อสอบที่ ingest มา\n\n"
            f"โรคที่มีข้อมูล: {', '.join(list(kb['disease_index'].keys())[:20])}\n"
            f"(แสดง 20 อันดับแรก)"
        )

    mode = "compact" if compact else "full"
    architect_input = json.dumps({
        "disease_name":      disease_data["disease_name"],
        "exam_data":         disease_data,
        "mode":              mode,
        "comparison_target": None,
    }, ensure_ascii=False, indent=2)

    disease_draft = _call(
        "examflow_disease",
        system=_load_prompt("disease_architect"),
        prompt=architect_input
    )

    grounded = _call(
        "examflow_grounding",
        system=_load_prompt("grounding_gate"),
        prompt=json.dumps({
            "user_query":    user_input,
            "draft_answer":  disease_draft,
            "source_context": json.dumps(disease_data, ensure_ascii=False)
        }, ensure_ascii=False)
    )

    if compact:
        return grounded

    # Full mode: format for Obsidian
    formatted = _call(
        "examflow_grounding",
        system=_load_prompt("obsidian_formatter"),
        prompt=grounded
    )

    # Save to reports/
    safe_name = re.sub(r'[^\w\s-]', '', disease_data["disease_name"]).strip().replace(' ', '_')
    report_name = f"disease_{safe_name}_{datetime.now().strftime('%Y%m%d')}.md"
    report_path = os.path.join(REPORTS_DIR, report_name)
    os.makedirs(REPORTS_DIR, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(formatted)

    # Auto-copy to Obsidian vault
    obsidian_ok = _save_to_obsidian(formatted, "diseases", report_name)
    obsidian_msg = f"✅ บันทึกไปที่ Obsidian vault/diseases/{report_name}" if obsidian_ok else "⚠️ ไม่สามารถบันทึกไป Obsidian ได้"

    return f"{formatted}\n\n---\n{obsidian_msg}\n*Local: `{report_path}`*"


# ─────────────────────────────────────────────────────────
# G4: Vignette Generator
# ─────────────────────────────────────────────────────────
def run_g4_vignette(user_input: str) -> str:
    kb = _load_exam_kb()
    if not kb:
        return _kb_empty_response()

    disease_name = _extract_disease_name(user_input)
    exam_samples = _get_disease_questions(kb, disease_name)

    if not exam_samples:
        exam_samples = kb["questions"]

    # Extract topic_type preference from user_input
    topic_type = "diagnosis"
    for t in ["management", "investigation", "mechanism", "complication"]:
        if t in user_input.lower():
            topic_type = t
            break

    difficulty = "high_yield"
    if "must_know" in user_input.lower() or "ต้องออก" in user_input:
        difficulty = "must_know"

    vignette_input = json.dumps({
        "topic":        disease_name or user_input,
        "exam_samples": exam_samples,
        "topic_type":   topic_type,
        "difficulty":   difficulty,
    }, ensure_ascii=False, indent=2)

    vignette_draft = _call(
        "examflow_disease",  # uses claude for quality vignettes
        system=_load_prompt("vignette_writer"),
        prompt=vignette_input
    )

    grounded = _call(
        "examflow_grounding",
        system=_load_prompt("grounding_gate"),
        prompt=json.dumps({
            "user_query":    user_input,
            "draft_answer":  vignette_draft,
            "source_context": json.dumps(exam_samples, ensure_ascii=False)
        }, ensure_ascii=False)
    )

    safe_name = re.sub(r'[^\w\s-]', '', disease_name or "topic").strip().replace(' ', '_')
    saved = _save_report(f"vignette_{safe_name}", grounded)
    return f"{grounded}\n\n---\n*บันทึกที่ `{saved}`*"


# ─────────────────────────────────────────────────────────
# G5: Gap Detector
# ─────────────────────────────────────────────────────────
def run_g5_gap(user_input: str) -> str:
    kb = _load_exam_kb()
    if not kb:
        return _kb_empty_response()

    vault_files = _get_vault_disease_files()

    gap_input = json.dumps({
        "disease_index":   kb["disease_index"],
        "vault_files":     vault_files,
        "pattern_analysis": kb["pattern_analysis"],
        "user_query":      user_input,
    }, ensure_ascii=False, indent=2)

    gap_raw = _call(
        "examflow_gap",
        system=_load_prompt("gap_detector"),
        prompt=gap_input
    )

    grounded = _call(
        "examflow_grounding",
        system=_load_prompt("grounding_gate"),
        prompt=json.dumps({
            "user_query":    user_input,
            "draft_answer":  gap_raw,
            "source_context": json.dumps(kb["disease_index"], ensure_ascii=False)
        }, ensure_ascii=False)
    )

    saved = _save_report("gaps", grounded)
    return f"{grounded}\n\n---\n*บันทึกที่ `{saved}`*"


# ─────────────────────────────────────────────────────────
# G6: สอบพรุ่งนี้ (Ultra Summary)
# ─────────────────────────────────────────────────────────
def run_g6_ultra(user_input: str) -> str:
    # Same as G3 but compact mode — no file save
    return run_g3_disease(user_input, compact=True)


# ─────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────
def _extract_disease_name(text: str) -> str:
    """Extract disease name from user query."""
    patterns = [
        r'สรุป\s+(.+?)(?:\s+ต้องรู้|\s+สำหรับสอบ|$)',
        r'สรุปเรื่อง\s+(.+)',
        r'(.+?)\s+ต้องรู้อะไร',
        r'ออกโจทย์\s+(.+)',
        r'จำลองข้อสอบ\s+(.+)',
        r'5 จุดหลัก\s+(.+)',
        r'ultra summary\s+(.+)',
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    # Fallback: return cleaned text
    return re.sub(r'(สรุป|ต้องรู้|ออกโจทย์|ฝึกทำ|สอบพรุ่งนี้|สรุปสำหรับสอบ)', '', text).strip()


# ─────────────────────────────────────────────────────────
# G7: Bulk Scope — scope note ทุกโรคใน exam_kb → Obsidian
# ─────────────────────────────────────────────────────────
def _scope_one_disease(kb: dict, disease_name: str, disease_data: dict) -> tuple[str, str, bool]:
    """Generate + save scope note for one disease. Returns (disease_name, obsidian_path, ok)."""
    questions = _get_disease_questions(kb, disease_name)
    payload = json.dumps({
        "disease_name":  disease_name,
        "disease_index": disease_data,
        "questions":     questions,
    }, ensure_ascii=False, indent=2)

    note = _call(
        "examflow_scope",
        system=_load_prompt("scope_disease"),
        prompt=payload,
    )

    safe_name = re.sub(r'[^\w\s-]', '', disease_name).strip().replace(' ', '_')
    filename = f"scope_{safe_name}.md"
    ok = _save_to_obsidian(note, "05 - Exam Scope", filename)
    return disease_name, filename, ok


def run_g7_scope_all(user_input: str) -> str:
    kb = _load_exam_kb()
    if not kb:
        return _kb_empty_response()

    diseases = kb.get("disease_index", {})
    if not diseases:
        return "[ExamFlow G7] ไม่พบโรคใน exam_kb — กรุณา ingest ข้อสอบก่อน"

    total = len(diseases)
    print(f"[ExamFlow G7] Generating scope notes for {total} diseases...")

    done, errors = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        futures = {
            ex.submit(_scope_one_disease, kb, name, data): name
            for name, data in diseases.items()
        }
        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            disease_name = futures[future]
            try:
                name, filename, ok = future.result()
                status = "✅" if ok else "⚠️"
                print(f"  [{i}/{total}] {status} {name}")
                done.append(f"{status} {name} → {filename}")
            except Exception as e:
                print(f"  [{i}/{total}] ❌ {disease_name}: {e}")
                errors.append(f"❌ {disease_name}: {e}")

    summary_lines = [
        f"## ExamFlow G7 — Bulk Scope Complete",
        f"โรคทั้งหมด: {total} | สำเร็จ: {len(done) - len(errors)} | Error: {len(errors)}",
        f"บันทึกไปที่ Obsidian vault/05 - Exam Scope/",
        "",
        "### รายการ",
    ] + done + (["", "### Errors"] + errors if errors else [])

    return "\n".join(summary_lines)


# ─────────────────────────────────────────────────────────
# Main dispatcher
# ─────────────────────────────────────────────────────────
def run_examflow_branch(branch: str, user_input: str, directives: list = None) -> str:
    """Called from session.py._run_pipeline() for G1–G6 branches."""
    print(f"[ExamFlow] Branch {branch} — {user_input[:60]}")

    dispatch = {
        "G1": run_g1_scope,
        "G2": run_g2_analysis,
        "G3": run_g3_disease,
        "G4": run_g4_vignette,
        "G5": run_g5_gap,
        "G6": run_g6_ultra,
    }

    fn = dispatch.get(branch)
    if fn is None:
        return f"[ExamFlow] Unknown branch: {branch}"

    try:
        return fn(user_input)
    except Exception as e:
        print(f"[ExamFlow] Branch {branch} error: {e}")
        return f"[ExamFlow Error] {e}"
