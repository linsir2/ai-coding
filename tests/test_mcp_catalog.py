"""M2: the MCP catalog — every tool the model can call, inside a budget.

One dispatcher (``mcp_call``) replaces one function schema per remote tool.
The R7d measurement is why: 13 tools cost 13,301 characters *per step*, every
step (docs/features.md 538-541).  The catalog is that cost's replacement, so it
is built bounded here, and its caller regenerates it per step from live state
(agent_loop's volatile tail), never freezes it into a schema.

OpenCode bounds the same thing at INLINE_BUDGET = 2,000 tokens
(packages/core/src/codemode/catalog.ts:50); ~4 chars per token is 8,000.
"""

from types import SimpleNamespace

from ai_coding.core.mcp import build_mcp_catalog


def _tool(name="read_file", description="does a thing"):
    return SimpleNamespace(name=name, description=description)


def test_a_server_and_its_tools_are_listed_by_name():
    text = build_mcp_catalog([("fs", [_tool("read_file"), _tool("write_file")])])

    assert "fs" in text
    assert "read_file" in text
    assert "write_file" in text


def test_nothing_connected_is_an_empty_catalog():
    assert build_mcp_catalog([]) == ""


def test_a_long_description_is_cut_rather_than_dropping_the_tool():
    text = build_mcp_catalog([("fs", [_tool("t", description="y" * 500)])])

    assert "y" * 500 not in text
    assert len(text) < 400


def test_the_budget_keeps_configured_servers_and_drops_the_rest_loudly():
    # Six servers of fifteen verbose tools each: more than 8,000 characters of
    # catalog.  Config order is the priority, and what went must be said.
    servers = [
        (f"s{i}", [_tool(f"t{j}", description="z" * 100) for j in range(15)])
        for i in range(6)
    ]

    text = build_mcp_catalog(servers)

    assert len(text) <= 8_000
    assert "s0" in text
    assert "s5" not in text
    assert "omitted" in text
