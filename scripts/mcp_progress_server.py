#!/usr/bin/env python3
"""A real MCP stdio server that *reports progress* (R8c-S3).

``tests/test_mcp.py`` injects a fake session; this file exists so that the
progress path has one genuine round trip: a real server, a real pipe, and a tool
that calls ``ctx.report_progress`` a few times.  A server is free to report
nothing - that is why the assertion needs a server that does.

Run it by hand:

    .venv/bin/python scripts/mcp_progress_server.py    # then speak JSON-RPC
"""

from __future__ import annotations

import asyncio

from mcp.server.fastmcp import Context, FastMCP

server = FastMCP("progress-demo")


@server.tool()
async def slow_task(steps: int, ctx: Context) -> str:
    """Do ``steps`` units of work, reporting progress after each one."""
    for index in range(1, int(steps) + 1):
        await ctx.report_progress(index, int(steps), f"step {index}")
        await asyncio.sleep(0.05)
    return f"did {int(steps)} steps"


if __name__ == "__main__":
    server.run(transport="stdio")
