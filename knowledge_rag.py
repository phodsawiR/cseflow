"""
knowledge_rag.py — Local RAG with ChromaDB + Gemini text-embedding-004
Replaces NotebookLM browser-based queries with persistent local vector search.
"""
import hashlib
import os
import re
import time
import yaml
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

CHROMA_DIR     = Path("knowledge_base/chroma_db")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
_EMBED_MODEL   = "gemini-embedding-001"
_EMBED_DIM     = 3072

_gemini_client = None
_chroma_client = None


# ── Lazy singletons ───────────────────────────────────────────────────────────

def _gemini():
    global _gemini_client
    if _gemini_client is None:
        if not GOOGLE_API_KEY:
            raise ValueError("Missing GOOGLE_API_KEY in .env")
        from google import genai
        _gemini_client = genai.Client(api_key=GOOGLE_API_KEY)
    return _gemini_client


def _chroma():
    global _chroma_client
    if _chroma_client is None:
        try:
            import chromadb
        except ImportError:
            raise ImportError("pip install chromadb")
        CHROMA_DIR.mkdir(parents=True, exist_ok=True)
        _chroma_client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    return _chroma_client


# ── Helpers ───────────────────────────────────────────────────────────────────

def _col_name(specialty: str) -> str:
    """Normalize specialty to valid ChromaDB collection name (a-z0-9_)."""
    name = specialty.lower().replace(" ", "_").replace("-", "_")
    name = re.sub(r"[^a-z0-9_]", "", name)
    return name or "general"


def _doc_id(fpath: Path) -> str:
    """Stable 32-char ID derived from file path."""
    return hashlib.md5(str(fpath).encode()).hexdigest()[:32]


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            try:
                meta = yaml.safe_load(content[3:end]) or {}
                return meta, content[end + 3:].strip()
            except Exception:
                pass
    return {}, content


def _embed_one(text: str) -> list[float]:
    """Embed a single text with retry on rate limit."""
    client = _gemini()
    for attempt in range(3):
        try:
            result = client.models.embed_content(
                model=_EMBED_MODEL,
                contents=text,
            )
            return result.embeddings[0].values
        except Exception as e:
            if attempt < 2:
                time.sleep(2 ** attempt)
            else:
                raise e
    return []


def _embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed texts with rate limiting between calls."""
    embeddings = []
    for i, text in enumerate(texts):
        embeddings.append(_embed_one(text))
        if (i + 1) % 10 == 0:
            time.sleep(0.5)  # gentle rate limit
    return embeddings


# ── Public API ────────────────────────────────────────────────────────────────

def index_files(files: list[Path], metadata: dict) -> int:
    """
    Embed and upsert chunk files into ChromaDB.
    Returns number of documents indexed.
    """
    specialty = str(metadata.get("Specialty", "general")).replace(" ", "_")
    col_name  = _col_name(specialty)
    db = _chroma()
    col = db.get_or_create_collection(
        name=col_name,
        metadata={"hnsw:space": "cosine"},
    )

    texts, ids, metas = [], [], []
    for fpath in files:
        try:
            raw = fpath.read_text(encoding="utf-8")
            chunk_meta, body = _parse_frontmatter(raw)
            if not body.strip():
                continue
            texts.append(body[:8000])
            ids.append(_doc_id(fpath))
            metas.append({
                "specialty": specialty,
                "doc_type":  str(metadata.get("Type", "")),
                "title":     str(chunk_meta.get("chunk_title", fpath.stem)),
                "source":    str(chunk_meta.get("source_document", "")),
                "file_path": str(fpath),
            })
        except Exception as e:
            print(f"[rag] skip {fpath.name}: {e}")

    if not texts:
        return 0

    print(f"[rag] embedding {len(texts)} chunks for '{specialty}'...")
    embeddings = _embed_batch(texts)

    col.upsert(documents=texts, embeddings=embeddings, ids=ids, metadatas=metas)
    print(f"[rag] indexed {len(texts)} chunks -> collection '{col_name}'")
    return len(texts)


def query(specialty: str, question: str, n_results: int = 5) -> str:
    """Semantic search in a specialty collection. Returns formatted context."""
    col_name = _col_name(specialty)
    db = _chroma()

    try:
        col = db.get_collection(col_name)
    except Exception:
        return f"[RAG: collection '{specialty}' not found — no documents indexed yet]"

    count = col.count()
    if count == 0:
        return f"[RAG: collection '{specialty}' is empty]"

    q_emb   = _embed_one(question)
    results = col.query(
        query_embeddings=[q_emb],
        n_results=min(n_results, count),
        include=["documents", "metadatas", "distances"],
    )

    docs  = results.get("documents", [[]])[0]
    metas = results.get("metadatas", [[]])[0]
    dists = results.get("distances", [[]])[0]

    if not docs:
        return f"[RAG: no results for '{question}' in '{specialty}']"

    lines = [f"[RAG source: {specialty} | query: {question}]"]
    for i, (doc, meta, dist) in enumerate(zip(docs, metas, dists), 1):
        title = meta.get("title", "")
        score = round(1 - dist, 3)  # cosine similarity
        lines.append(f"\n--- [{i}] {title} (relevance={score}) ---\n{doc[:1500]}")

    return "\n".join(lines)


def query_multi(targets: list[dict], n_per_target: int = 3) -> str:
    """
    Query multiple specialties × keywords.
    targets: [{"notebook": specialty, "keywords": [...], "priority": int}, ...]
    """
    results = []
    for target in sorted(targets, key=lambda t: t.get("priority", 1)):
        specialty = target.get("notebook", "general")
        keywords  = target.get("keywords", [])
        priority  = target.get("priority", 1)
        for kw in keywords:
            r = query(specialty, kw, n_results=n_per_target)
            results.append(f"[priority={priority}]\n{r}")

    return "\n\n---\n\n".join(results) if results else "[RAG: no results]"


def list_collections() -> list[str]:
    """Return names of all indexed specialty collections."""
    return [c.name for c in _chroma().list_collections()]


def collection_stats() -> dict:
    """Return {specialty: chunk_count} for all collections."""
    db = _chroma()
    return {c.name: db.get_collection(c.name).count() for c in db.list_collections()}
