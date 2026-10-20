"""stage 4 r2: the plan submission guard.

Measured twice (7ad86804, 1fd3be1e): the model answers an exploration request
with a prose analysis and never calls present_plan -- no review card, no
execution, a silent COMPLETED.  The guard is dsh's turn-stopping idea at our
post-step seam: one remedy nudge while the turn still has budget, and a
terminal note when even the remedy is ignored.

Harness: self-contained, shaped after tests/test_agent_loop_react.py (the
repo's convention for loop-level files).
"""

import pytest

from ai_coding.ai.engine import ChatEngine, EngineError
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.turn_policy import PLAN_DISMISSED_NOTE, TurnPolicy, TurnPolicyConfig
from ai_coding.domain.loop import StepResult, TurnStatus
from ai_coding.domain.message import ToolCallRef, ToolResult
from ai_coding.domain.task import TaskState
from ai_coding.security.approval import AutoApproveCallback
from ai_coding.service.session_service import SessionService
from ai_coding.tools.base import BaseTool
from ai_coding.tools.registry import ToolRegistry
from ai_coding.tools.todo_write_tool import TodoWriteTool


class _PresentPlan(BaseTool):
    """The submission tool, by name -- the guard keys on the name only."""

    name = "present_plan"
    parallel_safe = False
    mutates_world = False

    @property
    def description(self) -> str:
        return "submit the plan for review"

    @property
    def params_json_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, params, ctx=None) -> ToolResult:
        return ToolResult(success=True, output="计划已批准")


class _DismissedPlan(_PresentPlan):
    """The same call, dismissed: esc on the card -- the loop must end the turn
    right after this batch (R08)."""

    async def execute(self, params, ctx=None) -> ToolResult:
        return ToolResult(
            success=True,
            output="已搁置",
            data={"approved": False, "dismissed": True},
        )


class _ScriptedEngine(ChatEngine):
    def __init__(self, steps: list) -> None:
        self._steps = list(steps)

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        if not self._steps:
            raise AssertionError("engine called more times than the script allows")
        item = self._steps.pop(0)
        if on_delta is not None and item.text:
            on_delta(item.text)
        return item

    async def aclose(self) -> None:
        return None


def _text(text: str) -> StepResult:
    return StepResult(text=text, finish_reason="stop")


def _call(call_id: str, name: str) -> StepResult:
    return StepResult(
        text=None,
        tool_calls=[ToolCallRef(id=call_id, name=name, arguments="{}")],
        finish_reason="tool_calls",
    )


def _loop(
    engine: ChatEngine, sessions: SessionService, tool: BaseTool | None = None
) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(tool if tool is not None else _PresentPlan())
    return AgentLoop(
        engine,
        sessions,
        registry,
        approval=AutoApproveCallback(),
        settings=AIConfig(),
        workspace=".",
    )


def _plan_session(sessions: SessionService):
    session = sessions.create()
    session.mode, session.readonly = "plan", True
    return session


def _plan_reminders(session) -> list[str]:
    return [
        m.content or ""
        for m in session.messages
        if m.synthetic and "present_plan" in (m.content or "")
    ]


@pytest.mark.asyncio
async def test_a_bare_text_tail_gets_one_plan_reminder_then_recovers(tmp_path):
    engine = _ScriptedEngine(
        [_text("分析完毕。"), _call("c1", "present_plan"), _text("计划已提交。")]
    )
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    session = _plan_session(sessions)

    await loop.process_input(session, "探索一下记忆层怎么设计的")

    assert len(_plan_reminders(session)) == 1
    outcome = loop.last_outcome
    assert outcome is not None and outcome.status is TurnStatus.COMPLETED
    assert outcome.note == ""  # the remedy landed: no terminal notice


