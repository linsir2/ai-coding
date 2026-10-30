"""``parallel_research`` — 2–4 independent read-only questions in one call.

The tool is a thin adapter: parsing and result mapping live here, the fan-out
lives in :mod:`ai_coding.core.research_fanout`, and everything that actually
runs a child (semaphore, timeout, usage) stays in
:class:`~ai_coding.core.subagent.SubAgentRunner`.  One tool, one orchestration
class, no second sub-agent runtime.

Read-only is enforced three ways: the ``explore`` agent's rules, the child
session's ``readonly`` flag (the tool surface drops mutating tools), and the
dispatch gate that refuses a mutating call structurally.  There is deliberately
no worktree: a readonly child has nothing to isolate from, and a HEAD snapshot
would hide the user's uncommitted work.  A project agent may override
``explore``; the readonly flag is what keeps the contract true anyway.
"""

from __future__ import annotations

from typing import Any

from ai_coding.core.observers import EmitObserver
from ai_coding.core.research_fanout import (
    CHILD_TIMEOUT_GRACE,
    ResearchFanOut,
    ResearchQuestion,
    render_report,
    total_usage,
)
from ai_coding.domain.message import ToolResult
from ai_coding.domain.subagent import (
    CONTEXT_MAX,
    OBJECTIVE_MAX,
    SCOPE_ITEM_MAX,
    SCOPE_ITEMS_MAX,
    SUCCESS_STATUSES,
    ParentContext,
)
from ai_coding.tools.base import BaseTool, ToolContext, resolve_ctx

__all__ = ["ParallelResearchTool"]

_MAX_QUESTIONS = 4
_MIN_QUESTIONS = 2


class ParallelResearchTool(BaseTool):
    """Fan out 2–4 read-only research questions and merge their answers."""

    #: Several of these may share one assistant batch; the runner's semaphore
    #: is the only concurrency bound (this tool adds none).
    parallel_safe = True
    mutates_world = False

    def __init__(self, runner: Any) -> None:
        self._runner = runner
        self._fanout = ResearchFanOut(runner)

    @property
    def name(self) -> str:
        return "parallel_research"

    @property
    def max_duration_seconds(self) -> float | None:
        """Dispatcher ceiling for the whole call (B0).

        The runner's semaphore runs at most ``max_concurrent`` children at once,
        so four questions can need two rounds of ``timeout_seconds + grace``.
        A ceiling of one round would cancel the call while the last lane is
        still inside its own budget, throwing away answers about to arrive; the
        children's own deadlines cannot help once the *tool* is cancelled.
        """
        timeout = getattr(self._runner, "timeout_seconds", None)
        if timeout is None:
            return None
        slots = max(1, int(getattr(self._runner, "max_concurrent", 1) or 1))
        lanes = -(-_MAX_QUESTIONS // slots)
        return float(lanes) * (float(timeout) + CHILD_TIMEOUT_GRACE)

    @property
    def description(self) -> str:
        return (
            "Run 2–4 INDEPENDENT, read-only research questions in parallel, each "
            "in its own explore sub-agent (clean context, the live working tree, "
            "no writes or commands), then return one merged report with per-question "
            "summaries and key files. Use it when the questions do not depend on "
            "each other; for one question use subagent, and for sequential "
            "follow-ups use ordinary tools. It is costly: every question is a "
            "full sub-agent run. Give a shared 'context' so the children do not "
            "need this conversation."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "minItems": _MIN_QUESTIONS,
                    "maxItems": _MAX_QUESTIONS,
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": "Short id; omit to get q1..qN.",
                            },
                            "question": {
                                "type": "string",
                                "description": "One independent question.",
                            },
                            "scope": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Directories/files to look in first.",
                            },
                        },
                        "required": ["question"],
                    },
                },
                "context": {
                    "type": "string",
                    "description": (
                        "Shared background and constraints every child needs "
                        "(goal, decisions, paths)."
                    ),
                },
            },
            "required": ["questions"],
        }

    async def execute(
        self, params: dict[str, Any], ctx: ToolContext | None = None
    ) -> ToolResult:
        context = resolve_ctx(ctx)
        questions, error, message = _parse_questions(params)
        if questions is None:
            return ToolResult(
                success=False,
                output=f"error: {message}",
                error=error,
                data={"questions": 0},
            )
        parent = None
        if context.messages or context.instructions or context.permissions:
            parent = ParentContext(
                messages=tuple(context.messages),
                instructions=context.instructions,
                permissions=tuple(context.permissions),
            )
        shared = str(params.get("context", "") or "").strip()
        packets = await self._fanout.run(
            questions,
            shared_context=shared,
            parent=parent,
            observer=EmitObserver(context.emit),
            parent_span=context.call_id,
        )
        completed = sum(1 for packet in packets if packet.status in SUCCESS_STATUSES)
        success = completed > 0
        return ToolResult(
            success=success,
            output=render_report(packets),
            error=None if success else "all_children_failed",
            data={
                "results": [packet.to_dict() for packet in packets],
                "usage": total_usage(packets),
                "questions": len(packets),
                "completed": completed,
            },
        )


