"""
Manifest loader + inbox scanner for batch ingestion.

Manifest format (YAML or JSON):
    sources:
      - url:  https://ginasthma.org/.../GINA-2025.pdf
        type: Guideline
      - file: ./pdfs/harrisons.pdf
        type: Textbook
        force: true
      - pair:                        # reserved for future exam-prep branch
          content: ./pdfs/cardio.pdf
          exam:    ./pdfs/cardio_exam.pdf
          type:    Exam_Prep

Each spec dict returned has keys:
    file | url        — source location  (mutually exclusive)
    type              — optional document type override
    force             — optional, re-ingest even if already indexed
    pair              — optional, dict {content, exam, type} (future feature)
"""
from __future__ import annotations
import json
import yaml
from pathlib import Path
from typing import Any

# Folder convention for "drop and forget" mode
INBOX_DIR = Path("inbox")
INBOX_DONE_DIR = INBOX_DIR / "processed"

# Default manifest path
DEFAULT_MANIFEST = Path("knowledge_pipeline/sources.yaml")


def load_manifest(path: str | Path) -> list[dict[str, Any]]:
    """Parse manifest YAML/JSON → list of source specs."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Manifest not found: {p}")

    text = p.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text) if p.suffix.lower() in (".yaml", ".yml") else json.loads(text)
    except Exception as e:
        raise ValueError(f"Failed to parse manifest {p}: {e}") from e

    if not isinstance(data, dict) or "sources" not in data:
        raise ValueError(f"Manifest must contain top-level 'sources:' key. Got: {type(data)}")

    sources = data["sources"]
    if not isinstance(sources, list):
        raise ValueError("'sources' must be a list")

    cleaned: list[dict[str, Any]] = []
    for i, item in enumerate(sources):
        if not isinstance(item, dict):
            print(f"⚠️  Skipping entry {i}: not a dict")
            continue
        if not any(k in item for k in ("url", "file", "pair")):
            print(f"⚠️  Skipping entry {i}: must have one of url/file/pair")
            continue
        cleaned.append(item)

    return cleaned


def parse_manifest_text(text: str) -> list[dict[str, Any]]:
    """Same as load_manifest but accepts raw YAML/JSON string (for web upload)."""
    try:
        data = yaml.safe_load(text)
    except Exception as e:
        raise ValueError(f"Failed to parse manifest text: {e}") from e

    if not isinstance(data, dict) or "sources" not in data:
        raise ValueError("Manifest must contain top-level 'sources:' key")

    sources = data["sources"]
    if not isinstance(sources, list):
        raise ValueError("'sources' must be a list")

    return [item for item in sources if isinstance(item, dict)
            and any(k in item for k in ("url", "file", "pair"))]


def scan_inbox(inbox_dir: str | Path = INBOX_DIR) -> list[dict[str, Any]]:
    """Return one spec per PDF file in inbox/ (recursively). Skips processed/."""
    inbox = Path(inbox_dir)
    if not inbox.exists():
        return []

    specs: list[dict[str, Any]] = []
    for pdf in inbox.rglob("*.pdf"):
        if INBOX_DONE_DIR in pdf.parents:
            continue
        specs.append({"file": str(pdf)})
    return specs


def archive_inbox_file(pdf_path: str | Path) -> Path | None:
    """Move successfully-ingested file from inbox/ to inbox/processed/."""
    p = Path(pdf_path)
    if not p.exists() or INBOX_DIR not in p.parents:
        return None
    INBOX_DONE_DIR.mkdir(parents=True, exist_ok=True)
    dest = INBOX_DONE_DIR / p.name
    p.rename(dest)
    return dest
