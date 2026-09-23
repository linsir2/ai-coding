"""Loop events — the orchestration loop's outward channel (R1a-3).

The loop reports what it is doing through frozen event values, so the CLI, a
future tracing layer, or a test can all observe the same stream without the loop
knowing anything about them.

Events are deliberately *values*, not callbacks with state: they record things
that already happened, so they are immutable.  ``preview`` exists because a
single ``read`` can return 10MB — events must never carry that into a UI or a
log line.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, TypeVar, cast

from ai_coding.domain.loop import TurnOutcome
from ai_coding.domain.message import ToolCallRef
from ai_coding.domain.task import TaskState

OUTPUT_PREVIEW_CHARS = 500


@dataclass(frozen=True)
class IterationStarted:
    """A new model step is beginning."""

    index: int
    max_iterations: int


@dataclass(frozen=True)
class AssistantTextDelta:
    """One streamed chunk of assistant prose."""

    text: str


@dataclass(frozen=True)
class AssistantMessage:
    """A completed assistant step (its text and the tools it asked for)."""

    text: str | None
    tool_calls: tuple[ToolCallRef, ...] = ()


@dataclass(frozen=True)
class ToolApprovalRequested:
    """A WARN-tier call is about to prompt the user."""

    tool_name: str
    params: dict[str, Any]
    reason: str = ""


@dataclass(frozen=True)
class ToolStarted:
    """The runtime began handling this call (not necessarily executing it)."""

    call_id: str
    tool_name: str
    params: dict[str, Any]


@dataclass(frozen=True)
class ToolFinished:
    """Outcome of one call, including the ones that were denied or skipped."""

    call_id: str
    tool_name: str
    success: bool
    output_preview: str = ""
    duration_ms: int = 0
    # Machine-readable failure code (R2a): "tool_timeout", "denied", "unknown_tool",
    # "not_approved", "stopped", "skipped", "tool_error"… None means success.
    error: str | None = None


@dataclass(frozen=True)
class TaskUpdated:
    """The task list changed (emitted after a todo_write call)."""

    task: TaskState


@dataclass(frozen=True)
class TurnFinished:
    """The turn ended, for better or worse."""

    outcome: TurnOutcome


@dataclass(frozen=True)
class MemoryInjected:
    """The volatile constraint block was attached to this request (R3b).

    Memory never enters the system prompt: it is a tail block, so a change costs
    only that block's cache, not the whole conversation (docs/r3-analysis.md 1.13).
    """

    note_ids: tuple[str, ...]
    chars: int


@dataclass(frozen=True)
class MemoryRecalled:
    """A ``memory_search`` tool call answered with these notes (R3b)."""

    note_ids: tuple[str, ...]
    scores: tuple[float, ...]
    chars: int


@dataclass(frozen=True)
class MemoryHarvested:
    """What the end-of-turn harvest wrote back (R3b)."""

    added: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    superseded: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()


@dataclass(frozen=True)
class MemoryConsolidated:
    """What an index consolidation changed (R3c)."""

    merged: tuple[str, ...] = ()
    promoted: tuple[str, ...] = ()
    expired: tuple[str, ...] = ()
    evicted: tuple[str, ...] = ()


LoopEvent = (
    IterationStarted
    | AssistantTextDelta
    | AssistantMessage
    | ToolApprovalRequested
    | ToolStarted
    | ToolFinished
    | TaskUpdated
    | TurnFinished
    | MemoryInjected
    | MemoryRecalled
    | MemoryHarvested
    | MemoryConsolidated
)


class LoopObserver(Protocol):
    """Anything that wants to watch a turn.  Sync — observers must not block."""

    def on_event(self, event: LoopEvent) -> None: ...


class NullObserver:
    """Observer that discards everything (the default when none is supplied)."""

    def on_event(self, event: LoopEvent) -> None:
        return None


_T = TypeVar("_T")


class RecordingObserver:
    """Collects events for tests and for post-hoc inspection."""

    def __init__(self) -> None:
        self.events: list[LoopEvent] = []

    def on_event(self, event: LoopEvent) -> None:
        self.events.append(event)

    def of_type(self, event_type: type[_T]) -> list[_T]:
        """All recorded events of one type, in arrival order."""
        return [cast(_T, e) for e in self.events if isinstance(e, event_type)]


def preview(text: str, limit: int = OUTPUT_PREVIEW_CHARS) -> str:
    """Truncate ``text`` for event payloads, marking the cut with an ellipsis."""
    if limit < 0:
        limit = 0
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


__all__ = [
    "OUTPUT_PREVIEW_CHARS",
    "IterationStarted",
    "AssistantTextDelta",
    "AssistantMessage",
    "ToolApprovalRequested",
    "ToolStarted",
    "ToolFinished",
    "TaskUpdated",
    "TurnFinished",
    "MemoryInjected",
    "MemoryRecalled",
    "MemoryHarvested",
    "MemoryConsolidated",
    "LoopEvent",
    "LoopObserver",
    "NullObserver",
    "RecordingObserver",
    "preview",
]
