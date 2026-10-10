"""R7d: trust in an MCP server is declared, not assumed.

Every remote tool arrives as an unknown name, and the gate is fail-closed, so
`everything__read_file` landed in the WARN tier: configured once, prompted on
*every call* (docs/r7-analysis.md 1.5, r7-backlog.md 3.1).

A server that the user declared in their own config can say so, and only then do
its tools skip the prompt.  Everything else — the hard-deny list, the local tool
table, unknown tools — behaves exactly as before, and a server that says nothing
keeps prompting.
"""

from __future__ import annotations

from ai_coding.config.models import (
    AIConfig,
    AppConfig,
    MCPConfig,
    MCPServerConfig,
    MCPToolRules,
    ToolsConfig,
)
from ai_coding.security.permission_gate import PermissionDecision, PermissionGate


def _app(*servers: MCPServerConfig) -> AppConfig:
    return AppConfig(
        tools=ToolsConfig(),
        ai=AIConfig(),
        mcp=MCPConfig(enabled=True, servers=list(servers)),
    )


def _server(name: str, **kwargs: object) -> MCPServerConfig:
    return MCPServerConfig(name=name, command=["npx"], **kwargs)


# ------------------------------------------------------------------ default


def test_a_remote_tool_still_prompts_by_default():
    gate = PermissionGate()
    assert gate.check("everything__read_file", {}) == PermissionDecision.WARN


def test_from_config_without_a_trusted_server_changes_nothing():
    gate = PermissionGate.from_config(_app(_server("everything")))
    assert gate.check("everything__read_file", {}) == PermissionDecision.WARN


# ------------------------------------------------------------------- trust


def test_an_auto_approve_server_is_allowed():
    gate = PermissionGate.from_config(_app(_server("everything", auto_approve=True)))
    assert gate.check("everything__read_file", {}) == PermissionDecision.ALLOW


def test_trust_is_per_server_not_per_tool_name():
    gate = PermissionGate.from_config(_app(_server("everything", auto_approve=True)))
    assert gate.check("other__read_file", {}) == PermissionDecision.WARN
    assert gate.check("everything__anything_at_all", {}) == PermissionDecision.ALLOW


def test_a_local_tool_name_that_merely_looks_remote_is_not_trusted():
    gate = PermissionGate.from_config(_app(_server("bash", auto_approve=True)))
    assert gate.check("bash", {"command": "ls"}) == PermissionDecision.WARN


# ------------------------------------------------------ the old rules hold


def test_the_hard_deny_list_still_denies():
    gate = PermissionGate.from_config(_app(_server("everything", auto_approve=True)))
    assert gate.check("bash", {"command": "rm -rf /"}) == PermissionDecision.DENY


def test_an_unknown_local_tool_still_warns():
    gate = PermissionGate.from_config(_app(_server("everything", auto_approve=True)))
    assert gate.check("who_knows", {}) == PermissionDecision.WARN


def test_the_local_allow_list_is_unchanged():
    gate = PermissionGate.from_config(_app(_server("everything", auto_approve=True)))
    for name in ("read", "glob", "todo_write", "skill", "subagent", "memory_search", "ask_user"):
        assert gate.check(name, {}) == PermissionDecision.ALLOW


# ------------------------------------------------------ per-tool rules (M2)

# One dispatcher means one schema, but the gate still has to judge the real
# call: catalog visibility and execution authorization stay separate
# (OpenCode, packages/core/src/tool/AGENTS.md:54).  These rules are declared
# per server, beside the tools whitelist they resemble.


def _rules(**kwargs) -> MCPServerConfig:
    return MCPServerConfig(name="fs", command=["mcp-fs"], permissions=MCPToolRules(**kwargs))


def test_a_server_can_deny_one_of_its_tools_by_name():
    gate = PermissionGate.from_config(_app(_rules(deny=["write_file"])))

    assert gate.check("fs__write_file", {}) == PermissionDecision.DENY
    assert gate.check("fs__read_file", {}) == PermissionDecision.WARN


def test_a_config_deny_beats_an_always_the_operator_gave_earlier():
    """A standing grant may never reopen what the config refuses outright —
    the same ordering reason the bash deny list is checked first."""
    gate = PermissionGate.from_config(_app(_rules(deny=["write_file"])))
    gate.grant("fs__write_file", {})

    assert gate.check("fs__write_file", {}) == PermissionDecision.DENY


def test_an_allow_rule_skips_the_prompt():
    gate = PermissionGate.from_config(_app(_rules(allow=["read_file"])))

    assert gate.check("fs__read_file", {}) == PermissionDecision.ALLOW


def test_an_always_on_a_remote_tool_is_remembered_by_name():
    """grant_key used to return None for every remote tool, so "Always" could
    never persist one (docs/r7-analysis.md).  The tool name *is* the resource."""
    gate = PermissionGate.from_config(_app(_server("fs")))

    assert gate.grant("fs__read_file", {}) == "fs__read_file"
    assert gate.grants == ("fs__read_file",)
    assert gate.check("fs__read_file", {}) == PermissionDecision.ALLOW
    assert gate.check("fs__write_file", {}) == PermissionDecision.WARN
