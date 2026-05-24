"""
ingest_pdf.py — Fast PDF ingestion directly into ChromaDB (no markdown conversion).
Extracts text per-page with pypdfium2, groups into overlapping chunks, indexes.

Usage:
  python ingest_pdf.py path/to/file.pdf
  python ingest_pdf.py path/to/file.pdf --specialty Cardiology --type Guideline
  python ingest_pdf.py *.pdf --pages-per-chunk 8
"""

import argparse
import hashlib
import os
import re
import sys
import yaml
from datetime import datetime
from pathlib import Path
from typing import Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

try:
    import pypdfium2 as pdfium
except ImportError:
    print("pip install pypdfium2")
    sys.exit(1)

import knowledge_rag as rag

KNOWLEDGE_BASE_DIR = Path("knowledge_pipeline/knowledge_base")
VALID_TYPES        = ["Textbook", "Guideline", "Slides", "Exam_Prep", "Other"]
PAGES_PER_CHUNK    = 8      # default pages per chunk
MIN_CHUNK_CHARS    = 300    # skip near-empty chunks (scanned/blank pages)
OVERLAP_PAGES      = 1      # pages of overlap between chunks


# ── PDF text extraction ───────────────────────────────────────────────────────

def extract_pages(pdf_path: Path) -> list[tuple[int, str]]:
    """Return [(page_number, text), ...] for all pages with extractable text."""
    doc    = pdfium.PdfDocument(str(pdf_path))
    pages  = []
    total  = len(doc)

    for i in range(total):
        page    = doc[i]
        textobj = page.get_textpage()
        text    = textobj.get_text_bounded().strip()
        textobj.close()
        page.close()
        if text:
            pages.append((i + 1, text))  # 1-indexed page numbers

        if (i + 1) % 50 == 0 or i + 1 == total:
            print(f"  extracting... {i+1}/{total} pages", end="\r")

    doc.close()
    print(f"  extracted {len(pages)}/{total} pages with text" + " " * 20)
    return pages


# ── Chunking ──────────────────────────────────────────────────────────────────

def make_chunks(
    pages: list[tuple[int, str]],
    pages_per_chunk: int = PAGES_PER_CHUNK,
    overlap: int = OVERLAP_PAGES,
) -> list[dict]:
    """
    Sliding-window chunk over pages.
    Returns [{"start_page": int, "end_page": int, "text": str}, ...]
    """
    chunks = []
    step   = max(1, pages_per_chunk - overlap)
    n      = len(pages)

    for i in range(0, n, step):
        window  = pages[i : i + pages_per_chunk]
        text    = "\n\n".join(t for _, t in window)
        if len(text) < MIN_CHUNK_CHARS:
            continue
        chunks.append({
            "start_page": window[0][0],
            "end_page":   window[-1][0],
            "text":       text,
        })

    return chunks


# ── Metadata detection ────────────────────────────────────────────────────────

def detect_metadata(pdf_path: Path, sample_text: str) -> dict:
    """Ask Gemini to classify specialty + type from first ~3000 chars."""
    from google import genai

    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return {"Specialty": "General_Practice", "Type": "Textbook", "Title": pdf_path.stem}

    client = genai.Client(api_key=api_key)
    prompt = f"""Analyze this medical document excerpt. Reply ONLY with YAML.

Fields:
- Specialty: (Cardiology / Pulmonology / Nephrology / Endocrine / Infectious_Disease /
              Pharmacology / General_Practice / Surgery / Pediatrics / Neurology)
- Type: MUST be one of: Textbook, Guideline, Slides, Exam_Prep, Other
- Title: short document title

CONTENT:
{sample_text[:3000]}
"""
    try:
        resp = client.models.generate_content(model="gemini-3.5-flash", contents=prompt)
        text = resp.text.strip()
        if "```yaml" in text:
            text = text.split("```yaml")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]
        meta = yaml.safe_load(text) or {}
        if not isinstance(meta, dict):
            meta = {}
        if meta.get("Type") not in VALID_TYPES:
            meta["Type"] = "Other"
        return meta
    except Exception as e:
        print(f"  [warn] metadata detection failed: {e}")
        return {"Specialty": "General_Practice", "Type": "Other", "Title": pdf_path.stem}


# ── Save chunks to disk ───────────────────────────────────────────────────────

def save_chunks(
    chunks:      list[dict],
    metadata:    dict,
    source_name: str,
) -> list[Path]:
    specialty  = str(metadata.get("Specialty", "General_Practice")).replace(" ", "_")
    doc_type   = str(metadata.get("Type", "Other"))
    target_dir = KNOWLEDGE_BASE_DIR / specialty / doc_type
    target_dir.mkdir(parents=True, exist_ok=True)

    saved = []
    for i, chunk in enumerate(chunks):
        fname = f"{source_name}__{i:03d}__p{chunk['start_page']}-{chunk['end_page']}.md"
        fpath = target_dir / fname

        chunk_meta = {
            **metadata,
            "chunk_index":     i,
            "start_page":      chunk["start_page"],
            "end_page":        chunk["end_page"],
            "source_document": source_name,
            "ingested_at":     datetime.now().strftime("%Y-%m-%d"),
        }
        fm = yaml.dump(chunk_meta, sort_keys=False, allow_unicode=True)
        fpath.write_text(f"---\n{fm}---\n\n{chunk['text']}", encoding="utf-8")
        saved.append(fpath)

    return saved


# ── Main ingest function ──────────────────────────────────────────────────────

