"""Proof-graded receipts (R1-6, TDD): verified vs claimed, on the receipt.

``_settle`` has two roads to ``completed`` — proof commands passed, or the
model said so — and the receipt could not tell them apart.  The grade is
derived, not stored: a completed attested delivery with acceptance verdicts
was verified (``_settle`` completes verify-steps only when every contract
passed); without verdicts it is on-claim; backfilled is fiat; anything
unfinished is n/a.  Visibility only — no road changes meaning.
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
    def __init__(self, script, run_budget=0):
        self.settings = SimpleNamespace(
            plan_step_max_iterations=8, plan_max_step_turns=run_budget
        )
        self.policy = TurnPolicy(TurnPolicyConfig(max_iterations=100))
        self._script = list(script)
        self.calls = []

    async def process_input(self, session, text, on_delta=None, observer=None):
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
        elif action == "verify-pass":
            for item in session.acceptance:
                item.passed = True
                item.output = "ok"
        elif action == "verify-fail":
            for item in session.acceptance:
                item.passed = False
                item.output = "assert 245 == 200"
        elif action == "ignore":
            pass
        return "step done"


@pytest.mark.asyncio
async def test_verified_step_grades_verified():
    session = _session_with("fix it", verifies=["pytest -q"])
    report = await PlanRunner(_StubLoop(["verify-pass"])).run(session)

    assert report.completed == ["1"]
    assert report.deliveries[0].proof == "verified"


@pytest.mark.asyncio
async def test_claimed_step_grades_on_claim():
    session = _session_with("write docs")
    report = await PlanRunner(_StubLoop(["complete"])).run(session)

    assert report.completed == ["1"]  # the road still means done…
    assert report.deliveries[0].proof == "on-claim"  # …but the grade says how
    assert "无证明完成" in report.render() and "：1" in report.render()


@pytest.mark.asyncio
async def test_backfilled_step_grades_fiat():
    session = _session_with("one", "two")
    report = await PlanRunner(_StubLoop(["complete-all"])).run(session)

    by_id = {d.step_id: d for d in report.deliveries}
    assert by_id["2"].proof == "fiat"


@pytest.mark.asyncio
async def test_unfinished_steps_grade_na():
    blocked = _session_with("fix it", verifies=["pytest -q"])
    blocked_report = await PlanRunner(_StubLoop(["verify-fail", "verify-fail"])).run(blocked)
    stalled = _session_with("write docs")
    stalled_report = await PlanRunner(_StubLoop(["ignore"])).run(stalled)

    assert blocked_report.deliveries[0].proof == "n/a"
    assert stalled_report.deliveries[0].proof == "n/a"


def test_old_receipt_with_verdicts_grades_verified():
    loaded = StepDelivery.from_dict({
        "stepId": "1", "statement": "s", "status": "completed",
        "ac": [{"id": "AC-1", "passed": True}],
    })
    assert loaded.proof == "verified"


def test_illegal_combo_falls_back_to_on_claim():
    handmade = StepDelivery(step_id="1", statement="s", status="completed",
                            attempts=1, ac=[], attested=True)
    assert handmade.proof == "on-claim"  # never claim proof that is absent


@pytest.mark.asyncio
async def test_clean_verified_run_has_no_proof_note():
    session = _session_with("fix it", verifies=["pytest -q"])
    report = await PlanRunner(_StubLoop(["verify-pass"])).run(session)

    assert "无证明" not in report.render()
