"""TDD (r24/C2): the wave runner — continuous sessions, item boundaries."""

from types import SimpleNamespace

import pytest

from ai_coding.core.turn_policy import TurnPolicy, TurnPolicyConfig
from ai_coding.core.wave_runner import WAVE_ITEM_HEAD, WaveRunner
from ai_coding.domain.loop import TurnStatus
from ai_coding.domain.session import SessionData
from ai_coding.domain.task import TaskState


def _session_with(*contents, verifies=(), deps=None):
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    task = TaskState(goal="plan")
    for index, content in enumerate(contents):
        item = task.add(content)
        if index < len(verifies) and verifies[index]:
            item.verify = verifies[index]
    for item_id, parents in (deps or {}).items():
        task.get(item_id).depends_on.extend(parents)
    session.task = task
    return session


class _Closer:
    def __init__(self):
        self.saves = 0

    def save(self, session):
        self.saves += 1


class _StubLoop:
    """Stands in for AgentLoop: scripted turn outcomes, no model, no shell."""

    def __init__(self, actions, *, usage_per_turn=0, workspace="."):
        self.settings = SimpleNamespace(
            plan_step_max_iterations=60,
            plan_max_waves=0,
            plan_wave_size=4,
            plan_wave_token_budget=0,
            plan_wave_wall_seconds=0,
            plan_max_item_attempts=2,
            plan_token_budget=0,
        )
        self.policy = TurnPolicy(TurnPolicyConfig(max_iterations=100))
        self._actions = list(actions)
        self._usage = int(usage_per_turn)
        self._verify_results: list[bool] = []
        self.calls: list[str] = []
        self.policies: list[int] = []
        self.sessions: list[SessionData] = []
        self.closer = _Closer()
        self.workspace = workspace
        self.extra_workspace_roots = ()
        self.last_outcome = SimpleNamespace(
            status=TurnStatus.COMPLETED, text="ok", usage=None
        )

    async def process_input(self, session, text, on_delta=None, observer=None):
        session.turn_count += 1
        self.calls.append(text)
        self.sessions.append(session)
        self.policies.append(int(self.policy.config.max_turn_tokens))
        action = self._actions.pop(0) if self._actions else "complete"
        if action == "complete":
            target = self._target(session.task, text)
            if target is not None:
                target.status = "completed"
        elif action == "complete-both":
            for item in session.task.open_items():
                if item.status != "blocked":
                    item.status = "completed"
        elif action in ("verify-pass", "verify-fail"):
            for entry in session.acceptance:
                entry.passed = action == "verify-pass"
                entry.output = "ok" if entry.passed else "boom"
        elif action == "noop":
            pass
        usage = (
            {"total_tokens": self._usage, "input_tokens": self._usage}
            if self._usage
            else None
        )
        self.last_outcome = SimpleNamespace(
            status=TurnStatus.COMPLETED, text=action, usage=usage
        )
        return "ok"

    async def verify_acceptance(self, session, books=None):
        verdict = self._verify_results.pop(0) if self._verify_results else True
        for entry in session.acceptance:
            entry.passed = verdict
            entry.output = "ok" if verdict else "boom"

    @staticmethod
    def _target(task, text):
        for item in task.open_items():
            if item.content in text:
                return item
        return None


@pytest.mark.asyncio
async def test_one_wave_reuses_one_session_and_writes_each_receipt():
    session = _session_with("a", "b", "c")
    loop = _StubLoop(["complete", "complete", "complete"])

    report = await WaveRunner(loop).run(session)

    assert report.waves == 1
    assert report.completed == ["1", "2", "3"]
    assert len(loop.calls) == 3
    # One session for the whole wave: continuity is the point of C.
    assert len({id(s) for s in loop.sessions}) == 1
    wave = loop.sessions[0]
    assert wave.ephemeral is True
    assert wave.task is session.task
    assert [d.step_id for d in session.step_deliveries] == ["1", "2", "3"]
    assert "已完成：1,2,3" in session.task.wave_note
    assert session.task.armed is False


@pytest.mark.asyncio
async def test_wave_session_inherits_parent_permissions():
    session = _session_with("a")
    session.permissions = [{"action": "bash", "resource": "*", "effect": "allow"}]
    loop = _StubLoop(["complete"])

    await WaveRunner(loop).run(session)

    assert loop.sessions[0].permissions == session.permissions


@pytest.mark.asyncio
async def test_failed_verification_blocks_and_skips_dependents():
    session = _session_with(
        "a", "b", "c",
        verifies=("true", "", ""),
        deps={"2": ["1"]},
    )
    loop = _StubLoop(["verify-fail", "verify-fail", "complete"])

    report = await WaveRunner(loop).run(session)

    assert report.completed == ["3"]
    assert report.blocked == ["1"]
    assert report.skipped == ["2"]
    assert session.task.get("1").status == "blocked"
    assert session.task.get("2").status == "skipped"
    assert [d.status for d in session.step_deliveries] == ["blocked", "skipped", "completed"]


