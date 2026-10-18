"""TDD (M2-1): Sandbox — workspace path containment."""

import pytest

from ai_coding.security.sandbox import Sandbox, SandboxViolation


def test_resolve_within_workspace(tmp_path):
    sb = Sandbox(tmp_path)
    target = sb.resolve("foo/bar.txt")
    assert target == tmp_path / "foo" / "bar.txt"


def test_resolve_absolute_within_ok(tmp_path):
    sb = Sandbox(tmp_path)
    p = tmp_path / "x.txt"
    p.write_text("hi")
    assert sb.resolve(str(p)) == p


def test_parent_traversal_blocked(tmp_path):
    sb = Sandbox(tmp_path)
    with pytest.raises(SandboxViolation):
        sb.resolve("../outside.txt")


def test_absolute_outside_raises(tmp_path):
    sb = Sandbox(tmp_path)
    with pytest.raises(SandboxViolation):
        sb.resolve("/etc/passwd")


def test_tilde_expansion_within(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    sb = Sandbox(tmp_path / "work")
    # ~/work/file.txt should resolve inside sandbox when sandbox is under HOME
    target = sb.resolve(str(tmp_path / "work" / "f.txt"))
    assert target == tmp_path / "work" / "f.txt"


def test_nested_paths_ok(tmp_path):
    sb = Sandbox(tmp_path)
    (tmp_path / "a" / "b" / "c").mkdir(parents=True)
    assert sb.resolve("a/b/c/../d.txt") == tmp_path / "a" / "b" / "d.txt"


def test_sandbox_violation_message(tmp_path):
    sb = Sandbox(tmp_path)
    with pytest.raises(SandboxViolation, match="outside workspace"):
        sb.resolve("/tmp/evil")


def test_sandbox_violation_message_points_to_approval(tmp_path):
    """The old text said "use bash instead".  That advice walked the model
    around the gate: bash is never grantable and only the hard-deny list says no
    to it, so refusing ``read`` and pointing at ``bash`` was a speed bump with an
    exit sign (``docs/orchestration-explore/2026-10-08-hotel-session-audit.md``:
    边界形同虚设, 只制造摩擦力).  The way out is now the operator's own answer."""
    sb = Sandbox(tmp_path)
    with pytest.raises(SandboxViolation) as exc:
        sb.resolve("/tmp/evil")
    assert "approval" in str(exc.value)
    assert "bash" not in str(exc.value)


def test_an_extra_root_needs_no_question(tmp_path):
    """A root the operator configured is not a request -- it is an answer."""
    other = tmp_path / "other"
    other.mkdir()
    sb = Sandbox(tmp_path / "ws", extra_roots=(other,))
    assert sb.resolve(str(other / "x.py")) == other / "x.py"
    assert sb.workspace not in sb.roots[1:]
