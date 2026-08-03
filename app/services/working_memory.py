"""Validated working-memory patch construction.

This project currently uses a deterministic reply generator. Keep the updater
deterministic too; when an LLM tool-call layer is added, replace only
``infer_working_memory_update`` with schema-validated model output.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

EXECUTION_STATES = {"idle", "in_progress", "waiting_user", "waiting_tool", "blocked", "done"}
ITEM_KINDS = {
    "subtask", "next_action", "constraint", "decision",
    "open_question", "entity_reference", "temporary_fact",
}


class WorkingMemoryItemPatch(BaseModel):
    kind: str
    dedupe_key: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1, max_length=2000)
    priority: int = Field(default=50, ge=0, le=100)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        if self.kind not in ITEM_KINDS:
            raise ValueError(f"unsupported working-memory item kind: {self.kind}")


class WorkingMemoryPatch(BaseModel):
    active_goal: Optional[str] = Field(default=None, max_length=2000)
    execution_state: Optional[str] = None
    task_context: Optional[Dict[str, Any]] = None
    upsert_items: list[WorkingMemoryItemPatch] = Field(default_factory=list, max_length=12)
    resolve_item_keys: list[str] = Field(default_factory=list, max_length=12)
    refresh_ttl: bool = False

    def model_post_init(self, __context: Any) -> None:
        if self.execution_state is not None and self.execution_state not in EXECUTION_STATES:
            raise ValueError(f"unsupported execution state: {self.execution_state}")


class AgentResult(BaseModel):
    """The only contract `/chat` needs from an assistant implementation.

    A future LLM/tool agent must return this shape through structured output;
    FastAPI validates it before any working-memory mutation is attempted.
    """
    reply: str = Field(min_length=1)
    used_memory: Dict[str, Any] = Field(default_factory=dict)
    working_memory_patch: WorkingMemoryPatch = Field(default_factory=WorkingMemoryPatch)


GOAL_PREFIX = re.compile(
    r"^(?:please\s+)?(?:help me\s+)?(build|fix|implement|add|create|design|debug|plan|improve|refactor|set up|setup)\b",
    re.IGNORECASE,
)
QUESTION_PREFIX = re.compile(r"^(what|why|how|when|where|who|which|can|could|would|should|is|are|do|does|did)\b", re.IGNORECASE)


def _key(prefix: str, text: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return f"{prefix}:{normalized[:120] or 'item'}"


def infer_working_memory_update(message: str, current_wm: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Create a conservative patch from explicit user intent.

    It deliberately does not copy the raw user message into working memory.
    """
    text = (message or "").strip()
    if not text:
        return WorkingMemoryPatch().model_dump()

    lower = text.lower()
    current_goal = (current_wm or {}).get("active_goal")
    items: list[WorkingMemoryItemPatch] = []
    resolved: list[str] = []
    goal: Optional[str] = None
    state: Optional[str] = None
    refresh = False

    is_new_goal = bool(GOAL_PREFIX.match(text))
    is_question = text.endswith("?") or bool(QUESTION_PREFIX.match(text))
    is_completion = bool(re.search(r"\b(done|completed|finished|resolved)\b", lower))

    if is_new_goal:
        goal = text[:500]
        state = "in_progress"
        refresh = True
        items.append(WorkingMemoryItemPatch(
            kind="next_action", dedupe_key=_key("goal", goal), content=goal,
            priority=100, metadata={"source": "explicit_user_goal"},
        ))
        if current_goal and current_goal != goal:
            resolved.append(_key("goal", current_goal))
    elif is_question:
        # A user question is unresolved task context, not evidence that the
        # assistant is waiting for the user. Only an assistant/tool workflow
        # should set waiting_user or waiting_tool.
        state = "in_progress" if current_goal else "idle"
        refresh = bool(current_goal)
        items.append(WorkingMemoryItemPatch(
            kind="open_question", dedupe_key=_key("question", text), content=text[:1000],
            priority=80, confidence=0.9, metadata={"source": "user_question"},
        ))
    elif is_completion and current_goal:
        state = "done"
        refresh = True
        resolved.append(_key("goal", current_goal))

    patch = WorkingMemoryPatch(
        active_goal=goal,
        execution_state=state,
        upsert_items=items,
        resolve_item_keys=resolved,
        refresh_ttl=refresh,
    )
    return patch.model_dump(exclude_none=True)
