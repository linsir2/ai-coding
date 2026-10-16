"""C-1: read back what compaction took out of the model's window.

Both reference harnesses name this as their own gap (dsh 08 §8-2: "no recall,
no expansion of the shadowed region"; oc 06 §10: memory is the compaction
summary and nothing else). Here the bytes were never lost -- ``Compactor``
works on a copy and never mutates the session's history
(``context_compression.py``) -- so what the summary dropped is still the
session's own ``messages``.  This tool is the door to them.

It reads ``ctx.session_messages`` (the session's transcript) rather than
``ctx.messages`` (this request's view, already rewritten by compaction): the
latter is by construction the part the model can already see, so searching it
would answer a question nobody asked.

It searches *this* session only.  A cross-session search is a different tool
with a different blast radius (every session file in the workspace, none of
them the one being summarized); dsh's own ``session-query`` deliberately
excludes the caller's session, and opencode has no equivalent at all.  That
waited for its own measurement rather than riding along here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ai_coding.domain.message import ToolResult
from ai_coding.tools.base import BaseTool, ToolContext

__all__ = ["HistorySearchTool", "search_history", "HistoryHit"]

#: How many hits one call may bring back. A cap the model cannot widen past:
#: this output lands in the same window that ran short of context.
_DEFAULT_LIMIT = 8
_MAX_LIMIT = 20

#: How much of one message is quoted. Long enough to recognise a decision, short
#: enough that twenty hits cannot crowd out the conversation.
_CONTENT_CHARS = 400


@dataclass(frozen=True)
class HistoryHit:
    """One matching message, addressed the way the transcript addresses things."""

    index: int
    role: str
    timestamp: str
    content: str


def search_history(
    messages: Any, query: str, *, limit: int = _DEFAULT_LIMIT
) -> list[HistoryHit]:
    """Case-insensitive substring search over a transcript, oldest first.

    Substring, not embedding: the question this answers is "what did I write
    about X", where the model already knows the words and needs them found.
    Ranking a meaning-similarity index over the same corpus would cost an
    embedding per message and make a miss indistinguishable from a paraphrase.
    """
    needle = str(query or "").strip().lower()
    if not needle:
        return []
    cap = max(1, min(int(limit), _MAX_LIMIT))
    hits: list[HistoryHit] = []
    for index, message in enumerate(messages or ()):
        body = str(getattr(message, "content", "") or "").strip()
        if not body or needle not in body.lower():
            continue
        hits.append(
            HistoryHit(
                index=index,
                role=str(getattr(message, "role", "") or "?"),
                timestamp=str(getattr(message, "timestamp", "") or ""),
                content=body,
            )
        )
        if len(hits) >= cap:
            break
    return hits


def _clip(text: str, limit: int = _CONTENT_CHARS) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


class HistorySearchTool(BaseTool):
    """Find what an earlier part of this conversation said."""

    parallel_safe = True
    #: Read-only, and it must be: the tool exists for plan mode, where the
    #: model's window is the most compacted it ever gets.
    mutates_world = False

    @property
    def name(self) -> str:
        return "history_search"

    @property
    def description(self) -> str:
        return (
            "Search this conversation's own transcript for a word or phrase and get "
            "back the matching messages with their position, role and timestamp. "
            "Use it when an earlier part of the conversation was compacted away -- "
            "the summary may have kept the gist but not the exact wording, a "
            "path, an id or a number the user gave you. It searches only this "
            "session, and it searches what was said, not what it meant: quote "
            "what you remember and search for that."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The word or phrase to look for, as it was written.",
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        f"How many matches to return "
                        f"(default {_DEFAULT_LIMIT}, max {_MAX_LIMIT})."
                    ),
                },
            },
            "required": ["query"],
        }

    async def execute(self, params: dict[str, Any], ctx: ToolContext | None = None) -> ToolResult:
        context = ctx if ctx is not None else ToolContext()
        query = str(params.get("query", "") or "").strip()
        if not query:
            return ToolResult(
                success=False,
                output="error: 'query' is required (quote the words you are looking for)",
                error="empty_query",
            )
        transcript = tuple(context.session_messages or ())
        if not transcript:
            # Distinct from "no match": the wiring is missing, and saying
            # "you never said that" would be a confident lie.
            return ToolResult(
                success=False,
                output=(
                    "error: this session has no transcript attached to the tool "
                    "(history_search is not wired here)"
                ),
                error="no_transcript",
            )
        limit = params.get("limit", _DEFAULT_LIMIT)
        try:
            capped = int(limit) if limit is not None else _DEFAULT_LIMIT
        except (TypeError, ValueError):
            capped = _DEFAULT_LIMIT

        hits = search_history(transcript, query, limit=capped)
        if not hits:
            return ToolResult(
                success=True,
                output=(
                    f"no match for {query!r} "
                    f"in {len(transcript)} messages of this session"
                ),
                data={"query": query, "matches": 0, "scanned": len(transcript)},
            )
        lines = [
            f"#{hit.index} [{hit.role}] {hit.timestamp} {_clip(hit.content)}" for hit in hits
        ]
        truncated = len(hits) >= min(max(1, capped), _MAX_LIMIT) and hits[-1].index + 1 < len(
            transcript
        )
        header = f"{len(hits)} match(es) for {query!r} in {len(transcript)} messages"
        if truncated:
            header += " (older matches may exist: raise limit or narrow the phrase)"
        return ToolResult(
            success=True,
            output="\n".join([header, *lines]),
            data={
                "query": query,
                "matches": len(hits),
                "scanned": len(transcript),
                "positions": [hit.index for hit in hits],
            },
        )
