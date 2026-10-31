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
        self.continuation_caps: list[int] = []
        self.sessions: list[SessionData] = []
        self.closer = _Closer()
        self.workspace = workspace
        self.extra_workspace_roots = ()
        self.last_outcome = SimpleNamespace(
            status=TurnStatus.COMPLETED, text="ok", usage=None
        )

    async def process_input(self, session, text, on_delta=None, observer=None, synthetic=False):
        session.turn_count += 1
        self.calls.append(text)
        self.sessions.append(session)
        self.policies.append(int(self.policy.config.max_turn_tokens))
        self.continuation_caps.append(self.policy.config.max_auto_continuations)
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
    assert loop.continuation_caps == [1, 0], (
        "the repair turn must not spend a second continuation on the same item"
    )
    assert session.task.get("1").status == "completed"
    assert "verify passed" in (session.task.get("1").evidence or "")


class _CrossWaveLoop(_StubLoop):
    """Marks an item from a *later* wave completed during the first turn."""

    def __init__(self, actions, **kwargs):
        super().__init__(actions, **kwargs)
        self.settings.plan_wave_size = 2

    async def process_input(self, session, text, **kwargs):
        result = await super().process_input(session, text, **kwargs)
        if len(self.calls) == 1:
            session.task.get("3").status = "completed"
        return result


@pytest.mark.asyncio
async def test_an_item_completed_in_a_later_wave_still_gets_its_receipt():
    """Measured 2026-10-31: 3 items, task said 3/3, report said 2/3, because
    item 3 was marked completed by another item's turn and a later wave never
    ran it.  The sweep covers the whole task, not only the current wave."""
    session = _session_with("a", "b", "c")
    loop = _CrossWaveLoop(["complete", "complete"])

    report = await WaveRunner(loop).run(session)

    assert sorted(report.completed) == ["1", "2", "3"]
    assert {d.step_id for d in session.step_deliveries} == {"1", "2", "3"}
    item3 = next(d for d in session.step_deliveries if d.step_id == "3")
    assert item3.attested is False, "a claim with no proof is fiat, not attested"
    assert item3.attempts == 0


@pytest.mark.asyncio
async def test_a_resumed_receipt_does_not_become_a_second_delivery():
    from ai_coding.domain.plan_delivery import StepDelivery

    session = _session_with("a")
    session.task.get("1").status = "completed"
    session.step_deliveries = [
        StepDelivery(step_id="1", statement="a", status="completed", attempts=1)
    ]
    loop = _StubLoop(["complete"])

    report = await WaveRunner(loop).run(session)

    assert report.completed == []
    assert [d.step_id for d in session.step_deliveries] == ["1"]


@pytest.mark.asyncio
async def test_extra_items_finished_in_one_turn_are_settled():
    session = _session_with("a", "b")
    loop = _StubLoop(["complete-both"])

    report = await WaveRunner(loop).run(session)

    assert report.completed == ["1", "2"]
    assert len(loop.calls) == 1
    assert {d.step_id for d in session.step_deliveries} == {"1", "2"}


@pytest.mark.asyncio
async def test_a_real_item_spend_runs_the_whole_plan_in_one_run():
    """A+C: one real item costs ~269k tokens (measured), so the shipped
    defaults carry no per-wave spend cap.  Five such items must finish in one
    run -- no stop, no manual /plan run."""
    session = _session_with("a", "b", "c", "d", "e")
    loop = _StubLoop(["complete"] * 5, usage_per_turn=270_000)

    report = await WaveRunner(loop).run(session)

    assert report.stopped is None
    assert report.completed == ["1", "2", "3", "4", "5"]
    assert report.waves == 2  # five items at the default wave size of four
    assert all(item.status == "completed" for item in session.task.items)


@pytest.mark.asyncio
async def test_a_plan_that_completes_nothing_for_three_waves_is_stopped():
    """The one automatic brake: waves that explore but finish no item are the
    failure mode worth stopping (a slow plan is not)."""
    session = _session_with(*[f"i{index}" for index in range(12)])
    loop = _StubLoop(["noop"] * 12)

    report = await WaveRunner(loop).run(session)

    assert report.stopped is not None
    assert report.stopped.startswith("no progress:")
    assert report.completed == []
    assert report.waves == 3
    assert all(item.status == "pending" for item in session.task.items)


@pytest.mark.asyncio
async def test_a_slow_plan_that_completes_one_item_per_wave_is_not_stopped():
    """Progress resets the counter -- only a run of empty waves fires it.

    The run may still end for C2's own reason (items attempted and left open
    are "stalled"); what this pins is that the *no-progress* brake never fires
    while each wave banks an item."""
    session = _session_with(*[f"i{index}" for index in range(9)])
    loop = _StubLoop(
        ["complete", "noop", "noop", "complete", "noop", "noop", "complete", "noop", "noop"]
    )

    report = await WaveRunner(loop).run(session)

    assert report.completed == ["1", "4", "7"]
    assert report.stopped is None or not report.stopped.startswith("no progress:")


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
    # not a completion.  C3 adds a replan attempt first; this pins the ending
    # with that feature off (its own suite covers the replan path).
    session = _session_with("a", deps={"1": ["99"]})
    loop = _StubLoop([])
    loop.settings.plan_max_replans = 0

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
async def test_each_item_gets_one_wall_clock_shared_by_its_turns():
    """A repair pass used to buy a fresh turn timeout, so N attempts multiplied
    the ceiling.  One item now gets one deadline (``plan_item_wall_seconds``),
    and the settle turn that follows gets none again."""
    session = _session_with("a", "b")
    loop = _StubLoop(["complete", "complete"])
    loop.settings.plan_item_wall_seconds = 1.0
    seen: list = []
    original = loop.process_input

    async def spy(step_session, text, **kwargs):
        seen.append(step_session.item_deadline)
        return await original(step_session, text, **kwargs)

    loop.process_input = spy  # type: ignore[method-assign]

    await WaveRunner(loop).run(session)

    assert len(seen) == 2
    assert all(deadline is not None for deadline in seen)
    assert seen[0] != seen[1], "a new item gets a new clock"
    assert loop.sessions[-1].item_deadline is None, "no clock outlives its item"


