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
from _mcp_wait import until_mcp_settled

from ai_coding.config.models import MCPConfig, MCPServerConfig, MCPTimeout
from ai_coding.core.mcp import MCPManager, MCPToolAdapter


class _Session:
    """Stands in for an initialized MCP ClientSession."""

    def __init__(
        self,
        tools=(),
        call_result=None,
        raise_on_call=None,
        list_delay=0.0,
        call_delay=0.0,
    ) -> None:
        self._tools = list(tools)
        self._call_result = call_result
        self._raise = raise_on_call
        self._list_delay = list_delay
        self._call_delay = call_delay
        self.calls: list[tuple] = []
        self.closed = False

    async def list_tools(self):
        await asyncio.sleep(self._list_delay)
        return SimpleNamespace(tools=self._tools)

    async def call_tool(self, name, arguments=None, progress_callback=None):
        self.calls.append((name, arguments))
        await asyncio.sleep(self._call_delay)
        if self._raise is not None:
            raise self._raise
        if progress_callback is not None:
            for step in range(1, 4):
                progress_callback(step, 3, f"step {step}")
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


def _callable(manager) -> list[str]:
    """``server__tool`` names, from the one source the dispatcher reads (M2)."""
    return [
        f"{server}__{handle.name}"
        for server, handles in manager.available()
        for handle in handles
    ]


def _server(name="fs", command=("mcp-fs",), **kwargs) -> MCPServerConfig:
    """OpenCode-v2 shape: ``command`` is argv as one list (no separate args)."""
    return MCPServerConfig(name=name, command=list(command), **kwargs)


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
        _cfg(
            _server("dead", timeout=MCPTimeout(startup=50)),
            _server("alive", timeout=MCPTimeout(startup=50)),
        ),
        session_factory=hanging_factory,
        observer=observer,
    )

    await manager.open_all()
    assert await until_mcp_settled(manager)
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
        _cfg(_server(disabled=True)),
        session_factory=lambda server: called.append(server),
    )
    await manager.open_all()
    assert await until_mcp_settled(manager)
    assert _callable(manager) == []
    assert called == []


@pytest.mark.asyncio
async def test_disabled_server_skipped():
    called: list = []
    manager = MCPManager(
        _cfg(_server(disabled=True)),
        session_factory=lambda server: called.append(server),
    )
    await manager.open_all()
    assert await until_mcp_settled(manager)
    assert _callable(manager) == []
    assert called == []


@pytest.mark.asyncio
async def test_server_without_name_or_command_skipped():
    called: list = []
    manager = MCPManager(
        _cfg(MCPServerConfig(name="", command=["x"])),
        session_factory=lambda server: called.append(server),
    )
    await manager.open_all()
    assert await until_mcp_settled(manager)
    assert _callable(manager) == []
    assert called == []


# ---- adapter shape ---------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_become_base_tools():
    session = _Session(tools=[_tool("read_file"), _tool("write_file")])
    manager = MCPManager(_cfg(_server()), session_factory=_factory({"fs": session}))

    await manager.open_all()
    assert await until_mcp_settled(manager)

    assert _callable(manager) == ["fs__read_file", "fs__write_file"]
    await manager.aclose()


@pytest.mark.asyncio
async def test_tool_name_prefixed_with_server():
    session = _Session(tools=[_tool("read_file")])
    manager = MCPManager(_cfg(_server("fsx")), session_factory=_factory({"fsx": session}))

    await manager.open_all()
    assert await until_mcp_settled(manager)

    assert _callable(manager) == ["fsx__read_file"]
    await manager.aclose()


@pytest.mark.asyncio
async def test_schema_passed_through():
    schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    session = _Session(tools=[_tool(schema=schema)])
    manager = MCPManager(_cfg(_server()), session_factory=_factory({"fs": session}))

    await manager.open_all()
    assert await until_mcp_settled(manager)

    # M2: the schema and description the model sees are the catalog's, built
    # from the same tool the dispatcher would call.
    handle = manager.lookup("fs", "read_file")
    assert handle is not None and handle.description == "reads"
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

    await manager.open_all()
    assert await until_mcp_settled(manager)

    assert _callable(manager) == ["good__ok"]  # only the healthy server
    await manager.aclose()


