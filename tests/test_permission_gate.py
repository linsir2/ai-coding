"""TDD (M2-2): PermissionGate — ALLOW/WARN/DENY decisions."""

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
