"""Body observer: writes loop events to BodyBuffer."""

from __future__ import annotations

import json
import time
from collections.abc import Callable

from ai_coding.core.events import (
    IterationStarted,
    LoopEvent,
    ToolChunk,
    ToolFinished,
    ToolStarted,
    TurnFinished,
)
from ai_coding.domain.loop import TurnStatus
from ai_coding.ui.body import BodyBuffer

_PARAM_PREVIEW = 100
_OUTPUT_PREVIEW = 120
_CHUNK_THROTTLE_SECONDS = 0.15


def _short(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


class BodyObserver:
    """Renders loop events into a BodyBuffer (implements LoopObserver)."""

    def __init__(
        self,
        body: BodyBuffer,
        *,
        clock: Callable[[], float] | None = None,
        chunks: bool = True,
    ) -> None:
        self._body = body
        self._clock = clock or time.monotonic
        self._last_chunk: dict[str, float] = {}
        self._chunks = chunks

    def on_event(self, event: LoopEvent) -> None:
        if isinstance(event, IterationStarted):
            self._body.write(f"[turn {event.index}/{event.max_iterations}]\n")
        elif isinstance(event, ToolStarted):
            params = _short(json.dumps(event.params, ensure_ascii=False), _PARAM_PREVIEW)
            self._body.write(f"[tool: {event.tool_name}] {params}\n")
        elif isinstance(event, ToolFinished):
            mark = "ok" if event.success else "failed"
            preview = _short(event.output_preview, _OUTPUT_PREVIEW)
            self._body.write(f"  -> {mark}{': ' + preview if preview else ''}\n")
        elif isinstance(event, ToolChunk):
            if not self._chunks:
                return
            now = self._clock()
            previous = self._last_chunk.get(event.call_id)
            if previous is not None and now - previous < _CHUNK_THROTTLE_SECONDS:
                return
            self._last_chunk[event.call_id] = now
            self._body.write(f"  | {_short(event.text, _OUTPUT_PREVIEW)}\n")
        elif isinstance(event, TurnFinished):
            if event.outcome.status is TurnStatus.TIMEOUT:
                self._body.write(f"[turn] timed out: {event.outcome.reason}\n")
