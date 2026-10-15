"""TDD (13 §2.1): the ladder runs inside the loop and survives the turn boundary.

脚本引擎与循环构造沿用 ``tests/test_agent_loop_react.py`` 的形状（该仓测试自包含）。
"""

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.domain.loop import StepResult
from ai_coding.domain.message import ToolCallRef, ToolResult
from ai_coding.domain.task import TaskState, TodoItem
from ai_coding.security.approval import AutoApproveCallback
from ai_coding.service.session_service import SessionService
from ai_coding.tools.base import BaseTool
from ai_coding.tools.registry import ToolRegistry


class _Flaky(BaseTool):
    """Always fails: the ladder needs a turn where nothing ever works."""

    name = "flaky"
    parallel_safe = True

    @property
    def description(self) -> str:
        return "a tool that always fails"

    @property
    def params_json_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, params, ctx=None) -> ToolResult:
        return ToolResult(success=False, output="boom", error="nonzero_exit")


class _ScriptedEngine(ChatEngine):
    """Returns pre-scripted steps and records every request it received."""

    def __init__(self, steps: list) -> None:
        self._steps = list(steps)
        self.requests: list[dict] = []

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.requests.append({"messages": list(messages), "tools": list(tools)})
        if not self._steps:
            raise AssertionError("engine called more times than the script allows")
        item = self._steps.pop(0)
        if isinstance(item, Exception):
            raise item
        if on_delta is not None and item.text:
            on_delta(item.text)
        return item

    async def aclose(self) -> None:
        return None


def _call(call_id: str) -> StepResult:
    return StepResult(
        text=None,
        tool_calls=[ToolCallRef(id=call_id, name="flaky", arguments="{}")],
        finish_reason="tool_calls",
    )


def _text(text: str) -> StepResult:
    return StepResult(text=text, finish_reason="stop")


def _loop(engine: ChatEngine, sessions: SessionService, **settings) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(_Flaky())
    return AgentLoop(
        engine,
        sessions,
        registry,
        approval=AutoApproveCallback(),
        settings=AIConfig(**settings),
        workspace=".",
    )


@pytest.mark.asyncio
async def test_a_turn_cut_mid_window_retries_the_same_level_next_turn(tmp_path):
    engine = _ScriptedEngine([
        _call("c1"), _call("c2"), _call("c3"),   # 两次全败 → L1；窗口内再败 → L2
        _text("先停一下"),                        # L2 窗口中途，模型收尾 → 回合 1 结束
        _call("c4"), _call("c5"),                 # 回合 2 再次全败 → 触发
        _text("again"),
    ])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    session = sessions.create()

    await loop.process_input(session, "go")
    first = session.intervention_state["strategy"]
    assert (first["level"], first["outcome"]) == (2, "unfinished")   # 回合边界 ≠ failed

    await loop.process_input(session, "again")
    second = session.intervention_state["strategy"]
    assert second["level"] == 2                                       # 同级重试，不越级
    nudges = [
        m.content or "" for m in session.messages
        if m.synthetic and "strategy-gate" in (m.content or "")
    ]
    assert "直接拦截" in nudges[-1]                                   # L2 的武装文案
    persisted = sessions.load(session.session_id)
    assert persisted.intervention_state["strategy"]["level"] == 2     # 搭车落盘、读回存活


@pytest.mark.asyncio
async def test_completing_the_task_clears_the_ledger(tmp_path):
    engine = _ScriptedEngine([_text("done")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    session = sessions.create()
    session.task = TaskState(
        goal="g", items=[TodoItem(id="1", content="c", status="completed")]
    )
    session.intervention_state = {
        "task": {"level": 1, "at": [1, 3], "outcome": "unfinished", "fires": 1, "ref": {}}
    }

    await loop.process_input(session, "go")

    assert session.intervention_state is None       # items 全终态 → 账本清空
    persisted = sessions.load(session.session_id)
    assert persisted.intervention_state is None     # 条件写：清空后文件里没有键
