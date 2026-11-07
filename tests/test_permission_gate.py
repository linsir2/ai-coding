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


def test_tool_search_is_a_lookup_not_an_unknown_tool():
    """R2-4: the deferred-tool lookup asked the operator for approval.

    ``tool_search`` is local and read-only (``mutates_world = False``); leaving
    it out of the allow list sent every call through the "unknown tool" branch
    and put an approval panel in front of a table lookup (measured on three
    calls in one run).
    """
    gate = _gate()
    assert gate.check("tool_search", {"query": "job"}) == PermissionDecision.ALLOW


def test_every_local_read_only_tool_is_allowed_out_of_the_box(tmp_path):
    """A new read-only tool must not start life behind an approval panel.

    The defect R2-4 found was structural: a tool was added to the registry and
    nobody remembered this list.  Enumerating the real registry turns that into
    a test failure instead of a prompt in front of a live operator.
    """
    from ai_coding.cli import _build_tool_registry
    from ai_coding.config.loader import ConfigLoader
    from ai_coding.skills.registry import SkillRegistry

    app_cfg = ConfigLoader.load_app(None)
    registry = _build_tool_registry(app_cfg, str(tmp_path), SkillRegistry([]))
    gate = _gate()
    guarded = {
        tool.name
        for tool in registry.all_tools
        if not getattr(tool, "mutates_world", True) and tool.origin == "local"
    }
    assert guarded, "the registry should own read-only tools"
    warned = sorted(
        name for name in guarded if gate.check(name, {}) != PermissionDecision.ALLOW
    )
    assert warned == [], (
        "read-only local tools must not ask for approval by default: "
        f"{warned}"
    )


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
    threshold) are the authority rule.  WARN would ask the user to approve
    the model's judgement about its own work."""
    gate = PermissionGate()
    assert gate.check("start_goal", {"objective": "x"}) == PermissionDecision.ALLOW
    assert gate.check("update_goal", {"status": "complete"}) == PermissionDecision.ALLOW


# ---- W2/T: the red lines read the command's real words


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /",
        "rm -r -f /",
        "rm --recursive --force /",
        "chmod 777 /tmp/x",
        "chmod a+rwx /tmp/x",
        "chmod 0777 /tmp/x",
        "sudo rm x",
        "cat ~/.ssh/id_rsa",
        "cat .env",
    ],
)
def test_the_red_lines_survive_the_shell_grammar(command: str):
    """W2/T: the words come from the shell grammar now, and the red lines
    have to mean the same thing under it.

    Each of these is a red line today.  Reading the command's words out of
    a syntax tree instead of a regular expression must not change that --
    the refusal and the approval panel share this verdict, so a difference
    here is a difference the operator sees.
    """
    gate = _gate()
    assert gate.check("bash", {"command": command}) == PermissionDecision.DENY


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "rm -rf ./build",
        "grep -r TODO src/",
        "chmod 644 script.sh",
        "cat README.md",
        "cat .env.example",
    ],
)
def test_ordinary_work_stays_ordinary_under_the_shell_grammar(command: str):
    """The other side: reading the words properly must not widen the net."""
    gate = _gate()
    assert gate.check("bash", {"command": command}) == PermissionDecision.WARN


@pytest.mark.parametrize(
    "command",
    [
        "echo 'rm -rf /'",
        'echo "rm -rf /"',
        "# rm -rf /",
    ],
)
def test_a_quoted_or_commented_red_line_is_not_a_command(command: str):
    """W2/T: what the grammar can tell apart, the red lines now do too.

    A quoted ``rm -rf /`` is a string and a commented one is a comment --
    neither runs ``rm``.  The old table matched the *text*, so it refused
    both, and a refusal of something that will never run teaches the
    model that the red lines are noise.

    Pinned here so the behaviour is a decision rather than an accident.
    """
    gate = _gate()
    assert gate.check("bash", {"command": command}) == PermissionDecision.WARN


