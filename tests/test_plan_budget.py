"""Plan-run global budget (R1-4, TDD): one run's turns are bounded.

A step is bounded (``plan_step_max_iterations`` + one repair), but the run
is not: ``PlanRunner.run`` loops until stalled/blocked/deadlock/done, so a
30-step plan that keeps "completing" spends 30x(1-2) turns with no ceiling.
Worse, each ``process_input`` starts a FRESH wall-clock deadline
(``AgentLoop._begin_turn`` builds ``Deadline.after(turn_timeout)`` per call),
so a long plan is wall-clock unbounded too — worst case steps x 600s.

A turn-count cap bounds all three at once: turns directly, tokens via
turns x step-iterations x max_tokens, wall-clock via turns x turn_timeout.
The stop is resumable: finished steps persist on the task, so the next
``/plan-run`` continues where this one stopped.
"""

from types import SimpleNamespace

import pytest

from ai_coding.config.models import AIConfig
from ai_coding.core.plan_runner import PlanRunner
from ai_coding.core.turn_policy import TurnPolicy, TurnPolicyConfig
from ai_coding.domain.plan_delivery import StepDelivery
from ai_coding.domain.session import SessionData
from ai_coding.domain.task import TaskState


def _session_with(*contents, verifies=()):
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    task = TaskState(goal="plan")
    for index, content in enumerate(contents):
        item = task.add(content)
        if index < len(verifies) and verifies[index]:
            item.verify = verifies[index]
    session.task = task
    return session


class _StubLoop:
    """Stands in for AgentLoop: plays a script per call, capped or not."""

    def __init__(self, script, step_budget=8, run_budget=0, usage=None):
        self.settings = SimpleNamespace(
            plan_step_max_iterations=step_budget,
            plan_max_step_turns=run_budget,
        )
        self.policy = TurnPolicy(TurnPolicyConfig(max_iterations=100))
        self._script = list(script)
        self._usage = usage
        self.calls = []

    async def process_input(self, session, text, on_delta=None, observer=None, **kwargs):
        session.turn_count += 1
        self.calls.append(text)
        action = self._script.pop(0) if self._script else "complete"
        if action == "complete":
            for item in session.task.open_items():
                if not item.is_terminal():
                    item.status = "completed"
                    break
        elif action == "verify-pass":
            for item in session.acceptance:
                item.passed = True
                item.output = "ok"
        elif action == "verify-fail":
            for item in session.acceptance:
                item.passed = False
                item.output = "assert 245 == 200"
        return "step done"


class _UsageLoop(_StubLoop):
    """A stub whose loop reports per-turn usage like the real one."""

    @property
    def last_outcome(self):
        return SimpleNamespace(
            usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
        )


def _usage_of(loop):
    return getattr(loop, "last_outcome", None)


def test_config_defaults_run_budget_to_twenty():
    assert AIConfig().plan_max_step_turns == 20


@pytest.mark.asyncio
async def test_zero_cap_is_unbounded():
    session = _session_with(*[f"step {n}" for n in range(25)])
    loop = _StubLoop(["complete"] * 25, run_budget=0)
    report = await PlanRunner(loop).run(session)

    assert len(loop.calls) == 25
    assert report.stopped is None
    assert len(report.completed) == 25


@pytest.mark.asyncio
async def test_cap_stops_the_run_and_leaves_later_steps_open():
    session = _session_with("one", "two", "three", "four")
    loop = _StubLoop(["complete"] * 4, run_budget=3)
    report = await PlanRunner(loop).run(session)

    assert len(loop.calls) == 3
    assert report.completed == ["1", "2", "3"]
    assert report.stopped is not None and report.stopped.startswith("budget")
    assert session.task.items[3].status == "pending"  # never touched


@pytest.mark.asyncio
async def test_repair_attempt_counts_against_the_cap():
    session = _session_with("fix it", "next", verifies=["pytest -q"])
    loop = _StubLoop(["verify-fail", "verify-pass", "complete"], run_budget=2)
    report = await PlanRunner(loop).run(session)

    # step 1 cost 2 turns (try + repair); nothing left for step 2.
    assert report.deliveries[0].attempts == 2
    assert len(loop.calls) == 2
    assert report.stopped is not None and report.stopped.startswith("budget")
    assert report.completed == ["1"]


@pytest.mark.asyncio
async def test_exact_cap_finish_is_clean():
    session = _session_with("one", "two")
    loop = _StubLoop(["complete", "complete"], run_budget=2)
    report = await PlanRunner(loop).run(session)

    assert report.completed == ["1", "2"]
    assert report.stopped is None  # used up, but finished — no alarm


@pytest.mark.asyncio
async def test_report_counts_turns_and_sums_usage():
    session = _session_with("one", "two")
    loop = _UsageLoop(["complete", "complete"], run_budget=0)
    report = await PlanRunner(loop).run(session)

    assert report.turns_used == 2
    assert report.tokens_used == {
        "prompt_tokens": 200, "completion_tokens": 40, "total_tokens": 240,
    }
    assert report.deliveries[0].usage == {
        "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
    }
    assert "2 回合" in report.render() and "240" in report.render()


@pytest.mark.asyncio
async def test_loop_without_outcome_leaves_usage_unknown():
    session = _session_with("one")
    assert _usage_of(_StubLoop(["complete"])) is None  # the guard we rely on
    report = await PlanRunner(_StubLoop(["complete"])).run(session)

    assert report.turns_used == 1
    assert report.tokens_used is None
    assert report.deliveries[0].usage is None


def test_old_delivery_dict_without_usage_still_loads():
    loaded = StepDelivery.from_dict({"stepId": "1", "statement": "s"})
    assert loaded.usage is None
    assert loaded.to_dict()["usage"] is None
