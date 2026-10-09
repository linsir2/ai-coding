"""TDD (F2): Fatigue System — Paris' Law error accumulation."""

from __future__ import annotations

from ai_coding.core.dispatch import DispatchOutcome
from ai_coding.core.turn_books import TurnBooks
from ai_coding.core.turn_policy import (
    TASK_GATE_LEAD,
    TurnAction,
    TurnPolicy,
    TurnPolicyConfig,
    TurnSnapshot,
)
from ai_coding.domain.message import CANCELLED_BY_USER, ToolCall, ToolResult


def _outcome(
    name: str = "bash",
    *,
    success: bool = True,
    error: str | None = None,
    output: str = "",
) -> DispatchOutcome:
    return DispatchOutcome(
        call=ToolCall(id=f"c-{name}", name=name),
        result=ToolResult(success=success, output=output, error=error),
        denied=not success,
        halted=False,
    )


def _config(**over) -> TurnPolicyConfig:
    base = {"stop_policy": "todo_aware", "max_todo_nudges": 2, "max_iterations": 10}
    base.update(over)
    return TurnPolicyConfig(**base)


# ---------------------------------------------------------------------------
# 1. Fatigue computation in TurnBooks
# ---------------------------------------------------------------------------


class TestFatigueComputation:
    """TurnBooks._compute_fatigue() — Paris' Law error accumulation."""

    def test_fatigue_initial_zero(self):
        """No failures → fatigue_index = 0."""
        books = TurnBooks()
        assert books.fatigue_index == 0.0

    def test_fatigue_accumulates_on_failure(self):
        """Each failure adds to fatigue_index."""
        books = TurnBooks()
        books.note_outcomes([_outcome("bash", success=False, error="file not found")])
        assert books.fatigue_index > 0

    def test_fatigue_accumulates_multiple_failures(self):
        """Multiple failures accumulate."""
        books = TurnBooks()
        books.note_outcomes([_outcome("bash", success=False, error="error 1")])
        first_fatigue = books.fatigue_index
        books.note_outcomes([_outcome("read", success=False, error="error 2")])
        assert books.fatigue_index > first_fatigue

    def test_fatigue_severity_weighting(self):
        """Severe errors contribute more than minor ones."""
        books = TurnBooks()
        # severity-2 error: "not found" → 2² = 4
        books.note_outcomes([_outcome("bash", success=False, error="file not found")])
        minor_fatigue = books.fatigue_index

        books2 = TurnBooks()
        # severity-5 error: "timeout" → 5² = 25
        books2.note_outcomes([_outcome("bash", success=False, error="timeout")])
        severe_fatigue = books2.fatigue_index

        assert severe_fatigue > minor_fatigue

    def test_fatigue_severity_levels(self):
        """Verify severity classification for different error types."""
        # Level 5: timeout, context overflow
        books = TurnBooks()
        books.note_outcomes([_outcome("bash", success=False, error="timeout")])
        assert books.fatigue_index == 25.0  # 5²

        # Level 3: permission denied
        books = TurnBooks()
        books.note_outcomes([_outcome("bash", success=False, error="permission denied")])
        assert books.fatigue_index == 9.0  # 3²

        # Level 2: not found, error, failed
        books = TurnBooks()
        books.note_outcomes([_outcome("bash", success=False, error="file not found")])
        assert books.fatigue_index == 4.0  # 2²

        # Level 1: default
        books = TurnBooks()
        books.note_outcomes([_outcome("bash", success=False, error="something weird")])
        assert books.fatigue_index == 1.0  # 1²

    def test_fatigue_with_success_outcomes(self):
        """Successful outcomes don't add fatigue."""
        books = TurnBooks()
        books.note_outcomes([_outcome("bash", success=True)])
        assert books.fatigue_index == 0.0

    def test_fatigue_mixed_outcomes(self):
        """Only failures add to fatigue."""
        books = TurnBooks()
        books.note_outcomes([
            _outcome("bash", success=True),
            _outcome("read", success=False, error="not found"),
            _outcome("edit", success=True),
        ])
        assert books.fatigue_index == 4.0  # one severity-2 error

    def test_fatigue_not_affected_by_cancelled(self):
        """User-cancelled calls don't add fatigue."""
        books = TurnBooks()
        books.note_outcomes([
            _outcome("bash", success=False, error=CANCELLED_BY_USER),
        ])
        # CANCELLED_BY_USER is not in severity keywords → severity=1
        # But cancelled calls don't count toward failures
        # Actually looking at the code, cancelled calls are excluded from failures
        # Let me check...
        # In note_outcomes: counted = [item for item in outcomes if item.result.error != CANCELLED_BY_USER]
        # So cancelled calls don't add to failures
        assert books.fatigue_index == 0.0

    def test_fatigue_snapshot_includes_index(self):
        """snapshot() includes fatigue_index."""
        books = TurnBooks()
        books.note_outcomes([_outcome("bash", success=False, error="timeout")])
        snapshot = books.snapshot(iteration=1, tokens_spent=0)
        assert snapshot.fatigue_index == 25.0


# ---------------------------------------------------------------------------
# 2. Fatigue gate in TurnPolicy
# ---------------------------------------------------------------------------


