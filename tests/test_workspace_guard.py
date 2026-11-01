"""P0a/P0b: the shared workspace gate is its own module, not a plan-only one.

``outside_workspace_paths``/``outside_workspace_message`` were reached through
the old plan runner only because that is where they were first written.  The
plan/acceptance/first-turn paths all consume them, so they live in
``core.workspace_guard``.  Behaviour itself stays with
``test_plan_workspace_gate.py``.
"""

from ai_coding.core import workspace_guard


def test_guard_module_exports_the_two_public_functions():
    assert set(workspace_guard.__all__) == {
        "outside_workspace_message",
        "outside_workspace_paths",
    }


def test_prose_route_is_not_a_path_but_etc_passwd_is(tmp_path):
    implement = workspace_guard.outside_workspace_paths

    assert implement(["GET /api/v1/users"], tmp_path) == []
    assert implement(["/etc/passwd"], tmp_path) == ["/etc/passwd"]


def test_shell_candidates_are_read_by_the_grammar(tmp_path):
    assert workspace_guard.outside_workspace_paths(
        ["D=/data/x; rm -rf $D"], tmp_path, from_shell=True
    ) == ["/data/x"]


def test_extra_roots_are_reachable(tmp_path):
    other = tmp_path / "sibling"
    other.mkdir()

    assert workspace_guard.outside_workspace_paths(
        [f"看下 {other}/app.py"], tmp_path, extra_roots=(other,)
    ) == []


def test_device_and_shebang_are_exempt(tmp_path):
    assert workspace_guard.outside_workspace_paths(
        ["pytest tests/ -q 2>/dev/null"], tmp_path, from_shell=True
    ) == []
    assert workspace_guard.outside_workspace_paths(
        ["printf '#!/usr/bin/env python3\\nprint(1)\\n' > app.py"], tmp_path
    ) == []
