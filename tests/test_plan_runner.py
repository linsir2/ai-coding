"""Plan runner (R1-1, TDD): plans become executable budget allocations.

A plan is a todo list; a run executes open items in dependency order, one
bounded turn each, verifying proof commands through the R0 gate that already
lives in the loop.  Every test below uses stub loops — no model, no shell —
except where the contract demands the real machinery (policy swap, AC set).
"""

from types import SimpleNamespace

import pytest

from ai_coding.config.models import AIConfig
from ai_coding.core.plan_runner import STEP_HEAD, PlanRunner
from ai_coding.core.turn_policy import TurnPolicy, TurnPolicyConfig
from ai_coding.domain.plan_delivery import StepDelivery
from ai_coding.domain.session import SessionData
from ai_coding.domain.task import TaskState


def _session_with(*contents, verifies=()):
    """A session whose task holds one todo per content string."""
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    task = TaskState(goal="plan")
    for index, content in enumerate(contents):
        item = task.add(content)
        if index < len(verifies) and verifies[index]:
            item.verify = verifies[index]
    session.task = task
    return session


class _RecordingCloser:
    """Records the armed flag at every parent save (11-plan T4 durability)."""

    def __init__(self) -> None:
        self.armed_at_save: list[bool] = []

    def save(self, session) -> None:
        task = getattr(session, "task", None)
        self.armed_at_save.append(bool(getattr(task, "armed", False)))


class _StubLoop:
    """Stands in for AgentLoop: records prompts, plays a script per call."""

    def __init__(self, script, step_budget=8, *, workspace=".", extra_workspace_roots=()):
        self.settings = SimpleNamespace(
            plan_step_max_iterations=step_budget,
            plan_max_step_turns=0,
            stop_policy="todo_aware",
        )
        self.policy = TurnPolicy(TurnPolicyConfig(max_iterations=100))
        self._script = list(script)
        self.calls = []  # (text, policy_max_iterations_at_call)
        self.step_sessions: list = []
        self.restored = True
        self.closer = _RecordingCloser()
        # W2/R02: the runner's own workspace gate asks the same reachability
        # question the plan gates ask, so the loop has to carry the same
        # answers.
        self.workspace = workspace
        self.extra_workspace_roots = tuple(extra_workspace_roots)

    async def process_input(self, session, text, on_delta=None, observer=None):
        session.turn_count += 1
        self.calls.append((text, self.policy.config.max_iterations))
        self.step_sessions.append(session)
        action = self._script.pop(0) if self._script else "complete"
        if action == "complete":
            for item in session.task.open_items():
                # A blocked item is open but not claimable: the model's "done"
                # must land on the item it actually worked on.
                if item.status == "blocked":
                    continue
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
        elif action == "ignore":
            pass
        return "step done"


def test_config_defaults_step_budget_to_sixty():
    assert AIConfig().plan_step_max_iterations == 60


def test_step_delivery_round_trips_through_the_session():
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    assert "stepDeliveries" not in session.to_dict()
    session.step_deliveries = [
        StepDelivery(step_id="1", statement="fix it", status="completed",
                     attempts=1, ac=[{"id": "AC-1", "passed": True}],
                     summary="fixed", turns=(3, 3)),
    ]
    data = session.to_dict()
    assert data["stepDeliveries"][0]["stepId"] == "1"
    assert SessionData.from_dict(data).step_deliveries == session.step_deliveries


@pytest.mark.asyncio
async def test_run_executes_each_step_in_its_own_bounded_turn():
    session = _session_with("fix bug", "add tests")
    loop = _StubLoop(["complete", "complete"])
    report = await PlanRunner(loop).run(session)

    assert [d.step_id for d in report.deliveries] == ["1", "2"]
    assert all(d.status == "completed" for d in report.deliveries)
    assert len(loop.calls) == 2
    # Each step runs in its own turn, scoped to its own item...
    assert "fix bug" in loop.calls[0][0] and "add tests" not in loop.calls[0][0]
    assert "只做这一步" in loop.calls[0][0]
    # ...under the step budget, not the loop's own ceiling...
    assert [budget for _, budget in loop.calls] == [8, 8]
    # ...and the loop's own policy is restored afterwards.
    assert loop.policy.config.max_iterations == 100
    assert session.task.items[0].status == "completed"


