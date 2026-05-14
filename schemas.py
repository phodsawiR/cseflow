from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional
import re
import json

class Directive(BaseModel):
    target: str = Field(default="")
    instruction: str = Field(default="")

class NavigatorOutput(BaseModel):
    mode: str = Field(default="full")
    branch: Optional[str] = Field(default="A")
    patient_data: str = Field(default="")
    directives: List[Directive] = Field(default_factory=list)
    parallel_tasks: List[str] = Field(default_factory=list)

class SourceTarget(BaseModel):
    notebook: str = Field(default="")
    keywords: List[str] = Field(default_factory=list)
    priority: str = Field(default="medium")

class SourcePlan(BaseModel):
    notebook_targets: List[SourceTarget] = Field(default_factory=list)
    queries: List[str] = Field(default_factory=list)

class SmartRouterOutput(BaseModel):
    complexity: str = Field(default="medium")
    spawn_professor: bool = Field(default=False)

class RevisionRouterOutput(BaseModel):
    agent: str = Field(default="analyzer")
    section: str = Field(default="assessment")
    instruction: str = Field(default="")

def parse_llm_json(text: str) -> dict:
    """Extracts and parses JSON from LLM output, handling markdown wrappers."""
    if not text:
        return {}

    text = text.strip()
    # Remove markdown code blocks if present
    match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', text)
    if match:
        text = match.group(1)

    # Try to find the first { and last }
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1:
        text = text[start:end+1]

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}

