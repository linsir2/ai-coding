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
                "include_near_misses": {
                    "type": "boolean",
                    "description": "Include the closest non-matching notes (default true).",
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
        wanted_evidence = str(params.get("evidence", "") or "").strip().casefold()
        if wanted_evidence and wanted_evidence not in ("tool", "model"):
            # Validated before the recall, so a bad value never reaches memory.
            return ToolResult(
                success=False,
                output="error: 'evidence' must be 'tool', 'model' or empty",
                error="bad_evidence",
                data={"evidence": wanted_evidence},
            )
        include_near_misses = bool(params.get("include_near_misses", True))
        try:
            detailed = getattr(self._memory, "recall_with_diagnostics", None)
            if detailed is None:
                # A memory double from before R10c: keep it working.
                hits = await self._memory.recall_notes(
                    query, k=_as_int(params.get("k"), 3)
                )
                diagnostics = _EMPTY_DIAGNOSTICS
            else:
                hits, diagnostics = await detailed(query, k=_as_int(params.get("k"), 3))
        except Exception as exc:  # noqa: BLE001 — the tool boundary
            return ToolResult(
                success=False,
                output=f"memory search failed: {exc}",
                error="memory_error",
                data={"query": query},
            )
        hits = _filter_hits(hits, params)
        notes = [_note_payload(hit) for hit in hits]
        data: dict[str, Any] = {
            "notes": notes,
            "no_match": not notes,
            "terms_known": list(diagnostics.terms_known),
            "terms_unknown": list(diagnostics.terms_unknown),
            "semantic_unavailable": bool(diagnostics.semantic_unavailable),
        }
        if include_near_misses:
            data["near_misses"] = [
                {
                    "id": hit.note.id,
                    "kind": hit.note.kind.value,
                    "score": round(hit.score, 3),
                    "relevance": round(hit.relevance, 3),
                    "preview": hit.note.text[:40],
                }
                for hit in diagnostics.near_misses[:3]
            ]
        if not notes:
            summary = diagnostics.index_summary
            if summary is not None:
                data["index"] = {
                    "total": summary.total,
                    "by_kind": dict(summary.by_kind),
                    "recent": [
                        {"id": note_id, "kind": kind, "preview": preview}
                        for note_id, kind, preview in summary.recent
                    ],
                }
            return ToolResult(success=True, output=_miss_text(data), data=data)
        lines = [
            f"- [{note['kind']}#{note['id'][:8]} score={note['score']} "
            f"matched={'/'.join(note['matched_terms'])}] {note['text']}"
            for note in notes
        ]
        return ToolResult(success=True, output="\n".join(lines), data=data)


def _filter_hits(hits: list[Any], params: dict[str, Any]) -> list[Any]:
    """kind / evidence filters. Task 5 moves these into the candidate set."""
    wanted = str(params.get("kind", "") or "").strip().casefold()
    if wanted:
        hits = [hit for hit in hits if hit.note.kind.value == wanted]
    evidence = str(params.get("evidence", "") or "").strip().casefold()
    if evidence == "tool":
        hits = [hit for hit in hits if (hit.note.evidence or "") == "tool"]
    elif evidence == "model":
        # "model" is the *complement* of "tool": notes written before R6d have
        # no provenance at all and must not be hidden by this filter.
        hits = [hit for hit in hits if (hit.note.evidence or "") != "tool"]
    return hits


def _note_payload(hit: Any) -> dict[str, Any]:
    return {
        "id": hit.note.id,
        "kind": hit.note.kind.value,
        "score": round(hit.score, 3),
        "relevance": round(hit.relevance, 3),
        "matched_terms": list(hit.matched_terms),
        "text": hit.note.text,
        "updated_time": hit.note.updated_time or hit.note.created_time,
        "pinned": hit.note.pinned,
        "evidence": hit.note.evidence,
    }


def _miss_text(data: dict[str, Any]) -> str:
    """The dead end, replaced by something the model can act on.

    Three things it has to convey: how big memory is, which of its own words
    memory has never seen, and that rephrasing is the next move.
    """
    index = data.get("index") or {}
    total = index.get("total", 0)
    by_kind = index.get("by_kind") or {}
    breakdown = ", ".join(f"{k} {v}" for k, v in by_kind.items()) or "空"
    lines = [f"没有可靠匹配（索引里有 {total} 条：{breakdown}）。"]
    if data["terms_unknown"]:
        lines.append(f"这些词记忆里没有：{'、'.join(data['terms_unknown'][:6])}。")
    near = data.get("near_misses") or []
    if near:
        preview = "；".join(
            f"#{item['id'][:8]}({item['kind']}) {item['preview']}" for item in near[:3]
        )
        lines.append(f"最接近但未经证实：{preview}")
    lines.append("换一个说法再搜，或用 kind=decision/fact/preference/open_thread 缩小范围。")
    return "\n".join(lines)


class _EmptyDiagnostics:
    """Stand-in for a memory double that predates ``recall_with_diagnostics``."""

    terms_known: tuple[str, ...] = ()
    terms_unknown: tuple[str, ...] = ()
    near_misses: tuple[Any, ...] = ()
    index_summary: Any = None
    semantic_unavailable: bool = False
    no_vocabulary_match: bool = False


_EMPTY_DIAGNOSTICS = _EmptyDiagnostics()


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
