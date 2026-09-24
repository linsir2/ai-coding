"""TDD (R1a-6): MCP over the ``mcp`` package directly.

Rewritten from the M5 spec, which built ``agents.mcp.MCPServerStdio`` objects.
The manager now connects stdio servers itself and exposes their tools as ordinary
``BaseTool`` adapters in the same registry the dispatcher uses.

The transport is injected: these tests never spawn a subprocess (this sandbox
cannot do that reliably, and a unit test should not need a live server anyway).
"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from ai_coding.config.models import MCPConfig, MCPServerConfig
from ai_coding.core.mcp import MCPManager, MCPToolAdapter
from ai_coding.tools.base import BaseTool


class _Session:
    """Stands in for an initialized MCP ClientSession."""

    def __init__(self, tools=(), call_result=None, raise_on_call=None) -> None:
        self._tools = list(tools)
        self._call_result = call_result
        self._raise = raise_on_call
        self.calls: list[tuple] = []
        self.closed = False

    async def list_tools(self):
        return SimpleNamespace(tools=self._tools)

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        if self._raise is not None:
            raise self._raise
        return self._call_result


def _tool(name="read_file", description="reads", schema=None):
    return SimpleNamespace(
        name=name,
        description=description,
        input_schema=schema or {"type": "object", "properties": {"path": {"type": "string"}}},
    )


def _result(texts=(), structured=None, is_error=False):
    content = [SimpleNamespace(text=t) for t in texts]
    return SimpleNamespace(content=content, structured_content=structured, is_error=is_error)


def _factory(sessions: dict):
    """A session_factory over a name -> session (or Exception) table."""

    def factory(server):
        @asynccontextmanager
        async def _cm():
            item = sessions.get(server.name)
            if isinstance(item, Exception):
                raise item
            try:
                yield item
            finally:
                if hasattr(item, "closed"):
                    item.closed = True

        return _cm()

    return factory


def _cfg(*servers, enabled=True) -> MCPConfig:
    return MCPConfig(enabled=enabled, servers=list(servers))


def _server(name="fs", command="mcp-fs", enabled=True) -> MCPServerConfig:
    return MCPServerConfig(name=name, command=command, enabled=enabled)


@pytest.mark.asyncio
async def test_a_server_that_never_connects_is_reported_not_silent():
    """R4a: skipping a broken server is fine, skipping it *silently* is not."""
    from ai_coding.core.events import Degraded, RecordingObserver

    def hanging_factory(server):
        @asynccontextmanager
        async def _cm():
            await asyncio.sleep(30)  # never connects
            yield _Session()

        return _cm()

    observer = RecordingObserver()
    manager = MCPManager(
        _cfg(_server("dead"), _server("alive")),
        session_factory=hanging_factory,
        connection_timeout=0.05,
        observer=observer,
    )

    await manager.open_all()
    await manager.aclose()

    degraded = observer.of_type(Degraded)
    assert [event.component for event in degraded] == ["mcp_server", "mcp_server"]
    assert "dead" in degraded[0].reason
    assert degraded[0].fallback


# ---- gating ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_config_yields_no_tools():
    called: list = []

    manager = MCPManager(
        _cfg(_server(), enabled=False),
        session_factory=lambda server: called.append(server),
    )
    assert await manager.open_all() == []
    assert called == []


@pytest.mark.asyncio
async def test_disabled_server_skipped():
    called: list = []
    manager = MCPManager(
        _cfg(_server(enabled=False)),
        session_factory=lambda server: called.append(server),
    )
    assert await manager.open_all() == []
    assert called == []


@pytest.mark.asyncio
async def test_server_without_name_or_command_skipped():
    called: list = []
    manager = MCPManager(
        _cfg(MCPServerConfig(name="", command="x", enabled=True)),
        session_factory=lambda server: called.append(server),
    )
    assert await manager.open_all() == []
    assert called == []


# ---- adapter shape ---------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_become_base_tools():
    session = _Session(tools=[_tool("read_file"), _tool("write_file")])
    manager = MCPManager(_cfg(_server()), session_factory=_factory({"fs": session}))

    tools = await manager.open_all()

    assert len(tools) == 2
    assert all(isinstance(tool, BaseTool) for tool in tools)
    await manager.aclose()


@pytest.mark.asyncio
async def test_tool_name_prefixed_with_server():
    session = _Session(tools=[_tool("read_file")])
    manager = MCPManager(_cfg(_server("fsx")), session_factory=_factory({"fsx": session}))

    tools = await manager.open_all()

    assert tools[0].name == "fsx__read_file"
    await manager.aclose()


@pytest.mark.asyncio
async def test_schema_passed_through():
    schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    session = _Session(tools=[_tool(schema=schema)])
    manager = MCPManager(_cfg(_server()), session_factory=_factory({"fs": session}))

    tools = await manager.open_all()

    assert tools[0].params_json_schema == schema
    assert tools[0].description == "reads"
    await manager.aclose()


def test_adapter_is_not_parallel_safe():
    adapter = MCPToolAdapter(server="fs", tool=_tool(), session=_Session())
    assert adapter.parallel_safe is False


# ---- execution -------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_joins_text_content():
    session = _Session(call_result=_result(texts=["line one", "line two"]))
    adapter = MCPToolAdapter(server="fs", tool=_tool(), session=session)

    result = await adapter.execute({"path": "a.txt"})

    assert result.success is True
    assert result.output == "line one\nline two"
    assert session.calls == [("read_file", {"path": "a.txt"})]


@pytest.mark.asyncio
async def test_execute_reports_structured_content():
    session = _Session(call_result=_result(texts=["ok"], structured={"lines": 3}))
    adapter = MCPToolAdapter(server="fs", tool=_tool(), session=session)

    result = await adapter.execute({})

    assert result.data is not None
    assert result.data["structured"] == {"lines": 3}
    assert result.data["server"] == "fs"
    assert result.data["tool"] == "read_file"


@pytest.mark.asyncio
async def test_execute_marks_is_error():
    session = _Session(call_result=_result(texts=["boom"], is_error=True))
    adapter = MCPToolAdapter(server="fs", tool=_tool(), session=session)

    result = await adapter.execute({})

    assert result.success is False
    assert result.error == "mcp_error"
    assert "boom" in result.output


@pytest.mark.asyncio
async def test_execute_call_failure_is_tool_result():
    session = _Session(raise_on_call=RuntimeError("transport died"))
    adapter = MCPToolAdapter(server="fs", tool=_tool(), session=session)

    result = await adapter.execute({})

    assert result.success is False
    assert result.error == "mcp_error"
    assert "transport died" in result.output


# ---- lifecycle -------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_failure_degrades_silently():
    good = _Session(tools=[_tool("ok")])
    manager = MCPManager(
        _cfg(_server("broken"), _server("good")),
        session_factory=_factory(
            {"broken": RuntimeError("cannot start"), "good": good}
        ),
    )

    tools = await manager.open_all()

    assert [tool.name for tool in tools] == ["good__ok"]  # only the healthy server
    await manager.aclose()


@pytest.mark.asyncio
async def test_aclose_closes_sessions():
    session = _Session(tools=[_tool()])
    manager = MCPManager(_cfg(_server()), session_factory=_factory({"fs": session}))
    await manager.open_all()

    await manager.aclose()

    assert session.closed is True
    # closing twice (or without opening) must stay safe
    await manager.aclose()


@pytest.mark.asyncio
async def test_duplicate_server_names_are_skipped():
    """Two servers sharing a name would produce identical tool names.

    Registering both would raise from ToolRegistry and escape as a CLI
    traceback, which contradicts 'an MCP server is an enhancement'.
    """
    session = _Session(tools=[_tool("read_file")])
    manager = MCPManager(
        _cfg(_server("dup"), _server("dup")),
        session_factory=_factory({"dup": session}),
    )

    tools = await manager.open_all()

    assert [tool.name for tool in tools] == ["dup__read_file"]
    await manager.aclose()


# ---- R2d: real connections are bounded and configurable --------------------


def test_server_parameters_carry_command_args_env_and_cwd():
    from ai_coding.core.mcp import _server_parameters

    cfg = MCPServerConfig(
        name="fs",
        command="/usr/bin/python3",
        args=["-m", "server"],
        env={"TOKEN": "x"},
        cwd="/tmp/ws",
        enabled=True,
    )

    params = _server_parameters(cfg)

    assert params.command == "/usr/bin/python3"
    assert params.args == ["-m", "server"]
    assert params.env == {"TOKEN": "x"}
    assert str(params.cwd) == "/tmp/ws"


@pytest.mark.asyncio
async def test_open_all_skips_a_server_that_never_connects():
    """A dead server must cost the connection budget, not the whole startup."""
    import asyncio
    import time

    def hanging_factory(server):
        @asynccontextmanager
        async def _cm():
            await asyncio.sleep(30)  # never connects
            yield _Session()

        return _cm()

    manager = MCPManager(
        _cfg(_server("dead")),
        session_factory=hanging_factory,
        connection_timeout=0.1,
    )

    started = time.monotonic()
    tools = await manager.open_all()
    elapsed = time.monotonic() - started

    assert tools == []
    assert elapsed < 2.0
    await manager.aclose()


@pytest.mark.asyncio
async def test_default_session_factory_performs_the_handshake(monkeypatch):
    """A real MCP session must be *initialized* before it is handed over.

    Regression for R2d: ``ClientSession.__aenter__`` only starts the dispatcher.
    The R1a-6 factory never called ``initialize()``, so a real server rejected
    every request with -32602 and only the gated stdio test could see it.
    """
    import mcp
    import mcp.client.stdio as stdio_mod

    from ai_coding.core.mcp import _default_session_factory

    events: list[str] = []

    @asynccontextmanager
    async def fake_stdio_client(params):
        events.append("spawn")
        yield "read", "write"

    class FakeSession:
        def __init__(self, read, write) -> None:
            self.read, self.write = read, write

        async def __aenter__(self):
            events.append("enter")
            return self

        async def __aexit__(self, *exc):
            events.append("exit")
            return False

        async def initialize(self):
            events.append("initialize")

    monkeypatch.setattr(stdio_mod, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(mcp, "ClientSession", FakeSession)

    cfg = MCPServerConfig(name="s", command="x", enabled=True)
    async with _default_session_factory(cfg) as session:
        assert isinstance(session, FakeSession)

    assert events == ["spawn", "enter", "initialize", "exit"]
