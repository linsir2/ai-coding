"""Plan continuation runs through the *real* loop (B: structural, not injected).

These are not stub-loop tests.  They build the actual ``AgentLoop`` -- request
assembly, tool dispatch, ``todo_write``, boundary verification, the turn policy
and persistence -- and script only the model's steps.  That is the "real
machine" path the plan executor takes in production, minus the provider.
"""

import json

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.plan_executor import PlanExecutor
from ai_coding.domain.loop import StepResult
from ai_coding.domain.message import ToolCallRef
from ai_coding.domain.task import TaskState
from ai_coding.security.approval import AutoApproveCallback
from ai_coding.service.session_service import SessionService
from ai_coding.tools.bash_tool import BashTool
from ai_coding.tools.registry import ToolRegistry
from ai_coding.tools.todo_write_tool import TodoWriteTool


class _Scripted(ChatEngine):
    def __init__(self, steps):
        self.steps = list(steps)
        self.seen: list[list] = []

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.seen.append(list(messages))
        if not self.steps:
            raise AssertionError("model called more times than scripted")
        item = self.steps.pop(0)
        if on_delta is not None and item.text:
            on_delta(item.text)
        return item

    async def aclose(self):
        return None


def _text(value):
    return StepResult(text=value, finish_reason="stop")


def _call(call_id, name, **params):
    return StepResult(
        text=None,
        tool_calls=[ToolCallRef(id=call_id, name=name, arguments=json.dumps(params))],
        finish_reason="tool_calls",
    )


def _loop(engine, tmp_path):
    registry = ToolRegistry()
    registry.register(TodoWriteTool())
    registry.register(BashTool())
    return AgentLoop(
        engine,
        SessionService(tmp_path / "sessions"),
        registry,
        approval=AutoApproveCallback(),
        settings=AIConfig(),
        workspace=str(tmp_path / "ws"),
    )


def _session(loop, items):
    session = loop.sessions.create()
    task = TaskState(goal="real run")
    for content, verify in items:
        task.add(content, verify=verify)
    task.armed = True
    task.armed_profile = "plan"
    session.task = task
    session.agent = "build"
    session.readonly = False
    return session


def _plan_continuations(session) -> list[str]:
    """Messages the old policy injected at an idle boundary (must be empty)."""
    return [
        m.content or ""
        for m in session.messages
        if "[plan-continue]" in (m.content or "")
    ]


@pytest.fixture(autouse=True)
def _workspace(tmp_path):
    (tmp_path / "ws").mkdir()
    return tmp_path


@pytest.mark.asyncio
async def test_plan_completes_in_one_turn_and_repairs_a_red_proof_in_turn(tmp_path):
    engine = _Scripted(
        [
            _call("c1", "todo_write", action="update", id="1", status="completed"),
            _call("c2", "todo_write", action="update", id="2", status="completed"),
            _call("c3", "bash", command="touch made.txt"),
            _call("c4", "todo_write", action="update", id="2", status="completed"),
            _call("c5", "todo_write", action="update", id="3", status="completed"),
            _text("计划完成。"),
        ]
    )
    loop = _loop(engine, tmp_path)
    session = _session(
        loop, [("first", "true"), ("second", "test -f made.txt"), ("third", "")]
    )

    report = await PlanExecutor(loop).run(session)

    assert report.completed == ["1", "2", "3"]
    assert report.stalled == []
    assert session.turn_count == 1
    assert len(engine.seen) == 6
    # B: continuation is structural -- no text was injected at any boundary.
    assert _plan_continuations(session) == []
    # The red proof came back as the tool result the model then repaired.
    assert any(
        m.role == "tool" and "未通过" in (m.content or "") for m in session.messages
    )


@pytest.mark.asyncio
async def test_an_idle_model_closes_after_three_stops(tmp_path):
    engine = _Scripted([_text(f"我想想 {index}。") for index in range(8)])
    loop = _loop(engine, tmp_path)
    session = _session(loop, [("only", "")])

    report = await PlanExecutor(loop).run(session)

    assert len(engine.seen) == 3
    assert report.stopped == "plan stalled: 3 idle boundaries with no tool call"
    assert session.task.open_items()
    assert _plan_continuations(session) == []


@pytest.mark.asyncio
async def test_bookkeeping_spin_closes_after_six_rounds(tmp_path):
    engine = _Scripted(
        [
            _call(f"c{index}", "todo_write", action="list")
            for index in range(12)
        ]
    )
    loop = _loop(engine, tmp_path)
    session = _session(loop, [("never done", "")])

    report = await PlanExecutor(loop).run(session)

    assert len(engine.seen) == 6
    assert report.stopped == "plan stalled: 6 rounds without progress"


@pytest.mark.asyncio
async def test_the_plan_state_block_rides_requests_while_items_remain(tmp_path):
    """B3: the volatile block is what differs between two otherwise equal requests."""
    engine = _Scripted([_text("想想。"), _text("再想。"), _text("还想。")])
    loop = _loop(engine, tmp_path)
    session = _session(loop, [("only", "")])

    await PlanExecutor(loop).run(session)

    assert len(engine.seen) == 3
    requests = [
        " ".join(message.content or "" for message in seen) for seen in engine.seen
    ]
    # Present from the kickoff on: it is a tail block, not a boundary injection.
    assert all("<plan-state>" in request for request in requests)
    assert all("还有 1 项未完成" in request for request in requests)


@pytest.mark.asyncio
async def test_an_item_without_a_proof_completes_with_an_unattested_receipt(tmp_path):
    engine = _Scripted(
        [
            _call("c1", "todo_write", action="update", id="1", status="completed"),
            _text("完成。"),
        ]
    )
    loop = _loop(engine, tmp_path)
    session = _session(loop, [("docs", "")])

    report = await PlanExecutor(loop).run(session)

    assert report.completed == ["1"]
    assert session.step_deliveries[0].proof == "fiat"
