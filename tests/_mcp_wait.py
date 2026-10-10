"""Wait for the MCP supervisors' first attempts, for tests that used to get a
synchronous ``open_all``.

M3 made startup non-blocking (the operator's choice, OpenCode's fork): a row
says ``connecting`` until its first attempt settles, so a test that wants to
assert on a connected server now waits for that settle exactly like production
does — nothing about the assertion changes.
"""

from __future__ import annotations

import asyncio
import time


async def until_mcp_settled(manager, timeout: float = 5.0, step: float = 0.005) -> bool:
    """True once no configured server is still on its first attempt."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = manager.servers()
        if rows and all(row["status"] != "connecting" for row in rows):
            return True
        await asyncio.sleep(step)
    rows = manager.servers()
    return bool(rows) and all(row["status"] != "connecting" for row in rows)
