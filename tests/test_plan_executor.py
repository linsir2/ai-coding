"""TDD (stage 2): Plan-as-Goal — one continuous turn on the main session.

The approved plan must not be chopped into a ``process_input`` per item.  The
plan stop policy keeps the turn open when the model stops with items left
(opencode's idle hook); the executor verifies completed items afterwards and
opens at most a bounded repair turn.  These tests pin:

1. session identity and one-turn execution;
2. the plan policy contract (``stop_policy="plan"``, no acceptance in-turn);
3. bounded repair, blocked/skip propagation, crash leftovers;
4. no double token accounting and no ``roundsRun`` wave label.
"""

import asyncio
from types import SimpleNamespace

import pytest

from ai_coding.core.plan_executor import PlanExecutor
from ai_coding.core.plan_runner import PlanRunner
from ai_coding.core.turn_policy import (
    PLAN_CONTINUE,
    PLAN_CONTINUE_LEAD,
    TurnPolicy,
    TurnPolicyConfig,
    TurnSnapshot,
)
from ai_coding.core.wave_runner import WaveRunner, plan_runner_for
from ai_coding.domain.loop import TurnStatus
from tests.test_wave_runner import _session_with, _StubLoop


class _Loop(_StubLoop):
    """The wave stub plus the observations this executor needs.

    ``complete-all`` is the ideal continuous step: one model call finishes
    every open item, so the executor must not open a second turn.
    """

    def __init__(self, actions, **kwargs):
        super().__init__(actions, **kwargs)
        self.posture_calls: list[tuple[str | None, bool | None, str]] = []
        self.acceptance_seen: list[list[object]] = []
        self.stop_policies: list[str] = []
        self.auto_caps: list[int] = []
        self.executing_seen: list[bool] = []
        self.policy_objects: list[object] = []
        self.max_iterations: list[int] = []

    async def process_input(
        self, session, text, on_delta=None, observer=None, synthetic=False
    ):
        self.acceptance_seen.append(list(session.acceptance))
        self.stop_policies.append(str(self.policy.config.stop_policy))
        self.auto_caps.append(int(self.policy.config.max_auto_continuations))
        self.executing_seen.append(bool(getattr(session, "plan_executing", False)))
        self.policy_objects.append(self.policy)
        self.max_iterations.append(int(self.policy.config.max_iterations))
        action = self._actions[0] if self._actions else "complete-all"
        if action != "complete-all":
            return await super().process_input(
                session, text, on_delta=on_delta, observer=observer, synthetic=synthetic
            )
        if self._actions:
            self._actions.pop(0)
        session.turn_count += 1
        self.calls.append(text)
        self.sessions.append(session)
        self.policies.append(int(self.policy.config.max_turn_tokens))
        self.continuation_caps.append(self.policy.config.max_auto_continuations)
        for item in session.task.open_items():
            item.status = "completed"
        usage = (
            {"total_tokens": self._usage, "input_tokens": self._usage}
            if self._usage
            else None
        )
        self.last_outcome = SimpleNamespace(
            status=TurnStatus.COMPLETED, text=action, usage=usage
        )
        return "ok"

    def commit_posture(
        self, session, *, agent=None, readonly=None, reason="", observer=None
    ):
        self.posture_calls.append((agent, readonly, reason))
        if agent is not None:
            session.agent = agent
        if readonly is not None:
            session.readonly = readonly

    @staticmethod
    def _target(task, text):
        """Match an item line (``- id. content``); failure prose can contain the
        letters of other short placeholder names."""
        for line in text.splitlines():
            if not line.startswith("- "):
                continue
            for item in task.open_items():
                if f"{item.id}. {item.content}" in line:
                    return item
        return None


class _CancelLoop(_Loop):
    async def process_input(self, session, text, on_delta=None, observer=None, synthetic=False):
        raise asyncio.CancelledError()


def _settings(loop: _Loop, **overrides) -> SimpleNamespace:
    values = {
        "plan_step_max_iterations": 60,
        "plan_max_waves": 0,
        "plan_wave_size": 4,
        "plan_wave_token_budget": 0,
        "plan_wave_wall_seconds": 0,
        "plan_max_item_attempts": 2,
        "plan_token_budget": 0,
        "plan_item_wall_seconds": 0,
        "plan_max_continues": 30,
        "plan_orchestrator": "session",
    }
    values.update(overrides)
    loop.settings = SimpleNamespace(**values)
    return loop.settings