@pytest.mark.asyncio
async def test_a_repair_turn_can_turn_a_failed_verify_green():
    session = _session_with("a", verifies=("true",))
    loop = _StubLoop(["verify-fail", "verify-pass"])

    report = await WaveRunner(loop).run(session)

    assert report.completed == ["1"]
    assert len(loop.calls) == 2
    assert session.task.get("1").status == "completed"
    assert "verify passed" in (session.task.get("1").evidence or "")


@pytest.mark.asyncio
async def test_extra_items_finished_in_one_turn_are_settled():
    session = _session_with("a", "b")
    loop = _StubLoop(["complete-both"])

    report = await WaveRunner(loop).run(session)

    assert report.completed == ["1", "2"]
    assert len(loop.calls) == 1
    assert {d.step_id for d in session.step_deliveries} == {"1", "2"}


@pytest.mark.asyncio
async def test_wave_budget_stops_between_turns_and_leaves_the_item_open():
    session = _session_with("a", "b")
    loop = _StubLoop(["complete"], usage_per_turn=60)
    loop.settings.plan_wave_token_budget = 50

    report = await WaveRunner(loop).run(session)

    assert report.completed == ["1"]
    assert session.task.get("2").status == "pending"
    assert report.stopped is not None and report.stopped.startswith("budget: wave token")


@pytest.mark.asyncio
async def test_the_remaining_wave_budget_clamps_the_next_turn():
    session = _session_with("a", "b")
    loop = _StubLoop(["complete", "complete"], usage_per_turn=200)
    loop.settings.plan_wave_token_budget = 300

    await WaveRunner(loop).run(session)

    # The second turn may spend only what the first one left.
    assert loop.policies == [300, 100]


def _unique_sessions(loop):
    """Wave sessions in order, one entry per wave (calls repeat an object)."""
    out: list[SessionData] = []
    for session in loop.sessions:
        if not out or out[-1] is not session:
            out.append(session)
    return out


@pytest.mark.asyncio
async def test_five_items_run_in_three_waves_and_carry_the_handoff():
    session = _session_with("a", "b", "c", "d", "e")
    loop = _StubLoop(["complete"] * 5)
    loop.settings.plan_wave_size = 2

    report = await WaveRunner(loop).run(session)

    assert report.waves == 3
    # Each wave is a fresh session; the second one starts from the first's note.
    waves = _unique_sessions(loop)
    assert len(waves) == 3
    assert "[上一波交接]" in waves[1].messages[0].content
    assert "已完成：1,2" in waves[1].messages[0].content


@pytest.mark.asyncio
async def test_deadlock_is_reported_not_called_done():
    # An unknown dependency means no item is ever ready: that is a deadlock,
    # not a completion.
    session = _session_with("a", deps={"1": ["99"]})
    loop = _StubLoop([])

    report = await WaveRunner(loop).run(session)

    assert report.stopped is not None
    assert "deadlock" in report.stopped
    assert report.completed == []


@pytest.mark.asyncio
async def test_a_completed_item_without_a_receipt_is_reverified_on_resume():
    session = _session_with("a", verifies=("true",))
    session.task.get("1").status = "completed"
    loop = _StubLoop([])

    await WaveRunner(loop).run(session)

    assert loop.calls == []  # no new turn spent
    assert session.task.get("1").status == "completed"
    delivery = session.step_deliveries[0]
    assert delivery.step_id == "1"
    assert delivery.attested is False
    assert "断点补验通过" in delivery.summary


@pytest.mark.asyncio
async def test_a_resume_backfill_failure_blocks_and_skips_dependents():
    session = _session_with("a", "b", verifies=("true", ""), deps={"2": ["1"]})
    session.task.get("1").status = "completed"
    loop = _StubLoop([])
    loop._verify_results = [False]

    await WaveRunner(loop).run(session)

    assert session.task.get("1").status == "blocked"
    assert session.task.get("2").status == "skipped"
    assert session.task.get("1").evidence.startswith("verify failed on resume")


@pytest.mark.asyncio
async def test_a_previous_stalled_receipt_does_not_suppress_a_new_completion():
    """The id is the item's, not the attempt's.

    A run that left item 1 stalled wrote a receipt; the next run finishes it
    and must append a *completed* receipt even though the id is already in
    ``step_deliveries``.
    """
    from ai_coding.domain.plan_delivery import StepDelivery

    session = _session_with("a")
    session.step_deliveries = [
        StepDelivery(step_id="1", statement="a", status="stalled", attempts=1)
    ]
    loop = _StubLoop(["complete"])

    report = await WaveRunner(loop).run(session)

    assert report.completed == ["1"]
    assert [entry.status for entry in session.step_deliveries] == ["stalled", "completed"]


@pytest.mark.asyncio
async def test_the_prompt_is_scoped_to_the_current_item():
    session = _session_with("a", "b", "c")
    loop = _StubLoop(["complete", "complete", "complete"])

    await WaveRunner(loop).run(session)

    assert loop.calls[0].startswith(WAVE_ITEM_HEAD)
    assert " b" not in loop.calls[0]
    assert " c" not in loop.calls[0]