@pytest.mark.asyncio
async def test_items_approved_through_present_plan_run_to_completion():
    """P1 end to end: the data flow that replaced the planning-phase todo list.

    Approval writes the submitted items onto the task; the runner consumes
    exactly those, and an item's ``verify`` becomes its acceptance contract
    (item 1 passes through the contract, item 2 through the model's claim).
    """
    from ai_coding.security.user_prompt import UserAnswer
    from ai_coding.tools.base import ToolContext
    from ai_coding.tools.present_plan_tool import PresentPlanTool

    class _Approve:
        async def ask(self, question, options=(), *, detail="", intent="", data=None):
            return UserAnswer(answered=True, text='{"choice":"approve"}')

    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    session.task = TaskState(goal="设计酒店系统")  # begin_plan's shape: goal, no items

    result = await PresentPlanTool(_Approve()).execute(
        {
            "plan": "# 计划",
            "items": [
                {"content": "建目录", "verify": "true"},
                {"content": "写 db.py"},
            ],
        },
        # R03: the tool is plan-only; this path approves through the real tool.
        ToolContext(
            task=session.task, workspace="/tmp/ws", session_id="s", mode="plan"
        ),
    )
    assert result.data == {"approved": True}

    loop = _StubLoop(["verify-pass", "complete"])
    report = await PlanRunner(loop).run(session)

    assert [d.step_id for d in report.deliveries] == ["1", "2"]
    assert all(d.status == "completed" for d in report.deliveries)
    assert [item.content for item in session.task.items] == ["建目录", "写 db.py"]
    assert all(item.status == "completed" for item in session.task.items)


@pytest.mark.asyncio
async def test_run_respects_dependency_order():
    session = _session_with("first", "second needs first")
    session.task.items[1].depends_on = ["1"]
    loop = _StubLoop(["complete", "complete"])
    await PlanRunner(loop).run(session)
    assert "first" in loop.calls[0][0]
    assert "second needs first" in loop.calls[1][0]


@pytest.mark.asyncio
async def test_approval_maps_depends_on_and_the_run_waits_on_the_blocker():
    """R09 end to end: approval turns indices into ids, and a dependent step
    stays out of the round while its predecessor is unresolved — without the
    mapping the runner would start it against an unfinished prerequisite."""
    from ai_coding.security.user_prompt import UserAnswer
    from ai_coding.tools.base import ToolContext
    from ai_coding.tools.present_plan_tool import PresentPlanTool

    class _Approve:
        async def ask(self, question, options=(), *, detail="", intent="", data=None):
            return UserAnswer(answered=True, text='{"choice":"approve"}')

    session = SessionData(
        session_id="s", title="", created_time="t", last_access_time="t"
    )
    session.task = TaskState(goal="依赖链")
    result = await PresentPlanTool(_Approve()).execute(
        {
            "plan": "# 计划",
            "items": [
                {"content": "first"},
                {"content": "second needs first", "depends_on": [0]},
            ],
        },
        ToolContext(
            task=session.task, workspace="/tmp/ws", session_id="s", mode="plan"
        ),
    )
    assert result.data == {"approved": True}
    assert session.task.items[1].depends_on == ["1"]

    loop = _StubLoop(["ignore"])  # the first step never claims done
    report = await PlanRunner(loop).run(session)

    assert len(loop.calls) == 1  # the dependent step must not start
    assert report.stalled == ["1"]
    assert report.stopped.startswith("deadlock")
    assert session.task.items[1].status == "pending"