def test_a_variable_prefix_does_not_hide_a_red_line():
    """W2/T: ``FOO=bar rm -rf /`` is still ``rm -rf /``.

    The grammar knows the assignment is a prefix and the command is the
    rest; a text scan saw one word and lost the shape.
    """
    gate = _gate()
    assert gate.check("bash", {"command": "FOO=bar rm -rf /"}) == PermissionDecision.DENY


# ---- M-D3: TTL and per-grant revocation ------------------------------------


def test_an_expired_grant_stops_answering():
    now = {"t": 1000.0}
    gate = PermissionGate(clock=lambda: now["t"], grant_ttl=60.0)
    gate.grant("edit", {"path": "src/a.py"}, posture="build")

    assert gate.check("edit", {"path": "src/a.py"}) == PermissionDecision.ALLOW

    now["t"] += 61.0

    assert gate.check("edit", {"path": "src/a.py"}) == PermissionDecision.WARN
    assert gate.grants == ()


def test_an_expired_grant_does_not_open_the_sandbox(tmp_path):
    """P2-1: the file tools' path check honours the TTL, not only the gate's.

    ``Sandbox`` asks ``path_allowed`` before refusing, so a grant that had
    expired on the gate side but still opened the path side would keep an
    "always" read working after its lifetime -- the two halves of the same
    boundary disagreeing about what is authorized.
    """
    now = {"t": 1000.0}
    gate = PermissionGate(
        workspace=str(tmp_path), clock=lambda: now["t"], grant_ttl=60.0
    )
    outside = tmp_path.parent / "perm-grant-outside"
    gate.grant("read", {"path": str(outside / "a.py")}, posture="build")

    assert gate.path_allowed(outside / "a.py") is True
    assert gate.path_allowed(outside / "b.py") is True

    now["t"] += 61.0

    assert gate.grants == ()
    assert gate.path_allowed(outside / "a.py") is False
    assert gate.path_allowed(outside / "b.py") is False


def test_a_permanent_grant_never_expires():
    now = {"t": 1000.0}
    gate = PermissionGate(clock=lambda: now["t"], grant_ttl=None)
    gate.grant("edit", {"path": "src/a.py"}, posture="build")

    now["t"] += 10_000_000.0

    assert gate.check("edit", {"path": "src/a.py"}) == PermissionDecision.ALLOW


def test_reapproving_refreshes_the_grants_lifetime():
    """P3-2: "always" again restarts the clock rather than keeping the old one."""
    now = {"t": 1000.0}
    gate = PermissionGate(clock=lambda: now["t"], grant_ttl=60.0)
    gate.grant("edit", {"path": "src/a.py"}, posture="build")

    now["t"] += 50.0
    gate.grant("edit", {"path": "src/a.py"}, posture="build")

    now["t"] += 50.0  # 100s since the first grant, 50s since the second

    assert gate.grants == ("edit:src/a.py@build",)
    assert gate.check("edit", {"path": "src/a.py"}) == PermissionDecision.ALLOW


def test_a_single_grant_can_be_revoked():
    """U14: a full key removes exactly that variant -- not every looser one."""
    gate = PermissionGate()
    gate.grant("edit", {"path": "src/a.py"}, posture="build")
    gate.grant("edit", {"path": "src/a.py"}, posture="readonly")

    assert gate.revoke("edit:src/a.py@build") is True

    assert gate.grants == ("edit:src/a.py@readonly",)
    assert gate.revoke("edit:src/a.py@build") is False
    assert gate.revoke("edit:src/a.py@readonly") is True
    assert gate.grants == ()
    assert gate.check("edit", {"path": "src/a.py"}, posture="build") == (
        PermissionDecision.WARN
    )


