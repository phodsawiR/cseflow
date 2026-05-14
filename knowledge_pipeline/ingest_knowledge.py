"""
CaseFlow Knowledge Ingestion Pipeline
--------------------------------------
Supports: Textbook, Guideline, Slides, Exam_Prep
Steps:
  1. Download PDF (URL via Crawl4AI, or local --file)
  2. Convert PDF → Markdown (Marker)
  3. Detect document type + metadata (Gemini)
  4. Split by structure (TOC / header level)
  5. Save chunks to knowledge_base/[Specialty]/[Type]/
  6. Upload chunks to correct NotebookLM notebook
"""

import argparse
import os
import asyncio
import sys
import shutil
import yaml
import json
import re
import subprocess
from pathlib import Path

# Fix emoji output on Windows Thai console
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from typing import Optional
from datetime import datetime
from dataclasses import dataclass, field

from google import genai
from dotenv import load_dotenv

try:
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
except ImportError:
    print("❌ crawl4ai not found. pip install crawl4ai")
    sys.exit(1)

try:
    from notebooklm import NotebookLMClient
    NLM_AVAILABLE = True
except ImportError:
    NLM_AVAILABLE = False
    print("⚠️  notebooklm library not found — upload will be skipped.")

load_dotenv()

# ── Paths ────────────────────────────────────────────────────────────────────
KNOWLEDGE_BASE_DIR = Path("knowledge_pipeline/knowledge_base")
TEMP_DIR            = Path("temp_ingest")
INDEX_FILE          = KNOWLEDGE_BASE_DIR / "index.json"
NOTEBOOKS_FILE      = Path("notebooks.json")
GOOGLE_API_KEY      = os.getenv("GOOGLE_API_KEY")

KNOWLEDGE_BASE_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)

# ── Constants ─────────────────────────────────────────────────────────────────
VALID_TYPES = ["Textbook", "Guideline", "Slides", "Exam_Prep"]

# Header level to split at, per document type
SPLIT_LEVEL = {
    "Textbook":  1,   # H1 = Chapter
    "Guideline": 2,   # H2 = Major section
    "Slides":    2,   # H2 = Slide group
    "Exam_Prep": 2,   # H2 = Topic group
}

# Minimum characters for a chunk to be kept (filters TOC stubs)
MIN_CHUNK_CHARS = 1_500

# Maximum characters for any chunk (applies to all types)
# ~20,000 words — well under NotebookLM's limit but focused enough for retrieval
MAX_CHUNK_CHARS = 100_000

# Specialty → notebook key normalization
SPECIALTY_MAP = {
    "general_practice":  "symptom_approach",
    "general":           "symptom_approach",
    "internal_medicine": "symptom_approach",
    "unknown":           "symptom_approach",
    "endocrinology":     "endocrine",
    "renal":             "nephrology",
    "cardio":            "cardiology",
    "pulmo":             "pulmonology",
    "id":                "infectious_disease",
    "pharma":            "pharmacology",
}


# ── Data classes ─────────────────────────────────────────────────────────────
@dataclass
class Chunk:
    title:   str
    content: str
    index:   int


