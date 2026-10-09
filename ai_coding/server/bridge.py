"""Loop events -> wire notifications (M2).

The transport between ``AgentLoop`` and the TUI is one JSON-RPC notification:
``event`` with ``{"type": str, "data": dict}``.  This module owns the mapping,
so the loop keeps knowing nothing about the front end and the front end gets a
stable, camelCase contract instead of Python dataclass shapes.

Two channels, because the loop has two:

* ``on_delta`` — streamed prose, a plain ``Callable[[str], None]`` on the loop;
* ``on_event`` — everything else, a ``LoopObserver`` (per-call observers are
  braided with the trace recorder by ``AgentLoop._bind_observer``, so passing
  this bridge as the observer loses nothing).

What maps: the conversation transcript set the old Textual UI rendered
(``ui/blocks.py``) plus four deliberate additions (handoff, approval notice,
turn ERROR marker, compaction notice — see docs/opencode-gui-m2-plan.md §1).
What does not: telemetry (assembly, model steps, iteration counters), memory
bookkeeping and ``Degraded`` — the ``/trace`` dialog (M3) owns those.  Dropping
them here keeps the wire small and the reducer honest.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from ai_coding.core.events import (
    AssistantMessage,
    BackgroundTaskDone,
    ContextCompacted,
    LedgerUpdated,
    LoopEvent,
    ProviderFallback,
    ProviderRetried,
    ScopedEvent,
    TaskUpdated,
    ToolApprovalRequested,
    ToolChunk,
    ToolFinished,
    ToolStarted,
    ToolSurfaceWarning,
    TurnFinished,
)

__all__ = ["CHUNK_THROTTLE_SECONDS", "EventBridge", "to_wire"]

#: One running tool can emit chunks per line; the UI tails them, so 150ms is
#: the cadence the old transcript used (ui/blocks.py TranscriptObserver).
CHUNK_THROTTLE_SECONDS = 0.15

Notify = Callable[[str, dict[str, Any]], None]


def _scoped_inner(event: ScopedEvent) -> dict[str, Any] | None:
    """One dim timeline line for delegated work, same three kinds as blocks.py.

    The child's reasoning stays inside the child (R1a-5); only its calls and
    what it said travel up.  Anything finer-grained belongs to the trace.
    """
    inner = event.event
    if isinstance(inner, ToolStarted):
        return {
            "kind": "tool",
            "phase": "started",
            "toolName": inner.tool_name,
            "params": inner.params,
        }
    if isinstance(inner, ToolFinished):
        return {
            "kind": "tool",
            "phase": "finished",
            "toolName": inner.tool_name,
            "success": inner.success,
            "outputPreview": inner.output_preview,
            "error": inner.error,
        }
    if isinstance(inner, AssistantMessage):
        if not inner.text:
            return None
        return {"kind": "text", "text": inner.text}
    return None


def to_wire(event: LoopEvent) -> dict[str, Any] | None:
    """Map one loop event to its wire message, or ``None`` if it is not shown.

    Pure: no state, no clock — every field decision here is pinned by
    ``tests/test_server_bridge.py`` and mirrored by the TS reducer.
    """
    if isinstance(event, AssistantMessage):
        # Prose also travels the delta stream; this event finalises it with the
        # authoritative text and carries the step's thinking (blocked from the
        # model, shown to the human).
        return {
            "type": "message.finished",
            "data": {"text": event.text, "reasoning": event.reasoning},
        }
    if isinstance(event, ToolStarted):
        return {
            "type": "tool.started",
            "data": {
                "callId": event.call_id,
                "toolName": event.tool_name,
                "params": event.params,
                "iteration": event.iteration,
            },
        }
    if isinstance(event, ToolFinished):
        return {
            "type": "tool.finished",
            "data": {
                "callId": event.call_id,
                "toolName": event.tool_name,
                "success": event.success,
                "outputPreview": event.output_preview,
                "durationMs": event.duration_ms,
                "error": event.error,
                "iteration": event.iteration,
            },
        }
    if isinstance(event, ToolChunk):
        return {
            "type": "tool.chunk",
            "data": {
                "callId": event.call_id,
                "stream": event.stream,
                "text": event.text,
                "seq": event.seq,
            },
        }
    if isinstance(event, ToolApprovalRequested):
        # M2 runs AutoApproveCallback; the notice keeps the security gate
        # visible.  M5 replaces it with the interactive panel.
        return {
            "type": "approval.requested",
            "data": {"toolName": event.tool_name, "params": event.params, "reason": event.reason},
        }
    if isinstance(event, TaskUpdated):
        task = event.task
        return {
            "type": "plan.updated",
            "data": {
                "display": task.render_display(),
                "done": task.done_count,
                "total": task.total,
            },
        }
    if isinstance(event, LedgerUpdated):
        return {
            "type": "ledger.updated",
            "data": {
                "items": [
                    {
                        "level": item.level,
                        "question": item.question,
                        "evidence": list(item.evidence),
                    }
                    for item in event.items
                ]
            },
        }
    if isinstance(event, TurnFinished):
        outcome = event.outcome
        return {
            "type": "turn.finished",
            "data": {
                "status": outcome.status.value,
                "reason": outcome.reason,
                "iterations": outcome.iterations,
                "truncatedByLength": outcome.truncated_by_length,
                "handoff": outcome.handoff,
            },
        }
    if isinstance(event, ProviderRetried):
        return {
            "type": "provider.retried",
            "data": {
                "kind": event.kind,
                "attempt": event.attempt,
                "delaySeconds": event.delay_seconds,
                "reason": event.reason,
            },
        }
    if isinstance(event, ProviderFallback):
        return {
            "type": "provider.fallback",
            "data": {
                "fromModel": event.from_model,
                "toModel": event.to_model,
                "kind": event.kind,
                "reason": event.reason,
            },
        }
    if isinstance(event, ToolSurfaceWarning):
        return {
            "type": "tools.warning",
            "data": {
                "toolsChars": event.tools_chars,
                "threshold": event.threshold,
                "origins": [list(pair) for pair in event.origins],
            },
        }
    if isinstance(event, BackgroundTaskDone):
        return {
            "type": "background.done",
            "data": {
                "runId": event.run_id,
                "name": event.name,
                "status": event.status,
                "summary": event.summary,
                "truncated": event.truncated,
                "originSession": event.origin_session,
                "ledgerPath": event.ledger_path,
            },
        }
    if isinstance(event, ContextCompacted):
        return {
            "type": "context.compacted",
            "data": {
                "beforeMessages": event.before_messages,
                "afterMessages": event.after_messages,
            },
        }
    if isinstance(event, ScopedEvent):
        inner = _scoped_inner(event)
        if inner is None:
            return None
        return {"type": "subagent.event", "data": {"scope": event.scope, "inner": inner}}
    return None


class EventBridge:
    """``LoopObserver`` + delta sink that push wire events to one notify call.

    Stateful only for the chunk throttle: a running ``bash`` can emit thousands
    of fragments, and the UI wants the tail, not the firehose.  The clock is
    injectable so the throttle is testable without sleeping.
    """

    def __init__(self, notify: Notify, *, clock: Callable[[], float] | None = None) -> None:
        self._notify = notify
        self._clock = clock or time.monotonic
        self._last_chunk: dict[str, float] = {}

    def on_delta(self, text: str) -> None:
        self._notify("event", {"type": "text.delta", "data": {"text": text}})

    def on_event(self, event: LoopEvent) -> None:
        if isinstance(event, ToolChunk):
            now = self._clock()
            previous = self._last_chunk.get(event.call_id)
            if previous is not None and now - previous < CHUNK_THROTTLE_SECONDS:
                return
            self._last_chunk[event.call_id] = now
        message = to_wire(event)
        if message is not None:
            self._notify("event", message)