# ---- the plan stop policy --------------------------------------------


def _plan_policy(**overrides) -> TurnPolicy:
    return TurnPolicy(TurnPolicyConfig(stop_policy=PLAN_CONTINUE, **overrides))


def _snapshot_with(*contents, statuses=(), continuations=0) -> TurnSnapshot:
    from ai_coding.domain.task import TaskState

    task = TaskState(goal="g")
    for content in contents:
        task.add(content)
    for item, status in zip(task.items, statuses, strict=False):
        item.status = status
    return TurnSnapshot(
        iteration=1, open_items=tuple(task.open_items()), continuations=continuations
    )


def test_plan_policy_continues_when_the_model_stops_with_open_items():
    decision = _plan_policy().decide_after_step(_snapshot_with("a", "b"))

    assert decision.action.value == "nudge"
    assert decision.reason == "continue the plan"
    assert PLAN_CONTINUE_LEAD in decision.inject
    assert "- 1. a" in decision.inject


def test_plan_policy_finishes_when_nothing_is_left():
    decision = _plan_policy().decide_after_step(
        _snapshot_with("a", statuses=("completed",))
    )

    assert decision.action.value == "finish"
    assert decision.reason == "plan complete"


def test_plan_policy_stops_asking_after_its_budget():
    decision = _plan_policy(max_plan_continues=2).decide_after_step(
        _snapshot_with("a", continuations=2)
    )

    assert decision.action.value == "finish"
    assert "continuation budget" in (decision.reason or "")


def test_plan_policy_ignores_blocked_items():
    decision = _plan_policy().decide_after_step(
        _snapshot_with("a", statuses=("blocked",))
    )

    assert decision.action.value == "finish"
    assert decision.reason == "plan complete"


# ---- executor: one turn, main session --------------------------------


@pytest.mark.asyncio
async def test_one_turn_runs_the_whole_plan_on_the_main_session():
    session = _session_with("a", "b", "c", verifies=["true", "true", "true"])
    loop = _Loop(["complete-all"])
    _settings(loop)
    loop._verify_results = [True, True, True]

    report = await PlanExecutor(loop).run(session)

    assert len(loop.calls) == 1
    assert all(seen is session for seen in loop.sessions)
    assert report.completed == ["1", "2", "3"]
    assert {entry.proof for entry in session.step_deliveries} == {"verified"}


@pytest.mark.asyncio
async def test_plan_policy_is_active_and_acceptance_stays_out_of_the_turn():
    session = _session_with("a", verifies=["true"])
    loop = _Loop(["complete-all"])
    _settings(loop)
    loop._verify_results = [True]

    await PlanExecutor(loop).run(session)

    assert loop.stop_policies == [PLAN_CONTINUE]
    assert loop.max_iterations == [60]
    assert loop.executing_seen == [True]
    assert loop.acceptance_seen == [[]]
    assert session.acceptance == []


@pytest.mark.asyncio
async def test_stopping_early_leaves_leftovers_without_a_second_turn():
    session = _session_with("a", "b", "c")
    loop = _Loop(["complete"])
    _settings(loop)

    report = await PlanExecutor(loop).run(session)

    assert len(loop.calls) == 1
    assert report.completed == ["1"]
    assert report.stalled == ["2", "3"]
    assert "plan incomplete" in (report.stopped or "")


@pytest.mark.asyncio
async def test_failed_verify_is_stalled_for_the_next_run():
    session = _session_with("a", "b", verifies=["true", "true"])
    loop = _Loop(["complete-all"])
    _settings(loop)
    loop._verify_results = [False, True]

    report = await PlanExecutor(loop).run(session)

    # One turn, no repair turn: a failed proof stays retryable, not a dead end.
    assert len(loop.calls) == 1
    assert report.completed == ["2"]
    assert report.stalled == ["1"]
    assert session.task.get("1").status == "in_progress"
    assert {entry.status for entry in session.step_deliveries} == {
        "completed",
        "stalled",
    }


