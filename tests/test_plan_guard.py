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
from ai_coding.domain.loop import StepResult, TurnStatus
from ai_coding.domain.message import ToolCallRef, ToolResult
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


def _loop(engine: ChatEngine, sessions: SessionService) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(_PresentPlan())
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