def test_revoking_by_base_name_drops_every_posture_variant():
    gate = PermissionGate()
    gate.grant("edit", {"path": "src/a.py"}, posture="build")
    gate.grant("edit", {"path": "src/a.py"}, posture="readonly")
    gate.grant("edit", {"path": "src/b.py"}, posture="build")

    assert gate.revoke("edit:src/a.py") is True

    assert gate.grants == ("edit:src/b.py@build",)


def test_revoking_an_unknown_key_is_not_an_error():
    gate = PermissionGate()

    assert gate.revoke("edit:src/a.py") is False


def test_revoking_reaches_the_store(tmp_path):
    from ai_coding.security.grants import GrantStore

    store = GrantStore(tmp_path, tmp_path / "grants.json")
    gate = PermissionGate(store=store)
    gate.grant("edit", {"path": "src/a.py"}, posture="build")

    assert gate.revoke("edit:src/a.py") is True

    assert store.load() == set()


def test_grant_records_carry_the_expiry():
    """U15: the panel's row shape -- the key and its deadline in one place."""
    now = {"t": 1000.0}
    gate = PermissionGate(clock=lambda: now["t"], grant_ttl=60.0)
    gate.grant("edit", {"path": "src/a.py"}, posture="build")
    gate.grant("write", {"path": "src/b.py"})

    assert gate.grant_records() == [
        ("edit:src/a.py@build", 1060.0),
        ("write:src/b.py", 1060.0),
    ]


def test_a_permanent_grant_has_no_deadline():
    gate = PermissionGate(grant_ttl=None)
    gate.grant("edit", {"path": "src/a.py"})

    assert gate.grant_records() == [("edit:src/a.py", None)]


# ---- P2-C3: merge_agent's verify command is judged as bash ----------------


def test_merge_agent_is_unknown_and_asks():
    gate = _gate()
    assert gate.check("merge_agent", {"target": "run-1"}) == PermissionDecision.WARN


def test_merge_agent_verify_reuses_bash_red_line():
    gate = _gate()
    d = gate.check(
        "merge_agent", {"target": "run-1", "verify_cmd": "rm -rf /"}
    )
    assert d == PermissionDecision.DENY


def test_merge_agent_verify_reuses_bash_warn():
    gate = _gate()
    d = gate.check(
        "merge_agent", {"target": "run-1", "verify_cmd": "pytest -q"}
    )
    assert d == PermissionDecision.WARN


def test_bash_inherit_network_warns_when_default_is_deny():
    gate = PermissionGate(network_mode="deny")
    d = gate.check(
        "bash",
        {
            "command": "npm install",
            "network_permissions": "inherit",
            "justification": "需要访问 npm registry",
        },
    )
    assert d == PermissionDecision.WARN
    assert "host network" in d.reason
    assert "npm registry" in d.reason


def test_bash_inherit_network_does_not_warn_when_default_is_inherit():
    gate = PermissionGate(network_mode="inherit")
    d = gate.check(
        "bash",
        {
            "command": "npm install",
            "network_permissions": "inherit",
            "justification": "需要访问 npm registry",
        },
    )
    assert d == PermissionDecision.WARN  # bash itself is still WARN
    assert "host network" not in d.reason


def test_shell_read_with_a_network_key_is_denied_outright():
    """X-01a: DENY, not WARN -- asking the operator first would waste a prompt
    on a call the tool refuses anyway."""
    gate = PermissionGate(network_mode="deny")

    d = gate.check(
        "shell_read",
        {
            "command": "ls",
            "network_permissions": "inherit",
            "justification": "local dev server",
        },
    )

    assert d == PermissionDecision.DENY
    assert "bash" in d.reason, "the reason must name the approval path that works"


def test_shell_read_with_a_null_network_key_is_still_denied():
    """Presence, not truthiness: ``null`` is still a request for the field."""
    gate = PermissionGate(network_mode="deny")

    d = gate.check(
        "shell_read",
        {"command": "ls", "network_permissions": None},
    )

    assert d == PermissionDecision.DENY


