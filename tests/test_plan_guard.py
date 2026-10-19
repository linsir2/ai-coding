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

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.turn_policy import PLAN_DISMISSED_NOTE
from ai_coding.domain.loop import StepResult, TurnStatus
from ai_coding.domain.message import ToolCallRef, ToolResult
from ai_coding.domain.task import TaskState
from ai_coding.security.approval import AutoApproveCallback
from ai_coding.service.session_service import SessionService
from ai_coding.tools.base import BaseTool
from ai_coding.tools.registry import ToolRegistry


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
