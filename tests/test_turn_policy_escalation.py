"""Escalation in the pure policy (R6b): change strategy, then stop and report.

The three tiers are: a repeated call is refused by the R4b duplicate guard (not
here), repeated failures get one "change strategy" nudge, and a turn in which
nothing has ever succeeded stops early so the handoff (R6a) can report it
(docs/r6-analysis.md 2.2).
"""

from __future__ import annotations

from ai_coding.core.turn_policy import (
    TurnAction,
    TurnPolicy,
    TurnPolicyConfig,
    TurnSnapshot,
)


def _policy(**overrides: int) -> TurnPolicy:
    return TurnPolicy(TurnPolicyConfig(max_iterations=10, **overrides))


def test_repeated_failures_ask_for_a_strategy_change():
    decision = _policy().decide_next_iteration(
        TurnSnapshot(iteration=3, failed_streak=2, successful_calls=3)
    )
    assert decision.action is TurnAction.NUDGE
    assert decision.reason == "repeated tool failures"
    assert "[strategy-gate]" in (decision.inject or "")
    assert "ask_user" in (decision.inject or "")


def test_the_strategy_nudge_is_given_once_per_turn():
    decision = _policy().decide_next_iteration(
        TurnSnapshot(iteration=4, failed_streak=5, successful_calls=1, strategy_nudges=1)
    )
    assert decision.action is TurnAction.ITERATE


def test_a_turn_with_no_success_stops_before_the_iteration_budget():
    decision = _policy().decide_next_iteration(
        TurnSnapshot(
            iteration=4,
            failed_streak=3,
            attempted_calls=3,
            successful_calls=0,
            strategy_nudges=1,
        )
    )
    assert decision.action is TurnAction.FINISH
    assert "no progress" in decision.reason
    assert decision.reason.startswith("no progress")


def test_a_successful_call_disarms_the_stall_stop():
    decision = _policy().decide_next_iteration(
        TurnSnapshot(
            iteration=4,
            failed_streak=1,
            attempted_calls=3,
            successful_calls=2,
            strategy_nudges=1,
        )
    )
    assert decision.action is TurnAction.ITERATE


def test_a_turn_that_never_calls_a_tool_is_not_a_stall():
    """A model that just talks is the stop policy's business, not the ladder's.

    ``strict`` keeps nudging such a turn until the iteration budget ends
    (tests/test_agent_loop_react.py::test_strict_policy_ignores_the_nudge_cap).
    """
    decision = _policy().decide_next_iteration(
        TurnSnapshot(iteration=5, failed_streak=0, attempted_calls=0, successful_calls=0)
    )
    assert decision.action is TurnAction.ITERATE


def test_budget_still_wins_over_escalation():
    decision = _policy().decide_next_iteration(
        TurnSnapshot(iteration=4, failed_streak=9, attempted_calls=9, successful_calls=0),
        budget_expired=True,
    )
    assert decision.action is TurnAction.FINISH
    assert decision.reason == "turn deadline exceeded"


def test_escalation_is_off_when_the_thresholds_are_zero():
    policy = _policy(failed_streak_threshold=0, stall_iterations=0)
    for iteration in (2, 5, 9):
        decision = policy.decide_next_iteration(
            TurnSnapshot(
                iteration=iteration,
                failed_streak=9,
                attempted_calls=9,
                successful_calls=0,
            )
        )
        assert decision.action is TurnAction.ITERATE


def test_from_ai_config_reads_the_new_keys():
    settings = type(
        "S",
        (),
        {
            "max_tool_iterations": 4,
            "failed_streak_threshold": 3,
            "max_strategy_nudges": 2,
            "stall_iterations": 5,
        },
    )()
    config = TurnPolicyConfig.from_ai_config(settings)
    assert (
        config.failed_streak_threshold,
        config.max_strategy_nudges,
        config.stall_iterations,
    ) == (3, 2, 5)
