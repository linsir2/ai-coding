"""``memory_write`` — the model writing its own durable note (R7e-1).

The harvest is retrospective: it runs after the turn, over the last N messages,
with an extractor.  Anything the model *knows* matters next session had no channel
of its own - it could only hope the extractor noticed.  This is that channel, and
it is deliberately narrow: one sentence, one of four kinds, and (optionally) an
explicit id to replace.

Writing is durable and cross-session, but it is not a side effect on the user's
repository, and the harness writes notes of its own accord anyway - so the gate
allows it (docs/r7e-analysis.md Q1).  ``evidence="model"`` is what makes these
notes visible as such in ``/memory``.
"""

from __future__ import annotations

from typing import Any

from ai_coding.domain.message import ToolResult
from ai_coding.memory.note import NoteKind
from ai_coding.tools.base import BaseTool, ToolContext, resolve_ctx

__all__ = ["MAX_TEXT", "MemoryWriteTool"]

#: A memory is one line.  Anything longer is a file, or a todo item.
MAX_TEXT = 500
_KINDS = ", ".join(kind.value for kind in NoteKind)


class MemoryWriteTool(BaseTool):
    """Write one durable note (fact / decision / preference / open_thread)."""

    parallel_safe = True

    def __init__(self, memory: Any) -> None:
        self._memory = memory

    @property
    def name(self) -> str:
        return "memory_write"

    @property
    def description(self) -> str:
        return (
            "Write one durable note about this project, to be recalled in later "
            "sessions. Use it when something will still matter next time and is not "
            "already obvious from the code: a decision and why it was made, a user "
            "preference, a convention, or an open thread. Do NOT use it to record "
            "what you just did this turn - the harness keeps that itself. Keep it to "
            "one sentence; pass 'supersedes' with an existing note id to replace a "
            "note that is now wrong (search first with memory_search)."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "One sentence to remember (max 500 characters).",
                },
                "kind": {
                    "type": "string",
                    "description": f"One of: {_KINDS}. Defaults to fact.",
                },
                "importance": {
                    "type": "integer",
                    "description": "1-5, how much this should survive (default 3).",
                },
                "supersedes": {
                    "type": "string",
                    "description": "Optional id of an existing note this replaces.",
                },
                "scene": {
                    "type": "string",
                    "description": (
                        "Optional short topic label (max 80 characters) grouping "
                        "this note with related ones, e.g. '依赖管理'. Use the same "
                        "wording memory already uses — a search that misses lists "
                        "the labels in index.top_scenes."
                    ),
                },
            },
            "required": ["text"],
        }

    async def execute(
        self, params: dict[str, Any], ctx: ToolContext | None = None
    ) -> ToolResult:
        text = str(params.get("text", "") or "").strip()
        if not text:
            return ToolResult(
                success=False, output="error: 'text' is required", error="missing_text"
            )
        truncated = len(text) > MAX_TEXT
        if truncated:
            text = text[:MAX_TEXT] + "…"
        kind = str(params.get("kind", "") or NoteKind.FACT.value).strip().casefold()
        if kind not in {member.value for member in NoteKind}:
            return ToolResult(
                success=False,
                output=f"error: unknown kind '{kind}'; use one of: {_KINDS}",
                error="unknown_kind",
                data={"kind": kind},
            )
        context = resolve_ctx(ctx)
        try:
            report = self._memory.write_note(
                text,
                kind=kind,
                importance=_as_importance(params.get("importance")),
                supersedes=str(params.get("supersedes", "") or "").strip(),
                turn=context.turn,
                session_id=context.session_id,
                scene=str(params.get("scene", "") or ""),
            )
        except Exception as exc:  # noqa: BLE001 - a memory write is never fatal
            return ToolResult(
                success=False,
                output=f"memory write failed: {exc}",
                error="memory_error",
                data={"text_chars": len(text)},
            )
        action, note_id = _classify(report)
        if not note_id:
            return ToolResult(
                success=False,
                output=(
                    "memory is disabled or the note could not be written; "
                    "carry on without it"
                ),
                error="memory_unavailable",
                data={"action": action},
            )
        spoken = (
            f"already remembered as [{kind}#{note_id[:8]}]"
            if action == "skipped"
            else f"remembered [{kind}#{note_id[:8]}] {text}"
        )
        if truncated:
            spoken += f" (note: truncated to {MAX_TEXT} characters)"
        return ToolResult(
            success=True,
            output=spoken,
            data={
                "action": action,
                "note_id": note_id,
                "kind": kind,
                "truncated": truncated,
                "text_chars": len(text),
            },
        )


def _as_importance(raw: Any) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 3
    return max(1, min(5, value))


def _classify(report: Any) -> tuple[str, str]:
    """Which of the four things happened, and to which note."""
    for action, ids in (
        ("added", getattr(report, "added", ())),
        ("superseded", getattr(report, "superseded", ())),
        ("updated", getattr(report, "updated", ())),
        ("skipped", getattr(report, "skipped", ())),
    ):
        if ids:
            return action, str(ids[0])
    return "none", ""
