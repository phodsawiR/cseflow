"""
ExamFlow — Lecture-Exam Bridge (Branch G8)
รับ PDF สไลด์เลคเชอร์ → สกัดหัวข้อ → เทียบกับข้อสอบเก่า → สรุปก่อนสอบ

Features:
  - Text-mode fallback: ถ้า PDF มีข้อความอ่านได้ → ไม่ใช้ Vision API (เร็ว 3–5x)
  - Cache: บันทึก extracted topics ไว้ → re-run ด้วย hint ต่างกันไม่ต้องสกัดซ้ำ
  - Auto G7: สร้าง scope notes ใน Obsidian สำหรับโรคที่ match แต่ยังไม่มี note

Usage:
  python examflow/lecture_bridge.py --pdf "inbox/lectures/slide.pdf"
  python examflow/lecture_bridge.py --pdf slide.pdf --hint "เน้น management" --hint "หน้า 11 ออกแน่"
  python examflow/lecture_bridge.py --pdf slide.pdf --no-cache
  python examflow/lecture_bridge.py --pdf slide.pdf --no-g7
"""
import argparse
import hashlib
import json
import os
import re
import sys
import concurrent.futures
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv()

from google import genai
from google.genai import types as _gt

EXAM_KB_PATH  = os.getenv("EXAM_KB_PATH", "./examflow/exam_kb.json")
OBSIDIAN_PATH = os.getenv("OBSIDIAN_VAULT_PATH", r"C:\Users\ASUS\Documents\Obsidian vault")
REPORTS_DIR   = "reports"
_PROMPT_DIR   = _ROOT / "prompts" / "examflow"
_CACHE_DIR    = _ROOT / "inbox" / "lectures" / ".cache"
_GEMINI_MODEL = "gemini-3.5-flash"

# ข้อความเฉลี่ยต่อหน้า (chars) ที่ถือว่าเป็น text-based PDF
_TEXT_MODE_THRESHOLD = 80

_client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))


# ── helpers ───────────────────────────────────────────────────────────────────

def _load_prompt(name: str) -> str:
    p = _PROMPT_DIR / f"{name}.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _load_exam_kb() -> dict | None:
    if not Path(EXAM_KB_PATH).exists():
        return None
    with open(EXAM_KB_PATH, encoding="utf-8") as f:
        return json.load(f)


