"""
ExamFlow — PDF Ingestion Pipeline
Extracts questions from exam PDFs into exam_kb.json
Usage:
  python examflow/ingest_exam.py --pdf inbox/exams/exam_2023.pdf [--year 2023]
  python examflow/ingest_exam.py --dedup          # remove duplicates from existing KB
"""
import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import fitz  # pymupdf
import jsonschema
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

EXAM_KB_PATH = os.getenv("EXAM_KB_PATH", "./examflow/exam_kb.json")
SCHEMA_PATH  = os.path.join(os.path.dirname(__file__), "exam_kb_schema.json")
PROMPT_PATH  = os.path.join(os.path.dirname(__file__), "..", "prompts", "examflow", "extraction_agent.md")

gemini_client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
_GEMINI_FLASH_MODEL = "gemini-3.5-flash"

_OLLAMA_URL   = os.getenv("OLLAMA_URL", "http://localhost:11434")
_OLLAMA_MODEL = os.getenv("OLLAMA_EXTRACTION_MODEL", "qwen2.5:14b")


# ── Fingerprint helpers ────────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    """Strip spaces/punctuation/newlines, lowercase — for duplicate detection."""
    text = text.lower()
    text = re.sub(r"[\s\W]+", "", text, flags=re.UNICODE)
    return text


def _fingerprint(question_text: str) -> str:
    """SHA-256 of first 200 normalized chars — catches same stem with minor edits."""
    norm = _normalize(question_text)[:200]
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _kb_fingerprints(kb: dict) -> set[str]:
    """Build fingerprint set from all questions currently in KB."""
    return {_fingerprint(q["question_text"]) for q in kb["questions"] if q.get("question_text")}


# ── KB I/O ─────────────────────────────────────────────────────────────────────

def _load_schema() -> dict:
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        return json.load(f)


def _load_prompt() -> str:
    with open(PROMPT_PATH, encoding="utf-8") as f:
        return f.read()


def _load_or_init_kb() -> dict:
    if os.path.exists(EXAM_KB_PATH):
        kb = json.loads(Path(EXAM_KB_PATH).read_text(encoding="utf-8"))
        # back-compat: add notes array if missing (old KB files)
        if "notes" not in kb:
            kb["notes"] = []
        if "total_notes" not in kb["metadata"]:
            kb["metadata"]["total_notes"] = 0
        return kb
    return {
        "metadata": {
            "generated_at": datetime.now().isoformat(),
            "source_files": [],
            "total_questions": 0,
            "total_notes": 0,
            "schema_version": "1.1"
        },
        "questions": [],
        "notes": [],
        "disease_index": {},
        "system_index": {},
        "pattern_analysis": {
            "most_frequent_diseases": [],
            "topic_type_distribution": {},
            "cross_year_trends": [],
            "predicted_high_yield_next": []
        }
    }


def _save_kb(kb: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(EXAM_KB_PATH)), exist_ok=True)
    with open(EXAM_KB_PATH, "w", encoding="utf-8") as f:
        json.dump(kb, f, ensure_ascii=False, indent=2)


# ── PDF processing ─────────────────────────────────────────────────────────────

def _extract_text_from_pdf(pdf_path: str) -> list[str]:
    doc = fitz.open(pdf_path)
    pages = []
    for page in doc:
        pages.append(page.get_text())
    doc.close()
    return pages


def _split_into_questions(pages: list[str]) -> list[str]:
    full_text = "\n".join(pages)
    patterns = [
        r'\n(?=\d{1,3}[\.\)]\s)',          # "63. A" with space
        r'\n(?=\d{1,3}[\.\)][A-Z\d])',     # "63.A" without space (Thai exam PDFs)
        r'\n(?=ข้อที่\s*\d+)',
        r'\n(?=Question\s*\d+)',
    ]
    for pattern in patterns:
        chunks = re.split(pattern, full_text)
        if len(chunks) > 5:
            return [c.strip() for c in chunks if c.strip() and len(c.strip()) > 50]
    return [p.strip() for p in pages if p.strip() and len(p.strip()) > 50]