@pytest.mark.asyncio
async def test_verify_failure_triggers_one_repair_run_then_blocks():
    session = _session_with("fix it", verifies=["pytest tests/ -q"])
    loop = _StubLoop(["verify-fail", "verify-fail"])
    report = await PlanRunner(loop).run(session)

    # One repair run carrying the failure output, then blocked — never a third.
    assert len(loop.calls) == 2
    assert "245" in loop.calls[1][0]
    assert report.blocked == ["1"]
    assert session.task.items[0].status == "blocked"
    assert "245" in (session.task.items[0].evidence or "")
    assert report.deliveries[0].status == "blocked"
    assert report.deliveries[0].attempts == 2


@pytest.mark.asyncio
async def test_a_blocked_step_does_not_stop_independent_steps():
    """r22: one failed proof used to end the whole run.

    The measured hotel plan verified one step with a wrong path, blocked it,
    and stopped — steps that never depended on it were never touched.  The
    run now records the block and continues with the ready items.
    """
    session = _session_with("first", "second", verifies=["pytest -q"])
    loop = _StubLoop(["verify-fail", "verify-fail", "complete"])
    report = await PlanRunner(loop).run(session)

    assert report.blocked == ["1"]
    assert report.completed == ["2"]
    assert len(loop.calls) == 3
    assert session.task.items[0].status == "blocked"
    assert session.task.items[1].status == "completed"


@pytest.mark.asyncio
async def test_verify_failure_repaired_on_second_attempt_completes():
    session = _session_with("fix it", verifies=["pytest tests/ -q"])
    loop = _StubLoop(["verify-fail", "verify-pass"])
    report = await PlanRunner(loop).run(session)

    assert report.completed == ["1"]
    assert report.deliveries[0].attempts == 2
    assert session.task.items[0].status == "completed"


@pytest.mark.asyncio
async def test_items_without_verify_trust_a_completed_turn():
    session = _session_with("write docs")
    loop = _StubLoop(["complete"])
    report = await PlanRunner(loop).run(session)
    assert report.completed == ["1"]


@pytest.mark.asyncio
async def test_unfinished_unverifiable_step_stays_open_and_the_run_moves_on():
    """No verify command, model marked nothing: leave it open (retryable next
    run) — and since 19 §3.4-3 (Q5=A) the run records the stall and continues
    to the next ready item instead of stopping the whole run."""
    session = _session_with("write docs", "next step")
    loop = _StubLoop(["ignore", "ignore"])
    report = await PlanRunner(loop).run(session)

    assert session.task.items[0].status == "pending"
    assert report.stalled == ["1", "2"]
    assert len(loop.calls) == 2  # the next ready step ran anyway
    assert report.stopped is None


@pytest.mark.asyncio
async def test_session_acceptance_is_set_per_step_and_restored():
    """The R0 gate sees exactly this step's contracts — never the user's own
    list, which is saved aside and restored afterwards."""
    from ai_coding.domain.acceptance import AcceptanceItem

    session = _session_with("fix it", verifies=["pytest tests/ -q"])
    session.acceptance = [AcceptanceItem(id="user-AC", statement="u", verify="true")]
    seen = []

    class _SpyLoop(_StubLoop):
        async def process_input(self, session, text, on_delta=None, observer=None):
            seen.append([item.id for item in session.acceptance])
            return await super().process_input(session, text, on_delta, observer)

    await PlanRunner(_SpyLoop(["verify-pass"])).run(session)
    assert seen == [["plan-1"]]
    assert [item.id for item in session.acceptance] == ["user-AC"]


@pytest.mark.asyncio
async def test_empty_plan_reports_nothing_to_do():
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    loop = _StubLoop([])
    report = await PlanRunner(loop).run(session)
    assert report.deliveries == [] and loop.calls == []
    assert report.stopped == "no-open-items"


