"""``checkpoint_write`` — the model writes one line for its next self (R8a).

The harvest is retrospective: it runs after the turn, over the last N messages.
The digest ring is the same idea for a *single* turn — one line that survives a
compaction — but R6 wrote that line itself (the turn's request and its final
sentence).  This tool is the channel for the thing only the model knows: what it
was doing and what the next step is.

Deliberately narrow, and deliberately not ``memory_write``:

* ``checkpoint_write`` → this session, the digest ring, injected only after a
  compaction, gone by itself once the ring rolls over;
* ``memory_write`` → durable, cross-session, lives in ``.memory`` until someone
  deletes it (docs/r8-analysis.md 1.2 / 3.1).
"""

from __future__ import annotations

from typing import Any

from ai_coding.domain.message import ToolResult
from ai_coding.tools.base import BaseTool, ToolContext, resolve_ctx

__all__ = ["CheckpointTool"]


class CheckpointTool(BaseTool):
    """Write one line about where this turn got to."""

    #: It only touches the turn's own book: no file, no process, no shared state.
    parallel_safe = True
    mutates_world = False

    @property
    def name(self) -> str:
        return "checkpoint_write"

    @property
    def description(self) -> str:
        return (
            "Write one short line for your own future turns in THIS session: what "
            "you were doing, what you decided, what the next step is. The harness "
            "already records which files changed and the turn's status, so do not "
            "repeat those. Use memory_write instead when the note must still "
            "matter in another session."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "note": {
                    "type": "string",
                    "description": "One line, at most ~120 characters.",
                }
            },
            "required": ["note"],
        }

    async def execute(
        self, params: dict[str, Any], ctx: ToolContext | None = None
    ) -> ToolResult:
        note = str(params.get("note", "") or "").strip()
        if not note:
            return ToolResult(
                success=False,
                output="error: 'note' is required",
                error="missing_note",
                data={"note": ""},
            )
        sink = resolve_ctx(ctx).note_checkpoint
        if sink is None:
            # A sub-agent (or a bare unit call) has no turn book of its own; say so
            # instead of pretending the line was kept.
            return ToolResult(
                success=False,
                output="error: this turn cannot keep a checkpoint",
                error="checkpoint_unavailable",
                data={"note": note},
            )
        sink(note)
        return ToolResult(success=True, output=f"checkpoint: {note}")
