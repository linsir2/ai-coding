"""The plan exit at the loop seam: an approval ends the turn.

``present_plan`` is the plan-mode turn's first exit: the human approves, the
plan is armed, the runner executes it.  The research exit and the dismissal
both had a closing branch at the loop seam -- the approval did not.  So after
the review card came back "approved" the model kept exploring, the planning
budget ran out, and the turn ended *badly*.  A badly ended turn is the one
thing that makes the close take the approval back (W2): the operator had
approved a plan and the system silently undid it, which is what the live
check measured (scripts/plan_lifecycle_live_check.py, the [swap] scenario).

The seam under test is the same one the research exit and the dismissal use:
the approval ends the turn at the batch -- no second model call -- and the
approval it carried stays armed.
"""

import json

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.plan_runner import continuations_disabled
from ai_coding.core.turn_policy import PLAN_APPROVAL_WITHDRAWN_NOTE
from ai_coding.domain.loop import StepResult, TurnStatus
from ai_coding.domain.message import ToolCallRef, ToolResult
from ai_coding.security.approval import ApprovalResult, AutoApproveCallback
from ai_coding.security.user_prompt import UserAnswer
from ai_coding.service.session_service import SessionService
from ai_coding.tools.base import BaseTool
from ai_coding.tools.present_plan_tool import PresentPlanTool
from ai_coding.tools.registry import ToolRegistry


class _ScriptedEngine(ChatEngine):
    """A model that asks for tools, and counts how often it was asked."""

    def __init__(self, steps: list) -> None:
        self._steps = list(steps)
        self.calls = 0

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.calls += 1
        if not self._steps:
            raise AssertionError("engine called more times than the script allows")
        item = self._steps.pop(0)
        if on_delta is not None and item.text:
            on_delta(item.text)
        return item

    async def aclose(self) -> None:
        return None


class _Approve:
    """A human who approves the plan."""

    def __init__(self) -> None:
        self.asked = 0

    async def ask(self, question, options=(), *, detail="", intent="", data=None):
        self.asked += 1
        return UserAnswer(answered=True, text=json.dumps({"choice": "approve"}))


class _Stop:
    """An operator who stops the turn when the gate asks."""

    async def approve(self, tool_name, params, reason):
        return ApprovalResult.STOP


class _Read(BaseTool):
    """The model keeps looking at things: the turn has to end anyway."""

    name = "read"
    parallel_safe = True
    mutates_world = False

    @property
    def description(self) -> str:
        return "look at a file"

    @property
    def params_json_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, params, ctx=None) -> ToolResult:
        return ToolResult(success=True, output="看完了")


class _Probe(BaseTool):
    """A call the gate asks about -- and the operator stops.

    The name is not on any list, so the gate treats it as a question (fail
    closed), which is the only way a batch can be stopped from the inside.
    """

    name = "probe"
    parallel_safe = False
    mutates_world = False

    @property
    def description(self) -> str:
        return "a call the operator stops"

    @property
    def params_json_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, params, ctx=None) -> ToolResult:
        return ToolResult(success=True, output="不该走到这里")


def _call(call_id: str, name: str, args: dict | None = None) -> StepResult:
    return StepResult(
        text=None,
        tool_calls=[
            ToolCallRef(
                id=call_id, name=name, arguments=json.dumps(args or {})
            )
        ],
        finish_reason="tool_calls",
    )


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


def _loop(
    engine: ChatEngine,
    sessions: SessionService,
    registry: ToolRegistry,
    *,
    approval=None,
) -> AgentLoop:
    return AgentLoop(
        engine,
        sessions,
        registry,
        approval=approval if approval is not None else AutoApproveCallback(),
        settings=AIConfig(),
        workspace=".",
    )


def _registry(*tools: BaseTool) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


def _plan_session(sessions: SessionService):
    session = sessions.create()
    session.agent, session.readonly = "plan", True
    return session


_PLAN_CALL = (
    "c_plan",
    "present_plan",
    {"plan": "# 计划", "items": [{"content": "建目录"}]},
)


# ---- the approval is the exit ------------------------------------------------


@pytest.mark.asyncio
async def test_an_approved_plan_ends_the_turn_at_the_approval(tmp_path):
    """The approval closes the planning round: the model is not asked again.

    The budget is two rounds, so a turn that kept exploring would run out of
    it -- and a turn that ends on the budget is a turn that ended badly, which
    is what took the approval back.
    """
    engine = _ScriptedEngine([_call(*_PLAN_CALL), _call("c2", "read")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions, _registry(PresentPlanTool(_Approve()), _Read()))
    session = _plan_session(sessions)

    with continuations_disabled(loop, max_iterations=2):
        await loop.process_input(session, "给个计划")

    assert engine.calls == 1, "the turn must end at the approval, not explore on"
    outcome = loop.last_outcome
    assert outcome is not None and outcome.status is TurnStatus.COMPLETED
    assert outcome.reason == "plan approved"


@pytest.mark.asyncio
async def test_an_approved_plan_stays_armed_after_the_turn(tmp_path):
    """The approval is what the run needs, so the turn must leave it armed.

    The withdrawal at the close (W2) is for the turns that did not end
    cleanly; a turn that ended at the approval ends cleanly, so the plan stays
    armed and the runner can execute it.
    """
    engine = _ScriptedEngine([_call(*_PLAN_CALL), _call("c2", "read")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions, _registry(PresentPlanTool(_Approve()), _Read()))
    session = _plan_session(sessions)

    with continuations_disabled(loop, max_iterations=2):
        await loop.process_input(session, "给个计划")

    assert session.task is not None
    assert session.task.armed is True
    assert session.task.armed_profile == "plan"
    assert [item.content for item in session.task.items] == ["建目录"]


@pytest.mark.asyncio
async def test_an_approved_turn_leaves_no_withdrawal_note(tmp_path):
    """Nothing was taken back, so there is nothing to report.

    The withdrawal's note says "the approval was taken back"; on this path
    the approval was kept, so the note must stay empty -- a line about a
    withdrawal that did not happen would be a lie.
    """
    engine = _ScriptedEngine([_call(*_PLAN_CALL), _call("c2", "read")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions, _registry(PresentPlanTool(_Approve()), _Read()))
    session = _plan_session(sessions)

    with continuations_disabled(loop, max_iterations=2):
        await loop.process_input(session, "给个计划")

    outcome = loop.last_outcome
    assert outcome is not None
    assert outcome.note == ""
    assert outcome.note != PLAN_APPROVAL_WITHDRAWN_NOTE


# ---- the withdrawal still owns the turns that ended badly --------------------


@pytest.mark.asyncio
async def test_a_stopped_batch_still_withdraws_the_approval(tmp_path):
    """W2: the withdrawal is for the turns that did not end cleanly.

    An approval in a batch the operator stopped is exactly that: the front
    end refuses to run a plan from a stopped turn, so the backend must not
    leave it armed.  The order of the two branches is what makes this true --
    the halt is read before the approval, so a stopped batch is still a
    stopped turn and not a clean one.
    """
    engine = _ScriptedEngine([_calls(_PLAN_CALL, ("c_probe", "probe", {}))])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(
        engine,
        sessions,
        _registry(PresentPlanTool(_Approve()), _Probe()),
        approval=_Stop(),
    )
    session = _plan_session(sessions)

    await loop.process_input(session, "给个计划")

    outcome = loop.last_outcome
    assert outcome is not None and outcome.status is TurnStatus.STOPPED
    assert outcome.note == PLAN_APPROVAL_WITHDRAWN_NOTE
    assert session.task is not None
    assert session.task.armed is False
    assert session.task.armed_profile == ""
