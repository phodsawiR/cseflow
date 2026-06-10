"""
ExamFlow — Lecture Slide Ingestion
รับ PNG/JPG screenshots หรือ PDF slides → Gemini Vision → exam_kb.json (notes)

Usage:
  python ingest_lecture.py --folder inbox/lectures/HIV/
  python ingest_lecture.py --pdf    inbox/lectures/cardio_lecture.pdf
  python ingest_lecture.py --folder inbox/lectures/ --topic "Cardiology"

วิธีเตรียม OneNote:
  1. เปิด OneNote → ไปที่หน้า lecture
  2. วิธีที่ 1: File → Export → Export Current Page → PNG/PDF
  3. วิธีที่ 2: ใช้ Snipping Tool แคปทีละสไลด์ บันทึกเป็น PNG
  4. วางไฟล์ทั้งหมดในโฟลเดอร์เดียว แล้วรัน --folder
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

# ── ensure project root on path ───────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from google import genai
from google.genai import types as _gt

EXAM_KB_PATH   = os.getenv("EXAM_KB_PATH",  "./examflow/exam_kb.json")
PROMPT_PATH    = _ROOT / "prompts" / "examflow" / "lecture_extraction.md"
_GEMINI_MODEL  = "gemini-3.5-flash"

_client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))

_IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
_MIME = {
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp":  "image/bmp",
}


# ── helpers ───────────────────────────────────────────────────────────────────

def _load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def _load_or_init_kb() -> dict:
    if Path(EXAM_KB_PATH).exists():
        kb = json.loads(Path(EXAM_KB_PATH).read_text(encoding="utf-8"))
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
            "schema_version": "1.1",
        },
        "questions": [],
        "notes": [],
        "disease_index": {},
        "system_index": {},
        "pattern_analysis": {
            "most_frequent_diseases": [],
            "topic_type_distribution": {},
            "cross_year_trends": [],
            "predicted_high_yield_next": [],
        },
    }


def _save_kb(kb: dict):
    os.makedirs(os.path.dirname(os.path.abspath(EXAM_KB_PATH)), exist_ok=True)
    Path(EXAM_KB_PATH).write_text(
        json.dumps(kb, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ── Gemini Vision extraction ──────────────────────────────────────────────────

def _extract_slide(img_bytes: bytes, mime: str, prompt: str) -> dict | None:
    try:
        response = _client.models.generate_content(
            model=_GEMINI_MODEL,
            contents=[
                _gt.Part.from_bytes(data=img_bytes, mime_type=mime),
                _gt.Part.from_text(prompt),
            ],
            config=_gt.GenerateContentConfig(
                response_mime_type="application/json"
            ),
        )
        raw = response.text.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```json?\n?|```$", "", raw, flags=re.MULTILINE).strip()
        if raw.lower() in ("null", "none", ""):
            return None
        return json.loads(raw)
    except Exception as e:
        print(f"  [WARN] extraction failed: {e}")
        return None


# ── PDF → images ──────────────────────────────────────────────────────────────

def _pdf_to_images(pdf_path: Path) -> list[tuple[bytes, str]]:
    """Render each PDF page as PNG bytes via PyMuPDF."""
    try:
        import fitz
    except ImportError:
        print("[ERROR] pip install pymupdf")
        sys.exit(1)
    doc = fitz.open(str(pdf_path))
    pages = []
    mat = fitz.Matrix(2, 2)  # 2× zoom for better OCR
    for page in doc:
        pix = page.get_pixmap(matrix=mat)
        pages.append((pix.tobytes("png"), "image/png"))
    doc.close()
    return pages


# ── dedup fingerprint ─────────────────────────────────────────────────────────

def _fp(text: str) -> str:
    import hashlib
    norm = re.sub(r"[\s\W]+", "", text.lower(), flags=re.UNICODE)[:200]
    return hashlib.sha256(norm.encode()).hexdigest()


# ── rebuild indexes ───────────────────────────────────────────────────────────

def _rebuild_indexes(kb: dict):
    """Lightweight index rebuild — only disease_index must_know_facts from notes."""
    for n in kb.get("notes", []):
        for disease in n.get("diseases", []):
            if disease not in kb["disease_index"]:
                kb["disease_index"][disease] = {
                    "question_ids": [], "note_ids": [], "frequency": 0,
                    "years_appeared": [], "key_investigations": [],
                    "key_management": [], "must_know_facts": [],
                    "common_distractors": [], "trend": "stable",
                }
            entry = kb["disease_index"][disease]
            if n["id"] not in entry["note_ids"]:
                entry["note_ids"].append(n["id"])
            for pt in n.get("key_points", []):
                if pt and pt not in entry["must_know_facts"]:
                    entry["must_know_facts"].append(pt)
            for inv in n.get("investigations", []):
                if inv and inv not in entry["key_investigations"]:
                    entry["key_investigations"].append(inv)
            for mgmt in n.get("management", []):
                if mgmt and mgmt not in entry["key_management"]:
                    entry["key_management"].append(mgmt)


# ── main ingest ───────────────────────────────────────────────────────────────

def ingest_images(
    image_list: list[tuple[bytes, str, str]],  # (bytes, mime, label)
    source_name: str,
    topic_hint: str = "",
) -> int:
    """Ingest a list of slide images. Returns count of notes added."""
    kb     = _load_or_init_kb()
    prompt = _load_prompt()

    existing_fps = {
        _fp(n.get("content", ""))
        for n in kb.get("notes", [])
        if n.get("content")
    }
    session_fps: set[str] = set()

    added = skipped = 0
    existing_n_ids = {n["id"] for n in kb.get("notes", [])}

    for idx, (img_bytes, mime, label) in enumerate(image_list, 1):
        print(f"  [{idx}/{len(image_list)}] {label} ...", end=" ", flush=True)
        extracted = _extract_slide(img_bytes, mime, prompt)

        if not extracted:
            print("SKIP (no content)")
            skipped += 1
            continue

        # fill topic hint if model missed it
        if not extracted.get("topic") and topic_hint:
            extracted["topic"] = topic_hint

        content = extracted.get("content", "")
        fp = _fp(content) if content else None
        if fp and (fp in existing_fps or fp in session_fps):
            print("SKIP (duplicate)")
            skipped += 1
            continue

        # assign note ID
        year  = datetime.now().year
        n_num = len(kb.get("notes", [])) + added + 1
        n_id  = f"N{year}_L{n_num:03d}"
        while n_id in existing_n_ids:
            n_num += 1
            n_id = f"N{year}_L{n_num:03d}"

        note = {
            "id":          n_id,
            "source_file": source_name,
            "year":        year,
            "note_number": n_num,
            "note_type":   "lecture_slide",
            "slide_label": label,
            "topic":       extracted.get("topic", topic_hint),
            "slide_title": extracted.get("slide_title", ""),
            "content":     content,
            "diseases":    extracted.get("diseases", []),
            "systems":     extracted.get("systems", []),
            "tags":        extracted.get("tags", []),
            "key_points":  extracted.get("key_points", []),
            "investigations": extracted.get("investigations", []),
            "management":     extracted.get("management", []),
            "pathophysiology": extracted.get("pathophysiology", ""),
        }

        kb.setdefault("notes", []).append(note)
        existing_n_ids.add(n_id)
        if fp:
            session_fps.add(fp)
        added += 1
        topic_str = f"[{note['topic'][:30]}]" if note.get("topic") else ""
        print(f"OK -> {n_id} {topic_str}")

    if added > 0:
        if source_name not in kb["metadata"]["source_files"]:
            kb["metadata"]["source_files"].append(source_name)
        kb["metadata"]["total_notes"] = len(kb.get("notes", []))
        kb["metadata"]["generated_at"] = datetime.now().isoformat()
        _rebuild_indexes(kb)
        _save_kb(kb)

    print(
        f"\n[Lecture] Done: +{added} slides | skipped {skipped}"
        f" | Total notes: {kb['metadata']['total_notes']}"
    )
    return added


def ingest_folder(folder: Path, topic_hint: str = "") -> int:
    images = sorted(
        f for f in folder.iterdir()
        if f.is_file() and f.suffix.lower() in _IMG_EXTS
    )
    if not images:
        print(f"[ERROR] ไม่พบไฟล์ภาพใน {folder}")
        print(f"  รองรับ: {', '.join(_IMG_EXTS)}")
        return 0

    print(f"\n[Lecture] Folder: {folder} — {len(images)} images")
    source_name = folder.name
    image_list = []
    for f in images:
        mime = _MIME.get(f.suffix.lower(), "image/png")
        image_list.append((f.read_bytes(), mime, f.name))

    return ingest_images(image_list, source_name, topic_hint)


def ingest_pdf(pdf_path: Path, topic_hint: str = "") -> int:
    print(f"\n[Lecture] PDF: {pdf_path.name}")
    pages = _pdf_to_images(pdf_path)
    if not pages:
        print("[ERROR] ไม่สามารถแปลง PDF เป็นรูปได้")
        return 0
    print(f"  {len(pages)} pages")
    image_list = [
        (b, m, f"page {i+1}") for i, (b, m) in enumerate(pages)
    ]
    return ingest_images(image_list, pdf_path.name, topic_hint)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Ingest lecture slides (images or PDF) into exam KB"
    )
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--folder", help="Folder containing PNG/JPG slide images")
    grp.add_argument("--pdf",    help="PDF file of slides")
    parser.add_argument("--topic", default="", help="Topic hint (e.g. 'Cardiology')")
    args = parser.parse_args()

    if args.folder:
        ingest_folder(Path(args.folder), args.topic)
    else:
        ingest_pdf(Path(args.pdf), args.topic)
