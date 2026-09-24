"""R7e-3: the tool surface is paid for every step, so it says when it is expensive.

The number is static (the loop knows its schemas at construction), the breakdown is
per origin (so the warning names the server to trim), and the whole thing is
advisory: a test asserts the request is byte-identical with the warning on and off.
"""

from __future__ import annotations

from typing import Any

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.events import RecordingObserver, ToolSurfaceWarning
from ai_coding.domain.loop import StepResult
from ai_coding.domain.message import ChatMessage
from ai_coding.domain.session import SessionData
from ai_coding.security.approval import AutoApproveCallback
from ai_coding.tools.base import BaseTool, ToolContext
from ai_coding.tools.registry import ToolRegistry


class _Engine(ChatEngine):
    """Answers immediately, and remembers every request it was handed."""

    def __init__(self) -> None:
        self.requests: list[list[ChatMessage]] = []

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.requests.append(list(messages))
        return StepResult(text="好", tool_calls=[], finish_reason="stop")


class _Tool(BaseTool):
    parallel_safe = True

    def __init__(self, name: str, description: str, origin: str = "local") -> None:
        self._name = name
        self._description = description
        self._origin = origin

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    @property
    def origin(self) -> str:
        return self._origin

    async def execute(self, params: dict[str, Any], ctx: ToolContext | None = None):
        raise NotImplementedError


class _Sessions:
    def save(self, session: SessionData) -> None:
        return None


def _loop(
    *tools: BaseTool, observer: RecordingObserver | None = None, **overrides: Any
) -> tuple[AgentLoop, _Engine, RecordingObserver]:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    engine = _Engine()
    seen = observer or RecordingObserver()
    loop = AgentLoop(
        engine,
        _Sessions(),
        registry,
        approval=AutoApproveCallback(),
        settings=AIConfig(max_tool_iterations=1, stop_policy="natural", **overrides),
        observer=seen,
    )
    return loop, engine, seen


def _session() -> SessionData:
    return SessionData(session_id="s", title="", created_time="t", last_access_time="t")


@pytest.mark.asyncio
async def test_no_warning_below_the_threshold():
    loop, _engine, observer = _loop(_Tool("tiny", "x"))
    await loop.process_input(_session(), "一")
    assert observer.of_type(ToolSurfaceWarning) == []


@pytest.mark.asyncio
async def test_one_warning_above_the_threshold():
    loop, _engine, observer = _loop(_Tool("fat", "y" * 3000), tools_chars_warn=100)
    await loop.process_input(_session(), "一")
    warnings = observer.of_type(ToolSurfaceWarning)
    assert len(warnings) == 1
    assert warnings[0].tools_chars > warnings[0].threshold == 100


@pytest.mark.asyncio
async def test_the_warning_is_emitted_once_per_loop():
    loop, _engine, observer = _loop(_Tool("fat", "y" * 3000), tools_chars_warn=100)
    session = _session()
    await loop.process_input(session, "一")
    await loop.process_input(session, "二")
    assert len(observer.of_type(ToolSurfaceWarning)) == 1


@pytest.mark.asyncio
async def test_zero_disables_the_warning():
    loop, _engine, observer = _loop(_Tool("fat", "y" * 3000), tools_chars_warn=0)
    await loop.process_input(_session(), "一")
    assert observer.of_type(ToolSurfaceWarning) == []


@pytest.mark.asyncio
async def test_the_origins_add_up_to_the_tool_surface():
    loop, _engine, observer = _loop(
        _Tool("local_one", "z" * 400),
        _Tool("mcp_one", "z" * 400, origin="many"),
        _Tool("mcp_two", "z" * 400, origin="many"),
        tools_chars_warn=100,
    )
    await loop.process_input(_session(), "一")
    warning = observer.of_type(ToolSurfaceWarning)[0]
    origins = dict(warning.origins)
    assert set(origins) == {"local", "many"}
    assert abs(sum(origins.values()) - warning.tools_chars) <= 3 * 3  # 数组括号与逗号


@pytest.mark.asyncio
async def test_the_hint_names_the_most_expensive_origin():
    loop, _engine, observer = _loop(
        _Tool("local_one", "z" * 20),
        _Tool("mcp_one", "z" * 3000, origin="everything"),
        tools_chars_warn=100,
    )
    await loop.process_input(_session(), "一")
    warning = observer.of_type(ToolSurfaceWarning)[0]
    assert dict(warning.origins)["everything"] > dict(warning.origins)["local"]
    assert "everything" in warning.hint


@pytest.mark.asyncio
async def test_the_warning_never_changes_the_request():
    """R4 的纪律：观测只写不改。开不开告警，请求逐字节相同。"""
    quiet, quiet_engine, quiet_events = _loop(_Tool("fat", "y" * 3000), tools_chars_warn=0)
    loud, loud_engine, loud_events = _loop(_Tool("fat", "y" * 3000), tools_chars_warn=100)
    await quiet.process_input(_session(), "一")
    await loud.process_input(_session(), "一")
    assert [m.content for m in quiet_engine.requests[-1]] == [
        m.content for m in loud_engine.requests[-1]
    ]
    assert not quiet_events.of_type(ToolSurfaceWarning)
    assert loud_events.of_type(ToolSurfaceWarning)


def test_the_console_renders_one_readable_line():
    """R7e-3c: the REPL shows the number, the threshold and the top origins."""
    from ai_coding.core.events import ToolSurfaceWarning
    from ai_coding.ui.console import ConsoleObserver, DialogueConsole

    printed: list[str] = []

    class _Console(DialogueConsole):
        def info(self, message: str) -> None:
            printed.append(message)

    ConsoleObserver(_Console()).on_event(
        ToolSurfaceWarning(
            tools_chars=13_301,
            threshold=12_000,
            origins=(("everything", 10_062), ("local", 4_280)),
            hint="ignore me",
        )
    )
    assert printed and printed[0].startswith("[tools]")
    assert "13301" in printed[0]
    assert "12000" in printed[0]
    assert "everything=10062" in printed[0]
