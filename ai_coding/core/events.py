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
from typing import Any, Protocol, TypeVar

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
    #: M1b T1b: the agent that produced this step.  Empty means "not
    #: attributed" — an event built by a test that predates the agent table.
    agent: str = ""


@dataclass(frozen=True)
class AgentSwitched:
    """The session's agent changed (M1b T1b).

    Fired at the commit point — a turn boundary, never mid-turn — so the
    live transcript and the replayed one (which reads ``agentHistory``) place
    the switch at the same seam.  ``from_agent`` is carried explicitly:
    without it a client can only guess what was in charge before.
    """

    from_agent: str
    to_agent: str
    reason: str = ""
    ts: str = ""


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
    #: Which model answered (G7).  With a fallback chain the step may not have
    #: been taken by the configured default, and usage that cannot be attributed
    #: cannot answer "did the fallback cost more".  Empty means nobody said.
    model: str = ""


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
class ContextCompacted:
    """The history was rewritten before this request (hotel audit, root cause R5).

    Emitted once per turn, on the first assembly whose history the pipeline
    actually rewrote; per-iteration sizes stay in ``ContextAssembled``.
    """

    before_messages: int = 0
    after_messages: int = 0
    before_chars: int = 0
    after_chars: int = 0


CompactionReason = str
"""Why a compaction ran: auto (budget gate), manual (/compact), overflow (provider rejection)."""


@dataclass(frozen=True)
class CompactionStarted:
    """A compaction job started (durable, replayable)."""

    reason: CompactionReason = "auto"


@dataclass(frozen=True)
class CompactionDelta:
    """One streamed chunk of the summary text (ephemeral, live only)."""

    text: str = ""


@dataclass(frozen=True)
class LayerStat:
    """One compression layer's measured effect, at the layer's own boundary.

    ``removed`` is the layer's **net** change in message count, floored at zero
    (only L1's snip and L4's summary change it).  When it removes anything it
    installs exactly one message of its own — L1's ``[snipped N messages]``
    placeholder, L4's summary note — so the net is one less than the messages
    the layer actually took out, and reading it as "messages deleted" would
    over-count by one (plan-m-e §C.1).  ``rewritten`` counts bodies the layer
    replaced while keeping the message count (L2's envelope, L3's persist).
    The two are deliberately separate: L2/L3 never remove a message, so folding
    them into ``removed`` would render a misleading number.

    ``skipped`` counts how many a *count-preserving* layer examined and left
    alone.  Only L2 and L3 do that — they scan candidates and rewrite some in
    place, so "checked but did not act" is a real number there (L3 is the case
    that matters, because a non-trailing large output is not even in the
    trailing batch and an under-threshold trailing one is examined and
    skipped).  L1 and L4 change the count instead of scanning candidates, so
    they report 0: the messages they leave alone are the head/tail they
    deliberately kept, not candidates they declined, and a nonzero ``skipped``
    on them would read as "these were skipped".

    ``before_tokens``/``after_tokens`` are measured with ``_history_tokens``
    (text plus a flat 1600 per image), on the exact list the pipeline
    transforms at that point, so a layer's token delta is what ``/trace`` and
    the compaction card report.  Order is the pipeline order
    (L3 -> L1 -> L2 -> L4), never numeric, and a layer that did not run is
    absent rather than all-zero.
    """

    name: str
    removed: int
    rewritten: int
    skipped: int
    before_tokens: int
    after_tokens: int


@dataclass(frozen=True)
class CompactionEnded:
    """A compaction job finished (durable).

    Tokens are the compaction request's own usage, not the resulting
    context size (OpenCode parity, docs/compaction-debt.md P3).  ``text`` is
    the accepted summary body for the transcript row; deltas are lossy
    (bridge throttles them), so the row cannot be reconstructed from them.

    ``layers`` is the per-layer account of the pipeline boundary (L4's exit
    when it ran, else L2's).  ``estimated_before``/``estimated_after`` are
    that boundary's ends — NOT the ``Compactor`` boundary, which also carries
    the paging loop and can differ from the layers; the two are not the same
    measure and must not be compared (plan-m-e §C.1).
    """

    reason: CompactionReason = "auto"
    before_messages: int = 0
    after_messages: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    text: str = ""
    layers: tuple[LayerStat, ...] = ()
    estimated_before: int = 0
    estimated_after: int = 0


