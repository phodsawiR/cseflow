"""
notebooklm_client.py — Drop-in replacement for the old NotebookLM browser client.
Same public interface as before; queries ChromaDB (knowledge_rag) instead of NotebookLM.

Public API (unchanged — session.py imports these):
  query_notebook(specialty, question) -> str
  query_from_source_plan(source_plan) -> str
"""

import json
import os
from typing import Dict

from schemas import SourcePlan
import knowledge_rag as rag


def load_notebooks() -> Dict[str, str]:
    path = os.path.join(os.path.dirname(__file__), "notebooks.json")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


NOTEBOOKS: Dict[str, str] = load_notebooks()

# Map old NotebookLM notebook keys → ChromaDB collection names
# ChromaDB collections are named from the chunk's Specialty metadata field (lowercased).
# Old notebooks.json grouped "General_Practice" content under "symptom_approach" notebook.
_SPECIALTY_ALIASES: Dict[str, str] = {
    "symptom_approach":  "general_practice",   # old NLM notebook → new collection
    "general":           "general_practice",
    "internal_medicine": "general_practice",
    "endocrine":         "endocrine",
    "nephrology":        "nephrology",
    "renal":             "nephrology",
    "cardiology":        "cardiology",
    "pulmonology":       "pulmonology",
    "infectious_disease": "infectious_disease",
    "id":                "infectious_disease",
    "pharmacology":      "pharmacology",
}


def _resolve_specialty(specialty: str) -> str:
    """Map notebook key / old specialty name → ChromaDB collection name."""
    return _SPECIALTY_ALIASES.get(specialty, specialty)


# ─────────────────────────────────────────
# Public sync functions (same interface as before)
# ─────────────────────────────────────────

def query_notebook(specialty: str, question: str) -> str:
    """
    Query a single specialty collection — sync.
    Replaces the old NotebookLM API call.
    """
    resolved = _resolve_specialty(specialty)
    try:
        result = rag.query(resolved, question, n_results=5)
        return result
    except Exception as e:
        return f"[KB error: {e}]"


def query_from_source_plan(source_plan: SourcePlan) -> str:
    """
    Query all targets in a SourcePlan — sync wrapper for session.py.
    Replaces the old async NotebookLM multi-target query.
    """
    if not source_plan.notebook_targets:
        return "[KB: no targets in source plan]"

    targets = [
        {
            "notebook": _resolve_specialty(t.notebook),
            "keywords": t.keywords,
            "priority": t.priority,
        }
        for t in source_plan.notebook_targets
    ]

    try:
        return rag.query_multi(targets)
    except Exception as e:
        return f"[KB error: {e}]"