@pytest.mark.asyncio
async def test_ignoring_the_remedy_ends_with_the_note(tmp_path):
    engine = _ScriptedEngine([_text("分析完毕。"), _text("真的分析完毕。")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    session = _plan_session(sessions)

    await loop.process_input(session, "探索一下记忆层怎么设计的")

    assert len(_plan_reminders(session)) == 1
    outcome = loop.last_outcome
    assert outcome is not None and outcome.status is TurnStatus.COMPLETED
    assert "未提交计划" in outcome.note


@pytest.mark.asyncio
async def test_a_submitted_plan_never_hears_from_the_guard(tmp_path):
    engine = _ScriptedEngine([_call("c1", "present_plan"), _text("完成。")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    session = _plan_session(sessions)

    await loop.process_input(session, "给计划")

    assert _plan_reminders(session) == []
    assert loop.last_outcome is not None and loop.last_outcome.note == ""


@pytest.mark.asyncio
async def test_a_plain_turn_never_hears_from_the_guard(tmp_path):
    engine = _ScriptedEngine([_text("好的。")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    session = sessions.create()

    await loop.process_input(session, "你好")

    assert _plan_reminders(session) == []
    assert loop.last_outcome is not None and loop.last_outcome.note == ""


@pytest.mark.asyncio
async def test_a_dismissed_review_ends_the_turn_at_the_batch(tmp_path):
    """R08 (dsh): dismiss = "not now" -- the turn ends right after the batch
    (the engine is never asked again), no guard nudge, and the operator gets
    the terminal note instead of a silent end."""
    engine = _ScriptedEngine([_call("c1", "present_plan")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions, tool=_DismissedPlan())
    session = _plan_session(sessions)

    await loop.process_input(session, "给计划")

    assert _plan_reminders(session) == []
    outcome = loop.last_outcome
    assert outcome is not None and outcome.status is TurnStatus.COMPLETED
    assert outcome.reason == "plan dismissed"
    assert outcome.note == PLAN_DISMISSED_NOTE


@pytest.mark.asyncio
async def test_a_read_only_turn_does_not_ask_for_a_plan(tmp_path):
    """R08: /readonly alone is not planning -- the submission guard must not
    misfire, and the quiet finish costs one step (no remedy round)."""
    engine = _ScriptedEngine([_text("分析完毕。")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    session = sessions.create()
    session.readonly = True

    await loop.process_input(session, "看看这个")

    assert _plan_reminders(session) == []
    outcome = loop.last_outcome
    assert outcome is not None and outcome.status is TurnStatus.COMPLETED
    assert outcome.note == ""


# ---- B (audit fix): the dismissal branch is the only place a dismissal can
#      meet an armed task, so it is the only place that has to disarm it. ------


class _ArmingDismissedPlan(_PresentPlan):
    """A dismissal that arrives while a plan from this turn is already armed.

    With the B gate in place the real tool refuses a second submission, so this
    fixture stands in for the state the loop must still handle correctly: the
    dismissal flag and an armed task in the same batch (a keep that the model
    turned into a fresh submission, an earlier approval plus a later esc)."""

    def __init__(self) -> None:
        self._armed = False

    async def execute(self, params, ctx=None) -> ToolResult:
        if not self._armed:
            self._armed = True
            ctx.task.armed = True
            ctx.task.armed_profile = "plan"
            ctx.task.add("第一步")
        return ToolResult(
            success=True,
            output="已搁置",
            data={"approved": False, "dismissed": True},
        )


@pytest.mark.asyncio
async def test_a_dismissal_disarms_the_plan_it_ends_the_turn_over(tmp_path):
    """B: the dismissal note says nothing ran, so nothing may stay armed.  An
    armed plan the user just waved away would otherwise be picked up by the
    boot resume or a stray /plan run."""
    engine = _ScriptedEngine([_call("c1", "present_plan")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions, tool=_ArmingDismissedPlan())
    session = _plan_session(sessions)

    await loop.process_input(session, "给计划")

    outcome = loop.last_outcome
    assert outcome is not None and outcome.reason == "plan dismissed"
    assert session.task is not None
    assert session.task.armed is False
    assert session.task.armed_profile == ""


@pytest.mark.asyncio
async def test_a_dismissal_leaves_a_goal_task_armed(tmp_path):
    """Only the plan profile is the review's to disarm -- a goal task belongs
    to the goal runner, which has its own lifecycle."""
    engine = _ScriptedEngine([_call("c1", "present_plan")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions, tool=_DismissedPlan())
    session = _plan_session(sessions)
    session.task = TaskState()
    session.task.arm_goal("把发布跑通")

    await loop.process_input(session, "给计划")

    assert session.task.armed is True
    assert session.task.armed_profile == "goal"


# ---- W2 (boot 洞): an approval belongs to the turn that carried it ---------


class _ApprovingPlan(_PresentPlan):
    """The plan is approved: the task is armed, and the review said yes.

    Only the plan profile is armed here -- a goal task has its own arming
    path, and this fixture must not take it over.
    """

    async def execute(self, params, ctx=None) -> ToolResult:
        task = getattr(ctx, "task", None)
        if task is not None and getattr(task, "armed_profile", "") != "goal":
            task.armed = True
            task.armed_profile = "plan"
            task.add("第一步")
            if ctx.note_plan_approved is not None:
                ctx.note_plan_approved()
        return ToolResult(success=True, output="计划已批准", data={"approved": True})


class _FailingEngine(_ScriptedEngine):
    """A turn that dies after the approval: the engine fails on the call
    that comes after the plan was submitted."""

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        if not self._steps:
            raise EngineError("boom")
        item = self._steps.pop(0)
        if isinstance(item, Exception):
            raise item
        if on_delta is not None and item.text:
            on_delta(item.text)
        return item


@pytest.mark.asyncio
async def test_a_turn_that_dies_after_the_approval_withdraws_it(tmp_path):
    """W2: the approval belongs to the turn that carried it.

    The front end already refuses to start a run from a turn that did not end
    cleanly (``shouldDispatchPlanRun``), but the backend left the plan armed
    on disk -- and the boot resume reads the disk, so the same turn said
    "do not run" in one place and "run it" in the other.  The plan is
    withdrawn, so the boot resume has nothing to pick up.
    """
    engine = _FailingEngine([_call("c1", "present_plan"), EngineError("boom")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions, tool=_ApprovingPlan())
    session = _plan_session(sessions)

    await loop.process_input(session, "给计划")

    outcome = loop.last_outcome
    assert outcome is not None and outcome.status is TurnStatus.ERROR
    assert session.task is not None
    assert session.task.armed is False
    assert session.task.armed_profile == ""


@pytest.mark.asyncio
async def test_a_clean_turn_keeps_the_approval(tmp_path):
    """The other side: a turn that ended cleanly is what the approval was
    for, and the run it authorises still has to happen."""
    engine = _ScriptedEngine([_call("c1", "present_plan"), _text("完成。")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions, tool=_ApprovingPlan())
    session = _plan_session(sessions)

    await loop.process_input(session, "给计划")

    outcome = loop.last_outcome
    assert outcome is not None and outcome.status is TurnStatus.COMPLETED
    assert session.task is not None
    assert session.task.armed is True
    assert session.task.armed_profile == "plan"


@pytest.mark.asyncio
async def test_a_goal_task_survives_a_turn_that_dies(tmp_path):
    """A goal's arm is the goal runner's business: it keeps running across
    turns by design, so a turn that dies must not withdraw it."""
    engine = _FailingEngine([_call("c1", "present_plan"), EngineError("boom")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions, tool=_ApprovingPlan())
    session = _plan_session(sessions)
    session.task = TaskState()
    session.task.arm_goal("把发布跑通")

    await loop.process_input(session, "给计划")

    assert session.task.armed is True
    assert session.task.armed_profile == "goal"


@pytest.mark.asyncio
async def test_a_plan_step_whose_turn_was_cut_off_does_not_withdraw_the_plan(
    tmp_path,
):
    """W2: the dangerous neighbour of the withdrawal.

    A plan step runs on the *shared* task and is often cut short by the
    step budget -- and a step that ends on ``max_iterations`` is not a turn
    that went wrong, it is a step that ran out of room.  If the withdrawal
    reached those, the driver would see the plan unarmed between rounds and
    stop after the first step (the driver re-checks ``task.armed`` between
    rounds).

    A step never approves a plan (``present_plan`` is plan-mode only and a
    step session is not in plan mode), so the withdrawal must not touch it.
    """
    from ai_coding.core.plan_runner import PlanRunner

    class _BudgetLoop:
        """A loop whose step turns run out of iterations."""

        def __init__(self) -> None:
            self.settings = AIConfig()
            self.policy = TurnPolicy(TurnPolicyConfig(max_iterations=1))
            self.calls = 0

        async def process_input(self, session, text, on_delta=None, observer=None):
            self.calls += 1
            for item in session.task.open_items():
                if not item.is_terminal():
                    item.status = "completed"
                    break
            return "step done"

    loop = _BudgetLoop()
    session = SessionService(tmp_path / "sessions").create()
    session.task = TaskState(goal="plan")
    session.task.add("第一步")
    session.task.add("第二步")

    report = await PlanRunner(loop).run(session)

    assert report.stopped is None
    assert [item.status for item in session.task.items] == ["completed", "completed"]
    assert session.task.armed is False


@pytest.mark.asyncio
async def test_a_real_plan_step_cut_off_by_the_budget_keeps_the_plan_armed(
    tmp_path,
):
    """W2: the same property, through the real loop.

    The stub above cannot see the turn's close, which is where the
    withdrawal lives.  Here a real ``AgentLoop`` runs a plan whose steps run
    out of iterations -- the ordinary way a step ends -- and the plan must
    still finish: the driver checks ``task.armed`` between rounds, so a
    withdrawal that reached the steps would stop the plan after the first
    one.
    """
    from ai_coding.core.plan_runner import PlanRunner

    class _StepEngine(ChatEngine):
        """Every model call wants a tool; the budget is what ends the turn."""

        def __init__(self) -> None:
            self.calls = 0

        async def step(self, messages, *, tools, instructions=None, on_delta=None):
            self.calls += 1
            return StepResult(
                text=None,
                tool_calls=[
                    ToolCallRef(
                        id=f"c{self.calls}", name="todo_write",
                        arguments='{"action":"list"}',
                    )
                ],
                finish_reason="tool_calls",
            )

        async def aclose(self) -> None:
            return None

    registry = ToolRegistry()
    registry.register(TodoWriteTool())
    sessions = SessionService(tmp_path / "sessions")
    loop = AgentLoop(
        _StepEngine(),
        sessions,
        registry,
        settings=AIConfig(
            stop_policy="natural",
            max_tool_iterations=1,
            plan_step_max_iterations=1,
        ),
        workspace=".",
    )
    session = sessions.create()
    session.task = TaskState(goal="plan")
    session.task.add("第一步")
    session.task.add("第二步")

    report = await PlanRunner(loop).run(session)

    # Both steps ran: the plan was not cut short by a withdrawn arm.
    assert len(report.deliveries) == 2
    assert session.task.armed is False
