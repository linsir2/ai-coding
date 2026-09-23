"""TDD (R1a-1): StepResult / TurnOutcome / TurnStatus contracts."""

import dataclasses

import pytest

from ai_coding.domain.loop import StepResult, TurnOutcome, TurnStatus
from ai_coding.domain.message import ToolCallRef


def test_step_result_defaults():
    step = StepResult()
    assert step.text is None
    assert step.tool_calls == []
    assert step.finish_reason is None
    assert step.usage is None


def test_step_result_carries_tool_calls():
    ref = ToolCallRef(id="c1", name="read", arguments='{"path":"a"}')
    step = StepResult(text="hi", tool_calls=[ref], finish_reason="tool_calls")
    assert step.tool_calls[0].name == "read"
    assert step.finish_reason == "tool_calls"


def test_turn_status_members():
    # R2a added "timeout": the turn watchdog needs a terminal state of its own.
    assert {s.value for s in TurnStatus} == {
        "completed",
        "max_iterations",
        "timeout",
        "stopped",
        "aborted",
        "error",
    }


def test_turn_outcome_fields():
    outcome = TurnOutcome(status=TurnStatus.COMPLETED, text="done", iterations=3)
    assert outcome.status is TurnStatus.COMPLETED
    assert outcome.text == "done"
    assert outcome.iterations == 3
    assert outcome.reason == ""


def test_contracts_are_frozen():
    step = StepResult()
    outcome = TurnOutcome(status=TurnStatus.ERROR)
    with pytest.raises(dataclasses.FrozenInstanceError):
        step.text = "nope"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        outcome.text = "nope"  # type: ignore[misc]
