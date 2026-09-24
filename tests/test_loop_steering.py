"""R7e-2: a line typed mid-turn becomes an instruction the model sees next step."""

from __future__ import annotations

from typing import Any

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.events import InputSteered, RecordingObserver
from ai_coding.core.steering import SteeringQueue
from ai_coding.domain.message import ToolCallRef, ToolResult
from ai_coding.domain.session import SessionData
from ai_coding.security.approval import AutoApproveCallback
from ai_coding.tools.base import BaseTool, ToolContext
from ai_coding.tools.registry import ToolRegistry


class _Engine(ChatEngine):
    """Ask for one tool on the first step, then speak."""

    def __init__(self) -> None:
        self.requests: list[list[Any]] = []

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.requests.append(list(messages))
        if len(self.requests) == 1:
            return _tool_step()
        return _text_step()


def _tool_step():
    from ai_coding.domain.loop import StepResult

    return StepResult(
        text=None,
        tool_calls=[ToolCallRef(id="c1", name="poke", arguments="{}")],
        finish_reason="tool_calls",
    )


def _text_step(text: str = "好"):
    from ai_coding.domain.loop import StepResult

    return StepResult(text=text, tool_calls=[], finish_reason="stop")


class _Poke(BaseTool):
    parallel_safe = True

    @property
    def name(self) -> str:
        return "poke"

    @property
    def description(self) -> str:
        return "poke"

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, params: dict[str, Any], ctx: ToolContext | None = None) -> ToolResult:
        return ToolResult(success=True, output="poked")


class _Sessions:
    def save(self, session: SessionData) -> None:
        return None


def _loop(
    queue: SteeringQueue | None,
    engine: _Engine,
    *,
    observer: RecordingObserver | None = None,
    **overrides: Any,
) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(_Poke())
    return AgentLoop(
        engine,
        _Sessions(),
        registry,
        approval=AutoApproveCallback(),
        settings=AIConfig(max_tool_iterations=3, stop_policy="natural", **overrides),
        observer=observer,
        steering=queue,
    )


def _loop_with_recorder(
    queue: SteeringQueue | None, engine: _Engine
) -> tuple[AgentLoop, RecordingObserver]:
    observer = RecordingObserver()
    return _loop(queue, engine, observer=observer), observer


class _PlainEngine(ChatEngine):
    """Speaks immediately: one iteration, therefore one drain."""

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        return _text_step("说完了")


def _session() -> SessionData:
    return SessionData(session_id="s", title="", created_time="t", last_access_time="t")


@pytest.mark.asyncio
async def test_a_steering_line_becomes_a_persisted_user_message():
    queue = SteeringQueue()
    queue.put("改用 uv，不要 pip")
    session = _session()
    await _loop(queue, _Engine()).process_input(session, "改构建")
    contents = [m.content or "" for m in session.messages if m.role == "user"]
    assert any("[steering] 改用 uv，不要 pip" in content for content in contents)


@pytest.mark.asyncio
async def test_the_model_sees_the_steering_line_in_the_same_iteration():
    queue = SteeringQueue()
    queue.put("改用 uv")
    engine = _Engine()
    await _loop(queue, engine).process_input(_session(), "改构建")
    second_request = [m.content or "" for m in engine.requests[1]]
    assert any("[steering] 改用 uv" in content for content in second_request)


@pytest.mark.asyncio
async def test_steering_is_reported_once_per_batch():
    queue = SteeringQueue()
    queue.put("一")
    queue.put("二")
    loop, observer = _loop_with_recorder(queue, _Engine())
    await loop.process_input(_session(), "改构建")
    events = observer.of_type(InputSteered)
    assert len(events) == 1
    assert events[0].total == 2
    # 队列是**回合开始前**就填好的，所以第一次抽干发生在第 1 轮边界——
    # 这两句话进了本回合的第一个请求（用户「边跑边说」时，抽干点自然落到下一轮）。
    assert events[0].iteration == 1


@pytest.mark.asyncio
async def test_steering_does_not_count_as_an_iteration():
    queue = SteeringQueue()
    queue.put("早一点说")
    loop, _observer = _loop_with_recorder(queue, _Engine())
    await loop.process_input(_session(), "改构建")
    assert loop.last_outcome is not None
    assert loop.last_outcome.iterations == 2  # 只有两次真正的迭代


@pytest.mark.asyncio
async def test_no_source_means_no_behaviour_change():
    plain_engine = _Engine()
    queued_engine = _Engine()
    await _loop(None, plain_engine).process_input(_session(), "改构建")
    await _loop(SteeringQueue(), queued_engine).process_input(_session(), "改构建")
    assert [m.content for m in plain_engine.requests[1]] == [
        m.content for m in queued_engine.requests[1]
    ]


@pytest.mark.asyncio
async def test_leftovers_stay_queued_when_a_turn_only_drains_once():
    """一次抽干只拿走上限内的条数；剩下的留在队列里（多轮回合会继续抽）。"""
    queue = SteeringQueue(max_messages=1)
    queue.put("第一条")
    queue.put("第二条")
    loop, _observer = _loop_with_recorder(queue, _PlainEngine())
    await loop.process_input(_session(), "改构建")
    assert queue.pending == 1
