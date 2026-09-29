"""Loop events → human-readable blocks (pure: no Textual, offline-testable).

The console-era renderer's R8c semantics — one throttled line per tool call,
quiet on a completed turn — live here, with an injectable clock so the throttle
can be tested without sleeping.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from rich.text import Text

from ai_coding.core.events import (
    IterationStarted,
    LoopEvent,
    TaskUpdated,
    ToolChunk,
    ToolFinished,
    ToolStarted,
    ToolSurfaceWarning,
    TurnFinished,
)
from ai_coding.domain.loop import TurnStatus

_PARAM_PREVIEW = 100
_OUTPUT_PREVIEW = 120
_CHUNK_THROTTLE_SECONDS = 0.15
_TARGET_COMMAND_LIMIT = 60


@dataclass(frozen=True)
class Block:
    """One rendered unit of transcript content."""

    kind: str
    text: str
    style: str = ""
    call_id: str = ""
    tool_name: str = ""
    tool_target: str = ""
    tool_status: str = ""  # "" | "running" | "ok" | "failed"

    def render(self) -> Text:
        return Text(self.text, style=self.style)


def _short(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _tool_target(tool_name: str, params: dict[str, Any]) -> str:
    """Human-readable target for a tool call: a path or a truncated command."""
    if tool_name in {"read", "write", "edit", "glob"}:
        return str(params.get("path", ""))
    if tool_name == "bash":
        return _short(str(params.get("command", "")), _TARGET_COMMAND_LIMIT)
    return ""


def blocks_from_event(event: LoopEvent) -> Block | None:
    """Map one loop event to a block, or ``None`` when it is not shown."""
    if isinstance(event, IterationStarted):
        return Block("turn", f"[turn {event.index}/{event.max_iterations}]", "dim")
    if isinstance(event, ToolStarted):
        params = _short(json.dumps(event.params, ensure_ascii=False), _PARAM_PREVIEW)
        return Block(
            "tool",
            params,
            call_id=event.call_id,
            tool_name=event.tool_name,
            tool_target=_tool_target(event.tool_name, event.params),
            tool_status="running",
        )
    if isinstance(event, ToolFinished):
        preview = _short(event.output_preview or event.error or "", _OUTPUT_PREVIEW)
        return Block(
            "tool",
            preview,
            call_id=event.call_id,
            tool_name=event.tool_name,
            tool_status="ok" if event.success else "failed",
        )
    if isinstance(event, ToolChunk):
        return Block(
            "tool-chunk", f"│ {_short(event.text, _OUTPUT_PREVIEW)}", "dim", call_id=event.call_id
        )
    if isinstance(event, ToolSurfaceWarning):
        # R7e-3c parity: the old console showed the number, the threshold and
        # the top origins so the operator knows which server to trim.
        origins = " · ".join(f"{name}={chars}" for name, chars in event.origins[:3])
        suffix = f"  {origins}" if origins else ""
        return Block(
            "notice",
            f"[tools] {event.tools_chars} 字符（阈值 {event.threshold}）{suffix}",
            "dim",
        )
    if isinstance(event, TaskUpdated):
        return Block("task", event.task.render(), "dim")
    if isinstance(event, TurnFinished) and event.outcome.status is TurnStatus.TIMEOUT:
        return Block("turn-end", f"[turn] timed out: {event.outcome.reason}", "red")
    return None


class TranscriptObserver:
    """LoopObserver → blocks.  The chunk throttle lives here (R8c)."""

    def __init__(
        self,
        append: Callable[[Block], None],
        *,
        clock: Callable[[], float] | None = None,
        chunks: bool = True,
    ) -> None:
        self._append = append
        self._clock = clock or time.monotonic
        self._last_chunk: dict[str, float] = {}
        self._chunks = bool(chunks)

    def on_event(self, event: LoopEvent) -> None:
        if isinstance(event, ToolChunk):
            if not self._chunks:
                return
            now = self._clock()
            previous = self._last_chunk.get(event.call_id)
            if previous is not None and now - previous < _CHUNK_THROTTLE_SECONDS:
                return
            self._last_chunk[event.call_id] = now
        block = blocks_from_event(event)
        if block is not None:
            self._append(block)
