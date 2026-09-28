"""``memory_search`` — on-demand recall (R3b).

Why a tool and not another prompt section: a tool result is **appended to the
history**, so it is cached from then on, while anything injected per turn is paid
for every turn (docs/r3-analysis.md 1.13: 94% vs 7% prefix-hit on the same
content).  It also lets the model ask *precisely* ("what did we decide about
dependency management?") instead of the loop guessing on every request.

Read-only, so the permission gate allows it without asking the user.
"""

from __future__ import annotations

from typing import Any

from ai_coding.domain.message import ToolResult
from ai_coding.tools.base import BaseTool, ToolContext, resolve_ctx

__all__ = ["MemorySearchTool"]


class MemorySearchTool(BaseTool):
    """Search the durable memory store and return structured notes."""

    parallel_safe = True

    def __init__(self, memory: Any) -> None:
        self._memory = memory

    @property
    def name(self) -> str:
        return "memory_search"

    @property
    def description(self) -> str:
        return (
            "Search your own durable memory (project conventions, user preferences, "
            "decisions made in earlier sessions) and return the best matches with "
            "their ids and scores. Use it when the conversation refers to something "
            "that was decided or configured before, or when the user asks why "
            "something is the way it is."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to look up (a topic, file, tool or decision).",
                },
                "k": {
                    "type": "integer",
                    "description": "How many notes to return (default 3).",
                },
                "kind": {
                    "type": "string",
                    "description": "Optional filter: fact | decision | preference | open_thread.",
                },
                "evidence": {
                    "type": "string",
                    "description": (
                        "Optional filter: 'tool' = notes recorded in a turn where a "
                        "tool call succeeded; 'model' = everything else (including "
                        "notes from before this was recorded)."
                    ),
                },
            },
            "required": ["query"],
        }

    async def execute(
        self, params: dict[str, Any], ctx: ToolContext | None = None
    ) -> ToolResult:
        query = str(params.get("query", "") or "").strip()
        if not query:
            query = _query_from_context(resolve_ctx(ctx))
        if not query:
            return ToolResult(
                success=False,
                output="error: 'query' is required",
                error="empty_query",
                data={"query": ""},
            )
        try:
            hits = await self._memory.recall_notes(query, k=_as_int(params.get("k"), 3))
        except Exception as exc:  # noqa: BLE001 — the tool boundary
            return ToolResult(
                success=False,
                output=f"memory search failed: {exc}",
                error="memory_error",
                data={"query": query},
            )
        wanted = str(params.get("kind", "") or "").strip().casefold()
        if wanted:
            hits = [hit for hit in hits if hit.note.kind.value == wanted]
        wanted_evidence = str(params.get("evidence", "") or "").strip().casefold()
        if wanted_evidence and wanted_evidence not in ("tool", "model"):
            return ToolResult(
                success=False,
                output="error: 'evidence' must be 'tool', 'model' or empty",
                error="bad_evidence",
                data={"evidence": wanted_evidence},
            )
        if wanted_evidence == "tool":
            hits = [hit for hit in hits if (hit.note.evidence or "") == "tool"]
        elif wanted_evidence == "model":
            # "model" is the *complement* of "tool": notes written before R6d have
            # no provenance at all and must not be hidden by this filter.
            hits = [hit for hit in hits if (hit.note.evidence or "") != "tool"]
        notes = [
            {
                "id": hit.note.id,
                "kind": hit.note.kind.value,
                "score": round(hit.score, 3),
                "relevance": round(hit.relevance, 3),
                "text": hit.note.text,
                "updated_time": hit.note.updated_time or hit.note.created_time,
                "pinned": hit.note.pinned,
                "evidence": hit.note.evidence,
            }
            for hit in hits
        ]
        if not notes:
            return ToolResult(
                success=True, output="(no matching memories)", data={"notes": []}
            )
        lines = [
            f"- [{note['kind']}#{note['id'][:8]} score={note['score']}] {note['text']}"
            for note in notes
        ]
        return ToolResult(success=True, output="\n".join(lines), data={"notes": notes})


def _as_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _query_from_context(ctx: ToolContext) -> str:
    """Fall back to the parent turn's view when the model omits the query."""
    for message in reversed(ctx.messages):
        body = (message.content or "").strip()
        if message.role == "user" and body:
            return body
    return ""