@pytest.mark.asyncio
async def test_prior_deliveries_are_summarised_into_later_steps():
    session = _session_with("step one", "step two")
    loop = _StubLoop(["complete", "complete"])
    await PlanRunner(loop).run(session)
    # M1: prior progress travels in the step session's seed, not the prompt —
    # completed items are absent from the open list, so a step learns they're
    # done without re-listing them (re-listing would make the model redo work).
    seed_2 = loop.step_sessions[1].messages[0].content
    assert "step two" in seed_2
    assert "step one" not in seed_2


@pytest.mark.asyncio
async def test_a_failed_step_still_leaves_a_replay_marker():
    """A step that dies mid-turn must be visible, not just in the book.

    The runner-error path used to write a blocked receipt and no bubble, so
    the failure existed in the data and not in the session transcript — and
    nothing tested it.
    """

    class _DyingLoop:
        settings = SimpleNamespace(plan_step_max_iterations=8, plan_max_step_turns=0)

        def __init__(self):
            self.policy = TurnPolicy(TurnPolicyConfig(max_iterations=8))

        async def process_input(self, session, text, on_delta=None, observer=None):
            raise RuntimeError("engine exploded")

    session = SessionData(session_id="s", title="", created_time="t",
                          last_access_time="t")
    task = TaskState(goal="g")
    task.add("改一下 loader")
    session.task = task

    report = await PlanRunner(_DyingLoop()).run(session)

    assert [entry.status for entry in report.deliveries] == ["blocked"]
    # the receipt must reach the parent's book too, or the failure never
    # reaches the user at all
    assert [entry.step_id for entry in session.step_deliveries] == ["1"]
    assert session.step_deliveries[0].status == "blocked"
    bubbles = [m for m in session.messages if m.role == "user"]
    assert len(bubbles) == 1 and bubbles[0].content.startswith(STEP_HEAD)
    assert "engine exploded" in session.messages[-1].content


@pytest.mark.asyncio
async def test_the_step_marker_is_bookkeeping_not_the_operators_words():
    """D6: the replay marker is the harness writing about the run.

    Stored unstamped, it was indistinguishable from something the operator
    typed: request assembly kept it as human history, /undo and compaction
    treated it as such, and the live card drew "只做这一步，不要做别的：" as a
    user bubble.  The receipt's summary is the only part a human reads.
    """
    loop = _StubLoop(["complete"])
    session = _session_with("实现某功能")

    await PlanRunner(loop).run(session)

    markers = [
        message
        for message in session.messages
        if message.role == "user" and (message.content or "").startswith(STEP_HEAD)
    ]
    assert markers, "the step left no replay marker"
    assert all(message.synthetic for message in markers)
    summaries = [message for message in session.messages if message.role == "assistant"]
    assert summaries and not any(message.synthetic for message in summaries)


@pytest.mark.asyncio
async def test_arm_is_durable_before_rounds_and_cleared_after():
    """11-plan T4 (audit addendum): the run arms the task and persists it
    BEFORE the first round (crash point ①), counts the round, and the
    disarm is persisted on the way out (crash point ②/§4.2)."""
    from ai_coding.core.plan_runner import PlanRunner

    loop = _StubLoop(["complete"])
    session = _session_with("实现某功能")

    report = await PlanRunner(loop).run(session)

    saves = loop.closer.armed_at_save
    assert saves[0] is True, "task must be armed on disk before the first round"
    assert saves[-1] is False, "disarm must be persisted on the way out"
    assert session.task.rounds_run == 1
    assert report.completed == ["1"]


@pytest.mark.asyncio
async def test_a_fiat_only_run_still_backfills_and_leaves_no_arm():
    """The screenshot case stays whole through the driver: everything is
    already done, the loop's last pass backfills receipts, and the task is
    not left armed."""
    from ai_coding.core.plan_runner import PlanRunner

    loop = _StubLoop([])
    session = _session_with("one", "two")
    session.task.items[0].status = "completed"
    session.task.items[1].status = "completed"

    report = await PlanRunner(loop).run(session)

    assert loop.calls == []
    assert len(report.deliveries) == 2
    assert loop.closer.armed_at_save[-1] is False
    assert session.task.armed is False


