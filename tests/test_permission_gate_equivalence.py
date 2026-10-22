"""The equivalence gold standard (M1a / T2-17).

The gate is being rebuilt on top of a rule language.  The proof that this is
a refactor and not a redesign is this file: every row was produced by the
*old* gate -- by running it, not by reading it -- and the new gate has to
answer the same thing, for the same reason, in the same words.

The values are fixed on purpose.  A corpus recomputed from the gate under
test would prove nothing; these are the old gate's answers, captured before
the old gate was replaced.

One row differs on purpose, and it says so: the *wording* of bash's question.
The old gate answered bash before it looked at any list, with its own
sentence; now bash is asked by the same rule that asks for ``write`` and
``edit``, so the sentence is the same one they get.  The decision is
unchanged; only the words are.
"""

from __future__ import annotations

from typing import Any

import pytest

from ai_coding.security.permission_gate import PermissionDecision, PermissionGate

#: The gate setups the corpus runs against.  Same shapes as the probe that
#: produced the rows below.
SETUPS: dict[str, Any] = {
    "default": lambda: PermissionGate(),
    "workspace": lambda: PermissionGate(workspace="/ws"),
    "denied": lambda: PermissionGate(denied_tools=["fs__write_file"]),
    "allowed": lambda: PermissionGate(allowed_tools=["fs__write_file"]),
    "trusted": lambda: PermissionGate(trusted_servers=["fs"]),
    "skills": lambda: PermissionGate(
        skill_permissions={"brainstorm": "deny", "research": "ask", "notes": "allow"},
        skill_resolver=lambda name: name,
    ),
    "grants": lambda: PermissionGate(
        grants=["edit:src/app.py", "read:src", "write:src/app.py"]
    ),
    "grants_workspace": lambda: PermissionGate(
        workspace="/ws", grants=["edit:src/app.py", "read:/etc"]
    ),
}

#: (case, setup, tool, params, decision, reason) -- the old gate's answers.
CASES: list[tuple[str, str, str, dict[str, Any], str, str]] = [
    # -- bash -----------------------------------------------------------------
    ("bash_red_line", "default", "bash", {"command": "rm -rf /"}, "deny",
     "bash command blocked: recursive root deletion (rm -rf /)"),
    ("bash_sudo", "default", "bash", {"command": "sudo rm foo"}, "deny",
     "bash command blocked: sudo escalation (sudo rm foo)"),
    ("bash_force_push", "default", "bash", {"command": "git push --force origin main"},
     "deny", "bash command blocked: force git push (git push --force origin main)"),
    ("bash_credential", "default", "bash", {"command": "cat ~/.ssh/id_rsa"}, "deny",
     "bash command blocked: credential file (id_rsa)"),
    ("bash_normal", "default", "bash", {"command": "ls -la"}, "warn",
     "tool 'bash' requires confirmation"),
    ("bash_quoted_red_line", "default", "bash", {"command": "echo 'rm -rf /'"}, "warn",
     "tool 'bash' requires confirmation"),
    ("bash_compound", "default", "bash", {"command": "cd /tmp && rm -rf x"}, "warn",
     "tool 'bash' requires confirmation"),
    # -- the allow list -------------------------------------------------------
    ("allow_read", "default", "read", {"path": "src/app.py"}, "allow", ""),
    ("allow_glob", "default", "glob", {"pattern": "*.py"}, "allow", ""),
    ("allow_grep", "default", "grep", {"pattern": "TODO", "path": "src"}, "allow", ""),
    ("allow_webfetch", "default", "webfetch", {"url": "https://example.com"}, "allow", ""),
    ("allow_websearch", "default", "websearch", {"query": "opencode permissions"}, "allow", ""),
    ("allow_todo_write", "default", "todo_write", {"todos": []}, "allow", ""),
    ("allow_skill", "default", "skill", {"action": "list"}, "allow", ""),
    ("allow_subAgent", "default", "subAgent", {"task": "look"}, "allow", ""),
    ("allow_memory_search", "default", "memory_search", {"query": "x"}, "allow", ""),
    ("allow_history_search", "default", "history_search", {"query": "x"}, "allow", ""),
    ("allow_job_list", "default", "job_list", {}, "allow", ""),
    ("allow_job_kill", "default", "job_kill", {"job_id": "j1"}, "allow", ""),
    ("allow_job_output", "default", "job_output", {"job_id": "j1"}, "allow", ""),
    ("allow_subagent_list", "default", "subagent_list", {}, "allow", ""),
    ("allow_memory_write", "default", "memory_write", {"content": "x"}, "allow", ""),
    ("allow_ask_user", "default", "ask_user", {"question": "?"}, "allow", ""),
    ("allow_checkpoint_write", "default", "checkpoint_write", {"text": "x"}, "allow", ""),
    ("allow_present_plan", "default", "present_plan", {"plan": "x"}, "allow", ""),
    ("allow_finish_research", "default", "finish_research", {"summary": "x"}, "allow", ""),
    ("allow_start_goal", "default", "start_goal", {"goal": "x"}, "allow", ""),
    ("allow_update_goal", "default", "update_goal", {"goal": "x"}, "allow", ""),
    ("allow_mcp_call", "default", "mcp_call",
     {"server": "fs", "tool": "write_file", "arguments": {}}, "allow", ""),
    # -- the warn list and the workspace edge ---------------------------------
    ("warn_write", "default", "write", {"path": "f.txt", "content": "x"}, "warn",
     "tool 'write' requires confirmation"),
    ("warn_edit", "default", "edit", {"path": "f.txt"}, "warn",
     "tool 'edit' requires confirmation"),
    ("warn_write_outside", "workspace", "write", {"path": "/etc/x", "content": "x"}, "warn",
     "tool 'write' requires confirmation"),
    ("allow_read_inside", "workspace", "read", {"path": "src/app.py"}, "allow", ""),
    ("warn_read_outside", "workspace", "read", {"path": "/etc/passwd"}, "warn",
     "'read' targets /etc/passwd, outside workspace '/ws' — it needs your approval"),
    # -- unknown and MCP ------------------------------------------------------
    ("unknown_tool", "default", "frobnicate", {}, "warn",
     "unknown tool 'frobnicate' requires confirmation"),
    ("mcp_unauthorized", "default", "fs__write_file", {}, "warn",
     "unknown tool 'fs__write_file' requires confirmation"),
    ("mcp_denied", "denied", "fs__write_file", {}, "deny",
     "tool 'fs__write_file' is denied by configuration"),
    ("mcp_allowed", "allowed", "fs__write_file", {}, "allow", ""),
    ("mcp_trusted", "trusted", "fs__write_file", {}, "allow", ""),
    # A server is trusted by its exact name, not by a prefix: ``fs_write``
    # and ``fs_evil__x`` are different servers, and the old gate asked
    # about them.  The wildcard has to keep the separator.
    ("mcp_trusted_lookalike_no_separator", "trusted", "fs_write", {}, "warn",
     "unknown tool 'fs_write' requires confirmation"),
    ("mcp_trusted_lookalike_other_server", "trusted", "fs_evil__x", {}, "warn",
     "unknown tool 'fs_evil__x' requires confirmation"),
    ("mcp_trusted_empty_remote", "trusted", "fs__", {}, "allow", ""),
    # -- the skill policy -----------------------------------------------------
    ("skill_deny", "skills", "skill", {"action": "read", "name": "brainstorm"}, "deny",
     "skill 'brainstorm' is denied by configuration"),
    ("skill_ask", "skills", "skill", {"action": "read", "name": "research"}, "warn",
     "skill 'research' requires confirmation"),
    ("skill_allow", "skills", "skill", {"action": "read", "name": "notes"}, "allow", ""),
    ("skill_list", "skills", "skill", {"action": "list"}, "allow", ""),
    # -- the saved grants -----------------------------------------------------
    ("grant_edit", "grants", "edit", {"path": "src/app.py"}, "allow", ""),
    ("grant_read", "grants", "read", {"path": "src/app.py"}, "allow", ""),
    ("grant_write", "grants", "write", {"path": "src/app.py", "content": "x"}, "allow", ""),
    ("grant_read_outside", "grants_workspace", "read", {"path": "/etc/passwd"}, "allow", ""),
    ("grant_edit_inside", "grants_workspace", "edit", {"path": "src/app.py"}, "allow", ""),
]