@dataclass(frozen=True)
class CompactionFailed:
    """A compaction job failed; the turn continues on raw history (durable).

    ``unchanged`` is also a failure: the job ran and changed nothing (the
    summary was unusable or there was nothing new), so reporting Ended
    with N→N and zero usage would be a lie.  Usage is whatever streamed
    before the failure (often zeros) — same accounting rule as Ended.
    """

    reason: CompactionReason = "auto"
    error: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True)
class CompactionSkipped:
    """A compaction was requested but there was nothing worth doing (durable).

    Not a failure: the pipeline concluded there was nothing to summarise
    (nothing new since the checkpoint, or a single exchange), so no model
    call was made.  The transcript renders this muted, not red.
    """

    reason: CompactionReason = "auto"


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
class ProviderRetried:
    """A transient model call failed and is being tried again (M1).

    Not a ``Degraded``: that event means an enhancement *gave up and fell back*,
    and a retry that succeeds gave up nothing.  Folding the two together would
    also corrupt ``/trace``'s degraded count and the c9 acceptance case.

    This exists because the engine layer has no other way to speak.  With four
    attempts against a 60s per-request timeout the worst case is 254s of a turn
    in which nothing has been said; the status bar's ``● Working`` says *that*
    something is happening, and this says *why it is slow*.
    """

    kind: str
    attempt: int
    delay_seconds: float
    reason: str = ""


@dataclass(frozen=True)
class ProviderFallback:
    """One model gave up and the turn is now being answered by another (M2).

    Not a ``Degraded``, and deliberately not folded into ``ProviderRetried``:
    those are the same model trying itself again, this is a *different* model
    taking the turn.  ``Degraded`` is also wrong because it means an
    enhancement abandoned a nicety -- ``/trace`` counts those, and a model
    switch is not something the operator should have to filter out of a count
    to find it.

    ``reason`` is the underlying failure verbatim.  It can name a bad key, so the
    trace is where it belongs; the transcript truncates it when rendering.
    """

    from_model: str
    to_model: str
    kind: str
    reason: str = ""


@dataclass(frozen=True)
class ToolSurfaceWarning:
    """The MCP part of the tool surface costs more than the configured ceiling.

    R7e-3, recalibrated by r22.  Advisory and write-only: it is emitted once per
    loop, it is never assembled into a request, and the request is byte-identical
    whether or not anyone listens.

    ``tools_chars`` is the whole surface and ``mcp_chars`` the part the warning
    actually judged -- the built-in tools are not something the user can shrink,
    so only the MCP servers are worth telling them about, and the total stays on
    the event so the diagnosis is complete.  ``origins`` is
    ``((origin, chars), ...)`` biggest-first, so the reader can see which server
    to trim instead of one number nobody can act on.
    """

    tools_chars: int
    threshold: int
    origins: tuple[tuple[str, int], ...] = ()
    hint: str = ""
    mcp_chars: int = 0


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
class PlanStepReceipt:
    """One plan step's receipt, on the wire (E2 / D6).

    The runner writes the step head (the replay marker, stamped synthetic) and
    the one-line summary to the parent session.  The marker is bookkeeping: a
    later ``/resume`` uses it to rebuild the step timeline, and replay skips it
    so it cannot read as the operator's own words.  The summary is the one line
    a human reads, so it is the only thing this event carries.
    """

    summary: str


@dataclass(frozen=True)
class ResearchConcluded:
    """A plan-mode turn closed with a research conclusion (23).

    The conclusion is the *deliverable* of the research exit, and a tool
    result is the one channel that is lossy by design: the event carries 500
    chars, the UI draws 120, and ``finish_research`` streams no chunks, so
    the card cannot even be opened.  Measured on a real session, a 4116-char
    conclusion reached the user as 120 chars.

    So the conclusion gets its own channel, whole -- the shape
    ``PlanStepReceipt`` has for a plan step's summary.  The tool output stays
    what it is (the model's own history needs the conclusion there); this
    event is what the *human* reads.

    No turn id rides along: this is a content event, not a terminal one (the
    turn still ends with its own ``turn.finished``), and the bridge's turn id
    is only meaningful for the turns it launches -- a command's inner turn has
    none.  ``PlanStepReceipt`` and ``AssistantMessage`` are the same shape of
    event, and neither carries one.
    """

    summary: str


@dataclass(frozen=True)
class TaskUpdated:
    """The task list changed (emitted after a todo_write call)."""

    task: TaskState


