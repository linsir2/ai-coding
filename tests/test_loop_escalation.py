"""The loop's half of R6b: count failures, nudge once, stop when nothing works.

As in tests/test_loop_digest.py the fake tools are unknown to the permission gate
(WARN, fail-closed), so the loops carry ``AutoApproveCallback`` — the same policy
the non-interactive ``ask`` entry point uses.
"""

from __future__ import annotations

from typing import Any

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.domain.loop import StepResult, TurnStatus
from ai_coding.domain.message import ToolCallRef, ToolResult
from ai_coding.domain.session import SessionData
from ai_coding.security.approval import AutoApproveCallback
from ai_coding.tools.base import BaseTool, ToolContext
from ai_coding.tools.registry import ToolRegistry


class _Engine(ChatEngine):
    """Always asks for the same tool, so only the policy can end the turn."""

    def __init__(self, ask_for_tools: bool = True) -> None:
        self.ask_for_tools = ask_for_tools
        self.calls = 0

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.calls += 1
        if not self.ask_for_tools:
            return StepResult(text="我直接回答", tool_calls=[], finish_reason="stop")
        return StepResult(
            text=None,
            tool_calls=[ToolCallRef(id=f"c{self.calls}", name="flaky", arguments="{}")],
            finish_reason="tool_calls",
        )


class _Flaky(BaseTool):
    """Fails the first ``failures`` calls, then succeeds."""

    name = "flaky"
    parallel_safe = True

    def __init__(self, failures: int = 99) -> None:
        self.failures = failures
        self.seen = 0

    @property
    def description(self) -> str:
        return "a tool that fails"

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, params, ctx: ToolContext | None = None) -> ToolResult:
        self.seen += 1
        if self.seen <= self.failures:
            return ToolResult(success=False, output="boom", error="nonzero_exit")
        return ToolResult(success=True, output="ok")


class _Sessions:
    def __init__(self) -> None:
        self.saved: list[SessionData] = []

    def save(self, session: SessionData) -> None:
        self.saved.append(session)

    def mark_pending_approval(self, session_id: str, request) -> None:
        """P3: stand-in for the marker the real SessionService writes beside
        the session file; this fake keeps nothing on disk."""
        return None


def _loop(engine: ChatEngine, tool: BaseTool, **overrides: Any) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(tool)
    return AgentLoop(
        engine,
        _Sessions(),
        registry,
        approval=AutoApproveCallback(),
        settings=AIConfig(**overrides),
        workspace=".",
    )


def _session() -> SessionData:
    return SessionData(session_id="s", title="", created_time="t", last_access_time="t")


def _strategy_gates(session: SessionData) -> list[str]:
    return [
        message.content or ""
        for message in session.messages
        if message.role == "user" and "[strategy-gate]" in (message.content or "")
    ]


@pytest.mark.asyncio
async def test_failed_iterations_climb_the_strategy_ladder_in_the_session():
    """13 §2.1: one injected nudge per ladder level, L1 then L2 (armed)."""
    loop = _loop(
        _Engine(), _Flaky(), max_tool_iterations=6, stall_iterations=0, failed_streak_threshold=2
    )
    session = _session()
    await loop.process_input(session, "修好它")
    gates = _strategy_gates(session)
    assert len(gates) == 2                       # L1 → L2, not one-shot anymore
    assert "不要重复同样的做法" in gates[0]
    assert "直接拦截" in gates[1]
    assert loop.last_outcome is not None
    assert loop.last_outcome.status is TurnStatus.MAX_ITERATIONS


@pytest.mark.asyncio
async def test_a_stalled_turn_runs_the_ladder_then_hands_off():
    """Sanctioned change ①: the turn now lives through L1→L2→L3 windows and
    stops at L4 (no progress) instead of dying at the first stall."""
    loop = _loop(_Engine(), _Flaky(), max_tool_iterations=12, stall_iterations=3)
    session = _session()
    answer = await loop.process_input(session, "修好它")
    assert loop.last_outcome is not None
    assert "no progress" in loop.last_outcome.reason
    assert loop.last_outcome.iterations < 12
    assert "[回合未完成" in answer
    assert "最近一次失败: flaky → nonzero_exit" in answer
    assert "零进展" in answer  # 提前收尾不是「预算用尽」，报告不许说谎


@pytest.mark.asyncio
async def test_successful_tool_calls_count_towards_progress():
    loop = _loop(
        _Engine(), _Flaky(failures=1), max_tool_iterations=8, stall_iterations=3
    )
    session = _session()
    await loop.process_input(session, "修好它")
    assert loop.last_outcome is not None
    assert "no progress" not in loop.last_outcome.reason
    assert loop.last_outcome.status is TurnStatus.MAX_ITERATIONS


@pytest.mark.asyncio
async def test_escalation_never_fires_without_tool_calls():
    loop = _loop(_Engine(ask_for_tools=False), _Flaky(), max_tool_iterations=4)
    session = _session()
    answer = await loop.process_input(session, "打个招呼")
    assert answer == "我直接回答"
    assert _strategy_gates(session) == []
    assert loop.last_outcome is not None and loop.last_outcome.handoff == ""
