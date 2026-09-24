"""Everything one turn counts about itself (R7a).

The loop's body used to carry nine loose locals — ``iterations``, ``nudges``,
``attempted_calls``, ``successful_calls``, ``failed_streak``, ``strategy_nudges``,
``failures``, ``last_text`` and ``summaries`` — spread across a 228-line
``process_input``.  R6 added four of them (the escalation ladder needs to know how
the tool calls are going) and that is exactly why the method grew back after R5
had split it: every new behaviour had to reach into the same local scope.

This unit is pure: no IO, no asyncio, no events.  The loop notifies it
(``note_*``) and asks it for the one object a decision needs (``snapshot``); the
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
from ai_coding.domain.task import TodoItem

__all__ = ["Failure", "TurnBooks"]

#: One failed call, as the handoff reports it: (tool, error, output excerpt).
Failure = tuple[str, str, str]

_FAILURE_EXCERPT = 160


@dataclass
class TurnBooks:
    """The observable state of the turn so far."""

    #: The last iteration index the loop started (0 = nothing started yet).
    iterations: int = 0
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
    #: Summaries the compactor produced this turn (for the harvest to look at).
    summaries: list[str] = field(default_factory=list)

    # ---- notifications ----------------------------------------------------

    def note_iteration(self, index: int) -> None:
        """Record the iteration index about to start."""
        self.iterations = index

    def note_nudge(self) -> None:
        """The task gate injected a reminder and will iterate again."""
        self.nudges += 1

    def note_strategy_nudge(self) -> None:
        """The escalation ladder spent its one "change strategy" nudge."""
        self.strategy_nudges += 1

    def note_step(self, text: str | None) -> None:
        """Remember the latest model text (even when there is none)."""
        self.last_text = text

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
        self.failed_streak = 0 if succeeded else self.failed_streak + 1
        self.failures.extend(
            (
                item.call.name,
                str(item.result.error or "failed"),
                (item.result.output or "")[:_FAILURE_EXCERPT],
            )
            for item in outcomes
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
        )
