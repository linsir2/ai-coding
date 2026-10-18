"""TDD (M2-2): PermissionGate — ALLOW/WARN/DENY decisions."""

import pytest

from ai_coding.security.permission_gate import PermissionDecision, PermissionGate


def _gate() -> PermissionGate:
    return PermissionGate()


def test_read_allow():
    gate = _gate()
    d = gate.check("read", {"path": "foo.txt"})
    assert d == PermissionDecision.ALLOW


def test_glob_allow():
    gate = _gate()
    d = gate.check("glob", {"pattern": "*.py"})
    assert d == PermissionDecision.ALLOW


def test_write_warn():
    gate = _gate()
    d = gate.check("write", {"path": "f.txt", "content": "x"})
    assert d == PermissionDecision.WARN


def test_edit_warn():
    gate = _gate()
    d = gate.check("edit", {"path": "f.txt"})
    assert d == PermissionDecision.WARN


def test_bash_normal_warn():
    gate = _gate()
    d = gate.check("bash", {"command": "ls -la"})
    assert d == PermissionDecision.WARN


def test_bash_hard_deny_rm_root():
    gate = _gate()
    d = gate.check("bash", {"command": "rm -rf /"})
    assert d == PermissionDecision.DENY


def test_bash_hard_deny_sudo():
    gate = _gate()
    d = gate.check("bash", {"command": "sudo rm foo"})
    assert d == PermissionDecision.DENY


def test_bash_force_push_deny():
    gate = _gate()
    d = gate.check("bash", {"command": "git push --force origin main"})
    assert d == PermissionDecision.DENY


def test_bash_shutdown_deny():
    gate = _gate()
    d = gate.check("bash", {"command": "shutdown now"})
    assert d == PermissionDecision.DENY


# ---- the red lines are matched as commands, not as substrings


@pytest.mark.parametrize(
    "command",
    [
        "rm  -rf  /",           # extra spaces
        "rm -r -f /",           # flags reordered
        "RM -RF /",             # different case
        "rm -fr /",             # flags reordered again
        "chmod  777 /tmp/x",    # extra space
        "sudo  ls",             # two spaces after sudo
    ],
)
def test_bash_red_lines_survive_a_respelled_command(command: str):
    """Measured: every one of these slipped past the old substring table.  A red
    line that a space defeats is not a red line -- and a model that gets refused
    once learns the spelling that works, so the gap is self-teaching."""
    gate = _gate()
    assert gate.check("bash", {"command": command}) == PermissionDecision.DENY


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "rm -rf ./build",              # recursive, but scoped to the workspace
        "grep -r TODO src/",
        "git push origin main",        # a normal push
        "chmod 644 script.sh",         # not world-writable
    ],
)
def test_bash_ordinary_work_still_only_warns(command: str):
    """Tokenizing must not widen the net: the ordinary commands a coding turn
    runs all day are asked about, not refused."""
    gate = _gate()
    assert gate.check("bash", {"command": command}) == PermissionDecision.WARN


# ---- credentials are not ordinary reading (opencode's ".env read ask" is a deny here)


@pytest.mark.parametrize(
    "command",
    [
        "cat ~/.ssh/id_rsa",
        "cat /home/linsir365/.ssh/id_ed25519",
        "cat ~/.aws/credentials",
        "cat ~/.config/gh/hosts.yml",
        "cat ~/.npmrc",
        "cat .env",
        "cat /etc/shadow",
        "cat server.pem",
        "cp ~/.ssh/id_rsa /tmp/x",
    ],
)
def test_bash_reading_a_credential_is_denied(command: str):
    """The file tools answer this by asking; bash could answer it by not being
    asked, because ``bash`` is never grantable and only the hard-deny list ever
    says no.  Measured before this table: every one of these returned WARN, so
    the boundary the file tools enforced was a speed bump."""
    gate = _gate()
    assert gate.check("bash", {"command": command}) == PermissionDecision.DENY


@pytest.mark.parametrize(
    "command",
    [
        "cat README.md",
        "cat .env.example",          # a template, not a secret
        "cat src/config.py",
        "grep -r password src/ tests/",
    ],
)
def test_bash_reading_ordinary_files_still_warns(command: str):
    """The credential table matches a *path shape*, not the word: `.env.example`
    is documentation and `grep password` is how you audit for one."""
    gate = _gate()
    assert gate.check("bash", {"command": command}) == PermissionDecision.WARN


def test_unknown_tool_warn():
    gate = _gate()
    d = gate.check("mystery_tool", {})
    assert d == PermissionDecision.WARN