def _parse_questions(
    params: dict[str, Any],
) -> tuple[list[ResearchQuestion] | None, str | None, str]:
    """``(questions, error_code, message)``; questions is None on refusal."""
    raw = params.get("questions")
    if not isinstance(raw, list):
        return None, "bad_questions", "'questions' must be an array"
    if len(raw) < _MIN_QUESTIONS:
        return (
            None,
            "too_few_questions",
            (
                f"at least {_MIN_QUESTIONS} independent questions are required; "
                "for one question use subagent instead"
            ),
        )
    if len(raw) > _MAX_QUESTIONS:
        return (
            None,
            "too_many_questions",
            f"at most {_MAX_QUESTIONS} independent questions are allowed",
        )
    raw_context = params.get("context")
    if raw_context is not None:
        if not isinstance(raw_context, str):
            return None, "bad_questions", "'context' must be a string"
        if len(raw_context) > CONTEXT_MAX:
            return (
                None,
                "context_too_long",
                f"'context' must be at most {CONTEXT_MAX} characters",
            )

    used: set[str] = set()
    parsed: list[ResearchQuestion] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            return None, "bad_questions", f"question {index + 1} must be an object"
        text = entry.get("question")
        if not isinstance(text, str) or not text.strip():
            return None, "bad_questions", f"question {index + 1} needs 'question'"
        if len(text) > OBJECTIVE_MAX:
            return (
                None,
                "bad_questions",
                f"question {index + 1} is longer than {OBJECTIVE_MAX} characters",
            )
        raw_id = entry.get("id")
        if raw_id is None:
            question_id = _default_id(index, used)
        else:
            question_id = str(raw_id).strip()
            if not question_id:
                return None, "bad_questions", f"question {index + 1} has an empty id"
            if question_id in used:
                return (
                    None,
                    "duplicate_question_id",
                    f"duplicate question id: {question_id}",
                )
        raw_scope = entry.get("scope") or []
        if not isinstance(raw_scope, list):
            return None, "bad_questions", f"question {index + 1} 'scope' must be an array"
        scope = tuple(
            str(item).strip()[:SCOPE_ITEM_MAX]
            for item in raw_scope[:SCOPE_ITEMS_MAX]
            if str(item).strip()
        )
        used.add(question_id)
        parsed.append(ResearchQuestion(question_id, text.strip(), scope))
    return parsed, None, ""


def _default_id(index: int, used: set[str]) -> str:
    """``q{index+1}``, with a suffix when an explicit id already took it."""
    candidate = f"q{index + 1}"
    suffix = 2
    while candidate in used:
        candidate = f"q{index + 1}-{suffix}"
        suffix += 1
    return candidate