def ingest_pdf(
    pdf_path:       Path,
    specialty:      Optional[str] = None,
    doc_type:       Optional[str] = None,
    pages_per_chunk: int = PAGES_PER_CHUNK,
    force:          bool = False,
    no_save:        bool = False,
) -> dict:
    """
    Ingest a single PDF into ChromaDB.
    Returns {"ok": bool, "chunks": int, "specialty": str, "error": str}
    """
    source_name = pdf_path.stem

    # Check if already indexed (simple guard)
    index_file = KNOWLEDGE_BASE_DIR / "index.json"
    if not force and index_file.exists():
        import json
        idx = json.load(open(index_file, encoding="utf-8"))
        if source_name in idx and idx[source_name].get("rag_indexed"):
            print(f"  already indexed — use --force to re-ingest")
            return {"ok": True, "chunks": 0, "skipped": True}

    # Step 1: extract text
    print(f"[1/4] extracting text from {pdf_path.name}...")
    pages = extract_pages(pdf_path)
    if not pages:
        return {"ok": False, "error": "no extractable text (image-based PDF needs OCR)"}

    total_chars = sum(len(t) for _, t in pages)
    print(f"  {len(pages)} pages | {total_chars:,} chars")

    # Step 2: detect metadata
    print("[2/4] detecting metadata...")
    sample = "\n".join(t for _, t in pages[:5])
    metadata = detect_metadata(pdf_path, sample)
    if specialty:
        metadata["Specialty"] = specialty
    if doc_type:
        metadata["Type"] = doc_type
    if "Title" not in metadata:
        metadata["Title"] = source_name
    print(f"  {metadata.get('Specialty')} | {metadata.get('Type')} | {metadata.get('Title', '')[:60]}")

    # Step 3: chunk
    print(f"[3/4] chunking ({pages_per_chunk} pages/chunk)...")
    chunks = make_chunks(pages, pages_per_chunk=pages_per_chunk)
    print(f"  {len(chunks)} chunks")

    # Step 4: save + index
    if not no_save:
        print("[4/4] saving chunks to disk...")
        saved_files = save_chunks(chunks, metadata, source_name)
        print(f"  saved {len(saved_files)} files")

        print("[4/4] indexing into ChromaDB...")
        count = rag.index_files(saved_files, metadata)
    else:
        # Index directly from text without saving to disk
        print("[4/4] indexing directly into ChromaDB (no-save mode)...")
        from pathlib import Path as _P
        import tempfile, os
        specialty_str = str(metadata.get("Specialty", "general")).replace(" ", "_")
        col_name = re.sub(r"[^a-z0-9_]", "", specialty_str.lower()) or "general"
        db   = rag._chroma()
        col  = db.get_or_create_collection(col_name, metadata={"hnsw:space": "cosine"})
        texts, ids, metas = [], [], []
        for i, chunk in enumerate(chunks):
            texts.append(chunk["text"][:8000])
            ids.append(hashlib.md5(f"{source_name}_{i}".encode()).hexdigest()[:32])
            metas.append({
                "specialty":  specialty_str,
                "doc_type":   str(metadata.get("Type", "")),
                "title":      f"p{chunk['start_page']}-{chunk['end_page']}",
                "source":     source_name,
                "file_path":  "",
            })
        embs = rag._embed_batch(texts)
        col.upsert(documents=texts, embeddings=embs, ids=ids, metadatas=metas)
        count = len(texts)
        saved_files = []

    # Update index.json
    import json
    idx = {}
    if index_file.exists():
        idx = json.load(open(index_file, encoding="utf-8"))
    idx[source_name] = {
        "files":       [str(p) for p in saved_files],
        "specialty":   metadata.get("Specialty"),
        "type":        metadata.get("Type"),
        "metadata":    metadata,
        "rag_indexed": True,
        "ingested_at": datetime.now().strftime("%Y-%m-%d"),
    }
    index_file.parent.mkdir(parents=True, exist_ok=True)
    json.dump(idx, open(index_file, "w", encoding="utf-8"), indent=2, ensure_ascii=False)

    return {
        "ok":       True,
        "chunks":   count,
        "specialty": metadata.get("Specialty"),
        "type":     metadata.get("Type"),
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Fast PDF ingest → ChromaDB (no markdown conversion)"
    )
    parser.add_argument("files", nargs="+", help="PDF file path(s)")
    parser.add_argument("--specialty", help="Override specialty (e.g. Cardiology)")
    parser.add_argument("--type",      help="Override type (Textbook/Guideline/Slides/Exam_Prep)")
    parser.add_argument("--pages-per-chunk", type=int, default=PAGES_PER_CHUNK,
                        help=f"Pages per chunk (default: {PAGES_PER_CHUNK})")
    parser.add_argument("--force",   action="store_true", help="Re-ingest even if already indexed")
    parser.add_argument("--no-save", action="store_true", help="Index directly, skip saving .md files")
    args = parser.parse_args()

    results = []
    for pattern in args.files:
        # glob support
        paths = list(Path(".").glob(pattern)) if "*" in pattern else [Path(pattern)]
        for pdf_path in sorted(paths):
            if not pdf_path.exists():
                print(f"not found: {pdf_path}")
                continue
            if pdf_path.suffix.lower() != ".pdf":
                continue
            print(f"\n=== {pdf_path.name} ===")
            result = ingest_pdf(
                pdf_path,
                specialty      = args.specialty,
                doc_type       = args.type,
                pages_per_chunk = args.pages_per_chunk,
                force          = args.force,
                no_save        = args.no_save,
            )
            results.append(result)
            if result.get("ok") and not result.get("skipped"):
                print(f"done: {result['chunks']} chunks | {result.get('specialty')} | {result.get('type')}")
            elif result.get("skipped"):
                print("skipped (already indexed)")
            else:
                print(f"FAILED: {result.get('error')}")

    print(f"\n{'='*50}")
    print("ChromaDB collections:", rag.collection_stats())


if __name__ == "__main__":
    main()
