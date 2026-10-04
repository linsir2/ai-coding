"""C3 (TDD): auto-continue when the budget dies with work still open.

A turn that hits ``max_tool_iterations`` used to stop dead: the user got a
handoff and had to decide for themselves whether to continue.  C3 makes the
objective state the trigger — open todo items mean the work is unfinished, so
the loop injects a continuation prompt and starts a fresh iteration round,
up to ``max_auto_continuations`` times.  No open items means the work is
done, and the turn stops normally.
"""

import pytest

from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.turn_policy import TurnAction, TurnPolicy, TurnPolicyConfig, TurnSnapshot
from ai_coding.domain.loop import StepResult, TurnStatus
from ai_coding.domain.message import ToolCallRef
from ai_coding.domain.session import SessionData
from ai_coding.domain.task import TaskState, TodoItem
from ai_coding.service.session_service import SessionService
from ai_coding.tools.registry import ToolRegistry
from ai_coding.tools.todo_write_tool import TodoWriteTool


def _item(item_id: str, status: str = "pending") -> TodoItem:
    return TodoItem(id=item_id, content=f"do {item_id}", status=status)


def _config(**over) -> TurnPolicyConfig:
    base = {
        "stop_policy": "todo_aware",
        "max_todo_nudges": 2,
        "max_iterations": 3,
        "max_auto_continuations": 3,
    }
    base.update(over)
    return TurnPolicyConfig(**base)  # type: ignore[arg-type]


# ---- the continuation decision ---------------------------------------------


def test_budget_exhausted_with_open_items_continues():
    policy = TurnPolicy(_config(max_iterations=3, max_auto_continuations=3))
    decision = policy.decide_next_iteration(
        TurnSnapshot(iteration=4, open_items=(_item("1"),), continuations=0)
    )
    assert decision.action is TurnAction.NUDGE
    assert decision.reason == "continue unfinished work"


def test_budget_exhausted_with_no_open_items_stops():
    policy = TurnPolicy(_config(max_iterations=3, max_auto_continuations=3))
    decision = policy.decide_next_iteration(
        TurnSnapshot(iteration=4, open_items=(), continuations=0)
    )
    assert decision.action is TurnAction.FINISH
    assert decision.reason == "iteration budget exhausted"


def test_continuation_respects_the_cap():
    policy = TurnPolicy(_config(max_iterations=3, max_auto_continuations=2))
    decision = policy.decide_next_iteration(
        TurnSnapshot(iteration=4, open_items=(_item("1"),), continuations=2)
    )
    assert decision.action is TurnAction.FINISH
    assert decision.reason == "iteration budget exhausted"


def test_continuation_disabled_by_zero_cap():
    policy = TurnPolicy(_config(max_iterations=3, max_auto_continuations=0))
    decision = policy.decide_next_iteration(
        TurnSnapshot(iteration=4, open_items=(_item("1"),), continuations=0)
    )
    assert decision.action is TurnAction.FINISH


def test_continuation_message_names_the_open_items():
    policy = TurnPolicy(_config())
    decision = policy.decide_next_iteration(
        TurnSnapshot(iteration=4, open_items=(_item("1"), _item("2")), continuations=0)
    )
    assert decision.inject is not None
    assert "[continue]" in decision.inject
    assert "1. do 1" in decision.inject
    assert "2. do 2" in decision.inject


def test_continuation_fires_even_under_natural_stop_policy():
    """C3 is about unfinished work, not about the model's stop policy."""
    policy = TurnPolicy(_config(stop_policy="natural", max_iterations=3))
    decision = policy.decide_next_iteration(
        TurnSnapshot(iteration=4, open_items=(_item("1"),), continuations=0)
    )
    assert decision.action is TurnAction.NUDGE


def test_continuation_does_not_fire_within_budget():
    policy = TurnPolicy(_config(max_iterations=3))
    decision = policy.decide_next_iteration(
        TurnSnapshot(iteration=2, open_items=(_item("1"),), continuations=0)
    )
    assert decision.action is TurnAction.ITERATE


def test_from_ai_config_reads_the_continuation_cap():
    config = TurnPolicyConfig.from_ai_config(AIConfig(max_auto_continuations=5))
    assert config.max_auto_continuations == 5


def test_from_ai_config_defaults_the_continuation_cap_to_three():
    assert TurnPolicyConfig.from_ai_config(AIConfig()).max_auto_continuations == 3


# ---- handoff visibility -----------------------------------------------------


