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
                    "description": (
                        "Include the closest notes that did not clear the gate. "
                        "By default they are returned only when nothing matched; "
                        "pass true to also get them alongside a hit."
                    ),
                },
                "scope": {
                    "type": "string",
                    "description": (
                        "Optional: only notes whose scope matches exactly. Notes "
                        "written by this build are all 'workspace', so this only "
                        "narrows notes that were imported or written with another "
                        "scope."
                    ),
                },
                "since_days": {
                    "type": "integer",
                    "description": (
                        "Optional: only memories changed within the last N days "
                        "(falls back to the creation date when nothing has been "
                        "updated since)."
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
        wanted_evidence = str(params.get("evidence", "") or "").strip().casefold()
        if wanted_evidence and wanted_evidence not in ("tool", "model"):
            # Validated before the recall, so a bad value never reaches memory.
            return ToolResult(
                success=False,
                output="error: 'evidence' must be 'tool', 'model' or empty",
                error="bad_evidence",
                data={"evidence": wanted_evidence},
            )
        wanted_kind = str(params.get("kind", "") or "").strip().casefold()
        wanted_scope = str(params.get("scope", "") or "").strip()
        requested_near_misses = params.get("include_near_misses")
        # Normalised here, applied by the service: filters that narrow the candidate
        # set are the only path, so `k` is never spent on notes the model did not
        # ask for (and a filtered-out note is never recalled, so never touched).
        filters = {
            "kinds": [wanted_kind] if wanted_kind else None,
            "evidence": wanted_evidence or None,
            "scope": wanted_scope or None,
            "since_days": _optional_positive_int(params.get("since_days")),
        }
        try:
            detailed = getattr(self._memory, "recall_with_diagnostics", None)
            if detailed is None:
                # A memory double from before R10c: keep it working.  It cannot take
                # the filters, so they do not apply on this path.
                hits = await self._memory.recall_notes(query, k=_as_int(params.get("k"), 3))
                diagnostics = _EMPTY_DIAGNOSTICS
            else:
                hits, diagnostics = await detailed(
                    query, k=_as_int(params.get("k"), 3), **filters
                )
        except Exception as exc:  # noqa: BLE001 — the tool boundary
            return ToolResult(
                success=False,
                output=f"memory search failed: {exc}",
                error="memory_error",
                data={"query": query},
            )
        notes = [_note_payload(hit) for hit in hits]
        # A hit is the common case and does not need a diagnosis attached; the
        # model opts in when it wants candidates it can aim a follow-up at.
        ship_near_misses = (
            (not notes) if requested_near_misses is None else bool(requested_near_misses)
        )
        data: dict[str, Any] = {
            "notes": notes,
            "no_match": not notes,
            "terms_known": list(diagnostics.terms_known),
            "terms_unknown": list(diagnostics.terms_unknown),
            "semantic_unavailable": bool(diagnostics.semantic_unavailable),
        }
        if ship_near_misses:
            data["near_misses"] = [
                {
                    "id": hit.note.id,
                    "kind": hit.note.kind.value,
                    "score": round(hit.score, 3),
                    "relevance": round(hit.relevance, 3),
                    "preview": hit.note.text[:40],
                }
                # A candidate that shares no word with the query is not close to
                # anything, however well it scores on recency alone.
                for hit in diagnostics.near_misses[:3]
                if hit.matched_terms
            ]
        if not notes:
            summary = diagnostics.index_summary
            if summary is not None:
                data["index"] = {
                    # "active", not "total": status() reserves total for all notes
                    # including superseded ones, and this counts only the active set.
                    "active": summary.active,
                    "by_kind": dict(summary.by_kind),
                    "recent": [
                        {"id": note_id, "kind": kind, "preview": preview}
                        for note_id, kind, preview in summary.recent
                    ],
                }
            return ToolResult(
                success=True,
                output=_miss_text(
                    data,
                    # Measured by the service against the unfiltered index, and
                    # attributed to the filters that were actually asked for.
                    filtered_out=_filtered_out(diagnostics, filters),
                    filters=_filter_names(filters),
                    no_vocabulary_match=bool(diagnostics.no_vocabulary_match),
                ),
                data=data,
            )
        lines = [
            f"- [{note['kind']}#{note['id'][:8]} score={note['score']} "
            f"matched={'/'.join(note['matched_terms'])}] {note['text']}"
            for note in notes
        ]
        return ToolResult(success=True, output="\n".join(lines), data=data)


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


def _miss_text(
    data: dict[str, Any],
    *,
    filtered_out: int = 0,
    filters: str = "",
    no_vocabulary_match: bool = False,
) -> str:
    """The dead end, replaced by something the model can act on.

    Every clause has to be one we actually measured.  A count we never read, a
    "no match" that our own filter caused, or a relevance gate a pre-R10c double
    never ran are all lies, and this text is what the model reads.
    """
    index = data.get("index")
    if index is None:
        # No summary means memory is off, the ranker raised, or the double
        # predates diagnostics — none of which means the index is empty.
        lines = ["没有可靠匹配（读不到索引概览，不知道记忆里有多少条）。"]
    else:
        by_kind = index.get("by_kind") or {}
        breakdown = ", ".join(f"{k} {v}" for k, v in by_kind.items()) or "空"
        lines = [f"没有可靠匹配（索引里有 {index['active']} 条：{breakdown}）。"]
    unknown = data["terms_unknown"]
    if no_vocabulary_match:
        lines.append(
            f"这些词索引里一条笔记都没出现过：{'、'.join(unknown[:6])}——不是措辞问题，是这批内容不在记忆里。"
        )
    elif unknown:
        lines.append(f"这些词索引里没有：{'、'.join(unknown[:6])}。")
    near = data.get("near_misses") or []
    if near:
        preview = "；".join(
            f"#{item['id'][:8]}({item['kind']}) {item['preview']}" for item in near[:3]
        )
        lines.append(f"最接近但未经证实：{preview}")
    if filtered_out:
        # Saying "rephrase, or narrow by kind" here would be telling the model to
        # repeat the filter it just used, and blaming kind/evidence for a scope
        # miss would point it at a filter it never set.
        blamed = filters or "kind/evidence"
        lines.append(
            f"有 {filtered_out} 条通过了相关度门槛，"
            f"是 {blamed} 过滤把它们排除了：放宽过滤条件再搜一次。"
        )
        return "\n".join(lines)
    if not near and not unknown and index is not None:
        # Every word was known and nothing was close: the gate said no.  Gated on
        # a measured index, because otherwise we cannot tell a real rejection
        # from a double whose recall told us nothing.
        lines.append("查询用词索引都认识，但没有候选达到相关度门槛。")
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
    filtered_out: int = 0


_EMPTY_DIAGNOSTICS = _EmptyDiagnostics()


def _filter_names(filters: dict[str, Any]) -> str:
    """The parameters the caller actually set, named as the schema names them."""
    return "/".join(
        {"kinds": "kind"}.get(name, name) for name, value in filters.items() if value
    )


def _filtered_out(diagnostics: Any, filters: dict[str, Any]) -> int:
    """How many notes the filters cost this miss, or 0 when nothing claims a cause.

    Read with ``getattr`` because ``recall_with_diagnostics`` is duck-typed: a
    double that predates the field reports no count, and inventing one is exactly
    what this text must not do.
    """
    if not any(filters.values()):
        return 0
    try:
        return max(0, int(getattr(diagnostics, "filtered_out", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _as_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _optional_positive_int(value: Any) -> int | None:
    """A window in days, or nothing at all — 0 and negatives mean "no window"."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _query_from_context(ctx: ToolContext) -> str:
    """Fall back to the parent turn's view when the model omits the query."""
    for message in reversed(ctx.messages):
        body = (message.content or "").strip()
        if message.role == "user" and body:
            return body
    return ""
