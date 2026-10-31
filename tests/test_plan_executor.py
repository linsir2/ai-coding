"""TDD (stage 2): Plan-as-Goal — every round runs on the main session.

The executor keeps the plan's transcript where the human wrote it, verifies each
item mechanically *after* the turn (never inside it, so the acceptance ladder
cannot turn an autonomous run into an ``ask_user``), and lets the driver open
the next item on its own.  These tests pin the four things that make that safe:

1. session identity — no ephemeral step/wave sessions;
2. verification timing — acceptance is empty during ``process_input`` and runs
   afterwards, with the verdict written onto the receipt;
3. bounded repair and blocked/skip propagation;
4. crash leftovers and accounting (no fiat for a verify item, no double tokens).
"""

import asyncio
from types import SimpleNamespace

import pytest

from ai_coding.core.plan_executor import PlanExecutor
from ai_coding.core.plan_runner import PlanRunner
from ai_coding.core.wave_runner import WaveRunner, plan_runner_for
from tests.test_wave_runner import _session_with, _StubLoop


class _Loop(_StubLoop):
    """The wave stub plus the three observations this executor needs."""

    def __init__(self, actions, **kwargs):
        super().__init__(actions, **kwargs)
        self.posture_calls: list[tuple[str | None, bool | None, str]] = []
        self.acceptance_seen: list[list[object]] = []
        self.stop_policies: list[str] = []
        self.auto_caps: list[int] = []
        self.policy_objects: list[object] = []

    async def process_input(
        self, session, text, on_delta=None, observer=None, synthetic=False
    ):
        self.acceptance_seen.append(list(session.acceptance))
        self.stop_policies.append(str(self.policy.config.stop_policy))
        self.auto_caps.append(int(self.policy.config.max_auto_continuations))
        self.policy_objects.append(self.policy)
        return await super().process_input(
            session, text, on_delta=on_delta, observer=observer, synthetic=synthetic
        )

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
        """Match on the prompt's first line: failure output often contains the
        letters of other items' short placeholder names."""
        first = text.splitlines()[0]
        for item in task.open_items():
            if item.content in first:
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
        "plan_orchestrator": "session",
    }
    values.update(overrides)
    loop.settings = SimpleNamespace(**values)
    loop.settings.__dict__.setdefault("plan_orchestrator", "session")
    return loop.settings


# ---- session identity -------------------------------------------------


@pytest.mark.asyncio
async def test_each_item_runs_on_the_main_session():
    session = _session_with("a", "b", "c")
    loop = _Loop(["complete", "complete", "complete"])

    report = await PlanExecutor(loop).run(session)

    assert report.completed == ["1", "2", "3"]
    assert len(loop.sessions) == 3
    # The point of Plan-as-Goal: no step/wave session is ever created.
    assert all(seen is session for seen in loop.sessions)
    assert len(session.step_deliveries) == 3


# ---- verification timing ---------------------------------------------


@pytest.mark.asyncio
async def test_verify_runs_after_the_turn_not_during_it():
    session = _session_with("a", verifies=["true"])
    loop = _Loop(["complete"])
    loop._verify_results = [True]

    report = await PlanExecutor(loop).run(session)

    assert loop.acceptance_seen == [[]]
    assert report.completed == ["1"]
    assert session.step_deliveries[0].ac[0]["passed"] is True
    assert session.acceptance == []


@pytest.mark.asyncio
async def test_failed_verify_gets_one_repair_turn():
    session = _session_with("a", verifies=["true"])
    loop = _Loop(["complete", "complete"])
    loop._verify_results = [False, True]

    report = await PlanExecutor(loop).run(session)

    assert len(loop.calls) == 2
    assert report.completed == ["1"]
    assert session.step_deliveries[0].attempts == 2
    # The repair turn does not inherit a live acceptance contract.
    assert loop.acceptance_seen[0] == [] and loop.acceptance_seen[1] == []


@pytest.mark.asyncio
async def test_two_failed_verifies_block_and_skip_dependents():
    session = _session_with("a", "b", verifies=["true", None], deps={"2": ["1"]})
    loop = _Loop(["complete", "complete"])
    loop._verify_results = [False, False]

    report = await PlanExecutor(loop).run(session)

    assert session.task.get("1").status == "blocked"
    assert session.task.get("2").status == "skipped"
    assert report.blocked == ["1"]
    assert report.skipped == ["2"]


