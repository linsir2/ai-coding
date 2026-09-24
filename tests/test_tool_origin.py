"""R7e-3: every tool says where it came from.

The tool surface is re-sent on every model step, so a cost warning has to name the
expensive part.  Two directions matter and both are pinned here: a built-in tool is
``local``, and an MCP adapter is its server's name.
"""

from __future__ import annotations

from types import SimpleNamespace

from ai_coding.core.mcp import MCPToolAdapter
from ai_coding.tools.todo_write_tool import TodoWriteTool


def test_a_builtin_tool_is_local():
    assert TodoWriteTool().origin == "local"


def test_an_mcp_adapter_carries_its_server():
    adapter = MCPToolAdapter(
        server="everything",
        tool=SimpleNamespace(name="read_file", description="", input_schema={}),
        session=object(),
    )
    assert adapter.origin == "everything"
    assert adapter.name == "everything__read_file"