# ── Helpers ───────────────────────────────────────────────────────────────────
def load_json(path: Path) -> dict:
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_json(path: Path, data: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def normalize_specialty(specialty: str) -> str:
    s = specialty.lower().replace(" ", "_").replace("-", "_")
    return SPECIALTY_MAP.get(s, s)


def _slice_at_level(content: str, level: int) -> list[tuple[str, str]]:
    """Return [(title, body), ...] split at H{level}. Empty list if no headers found."""
    pattern = re.compile(r"^#{" + str(level) + r"} (.+)$", re.MULTILINE)
    matches = list(pattern.finditer(content))
    if not matches:
        return []
    result = []
    for i, m in enumerate(matches):
        end  = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        body = content[m.start():end].strip()
        result.append((m.group(1).strip(), body))
    return result


def _sub_split_recursive(
    parent_title: str, body: str, idx: int, sub_level: int
) -> list[Chunk]:
    """Recursively sub-split a chunk until under MAX_CHUNK_CHARS or H4."""
    if sub_level > 4:
        # If still too large and no headers found, force-split by char count
        if len(body) > MAX_CHUNK_CHARS:
            chunks = []
            current_idx = idx
            # Split into roughly MAX_CHUNK_CHARS chunks
            for i in range(0, len(body), MAX_CHUNK_CHARS):
                sub_body = body[i : i + MAX_CHUNK_CHARS]
                chunks.append(Chunk(
                    title=f"{parent_title} (Part {len(chunks)+1})",
                    content=f"# {parent_title}\n\n{sub_body}",
                    index=current_idx
                ))
                current_idx += 1
            return chunks
            
        return [Chunk(
            title=parent_title,
            content=f"# {parent_title}\n\n{body}",
            index=idx,
        )]

    slices = _slice_at_level(body, level=sub_level)
    if not slices:
        # No sub-headers at this level, try deeper
        return _sub_split_recursive(parent_title, body, idx, sub_level + 1)

    chunks: list[Chunk] = []
    current_idx = idx
    for sub_title, sub_body in slices:
        if len(sub_body) < 500: # smaller limit for sub-sections
            continue
        full_title   = f"{parent_title} — {sub_title}"
        full_content = f"# {parent_title}\n\n{sub_body}"
        if len(full_content) > MAX_CHUNK_CHARS:
            # Still too large → recurse one level deeper
            sub_res = _sub_split_recursive(full_title, sub_body, current_idx, sub_level + 1)
            chunks.extend(sub_res)
            current_idx = chunks[-1].index + 1
        else:
            chunks.append(Chunk(title=full_title, content=full_content, index=current_idx))
            current_idx += 1

    return chunks


def split_by_headers(content: str, level: int, doc_type: str = "") -> list[Chunk]:
    """
    Split markdown at H{level} boundaries. 
    Any chunk exceeding MAX_CHUNK_CHARS is recursively sub-split (H+1)
    to ensure manageable sizes for NotebookLM while preserving context.
    """
    slices = _slice_at_level(content, level)

    if not slices:
        if level < 3:
            return split_by_headers(content, level + 1, doc_type)
        return [Chunk(title="Full Document", content=content, index=0)]

    chunks: list[Chunk] = []
    idx = 0
    for chapter_title, chapter_body in slices:
        if len(chapter_body) < MIN_CHUNK_CHARS:
            continue

        # If any section is too large, force sub-split regardless of doc_type
        if len(chapter_body) > MAX_CHUNK_CHARS:
            sub_chunks = _sub_split_recursive(chapter_title, chapter_body, idx, sub_level=level + 1)
            if sub_chunks:
                chunks.extend(sub_chunks)
                idx = chunks[-1].index + 1
                continue

        chunks.append(Chunk(title=chapter_title, content=chapter_body, index=idx))
        idx += 1

    return chunks or [Chunk(title="Full Document", content=content, index=0)]


def _count_pdf_pages(pdf_path: Path) -> Optional[int]:
    """Return PDF page count, or None if unable to read."""
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(str(pdf_path))
        try:
            return len(doc)
        finally:
            doc.close()
    except Exception:
        return None


def _format_size(num_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024 or unit == "GB":
            return f"{num_bytes:.1f} {unit}" if unit != "B" else f"{num_bytes} B"
        num_bytes /= 1024
    return f"{num_bytes:.1f} GB"


# tqdm progress: "Recognizing layout:  23%|██▎       | 42/180 [..]"
_TQDM_PROGRESS_RE = re.compile(r"(\d+)%\|.*?\|\s*(\d+)/(\d+)")


def resolve_marker_cmd(pdf_path: str, out_dir: str) -> list[str]:
    import os
    norm_pdf = os.path.normpath(pdf_path)
    norm_out = os.path.normpath(out_dir)
    
    # We remove --batch_multiplier as it's not supported by marker_single.
    # We also add environment variables to suppress warnings.
    # Added --disable_multiprocessing, --disable_ocr, and --force_layout_block Text to save RAM.
    script = (
        "import sys; import os; import warnings; "
        "os.environ['PYTHONWARNINGS'] = 'ignore'; "
        "warnings.filterwarnings('ignore'); "
        "from marker.scripts.convert_single import convert_single_cli; "
        f"sys.argv = ['marker_single', r'{norm_pdf}', '--output_dir', r'{norm_out}', '--output_format', 'markdown', '--disable_multiprocessing', '--disable_ocr', '--force_layout_block', 'Text']; "
        "convert_single_cli()"
    )
    
    return [sys.executable, "-c", script]


# ── Main class ────────────────────────────────────────────────────────────────
class KnowledgeIngestor:

    def __init__(self, progress_callback=None, async_input_callback=None):
        if not GOOGLE_API_KEY:
            raise ValueError("❌ Missing GOOGLE_API_KEY in .env")
        self.genai    = genai.Client(api_key=GOOGLE_API_KEY)
        self.index    = load_json(INDEX_FILE)
        self.notebooks = load_json(NOTEBOOKS_FILE)
        self.progress_callback = progress_callback
        self.async_input_callback = async_input_callback

    def _report_progress(
        self,
        label: str,
        status: str = "running",
        detail: str = "",
        *,
        percent: Optional[int] = None,
        current: Optional[int] = None,
        total: Optional[int] = None,
    ):
        # Always print to terminal for debugging
        icon = {"running": "⏳", "done": "✅", "error": "❌"}.get(status, "·")
        bar = ""
        if percent is not None:
            bar = f" [{percent}%]"
        elif current is not None and total:
            bar = f" [{current}/{total}]"
        print(f"{icon} {label} {f'({detail})' if detail else ''}{bar}")

        # Also call the callback if provided (for UI updates)
        if self.progress_callback:
            try:
                self.progress_callback(
                    label, status, detail,
                    percent=percent, current=current, total=total,
                )
            except TypeError:
                # Backward-compat for callbacks that don't accept kwargs
                try:
                    self.progress_callback(label, status, detail)
                except Exception as e:
                    print(f"⚠️ Progress callback error: {e}")
            except Exception as e:
                print(f"⚠️ Progress callback error: {e}")

    # ── Gemini helper ─────────────────────────────────────────────────────────
    def _ask_gemini(self, prompt: str) -> str:
        resp = self.genai.models.generate_content(
            model="gemini-3-flash-preview",
            contents=prompt,
        )
        return resp.text.strip()

    def _parse_yaml_response(self, text: str) -> dict:
        if "```yaml" in text:
            text = text.split("```yaml")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]
        return yaml.safe_load(text.strip()) or {}

    # ── Step 1: Acquire PDF ───────────────────────────────────────────────────
    async def crawl_for_pdf(self, url: str) -> Optional[Path]:
        self._report_progress("กำลังเตรียมดาวน์โหลด PDF", "running", url)
        browser_cfg = BrowserConfig(headless=True)
        run_cfg     = CrawlerRunConfig(cache_mode=CacheMode.BYPASS)

        async with AsyncWebCrawler(config=browser_cfg) as crawler:
            result = await crawler.arun(url=url, config=run_cfg)
            if not result.success:
                self._report_progress("Crawl ล้มเหลว", "error", result.error_message)
                return None

            all_links = (
                result.links.get("external", []) +
                result.links.get("internal", [])
            )
            pdf_links = [
                lnk["href"] for lnk in all_links
                if lnk.get("href", "").lower().endswith(".pdf")
            ]

            if not pdf_links:
                if url.lower().endswith(".pdf"):
                    pdf_links = [url]
                else:
                    self._report_progress("ไม่พบไฟล์ PDF ในหน้าเว็บ", "error")
                    return None

            pdf_url = pdf_links[0]
            if not pdf_url.startswith("http"):
                from urllib.parse import urljoin
                pdf_url = urljoin(url, pdf_url)

            self._report_progress("กำลังดาวน์โหลด PDF", "running", pdf_url)
            import requests
            r = requests.get(pdf_url, stream=True, timeout=60)
            if r.status_code != 200:
                self._report_progress(f"HTTP Error {r.status_code}", "error")
                return None

            fname = Path(pdf_url).name
            if not fname.endswith(".pdf"):
                fname += ".pdf"
            dest = TEMP_DIR / fname
            total_bytes = int(r.headers.get("content-length") or 0)
            written = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)
                    written += len(chunk)
                    if total_bytes:
                        pct = min(100, int(written * 100 / total_bytes))
                        if pct % 5 == 0:
                            self._report_progress(
                                "กำลังดาวน์โหลด PDF", "running", pdf_url,
                                percent=pct, current=written, total=total_bytes,
                            )
            self._report_progress(
                "ดาวน์โหลด PDF สำเร็จ", "done", _format_size(written), percent=100,
            )
            return dest

    # ── Step 2: Convert PDF → Markdown ───────────────────────────────────────
    def convert_pdf_to_md(self, pdf_path: Path, fast: bool = False) -> Optional[Path]:
        out_dir = TEMP_DIR / "marker_output"
        out_dir.mkdir(exist_ok=True)

        # Sub-step: file metadata
        try:
            size_str = _format_size(pdf_path.stat().st_size)
        except Exception:
            size_str = ""
        self._report_progress("วิเคราะห์ไฟล์ PDF", "done", f"{pdf_path.name} · {size_str}".strip(" ·"))

        n_pages = _count_pdf_pages(pdf_path)
        if n_pages:
            self._report_progress("นับหน้า PDF", "done", f"{n_pages} หน้า")

        # ── Marker (AI Mode) with streaming progress ─────────────────────
        if not fast:
            marker_label = "แปลง PDF → Markdown (AI Mode)"
            self._report_progress(marker_label, "running", "กำลังโหลด Marker model...")
            cmd = resolve_marker_cmd(str(pdf_path), str(out_dir))

            try:
                env = os.environ.copy()
                env["PYTHONUNBUFFERED"] = "1"
                env["PYTHONWARNINGS"] = "ignore"

                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=0,
                    env=env,
                )

                # tqdm uses '\r' for in-place updates, so we can't rely on readline().
                # Read raw bytes in a background thread and split on both \r and \n.
                import threading
                from queue import Queue, Empty

                line_queue: Queue = Queue()

                def _reader():
                    buf = bytearray()
                    while True:
                        chunk = proc.stdout.read(256) if proc.stdout else b""
                        if not chunk:
                            if buf:
                                line_queue.put(buf.decode("utf-8", errors="replace"))
                            line_queue.put(None)
                            return
                        for byte in chunk:
                            if byte in (0x0A, 0x0D):  # \n or \r
                                if buf:
                                    line_queue.put(buf.decode("utf-8", errors="replace"))
                                    buf = bytearray()
                            else:
                                buf.append(byte)

                t = threading.Thread(target=_reader, daemon=True)
                t.start()

                last_percent = -10
                last_current = -1
                stage = ""
                while True:
                    try:
                        line = line_queue.get(timeout=1.0)
                    except Empty:
                        if proc.poll() is not None:
                            break
                        continue
                    if line is None:
                        break
                    m = _TQDM_PROGRESS_RE.search(line)
                    if m:
                        pct = int(m.group(1))
                        cur = int(m.group(2))
                        tot = int(m.group(3))
                        prefix = line[: m.start()].strip().rstrip(":").strip()
                        if prefix and len(prefix) < 60:
                            stage = prefix
                        # Throttle: emit every ≥3% jump or on final page of a stage
                        if (pct - last_percent >= 3) or (cur == tot and cur != last_current):
                            detail = f"{stage} · หน้า {cur}/{tot}" if stage else f"หน้า {cur}/{tot}"
                            self._report_progress(
                                marker_label, "running", detail,
                                percent=pct, current=cur, total=tot,
                            )
                            last_percent = pct
                            last_current = cur

                rc = proc.wait(timeout=600)
                t.join(timeout=2)

                if rc == 0:
                    stem = pdf_path.stem
                    candidates: list[Path] = []
                    sub = out_dir / stem
                    if sub.exists():
                        candidates += list(sub.glob("*.md"))
                    candidates += list(out_dir.rglob("*.md"))
                    for p in candidates:
                        if p.exists():
                            self._report_progress(
                                marker_label, "done",
                                f"{n_pages} หน้า" if n_pages else "เสร็จสมบูรณ์",
                                percent=100,
                            )
                            return p

                self._report_progress(marker_label, "error", f"exit code {rc}")
                self._report_progress("กำลังลองโหมดสำรอง (pdftext)", "running")
            except subprocess.TimeoutExpired:
                self._report_progress(marker_label, "error", "timeout (>10 นาที)")
                self._report_progress("กำลังลองโหมดสำรอง (pdftext)", "running")
            except Exception as e:
                self._report_progress(marker_label, "error", str(e)[:80])
                self._report_progress("กำลังลองโหมดสำรอง (pdftext)", "running")
        else:
            self._report_progress("โหมดด่วน (Fast Mode): ใช้ pdftext", "running")

        # ── Fallback / Fast: pdftext ─────────────────────────────────────
        try:
            from pdftext.extraction import plain_text_output

            md_content = plain_text_output(str(pdf_path), sort=True)
            if md_content:
                fpath = out_dir / f"{pdf_path.stem}.md"
                if not md_content.startswith("#"):
                    md_content = f"# {pdf_path.stem}\n\n{md_content}"

                with open(fpath, "w", encoding="utf-8") as f:
                    f.write(md_content)

                self._report_progress(
                    "แปลง PDF → Markdown (Lightweight)", "done",
                    f"{n_pages} หน้า" if n_pages else "เสร็จสมบูรณ์",
                    percent=100,
                )
                return fpath

            self._report_progress("การแปลงไฟล์ล้มเหลวทุกช่องทาง", "error")
            return None

        except Exception as e:
            self._report_progress("เกิดข้อผิดพลาดในการแปลงไฟล์ (โหมดสำรอง)", "error", str(e))
            return None

    # ── Step 3: Detect type + metadata ───────────────────────────────────────
    def detect_metadata(self, md_path: Path) -> dict:
        self._report_progress("กำลังวิเคราะห์เนื้อหาด้วย AI", "running")
        with open(md_path, "r", encoding="utf-8") as f:
            content = f.read(12000)

        prompt = f"""
Analyze this medical document and respond ONLY with a YAML block.

Extract these fields:
- Specialty: (e.g. Cardiology, Pulmonology, Nephrology, Endocrine,
              Infectious_Disease, Pharmacology, General_Practice,
              Surgery, Pediatrics, Neurology)
- Type: MUST be one of: Textbook, Guideline, Slides, Exam_Prep
  - Textbook   = comprehensive reference book with chapters/units
  - Guideline  = clinical practice guideline / recommendation document
  - Slides     = lecture slides or presentation (sparse text per page)
  - Exam_Prep  = past exam questions, MCQ banks, practice tests
- Year: (publication year, e.g. 2024)
- Title: (short document title)

Respond ONLY with YAML. No extra text.

CONTENT:
{content}
"""
        try:
            meta = self._parse_yaml_response(self._ask_gemini(prompt))
            if meta.get("Type") not in VALID_TYPES:
                meta["Type"] = "Textbook"
            self._report_progress("วิเคราะห์เนื้อหาสำเร็จ", "done", f"{meta.get('Specialty')} | {meta.get('Type')}")
            return meta
        except Exception as e:
            self._report_progress("AI วิเคราะห์ล้มเหลว (ใช้ค่าเริ่มต้น)", "done", str(e))
            return {
                "Specialty": "Unknown",
                "Type": "Textbook",
                "Year": "Unknown",
                "Title": md_path.stem,
            }

    # ── Step 4: Split document ───────────────────────────────────────────────
    def split_document(self, md_path: Path, doc_type: str) -> list[Chunk]:
        self._report_progress("กำลังแบ่งเนื้อหาเป็นส่วนๆ (Chunking)", "running")
        with open(md_path, "r", encoding="utf-8") as f:
            content = f.read()

        level  = SPLIT_LEVEL.get(doc_type, 1)
        chunks = split_by_headers(content, level, doc_type=doc_type)
        self._report_progress("แบ่งเนื้อหาสำเร็จ", "done", f"{len(chunks)} chunks")
        return chunks

    # ── Step 5: Save chunks ──────────────────────────────────────────────────
    def save_chunks(
        self,
        chunks:      list[Chunk],
        metadata:    dict,
        source_name: str,
        source_ref:  str = "",
    ) -> list[Path]:
        self._report_progress("กำลังบันทึกข้อมูลลงฐานข้อมูล", "running")
        specialty = str(metadata.get("Specialty", "General")).replace(" ", "_")
        doc_type  = str(metadata.get("Type", "Unknown"))
        target_dir = KNOWLEDGE_BASE_DIR / specialty / doc_type
        target_dir.mkdir(parents=True, exist_ok=True)

        saved: list[Path] = []
        for chunk in chunks:
            safe_title = re.sub(r"[^\w\s-]", "", chunk.title).strip().replace(" ", "_")[:50]
            fname = f"{source_name}__{chunk.index:03d}__{safe_title}.md"
            fpath = target_dir / fname

            chunk_meta = {
                **metadata,
                "chunk_title":     chunk.title,
                "chunk_index":     chunk.index,
                "source_document": source_name,
                "source_ref":      source_ref,
                "ingested_at":     datetime.now().strftime("%Y-%m-%d"),
            }
            frontmatter = yaml.dump(chunk_meta, sort_keys=False, allow_unicode=True)

            with open(fpath, "w", encoding="utf-8") as f:
                f.write(f"---\n{frontmatter}---\n\n{chunk.content}")

            saved.append(fpath)

        self._report_progress("บันทึกข้อมูลสำเร็จ", "done", f"{len(saved)} files")
        return saved

    # ── Step 6: Upload to NotebookLM ─────────────────────────────────────────
    async def _ensure_notebook(self, client, specialty: str, auto_create: bool) -> Optional[str]:
        """Resolve a notebook_id for `specialty`. Prompt-then-create when missing."""
        notebook_id = self.notebooks.get(specialty)
        if notebook_id and notebook_id != "NOTEBOOK_ID_HERE":
            return notebook_id

        title = specialty.replace("_", " ").title()
        self._report_progress(f"ไม่พบ Notebook สำหรับ '{specialty}'", "running")

        if not auto_create:
            if self.async_input_callback:
                ans = await self.async_input_callback(f"สร้าง notebook ใหม่ชื่อ '{title}'? [y/N]: ")
            else:
                try:
                    ans = await asyncio.to_thread(
                        input, f"   สร้าง notebook ใหม่ชื่อ '{title}'? [y/N]: "
                    )
                except EOFError:
                    ans = ""
            if ans.strip().lower() not in {"y", "yes", "ใช่"}:
                self._report_progress(f"ข้ามการ Upload (ผู้ใช้ปฏิเสธสร้าง notebook)", "done")
                return None

        try:
            nb = await client.notebooks.create(title=title)
            new_id = nb.id
        except Exception as e:
            self._report_progress("สร้าง Notebook ล้มเหลว", "error", str(e))
            return None

        self.notebooks[specialty] = new_id
        save_json(NOTEBOOKS_FILE, self.notebooks)
        self._report_progress(f"สร้าง notebook '{title}' สำเร็จ", "done", f"id={new_id}")
        return new_id

    async def upload_to_notebooklm(self, files: list[Path], metadata: dict, auto_create: bool = False):
        if not NLM_AVAILABLE:
            self._report_progress("ข้ามการ Upload (ไม่ได้ติดตั้ง library)", "done")
            return

        specialty = normalize_specialty(str(metadata.get("Specialty", "unknown")))

        try:
            client_cm = await NotebookLMClient.from_storage()
        except Exception as e:
            self._report_progress("NotebookLM Upload ล้มเหลว (Auth Error)", "error", str(e))
            return

        async with client_cm as client:
            notebook_id = await self._ensure_notebook(client, specialty, auto_create)
            if not notebook_id:
                return

            doc_title = str(metadata.get("Title", metadata.get("source_document", "Document")))
            upload_label = f"อัพโหลดไปยัง NotebookLM [{specialty}]"
            total = len(files)
            self._report_progress(
                upload_label, "running", f"0/{total}",
                percent=0, current=0, total=total,
            )

            ok = fail = 0
            for fpath in files:
                try:
                    content = fpath.read_text(encoding="utf-8")
                    if content.startswith("---"):
                        end = content.find("---", 3)
                        content = content[end + 3:].strip() if end != -1 else content

                    chunk_label = fpath.stem.replace("__", " — ").replace("_", " ")
                    source_title = f"{doc_title} — {chunk_label}"[:200]

                    await client.sources.add_text(
                        notebook_id,
                        title=source_title,
                        content=content,
                        wait=True,
                        wait_timeout=300.0,
                    )
                    ok += 1
                    pct = int(ok * 100 / total)
                    self._report_progress(
                        upload_label, "running", f"{ok}/{total}",
                        percent=pct, current=ok, total=total,
                    )
                    await asyncio.sleep(2)
                except Exception:
                    fail += 1

            self._report_progress(
                upload_label, "done", f"{ok} success, {fail} fail",
                percent=100, current=ok, total=total,
            )

    # ── Index ─────────────────────────────────────────────────────────────────
    def update_index(self, source_name: str, files: list[Path], metadata: dict):
        self.index[source_name] = {
            "files":       [str(p) for p in files],
            "specialty":   metadata.get("Specialty"),
            "type":        metadata.get("Type"),
            "metadata":    metadata,
            "ingested_at": datetime.now().strftime("%Y-%m-%d"),
        }
        save_json(INDEX_FILE, self.index)