# ---- no-verify items --------------------------------------------------


@pytest.mark.asyncio
async def test_no_verify_completion_is_on_claim():
    session = _session_with("a")
    loop = _Loop(["complete"])

    await PlanExecutor(loop).run(session)

    delivery = session.step_deliveries[0]
    assert delivery.status == "completed"
    assert delivery.proof == "on-claim"
    assert delivery.attested is True


@pytest.mark.asyncio
async def test_no_verify_left_open_is_stalled():
    session = _session_with("a")
    loop = _Loop(["noop"])

    report = await PlanExecutor(loop).run(session)

    assert session.task.get("1").status != "completed"
    assert session.step_deliveries[0].status == "stalled"
    assert "stalled" in (report.stopped or "")


# ---- one round did more than one item ---------------------------------


@pytest.mark.asyncio
async def test_a_round_completing_every_item_gets_receipts_for_all():
    session = _session_with("a", "b", verifies=["true", "true"])
    loop = _Loop(["complete-both"])
    loop._verify_results = [True, True]

    report = await PlanExecutor(loop).run(session)

    assert report.completed == ["1", "2"]
    assert len(session.step_deliveries) == 2
    assert {entry.status for entry in session.step_deliveries} == {"completed"}
    # An extra completion settled by its own proof is verified, not fiat.
    assert {entry.proof for entry in session.step_deliveries} == {"verified"}


# ---- brakes and accounting -------------------------------------------


@pytest.mark.asyncio
async def test_no_progress_stops_after_two_empty_rounds():
    session = _session_with("a", "b")
    loop = _Loop(["noop", "noop"])

    report = await PlanExecutor(loop).run(session)

    assert "no progress" in (report.stopped or "")
    assert session.task.get("1").status != "completed"
    assert session.task.get("2").status != "completed"


@pytest.mark.asyncio
async def test_token_budget_uses_per_round_delta():
    session = _session_with("a", "b")
    loop = _Loop(["complete", "complete"], usage_per_turn=60)
    _settings(loop, plan_token_budget=100)

    report = await PlanExecutor(loop).run(session)

    assert "budget" in (report.stopped or "")
    # 60 + 60, not 60 + 120.
    assert session.task.plan_tokens_used == 120


@pytest.mark.asyncio
async def test_deadlock_is_reported():
    session = _session_with("a", "b", deps={"1": ["2"], "2": ["1"]})
    loop = _Loop([])

    report = await PlanExecutor(loop).run(session)

    assert "deadlock" in (report.stopped or "")


@pytest.mark.asyncio
async def test_cancel_propagates_and_disarms():
    session = _session_with("a")
    loop = _CancelLoop([])

    with pytest.raises(asyncio.CancelledError):
        await PlanExecutor(loop).run(session)

    assert session.task.armed is False


@pytest.mark.asyncio
async def test_rounds_run_is_not_incremented():
    session = _session_with("a", "b", "c")
    loop = _Loop(["complete", "complete", "complete"])

    await PlanExecutor(loop).run(session)

    # ``roundsRun`` drives the plan card's "第 N 波"; a session run has no waves.
    assert session.task.rounds_run == 0


# ---- cleanup / posture / selection ------------------------------------


@pytest.mark.asyncio
async def test_session_is_cleaned_and_policy_restored():
    session = _session_with("a", verifies=["true"])
    loop = _Loop(["complete"])
    loop._verify_results = [True]
    original = loop.policy

    await PlanExecutor(loop).run(session)

    assert session.step_item_id == ""
    assert session.item_deadline is None
    assert session.acceptance == []
    assert loop.policy is original


@pytest.mark.asyncio
async def test_run_forces_build_posture():
    session = _session_with("a")
    session.agent = "plan"
    session.readonly = True
    loop = _Loop(["complete"])

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
    loop._verify_results = [False]

    report = await PlanExecutor(loop).run(session)

    assert session.task.get("1").status == "blocked"
    assert session.step_deliveries[0].status == "blocked"
    assert report.blocked == ["1"]