def test_plan_mode_ls_is_still_allowed():
    """The gate's new branch fires on the field, never on the tool name alone."""
    gate = PermissionGate()

    assert gate.check("shell_read", {"command": "ls"}) == PermissionDecision.ALLOW


def test_a_one_off_yes_opens_the_path_until_it_is_consumed(tmp_path):
    gate = PermissionGate(workspace=str(tmp_path))
    outside = tmp_path.parent / "one-off-outside"
    params = {"path": str(outside / "a.py")}

    gate.allow_once("read", params)

    assert gate.path_allowed(outside / "a.py") is True
    assert gate.grants == (), "a one-off answer is not a standing permission"

    gate.consume_once("read", params)

    assert gate.path_allowed(outside / "a.py") is False
    assert gate._once_grants == set()


def test_consuming_one_off_does_not_touch_an_always_grant(tmp_path):
    gate = PermissionGate(workspace=str(tmp_path))
    outside = tmp_path.parent / "always-outside"
    params = {"path": str(outside / "a.py")}

    gate.grant("read", params, posture="build")
    gate.allow_once("read", params)
    gate.consume_once("read", params)

    assert gate.path_allowed(outside / "a.py") is True
    assert gate.grants != ()


def test_bash_inherit_network_does_not_warn_when_default_is_off():
    gate = PermissionGate(network_mode="off")
    d = gate.check(
        "bash",
        {
            "command": "npm install",
            "network_permissions": "inherit",
            "justification": "host network already available",
        },
    )
    assert "host network" not in d.reason


def test_a_denied_bash_still_wins_over_a_network_request():
    gate = PermissionGate(network_mode="deny", denied_tools={"bash"})
    d = gate.check(
        "bash",
        {
            "command": "npm install",
            "network_permissions": "inherit",
            "justification": "需要访问 npm registry",
        },
    )
    assert d == PermissionDecision.DENY
    assert "denied by configuration" in d.reason


def test_network_request_is_not_remembered_by_always():
    gate = PermissionGate(network_mode="deny")
    params = {
        "command": "npm install",
        "network_permissions": "inherit",
        "justification": "需要访问 npm registry",
    }
    assert gate.grant_key("bash", params) is None


def test_from_config_reads_the_workspace_network_default():
    from ai_coding.config.models import (
        AppConfig,
        NetworkConfig,
        ProcessSandboxConfig,
    )

    inherit = PermissionGate.from_config(
        AppConfig(
            process_sandbox=ProcessSandboxConfig(network=NetworkConfig(mode="inherit"))
        )
    )
    deny = PermissionGate.from_config(
        AppConfig(
            process_sandbox=ProcessSandboxConfig(network=NetworkConfig(mode="deny"))
        )
    )

    params = {
        "command": "npm install",
        "network_permissions": "inherit",
        "justification": "需要访问 npm registry",
    }
    assert "host network" not in inherit.check("bash", params).reason
    assert "host network" in deny.check("bash", params).reason


def test_auto_approve_tools_allows_configured_local_tools():
    """P1-5: the operator can stop the per-call prompt for write/edit/bash.

    Empty by default; only the named tools change tier, and an unnamed sibling
    keeps asking.
    """
    from ai_coding.config.models import AppConfig, SecurityConfig

    gate = PermissionGate.from_config(
        AppConfig(security=SecurityConfig(auto_approve_tools=["write"]))
    )
    assert gate.check("write", {"path": "a.txt", "content": "x"}) == (
        PermissionDecision.ALLOW
    )
    assert gate.check("edit", {"path": "a.txt"}) == PermissionDecision.WARN


def test_auto_approve_tools_cannot_open_a_red_line():
    from ai_coding.config.models import AppConfig, SecurityConfig

    gate = PermissionGate.from_config(
        AppConfig(security=SecurityConfig(auto_approve_tools=["bash"]))
    )
    assert gate.check("bash", {"command": "rm -rf /"}) == PermissionDecision.DENY
