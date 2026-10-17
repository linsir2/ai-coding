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

It searches two corpora. The first is this session's own transcript, read from
``ctx.session_messages`` rather than ``ctx.messages`` (this request's view,
already rewritten by compaction): the latter is by construction the part the
model can already see, so searching it would answer a question nobody asked.

The second is ``<workspace>/transcripts/archive/`` -- what ``/compact`` replaced
with a summary. ``transcripts/archive`` is workspace-shared and
``ContextManager`` has no session concept, so **an archive hit is not this
conversation**: it may be any session in this workspace, and it may be months
old. Every line therefore names where it came from (``session #7`` or the
archive file's stamp), because a hit that hides its origin is worse than no
hit at all. Reading another session's session *files* is still not something
this tool does; only the archives a compaction left behind.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_coding.core.context_compression import iter_archived_records
from ai_coding.domain.message import ToolResult
from ai_coding.tools.base import BaseTool, ToolContext

__all__ = ["HistorySearchTool", "search_history", "search_archived_text", "HistoryHit"]

#: How many hits one call may bring back. A cap the model cannot widen past:
#: this output lands in the same window that ran short of context.
_DEFAULT_LIMIT = 8
_MAX_LIMIT = 20

#: How much of one message is quoted. Long enough to recognise a decision, short
#: enough that twenty hits cannot crowd out the conversation.
_CONTENT_CHARS = 400

#: ``transcripts/archive/`` -- the same directory :meth:`archive_transcript`
#: writes, reached the same way (``ContextManager``'s root is the workspace).
_ARCHIVE_SUBDIR = ("transcripts", "archive")
#: Files read at most, newest first. Hitting the cap is fine; hiding it is not,
#: so the count is reported.
_MAX_ARCHIVE_FILES = 20
#: Same value and same meaning as ``grep_tool._MAX_FILE_BYTES``: a transcript
#: this big is not worth the read, and the model is told it was not read.
_MAX_ARCHIVE_BYTES = 1_000_000


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


@dataclass(frozen=True)
class _Record:
    """The shape :func:`search_history` already reads.

    An archive file holds text, not messages, so its records are handed over in
    whatever object carries ``role``/``content``/``timestamp`` -- one adapter, so
    that the matching rule (case-folded substring, one implementation) has exactly
    one home.
    """

    role: str
    content: str
    timestamp: str


def search_archived_text(
    text: str, stamp: str, query: str, *, limit: int = _DEFAULT_LIMIT
) -> list[HistoryHit]:
    """One archived transcript, searched by the same matcher as a live session.

    The timestamp on every hit is the archive file's own stamp, not the message's:
    an archived message carries no timestamp of its own -- the file name is the
    only record of when it was said.
    """
    records = [
        _Record(role=role, content=body, timestamp=stamp)
        for role, body in iter_archived_records(text)
    ]
    return search_history(records, query, limit=limit)


def _archive_hits(
    root: Path, query: str, *, limit: int
) -> tuple[list[tuple[str, HistoryHit]], int, int, int]:
    """Search the workspace's archives: newest first.

    Returns ``(hits with their file stem, files present, files read, files
    skipped)``. The counts are the point of the tuple -- "no match" and "never
    looked" must not read the same to the model. Filenames lead with a
    zero-padded stamp, so sorting by name descending sorts by time.
    """
    try:
        files = sorted(root.glob("*.md"), reverse=True)
    except OSError:
        return [], 0, 0, 0
    hits: list[tuple[str, HistoryHit]] = []
    read = 0
    skipped = 0
    for path in files:
        if len(hits) >= limit or read >= _MAX_ARCHIVE_FILES:
            break
        try:
            if path.stat().st_size > _MAX_ARCHIVE_BYTES:
                skipped += 1
                continue
            text = path.read_text(encoding="utf-8")
        except OSError:
            skipped += 1
            continue
        read += 1
        for hit in search_archived_text(text, path.stem, query, limit=limit - len(hits)):
            hits.append((path.stem, hit))
    return hits, len(files), read, skipped


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
            "Search what this conversation has already said -- its own transcript, "
            "plus the transcripts in this workspace that a compaction replaced "
            "with a summary -- and get back the matching messages with their "
            "position, role and timestamp. Use it when an earlier part of the "
            "conversation was compacted away: the summary may have kept the gist "
            "but not the exact wording, a path, an id or a number the user gave "
            "you. Each result says whether it came from this session or from an "
            "archive, and an archive result may belong to a different session in "
            "this workspace. It searches what was said, not what it meant: quote "
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
        limit = params.get("limit", _DEFAULT_LIMIT)
        try:
            capped = int(limit) if limit is not None else _DEFAULT_LIMIT
        except (TypeError, ValueError):
            capped = _DEFAULT_LIMIT

        hits = search_history(transcript, query, limit=capped)
        # This conversation answers the question first. Only what it could not
        # answer is worth looking for elsewhere -- and when the session alone
        # filled the limit, that is said out loud rather than left implied.
        archive_lines: list[str] = []
        archive_note = ""
        if len(hits) < capped:
            found, total_files, read, skipped = _archive_hits(
                Path(context.workspace).joinpath(*_ARCHIVE_SUBDIR), query, limit=capped - len(hits)
            )
            archive_lines = [
                # No timestamp column: the stem in front of it *is* the time,
                # and printing it twice would be one fact stated twice.
                f"{stem} #{hit.index} [{hit.role}] {_clip(hit.content)}"
                for stem, hit in found
            ]
            if total_files:
                archive_note = f"{len(found)} in archive {read}/{total_files} files"
                if skipped:
                    archive_note += f" ({skipped} over size limit)"
            # No archive files at all: say nothing. Most sessions have never been
            # compacted by hand, and a second corpus with nothing in it is not
            # news -- the wording for those stays exactly what it was.
        else:
            archive_note = "archives not scanned (this session already filled the limit)"

        if not hits and not archive_lines:
            if not transcript:
                # Distinct from "no match": the wiring is missing, and saying
                # "you never said that" would be a confident lie. Asked *after*
                # the archives, because with two corpora a real answer outranks
                # the complaint.
                return ToolResult(
                    success=False,
                    output=(
                        "error: this session has no transcript attached to the tool "
                        "(history_search is not wired here)"
                    ),
                    error="no_transcript",
                )
            counts = f"in {len(transcript)} messages of this session"
            if archive_note:
                counts += f", {archive_note}"
            return ToolResult(
                success=True,
                output=f"no match for {query!r} {counts}",
                data={"query": query, "matches": 0, "scanned": len(transcript)},
            )
        lines = [
            f"session #{hit.index} [{hit.role}] {hit.timestamp} {_clip(hit.content)}"
            for hit in hits
        ]
        truncated = len(hits) >= min(max(1, capped), _MAX_LIMIT) and hits[-1].index + 1 < len(
            transcript
        )
        counts = f"{len(hits)} in {len(transcript)} messages of this session"
        if archive_note:
            counts += f", {archive_note}"
        header = f"{len(hits) + len(archive_lines)} match(es) for {query!r}: {counts}"
        if not transcript:
            header += " (no session transcript attached to this tool)"
        if truncated:
            header += " (older matches may exist: raise limit or narrow the phrase)"
        return ToolResult(
            success=True,
            output="\n".join([header, *lines, *archive_lines]),
            data={
                "query": query,
                "matches": len(hits) + len(archive_lines),
                "scanned": len(transcript),
                "positions": [hit.index for hit in hits],
            },
        )