# ── Single-source ingest (reusable) ───────────────────────────────────────────
async def ingest_one(
    spec: dict,
    ingestor: "KnowledgeIngestor",
    *,
    no_upload: bool = False,
    auto_create: bool = False,
    fast: bool = False,
) -> dict:
    """
    Ingest a single source spec. Returns {'ok': bool, 'source': str, 'chunks': int, 'error': str}.
    Spec keys: file | url, type?, force?
    """
    source_ref = ""
    pdf_path: Optional[Path] = None

    try:
        if "url" in spec:
            source_ref = spec["url"]
            pdf_path = await ingestor.crawl_for_pdf(spec["url"])
        elif "file" in spec:
            source_ref = spec["file"]
            pdf_path = Path(spec["file"])
            if not pdf_path.exists():
                return {"ok": False, "source": source_ref, "error": "file not found"}
        elif "pair" in spec:
            # Reserved for future exam-prep branch
            return {"ok": False, "source": "pair", "error": "pair mode not yet implemented"}
        else:
            return {"ok": False, "source": "?", "error": "spec missing url/file/pair"}

        if not pdf_path:
            return {"ok": False, "source": source_ref, "error": "PDF not obtained"}

        source_name = pdf_path.stem
        force = bool(spec.get("force", False))
        if not force and source_name in ingestor.index:
            return {"ok": True, "source": source_name, "chunks": 0, "skipped": "already indexed"}

        md_path = await asyncio.to_thread(ingestor.convert_pdf_to_md, pdf_path, fast)
        if not md_path:
            return {"ok": False, "source": source_name, "error": "conversion failed"}

        metadata = await asyncio.to_thread(ingestor.detect_metadata, md_path)
        if spec.get("type"):
            metadata["Type"] = spec["type"]

        chunks = await asyncio.to_thread(
            ingestor.split_document, md_path, str(metadata.get("Type"))
        )
        saved_files = await asyncio.to_thread(
            ingestor.save_chunks, chunks, metadata, source_name, source_ref
        )

        if not no_upload:
            await ingestor.upload_to_notebooklm(
                saved_files, metadata, auto_create=auto_create
            )

        ingestor.update_index(source_name, saved_files, metadata)
        return {
            "ok": True,
            "source": source_name,
            "chunks": len(saved_files),
            "specialty": metadata.get("Specialty"),
            "type": metadata.get("Type"),
        }

    except Exception as e:
        return {"ok": False, "source": source_ref or "?", "error": str(e)}