@pytest.mark.asyncio
async def test_aclose_closes_sessions():
    session = _Session(tools=[_tool()])
    manager = MCPManager(_cfg(_server()), session_factory=_factory({"fs": session}))
    await manager.open_all()
    assert await until_mcp_settled(manager)

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

    await manager.open_all()
    assert await until_mcp_settled(manager)

    assert _callable(manager) == ["dup__read_file"]
    await manager.aclose()


# ---- R2d: real connections are bounded and configurable --------------------


def test_server_parameters_split_command_argv_and_carry_env_and_cwd():
    from ai_coding.core.mcp import _server_parameters

    cfg = MCPServerConfig(
        name="fs",
        command=["/usr/bin/python3", "-m", "server"],
        environment={"TOKEN": "x"},
        cwd="/tmp/ws",
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
        _cfg(_server("dead", timeout=MCPTimeout(startup=100))),
        session_factory=hanging_factory,
    )

    started = time.monotonic()
    await manager.open_all()
    assert await until_mcp_settled(manager)
    elapsed = time.monotonic() - started

    assert _callable(manager) == []
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
    async def fake_stdio_client(params, errlog=None):
        events.append("spawn")
        # M3: the child's stderr is routed to our log rather than the terminal,
        # so the factory hands stdio_client a sink (see _StdErrLog).
        events.append("errlog" if errlog is not None else "no-errlog")
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

    cfg = MCPServerConfig(name="s", command=["x"])
    async with _default_session_factory(cfg) as session:
        assert isinstance(session, FakeSession)

    assert events == ["spawn", "errlog", "enter", "initialize", "exit"]


# ---- v2 split: startup bounds the connection, catalog bounds the listing -----


@pytest.mark.asyncio
async def test_a_slow_tool_listing_costs_the_catalog_budget_not_startup():
    """A server that connects instantly but never answers tools/list must be
    charged to the catalog budget, not spend the whole startup one — otherwise
    a hung listing hides behind a budget sized for npx cold starts."""
    import time

    session = _Session(list_delay=30)
    manager = MCPManager(
        _cfg(_server("slow", timeout=MCPTimeout(startup=60_000, catalog=50))),
        session_factory=_factory({"slow": session}),
    )

    started = time.monotonic()
    await manager.open_all()
    assert await until_mcp_settled(manager)
    elapsed = time.monotonic() - started

    assert _callable(manager) == []
    assert elapsed < 2.0
    row = _rows(manager)["slow"]
    assert row["status"] == "failed"
    assert "工具列表" in row["detail"] and "0.05" in row["detail"]
    await manager.aclose()


@pytest.mark.asyncio
async def test_an_execution_timeout_bounds_one_hanging_tool_call():
    """A per-server execution cap: the turn deadline bounds the whole step, this
    bounds one remote call so it cannot eat the step by itself."""
    import time

    session = _Session(call_delay=30)
    adapter = MCPToolAdapter(
        server="fs", tool=_tool(), session=session, execution_timeout=0.05
    )

    started = time.monotonic()
    result = await adapter.execute({}, None)
    elapsed = time.monotonic() - started

    assert result.success is False
    assert result.error == "mcp_error"
    assert elapsed < 2.0


# ---- R7d: the registration allowlist, and the connection default -------------


@pytest.mark.asyncio
async def test_the_tools_allowlist_filters_the_adapters():
    session = _Session(tools=[_tool("read_file"), _tool("write_file"), _tool("list_dir")])
    manager = MCPManager(
        _cfg(_server(tools=["read_file", "list_dir"])), session_factory=_factory({"fs": session})
    )
    try:
        await manager.open_all()
        assert await until_mcp_settled(manager)
        assert _callable(manager) == ["fs__read_file", "fs__list_dir"]
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_an_empty_allowlist_keeps_every_tool():
    session = _Session(tools=[_tool("read_file"), _tool("write_file")])
    manager = MCPManager(_cfg(_server()), session_factory=_factory({"fs": session}))
    try:
        await manager.open_all()
        assert await until_mcp_settled(manager)
        assert _callable(manager) == ["fs__read_file", "fs__write_file"]
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_the_allowlist_matches_remote_names_not_the_prefix():
    session = _Session(tools=[_tool("read_file")])
    manager = MCPManager(
        _cfg(_server(tools=["fs__read_file"])),  # the adapter name, not the remote one
        session_factory=_factory({"fs": session}),
    )
    try:
        await manager.open_all()
        assert await until_mcp_settled(manager)
        # The whitelist matches the *remote* name; the adapter name never matches.
        assert _callable(manager) == []
    finally:
        await manager.aclose()