@dataclass(frozen=True)
class JobChanged:
    """The workspace's background jobs, as they are right now (D-1b).

    A snapshot rather than a delta: the panel is three rows and every consumer
    of a delta would end up rebuilding it anyway.  Emitted on start, settlement
    and discard, so a client that connects later asks ``session.jobs`` once and
    then follows this.
    """

    jobs: tuple[dict[str, Any], ...] = ()


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
class SearchStalled:
    """The search ladder fired (B2/B5): look-only calls are piling up.

    The trace could not answer "how many looks did this turn spend before anyone
    noticed it was not acting": the nudge is a synthetic message, and a message
    says what was injected, never why.  One event per rung, so a post-mortem can
    read the climb off the jsonl.
    """

    iteration: int
    level: int
    calls: int
    streak: int
    #: I1/R-7 (all defaulted so old traces still read): how many looks returned
    #: evidence already seen, how many were new, the non-empty repeats among the
    #: stale ones, whether the turn was *forced* to close, and the gate's reason.
    stale_streak: int = 0
    novel: int = 0
    repeated: int = 0
    unconverged: bool = False
    reason: str = ""


@dataclass(frozen=True)
class ResumeStalled:
    """A boot resume made no progress twice in a row, so the task was disarmed (M-C3).

    ``resume_count`` alone cannot tell a resume that did nothing from one that
    did: it increments on every boot.  The fingerprint compares boot to boot --
    rounds, tokens, real (non-synthetic) messages and settled steps -- and an
    unchanged one means the resume produced none of them.  Two in a row ends the
    episode instead of flapping.

    ``before``/``after`` are the fingerprint the previous resume left and the one
    that failed to move; ``no_progress`` is the consecutive count that tripped
    the gate.  The operator-facing summary rides the ``initialize`` response's
    ``resumeNote`` -- this event is the structured record (the trace's
    ``dataclasses.asdict`` writes it).
    """

    resumes: int = 0
    no_progress: int = 0
    before: tuple[int, ...] = ()
    after: tuple[int, ...] = ()


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
    | AgentSwitched
    | ToolApprovalRequested
    | ToolStarted
    | ToolFinished
    | ToolChunk
    | ModelStepFinished
    | ContextAssembled
    | ContextCompacted
    | CompactionStarted
    | CompactionDelta
    | CompactionEnded
    | CompactionFailed
    | CompactionSkipped
    | Degraded
    | ToolSurfaceWarning
    | ProviderRetried
    | ProviderFallback
    | InputSteered
    | ScopedEvent
    | TaskUpdated
    | BackgroundTaskDone
    | PlanStepReceipt
    | ResearchConcluded
    | JobChanged
    | LedgerUpdated
    | TurnFinished
    | MemoryInjected
    | MemoryRecalled
    | MemoryHarvested
    | MemoryConsolidated
    | SearchStalled
    | ResumeStalled
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
        return [e for e in self.events if isinstance(e, event_type)]


def preview(text: str, limit: int = OUTPUT_PREVIEW_CHARS) -> str:
    """Truncate ``text`` for event payloads, marking the cut with an ellipsis."""
    if limit < 0:
        limit = 0
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


__all__ = [
    "OUTPUT_PREVIEW_CHARS",
    "CompactionReason",
    "LayerStat",
    "CompactionStarted",
    "CompactionDelta",
    "CompactionEnded",
    "CompactionFailed",
    "CompactionSkipped",
    "ContextAssembled",
    "ContextCompacted",
    "Degraded",
    "ProviderFallback",
    "ProviderRetried",
    "IterationStarted",
    "AssistantTextDelta",
    "AssistantMessage",
    "AgentSwitched",
    "ModelStepFinished",
    "ScopedEvent",
    "ToolApprovalRequested",
    "ToolStarted",
    "ToolFinished",
    "ToolSurfaceWarning",
    "InputSteered",
    "TaskUpdated",
    "BackgroundTaskDone",
    "PlanStepReceipt",
    "ResearchConcluded",
    "JobChanged",
    "TurnFinished",
    "MemoryInjected",
    "MemoryRecalled",
    "MemoryHarvested",
    "MemoryConsolidated",
    "SearchStalled",
    "ResumeStalled",
    "LoopEvent",
    "LoopObserver",
    "NullObserver",
    "RecordingObserver",
    "preview",
]