# ---- E2: the receipt bubbles travel on the wire too --------------------------
#
# The runner appends two bubbles to the parent session (the step head and the
# one-line summary) so /resume can replay them.  Until now nothing on the wire
# carried them: a connected TUI only learned of them after reopening the
# session, while a reloaded view showed them -- live and replay disagreed.
# A reload at run end is not the fix (handlers.py:733: the transcript is
# cumulative, and a reload would wipe the step's own streamed rows); the
# receipt travels as its own event.


@pytest.mark.asyncio
async def test_a_step_receipt_reaches_a_wired_observer():
    from ai_coding.core.events import PlanStepReceipt, RecordingObserver

    session = _session_with("fix bug", "add tests")
    loop = _StubLoop(["complete", "complete"])
    # A loop-shaped observer (on_event), not a bare callable: the callable
    # shape these tests used to pass was exactly how the production wiring
    # stayed dead -- the real bridge has on_event and no __call__.
    observer = RecordingObserver()

    await PlanRunner(loop).run(session, observer=observer)

    receipts = observer.of_type(PlanStepReceipt)
    assert len(receipts) == 2, f"one receipt per step, got {len(receipts)}"
    assert "fix bug" in receipts[0].summary
    # The step head stays in the parent's book as the replay marker, stamped
    # synthetic (D6): the wire carries only the line a human reads.
    markers = [
        message
        for message in session.messages
        if message.role == "user" and (message.content or "").startswith(STEP_HEAD)
    ]
    assert markers and all(message.synthetic for message in markers)


@pytest.mark.asyncio
async def test_a_crashed_step_receipts_on_the_wire_too():
    from ai_coding.core.events import PlanStepReceipt, RecordingObserver

    session = _session_with("explode")
    loop = _StubLoop(["explode"])
    observer = RecordingObserver()

    await PlanRunner(loop).run(session, observer=observer)

    assert observer.of_type(PlanStepReceipt), (
        "the failed path writes the same two bubbles; the wire must see them too"
    )


@pytest.mark.asyncio
async def test_a_runner_without_an_observer_records_as_before():
    session = _session_with("fix bug")
    loop = _StubLoop(["complete"])

    report = await PlanRunner(loop).run(session)

    assert report.deliveries[0].status == "completed"
    assert any("只做这一步" in (m.content or "") for m in session.messages)


# ---- R06: the runner's own mutations reach the wire too ----------------------


class _TaskSnapshots:
    """Records ``(armed, statuses)`` **at event time**.

    TaskUpdated shares the live task object (the bridge serializes it
    synchronously), so post-run inspection would only ever see the last state —
    this observer takes the snapshot the wire would have taken.
    """

    def __init__(self) -> None:
        self.armed: list[bool] = []
        self.statuses: list[list[str]] = []

    def on_event(self, event) -> None:
        from ai_coding.core.events import TaskUpdated

        if isinstance(event, TaskUpdated):
            self.armed.append(bool(event.task.armed))
            self.statuses.append([item.status for item in event.task.items])


@pytest.mark.asyncio
async def test_the_runner_emits_task_updated_on_arm_and_disarm():
    """R06 (F-47): arm/disarm happen outside any tool call; with nothing
    emitting TaskUpdated for them, a live card froze at approval time."""
    session = _session_with("fix bug")
    observer = _TaskSnapshots()

    await PlanRunner(_StubLoop(["complete"])).run(session, observer=observer)

    assert observer.armed, "the run's arm/disarm mutations must reach the wire"
    assert observer.armed[0] is True
    assert observer.armed[-1] is False


