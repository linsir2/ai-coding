"""N-backlog §E: the process layer's writable roots must be live.

Measured: `Sandbox.resolve()` asks the grantor before refusing, so the **path**
layer's answer is live -- but `BashTool.__init__` snapshotted `roots` at
construction, so a directory the file tools may write is one a bash command may
not (EROFS).  `builder.py:233-235` claims the two never diverge; at runtime a
grant breaks that claim.

This module pins the invariant: after a write grant, bash's roots are the same
set the path layer would allow, and a **read** grant does not become a write.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_coding.security.permission_gate import PermissionGate
from ai_coding.security.sandbox import Sandbox


def _gate(workspace: Path) -> PermissionGate:
    return PermissionGate(workspace=str(workspace))


def test_live_roots_follow_a_write_grant(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    gate = _gate(workspace)
    sandbox = Sandbox(workspace, grantor=gate)
    assert sandbox.live_roots() == (workspace.resolve(),)

    gate.grant("write", {"path": str(outside)})

    roots = sandbox.live_roots()
    assert outside.resolve() in roots, (
        "a directory the file tools may write must be writable by bash too"
    )


def test_a_read_grant_does_not_become_a_write(tmp_path):
    """A read approval must not turn into a writable bind mount."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    gate = _gate(workspace)
    sandbox = Sandbox(workspace, grantor=gate)
    gate.grant("read", {"path": str(outside)})

    assert outside.resolve() not in sandbox.live_roots(), (
        "a read grant became a writable root"
    )


def test_a_file_level_write_grant_is_kept(tmp_path):
    """`write:/outside/file.py` names a file; `_build` binds a file fine."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "file.py"
    target.write_text("x\n", encoding="utf-8")

    gate = _gate(workspace)
    sandbox = Sandbox(workspace, grantor=gate)
    gate.grant("write", {"path": str(target)})

    assert target.resolve() in sandbox.live_roots()


def test_live_roots_are_deduplicated(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    gate = _gate(workspace)
    sandbox = Sandbox(workspace, grantor=gate)
    gate.grant("write", {"path": str(workspace)})

    roots = sandbox.live_roots()
    assert len(roots) == len(set(roots)), "a root must not be bound twice"


def test_a_missing_directory_is_not_bound(tmp_path):
    """bwrap fails on a nonexistent bind source; wait for it to appear."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"          # not created yet

    gate = _gate(workspace)
    sandbox = Sandbox(workspace, grantor=gate)
    gate.grant("write", {"path": str(outside)})

    assert outside.resolve() not in sandbox.live_roots()

    outside.mkdir()
    assert outside.resolve() in sandbox.live_roots()


def test_a_gate_without_a_workspace_authorizes_nothing(tmp_path):
    """`path_allowed` refuses without a workspace; `live_roots` must agree."""
    sandbox = Sandbox(tmp_path / "ws", grantor=PermissionGate())
    assert sandbox.live_roots() == (Path(tmp_path / "ws").resolve(),)


def test_live_roots_do_not_include_the_parent_configuration(tmp_path):
    """A sub-agent sandbox is built with extra_roots=() on purpose.

    Its writable face is the sub-worktree plus its own grants -- never the
    parent's extra_workspace_roots (builder.py:351-353 says they are
    deliberately not passed down).
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    parent_root = tmp_path / "parent-root"
    parent_root.mkdir()

    sandbox = Sandbox(workspace, extra_roots=(parent_root,))
    assert sandbox.live_roots() == (workspace.resolve(), parent_root.resolve())

    child = Sandbox(workspace, extra_roots=())
    assert child.live_roots() == (workspace.resolve(),)
