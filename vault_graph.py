"""
vault_graph.py — Traverse Obsidian vault wiki-graph for context retrieval.
Given a list of topics, finds matching notes and follows [[wikilinks]] 1 hop
to return structured context for agent prompts.
"""
import os
import re
from pathlib import Path
from typing import Optional

VAULT_PATH = Path(os.path.dirname(os.path.abspath(__file__))) / "obsidian"

# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalize(name: str) -> str:
    return name.lower().replace("_", " ").strip()


def _build_index(vault_path: Path) -> dict[str, Path]:
    """Map normalized note name → file path for all .md files in vault."""
    index: dict[str, Path] = {}
    for fpath in vault_path.rglob("*.md"):
        if fpath.name.startswith("."):
            continue
        index[_normalize(fpath.stem)] = fpath
    return index


def _read_body(fpath: Path) -> str:
    """Read note, strip YAML frontmatter."""
    try:
        raw = fpath.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""
    if raw.startswith("---"):
        end = raw.find("---", 3)
        if end != -1:
            return raw[end + 3:].strip()
    return raw.strip()


def _extract_wikilinks(content: str) -> list[str]:
    links = re.findall(r'\[\[([^\]|#\n]+?)(?:[|#][^\]\n]*)?\]\]', content)
    return [_normalize(l) for l in links if l.strip()]


# ── Public API ────────────────────────────────────────────────────────────────

def get_vault_context(
    topics:             list[str],
    vault_path:         Path = VAULT_PATH,
    depth:              int  = 1,
    max_chars_per_note: int  = 1500,
    max_total_chars:    int  = 6000,
) -> str:
    """
    For each topic, load the matching Obsidian note + its linked neighbors (1 hop).
    Returns formatted context string, or "" if vault doesn't exist or no notes found.
    """
    if not vault_path.exists():
        return ""

    index  = _build_index(vault_path)
    seen:  set[str]  = set()
    parts: list[str] = []
    total: int       = 0

    def _add(fpath: Path, label: str):
        nonlocal total
        key = str(fpath)
        if key in seen or total >= max_total_chars:
            return
        seen.add(key)
        body = _read_body(fpath)[:max_chars_per_note]
        if len(body) < 50:  # skip near-empty notes
            return
        parts.append(f"[Vault: {label}]\n{body}")
        total += len(body)

    for topic in topics:
        norm = _normalize(topic)
        fpath = index.get(norm)

        # fuzzy fallback: first note whose name contains the topic
        if not fpath:
            for k, v in index.items():
                if norm in k or k in norm:
                    fpath = v
                    break

        if not fpath:
            continue

        _add(fpath, topic)

        if depth >= 1 and total < max_total_chars:
            body = _read_body(fpath)
            for linked in _extract_wikilinks(body)[:6]:
                linked_path = index.get(linked)
                if linked_path:
                    _add(linked_path, linked)

    if not parts:
        return ""

    return "[Obsidian Vault Context]\n\n" + "\n\n---\n\n".join(parts)


def list_note_names(vault_path: Path = VAULT_PATH) -> list[str]:
    """Return all note names in vault (for entity matching)."""
    if not vault_path.exists():
        return []
    return [fpath.stem for fpath in vault_path.rglob("*.md") if not fpath.name.startswith(".")]
