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
from ai_coding.domain.ledger import (
    UncertaintyItem,
    build_ledger,
    ledger_states,
)
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
    #: The last emitted ledger signature (R0-2): sorted ``(id, level)`` pairs.
    #: The loop compares each round's open set against this; an unchanged
    #: non-empty set means another round with zero uncertainty reduction.
    ledger_signature: tuple[tuple[str, str], ...] = ()
    #: Consecutive rounds the open set did not change (R0-2).  Only counts
    #: while something is actually open — an empty ledger never goes flat.
    ledger_flat_rounds: int = 0
    #: Ledger-gate nudges injected (``[ledger-gate]``) — its own budget.
    ledger_nudges: int = 0
    #: The current open set, as last derived (R0-2).  The snapshot projects
    #: these to ``LedgerState`` so a nudge can name the unknowns; the
    #: signature above is what the flat counter compares.
    ledger_current: tuple[UncertaintyItem, ...] = ()

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

    def note_acceptance_nudge(self) -> None:
        """The acceptance gate injected a reminder and will iterate again."""
        self.acceptance_nudges += 1

    def note_ledger_nudge(self) -> None:
        """The ledger gate injected a reminder and will iterate again."""
        self.ledger_nudges += 1

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

    def note_ledger(
        self,
        signature: Sequence[tuple[str, str]],
        items: Sequence[UncertaintyItem] = (),
    ) -> bool:
        """Track the open set's signature; return True when it moved.

        An unchanged *non-empty* set is another round with zero uncertainty
        reduction (the flat counter the policy gates on); an empty set never
        counts, and any movement resets the counter.  The items ride along so
        the snapshot can name the unknowns.
        """
        signature = tuple(signature)
        self.ledger_current = tuple(items)
        if signature == self.ledger_signature:
            if signature:
                self.ledger_flat_rounds += 1
            return False
        self.ledger_signature = signature
        self.ledger_flat_rounds = 0
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
        self.evidence_calls += sum(
            1 for item in succeeded if item.call.name not in STATE_ONLY_TOOLS
        )
        # R8c D-11: a call the *user* cancelled is not evidence either way - it is
        # not a failure to fix, so it must not push the escalation ladder (two of
        # them would inject a persisted strategy nudge, and the turn is supposed
        # to carry on after an Escape).
        counted = [item for item in outcomes if item.result.error != CANCELLED_BY_USER]
        if not counted:
            return
        self.failed_streak = 0 if succeeded else self.failed_streak + 1
        self.failures.extend(
            (
                item.call.name,
                str(item.result.error or "failed"),
                (item.result.output or "")[:_FAILURE_EXCERPT],
            )
            for item in counted
            if not item.result.success
        )

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
            ledger_open=self.ledger_open,
            ledger_flat_rounds=self.ledger_flat_rounds,
            ledger_nudges=self.ledger_nudges,
            ledger=ledger_states(self.ledger_current),
            continuations=continuations,
        )
