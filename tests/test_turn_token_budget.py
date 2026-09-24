"""TDD (R4b): the turn-level token gate.

The gate lives in ``TurnPolicy`` rather than in a guard, because "may another
iteration start?" is already that unit's question — and a guard deciding whether
a turn lives or dies would be an authority it should not have
(docs/r4-analysis.md 5.4).

Note the semantics: the check happens at an *iteration boundary*, so one model
step can overshoot.  Pretending otherwise would be a lie about what is knowable
before a call is made (P10).
"""

from ai_coding.core.turn_policy import (
    TurnAction,
    TurnPolicy,
    TurnPolicyConfig,
    TurnSnapshot,
)
from ai_coding.domain.loop import TurnStatus


def _policy(**over) -> TurnPolicy:
    base = {
        "stop_policy": "natural",
        "max_todo_nudges": 2,
        "max_iterations": 10,
        "max_turn_tokens": 0,
    }
    base.update(over)
    return TurnPolicy(TurnPolicyConfig(**base))  # type: ignore[arg-type]


def test_no_budget_configured_means_the_gate_never_fires():
    decision = _policy(max_turn_tokens=0).decide_next_iteration(
        TurnSnapshot(iteration=1, tokens_spent=10**9)
    )
    assert decision.action is TurnAction.ITERATE


def test_the_gate_fires_once_the_turn_has_spent_its_budget():
    decision = _policy(max_turn_tokens=1000).decide_next_iteration(
        TurnSnapshot(iteration=2, tokens_spent=1000)
    )
    assert decision.action is TurnAction.FINISH
    assert decision.reason == "turn token budget exhausted"


def test_the_gate_allows_one_more_iteration_below_the_budget():
    decision = _policy(max_turn_tokens=1000).decide_next_iteration(
        TurnSnapshot(iteration=2, tokens_spent=999)
    )
    assert decision.action is TurnAction.ITERATE


def test_the_clock_wins_over_the_token_gate():
    """Both budgets blown: the deadline is the more certain fact."""
    decision = _policy(max_turn_tokens=1).decide_next_iteration(
        TurnSnapshot(iteration=2, tokens_spent=10**6), budget_expired=True
    )
    assert decision.action is TurnAction.FINISH
    assert decision.reason == "turn deadline exceeded"


def test_the_iteration_budget_wins_over_the_token_gate():
    decision = _policy(max_turn_tokens=1, max_iterations=1).decide_next_iteration(
        TurnSnapshot(iteration=5, tokens_spent=10**6)
    )
    assert decision.action is TurnAction.FINISH
    assert decision.reason == "iteration budget exhausted"


def test_budget_is_a_first_class_turn_status():
    assert TurnStatus.BUDGET.value == "budget"