# ── Extraction agent (Gemini or Ollama) ────────────────────────────────────────

def _call_extraction_agent(question_text: str, system_prompt: str, use_local: bool = False) -> dict | None:
    if use_local:
        return _call_extraction_agent_local(question_text, system_prompt)
    return _call_extraction_agent_gemini(question_text, system_prompt)


def _call_extraction_agent_gemini(question_text: str, system_prompt: str) -> dict | None:
    content = f"{system_prompt}\n\nCHUNK TEXT:\n{question_text}"
    try:
        response = gemini_client.models.generate_content(
            model=_GEMINI_FLASH_MODEL,
            contents=content,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        raw = response.text.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```json?\n?|```$", "", raw, flags=re.MULTILINE).strip()
        return json.loads(raw)
    except (json.JSONDecodeError, Exception) as e:
        print(f"  [WARN] Extraction failed: {e}")
        return None


def _call_extraction_agent_local(question_text: str, system_prompt: str) -> dict | None:
    try:
        import ollama as _ollama
        import os as _os
        _threads = int(_os.getenv("OLLAMA_NUM_THREADS") or _os.cpu_count() or 16)
        response = _ollama.chat(
            model=_OLLAMA_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": f"CHUNK TEXT:\n{question_text}"}
            ],
            format="json",
            options={"temperature": 0.1, "num_thread": _threads}
        )
        raw = response["message"]["content"].strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```json?\n?|```$", "", raw, flags=re.MULTILINE).strip()
        return json.loads(raw)
    except (json.JSONDecodeError, Exception) as e:
        print(f"  [WARN] Extraction failed (local): {e}")
        return None


_VALID_TOPIC_TYPES    = {"diagnosis", "investigation", "management", "mechanism", "complication", "pharmacology", "other"}
_VALID_DIFFICULTIES   = {"must_know", "high_yield", "nice_to_know", "avoid"}
_VALID_PATTERNS       = {"vignette_with_labs", "image_based", "single_best", "recall", "calculation"}
_VALID_ANSWER_KEYS    = {"A", "B", "C", "D", "E", None}
_TOPIC_REMAP          = {"contraindication": "management", "prophylaxis": "management",
                         "treatment": "management", "pathophysiology": "mechanism",
                         "etiology": "mechanism", "comorbidity": "complication"}


def _normalize_extracted(item: dict) -> dict:
    """Coerce qwen/local-model quirks before schema validation."""
    # answer_key: lowercase → uppercase, composite 'A|B' → None
    ak = item.get("answer_key")
    if isinstance(ak, str):
        ak = ak.strip()
        if len(ak) == 1 and ak.upper() in "ABCDE":
            item["answer_key"] = ak.upper()
        else:
            item["answer_key"] = None

    # topic_type: remap known aliases, blank → "other"
    tt = item.get("topic_type", "")
    if not tt or tt not in _VALID_TOPIC_TYPES:
        item["topic_type"] = _TOPIC_REMAP.get(tt, "other")

    # difficulty: blank → "high_yield"
    if item.get("difficulty", "") not in _VALID_DIFFICULTIES:
        item["difficulty"] = "high_yield"

    # question_pattern: "other" and unknown → "single_best"
    if item.get("question_pattern", "") not in _VALID_PATTERNS:
        item["question_pattern"] = "single_best"

    return item


def _validate_question(q: dict, schema: dict) -> bool:
    try:
        question_schema = schema["properties"]["questions"]["items"]
        jsonschema.validate(instance=q, schema=question_schema)
        return True
    except jsonschema.ValidationError as e:
        print(f"  [WARN] Validation error: {e.message[:100]}")
        return False


# ── Index builder ──────────────────────────────────────────────────────────────

