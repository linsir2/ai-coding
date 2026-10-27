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

    def mark_pending_approval(self, session_id: str, request: object) -> None:
        """P3: stand-in for the marker the real SessionService writes beside
        the session file; this fake keeps nothing on disk."""
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
async def test_the_local_tool_surface_alone_never_warns():
    """r22: the built-in tools serialise to 12,760 chars -- past the 12,000
    ceiling R7e-3 picked when they were 4,280 -- so every session announced a
    cost the user cannot change.  Only the MCP part is theirs to configure, so
    only that part is worth a warning.
    """
    loop, _engine, observer = _loop(_Tool("fat", "y" * 30_000), mcp_chars_warn=2_000)
    await loop.process_input(_session(), "一")

    assert observer.of_type(ToolSurfaceWarning) == [], (
        "a tool surface the user cannot shrink is not a warning, it is noise"
    )


@pytest.mark.asyncio
async def test_an_mcp_surface_over_its_own_ceiling_warns():
    """The warning keeps its job: it is the MCP server that costs, and the hint
    still names it.  The total stays on the event so the diagnosis is complete.
    """
    loop, _engine, observer = _loop(
        _Tool("local_one", "z" * 400),
        _Tool("remote", "z" * 3_000, origin="mcp"),
        mcp_chars_warn=2_000,
    )
    await loop.process_input(_session(), "一")

    warnings = observer.of_type(ToolSurfaceWarning)
    assert len(warnings) == 1
    assert warnings[0].mcp_chars > warnings[0].threshold == 2_000
    assert warnings[0].tools_chars > warnings[0].mcp_chars, (
        "the total is still reported, so the reader can see how much is built in"
    )


@pytest.mark.asyncio
async def test_no_warning_below_the_threshold():
    loop, _engine, observer = _loop(_Tool("tiny", "x"))
    await loop.process_input(_session(), "一")
    assert observer.of_type(ToolSurfaceWarning) == []


@pytest.mark.asyncio
async def test_one_warning_above_the_threshold():
    loop, _engine, observer = _loop(
        _Tool("remote", "y" * 3000, origin="mcp"), mcp_chars_warn=100
    )
    await loop.process_input(_session(), "一")
    warnings = observer.of_type(ToolSurfaceWarning)
    assert len(warnings) == 1
    assert warnings[0].mcp_chars > warnings[0].threshold == 100


@pytest.mark.asyncio
async def test_the_warning_is_emitted_once_per_loop():
    loop, _engine, observer = _loop(
        _Tool("remote", "y" * 3000, origin="mcp"), mcp_chars_warn=100
    )
    session = _session()
    await loop.process_input(session, "一")
    await loop.process_input(session, "二")
    assert len(observer.of_type(ToolSurfaceWarning)) == 1


@pytest.mark.asyncio
async def test_zero_disables_the_warning():
    loop, _engine, observer = _loop(
        _Tool("remote", "y" * 3000, origin="mcp"), mcp_chars_warn=0
    )
    await loop.process_input(_session(), "一")
    assert observer.of_type(ToolSurfaceWarning) == []


@pytest.mark.asyncio
async def test_the_origins_add_up_to_the_tool_surface():
    loop, _engine, observer = _loop(
        _Tool("local_one", "z" * 400),
        _Tool("mcp_one", "z" * 400, origin="many"),
        _Tool("mcp_two", "z" * 400, origin="many"),
        mcp_chars_warn=100,
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
        mcp_chars_warn=100,
    )
    await loop.process_input(_session(), "一")
    warning = observer.of_type(ToolSurfaceWarning)[0]
    assert dict(warning.origins)["everything"] > dict(warning.origins)["local"]
    assert "everything" in warning.hint


@pytest.mark.asyncio
async def test_the_warning_never_changes_the_request(monkeypatch):
    """R4 的纪律：观测只写不改。开不开告警，请求逐字节相同。

    The clock is frozen on purpose: the environment block embeds
    ``时间: <local time>`` (request_assembly.py:393), and two separately-run
    loops straddle a second boundary on a fast host -- the requests then differ
    by the timestamp, and the assertion fails for a reason that has nothing to
    do with the warning (measured flaky, N-2).
    """
    from ai_coding.core import request_assembly

    monkeypatch.setattr(request_assembly, "now_local", lambda: "2026-01-01 00:00:00")
    quiet, quiet_engine, quiet_events = _loop(
        _Tool("remote", "y" * 3000, origin="mcp"), mcp_chars_warn=0
    )
    loud, loud_engine, loud_events = _loop(
        _Tool("remote", "y" * 3000, origin="mcp"), mcp_chars_warn=100
    )
    await quiet.process_input(_session(), "一")
    await loud.process_input(_session(), "一")
    assert [m.content for m in quiet_engine.requests[-1]] == [
        m.content for m in loud_engine.requests[-1]
    ]
    assert not quiet_events.of_type(ToolSurfaceWarning)
    assert loud_events.of_type(ToolSurfaceWarning)


