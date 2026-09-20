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