def _fold_key(name: str) -> str:
    """Case- and word-order-insensitive grouping key, e.g. 'Type 2 Diabetes Mellitus'
    and 'diabetes mellitus type 2' fold to the same key so they merge into one entry."""
    return " ".join(sorted(name.strip().casefold().split()))


def _canonical_name_map(counts: dict[str, int]) -> dict[str, str]:
    """Group raw extracted names (disease or system) that only differ by case/word
    order, and pick the most frequent exact spelling per group as the display name
    (ties broken alphabetically) so downstream tools see one entry per real-world topic."""
    groups: dict[str, list[str]] = {}
    for name in counts:
        groups.setdefault(_fold_key(name), []).append(name)
    canon_map: dict[str, str] = {}
    for variants in groups.values():
        canonical = max(variants, key=lambda v: (counts[v], v))
        for v in variants:
            canon_map[v] = canonical
    return canon_map


def _build_indexes(kb: dict) -> None:
    # ── Pre-pass: canonicalize disease/system names so "Wilson disease" and
    # "Wilson Disease" (or "Type 2 diabetes mellitus" / "Diabetes Mellitus Type 2")
    # collapse into a single disease_index entry instead of silently fragmenting
    # frequency counts across near-duplicate keys ──────────────────────────────
    disease_counts: dict = {}
    system_counts: dict = {}
    for q in kb["questions"]:
        for d in q.get("diseases", []):
            disease_counts[d] = disease_counts.get(d, 0) + 1
        for s in q.get("systems", []):
            system_counts[s] = system_counts.get(s, 0) + 1
    for n in kb.get("notes", []):
        for d in n.get("diseases", []):
            disease_counts[d] = disease_counts.get(d, 0) + 1
        for s in n.get("systems", []):
            system_counts[s] = system_counts.get(s, 0) + 1

    disease_canon = _canonical_name_map(disease_counts)
    system_canon  = _canonical_name_map(system_counts)

    def _cd(name: str) -> str:
        return disease_canon.get(name, name.strip())

    def _cs(name: str) -> str:
        return system_canon.get(name, name.strip())

    disease_idx: dict = {}
    system_idx: dict  = {}
    topic_counts: dict = {}

    def _ensure_disease(name: str):
        if name not in disease_idx:
            disease_idx[name] = {
                "question_ids": [], "note_ids": [], "frequency": 0, "years_appeared": [],
                "key_investigations": [], "key_management": [],
                "must_know_facts": [], "common_distractors": [], "trend": "stable"
            }

    def _ensure_system(name: str):
        if name not in system_idx:
            system_idx[name] = {"question_ids": [], "note_ids": [], "diseases": [], "top_topics": []}

    # ── Index questions ──────────────────────────────────────────────────────
    for q in kb["questions"]:
        for disease in q.get("diseases", []):
            disease = _cd(disease)
            _ensure_disease(disease)
            entry = disease_idx[disease]
            entry["question_ids"].append(q["id"])
            entry["frequency"] += 1
            year = q.get("year")
            if year and year not in entry["years_appeared"]:
                entry["years_appeared"].append(year)
            for ix in q.get("investigations_mentioned", []):
                if ix not in entry["key_investigations"]:
                    entry["key_investigations"].append(ix)
            for mgmt in q.get("management_mentioned", []):
                if mgmt not in entry["key_management"]:
                    entry["key_management"].append(mgmt)
            for wrong in q.get("distractors", {}).get("common_wrong_answers", []):
                if wrong not in entry["common_distractors"]:
                    entry["common_distractors"].append(wrong)

        for system in q.get("systems", []):
            system = _cs(system)
            _ensure_system(system)
            system_idx[system]["question_ids"].append(q["id"])
            for d in q.get("diseases", []):
                d = _cd(d)
                if d not in system_idx[system]["diseases"]:
                    system_idx[system]["diseases"].append(d)

        tt = q.get("topic_type", "other")
        topic_counts[tt] = topic_counts.get(tt, 0) + 1

    # ── Index notes (boost must_know_facts + note_ids) ────────────────────────
    for n in kb.get("notes", []):
        for disease in n.get("diseases", []):
            disease = _cd(disease)
            _ensure_disease(disease)
            entry = disease_idx[disease]
            if n["id"] not in entry["note_ids"]:
                entry["note_ids"].append(n["id"])
            for pt in n.get("key_points", []):
                if pt and pt not in entry["must_know_facts"]:
                    entry["must_know_facts"].append(pt)

        for system in n.get("systems", []):
            system = _cs(system)
            _ensure_system(system)
            if n["id"] not in system_idx[system]["note_ids"]:
                system_idx[system]["note_ids"].append(n["id"])
            for d in n.get("diseases", []):
                d = _cd(d)
                if d not in system_idx[system]["diseases"]:
                    system_idx[system]["diseases"].append(d)

    # ── Trend calculation ────────────────────────────────────────────────────
    all_years = sorted(set(y for q in kb["questions"] for y in [q.get("year")] if y))
    for disease, entry in disease_idx.items():
        years = sorted(entry["years_appeared"])
        if len(years) >= 2 and all_years and years[-1] == all_years[-1] and years[-2] == all_years[-2]:
            entry["trend"] = "increasing"
        elif len(years) == 0:
            entry["trend"] = "new"

    total = len(kb["questions"]) or 1
    topic_dist = {k: round(v / total, 2) for k, v in topic_counts.items()}
    sorted_diseases = sorted(disease_idx.items(), key=lambda x: x[1]["frequency"], reverse=True)

    kb["disease_index"] = disease_idx
    kb["system_index"]  = system_idx
    kb["pattern_analysis"]["most_frequent_diseases"] = [d for d, _ in sorted_diseases[:10]]
    kb["pattern_analysis"]["topic_type_distribution"] = topic_dist
    kb["pattern_analysis"]["predicted_high_yield_next"] = [
        d for d, e in sorted_diseases[:5] if e["trend"] == "increasing"
    ]


