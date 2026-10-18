"""B: the roots the operator configured, resolved from config into the sandbox.

A root is a standing answer ("always"), so it must not re-ask; the approval
prompt stays for the paths nobody declared.  Patterns are supported because the
useful shape is a sibling repo tree (``/home/me/projects/*``), not one directory.
"""

import pytest

from ai_coding.security.permission_gate import PermissionGate
from ai_coding.security.sandbox import Sandbox, SandboxViolation
from ai_coding.tools.builder import expand_root_patterns


def test_a_literal_root_is_itself(tmp_path):
    (tmp_path / "other").mkdir()
    assert expand_root_patterns([str(tmp_path / "other")]) == (tmp_path / "other",)


def test_a_star_expands_to_its_directories(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "deeper").mkdir()
    (tmp_path / "c.txt").write_text("")
    roots = expand_root_patterns([str(tmp_path / "*")])
    assert roots == (tmp_path / "a", tmp_path / "b")


def test_a_star_in_the_middle_expands_every_match(tmp_path):
    (tmp_path / "projects" / "hotel").mkdir(parents=True)
    (tmp_path / "projects" / "shop").mkdir(parents=True)
    (tmp_path / "other" / "hotel").mkdir(parents=True)
    roots = expand_root_patterns([str(tmp_path / "*" / "hotel")])
    assert roots == (tmp_path / "other" / "hotel", tmp_path / "projects" / "hotel")


def test_a_pattern_matching_a_file_does_not_become_a_root(tmp_path):
    """A file is not somewhere to run a search from, so only directories count."""
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b").mkdir()
    assert expand_root_patterns([str(tmp_path / "*")]) == (tmp_path / "b",)


def test_a_pattern_matching_nothing_is_not_an_error(tmp_path):
    """A typo must not silently open the workspace, and must not crash the
    session either; the config loader reports it and the sandbox carries on."""
    assert expand_root_patterns([str(tmp_path / "nope" / "*")]) == ()


def test_an_empty_pattern_is_ignored(tmp_path):
    assert expand_root_patterns(["", "   "]) == ()


def test_a_configured_root_needs_no_question(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "x.py").write_text("hi")
    sb = Sandbox(tmp_path / "ws", extra_roots=(other,))
    assert sb.resolve(str(other / "x.py")) == other / "x.py"


def test_the_gate_and_the_sandbox_share_one_decision(tmp_path):
    """The gate asks and the sandbox enforces; they must agree, or an approval
    the operator gave is a promise the tool does not keep (or the reverse)."""
    other = tmp_path / "other"
    other.mkdir()
    (other / "x.py").write_text("hi")
    gate = PermissionGate(workspace=str(tmp_path / "ws"))
    sb = Sandbox(tmp_path / "ws", grantor=gate)
    target = str(other / "x.py")
    assert gate.check("read", {"path": target}).decision.value == "warn"
    with pytest.raises(SandboxViolation):
        sb.resolve(target)
    gate.grant("read", {"path": target})
    assert gate.check("read", {"path": target}).decision.value == "allow"
    assert sb.resolve(target) == other / "x.py"
