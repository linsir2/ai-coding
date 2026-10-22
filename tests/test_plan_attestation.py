"""Plan-run attestation receipts (R1-5, TDD): fiat completions get receipts.

The model may mark steps done outside their bounded step turn — the R1-4
live run did three steps in one turn, leaving one delivery for three
completions.  The task state is right (done is done); the receipt book is
not.  The runner backfills unattested receipts for steps it never settled:
attempts=0, attested=False, zero turn cost.  Mechanical honesty, not model
control — the runner never forbids the batching, it just records it.
"""

from types import SimpleNamespace

import pytest

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
    """Stands in for AgentLoop; 'complete-all' marks every open item done."""

    def __init__(self, script, run_budget=0):
        self.settings = SimpleNamespace(
            plan_step_max_iterations=8, plan_max_step_turns=run_budget
        )
        self.policy = TurnPolicy(TurnPolicyConfig(max_iterations=100))
        self._script = list(script)
        self.calls = []

    async def process_input(self, session, text, on_delta=None, observer=None, **kwargs):
        session.turn_count += 1
        self.calls.append(text)
        action = self._script.pop(0) if self._script else "complete"
        if action in ("complete", "complete-all"):
            targets = session.task.open_items()
            if action == "complete":
                targets = targets[:1]
            for item in targets:
                if not item.is_terminal():
                    item.status = "completed"
        elif action == "ignore":
            pass
        return "step done"


@pytest.mark.asyncio
async def test_fiat_completions_get_unattested_receipts():
    session = _session_with("one", "two", "three")
    loop = _StubLoop(["complete-all"])
    report = await PlanRunner(loop).run(session)

    assert len(loop.calls) == 1  # one real turn did all three
    assert report.turns_used == 1
    assert len(report.deliveries) == 3
    by_id = {d.step_id: d for d in report.deliveries}
    assert by_id["1"].attested is True and by_id["1"].attempts == 1
    assert by_id["2"].attested is False and by_id["2"].attempts == 0
    assert by_id["3"].attested is False and by_id["3"].attempts == 0
    assert "分步之外" in by_id["2"].summary
    assert report.completed == ["1", "2", "3"]
    assert "分步外完成" in report.render()


@pytest.mark.asyncio
async def test_fiat_completions_cost_no_budget():
    session = _session_with("one", "two", "three")
    loop = _StubLoop(["complete-all"], run_budget=1)
    report = await PlanRunner(loop).run(session)

    assert report.turns_used == 1
    assert report.stopped is None  # the cap counts real turns, not fiat
    assert len(report.deliveries) == 3


@pytest.mark.asyncio
async def test_resumed_run_does_not_backfill_twice():
    session = _session_with("one", "two")
    session.task.items[1].status = "completed"
    session.step_deliveries = [
        StepDelivery(step_id="2", statement="two", status="completed",
                     attempts=0, summary="old receipt", attested=False),
    ]
    loop = _StubLoop(["complete"])
    report = await PlanRunner(loop).run(session)

    assert [d.step_id for d in report.deliveries] == ["1"]
    assert len(session.step_deliveries) == 2  # old kept, one added


@pytest.mark.asyncio
async def test_stalled_run_still_receipts_fiat_siblings():
    session = _session_with("one", "two")
    session.task.items[1].status = "completed"  # done before the run
    loop = _StubLoop(["ignore"])
    report = await PlanRunner(loop).run(session)

    assert report.stalled == ["1"]  # the stall is recorded (19 §3.4-3: the
    # run no longer stops — there is simply nothing else ready here)…
    by_id = {d.step_id: d for d in report.deliveries}
    assert by_id["2"].attested is False  # …and the fiat receipt lands first


@pytest.mark.asyncio
async def test_fiat_only_run_reports_zero_attested():
    """The screenshot case: everything already done, the run does no work."""
    session = _session_with("one", "two")
    session.task.items[0].status = "completed"
    session.task.items[1].status = "completed"
    loop = _StubLoop([])
    report = await PlanRunner(loop).run(session)

    assert loop.calls == []
    assert report.turns_used == 0
    assert len(report.deliveries) == 2
    assert all(d.attested is False for d in report.deliveries)
    assert "完成 0 步" in report.render()
    assert "分步外完成" in report.render()


@pytest.mark.asyncio
async def test_clean_run_render_has_no_attestation_note():
    session = _session_with("one")
    report = await PlanRunner(_StubLoop(["complete"])).run(session)

    assert report.completed == ["1"]
    assert "分步外" not in report.render()


def test_old_receipt_without_attested_defaults_true():
    loaded = StepDelivery.from_dict({"stepId": "1", "statement": "s"})
    assert loaded.attested is True
    assert loaded.to_dict()["attested"] is True


@pytest.mark.asyncio
async def test_a_reset_plan_gets_its_own_receipts():
    """The collision bug: ids restart at 1, so old receipts ate the new plan's.

    Reproduced live (probe_collision.py): after `/plan <topic>` reset the
    list, both new steps were completed out-of-band and the backfill
    produced zero receipts — the old plan's "1"/"2" matched them.
    """
    session = _session_with("old one", "old two")
    for index, statement in enumerate(("old one", "old two"), start=1):
        session.step_deliveries.append(StepDelivery(
            step_id=str(index), statement=statement, status="completed"))

    task = session.begin_plan("new goal")
    assert session.step_deliveries == []  # the cause: ids are reused, so the
    task.add("new one")                   # old book must not travel with them
    task.add("new two")
    for item in task.items:
        item.status = "completed"

    report = await PlanRunner(_StubLoop([])).run(session)

    receipts = [d for d in report.deliveries if d.statement.startswith("new")]
    assert len(receipts) == 2
    assert {d.step_id for d in receipts} == {"1", "2"}
    assert all(d.attested is False for d in receipts)
    assert [d.statement for d in session.step_deliveries] == ["new one", "new two"]