@pytest.mark.asyncio
async def test_settle_emits_task_updated_when_a_verified_step_completes():
    session = _session_with("fix it", verifies=["pytest tests/ -q"])
    observer = _TaskSnapshots()

    await PlanRunner(_StubLoop(["verify-pass"])).run(session, observer=observer)

    assert any("completed" in statuses for statuses in observer.statuses), (
        "the settle mutation (verify passed) must be visible live"
    )


@pytest.mark.asyncio
async def test_settle_emits_task_updated_when_a_verified_step_blocks():
    session = _session_with("fix it", verifies=["pytest tests/ -q"])
    observer = _TaskSnapshots()

    await PlanRunner(_StubLoop(["verify-fail", "verify-fail"])).run(
        session, observer=observer
    )

    assert any("blocked" in statuses for statuses in observer.statuses), (
        "the blocked verdict must be visible live"
    )


@pytest.mark.asyncio
async def test_a_dying_step_emits_task_updated_blocked():
    class _DyingLoop:
        settings = SimpleNamespace(plan_step_max_iterations=8, plan_max_step_turns=0)

        def __init__(self):
            self.policy = TurnPolicy(TurnPolicyConfig(max_iterations=8))

        async def process_input(self, session, text, on_delta=None, observer=None):
            raise RuntimeError("engine exploded")

    session = SessionData(session_id="s", title="", created_time="t",
                          last_access_time="t")
    task = TaskState(goal="g")
    task.add("改一下 loader")
    session.task = task
    observer = _TaskSnapshots()

    await PlanRunner(_DyingLoop()).run(session, observer=observer)

    assert any("blocked" in statuses for statuses in observer.statuses), (
        "a runner error writes blocked into the task; the wire must see it"
    )


@pytest.mark.asyncio
async def test_a_plan_naming_unreachable_paths_never_runs(tmp_path):
    """W2/R02: the runner's own workspace gate, before the first step.

    This is the gate that makes the refusal structural rather than
    advisory: every way into a run -- the command, the front end's
    auto-dispatch, the boot resume -- comes through this method, so a plan
    the file tools cannot execute never reaches a turn.
    """
    session = _session_with("写代码到 /data/x/app.py")
    loop = _StubLoop(["complete"], workspace=str(tmp_path))

    report = await PlanRunner(loop).run(session)

    assert report.stopped is not None
    assert "/data/x/app.py" in report.stopped
    assert loop.calls == [], "the plan ran despite naming an unreachable path"
    assert session.task.armed is False, "the refused plan stayed armed"
    assert session.task.armed_profile == ""


@pytest.mark.asyncio
async def test_the_runner_gate_withdraws_the_approval_that_armed_the_plan(tmp_path):
    """W2/R02: approval is what arms the plan, so refusing it must withdraw
    the approval.  An armed plan the runner refuses anyway would still be on
    disk, and the boot resume would pick it up and run it."""
    session = _session_with("写代码到 /data/x/app.py")
    session.task.armed = True
    session.task.armed_profile = "plan"
    loop = _StubLoop(["complete"], workspace=str(tmp_path))

    report = await PlanRunner(loop).run(session)

    assert report.stopped is not None
    assert session.task.armed is False
    assert session.task.armed_profile == ""
    assert loop.closer.armed_at_save == [False], (
        "the withdrawal must reach the disk, or the boot resume finds it armed"
    )


@pytest.mark.asyncio
async def test_the_runner_gate_honours_extra_roots(tmp_path):
    """W2/R02: one roots pipeline.  A directory the operator declared is
    reachable, so the runner's gate must let the plan through."""
    other = tmp_path / "sibling"
    other.mkdir()
    session = _session_with(f"看下 {other}/app.py")
    loop = _StubLoop(["complete"], workspace=str(tmp_path), extra_workspace_roots=(other,))

    report = await PlanRunner(loop).run(session)

    assert report.stopped is None
    assert len(loop.calls) == 1, "the plan did not run"