async def ingest_batch(
    specs: list[dict],
    *,
    no_upload: bool = False,
    auto_create: bool = False,
    fast: bool = False,
    progress_callback=None,
    async_input_callback=None,
    archive_inbox: bool = False,
) -> dict:
    """Run ingest_one over a list of specs. Continues on individual failure."""
    from knowledge_pipeline.manifest import archive_inbox_file

    ingestor = KnowledgeIngestor(progress_callback=progress_callback, async_input_callback=async_input_callback)
    results: list[dict] = []
    total = len(specs)

    for i, spec in enumerate(specs, 1):
        label = spec.get("url") or spec.get("file") or "pair"
        print(f"\n[{i}/{total}] {label}")
        if progress_callback:
            progress_callback(f"Batch {i}/{total}", "running", label[:80])

        result = await ingest_one(
            spec, ingestor,
            no_upload=no_upload, auto_create=auto_create, fast=fast,
        )
        results.append(result)

        if result["ok"]:
            chunks = result.get("chunks", 0)
            msg = f"✅ {result['source']} ({chunks} chunks)"
            if result.get("skipped"):
                msg = f"⏭️  {result['source']} (skipped: {result['skipped']})"
            print(msg)
            if archive_inbox and "file" in spec and not result.get("skipped"):
                archive_inbox_file(spec["file"])
        else:
            print(f"❌ {result['source']}: {result.get('error')}")

    shutil.rmtree(TEMP_DIR, ignore_errors=True)

    ok = sum(1 for r in results if r["ok"] and not r.get("skipped"))
    skipped = sum(1 for r in results if r.get("skipped"))
    failed = sum(1 for r in results if not r["ok"])

    summary = {"total": total, "ok": ok, "skipped": skipped, "failed": failed, "results": results}
    print(f"\n{'='*60}\n📊 Batch done: {ok} ok / {skipped} skipped / {failed} failed (of {total})")
    return summary


