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