@pytest.mark.asyncio
async def test_the_prompt_is_scoped_to_the_current_item():
    session = _session_with("a", "b", "c")
    loop = _StubLoop(["complete", "complete", "complete"])

    await WaveRunner(loop).run(session)

    assert loop.calls[0].startswith(WAVE_ITEM_HEAD)
    assert " b" not in loop.calls[0]
    assert " c" not in loop.calls[0]


# ---- D: state lands when an item lands, not when the wave ends --------------


class _SnapshotCloser:
    """Records what the task looked like at every save.

    A save count alone cannot tell "item 1 was persisted" from "the wave-end
    save happened to include it" -- both leave item 1 completed on disk when the
    run finishes.  The order is the fact: a snapshot in which item 1 is done and
    item 2 is not can only come from a save that ran between them.
    """

    def __init__(self):
        self.snapshots: list[list[tuple[str, str]]] = []

    def save(self, session):  # type: ignore[no-untyped-def]
        self.snapshots.append([(item.id, item.status) for item in session.task.items])


@pytest.mark.asyncio
async def test_each_completed_item_is_persisted_before_the_next_one_starts():
    """Measured live 2026-10-30: three items completed inside a wave, the wave
    was killed mid-flight, and the parent session still said ``pending`` for all
    three -- the only save points were arm, wave end and disarm."""
    session = _session_with("a", "b", "c")
    loop = _StubLoop(["complete", "complete", "complete"])
    loop.closer = _SnapshotCloser()

    await WaveRunner(loop).run(session)

    between = [
        dict(snapshot)
        for snapshot in loop.closer.snapshots
        if dict(snapshot).get("1") == "completed" and dict(snapshot).get("2") != "completed"
    ]
    assert between, (
        "no save happened while item 1 was done and item 2 was not; "
        f"snapshots={loop.closer.snapshots}"
    )


@pytest.mark.asyncio
async def test_a_dead_turn_after_an_item_keeps_that_item_on_disk():
    """The wave session is ephemeral (its transcript is disposable), but the
    *task* it shares with the parent is the durable record."""

    class _CrashingLoop(_StubLoop):
        async def process_input(self, session, text, on_delta=None, observer=None, synthetic=False):
            if self.calls:
                raise RuntimeError("turn died")
            return await super().process_input(session, text, on_delta, observer, synthetic)

    session = _session_with("a", "b")
    loop = _CrashingLoop(["complete", "complete"])
    loop.closer = _SnapshotCloser()

    # The runner turns a dead turn into a report entry, not an exception (one bad
    # turn must not kill the wave).  Either way item 1 already happened.
    await WaveRunner(loop).run(session)

    between = [
        dict(snapshot)
        for snapshot in loop.closer.snapshots
        if dict(snapshot).get("1") == "completed"
    ]
    assert between, f"item 1 never reached a save; snapshots={loop.closer.snapshots}"
    assert session.task.get("1").status == "completed"


# ---- E: the harness's own words must not rebuild the file block ------------


class _SyntheticRecordingLoop(_StubLoop):
    def __init__(self, actions, **kwargs):
        super().__init__(actions, **kwargs)
        self.synthetic_flags: list[bool] = []

    async def process_input(self, session, text, on_delta=None, observer=None, synthetic=False):
        self.synthetic_flags.append(synthetic)
        return await super().process_input(session, text, on_delta, observer, synthetic)


@pytest.mark.asyncio
async def test_the_item_prompt_is_sent_as_synthetic():
    """Measured 2026-10-30: the wave seed names ``backend/app/models.py`` and the
    file block rebuilt from it carried 22-28k characters into *every* request.
    The prompt is the harness's, so it must not answer 'what did the human say'."""
    session = _session_with("a", "b")
    loop = _SyntheticRecordingLoop(["complete", "complete"])

    await WaveRunner(loop).run(session)

    assert loop.synthetic_flags == [True, True], (
        f"item prompts reached the loop as human speech: {loop.synthetic_flags}"
    )


def test_the_wave_seed_is_marked_synthetic():
    session = _session_with("a")
    loop = _StubLoop([])
    runner = WaveRunner(loop)
    report = SimpleNamespace(waves=1)

    wave_session = runner._make_wave_session(session, session.task, "种子", report)

    assert wave_session.messages, "the seed is the wave's first message"
    assert wave_session.messages[0].synthetic is True
