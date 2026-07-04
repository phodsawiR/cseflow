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


def _strip_fence(text: str) -> str:
    """Some models occasionally wrap their entire answer in a ``` code fence
    (e.g. ```markdown ... ```). Left in place, Obsidian renders the whole note
    as flat monospace text — callouts, wikilinks, and color spans all break."""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r'^```[a-zA-Z]*\n?', '', t, count=1)
        t = re.sub(r'\n?```$', '', t, count=1)
        return t.strip()
    return text


def _call(agent_name: str, prompt: str, system: str = "") -> str:
    from router import call_agent
    return _strip_fence(call_agent(agent_name, prompt=prompt, system=system))


def _grounding_prompt(user_query: str, draft_answer: str, source_context: str) -> str:
    """Build the grounding_gate prompt as delimited plain text instead of a JSON
    blob. draft_answer is markdown that often contains literal quotes (span class
    attributes, quoted MCQ choice text like "D. ..."). json.dumps()-wrapping it
    forces those quotes through an escape/unescape round-trip that Gemini Flash
    sometimes fails to undo, leaking literal \\" into the saved note."""
    return (
        f"user_query:\n{user_query}\n\n"
        f"draft_answer:\n{draft_answer}\n\n"
        f"source_context:\n{source_context}"
    )


def _get_disease_data(kb: dict, disease_name: str) -> dict:
    """Find disease in disease_index — exact match first, then closest-length
    substring fallback (prevents e.g. "Portal Hypertension" silently matching
    the unrelated, shorter "Hypertension" entry just because it iterates first)."""
    name_lower = disease_name.strip().lower()
    di = kb["disease_index"]

    for key, val in di.items():
        if key.lower() == name_lower:
            return {"disease_name": key, **val}

    candidates = [
        (key, val) for key, val in di.items()
        if name_lower in key.lower() or key.lower() in name_lower
    ]
    if not candidates:
        return {}
    key, val = min(candidates, key=lambda kv: abs(len(kv[0]) - len(disease_name)))
    return {"disease_name": key, **val}


def _get_disease_questions(kb: dict, disease_name: str) -> list[dict]:
    """Get all question objects for a disease."""
    data = _get_disease_data(kb, disease_name)
    if not data:
        return []
    ids = set(data.get("question_ids", []))
    return [q for q in kb["questions"] if q["id"] in ids]


