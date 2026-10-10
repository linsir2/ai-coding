"""Gated integration test (R2d): a *real* MCP stdio round trip.

Why it is gated: this project is developed inside a sandbox whose seccomp filter
makes the asyncio self-pipe write fail with EPERM, so an event loop can only be
woken by its own timers.  Spawning a *second* Python asyncio program (which is
what an MCP server is) therefore cannot work there — the child never answers its
first ``initialize`` (measurements: docs/r2-analysis.md section 1.5).  On a
normal OS the same round trip takes about 1.6s, so CI enables it.

Run it yourself:

    AI_CODING_MCP_STDIO=1 .venv/bin/python -m pytest tests/test_mcp_stdio.py -q
"""

import os
import sys
from pathlib import Path

import pytest

from ai_coding.config.models import MCPConfig, MCPServerConfig, MCPTimeout
from ai_coding.core.mcp import MCPManager, MCPToolAdapter
from ai_coding.tools.base import ToolContext

GATE = "AI_CODING_MCP_STDIO"
SERVER = Path(__file__).resolve().parent.parent / "scripts" / "mcp_echo_server.py"

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get(GATE) != "1",
        reason=(
            f"real MCP stdio round trip needs a normal OS; set {GATE}=1 "
            "(docs/r2-analysis.md section 1.5)"
        ),
    ),
]


def _config() -> MCPConfig:
    return MCPConfig(
        enabled=True,
        servers=[
            MCPServerConfig(
                name="echo",
                command=[sys.executable, str(SERVER)],
                timeout=MCPTimeout(startup=30_000),
            )
        ],
    )


async def test_real_stdio_server_lists_and_answers():
    manager = MCPManager(_config())
    await manager.open_all()
    try:
        handle = manager.lookup("echo", "echo")
        assert handle is not None
        adapter = MCPToolAdapter(
            server=handle.server, tool=handle.tool, session=handle.session
        )
        assert adapter.name == "echo__echo"
        assert isinstance(adapter, MCPToolAdapter)
        assert adapter.params_json_schema.get("type") == "object"

        result = await adapter.execute({"text": "hello"}, ToolContext())

        assert result.success is True
        assert "echo:hello" in result.output
        assert result.data is not None
        assert result.data["server"] == "echo"
        assert result.data["is_error"] is False
    finally:
        await manager.aclose()
