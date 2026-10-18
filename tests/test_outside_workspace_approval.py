"""B: a file tool outside the workspace asks instead of hitting a wall.

The boundary this closes: ``read`` was refused, the model reached for ``bash
cat``, and the answer was yes -- so the sandbox was a speed bump, not a boundary
(measured in the hotel session, and recorded as such in
``docs/orchestration-explore/2026-10-08-hotel-session-audit.md``: "边界形同虚设,
只制造摩擦力").  opencode asks for the same request instead
(``docs/orchestration-research/06-opencode长任务机制.md:20`` — external directory
is an ``ask``, not a deny), and so does this now.
"""

import pytest

from ai_coding.security.file_state_tracker import FileStateTracker
from ai_coding.security.permission_gate import PermissionDecision, PermissionGate
from ai_coding.security.sandbox import Sandbox, SandboxViolation
from ai_coding.tools.edit_tool import EditTool
from ai_coding.tools.read_tool import ReadTool
from ai_coding.tools.write_tool import WriteTool


def _tools(tmp_path, gate=None):
    sb = Sandbox(tmp_path, grantor=gate)
    fst = FileStateTracker()
    return {
        "read": ReadTool(sb, fst),
        "write": WriteTool(sb, fst),
        "edit": EditTool(sb, fst),
    }


def test_sandbox_still_refuses_what_nothing_authorized(tmp_path):
    """The default is unchanged: with no gate, an outside path is still refused.
    A sub-agent and the bare REPL build stacks without one, and they must not
    silently gain the ability to read the whole filesystem."""
    sb = Sandbox(tmp_path)
    with pytest.raises(SandboxViolation):
        sb.resolve("/etc/passwd")


@pytest.mark.parametrize("name", ["read", "write", "edit"])
def test_precheck_offers_the_gate_instead_of_a_wall(tmp_path, name):
    """``precheck`` answers "may this run", not "will it fail".  It used to say
    the second, which is why the approval panel could never appear for a read
    (``core/dispatch.py`` ran precheck before the WARN branch)."""
    verdict = _tools(tmp_path)[name].precheck({"path": "/outside/x.py"})
    assert verdict is not None
    assert verdict.success is False
    assert verdict.error == "sandbox_violation"
    assert "bash" not in verdict.output, "the old advice walks the model around the gate"


@pytest.mark.parametrize("name", ["read", "write", "edit"])
def test_precheck_asks_when_the_gate_allows_the_directory(tmp_path, name):
    """A granted directory turns the refusal into an approval question, which is
    the whole point: the operator decides, once, and says it with a name."""
    gate = PermissionGate(
        grants=[f"{name}:/outside"], workspace=str(tmp_path)
    )
    assert _tools(tmp_path, gate=gate)[name].precheck({"path": "/outside/x.py"}) is None


@pytest.mark.parametrize("name", ["read", "write", "edit"])
def test_precheck_allows_inside_paths(tmp_path, name):
    assert _tools(tmp_path)[name].precheck({"path": "app/main.py"}) is None


def test_the_gate_asks_for_a_path_outside_the_workspace(tmp_path):
    """``read`` is ALLOW inside the workspace and WARN outside it.  The gate has
    to know the workspace to tell those apart -- the tool list alone cannot."""
    gate = PermissionGate(workspace=str(tmp_path))
    assert gate.check("read", {"path": "inside.py"}) == PermissionDecision.ALLOW
    assert gate.check("read", {"path": "/outside/x.py"}) == PermissionDecision.WARN


def test_answering_always_remembers_the_directory_not_the_file(tmp_path):
    """opencode's shape (``03-opencode编排数据流.md:180``): once/always/reject.
    The "always" is the *directory*, because a grant naming one file would ask
    again for every sibling -- which is the friction this exists to remove."""
    gate = PermissionGate(workspace=str(tmp_path))
    assert gate.check("read", {"path": "/outside/x.py"}) == PermissionDecision.WARN
    key = gate.grant("read", {"path": "/outside/x.py"})
    assert key == "read:/outside"
    assert gate.check("read", {"path": "/outside/y.py"}) == PermissionDecision.ALLOW
    assert gate.check("read", {"path": "/elsewhere/z.py"}) == PermissionDecision.WARN


def test_a_grant_for_one_directory_does_not_open_another(tmp_path):
    gate = PermissionGate(workspace=str(tmp_path))
    gate.grant("read", {"path": "/outside/x.py"})
    assert gate.check("read", {"path": "/elsewhere/z.py"}) == PermissionDecision.WARN


def test_a_grant_does_not_reach_above_its_directory(tmp_path):
    """``/outside`` is granted; ``/outside-elsewhere`` shares a prefix but not a
    directory, and a grant that matched by string would hand over both."""
    gate = PermissionGate(workspace=str(tmp_path))
    gate.grant("read", {"path": "/outside/x.py"})
    assert gate.check("read", {"path": "/outside-elsewhere/z.py"}) == PermissionDecision.WARN


def test_write_inside_the_workspace_is_unchanged(tmp_path):
    """B changes reads only.  A write inside the workspace was already WARN
    (stale-read protection) and stays WARN."""
    gate = PermissionGate(workspace=str(tmp_path))
    assert gate.check("write", {"path": "app/x.py"}) == PermissionDecision.WARN
    assert gate.check("edit", {"path": "app/x.py"}) == PermissionDecision.WARN
