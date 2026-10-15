"""TDD (R4c): hooks wired into the loop and the dispatcher.

The wiring is where the cache discipline can be broken, so the inject case
asserts the *shape* of the request: the hook text rides in the volatile tail and
the system prompt stays byte-identical (docs/r4-analysis.md 6.4).
"""

from collections.abc import Mapping
from typing import Any

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.ai.prompt import PromptAssembler
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.hooks import HookDecision, HookPoint, HookResult
from ai_coding.domain.loop import StepResult
from ai_coding.domain.message import ChatMessage, ToolCallRef, ToolResult
from ai_coding.domain.session import SessionData
from ai_coding.service.session_service import SessionService
from ai_coding.tools.base import BaseTool
from ai_coding.tools.registry import ToolRegistry


class _Engine(ChatEngine):
    def __init__(self, steps) -> None:  # type: ignore[no-untyped-def]
        self._steps = list(steps)
        self.requests: list[list[ChatMessage]] = []
        self.systems: list[str | None] = []

    async def step(self, messages, *, tools, instructions=None, on_delta=None):  # type: ignore[no-untyped-def]
        self.requests.append(list(messages))
        self.systems.append(instructions)
        return self._steps.pop(0)

    async def aclose(self) -> None:
        return None


class _Tool(BaseTool):
    parallel_safe = True

    def __init__(self, name: str = "read") -> None:
        self._name = name
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "fake"

    @property
    def params_json_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, params, ctx=None):  # type: ignore[override]
        self.calls += 1
        return ToolResult(success=True, output="ok")


class _FakeHooks:
    """Returns canned verdicts per point, so the wiring is what gets tested."""

    def __init__(self, results: Mapping[HookPoint, list[HookResult]]) -> None:
        self._results = dict(results)
        self.seen: list[HookPoint] = []

    async def run(self, point: HookPoint, ctx: Mapping[str, Any]) -> list[HookResult]:
        self.seen.append(point)
        return list(self._results.get(point, []))


def _session() -> SessionData:
    return SessionData(
        session_id="s1", title="", created_time="2026-01-01T00:00:00Z", last_access_time=""
    )


def _loop(engine, tmp_path, tool: _Tool, **kwargs) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(tool)
    kwargs.setdefault("settings", AIConfig(stop_policy="natural", max_tool_iterations=3))
    return AgentLoop(
        engine,
        SessionService(tmp_path / "sessions"),
        registry,
        prompt=PromptAssembler("SYS" * 40),
        workspace=str(tmp_path),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_injected_context_rides_in_the_tail_not_the_system_prompt(tmp_path):
    engine = _Engine([StepResult(text="done", finish_reason="stop")])
    hooks = _FakeHooks(
        {
            HookPoint.TURN_START: [
                HookResult(
                    hook="rules", decision=HookDecision.INJECT, context="use uv, not pip"
                )
            ]
        }
    )
    loop = _loop(engine, tmp_path, _Tool(), hooks=hooks)

    await loop.process_input(_session(), "hi")

    request = engine.requests[0]
    assert all("<hook-context>" not in (m.content or "") for m in request[:-1])
    assert "<hook-context>" in (request[-1].content or "")
    assert "use uv, not pip" in (request[-1].content or "")
    # the system prompt is unchanged by the hook: the cache prefix is intact
    # (13 §2.3: compare against what the builder produces, methods included)
    assert engine.systems[0] == PromptAssembler("SYS" * 40).build()


@pytest.mark.asyncio
async def test_a_blocking_tool_hook_stops_the_call(tmp_path):
    engine = _Engine(
        [
            StepResult(
                text=None,
                tool_calls=[ToolCallRef(id="c1", name="read", arguments="{}")],
                finish_reason="tool_calls",
            ),
            StepResult(text="done", finish_reason="stop"),
        ]
    )
    tool = _Tool()
    hooks = _FakeHooks(
        {
            HookPoint.TOOL_BEFORE: [
                HookResult(
                    hook="deny-read",
                    decision=HookDecision.BLOCK,
                    reason="reads are not allowed here",
                )
            ]
        }
    )
    loop = _loop(engine, tmp_path, tool, hooks=hooks)
    session = _session()

    await loop.process_input(session, "read something")

    assert tool.calls == 0, "a blocked call must never execute"
    tool_messages = [m for m in session.messages if m.role == "tool"]
    assert len(tool_messages) == 1, "the transcript must still pair the call"
    content = str(tool_messages[0].content)
    # The refusal is structured (the error code lives on the result, the details
    # in <data>), exactly like the gate's own denials.
    assert 'ok="false"' in content
    assert '"hook": "deny-read"' in content
    assert "reads are not allowed here" in content


@pytest.mark.asyncio
async def test_turn_end_hooks_are_fired(tmp_path):
    engine = _Engine([StepResult(text="done", finish_reason="stop")])
    hooks = _FakeHooks(
        {HookPoint.TURN_END: [HookResult(hook="notify", decision=HookDecision.ALLOW)]}
    )
    loop = _loop(engine, tmp_path, _Tool(), hooks=hooks)

    await loop.process_input(_session(), "hi")

    assert HookPoint.TURN_START in hooks.seen
    assert HookPoint.TURN_END in hooks.seen


@pytest.mark.asyncio
async def test_a_broken_hook_delivery_never_breaks_the_turn(tmp_path):
    class _Exploding:
        async def run(self, point, ctx):  # type: ignore[no-untyped-def]
            raise RuntimeError("hook transport down")

    engine = _Engine([StepResult(text="done", finish_reason="stop")])
    loop = _loop(engine, tmp_path, _Tool(), hooks=_Exploding())

    text = await loop.process_input(_session(), "hi")

    assert text == "done"
