"""M2: ``mcp_call`` — the one tool the model sees for every MCP server.

It replaces one registered ``BaseTool`` per remote tool, which is what made the
tool surface grow with the server's (13 tools = 13,301 characters per step,
docs/features.md 538-541).  The failure modes are the contract: a hallucinated
server or tool must come back as a result naming the real ones, never as an
exception out of the tool boundary (data-contracts.md:126).
"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from ai_coding.config.models import MCPConfig, MCPServerConfig, MCPToolRules
from ai_coding.core.mcp import MCPManager, build_mcp_catalog
from ai_coding.domain.message import ToolResult
from ai_coding.tools.mcp_call import MCPCallTool
from ai_coding.tools.registry import ToolRegistry

pytestmark = pytest.mark.asyncio


class _Session:
    def __init__(self, tools=(), call_result=None, call_delay=0.0) -> None:
        self._tools = list(tools)
        self._call_result = call_result
        self._call_delay = call_delay
        self.calls: list[tuple] = []

    async def list_tools(self):
        return SimpleNamespace(tools=self._tools)

    async def call_tool(self, name, arguments=None, progress_callback=None):
        self.calls.append((name, arguments))
        await asyncio.sleep(self._call_delay)
        return self._call_result


def _tool(name="read_file"):
    return SimpleNamespace(name=name, description="reads", input_schema={"type": "object"})


def _result(texts=("ok",), is_error=False):
    return SimpleNamespace(
        content=[SimpleNamespace(text=t) for t in texts],
        structured_content=None,
        is_error=is_error,
    )


def _factory(sessions: dict):
    def factory(server):
        @asynccontextmanager
        async def _cm():
            item = sessions.get(server.name)
            if isinstance(item, Exception):
                raise item
            yield item

        return _cm()

    return factory


def _manager(*servers: MCPServerConfig, sessions: dict | None = None) -> MCPManager:
    return MCPManager(
        MCPConfig(enabled=True, servers=list(servers)),
        session_factory=_factory(sessions or {}),
    )


async def _tools(manager: MCPManager) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(MCPCallTool(manager))
    return registry


async def test_every_server_shares_one_tool():
    """The point of the milestone: N servers, one schema in the request."""
    fs = _Session([_tool("read_file")])
    web = _Session([_tool("search")])
    manager = _manager(
        MCPServerConfig(name="fs", command=["mcp-fs"]),
        MCPServerConfig(name="web", command=["npx", "web"]),
        sessions={"fs": fs, "web": web},
    )
    await manager.open_all()

    registry = await _tools(manager)

    assert [tool.name for tool in registry.all_tools] == ["mcp_call"]
    await manager.aclose()


async def test_a_call_reaches_the_right_server_and_tool():
    session = _Session([_tool("read_file")], call_result=_result(("contents",)))
    manager = _manager(
        MCPServerConfig(name="fs", command=["mcp-fs"]), sessions={"fs": session}
    )
    await manager.open_all()
    tool = (await _tools(manager)).get("mcp_call")

    result = await tool.execute(
        {"server": "fs", "tool": "read_file", "input": {"path": "a.txt"}}
    )

    assert result.success is True
    assert result.output == "contents"
    assert session.calls == [("read_file", {"path": "a.txt"})]
    await manager.aclose()


async def test_an_unknown_server_comes_back_naming_the_real_ones():
    session = _Session([_tool("read_file")])
    manager = _manager(
        MCPServerConfig(name="fs", command=["mcp-fs"]), sessions={"fs": session}
    )
    await manager.open_all()
    tool = (await _tools(manager)).get("mcp_call")

    result = await tool.execute({"server": "nope", "tool": "read_file", "input": {}})

    assert result.success is False
    assert result.error == "mcp_error"
    assert "fs" in result.output
    await manager.aclose()


async def test_an_unknown_tool_comes_back_naming_that_server_tools():
    session = _Session([_tool("read_file")])
    manager = _manager(
        MCPServerConfig(name="fs", command=["mcp-fs"]), sessions={"fs": session}
    )
    await manager.open_all()
    tool = (await _tools(manager)).get("mcp_call")

    result = await tool.execute({"server": "fs", "tool": "delete_all", "input": {}})

    assert result.success is False
    assert "read_file" in result.output
    await manager.aclose()


async def test_a_server_that_never_connected_says_so():
    def broken(server):
        @asynccontextmanager
        async def _cm():
            raise RuntimeError("spawn failed: no such command")
            yield  # pragma: no cover

        return _cm()

    manager = MCPManager(
        MCPConfig(enabled=True, servers=[MCPServerConfig(name="dead", command=["x"])]),
        session_factory=broken,
    )
    await manager.open_all()
    tool = (await _tools(manager)).get("mcp_call")

    result = await tool.execute({"server": "dead", "tool": "read_file", "input": {}})

    assert result.success is False
    assert "not connected" in result.output
    assert "spawn failed" in result.output
    await manager.aclose()


@pytest.mark.asyncio
async def test_the_gate_judges_the_real_call_not_the_vessel():
    """What the operator approves must be fs__write_file, not mcp_call."""
    session = _Session([_tool("read_file"), _tool("write_file")])
    manager = _manager(
        MCPServerConfig(name="fs", command=["mcp-fs"]), sessions={"fs": session}
    )
    await manager.open_all()

    tool = MCPCallTool(manager)

    assert (
        tool.permission_name({"server": "fs", "tool": "write_file"}) == "fs__write_file"
    )
    # A hallucinated call is judged as the vessel itself, which the gate allows;
    # execute() is where it comes back as a result naming the real tools.
    assert (
        tool.permission_name({"server": "fs", "tool": "nope"})
        == "mcp_call"
    )


async def test_a_whitelisted_out_tool_is_not_callable_either():
    """R7d's whitelist cut the surface; the catalog and the dispatcher agree."""
    session = _Session([_tool("read_file"), _tool("write_file")])
    manager = _manager(
        MCPServerConfig(name="fs", command=["mcp-fs"], tools=["read_file"]),
        sessions={"fs": session},
    )
    await manager.open_all()
    tool = (await _tools(manager)).get("mcp_call")

    result = await tool.execute({"server": "fs", "tool": "write_file", "input": {}})

    assert result.success is False
    assert session.calls == []
    await manager.aclose()