@pytest.mark.asyncio
async def test_a_failed_dependency_keeps_an_already_completed_item():
    session = _session_with("a", "b", verifies=["true", None], deps={"2": ["1"]})
    loop = _Loop(["complete-all"])
    _settings(loop)
    loop._verify_results = [False]

    report = await PlanExecutor(loop).run(session)

    assert session.task.get("1").status == "in_progress"
    # The model completed the dependent in the same turn, before the proof ran;
    # a completed dependent keeps its receipt (the wave rule).
    assert session.task.get("2").status == "completed"
    assert report.stalled == ["1"]


# ---- no-verify items --------------------------------------------------


@pytest.mark.asyncio
async def test_no_verify_completion_is_on_claim():
    session = _session_with("a")
    loop = _Loop(["complete-all"])
    _settings(loop)

    await PlanExecutor(loop).run(session)

    delivery = session.step_deliveries[0]
    assert delivery.status == "completed"
    assert delivery.proof == "fiat"


# ---- brakes and accounting -------------------------------------------


@pytest.mark.asyncio
async def test_no_progress_stops_after_one_empty_turn():
    session = _session_with("a", "b")
    loop = _Loop(["noop"])
    _settings(loop)

    report = await PlanExecutor(loop).run(session)

    assert "no progress" in (report.stopped or "")
    assert session.task.get("1").status != "completed"


@pytest.mark.asyncio
async def test_token_budget_uses_the_turns_delta():
    session = _session_with("a", verifies=["true"])
    loop = _Loop(["complete-all"], usage_per_turn=120)
    _settings(loop, plan_token_budget=100)
    loop._verify_results = [True]

    report = await PlanExecutor(loop).run(session)

    assert "budget" in (report.stopped or "")
    assert session.task.plan_tokens_used == 120


@pytest.mark.asyncio
async def test_cancel_propagates_and_disarms():
    session = _session_with("a")
    loop = _CancelLoop([])
    _settings(loop)

    with pytest.raises(asyncio.CancelledError):
        await PlanExecutor(loop).run(session)

    assert session.task.armed is False


@pytest.mark.asyncio
async def test_rounds_run_is_not_incremented():
    session = _session_with("a", "b", "c")
    loop = _Loop(["complete-all"])
    _settings(loop)
    loop._verify_results = [True, True, True]

    await PlanExecutor(loop).run(session)

    # ``roundsRun`` drives the plan card's "第 N 波"; a session run has no waves.
    assert session.task.rounds_run == 0


# ---- cleanup / posture / selection ------------------------------------


@pytest.mark.asyncio
async def test_session_is_cleaned_and_policy_restored():
    session = _session_with("a", verifies=["true"])
    loop = _Loop(["complete-all"])
    _settings(loop)
    loop._verify_results = [False, True]
    original = loop.policy

    await PlanExecutor(loop).run(session)

    assert session.plan_executing is False
    assert session.step_item_id == ""
    assert session.item_deadline is None
    assert session.acceptance == []
    assert loop.policy is original


@pytest.mark.asyncio
async def test_run_forces_build_posture():
    session = _session_with("a")
    session.agent = "plan"
    session.readonly = True
    loop = _Loop(["complete-all"])
    _settings(loop)

    await PlanExecutor(loop).run(session)

    assert session.agent == "build"
    assert session.readonly is False
    assert loop.posture_calls


@pytest.mark.asyncio
async def test_plan_runner_for_selects_the_session_executor():
    loop = _Loop([])
    _settings(loop)
    assert isinstance(plan_runner_for(loop), PlanExecutor)

    _settings(loop, plan_orchestrator="wave")
    assert isinstance(plan_runner_for(loop), WaveRunner)

    _settings(loop, plan_orchestrator="legacy")
    assert isinstance(plan_runner_for(loop), PlanRunner)


@pytest.mark.asyncio
async def test_crash_leftover_with_verify_is_checked_not_fiat():
    session = _session_with("a", verifies=["true"])
    session.task.get("1").status = "completed"
    loop = _Loop([])
    _settings(loop)
    loop._verify_results = [False, False]

    report = await PlanExecutor(loop).run(session)

    assert session.task.get("1").status == "in_progress"
    assert session.step_deliveries[0].status == "stalled"
    assert report.stalled == ["1"]
