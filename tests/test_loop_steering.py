"""R7e-2: a line typed mid-turn becomes an instruction the model sees next step.

D-2 Phase 2: the rows live in the session's inbox table (next-step target),
not in an in-memory FIFO.  Same delivery point -- the iteration boundary,
after the previous batch finished -- same persisted `[steering]` message, same
reporting; only the source moved, so a restart no longer loses what the
operator said mid-turn.
"""

from __future__ import annotations

from typing import Any

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.events import InputSteered, RecordingObserver
from ai_coding.domain.message import ToolCallRef, ToolResult
from ai_coding.domain.session import InboxItem, SessionData
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

    def mark_pending_approval(
        self, session_id: str, request: dict[str, Any] | None
    ) -> None:
        """Stands in for the marker the real SessionService writes beside the
        session file (P3); this fake keeps nothing on disk."""
        return None


def _loop(
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
    )


def _loop_with_recorder(
    engine: _Engine,
) -> tuple[AgentLoop, RecordingObserver]:
    observer = RecordingObserver()
    return _loop(engine, observer=observer), observer


def _steer(session: SessionData, *texts: str) -> SessionData:
    """Rows as session.steer would have written them (D-2 Phase 2)."""
    for index, text in enumerate(texts):
        session.inbox.append(
            InboxItem(id=f"r{index}", text=text, target="next-step", created_at="t")
        )
    return session


class _PlainEngine(ChatEngine):
    """Speaks immediately: one iteration, therefore one drain."""

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        return _text_step("说完了")


def _session() -> SessionData:
    return SessionData(session_id="s", title="", created_time="t", last_access_time="t")


@pytest.mark.asyncio
async def test_a_steering_line_becomes_a_persisted_user_message():
    session = _steer(_session(), "改用 uv，不要 pip")
    await _loop(_Engine()).process_input(session, "改构建")
    contents = [m.content or "" for m in session.messages if m.role == "user"]
    assert any("[steering] 改用 uv，不要 pip" in content for content in contents)


@pytest.mark.asyncio
async def test_the_model_sees_the_steering_line_in_the_same_iteration():
    engine = _Engine()
    await _loop(engine).process_input(_steer(_session(), "改用 uv"), "改构建")
    second_request = [m.content or "" for m in engine.requests[1]]
    assert any("[steering] 改用 uv" in content for content in second_request)


@pytest.mark.asyncio
async def test_steering_is_reported_once_per_batch():
    loop, observer = _loop_with_recorder(_Engine())
    await loop.process_input(_steer(_session(), "一", "二"), "改构建")
    events = observer.of_type(InputSteered)
    assert len(events) == 1
    assert events[0].total == 2
    # 行是**回合开始前**就写好的，所以第一次抽干发生在第 1 轮边界——
    # 这两句话进了本回合的第一个请求（用户「边跑边说」时，抽干点自然落到下一轮）。
    assert events[0].iteration == 1


@pytest.mark.asyncio
async def test_steering_does_not_count_as_an_iteration():
    loop, _observer = _loop_with_recorder(_Engine())
    await loop.process_input(_steer(_session(), "早一点说"), "改构建")
    assert loop.last_outcome is not None
    assert loop.last_outcome.iterations == 2  # 只有两次真正的迭代


@pytest.mark.asyncio
async def test_no_source_means_no_behaviour_change(monkeypatch):
    # Empty table behaves like no table; and next-turn rows stay parked -- a
    # steer-scoped drain never promotes them (oc draws the same line in
    # execution.ts).  Both runs must show the model exactly the same request.
    # The environment block carries the wall clock (C-4); two builds compared
    # byte-for-byte must not straddle a second boundary.
    monkeypatch.setattr(
        "ai_coding.core.request_assembly.now_local", lambda: "2026-01-01T00:00:00+08:00"
    )
    plain_engine = _Engine()
    parked_engine = _Engine()
    parked = _session()
    parked.inbox.append(InboxItem(id="p1", text="下回合再说", target="next-turn", created_at="t"))
    await _loop(plain_engine).process_input(_session(), "改构建")
    await _loop(parked_engine).process_input(parked, "改构建")
    assert [m.content for m in plain_engine.requests[1]] == [
        m.content for m in parked_engine.requests[1]
    ]
    assert len(parked.inbox) == 1, "the next-turn row must survive the turn untouched"


@pytest.mark.asyncio
async def test_every_next_step_row_drains_in_one_iteration():
    """The old FIFO capped intake at five and a max_messages=1 variant proved
    leftovers stay; the table has no intake cap, so the drain takes everything
    and the next-turn rows stay parked.  A partial drain would leave the model
    reading half an instruction."""
    loop, _observer = _loop_with_recorder(_PlainEngine())
    session = _steer(_session(), "第一条", "第二条")
    session.inbox.append(
        InboxItem(id="p1", text="下回合再说", target="next-turn", created_at="t")
    )
    await loop.process_input(session, "改构建")
    assert [item.target for item in session.inbox] == ["next-turn"]
    contents = [m.content or "" for m in session.messages if m.role == "user"]
    assert any("[steering] 第一条" in content for content in contents)
    assert any("[steering] 第二条" in content for content in contents)