def test_the_startup_timeout_default_gives_npx_room_to_start():
    from ai_coding.config.models import MCPServerConfig

    # R7d: 30s left ~3s of headroom against a measured 27s npx cold start, so the
    # default is now 60s (docs/r7-analysis.md 3.4, r7-backlog.md 3.3).  The two
    # other budgets are the v2 split: listing tools and one call.
    timeout = MCPServerConfig().timeout
    assert timeout.startup == 60_000
    assert timeout.catalog == 30_000
    assert timeout.execution is None  # the turn deadline stays the only bound


@pytest.mark.asyncio
async def test_r8_mcp_progress_becomes_a_tool_chunk():
    """R8c-S3: the adapter turns report_progress into the same ToolChunk stream."""
    from ai_coding.core.events import ToolChunk
    from ai_coding.tools.base import ToolContext

    session = _Session(call_result=_result(texts=["done"]))
    adapter = MCPToolAdapter(server="fs", tool=_tool(), session=session)
    seen: list[object] = []

    result = await adapter.execute(
        {"path": "a.txt"}, ToolContext(emit=seen.append, call_id="c7")
    )

    chunks = [event for event in seen if isinstance(event, ToolChunk)]
    assert result.success is True
    assert [chunk.stream for chunk in chunks] == ["progress", "progress", "progress"]
    assert [chunk.text for chunk in chunks] == ["step 1", "step 2", "step 3"]
    assert all(chunk.call_id == "c7" and chunk.tool_name == "fs__read_file" for chunk in chunks)


# ---- m1-mcp: what became of every configured server ------------------------
#
# The manager already knew all of this; nothing carried it out of the process.
# These rows are the contract the IPC handler hands the dialog.


def _rows(manager) -> list[dict]:
    return {row["name"]: row for row in manager.servers()}


@pytest.mark.asyncio
async def test_a_connected_server_reports_its_tool_count():
    """T1: the happy row is connected + how many tools it brought."""
    session = _Session([_tool("read_file"), _tool("write_file")])
    manager = MCPManager(_cfg(_server("fs")), session_factory=_factory({"fs": session}))
    await manager.open_all()
    assert await until_mcp_settled(manager)
    try:
        row = _rows(manager)["fs"]
        assert row["status"] == "connected"
        assert row["toolCount"] == 2
        assert row["wantedCount"] == 0  # no whitelist in play
        assert row["detail"] == ""
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_a_server_that_never_connects_is_a_row_not_an_absence():
    """T3, the line this feature stands on: a server whose command is wrong has
    to stay visible.  It got an owner and failed, so the row says failed."""
    def broken_factory(server):
        @asynccontextmanager
        async def _cm():
            raise RuntimeError("spawn failed: no such command")
            yield  # pragma: no cover

        return _cm()

    manager = MCPManager(_cfg(_server("dead")), session_factory=broken_factory)
    await manager.open_all()
    assert await until_mcp_settled(manager)
    try:
        row = _rows(manager)["dead"]
        assert row["status"] == "failed"
        assert "spawn failed" in row["detail"]
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_a_server_that_times_out_is_reported_as_failed():
    """T2: ready is set on both paths, so failure has to be read from .error
    (or its absence), not from ready alone."""
    def hanging_factory(server):
        @asynccontextmanager
        async def _cm():
            await asyncio.sleep(30)  # never connects
            yield _Session()

        return _cm()

    manager = MCPManager(
        _cfg(_server("slow", timeout=MCPTimeout(startup=50))),
        session_factory=hanging_factory,
    )
    await manager.open_all()
    assert await until_mcp_settled(manager)
    try:
        row = _rows(manager)["slow"]
        assert row["status"] == "failed"
        assert row["detail"]
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_a_whitelist_that_filtered_tools_says_which_ones():
    """T4: R7d keeps 1 of 3 tools, and that cost has to be visible."""
    session = _Session([_tool("read_file"), _tool("write_file"), _tool("glob")])
    manager = MCPManager(
        _cfg(_server("fs", tools=["read_file"])), session_factory=_factory({"fs": session})
    )
    await manager.open_all()
    assert await until_mcp_settled(manager)
    try:
        row = _rows(manager)["fs"]
        assert row["status"] == "filtered"
        assert row["toolCount"] == 1
        assert row["wantedCount"] == 1
        assert "write_file" in row["detail"] and "glob" in row["detail"]
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_a_whitelist_that_matched_everything_is_not_a_filter():
    """T4b: all three wanted, three arrived — that is connected, not filtered."""
    session = _Session([_tool("read_file"), _tool("write_file"), _tool("glob")])
    manager = MCPManager(
        _cfg(_server("fs", tools=["read_file", "write_file", "glob"])),
        session_factory=_factory({"fs": session}),
    )
    await manager.open_all()
    assert await until_mcp_settled(manager)
    try:
        row = _rows(manager)["fs"]
        assert row["status"] == "connected"
        assert row["detail"] == ""
    finally:
        await manager.aclose()


