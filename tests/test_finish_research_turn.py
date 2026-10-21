"""N1: the research exit at the loop seam.

A plan-mode turn has two exits.  ``present_plan`` submits a plan for review;
``finish_research`` closes a research question with a conclusion.  The second
one is what the measured malformation needed: a research request was forced
into a plan whose items described work already done and whose verify commands
were invented to match (15 号稿 D1/D2).

The seam under test is the same one the dismissal branch uses: a successful
exit ends the turn at the batch, with a terminal note and no second model
call.
"""

import json

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.turn_policy import PLAN_RESEARCH_NOTE
from ai_coding.domain.loop import StepResult, TurnStatus
from ai_coding.domain.message import ToolCallRef, ToolResult
from ai_coding.security.approval import AutoApproveCallback
from ai_coding.security.user_prompt import UserAnswer
from ai_coding.service.session_service import SessionService
from ai_coding.tools.base import BaseTool
from ai_coding.tools.finish_research_tool import FinishResearchTool
from ai_coding.tools.present_plan_tool import PresentPlanTool
from ai_coding.tools.registry import ToolRegistry


class _FinishResearch(BaseTool):
    """The research exit, by name -- the loop keys on the name only."""

    name = "finish_research"
    parallel_safe = False
    mutates_world = False

    @property
    def description(self) -> str:
        return "close a research task with a conclusion"

    @property
    def params_json_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, params, ctx=None) -> ToolResult:
        return ToolResult(
            success=True,
            output="记忆层是三层。",
            data={"research_finished": True},
        )


class _FailedFinishResearch(_FinishResearch):
    """The same call, refused: the turn must go on and the guard must speak."""

    async def execute(self, params, ctx=None) -> ToolResult:
        return ToolResult(success=False, output="总结不能为空。", error="missing_summary")


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
    registry.register(tool if tool is not None else _FinishResearch())
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


# ---- the two exits, in one batch (the mutual exclusion) ----------------------


class _Dismiss:
    """A human who says "not now" (esc / empty on the review card)."""

    def __init__(self) -> None:
        self.asked = 0

    async def ask(self, question, options=(), *, detail="", intent="", data=None):
        self.asked += 1
        return UserAnswer(answered=False, reason="empty")


class _Approve:
    """A human who approves the plan."""

    def __init__(self) -> None:
        self.asked = 0

    async def ask(self, question, options=(), *, detail="", intent="", data=None):
        self.asked += 1
        return UserAnswer(answered=True, text=json.dumps({"choice": "approve"}))


def _calls(*specs: tuple[str, str, dict]) -> StepResult:
    """One model step that asks for several tools, in the model's own order."""
    return StepResult(
        text=None,
        tool_calls=[
            ToolCallRef(id=cid, name=name, arguments=json.dumps(args))
            for cid, name, args in specs
        ],
        finish_reason="tool_calls",
    )


def _exits_loop(engine: ChatEngine, sessions: SessionService, prompt) -> AgentLoop:
    """A loop whose registry carries the two real exits -- they hold the turn."""
    registry = ToolRegistry()
    registry.register(PresentPlanTool(prompt))
    registry.register(FinishResearchTool())
    return AgentLoop(
        engine,
        sessions,
        registry,
        approval=AutoApproveCallback(),
        settings=AIConfig(),
        workspace=".",
    )


def _tool_result(session, name: str) -> tuple[bool, str]:
    """``(success, text)`` of one tool's result, read off the transcript."""
    for message in reversed(session.messages):
        if message.role == "tool" and message.tool_name == name:
            content = message.content or ""
            return 'ok="false"' not in content, content
    raise AssertionError(f"no tool result for {name}")


_PLAN_CALL = (
    "c_plan",
    "present_plan",
    {"plan": "查记忆层", "items": [{"content": "读代码"}]},
)
_RESEARCH_CALL = ("c_research", "finish_research", {"summary": "记忆层是三层。"})


@pytest.mark.asyncio
async def test_a_dismissed_review_does_not_eat_the_research_conclusion(tmp_path):
    """占位 = 成功: the human's "not now" is not an exit, so the other exit
    stays open.  A dismissal must not swallow the model's conclusion."""
    engine = _ScriptedEngine([_calls(_PLAN_CALL, _RESEARCH_CALL)])
    sessions = SessionService(tmp_path / "sessions")
    loop = _exits_loop(engine, sessions, _Dismiss())
    session = _plan_session(sessions)

    await loop.process_input(session, "看看记忆层怎么设计的")

    ok, text = _tool_result(session, "finish_research")
    assert ok is True, text
    assert "记忆层是三层。" in text
    outcome = loop.last_outcome
    assert outcome is not None and outcome.reason == "research finished"
    assert session.task is not None and session.task.armed is False