def test_todo_write_allow():
    gate = _gate()
    d = gate.check("todo_write", {"action": "add"})
    assert d == PermissionDecision.ALLOW


def test_skill_allow():
    gate = _gate()
    d = gate.check("skill", {"name": "test"})
    assert d == PermissionDecision.ALLOW


def test_ask_user_allow():
    """R6c: asking is not doing.

    WARN here meant the REPL asked "Allow ask_user?" before the model's question —
    two prompts for one question, and any answer that was not "y" reached the model
    as "the user declined" (measured in a live REPL run).
    """
    gate = _gate()
    assert gate.check("ask_user", {"question": "改哪个？"}) == PermissionDecision.ALLOW


def test_present_plan_allow():
    """19 §3.2: presenting a plan is asking, not doing.

    WARN here meant the review card arrived behind an "unknown tool
    'present_plan' requires confirmation" prompt — approval asked for twice,
    the first time about the pen rather than the plan (seen in the tmux pass:
    the model even retried the tool around the prompt).
    """
    gate = _gate()
    assert gate.check("present_plan", {}) == PermissionDecision.ALLOW


def test_mcp_tool_names_are_still_warned():
    """The fail-closed rule for genuinely unknown tools is unchanged."""
    gate = _gate()
    assert gate.check("mcp__github__create_issue", {}) == PermissionDecision.WARN


def test_subagent_allow():
    gate = _gate()
    d = gate.check("subAgent", {"task": "do stuff"})
    assert d == PermissionDecision.ALLOW


def test_decision_has_reason_for_deny():
    gate = _gate()
    d = gate.check("bash", {"command": "sudo ls"})
    assert d == PermissionDecision.DENY
    assert d.reason  # non-empty reason string


def test_decision_reason_for_warn():
    gate = _gate()
    d = gate.check("write", {"path": "f.txt"})
    assert d == PermissionDecision.WARN
    assert d.reason


# ---- r16/E-5: per-skill policy for the model's skill tool -----------------


def test_a_denied_skill_cannot_be_read_by_the_model():
    gate = PermissionGate(skill_permissions={"danger": "deny"})

    decision = gate.check("skill", {"action": "read", "name": "danger"})

    assert decision == PermissionDecision.DENY


def test_an_ask_skill_routes_the_read_through_approval():
    gate = PermissionGate(skill_permissions={"careful": "ask"})

    decision = gate.check("skill", {"action": "read", "name": "careful"})

    assert decision == PermissionDecision.WARN
    assert "careful" in decision.reason


def test_the_policy_is_keyed_by_canonical_id_not_spelling():
    def resolve(name: str) -> str | None:
        return "tidy" if name in {"tidy", "clean"} else None

    gate = PermissionGate(skill_permissions={"clean": "deny"}, skill_resolver=resolve)

    assert gate.check("skill", {"action": "read", "name": "tidy"}) == PermissionDecision.DENY
    assert gate.check("skill", {"action": "read", "name": "clean"}) == PermissionDecision.DENY


def test_listing_and_unlisted_skills_are_untouched():
    gate = PermissionGate(skill_permissions={"danger": "deny"})

    assert gate.check("skill", {"action": "list"}) == PermissionDecision.ALLOW
    assert gate.check("skill", {"action": "read", "name": "plain"}) == PermissionDecision.ALLOW


def test_job_output_is_allowed_without_a_confirmation():
    """Waiting on a job spends the turn, not the user's patience: it reads and
    blocks, and touches nothing outside this process."""
    gate = _gate()

    assert gate.check("job_output", {"job_id": "job_1"}) == PermissionDecision.ALLOW


def test_subagent_list_is_allowed_without_a_confirmation():
    """C-3b: reading the background sub-agent table is a lookup; the table's
    writer is this process, and WARN would put an approval panel in front of
    the model checking its own delegated work."""
    gate = _gate()

    assert gate.check("subagent_list", {}) == PermissionDecision.ALLOW


def test_start_goal_and_update_goal_are_allowed_without_a_confirmation():
    """D-4 (Q2 拍板): the model's own taskification is an orchestration switch
    on this session; the tool-level guards (readonly / already-armed / blocked
    threshold) are the authority rule.  WARN would ask the user to approve the
    model's judgement about its own work."""
    gate = PermissionGate()
    assert gate.check("start_goal", {"objective": "x"}) == PermissionDecision.ALLOW
    assert gate.check("update_goal", {"status": "complete"}) == PermissionDecision.ALLOW
