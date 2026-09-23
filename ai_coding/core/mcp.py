"""MCP integration over the ``mcp`` package directly (R1a-6).

Replaces the M5 implementation, which produced ``agents.mcp.MCPServerStdio``
objects for the OpenAI Agents SDK.  Now the manager connects stdio servers
itself and exposes each remote tool as an ordinary ``BaseTool`` in the same
registry the dispatcher uses — so MCP tools go through the same permission gate
and the same approval path as local ones.

Design notes (docs/r1a-6-analysis.md §3.2):
  - the transport is injectable, so tests need no subprocess and no live server;
  - tool names are prefixed with the server name, since two servers may expose
    the same tool name;
  - a server that fails to connect is skipped silently: MCP is an enhancement.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from ai_coding.config.models import MCPConfig, MCPServerConfig
from ai_coding.domain.message import ToolResult
from ai_coding.tools.base import BaseTool, ToolContext

SessionFactory = Callable[[MCPServerConfig], Any]


def _default_session_factory(server: MCPServerConfig) -> Any:
    """Connect a stdio MCP server and yield an initialized ``ClientSession``."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    @asynccontextmanager
    async def _cm() -> AsyncIterator[Any]:
        params = StdioServerParameters(command=server.command, args=list(server.args))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                yield session

    return _cm()


def _join_text(content: Any) -> str:
    """Concatenate the text blocks of a ``CallToolResult.content`` list."""
    parts: list[str] = []
    for block in content or []:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts)


class MCPToolAdapter(BaseTool):
    """Exposes one remote MCP tool as a local ``BaseTool``."""

    # Remote side effects are unknown, so never run these concurrently.
    parallel_safe = False

    def __init__(self, *, server: str, tool: Any, session: Any) -> None:
        self._server = server
        self._tool = tool
        self._session = session

    @property
    def name(self) -> str:
        return f"{self._server}__{getattr(self._tool, 'name', 'tool')}"

    @property
    def description(self) -> str:
        return str(getattr(self._tool, "description", "") or "")

    @property
    def params_json_schema(self) -> dict[str, Any]:
        raw = getattr(self._tool, "input_schema", None)
        schema: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        return schema

    async def execute(
        self,
        params: dict[str, Any],
        ctx: ToolContext | None = None,
    ) -> ToolResult:
        tool_name = str(getattr(self._tool, "name", ""))
        try:
            result = await self._session.call_tool(tool_name, params)
        except Exception as exc:  # noqa: BLE001 — tool boundary
            return ToolResult(
                success=False,
                output=f"error: MCP tool '{tool_name}' failed: {exc}",
                error="mcp_error",
                data={"server": self._server, "tool": tool_name},
            )

        is_error = bool(getattr(result, "is_error", False))
        output = _join_text(getattr(result, "content", None))
        if not output:
            output = (
                "error: MCP tool reported a failure"
                if is_error
                else "(empty MCP result)"
            )
        return ToolResult(
            success=not is_error,
            output=output,
            data={
                "server": self._server,
                "tool": tool_name,
                "is_error": is_error,
                "structured": getattr(result, "structured_content", None),
            },
            error="mcp_error" if is_error else None,
        )


class MCPManager:
    """Connects the configured stdio MCP servers and returns their tools."""

    def __init__(
        self,
        config: MCPConfig | None = None,
        *,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._config = config or MCPConfig()
        self._session_factory = session_factory or _default_session_factory
        self._stack = AsyncExitStack()

    async def open_all(self) -> list[BaseTool]:
        """Connect every enabled server; a failure skips just that server."""
        if not self._config.enabled:
            return []
        tools: list[BaseTool] = []
        for server in self._config.servers:
            if not server.enabled or not server.name or not server.command:
                continue
            try:
                session = await self._stack.enter_async_context(
                    self._session_factory(server)
                )
                listed = await session.list_tools()
            except Exception:  # noqa: BLE001 — an MCP server is an enhancement
                continue
            for tool in getattr(listed, "tools", None) or []:
                tools.append(MCPToolAdapter(server=server.name, tool=tool, session=session))
        return tools

    async def aclose(self) -> None:
        """Tear down every session opened by :meth:`open_all` (idempotent)."""
        await self._stack.aclose()


__all__ = ["MCPManager", "MCPToolAdapter"]