def test_a_server_the_loader_skips_still_gets_a_row():
    """T3b: no name or no command is skipped before an owner exists, so the
    config is the only record — and a typo must not vanish from the list."""
    manager = MCPManager(
        _cfg(_server(name="", command=["mcp-fs"]), _server(command=[]))
    )
    rows = _rows(manager)
    assert len(rows) == 2
    for row in rows.values():
        assert row["status"] == "failed"
        assert row["detail"]


@pytest.mark.asyncio
async def test_a_dead_child_says_why_not_just_that_it_died():
    """M3: anyio reports a child that died as a TaskGroup, whose str() is always
    the same useless sentence.  The row has to carry the leaf."""
    class _Group(BaseException):
        """Stands in for anyio's group: the detail unwraps whatever has
        ``.exceptions`` (duck-typed on purpose), so the shape is what matters."""

        def __init__(self, subs: list[BaseException]) -> None:
            self.exceptions = list(subs)

        def __str__(self) -> str:
            return "unhandled errors in a TaskGroup (1 sub-exception)"

    inner = RuntimeError("No such file or directory: '/tmp/nope.py'")
    group = _Group([inner])

    def broken_factory(server):
        @asynccontextmanager
        async def _cm():
            raise group
            yield  # pragma: no cover

        return _cm()

    manager = MCPManager(_cfg(_server("dead")), session_factory=broken_factory)
    await manager.open_all()
    assert await until_mcp_settled(manager)
    try:
        row = _rows(manager)["dead"]
        assert row["status"] == "failed"
        assert "No such file or directory" in row["detail"]
        assert "TaskGroup" not in row["detail"]
    finally:
        await manager.aclose()


def test_a_childs_stderr_reaches_the_row_when_it_has_something_to_say():
    """M3: the most common real failure (a script that is not there, a package
    that was never installed) is visible *only* in the child's stderr, so the
    capture's tail joins the reason."""
    from ai_coding.core.mcp import _MCPSessionOwner

    owner = _MCPSessionOwner(_server("fs"), lambda server: None)
    owner.stderr_file.write(b"python: can't open file '/tmp/nope.py'\n")
    owner.stderr_file.flush()

    assert "can't open file" in owner.stderr_tail()


def test_with_mcp_off_every_configured_server_says_so():
    """MCP-MCP §2.1: an empty list would make "not enabled" and "not configured"
    look the same."""
    manager = MCPManager(_cfg(_server("fs"), enabled=False))
    rows = manager.servers()
    assert [row["name"] for row in rows] == ["fs"]
    assert rows[0]["status"] == "off"
    assert rows[0]["toolCount"] == 0