def _save_report(prefix: str, content: str) -> str:
    os.makedirs(REPORTS_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    path = os.path.join(REPORTS_DIR, f"{prefix}_{ts}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def _save_obsidian(content: str, subfolder: str, filename: str) -> bool:
    try:
        dest_dir = os.path.join(OBSIDIAN_PATH, subfolder)
        os.makedirs(dest_dir, exist_ok=True)
        with open(os.path.join(dest_dir, filename), "w", encoding="utf-8") as f:
            f.write(content)
        return True
    except Exception as e:
        print(f"  [WARN] Obsidian save failed: {e}")
        return False


def _parse_json_response(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```json?\n?|```$", "", raw, flags=re.MULTILINE).strip()
    return json.loads(raw)


# ── Cache ─────────────────────────────────────────────────────────────────────

def _pdf_hash(pdf_path: Path) -> str:
    h = hashlib.sha256(pdf_path.read_bytes()).hexdigest()[:16]
    return h


def _cache_path(pdf_path: Path) -> Path:
    return _CACHE_DIR / f"{_pdf_hash(pdf_path)}.json"


def _load_cache(pdf_path: Path) -> dict | None:
    cp = _cache_path(pdf_path)
    if cp.exists():
        try:
            data = json.loads(cp.read_text(encoding="utf-8"))
            print(f"  [cache] HIT — ใช้ topics ที่สกัดไว้แล้ว ({cp.name})")
            return data
        except Exception:
            pass
    return None


def _save_cache(pdf_path: Path, lecture_data: dict):
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cp = _cache_path(pdf_path)
    cp.write_text(json.dumps(lecture_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  [cache] บันทึก → {cp.name}")


# ── PDF helpers ───────────────────────────────────────────────────────────────

def _open_pdf(pdf_path: Path):
    try:
        import fitz
        return fitz.open(str(pdf_path))
    except ImportError:
        print("[ERROR] pip install pymupdf")
        sys.exit(1)


def _pdf_avg_text_per_page(pdf_path: Path) -> float:
    """คำนวณ avg chars per page เพื่อตัดสินใจ text vs vision mode."""
    doc = _open_pdf(pdf_path)
    total = sum(len(page.get_text()) for page in doc)
    avg = total / max(len(doc), 1)
    doc.close()
    return avg


def _pdf_to_text_pages(pdf_path: Path) -> list[str]:
    doc = _open_pdf(pdf_path)
    pages = [page.get_text() for page in doc]
    doc.close()
    return pages


def _pdf_to_images(pdf_path: Path) -> list[tuple[bytes, str]]:
    doc = _open_pdf(pdf_path)
    mat = __import__("fitz").Matrix(2, 2)
    pages = []
    for page in doc:
        pix = page.get_pixmap(matrix=mat)
        pages.append((pix.tobytes("png"), "image/png"))
    doc.close()
    return pages


# ── Step 1A: Text-mode extraction ────────────────────────────────────────────

_TEXT_EXTRACT_PROMPT = """
คุณกำลังอ่านเนื้อหาสไลด์เลคเชอร์อายุรศาสตร์ปี 4 (text เต็ม ไม่ใช่รูปภาพ)

สกัด JSON ดังนี้:
{
  "lecture_title": "ชื่อเลคเชอร์ (ถ้าไม่มีใส่ null)",
  "topics": [
    {
      "topic": "ชื่อโรค/หัวข้อ (ภาษาอังกฤษ)",
      "key_points": ["key concept ที่อาจออกสอบ", ...],
      "diseases_mentioned": ["โรคที่กล่าวถึง", ...],
      "investigations_mentioned": ["investigation", ...],
      "management_mentioned": ["การรักษา", ...]
    }
  ]
}

กฎ:
- แยกแต่ละโรค/หัวข้อหลักเป็น topic แยก
- key_points เน้นสิ่งที่อาจออกสอบ (criteria, threshold, drug of choice, ข้อบ่งชี้)
- ตอบเป็น JSON เท่านั้น ไม่มี markdown
"""

def _extract_text_mode(pdf_path: Path) -> dict:
    pages = _pdf_to_text_pages(pdf_path)
    total_pages = len(pages)
    print(f"  [text-mode] {total_pages} pages → Gemini Flash (text)...")

    batch_size = 20  # text ส่งได้มากกว่า vision ต่อ request
    all_topics = []
    lecture_title = None

    for i in range(0, total_pages, batch_size):
        batch_text = "\n\n--- หน้า {} ---\n".format(i + 1).join(pages[i:i + batch_size])
        prompt = f"{_TEXT_EXTRACT_PROMPT}\n\nเนื้อหาสไลด์:\n{batch_text[:40000]}"
        try:
            resp = _client.models.generate_content(
                model=_GEMINI_MODEL,
                contents=prompt,
                config=_gt.GenerateContentConfig(max_output_tokens=8192),
            )
            data = _parse_json_response(resp.text)
            if not lecture_title and data.get("lecture_title"):
                lecture_title = data["lecture_title"]
            all_topics.extend(data.get("topics", []))
            print(f"  [text] batch {i // batch_size + 1}/{-(-total_pages // batch_size)} → {len(data.get('topics', []))} topics")
        except Exception as e:
            print(f"  [WARN] text batch {i // batch_size + 1} failed: {e}")

    return {"lecture_title": lecture_title, "topics": all_topics}


# ── Step 1B: Vision-mode extraction ──────────────────────────────────────────

_VISION_EXTRACT_PROMPT = """
คุณกำลังดูสไลด์เลคเชอร์ของนิสิตแพทย์ปี 4 อายุรศาสตร์

สกัด JSON ดังนี้:
{
  "lecture_title": "ชื่อเลคเชอร์จากสไลด์ (ถ้าไม่มีให้ใส่ null)",
  "topics": [
    {
      "topic": "ชื่อโรค/หัวข้อ (ภาษาอังกฤษ)",
      "key_points": ["key concept 1", "key concept 2", ...],
      "diseases_mentioned": ["ชื่อโรคที่กล่าวถึง"],
      "investigations_mentioned": ["investigation ที่กล่าวถึง"],
      "management_mentioned": ["การรักษาที่กล่าวถึง"]
    }
  ]
}

กฎ:
- แยกแต่ละโรค/หัวข้อหลักเป็น topic แยก
- key_points คือจุดสำคัญที่อาจออกสอบ
- ตอบเป็น JSON เท่านั้น ไม่มี markdown
"""

def _extract_vision_mode(pdf_path: Path) -> dict:
    pages = _pdf_to_images(pdf_path)
    total_pages = len(pages)
    print(f"  [vision-mode] {total_pages} pages → Gemini Vision (batch 10)...")

    batch_size = 10
    all_topics = []
    lecture_title = None

    for i in range(0, total_pages, batch_size):
        batch = pages[i:i + batch_size]
        parts = [_gt.Part(inline_data=_gt.Blob(data=b, mime_type=m)) for b, m in batch]
        parts.append(_gt.Part(text=_VISION_EXTRACT_PROMPT))
        try:
            resp = _client.models.generate_content(
                model=_GEMINI_MODEL,
                contents=parts,
                config=_gt.GenerateContentConfig(max_output_tokens=8192),
            )
            data = _parse_json_response(resp.text)
            if not lecture_title and data.get("lecture_title"):
                lecture_title = data["lecture_title"]
            all_topics.extend(data.get("topics", []))
            print(f"  [vision] batch {i // batch_size + 1}/{-(-total_pages // batch_size)} → {len(data.get('topics', []))} topics")
        except Exception as e:
            print(f"  [WARN] vision batch {i // batch_size + 1} failed: {e}")

    return {"lecture_title": lecture_title, "topics": all_topics}


# ── Step 1: Consolidate after multi-batch ────────────────────────────────────

def _consolidate(all_topics: list, lecture_title: str, n_pages: int) -> list:
    """Merge duplicate topics across batches (only if multi-batch)."""
    if n_pages <= 10:
        return all_topics

    print(f"  [consolidate] merge {len(all_topics)} raw topics...")
    prompt = f"""
topics เหล่านี้สกัดจากสไลด์หลายหน้า อาจมีซ้ำกัน
รวม topics ที่เป็นเรื่องเดียวกัน และ deduplicate key_points ที่ซ้ำกัน
ตอบ JSON:
{{"lecture_title": "{lecture_title or 'Lecture'}", "topics": [
  {{"topic": "...", "key_points": [...], "diseases_mentioned": [...],
    "investigations_mentioned": [...], "management_mentioned": [...]}}
]}}

Input:
{json.dumps(all_topics, ensure_ascii=False)[:30000]}

ตอบ JSON เท่านั้น ไม่มี markdown
"""
    try:
        resp = _client.models.generate_content(
            model=_GEMINI_MODEL,
            contents=prompt,
            config=_gt.GenerateContentConfig(max_output_tokens=8192),
        )
        merged = _parse_json_response(resp.text)
        result = merged.get("topics", all_topics)
        print(f"  [consolidate] {len(all_topics)} → {len(result)} unique topics")
        return result
    except Exception as e:
        print(f"  [WARN] consolidate failed ({e}) — ใช้ raw topics")
        return all_topics


# ── Step 1 (main): Extract topics from PDF ───────────────────────────────────

def _extract_topics_from_pdf(pdf_path: Path, use_cache: bool = True) -> dict:
    # Cache check
    if use_cache:
        cached = _load_cache(pdf_path)
        if cached:
            return cached

    # Detect mode
    avg_chars = _pdf_avg_text_per_page(pdf_path)
    print(f"  [detect] avg {avg_chars:.0f} chars/page → ", end="")

    if avg_chars >= _TEXT_MODE_THRESHOLD:
        print("text-mode (ประหยัด Vision API)")
        raw = _extract_text_mode(pdf_path)
    else:
        print("vision-mode (image-heavy slides)")
        raw = _extract_vision_mode(pdf_path)

    lecture_title = raw.get("lecture_title")
    n_pages = sum(1 for _ in _open_pdf(pdf_path))  # re-count pages (already cached by OS)
    all_topics = _consolidate(raw.get("topics", []), lecture_title or "", n_pages)

    lecture_data = {"lecture_title": lecture_title, "topics": all_topics}

    # Save cache
    if use_cache:
        _save_cache(pdf_path, lecture_data)

    return lecture_data


# ── Step 2: Cross-reference with exam_kb ─────────────────────────────────────

def _fuzzy_match(name: str, kb_keys: list[str]) -> list[str]:
    name_l = name.lower()
    matched = []
    for key in kb_keys:
        key_l = key.lower()
        if name_l in key_l or key_l in name_l:
            matched.append(key)
            continue
        name_words = {w for w in re.split(r'\W+', name_l) if len(w) > 3}
        key_words  = {w for w in re.split(r'\W+', key_l)  if len(w) > 3}
        if name_words & key_words:
            matched.append(key)
    return matched


def _cross_reference(lecture_data: dict, kb: dict) -> tuple[list, list]:
    kb_keys = list(kb["disease_index"].keys())
    q_by_id = {q["id"]: q for q in kb["questions"]}
    matched, unmatched = [], []

    all_topic_names: set[str] = set()
    for t in lecture_data.get("topics", []):
        all_topic_names.add(t["topic"])
        for d in t.get("diseases_mentioned", []):
            all_topic_names.add(d)

    for topic_name in all_topic_names:
        hits = _fuzzy_match(topic_name, kb_keys)
        if not hits:
            unmatched.append(topic_name)
            continue

        agg: dict = {
            "topic":               topic_name,
            "matched_kb_names":    hits,
            "frequency":           0,
            "question_ids":        [],
            "questions":           [],
            "must_know_facts":     [],
            "key_investigations":  [],
            "key_management":      [],
            "common_distractors":  [],
            "trend":               "stable",
        }
        for kb_name in hits:
            entry = kb["disease_index"][kb_name]
            agg["frequency"]          += entry.get("frequency", 0)
            agg["question_ids"]       += entry.get("question_ids", [])
            agg["must_know_facts"]    += entry.get("must_know_facts", [])
            agg["key_investigations"] += entry.get("key_investigations", [])
            agg["key_management"]     += entry.get("key_management", [])
            agg["common_distractors"] += entry.get("common_distractors", [])
            if entry.get("trend") in ("increasing", "new"):
                agg["trend"] = entry["trend"]

        for field in ("question_ids", "must_know_facts", "key_investigations",
                      "key_management", "common_distractors"):
            agg[field] = list(dict.fromkeys(agg[field]))

        agg["questions"] = [
            {
                "id":            q["id"],
                "question_text": q.get("question_text", "")[:300],
                "diseases":      q.get("diseases", []),
                "distractors":   q.get("distractors", {}),
            }
            for qid in agg["question_ids"][:5]
            if (q := q_by_id.get(qid))
        ]

        if agg["frequency"] > 0 or agg["question_ids"]:
            matched.append(agg)
        else:
            unmatched.append(topic_name)

    matched.sort(key=lambda x: x["frequency"], reverse=True)
    return matched, list(set(unmatched))


# ── Step 3: Generate summary ──────────────────────────────────────────────────

def _generate_summary(lecture_data: dict, matched: list, unmatched: list,
                      hints: list[str] | None = None) -> str:
    from router import call_agent

    payload = {
        "lecture_title":    lecture_data.get("lecture_title") or "Lecture",
        "lecture_topics":   lecture_data.get("topics", []),
        "matched":          matched,
        "unmatched_topics": unmatched,
        "user_hints":       hints or [],
    }

    print("  [aligner] lecture_aligner → summarizing...")
    raw = call_agent(
        "examflow_lecture",
        system=_load_prompt("lecture_aligner"),
        prompt=json.dumps(payload, ensure_ascii=False, indent=2),
    )

    print("  [grounding] grounding_gate → verifying...")
    grounded = call_agent(
        "examflow_grounding",
        system=_load_prompt("grounding_gate"),
        prompt=json.dumps({
            "user_query":     f"สรุปก่อนสอบจาก lecture: {payload['lecture_title']}",
            "draft_answer":   raw,
            "source_context": json.dumps({
                "matched_exam_data": matched,
                "lecture_topics":    lecture_data.get("topics", []),
            }, ensure_ascii=False),
        }, ensure_ascii=False),
    )
    return grounded


# ── Step 4 (Auto G7): Scope notes for matched diseases ───────────────────────

def _auto_g7(matched: list, kb: dict):
    """สร้าง scope note ใน Obsidian สำหรับโรคที่ match แต่ยังไม่มี note."""
    from examflow.pipeline import _scope_one_disease

    scope_dir = Path(OBSIDIAN_PATH) / "05 - Exam Scope"
    existing  = {f.stem.lower() for f in scope_dir.glob("*.md")} if scope_dir.exists() else set()

    # รวม kb names จากทุก matched topic
    all_kb_names: list[str] = []
    for m in matched:
        for kb_name in m.get("matched_kb_names", []):
            if kb_name not in all_kb_names:
                all_kb_names.append(kb_name)

    # กรองเฉพาะที่ยังไม่มี note
    missing = []
    for kb_name in all_kb_names:
        safe = re.sub(r"[^\w\-]", "_", kb_name).lower()
        if not any(safe in ex or ex in safe for ex in existing):
            missing.append(kb_name)

    if not missing:
        print("  [G7] ✅ มี scope note ครบแล้วทุกโรคที่ match")
        return

    print(f"  [G7] สร้าง scope notes สำหรับ {len(missing)} โรคที่ยังไม่มี...")
    diseases_data = kb.get("disease_index", {})

    done, errors = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        futures = {
            ex.submit(_scope_one_disease, kb, name, diseases_data.get(name, {})): name
            for name in missing
            if name in diseases_data
        }
        for i, fut in enumerate(concurrent.futures.as_completed(futures), 1):
            disease_name = futures[fut]
            try:
                _, filename, ok = fut.result()
                status = "✅" if ok else "⚠️"
                print(f"  [G7] [{i}/{len(missing)}] {status} {disease_name}")
                (done if ok else errors).append(disease_name)
            except Exception as e:
                print(f"  [G7] [{i}/{len(missing)}] ❌ {disease_name}: {e}")
                errors.append(disease_name)

    print(f"  [G7] สำเร็จ {len(done)} | error {len(errors)}")


# ── Main entry point ──────────────────────────────────────────────────────────

def run_lecture_bridge(pdf_path: str, title_override: str = "",
                       hints: list[str] | None = None,
                       use_cache: bool = True,
                       auto_g7: bool = True) -> str:
    pdf = Path(pdf_path)
    if not pdf.exists():
        return f"[ERROR] ไม่พบไฟล์: {pdf_path}"

    kb = _load_exam_kb()
    if not kb:
        return (
            "[ERROR] ไม่พบ exam_kb.json\n"
            "กรุณา ingest ข้อสอบก่อน:\n"
            "  python examflow/ingest_exam.py --pdf inbox/exams/exam.pdf"
        )

    print(f"\n[G8] Lecture-Exam Bridge: {pdf.name}")
    print("─" * 50)

    # Step 1: extract (text-mode / vision-mode + cache)
    print("[1/3] สกัดหัวข้อจากสไลด์...")
    lecture_data = _extract_topics_from_pdf(pdf, use_cache=use_cache)
    if title_override:
        lecture_data["lecture_title"] = title_override
    if not lecture_data.get("lecture_title"):
        lecture_data["lecture_title"] = pdf.stem.replace("_", " ")
    print(f"  → {len(lecture_data.get('topics', []))} topics")

    # Step 2: cross-reference
    print("[2/3] เทียบกับ exam KB...")
    matched, unmatched = _cross_reference(lecture_data, kb)
    print(f"  → matched: {len(matched)} | unmatched: {len(unmatched)}")

    # Step 3: generate summary
    print("[3/3] สร้างสรุปก่อนสอบ...")
    if hints:
        print(f"  [hints] {len(hints)} hints → aligner")
    summary = _generate_summary(lecture_data, matched, unmatched, hints)

    # Save
    safe_title   = re.sub(r"[^\w\-]", "_", lecture_data["lecture_title"])[:40]
    report_path  = _save_report(f"lecture_{safe_title}", summary)
    obs_filename = f"Lecture_{safe_title}_{datetime.now().strftime('%Y%m%d')}.md"
    print(f"\n  ✅ Report   : {report_path}")
    if _save_obsidian(summary, "06 - Lecture Summaries", obs_filename):
        print(f"  ✅ Obsidian : 06 - Lecture Summaries/{obs_filename}")

    # Step 4 (optional): auto G7
    if auto_g7 and matched:
        print("\n[G7 auto] สร้าง scope notes สำหรับโรคที่ match...")
        try:
            _auto_g7(matched, kb)
        except Exception as e:
            print(f"  [G7 auto] ⚠️  {e}")

    print("\n" + "─" * 50)
    return summary


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="ExamFlow Lecture-Exam Bridge (G8)")
    ap.add_argument("--pdf",      required=True,   help="Path to lecture PDF")
    ap.add_argument("--title",    default="",      help="Override lecture title")
    ap.add_argument("--hint",     action="append", metavar="TEXT",
                    help="User hint (repeatable): --hint 'เน้น management' --hint 'หน้า 11 ออกแน่'")
    ap.add_argument("--no-cache", action="store_true", help="ข้าม cache — สกัดใหม่ทั้งหมด")
    ap.add_argument("--no-g7",   action="store_true",  help="ไม่สร้าง scope notes อัตโนมัติ")
    args = ap.parse_args()

    result = run_lecture_bridge(
        args.pdf,
        title_override=args.title,
        hints=args.hint,
        use_cache=not args.no_cache,
        auto_g7=not args.no_g7,
    )
    print("\n" + result)


if __name__ == "__main__":
    main()
