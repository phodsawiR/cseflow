import asyncio
import json
import os
from typing import Dict, List, Any, Union
from notebooklm import NotebookLMClient
from schemas import SourcePlan

def load_notebooks() -> Dict[str, str]:
    path = os.path.join(os.path.dirname(__file__), "notebooks.json")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

NOTEBOOKS: Dict[str, str] = load_notebooks()


# ─────────────────────────────────────────
# Core async functions
# ─────────────────────────────────────────
async def _query_async(notebook_id: str, question: str) -> str:
    async with await NotebookLMClient.from_storage() as client:
        response = await client.chat.ask(notebook_id, question)
        return response.answer if hasattr(response, "answer") else str(response)


async def _query_plan_async(source_plan: SourcePlan) -> str:
    results: List[str] = []
    async with await NotebookLMClient.from_storage() as client:
        for target in source_plan.notebook_targets:
            specialty  = target.notebook
            keywords   = target.keywords
            priority   = target.priority
            notebook_id = NOTEBOOKS.get(specialty)

            if not notebook_id:
                results.append(f"[KB: notebook '{specialty}' not found in notebooks.json]")
                continue

            for keyword in keywords:
                try:
                    resp = await client.chat.ask(notebook_id, keyword)
                    answer = resp.answer if hasattr(resp, "answer") else str(resp)
                    results.append(
                        f"[Source: {specialty} | Query: {keyword} | Priority: {priority}]\n{answer}"
                    )
                except Exception as e:
                    results.append(f"[KB error — {specialty}/{keyword}: {e}]")

    return "\n\n---\n\n".join(results) if results else "[KB: no results found]"


# ─────────────────────────────────────────
# Sync wrappers (ใช้ใน session.py ได้เลย)
# ─────────────────────────────────────────
def query_notebook(specialty: str, question: str) -> str:
    """Query notebook เดียวตาม specialty — sync"""
    notebook_id = NOTEBOOKS.get(specialty)
    if not notebook_id:
        return f"[KB: notebook '{specialty}' not found]"
    try:
        return asyncio.run(_query_async(notebook_id, question))
    except Exception as e:
        return f"[KB error: {e}]"


def query_from_source_plan(source_plan: SourcePlan) -> str:
    """Query ทุก target ใน source_plan — sync wrapper สำหรับ session.py"""
    if not source_plan.notebook_targets:
        return "[KB: no targets in source plan]"
    try:
        return asyncio.run(_query_plan_async(source_plan))
    except Exception as e:
        return f"[KB error: {e}]"
