"""A standing "always" remembers the posture it was given in (R18).

Gemini's ladder: an authorization made in one posture applies there and in
every looser one.  A build grant must not leak back into plan, while a plan
grant is good everywhere.  Old grants, written before postures existed, keep
meaning "everywhere" -- a file on disk is not rewritten just because the
reader learned a new word.
"""

from __future__ import annotations

from pathlib import Path

from ai_coding.security.grants import GrantStore
from ai_coding.security.permission_gate import PermissionDecision, PermissionGate


def _granted(gate: PermissionGate, posture: str) -> PermissionDecision:
    return gate.check("edit", {"path": "src/a.py"}, posture=posture)


def test_a_build_grant_is_not_spent_in_stricter_postures():
    gate = PermissionGate()
    gate.grant("edit", {"path": "src/a.py"}, posture="build")

    assert _granted(gate, "build") == PermissionDecision.ALLOW
    assert _granted(gate, "readonly") == PermissionDecision.WARN
    assert _granted(gate, "plan") == PermissionDecision.WARN


def test_a_plan_grant_is_good_everywhere():
    gate = PermissionGate()
    gate.grant("edit", {"path": "src/a.py"}, posture="plan")

    assert _granted(gate, "plan") == PermissionDecision.ALLOW
    assert _granted(gate, "readonly") == PermissionDecision.ALLOW
    assert _granted(gate, "build") == PermissionDecision.ALLOW


def test_a_readonly_grant_reaches_build_but_not_plan():
    gate = PermissionGate()
    gate.grant("edit", {"path": "src/a.py"}, posture="readonly")

    assert _granted(gate, "build") == PermissionDecision.ALLOW
    assert _granted(gate, "readonly") == PermissionDecision.ALLOW
    assert _granted(gate, "plan") == PermissionDecision.WARN


def test_a_grant_without_a_posture_still_means_everywhere():
    gate = PermissionGate(grants={"edit:src/a.py"})

    assert _granted(gate, "plan") == PermissionDecision.ALLOW
    assert _granted(gate, "readonly") == PermissionDecision.ALLOW
    assert _granted(gate, "build") == PermissionDecision.ALLOW


def test_a_grant_without_a_posture_is_written_bare():
    gate = PermissionGate()
    gate.grant("edit", {"path": "src/a.py"})

    assert gate.grants == ("edit:src/a.py",)


def test_the_posture_is_written_down_and_read_back(tmp_path: Path):
    store = GrantStore(tmp_path, tmp_path / "grants.json")
    gate = PermissionGate(store=store)
    assert gate.grant("edit", {"path": "src/a.py"}, posture="build") == (
        "edit:src/a.py"
    )
    assert gate.grants == ("edit:src/a.py@build",)

    reloaded = PermissionGate(store=store, grants=store.load())
    assert _granted(reloaded, "build") == PermissionDecision.ALLOW
    assert _granted(reloaded, "readonly") == PermissionDecision.WARN


def test_path_allowed_understands_a_posture_scoped_grant(tmp_path: Path):
    outside = tmp_path / "outside"
    gate = PermissionGate(workspace=tmp_path)
    gate.grant("read", {"path": str(outside / "deep" / "a.py")}, posture="build")

    assert gate.path_allowed(outside / "deep" / "a.py")
    assert gate.path_allowed(outside / "deep" / "b.py")


def test_an_unknown_posture_suffix_does_not_open_a_grant():
    gate = PermissionGate(grants={"edit:src/a.py@sometimes"})

    assert _granted(gate, "build") == PermissionDecision.WARN
    assert _granted(gate, "plan") == PermissionDecision.WARN


def test_a_path_that_ends_in_a_posture_name_keeps_its_own_scope():
    gate = PermissionGate()
    gate.grant("edit", {"path": "src/a@build"}, posture="build")

    assert gate.check("edit", {"path": "src/a@build"}, posture="build") == (
        PermissionDecision.ALLOW
    )
    assert gate.check("edit", {"path": "src/a@build"}, posture="plan") == (
        PermissionDecision.WARN
    )


def test_an_old_path_that_ends_in_a_posture_name_is_read_as_scoped():
    """The old format has no way to say "this @build is part of the path".
    Reading it as a scope can only lose a grant, never widen one."""
    gate = PermissionGate(grants={"edit:src/a@build"})

    assert _granted(gate, "build") == PermissionDecision.WARN
    assert gate.check("edit", {"path": "src/a"}, posture="build") == (
        PermissionDecision.ALLOW
    )