async def test_a_remote_error_result_is_not_an_exception():
    session = _Session([_tool("read_file")], call_result=_result(("boom",), is_error=True))
    manager = _manager(
        MCPServerConfig(name="fs", command=["mcp-fs"]), sessions={"fs": session}
    )
    await manager.open_all()
    tool = (await _tools(manager)).get("mcp_call")

    result = await tool.execute({"server": "fs", "tool": "read_file", "input": {}})

    assert isinstance(result, ToolResult)
    assert result.success is False
    assert result.error == "mcp_error"
    await manager.aclose()


async def test_a_denied_tool_still_has_a_place_in_the_catalog():
    """Visibility and authorization are separate questions: the manager does
    not read permissions, so a tool the gate refuses is still *listed* — which
    is what makes the refusal legible instead of an unexplained absence."""
    session = _Session([_tool("read_file"), _tool("write_file")])
    manager = _manager(
        MCPServerConfig(
            name="fs",
            command=["mcp-fs"],
            permissions=MCPToolRules(deny=["write_file"]),
        ),
        sessions={"fs": session},
    )
    await manager.open_all()

    listed = [h.name for _server, handles in manager.available() for h in handles]

    assert listed == ["read_file", "write_file"]
    await manager.aclose()


async def test_the_catalog_builder_sees_exactly_what_the_dispatcher_can_call():
    """One source for both: what the model reads and what it can invoke."""
    fs = _Session([_tool("read_file")])
    web = _Session([_tool("search")])
    manager = _manager(
        MCPServerConfig(name="fs", command=["mcp-fs"]),
        MCPServerConfig(name="web", command=["npx", "web"]),
        sessions={"fs": fs, "web": web},
    )
    await manager.open_all()

    catalog = build_mcp_catalog(manager.available())

    assert "fs" in catalog and "read_file" in catalog
    assert "web" in catalog and "search" in catalog
    await manager.aclose()
