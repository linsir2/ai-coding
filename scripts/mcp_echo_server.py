#!/usr/bin/env python3
"""A minimal *real* MCP stdio server, used by the gated round-trip test (R2d).

``tests/test_mcp.py`` injects a fake session; this file exists so that at least
one test spawns a genuine server over a genuine pipe, lists its tools and calls
one.  It is deliberately tiny: it proves the transport, nothing else.

Run it by hand:

    .venv/bin/python scripts/mcp_echo_server.py      # then speak JSON-RPC on stdio
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

server = MCPServer("echo")


@server.tool()
def echo(text: str) -> str:
    """Return the given text prefixed with ``echo:``."""
    return f"echo:{text}"


if __name__ == "__main__":
    server.run(transport="stdio")
