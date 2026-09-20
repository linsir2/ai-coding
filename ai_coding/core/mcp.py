"""MCP server management (M5-1).

Builds ``agents.mcp.MCPServerStdio`` objects from our ``MCPConfig``. This module
is an SDK boundary (like the AI service adapter): construction is offline —
actual server connection / tool discovery happens at run time inside the OpenAI
Agents SDK, so building a manager or servers never touches the network.
"""

from __future__ import annotations

from typing import Any

from agents.mcp import MCPServerStdio, MCPServerStdioParams

from ai_coding.config.models import MCPConfig


class MCPManager:
    """Translate an :class:`MCPConfig` into a list of SDK MCP servers.

    Only ``enabled`` servers with a non-blank ``name`` and ``command`` are kept;
    disabled servers and a disabled MCP block yield ``[]``.  This is the single
    config → SDK mapping point so the AI service stays SDK-agnostic about MCP.
    """

    def __init__(self, config: Any | None = None) -> None:
        self._config: MCPConfig = config or MCPConfig()

    def build_servers(self) -> list[Any]:
        """Return SDK MCP servers for the configured (enabled) servers."""
        if not self._config.enabled:
            return []
        servers: list[Any] = []
        for s in self._config.servers:
            if not s.enabled:
                continue
            if not s.name or not s.command:
                continue
            servers.append(
                MCPServerStdio(
                    params=MCPServerStdioParams(
                        command=s.command,
                        args=list(s.args),
                    ),
                    name=s.name,
                    cache_tools_list=True,
                )
            )
        return servers


__all__ = ["MCPManager"]
