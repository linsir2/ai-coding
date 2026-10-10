"""Gated integration test (R8c-S3): a real server's progress becomes ToolChunks.

The adapter is unit-tested with a fake session in ``tests/test_mcp.py``; this one
proves the same thing over a genuine stdio pipe, against a server that really
calls ``ctx.report_progress``.  Gated for the same reason as
``tests/test_mcp_stdio.py``: it spawns a second Python process.

Run it yourself:

    AI_CODING_MCP_STDIO=1 .venv/bin/python -m pytest tests/test_mcp_progress.py -q
"""

import os
import sys
from pathlib import Path

import pytest
from _mcp_wait import until_mcp_settled

from ai_coding.config.models import MCPConfig, MCPServerConfig
from ai_coding.core.events import ToolChunk
from ai_coding.core.mcp import MCPManager, MCPToolAdapter
from ai_coding.tools.base import ToolContext

GATE = "AI_CODING_MCP_STDIO"
SERVER = Path(__file__).resolve().parent.parent / "scripts" / "mcp_progress_server.py"

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get(GATE) != "1",
        reason=(
            f"a real MCP stdio round trip needs a normal OS; set {GATE}=1 "
            "(docs/r2-analysis.md section 1.5)"
        ),
    ),
]


def _config() -> MCPConfig:
    return MCPConfig(
        enabled=True,
        connection_timeout=30,
        servers=[
            MCPServerConfig(
                name="progress",
                command=[sys.executable, str(SERVER)],
                enabled=True,
            )
        ],
    )


async def test_a_real_server_progress_reaches_us_as_chunks():
    manager = MCPManager(_config())
    await manager.open_all()
    assert await until_mcp_settled(manager)
    try:
        handle = manager.lookup("progress", "slow_task")
        assert handle is not None
        adapter = MCPToolAdapter(
            server=handle.server, tool=handle.tool, session=handle.session
        )
        seen: list[object] = []

        result = await adapter.execute(
            {"steps": 3}, ToolContext(emit=seen.append, call_id="c9")
        )

        chunks = [event for event in seen if isinstance(event, ToolChunk)]
        assert result.success is True
        assert [chunk.stream for chunk in chunks] == ["progress", "progress", "progress"]
        assert [chunk.text for chunk in chunks] == ["step 1", "step 2", "step 3"]
        assert all(chunk.call_id == "c9" for chunk in chunks)
    finally:
        await manager.aclose()