# ── Main ingest ────────────────────────────────────────────────────────────────

def ingest_pdf(pdf_path: str, year: int | None = None, force: bool = False, use_local: bool = False) -> int:
    """Ingest one PDF into exam_kb.json. Returns number of questions added.
    force=True re-ingests even if already in source_files (dedup prevents question duplication).
    use_local=True uses Ollama instead of Gemini Flash (free, slightly slower).
    """
    pdf_path = str(Path(pdf_path).resolve())
    filename = os.path.basename(pdf_path)

    if not year:
        m = re.search(r'(20\d{2})', filename)
        year = int(m.group(1)) if m else datetime.now().year

    mode_tag = "[LOCAL]" if use_local else "[Gemini]"
    print(f"\n[ExamFlow] Ingesting: {filename} (year={year}) {mode_tag}{' [FORCE]' if force else ''}")

    kb     = _load_or_init_kb()
    schema = _load_schema()
    prompt = _load_prompt()

    if not force and filename in kb["metadata"]["source_files"]:
        print(f"[ExamFlow] Already ingested: {filename} — skipping (use --force to re-run)")
        return 0

    pages  = _extract_text_from_pdf(pdf_path)
    chunks = _split_into_questions(pages)
    print(f"[ExamFlow] Found {len(chunks)} question chunks")

    existing_q_ids = {q["id"] for q in kb["questions"]}
    existing_n_ids = {n["id"] for n in kb.get("notes", [])}
    seen_fps       = _kb_fingerprints(kb)
    session_fps: set[str] = set()

    q_counter = n_counter = skipped_dup = 0

    for i, chunk in enumerate(chunks, start=1):
        print(f"  [{i}/{len(chunks)}] Extracting...", end=" ", flush=True)
        extracted = _call_extraction_agent(chunk, prompt, use_local=use_local)

        if not extracted:
            print("SKIP (no content)")
            continue

        # ── Handle list response (Gemini sometimes returns array of items) ──
        items = extracted if isinstance(extracted, list) else [extracted]
        first = True

        for item in items:
            if not isinstance(item, dict):
                continue

            if not first:
                print(f"  [{i}/{len(chunks)}] (extra item)...", end=" ", flush=True)
            first = False

            content_type = item.get("type", "question")

            # ── Fingerprint & dedup ──────────────────────────────────────────
            if content_type == "question":
                raw_text = item.get("question_text", "")
            else:
                raw_text = item.get("content", "")

            fp = _fingerprint(raw_text) if raw_text else None
            if fp and (fp in seen_fps or fp in session_fps):
                print("SKIP (duplicate)")
                skipped_dup += 1
                continue

            # ── Route: question ──────────────────────────────────────────────
            if content_type == "question":
                q_num = i
                q_id  = f"Q{year}_{q_num:02d}"
                while q_id in existing_q_ids:
                    q_num += 1
                    q_id = f"Q{year}_{q_num:02d}"

                item.pop("type", None)
                item.update({
                    "id":              q_id,
                    "source_file":     filename,
                    "year":            year,
                    "question_number": q_num,
                })
                item = _normalize_extracted(item)

                if _validate_question(item, schema):
                    kb["questions"].append(item)
                    existing_q_ids.add(q_id)
                    if fp:
                        session_fps.add(fp)
                    q_counter += 1
                    print(f"Q -> {q_id}")
                else:
                    print("SKIP (schema invalid)")

            # ── Route: note ──────────────────────────────────────────────────
            elif content_type == "note":
                n_num = len(kb.get("notes", [])) + n_counter + 1
                n_id  = f"N{year}_{n_num:02d}"
                while n_id in existing_n_ids:
                    n_num += 1
                    n_id = f"N{year}_{n_num:02d}"

                item.pop("type", None)
                item.update({
                    "id":          n_id,
                    "source_file": filename,
                    "year":        year,
                    "note_number": n_num,
                })

                if "notes" not in kb:
                    kb["notes"] = []
                kb["notes"].append(item)
                existing_n_ids.add(n_id)
                if fp:
                    session_fps.add(fp)
                n_counter += 1
                nt = item.get("note_type", "note")
                print(f"N → {n_id} [{nt}]")

            else:
                print("SKIP (unknown type)")

    if skipped_dup:
        print(f"  [dedup] ข้ามซ้ำ {skipped_dup} ชิ้น")

    total_added = q_counter + n_counter
    if total_added > 0:
        if filename not in kb["metadata"]["source_files"]:
            kb["metadata"]["source_files"].append(filename)
        kb["metadata"]["total_questions"] = len(kb["questions"])
        kb["metadata"]["total_notes"]     = len(kb.get("notes", []))
        kb["metadata"]["generated_at"]    = datetime.now().isoformat()
        _build_indexes(kb)
        _save_kb(kb)
        print(
            f"\n[ExamFlow] Done: +{q_counter} questions, +{n_counter} notes"
            f" | Total: {kb['metadata']['total_questions']}Q / {kb['metadata']['total_notes']}N"
        )
    else:
        if filename not in kb["metadata"]["source_files"]:
            kb["metadata"]["source_files"].append(filename)
            _save_kb(kb)
        print(f"\n[ExamFlow] Done: 0 new (all duplicates or invalid)")

    return q_counter