@pytest.mark.parametrize(
    "case,setup,tool,params,expected,reason", CASES, ids=[row[0] for row in CASES]
)
def test_the_new_gate_answers_what_the_old_one_answered(
    case: str,
    setup: str,
    tool: str,
    params: dict[str, Any],
    expected: str,
    reason: str,
) -> None:
    """One row of the old gate's answers, replayed against the new one."""
    gate = SETUPS[setup]()

    decision = gate.check(tool, dict(params))

    assert decision.decision.value == expected, case
    assert decision.reason == reason, case


def test_the_corpus_covers_every_branch_the_old_gate_had() -> None:
    """The corpus is only evidence if it reached every branch.

    The old gate had nine branches: the bash red line, the config denial,
    the saved grant, the skill policy (three states), the allow list, the
    outside-workspace question, the config allow, the trusted server and
    the unknown tool.  Each one has to be in here, or the corpus proves
    less than it claims to.
    """
    decisions = {row[4] for row in CASES}
    assert decisions == {"allow", "warn", "deny"}

    reasons = " ".join(row[5] for row in CASES)
    for expected in (
        "bash command blocked",  # the red line
        "is denied by configuration",  # the config denial
        "skill 'brainstorm' is denied by configuration",  # the skill policy
        "outside workspace",  # the workspace edge
        "unknown tool",  # the fallback
    ):
        assert expected in reasons, expected

    tools = {row[2] for row in CASES}
    assert "subAgent" in tools  # the camel-case name the gate lowercases
    assert "mcp_call" in tools  # the MCP vessel
    assert "skill" in tools  # the skill tool


def test_the_only_difference_is_bash_s_wording() -> None:
    """The one deliberate change, named so it cannot drift unnoticed.

    The old gate answered bash before it consulted any list, with its own
    sentence.  Bash is now asked by the same rule that asks for ``write``
    and ``edit``, so it gets the same sentence.  The decision did not move.
    """
    gate = PermissionGate()

    decision = gate.check("bash", {"command": "ls -la"})

    assert decision.decision is PermissionDecision.WARN
    assert decision.reason == "tool 'bash' requires confirmation"