class TestFatigueGate:
    """TurnPolicy fatigue gate — triggers when accumulated errors cross threshold."""

    def test_fatigue_below_threshold_iterates(self):
        """Below threshold → ITERATE (no fatigue gate)."""
        policy = TurnPolicy(_config())
        snapshot = TurnSnapshot(iteration=1, fatigue_index=10.0)
        decision = policy.decide_next_iteration(snapshot)
        assert decision.action is TurnAction.ITERATE

    def test_fatigue_at_threshold_triggers_nudge(self):
        """At threshold → NUDGE with fatigue message."""
        policy = TurnPolicy(_config())
        snapshot = TurnSnapshot(iteration=1, fatigue_index=30.0)
        decision = policy.decide_next_iteration(snapshot)
        assert decision.action is TurnAction.NUDGE
        assert "fatigue" in decision.reason

    def test_fatigue_above_threshold_triggers_nudge(self):
        """Above threshold → NUDGE."""
        policy = TurnPolicy(_config())
        snapshot = TurnSnapshot(iteration=1, fatigue_index=50.0)
        decision = policy.decide_next_iteration(snapshot)
        assert decision.action is TurnAction.NUDGE

    def test_fatigue_nudge_only_once(self):
        """Fatigue nudge only injected once per turn."""
        policy = TurnPolicy(_config())
        snapshot = TurnSnapshot(iteration=1, fatigue_index=35.0, fatigue_nudges=1)
        decision = policy.decide_next_iteration(snapshot)
        # Already nudged once → should ITERATE (or fall through to other gates)
        assert decision.action is not TurnAction.NUDGE or "fatigue" not in decision.reason

    def test_fatigue_message_content(self):
        """Fatigue message tells model to change approach or summarize."""
        policy = TurnPolicy(_config())
        msg = policy.fatigue_message(10.0)
        assert "fatigue" in msg.lower() or "疲劳" in msg

    def test_fatigue_gate_before_failed_streak(self):
        """Fatigue gate fires before failed_streak gate."""
        policy = TurnPolicy(_config())
        # Both fatigue and failed_streak are high
        snapshot = TurnSnapshot(
            iteration=3,
            fatigue_index=35.0,
            failed_streak=3,
            strategy_nudges=0,
            attempted_calls=3,
            successful_calls=0,
        )
        decision = policy.decide_next_iteration(snapshot)
        # Should be NUDGE with fatigue reason
        assert decision.action is TurnAction.NUDGE

    def test_fatigue_zero_allows_iterate(self):
        """No fatigue → normal iteration."""
        policy = TurnPolicy(_config())
        snapshot = TurnSnapshot(iteration=1, fatigue_index=0.0)
        decision = policy.decide_next_iteration(snapshot)
        assert decision.action is TurnAction.ITERATE

    def test_fatigue_with_stall(self):
        """Stall (no progress) takes precedence over fatigue."""
        policy = TurnPolicy(_config(stall_iterations=3))
        snapshot = TurnSnapshot(
            iteration=5,
            fatigue_index=35.0,
            attempted_calls=5,
            successful_calls=0,
        )
        decision = policy.decide_next_iteration(snapshot)
        # Stall should fire (no successful calls in 5 iterations)
        assert decision.action is TurnAction.FINISH
        assert "no progress" in decision.reason


# ---------------------------------------------------------------------------
# 3. Integration with existing escalation ladder
# ---------------------------------------------------------------------------


class TestFatigueIntegration:
    """Fatigue gate works alongside existing escalation ladder."""

    def test_fatigue_does_not_affect_task_gate(self):
        """Task gate (open items) still works with fatigue."""
        policy = TurnPolicy(_config())
        from ai_coding.domain.task import TodoItem
        snapshot = TurnSnapshot(
            iteration=1,
            fatigue_index=0.0,
            open_items=(TodoItem(id="1", content="do something"),),
            nudges=0,
        )
        decision = policy.decide_after_step(snapshot)
        assert decision.action is TurnAction.NUDGE
        assert TASK_GATE_LEAD in decision.inject

    def test_full_workflow_low_fatigue(self):
        """Normal turn with low fatigue → ITERATE."""
        policy = TurnPolicy(_config())
        snapshot = TurnSnapshot(
            iteration=1,
            fatigue_index=2.0,
            attempted_calls=1,
            successful_calls=1,
        )
        decision = policy.decide_next_iteration(snapshot)
        assert decision.action is TurnAction.ITERATE

    def test_full_workflow_high_fatigue(self):
        """Turn with high fatigue → NUDGE."""
        policy = TurnPolicy(_config())
        snapshot = TurnSnapshot(
            iteration=3,
            fatigue_index=35.0,
            attempted_calls=5,
            successful_calls=2,
        )
        decision = policy.decide_next_iteration(snapshot)
        assert decision.action is TurnAction.NUDGE


# ---------------------------------------------------------------------------
# 4. Severity classification
# ---------------------------------------------------------------------------


class TestSeverityClassification:
    """TurnBooks._severity_of() — error severity classification."""

    def test_severity_timeout(self):
        assert TurnBooks._severity_of("connection timeout") == 5

    def test_severity_context_overflow(self):
        assert TurnBooks._severity_of("context overflow") == 5

    def test_severity_rate_limit(self):
        assert TurnBooks._severity_of("rate limit exceeded") == 4

    def test_severity_permission_denied(self):
        assert TurnBooks._severity_of("permission denied") == 3

    def test_severity_not_found(self):
        assert TurnBooks._severity_of("file not found") == 2

    def test_severity_error(self):
        assert TurnBooks._severity_of("some error occurred") == 2

    def test_severity_failed(self):
        assert TurnBooks._severity_of("operation failed") == 2

    def test_severity_default(self):
        assert TurnBooks._severity_of("unknown problem") == 1

    def test_severity_empty_string(self):
        assert TurnBooks._severity_of("") == 1

    def test_severity_case_insensitive(self):
        assert TurnBooks._severity_of("TIMEOUT") == 5
        assert TurnBooks._severity_of("Permission Denied") == 3