# ── Post-ingest dedup ──────────────────────────────────────────────────────────

def dedup_kb() -> tuple[int, int]:
    """
    Remove duplicate questions AND notes from exam_kb.json in place.
    Keeps the EARLIEST occurrence. Returns (before_total, after_total).
    """
    if not os.path.exists(EXAM_KB_PATH):
        print("[dedup] exam_kb.json not found — nothing to do")
        return 0, 0

    kb = _load_or_init_kb()
    before_q = len(kb["questions"])
    before_n = len(kb.get("notes", []))
    print(f"[dedup] Loaded {before_q} questions + {before_n} notes")

    kb["questions"].sort(key=lambda q: (q.get("year") or 9999, q.get("question_number") or 999))
    kb.setdefault("notes", [])
    kb["notes"].sort(key=lambda n: (n.get("year") or 9999, n.get("note_number") or 999))

    seen: set[str] = set()

    def _dedup_list(items: list, text_key: str, label: str) -> list:
        unique = []
        removed = 0
        for item in items:
            raw = item.get(text_key, "")
            fp  = _fingerprint(raw) if raw else None
            if fp and fp in seen:
                removed += 1
                print(f"  DUP {label}: {item['id']} ({item.get('source_file','?')})")
            else:
                unique.append(item)
                if fp:
                    seen.add(fp)
        return unique

    kb["questions"] = _dedup_list(kb["questions"], "question_text", "Q")
    kb["notes"]     = _dedup_list(kb["notes"],     "content",       "N")

    after_q = len(kb["questions"])
    after_n = len(kb["notes"])
    kb["metadata"]["total_questions"] = after_q
    kb["metadata"]["total_notes"]     = after_n
    kb["metadata"]["generated_at"]    = datetime.now().isoformat()
    _build_indexes(kb)
    _save_kb(kb)

    removed = (before_q - after_q) + (before_n - after_n)
    print(
        f"\n[dedup] Questions: {before_q} → {after_q} (-{before_q - after_q})"
        f"\n[dedup] Notes:     {before_n} → {after_n} (-{before_n - after_n})"
        f"\n[dedup] Total removed: {removed}"
    )
    return before_q + before_n, after_q + after_n


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ExamFlow PDF Ingestion")
    parser.add_argument("--pdf",   help="Path to exam PDF")
    parser.add_argument("--year",  type=int, help="Exam year (auto-detect from filename)")
    parser.add_argument("--dedup",  action="store_true",
                        help="Remove duplicate questions from existing exam_kb.json")
    parser.add_argument("--force",  action="store_true",
                        help="Re-ingest even if filename already in KB (dedup prevents duplicate questions)")
    parser.add_argument("--rerun",  action="store_true",
                        help="Re-ingest ALL previously ingested files (to pick up notes with new logic)")
    parser.add_argument("--local", action="store_true",
                        help="Use local Ollama (qwen2.5:14b) instead of Gemini Flash — free but slower")
    args = parser.parse_args()

    if args.dedup:
        dedup_kb()
        sys.exit(0)

    if args.rerun:
        # Re-ingest all already-ingested files to pick up notes with new extraction logic
        if not os.path.exists(EXAM_KB_PATH):
            print("[ERROR] exam_kb.json not found")
            sys.exit(1)
        import json as _json
        kb = _json.loads(Path(EXAM_KB_PATH).read_text(encoding="utf-8"))
        done_files = kb["metadata"]["source_files"]
        if not done_files:
            print("No previously ingested files found")
            sys.exit(0)
        inbox = Path(os.getenv("EXAM_INBOX_PATH", "./inbox/exams/"))
        total_q = total_n = 0
        for fname in done_files:
            pdf = inbox / fname
            if not pdf.exists():
                print(f"[SKIP] ไม่พบไฟล์ {fname} ใน {inbox}")
                continue
            q = ingest_pdf(str(pdf), force=True, use_local=args.local)
            total_q += q
        print(f"\n[rerun] เสร็จ — รวม +{total_q} questions ใหม่")
        sys.exit(0)

    if not args.pdf:
        parser.error("--pdf is required unless --dedup or --rerun is used")

    if not os.path.exists(args.pdf):
        print(f"[ERROR] PDF not found: {args.pdf}")
        sys.exit(1)

    count = ingest_pdf(args.pdf, args.year, force=args.force, use_local=args.local)
    sys.exit(0 if count >= 0 else 1)
