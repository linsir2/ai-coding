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

from ai_coding.domain.ledger import UncertaintyItem
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
    #: The step's thinking, if the endpoint produced any (R1-4).  Travels so
    #: the transcript can show it; never re-fed to the model (the request
    #: mapping ignores it — pinned by test).
    reasoning: str | None = None


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
    # R4a: which iteration asked for it.  Without this a concurrent batch cannot
    # be reconstructed from the stream (docs/r4-analysis.md §3.3).
    iteration: int = 0


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
    # R4a: the iteration this call belonged to (see ``ToolStarted``).
    iteration: int = 0


# ---- R8c: streamed tool output --------------------------------------------


@dataclass(frozen=True)
class ToolChunk:
    """One streamed fragment of a running tool (R8c).

    Observation only: chunks never enter a request, the history or the session
    file - the model still only sees the final ``ToolResult``, so a 10MB command
    cannot grow the context (docs/r8-analysis.md 6.4).  ``stream`` is "stdout",
    "stderr" or "progress"; ``seq`` orders the fragments of one call.
    """

    call_id: str
    tool_name: str
    stream: str
    text: str
    seq: int = 0


# ---- R4a: the observability events ----------------------------------------


@dataclass(frozen=True)
class ModelStepFinished:
    """One model step is over: what it cost and how long it took (R4a).

    ``usage`` is whatever the endpoint reported, untouched.  ``cached_tokens``
    is the provider's own prompt-cache figure — the truthful number — while
    ``ContextAssembled.prefix_shared_ratio`` is only our estimate
    (docs/r4-analysis.md §4.4).
    """

    index: int
    finish_reason: str | None = None
    text_chars: int = 0
    tool_call_count: int = 0
    usage: dict[str, int] | None = None
    cached_tokens: int | None = None
    cache_write_tokens: int | None = None
    latency_ms: int = 0


@dataclass(frozen=True)
class ContextAssembled:
    """How this request was assembled — counts only, never content (R4a).

    This is what makes "where did the tokens go" answerable: the system prompt,
    the tool schemas, the history and each volatile tail block are measured
    separately, and the shared prefix with the previous request is recorded so a
    cache regression is visible without a separate experiment.

    ``tools_chars`` matters: tool schemas are part of the prompt, so leaving them
    out would make ``estimated_tokens`` systematically low (§4.3).
    """

    iteration: int
    system_chars: int
    tools_chars: int = 0
    history_messages: int = 0
    history_chars: int = 0
    tail_blocks: tuple[tuple[str, int], ...] = ()
    estimated_tokens: int = 0
    prefix_shared_chars: int = 0
    prefix_shared_ratio: float = 0.0
    digest: str = ""


@dataclass(frozen=True)
class Degraded:
    """An enhancement gave up and fell back, instead of failing the turn (R4a).

    The architecture already degrades silently on purpose; this event makes each
    of those fallbacks visible (docs/r4-analysis.md §1.1).
    """

    component: str
    reason: str = ""
    fallback: str = ""


@dataclass(frozen=True)
class ToolSurfaceWarning:
    """The serialised tool schemas cost more than the configured ceiling (R7e-3).

    Advisory and write-only: it is emitted once per loop, it is never assembled into
    a request, and the request is byte-identical whether or not anyone listens.
    ``origins`` is ``((origin, chars), ...)`` biggest-first, so the reader can see
    which MCP server to trim instead of one number nobody can act on.
    """

    tools_chars: int
    threshold: int
    origins: tuple[tuple[str, int], ...] = ()
    hint: str = ""


@dataclass(frozen=True)
class InputSteered:
    """The user typed while the turn was running, and it became an instruction (R7e-2).

    The line is persisted as a ``[steering] …`` user message (Q4) for the same
    reason the task gate is: it changed what the model did, so replaying the
    transcript without it would not reproduce the turn.
    """

    text_chars: int
    iteration: int = 0
    total: int = 1


@dataclass(frozen=True)
class ScopedEvent:
    """An event that happened inside a nested run (a sub-agent), R4a.

    The inner loop gets a :class:`~ai_coding.core.observers.ScopedObserver`, so
    its events reach the parent's observer carrying who they belong to.  The
    sub-agent's *reasoning* still never travels back (R1a-5); only these facts do.
    """

    scope: str
    parent: str
    event: LoopEvent


@dataclass(frozen=True)
class TaskUpdated:
    """The task list changed (emitted after a todo_write call)."""

    task: TaskState


@dataclass(frozen=True)
class BackgroundTaskDone:
    """A background sub-agent finished, failed, timed out or was cancelled.

    Emitted on the captured parent emit when the child settles — possibly
    turns after the spawning turn ended, which is exactly the point (R1-2).
    The summary is capped at build time; the full packet lives in the ledger
    file (``transcripts/subagents/``), same as synchronous runs.
    """

    run_id: str
    name: str = "sub-agent"
    status: str = "completed"  # completed | timeout | error | cancelled
    summary: str = ""
    truncated: bool = False
    #: Short id of the session that spawned the run — the announce may land
    #: in a different (newer) view, and the reader deserves to know whose.
    origin_session: str = ""
    #: Pointer to the full packet (``transcripts/subagents/*.json``, facts
    #: included) — the summary above is capped, this is how the reader (or a
    #: later turn) retrieves the whole report.  ``None`` when no ledger root
    #: was configured or the write failed (best-effort).
    ledger_path: str | None = None


@dataclass(frozen=True)
class LedgerUpdated:
    """The turn's open unknowns moved (R0-2).

    Emitted only when the derived open set changes signature — never every
    round — so the transcript shows a question once, not as a heartbeat.
    Items are full values (id, question, level, evidence tails); the 10MB
    rule still holds because evidence entries are capped at build time.
    """

    items: tuple[UncertaintyItem, ...] = ()


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
    # `HarvestReport.evicted` was already filled — this is how a note leaves
    # `.memory`, so a turn that only evicted used to leave no trace at all.
    evicted: tuple[str, ...] = ()
    # R6d: the turn's provenance for those notes — "tool" when at least one tool
    # call succeeded in this turn, "model" when it only talked.
    evidence: str = ""


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
    | ToolChunk
    | ModelStepFinished
    | ContextAssembled
    | Degraded
    | ToolSurfaceWarning
    | InputSteered
    | ScopedEvent
    | TaskUpdated
    | BackgroundTaskDone
    | LedgerUpdated
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
    "ContextAssembled",
    "Degraded",
    "IterationStarted",
    "AssistantTextDelta",
    "AssistantMessage",
    "ModelStepFinished",
    "ScopedEvent",
    "ToolApprovalRequested",
    "ToolStarted",
    "ToolFinished",
    "ToolSurfaceWarning",
    "InputSteered",
    "TaskUpdated",
    "BackgroundTaskDone",
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