def _get_vault_disease_files() -> list[str]:
    """List .md files across both places disease notes live: '02 - Diseases/'
    (hand-curated notes) and '05 - Exam Scope/MCQ/' (G3-generated exam summaries)."""
    dirs = [os.path.join(OBSIDIAN_PATH, "02 - Diseases"),
            os.path.join(OBSIDIAN_PATH, "05 - Exam Scope", "MCQ")]
    files = []
    for d in dirs:
        if os.path.exists(d):
            files.extend(glob.glob(os.path.join(d, "**", "*.md"), recursive=True))
    if not files:
        files = glob.glob(os.path.join(OBSIDIAN_PATH, "**", "*.md"), recursive=True)
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
        prompt=_grounding_prompt(user_input, scope_raw, json.dumps({
            "questions":     questions,
            "disease_index": kb["disease_index"],
        }, ensure_ascii=False))
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
    # _call(agent_name, prompt, system) — note: prompt before system
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f_pattern    = ex.submit(_call, "examflow_pattern",    kb_summary, _load_prompt("pattern_finder"))
        f_distractor = ex.submit(_call, "examflow_distractor", kb_summary, _load_prompt("distractor_analyzer"))
        pattern_raw    = f_pattern.result()
        distractor_raw = f_distractor.result()

    merged = f"{pattern_raw}\n\n---\n\n## Distractor Analysis\n\n{distractor_raw}"

    grounded = _call(
        "examflow_grounding",
        system=_load_prompt("grounding_gate"),
        prompt=_grounding_prompt(user_input, merged, json.dumps(kb["pattern_analysis"], ensure_ascii=False))
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
    sample_questions = _get_disease_questions(kb, disease_data["disease_name"])[:3]
    architect_input = json.dumps({
        "disease_name":      disease_data["disease_name"],
        "exam_data":         disease_data,
        "sample_questions":  sample_questions,
        "today":             datetime.now().strftime("%Y-%m-%d"),
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
        prompt=_grounding_prompt(user_input, disease_draft, json.dumps(disease_data, ensure_ascii=False))
    )
    # Fall back to ungrounded draft if grounding gate fails
    if grounded.startswith("[ERROR"):
        grounded = disease_draft

    if compact:
        return grounded

    # Full mode: format for Obsidian (separate agent from grounding gate)
    formatted = _call(
        "examflow_obsidian",
        system=_load_prompt("obsidian_formatter"),
        prompt=grounded
    )
    # Fall back to grounded content if obsidian formatter fails
    if formatted.startswith("[ERROR"):
        formatted = grounded

    # Save to reports/
    safe_name = _sanitize_disease_name(disease_data["disease_name"])
    report_name = f"disease_{safe_name}_{datetime.now().strftime('%Y%m%d')}.md"
    report_path = os.path.join(REPORTS_DIR, report_name)
    os.makedirs(REPORTS_DIR, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(formatted)

    # Auto-copy to Obsidian vault — MCQ subfolder under Exam Scope (grouped with
    # other exam-derived content for now; user may split it out later)
    obsidian_ok = _save_to_obsidian(formatted, os.path.join("05 - Exam Scope", "MCQ"), report_name)
    obsidian_msg = f"✅ บันทึกไปที่ Obsidian vault/05 - Exam Scope/MCQ/{report_name}" if obsidian_ok else "⚠️ ไม่สามารถบันทึกไป Obsidian ได้"

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
        prompt=_grounding_prompt(user_input, vignette_draft, json.dumps(exam_samples, ensure_ascii=False))
    )

    safe_name = _sanitize_disease_name(disease_name or "topic")
    saved = _save_report(f"vignette_{safe_name}", grounded)
    return f"{grounded}\n\n---\n*บันทึกที่ `{saved}`*"


def _sanitize_disease_name(name: str) -> str:
    """Same sanitization G3 uses to build a filename from a disease name — reused
    here so vault-coverage matching can never drift out of sync with it (e.g. an
    apostrophe in "Graves' disease" must be stripped identically on both sides,
    or an already-covered disease with punctuation in its name looks like a gap)."""
    return re.sub(r'[^\w\s-]', '', name).strip().replace(' ', '_')


def _vault_filename_tokens(filename: str) -> frozenset:
    name = re.sub(r'\.md$', '', filename, flags=re.IGNORECASE)
    name = _sanitize_disease_name(name)
    name = re.sub(r'[_\-]+', ' ', name)
    return frozenset(name.strip().casefold().split())


def _match_vault_coverage(disease_names: list[str], vault_files: list[str]) -> dict[str, str | None]:
    """Deterministic disease -> vault filename matcher (case/word-order-insensitive).
    LLMs are unreliable at exact-matching a disease name against 100+ filenames in one
    pass, so this does the matching in code and only hands the LLM a yes/no + filename."""
    vault_norm = [(f, _vault_filename_tokens(f)) for f in vault_files]
    coverage: dict[str, str | None] = {}
    for d in disease_names:
        d_tokens = _vault_filename_tokens(d + ".md")
        if not d_tokens:
            coverage[d] = None
            continue
        exact = [f for f, t in vault_norm if t == d_tokens]
        if exact:
            coverage[d] = exact[0]
            continue
        # fallback: disease name's tokens are a subset of the filename's tokens
        # (e.g. "Meningitis" ⊆ "Tuberculous meningitis") — prefer the closest (smallest) match
        candidates = sorted(
            (f for f, t in vault_norm if d_tokens <= t),
            key=lambda f: len(_vault_filename_tokens(f))
        )
        coverage[d] = candidates[0] if candidates else None
    return coverage


# ─────────────────────────────────────────────────────────
# G5: Gap Detector
# ─────────────────────────────────────────────────────────
def run_g5_gap(user_input: str) -> str:
    kb = _load_exam_kb()
    if not kb:
        return _kb_empty_response()

    vault_files = _get_vault_disease_files()
    coverage = _match_vault_coverage(list(kb["disease_index"].keys()), vault_files)

    # Attach precomputed coverage directly onto each disease entry — the LLM formats
    # this, it does not have to fuzzy-match 500+ diseases against 170+ filenames itself.
    disease_index_annotated = {
        name: {**data, "vault_file": coverage[name], "vault_covered": coverage[name] is not None}
        for name, data in kb["disease_index"].items()
    }

    gap_input = json.dumps({
        "disease_index":   disease_index_annotated,
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
        prompt=_grounding_prompt(user_input, gap_raw, json.dumps(disease_index_annotated, ensure_ascii=False))
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
    # Strip [branch Gx] / [branch U] prefix first
    text = re.sub(r'\[branch\s*[gGuU]\d*\]\s*', '', text, flags=re.IGNORECASE).strip()

    patterns = [
        r'สรุปโรค\s+(.+)',
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
    # Fallback: strip Thai command words
    return re.sub(r'(สรุปโรค|สรุป|ต้องรู้|ออกโจทย์|ฝึกทำ|สอบพรุ่งนี้|สรุปสำหรับสอบ)', '', text).strip()


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

    safe_name = _sanitize_disease_name(disease_name)
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
        "G7": run_g7_scope_all,
    }

    fn = dispatch.get(branch)
    if fn is None:
        return f"[ExamFlow] Unknown branch: {branch}"

    try:
        return fn(user_input)
    except Exception as e:
        print(f"[ExamFlow] Branch {branch} error: {e}")
        return f"[ExamFlow Error] {e}"