@pytest.mark.asyncio
async def test_the_plan_after_a_conclusion_is_refused_and_arms_nothing(tmp_path):
    """The two exits are mutually exclusive: the first one to succeed owns the
    turn.  The plan submission is refused, the human is never asked, and
    nothing is armed."""
    engine = _ScriptedEngine([_calls(_RESEARCH_CALL, _PLAN_CALL)])
    sessions = SessionService(tmp_path / "sessions")
    human = _Approve()
    loop = _exits_loop(engine, sessions, human)
    session = _plan_session(sessions)

    await loop.process_input(session, "看看记忆层怎么设计的")

    ok, text = _tool_result(session, "present_plan")
    assert ok is False, text
    assert "不能再提交计划" in text
    assert human.asked == 0
    assert session.task is not None
    assert session.task.armed is False
    assert session.task.items == []


@pytest.mark.asyncio
async def test_a_conclusion_ends_the_turn_at_the_batch(tmp_path):
    """The conclusion is the deliverable, so the turn ends there: the engine is
    never asked again, and the operator gets the terminal note."""
    engine = _ScriptedEngine([_call("c1", "finish_research")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    session = _plan_session(sessions)

    await loop.process_input(session, "看看记忆层怎么设计的")

    assert _plan_reminders(session) == []
    outcome = loop.last_outcome
    assert outcome is not None and outcome.status is TurnStatus.COMPLETED
    assert outcome.reason == "research finished"
    assert outcome.note == PLAN_RESEARCH_NOTE


@pytest.mark.asyncio
async def test_a_conclusion_arms_nothing(tmp_path):
    """The execution layer must not wake up: no items, no arm, no run."""
    engine = _ScriptedEngine([_call("c1", "finish_research")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    session = _plan_session(sessions)

    await loop.process_input(session, "看看记忆层怎么设计的")

    assert session.task is not None
    assert session.task.armed is False
    assert session.task.items == []


@pytest.mark.asyncio
async def test_a_refused_conclusion_leaves_the_turn_to_the_guard(tmp_path):
    """A failed exit is not an exit: the turn goes on, and the guard still
    offers its one remedy."""
    engine = _ScriptedEngine(
        [_call("c1", "finish_research"), _text("分析完毕。"), _text("真的分析完毕。")]
    )
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions, tool=_FailedFinishResearch())
    session = _plan_session(sessions)

    await loop.process_input(session, "看看记忆层怎么设计的")

    assert len(_plan_reminders(session)) == 1
    outcome = loop.last_outcome
    assert outcome is not None and outcome.status is TurnStatus.COMPLETED
    assert "未提交计划" in outcome.note


# ---- the tool table (the model's contract) ----------------------------------


class _RecordingEngine(_ScriptedEngine):
    """An engine that remembers the tool table it was handed."""

    def __init__(self, steps: list) -> None:
        super().__init__(steps)
        self.surfaces: list[list] = []

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.surfaces.append(tools)
        return await super().step(
            messages, tools=tools, instructions=instructions, on_delta=on_delta
        )


def _tool_names(schemas: list) -> list[str]:
    return [schema["function"]["name"] for schema in schemas]


def _registry_loop(engine: ChatEngine, sessions: SessionService) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(FinishResearchTool())
    return AgentLoop(
        engine,
        sessions,
        registry,
        approval=AutoApproveCallback(),
        settings=AIConfig(),
        workspace=".",
    )


@pytest.mark.asyncio
async def test_the_research_exit_is_not_in_the_build_mode_tool_table(tmp_path):
    """The tool table is a contract: outside the planning posture the research
    exit must not be advertised -- it could only ever be refused."""
    engine = _RecordingEngine([_text("好了。")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _registry_loop(engine, sessions)
    session = sessions.create()

    await loop.process_input(session, "看看记忆层怎么设计的")

    assert "finish_research" not in _tool_names(engine.surfaces[0])


@pytest.mark.asyncio
async def test_the_research_exit_is_in_the_plan_mode_tool_table(tmp_path):
    """The other half: the planning posture is where the exit belongs, so the
    model must be able to see it there."""
    engine = _RecordingEngine([_text("好了。"), _text("真的好了。")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _registry_loop(engine, sessions)
    session = _plan_session(sessions)
    await loop.process_input(session, "看看记忆层怎么设计的")

    assert "finish_research" in _tool_names(engine.surfaces[0])