def test_handoff_says_how_many_continuations_ran():
    from ai_coding.domain.loop import TurnOutcome, TurnStatus
    from ai_coding.domain.turn_digest import render_handoff

    outcome = TurnOutcome(
        status=TurnStatus.MAX_ITERATIONS,
        reason="iteration budget exhausted",
        iterations=8,
    )
    task = TaskState(goal="g")
    task.add("still open")
    text = render_handoff(outcome=outcome, task=task, continuations=2)
    assert "2" in text


# ---- loop integration -------------------------------------------------------


class _ScriptedEngine:
    """Returns scripted steps; records every request. No network."""

    def __init__(self, steps):
        self._steps = list(steps)
        self.requests: list[dict] = []

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.requests.append({"messages": list(messages)})
        if self._steps:
            return self._steps.pop(0)
        return StepResult(text="done", finish_reason="stop")

    async def aclose(self):
        return None


def _tool_call(call_id: str) -> StepResult:
    """A model step that keeps working (asks for a tool, no final text)."""
    return StepResult(
        tool_calls=[ToolCallRef(
            id=call_id, name="todo_write", arguments='{"action":"list"}',
        )],
        finish_reason="tool_calls",
    )


def _integration_loop(engine, sessions, **settings_over) -> AgentLoop:
    base = {
        "stop_policy": "natural",
        "max_tool_iterations": 1,
        "max_auto_continuations": 2,
    }
    base.update(settings_over)
    registry = ToolRegistry()
    registry.register(TodoWriteTool())
    return AgentLoop(engine, sessions, registry, settings=AIConfig(**base))


@pytest.mark.asyncio
async def test_continuation_actually_buys_model_rounds(tmp_path):
    """The continuation must RE-ARM the budget, not just burn the count.

    Regression for a fake implementation: injecting the prompt and bumping
    the counter without resetting the iteration index means the next round
    immediately sees "budget exhausted" again — the model is never called,
    the cap burns down, and the turn ends with zero extra work.
    """
    engine = _ScriptedEngine([_tool_call(f"c{i}") for i in range(6)])
    sessions = SessionService(tmp_path / "sessions")
    loop = _integration_loop(engine, sessions)
    session = sessions.create()
    session.task = TaskState(goal="g")
    session.task.add("still open")

    await loop.process_input(session, "go")

    continuations = [m for m in session.messages if "[continue]" in (m.content or "")]
    assert len(continuations) == 2          # cap respected
    assert len(engine.requests) == 3        # each round really ran the model
    assert session.continuations == 2
    assert loop.last_outcome is not None
    assert loop.last_outcome.status is TurnStatus.MAX_ITERATIONS


@pytest.mark.asyncio
async def test_continuations_reset_for_the_next_user_turn(tmp_path):
    """The allowance is per user turn, not per session lifetime."""
    engine = _ScriptedEngine([_tool_call(f"c{i}") for i in range(12)])
    sessions = SessionService(tmp_path / "sessions")
    loop = _integration_loop(engine, sessions)
    session = sessions.create()
    session.task = TaskState(goal="g")
    session.task.add("still open")

    await loop.process_input(session, "first")
    assert session.continuations == 2
    await loop.process_input(session, "second")

    continuations = [m for m in session.messages if "[continue]" in (m.content or "")]
    assert len(continuations) == 4          # 2 per turn
    assert len(engine.requests) == 6        # both turns bought their rounds


@pytest.mark.asyncio
async def test_plan_step_does_not_auto_continue(tmp_path):
    """A plan step is one bounded turn — C3 must not extend it.

    The step session shares the plan's task, so on budget exhaustion every
    other open item looks like "unfinished work": C3 would let one step
    spend the whole plan's remaining items list.  The runner closes it.
    """
    from ai_coding.core.plan_runner import PlanRunner

    seen: list[int] = []

    class _StubLoop:
        def __init__(self):
            self.settings = AIConfig(
                stop_policy="natural", max_tool_iterations=1,
                max_auto_continuations=3, plan_step_max_iterations=1,
            )
            self.policy = TurnPolicy(TurnPolicyConfig.from_ai_config(self.settings))

        async def process_input(self, session, text, on_delta=None, observer=None):
            seen.append(self.policy.config.max_auto_continuations)
            for item in session.task.open_items():
                if not item.is_terminal():
                    item.status = "completed"
                    break
            return "done"

    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    task = TaskState(goal="g")
    task.add("one")
    task.add("two")
    session.task = task

    await PlanRunner(_StubLoop()).run(session)

    assert seen and all(value == 0 for value in seen)
