#!/usr/bin/env python3
"""A *real* MCP stdio server with 13 verbose tools (R7d).

`scripts/mcp_echo_server.py` proves the transport; this one is for the other
question — what a talkative server costs.  Every tool schema is re-sent on every
model step, so the number that matters is ``tools_chars`` (``ContextAssembled``),
and the des descriptions are deliberately written the way a real server writes
them (the official ``server-everything`` style, ~350 characters each).

Measured on this machine (docs/r7-analysis.md 1.2):

    no MCP                      4,280 chars
    + 13 verbose tools         14,342 chars   (+235%)
    + 2 of them (allowlist)     5,827 chars   (+36%)

Run it by hand, or point a ``mcp.servers`` entry at it:

    .venv/bin/python scripts/mcp_many_tools_server.py
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

server = FastMCP("many")

_DESCRIPTION = (
    "Performs operation number {index} against the configured target. Accepts a "
    "string argument and returns a human readable description of what it did, "
    "including the resolved path, the number of bytes touched and the elapsed "
    "time in milliseconds. Use this tool when you need to inspect or modify "
    "resources of kind {index} and you want a structured, verbose result back "
    "for the coding agent to reason about."
)


def _make(index: int):
    def tool(x: str = "", flag: bool = False, depth: int = 1) -> str:
        """Placeholder; the docstring is replaced below."""
        return f"tool{index}:{x}:{flag}:{depth}"

    tool.__name__ = f"tool_{index}"
    tool.__doc__ = _DESCRIPTION.format(index=index)
    return tool


for _index in range(13):
    server.tool()(_make(_index))


if __name__ == "__main__":
    server.run(transport="stdio")