# ── CLI entry point ───────────────────────────────────────────────────────────
async def main():
    from knowledge_pipeline.manifest import load_manifest, scan_inbox, INBOX_DIR

    parser = argparse.ArgumentParser(
        description="CaseFlow Knowledge Ingestion Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single source
  python -m knowledge_pipeline.ingest_knowledge --url https://example.com/guideline.pdf
  python -m knowledge_pipeline.ingest_knowledge --file ./textbook.pdf --type Textbook

  # Batch via manifest
  python -m knowledge_pipeline.ingest_knowledge --manifest knowledge_pipeline/sources.yaml --auto-create

  # Batch scan of inbox/ folder
  python -m knowledge_pipeline.ingest_knowledge --inbox --auto-create
        """,
    )
    parser.add_argument("--url",         type=str, help="URL to crawl for PDF")
    parser.add_argument("--file",        type=str, help="Path to local PDF file")
    parser.add_argument("--manifest",    type=str, help="Path to YAML/JSON manifest of sources")
    parser.add_argument("--inbox",       nargs="?", const=str(INBOX_DIR), default=None,
                        help="Scan folder for PDFs (default: ./inbox/)")
    parser.add_argument("--archive",     action="store_true",
                        help="Move processed PDFs from inbox/ to inbox/processed/ on success")
    parser.add_argument("--type",        type=str, choices=VALID_TYPES,
                        help="Override auto-detected document type (single-source mode only)")
    parser.add_argument("--force",       action="store_true",
                        help="Re-ingest even if file already exists in index")
    parser.add_argument("--no-upload",   action="store_true",
                        help="Save files locally only, skip NotebookLM upload")
    parser.add_argument("--fast",        action="store_true",
                        help="Skip high-accuracy AI conversion, use lightweight pdftext mode")
    parser.add_argument("--auto-create", action="store_true",
                        help="สร้าง notebook ใหม่อัตโนมัติเมื่อไม่เจอ specialty ใน notebooks.json")
    args = parser.parse_args()

    # ── Build spec list ──────────────────────────────────────────────────────
    specs: list[dict] = []
    if args.manifest:
        specs = load_manifest(args.manifest)
        print(f"📄 Loaded {len(specs)} sources from manifest")
    elif args.inbox is not None:
        specs = scan_inbox(args.inbox)
        print(f"📂 Scanned {len(specs)} PDF(s) in {args.inbox}")
    elif args.url:
        specs = [{"url": args.url, "type": args.type, "force": args.force}]
    elif args.file:
        specs = [{"file": args.file, "type": args.type, "force": args.force}]
    else:
        print("⚠️  Provide --url, --file, --manifest, or --inbox. Use -h for help.")
        return

    if not specs:
        print("⚠️  No sources to ingest.")
        return

    try:
        await ingest_batch(
            specs,
            no_upload=args.no_upload,
            auto_create=args.auto_create,
            fast=args.fast,
            archive_inbox=args.archive,
        )
    except KeyboardInterrupt:
        print("\n⚠️  Interrupted.")


if __name__ == "__main__":
    asyncio.run(main())
