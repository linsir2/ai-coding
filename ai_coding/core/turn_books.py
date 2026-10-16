"""Everything one turn counts about itself (R7a).

The loop's body used to carry nine loose locals — ``iterations``, ``nudges``,
``attempted_calls``, ``successful_calls``, ``failed_streak``, ``strategy_nudges``,
``failures``, ``last_text`` and ``summaries`` — spread across a 228-line
``process_input``.  R6 added four of them (the escalation ladder needs to know how
the tool calls are going) and that is exactly why the method grew back after R5
had split it: every new behaviour had to reach into the same local scope.

Two values shape a turn, and they are deliberately different things:

* :class:`TurnFrame` is what the **prologue fixed** — the turn number, the task
  list, what ``turn.start`` hooks injected, the turn clock.  Nothing in an
  iteration may change it;
* :class:`TurnBooks` is what the **iterations counted** — the nine counters.

Both are pure: no IO, no asyncio, no events.  The loop notifies the books
(``note_*``) and asks them for the one object a decision needs (``snapshot``); the
rules stay table-testable in ``tests/test_turn_books.py``.

The accounting rules are the ones R6 measured and argued for, moved verbatim:

* an **empty batch** (a plan-only step) is not a failure streak;
* a batch with **anything successful** resets the streak;
* a failure is recorded as ``(tool, error-or-"failed", first 160 chars of output)``
  — the material ``render_handoff`` quotes back to the user.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from ai_coding.core.dispatch import DispatchOutcome
from ai_coding.core.turn_policy import TurnSnapshot
from ai_coding.domain.acceptance import AcceptanceState
from ai_coding.domain.ledger import UncertaintyItem, build_ledger
from ai_coding.domain.message import CANCELLED_BY_USER
from ai_coding.domain.task import TaskState, TodoItem
from ai_coding.domain.turn_digest import DEFAULT_NOTE_CHARS, one_line
from ai_coding.infra.deadline import Deadline

__all__ = ["STATE_ONLY_TOOLS", "Failure", "TurnBooks", "TurnFrame"]

#: One failed call, as the handoff reports it: (tool, error, output excerpt).
Failure = tuple[str, str, str]

#: Tools whose *success* is not evidence about the world (R8a, D-14): writing a
#: checkpoint tells the turn about itself, so a turn that only did that is still
#: a "model" turn as far as the harvest's provenance is concerned.
STATE_ONLY_TOOLS = frozenset({"checkpoint_write"})

#: Errors that are verdicts rather than breakage (R8c D-11 cancel; 20 §批次3
#: plan-validation refusals).  A round whose calls all carry these is not
#: evidence of failure: it neither records a failure nor keeps the burn --
#: the strategy window's helped-signal is ``failed_streak == 0``, so an
#: all-neutral round must reset (24d7109b: a parked ``no_plan_items`` refusal
#: kept the L1 window "failed" and L2 spent its nudge on a duplicate report).
_NOT_EVIDENCE_ERRORS = frozenset({CANCELLED_BY_USER, "no_plan_items", "missing_plan"})

# F2: Paris' Law fatigue tracking — accumulated error damage.
_FATIGUE_C = 1.0
_FATIGUE_M = 2.0

_SEVERITY_KEYWORDS: dict[int, tuple[str, ...]] = {
    5: ("timeout", "context overflow", "out of memory", "token limit"),
    4: ("rate limit", "too many requests", "quota exceeded"),
    3: ("permission denied", "access forbidden", "unauthorized"),
    2: ("not found", "invalid", "malformed", "bad request", "error", "failed"),
}

_FAILURE_EXCERPT = 160


@dataclass(frozen=True)
class TurnFrame:
    """What one turn is, before its first iteration (R7a)."""

    turn_number: int
    task: TaskState
    #: What ``turn.start`` hooks injected (a volatile tail block; never persisted).
    hook_context: str = ""
    #: The turn clock, or ``None`` when the configuration sets no ceiling.
    deadline: Deadline | None = None


@dataclass
class TurnBooks:
    """The observable state of the turn so far."""

    #: The last iteration index the loop started (0 = nothing started yet).
    iterations: int = 0
    #: Every model step this turn made, across C3 continuation rounds.  The
    #: outcome and handoff report this one; ``iterations`` is round-local.
    total_iterations: int = 0
    #: Task-gate nudges injected (``[task-gate]``) — the R2 stop policy's budget.
    nudges: int = 0
    #: Tool calls asked for this turn, successful or not.
    attempted_calls: int = 0
    #: Tool calls that came back with ``success=True``.
    successful_calls: int = 0
    #: Consecutive iterations in which every attempted call failed (R6b tier 2).
    failed_streak: int = 0
    #: Strategy nudges injected (``[strategy-gate]``).
    strategy_nudges: int = 0
    #: stage 4 r2: the plan submission guard spent its one remedy nudge.
    #: ``plan_submitted`` itself needs no field -- ``tool_success`` already
    #: remembers that present_plan succeeded.
    plan_nudged: bool = False
    #: What failed, newest last — the handoff names the most recent one.
    failures: list[Failure] = field(default_factory=list)
    #: The latest thing the model said (``None`` when it said nothing at all).
    last_text: str | None = None
    #: Finish reason of the latest model step (``None`` before the first step).
    #: The loop reads it when closing the turn: only ``"length"`` is
    #: actionable (a bare stop is indistinguishable from done).
    last_finish_reason: str | None = None
    #: Summaries the compactor produced this turn (for the harvest to look at).
    summaries: list[str] = field(default_factory=list)
    #: Whether the tail's memory block has been reported this turn (R3 emits one
    #: ``MemoryInjected`` per turn, not one per iteration).
    memory_announced: bool = False
    #: Whether the tail's compaction event has been reported this turn (the
    #: ``ContextCompacted`` event fires once per turn, not once per iteration).
    compaction_announced: bool = False
    #: Lines the user typed while this turn was running and the loop absorbed
    #: (R7e-2).
    steering_messages: int = 0
    #: What the model asked its future self to remember about this turn (R8a).
    #: ``None`` = it never wrote one, and the harness line stands alone.
    checkpoint: str | None = None
    #: Successful calls that are evidence about the world (R8a, D-14).  It is what
    #: the harvest reads, so "the turn used tools" cannot be faked by writing a
    #: checkpoint: that is the turn talking about itself.
    evidence_calls: int = 0
    #: Acceptance verdicts this turn (R0-1): item id -> (passed, output tail).
    #: Written by the loop after running the proof commands; the policy only
    #: reads, so the stop decision stays table-testable.
    verify: dict[str, tuple[bool, str]] = field(default_factory=dict)
    #: Acceptance-gate nudges injected (``[acceptance-gate]``) — its own budget,
    #: separate from the todo and strategy ladders.
    acceptance_nudges: int = 0
    #: Per-step model latency, newest last (R0-4) — the reporter already emits
    #: each step's latency as an event; the books keep the series so the turn
    #: can account its wall-clock the way it accounts its iterations.
    step_latencies_ms: list[int] = field(default_factory=list)
    #: Model steps abandoned at the turn deadline (R0-4).  Their streamed
    #: deltas survive in the transcript; without this counter the books would
    #: claim those seconds never happened (measured: a 179s step vanishing).
    abandoned_steps: int = 0
    #: Latency of the latest model step, however it ended (R0-4).
    last_step_latency_ms: int = 0
    #: Tool names with at least one success this turn (R0-2): a later success
    #: is the evidence that closes an error question, so successes must be
    #: remembered by name, not just counted.
    tool_success: set[str] = field(default_factory=set)
    #: The last emitted ledger signature: sorted ``(id, level)`` pairs.  The
    #: loop compares each round's open set against this to detect movement.
    ledger_signature: tuple[tuple[str, str], ...] = ()
    #: F1: skill loaded this turn (for outcome tracking). Empty = none loaded.
    loaded_skill: str = ""
    #: F2: Paris' Law fatigue index — accumulated error damage (Σ severity²).
    fatigue_index: float = 0.0
    #: F2: fatigue nudges already injected this turn.
    fatigue_nudges: int = 0
    #: 13 §2.1: successful ``ask_user`` calls this turn — the helped signal of
    #: the ladder's ask levels (L3 for strategy/fatigue, L2 for task/acceptance).
    ask_user_calls: int = 0
    #: 13 §2.5 (B): text deltas delivered this turn.  "The operator already saw
    #: part of the answer" is what earns a continuation instead of a failure.
    stream_deltas: int = 0
    #: 13 §2.5 (B): stream continuations spent this turn (bounded to 1).
    stream_continued: int = 0
    #: 13 §2.7 (H): retries spent on three-way-empty model steps (bounded to 1).
    empty_retries: int = 0

    # ---- notifications ----------------------------------------------------

    def note_iteration(self, index: int) -> None:
        """Record the iteration index about to start.

        ``iterations`` stays round-local (the C3 restart resets it), while
        ``total_iterations`` counts every model step this turn made — the
        number outcome and handoff report (H2 audit fix: a turn with three
        continuation rounds used to claim the last round's index).
        """
        self.iterations = index
        self.total_iterations += 1

    def note_nudge(self) -> None:
        """The task gate injected a reminder and will iterate again."""
        self.nudges += 1

    def note_plan_nudge(self) -> None:
        """stage 4 r2: the plan submission guard spent its one remedy."""
        self.nudges += 1
        self.plan_nudged = True

    def note_acceptance_nudge(self) -> None:
        """The acceptance gate injected a reminder and will iterate again."""
        self.acceptance_nudges += 1

    def note_fatigue_nudge(self) -> None:
        """The fatigue gate injected a reminder and will iterate again."""
        self.fatigue_nudges += 1

    def note_delta(self) -> None:
        """One text delta reached the caller (the stream-continuation signal)."""
        self.stream_deltas += 1

    def note_stream_continue(self) -> None:
        """The turn spent its one continuation on a broken stream (13 §2.5)."""
        self.stream_continued += 1

    def note_empty_retry(self) -> None:
        """The turn spent its one retry on an empty model step (13 §2.7)."""
        self.empty_retries += 1

    @property
    def ledger_open(self) -> int:
        """Open questions right now (length of the tracked signature)."""
        return len(self.ledger_signature)

    def ledger_items(self, *, stall_threshold: int) -> list[UncertaintyItem]:
        """Derive this round's open set from the recorded observations.

        Pure: the rules live in ``domain/ledger.py``; the books only supply
        the state.  The loop calls this once per round and emits when the
        signature moves.
        """
        return build_ledger(
            failures=self.failures,
            verify=self.verify,
            failed_streak=self.failed_streak,
            iteration=self.iterations,
            succeeded_tools=frozenset(self.tool_success),
            stall_threshold=stall_threshold,
        )

    def note_ledger(self, signature: Sequence[tuple[str, str]]) -> bool:
        """Track the open set's signature; return True when it moved.

        Emits are edge-triggered: the signature is what the emission triggers on.
        The ``LedgerUpdated`` event carries the item names, derived separately.
        """
        signature = tuple(signature)
        if signature == self.ledger_signature:
            return False
        self.ledger_signature = signature
        return True

    def note_strategy_nudge(self) -> None:
        """The escalation ladder spent its one "change strategy" nudge."""
        self.strategy_nudges += 1

    def note_steering(self, count: int = 1) -> None:
        """The user steered the turn: new instructions, so the streak restarts (Q5).

        The call counters are deliberately untouched: nothing succeeded, and the
        stall ladder is about tool progress, not about how many times we asked.
        """
        self.steering_messages += max(0, int(count))
        self.failed_streak = 0

    def note_step(
        self,
        text: str | None,
        *,
        finish_reason: str | None = None,
        latency_ms: int = 0,
    ) -> None:
        """Remember the latest model text (even when there is none)."""
        self.last_text = text
        self.last_finish_reason = finish_reason
        self.last_step_latency_ms = int(latency_ms)
        self.step_latencies_ms.append(int(latency_ms))

    def note_abandoned(self, latency_ms: int) -> None:
        """A model step cut by the turn clock: count its seconds (R0-4)."""
        self.abandoned_steps += 1
        self.last_step_latency_ms = int(latency_ms)
        self.step_latencies_ms.append(int(latency_ms))

    def note_verify(self, results: dict[str, tuple[bool, str]]) -> None:
        """Record acceptance verdicts by item id (R0-1)."""
        self.verify.update(results)

    def note_checkpoint(self, text: str, *, limit: int = DEFAULT_NOTE_CHARS) -> None:
        """Record the model's own line for this turn (R8a, D-1/D-12).

        Last write wins: within one batch the calls run in the model's order, so
        the final line is the one it wanted to leave.  A blank body is a no-op -
        an empty write must never erase a real one.
        """
        body = one_line(text, limit)
        if body:
            self.checkpoint = body

    def note_outcomes(self, outcomes: Sequence[DispatchOutcome]) -> None:
        """Count a batch of tool outcomes: attempts, successes, streak, failures.

        A batch that is empty — the model planned without calling anything — is
        not evidence either way, so it neither raises nor resets the streak
        (that is what keeps the ``strict`` todo policy working, R6b).
        """
        if not outcomes:
            return
        self.attempted_calls += len(outcomes)
        succeeded = [item for item in outcomes if item.result.success]
        self.successful_calls += len(succeeded)
        self.tool_success.update(item.call.name for item in succeeded)
        # 13 §2.1: the ladder's ask helped-signal counts only what actually
        # reached the user (a denied call never did).
        self.ask_user_calls += sum(
            1 for item in succeeded if item.call.name == "ask_user"
        )
        self.evidence_calls += sum(
            1 for item in succeeded if item.call.name not in STATE_ONLY_TOOLS
        )
        # R8c D-11 + 20 §批次3: a call the *user* cancelled, or one whose
        # result is a plan-validation refusal, is not a failure to fix -- it
        # must not push the escalation ladder (two cancels would inject a
        # persisted strategy nudge, and the turn is supposed to carry on after
        # an Escape).  An all-neutral round now also resets the burn: the
        # strategy window closes "helped" exactly when the streak is 0, so
        # parking it here would read a working loop as a failing one.
        counted = [item for item in outcomes if item.result.error not in _NOT_EVIDENCE_ERRORS]
        if not counted:
            self.failed_streak = 0
            return
        self.failed_streak = 0 if succeeded else self.failed_streak + 1
        for item in counted:
            if not item.result.success:
                error = str(item.result.error or "failed")
                self.failures.append((
                    item.call.name,
                    error,
                    (item.result.output or "")[:_FAILURE_EXCERPT],
                ))
                # F2: incremental fatigue accumulation (Paris' Law: ΔD = C × s^m)
                severity = self._severity_of(error)
                self.fatigue_index += _FATIGUE_C * (severity ** _FATIGUE_M)

    # ---- F2: Paris' Law fatigue ------------------------------------------

    @staticmethod
    def _severity_of(error: str) -> int:
        """Derive error severity from error text (1-5, 5 = most severe)."""
        error_lower = error.lower()
        for level in (5, 4, 3, 2):
            if any(kw in error_lower for kw in _SEVERITY_KEYWORDS.get(level, ())):
                return level
        return 1

    # ---- what a decision needs -------------------------------------------

    def snapshot(
        self,
        *,
        iteration: int,
        tokens_spent: int,
        open_items: Sequence[TodoItem] = (),
        final_text: str | None = None,
        acceptance: Sequence[AcceptanceState] = (),
        continuations: int = 0,
    ) -> TurnSnapshot:
        """The one object :class:`TurnPolicy` decides from.

        Both call sites (may-an-iteration-start, and how-a-step-ended) use this,
        so the policy cannot be handed two different views of the same turn.
        """
        return TurnSnapshot(
            iteration=iteration,
            nudges=self.nudges,
            open_items=tuple(open_items),
            final_text=final_text,
            tokens_spent=tokens_spent,
            failed_streak=self.failed_streak,
            attempted_calls=self.attempted_calls,
            successful_calls=self.successful_calls,
            strategy_nudges=self.strategy_nudges,
            acceptance=tuple(acceptance),
            acceptance_nudges=self.acceptance_nudges,
            continuations=continuations,
            fatigue_index=self.fatigue_index,
            fatigue_nudges=self.fatigue_nudges,
            ask_user_calls=self.ask_user_calls,
            plan_submitted="present_plan" in self.tool_success,
            plan_nudged=self.plan_nudged,
        )
