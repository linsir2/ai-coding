"""Rich console wrapper for the REPL (M5-2).

Thin, mostly-styling layer on top of ``rich.console.Console`` so the interactive
loop and its components can share consistent coloring without coupling to
``typer.echo``.  ``record=True`` consoles can be inspected in tests.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable

from rich.console import Console

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

_ANSWER_STYLE = "cyan"
_INFO_STYLE = "green"
_ERROR_STYLE = "bold red"
_PARAM_PREVIEW = 100
_OUTPUT_PREVIEW = 120
#: R8c: at most one streamed line per call in this window.
_CHUNK_THROTTLE_SECONDS = 0.15


def _short(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


class DialogueConsole:
    """Render REPL output (assistant deltas, info, errors) with rich."""

    def __init__(self, console: Console | None = None) -> None:
        self._c = console or Console()

    def delta(self, token: str) -> None:
        """Emit one assistant output token (no trailing newline)."""
        self._c.print(token, end="", style=_ANSWER_STYLE)

    def info(self, message: str) -> None:
        self._c.print(message, style=_INFO_STYLE)

    def error(self, message: str) -> None:
        self._c.print(message, style=_ERROR_STYLE)

    def rule(self, title: str = "") -> None:
        self._c.rule(title)


class ConsoleObserver:
    """Renders loop events as compact CLI lines (implements ``LoopObserver``).

    ``AssistantTextDelta`` is deliberately not rendered: the turn's ``on_delta``
    sink already streams the text, and printing it twice would duplicate output.
    """

    def __init__(
        self,
        console: DialogueConsole,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._c = console
        # Injectable so the throttle can be tested without sleeping.
        self._clock = clock or time.monotonic
        self._last_chunk: dict[str, float] = {}

    def on_event(self, event: LoopEvent) -> None:
        if isinstance(event, IterationStarted):
            self._c.info(f"[turn {event.index}/{event.max_iterations}]")
        elif isinstance(event, ToolStarted):
            params = _short(json.dumps(event.params, ensure_ascii=False), _PARAM_PREVIEW)
            self._c.info(f"[tool: {event.tool_name}] {params}")
        elif isinstance(event, ToolFinished):
            mark = "ok" if event.success else "failed"
            preview = _short(event.output_preview, _OUTPUT_PREVIEW)
            self._c.info(f"  -> {mark}{': ' + preview if preview else ''}")
        elif isinstance(event, ToolChunk):
            # Streamed output: one line per window, so a chatty command cannot
            # push the rest of the turn off the screen (R8c-S1).
            now = self._clock()
            previous = self._last_chunk.get(event.call_id)
            if previous is not None and now - previous < _CHUNK_THROTTLE_SECONDS:
                return
            self._last_chunk[event.call_id] = now
            self._c.info(f"  | {_short(event.text, _OUTPUT_PREVIEW)}")
        elif isinstance(event, ToolSurfaceWarning):
            origins = " · ".join(
                f"{name}={chars}" for name, chars in event.origins[:3]
            )
            self._c.info(
                f"[tools] {event.tools_chars} 字符（阈值 {event.threshold}）"
                f"{' · ' + origins if origins else ''}"
            )
        elif isinstance(event, TaskUpdated):
            self._c.info(event.task.render())
        elif isinstance(event, TurnFinished):
            # Only the abnormal endings deserve a line: the normal ones are
            # already visible as streamed prose (R2a).
            if event.outcome.status is TurnStatus.TIMEOUT:
                self._c.info(f"[turn] timed out: {event.outcome.reason}")


__all__ = ["DialogueConsole", "ConsoleObserver"]
