"""T1/B: a file tool outside the workspace asks instead of hitting a wall.

B changed this contract on purpose.  It used to be "refuse before anyone is
asked", because the only way out was ``bash`` -- which is never grantable, so the
refusal bought a speed bump and the operator said yes anyway (hotel audit:
边界形同虚设, 只制造摩擦力).  Now an outside path is an approval question
(opencode's external-directory ask), so ``precheck`` still refuses *now* -- the
call has not run -- but the reason it reports is the one the model should act
on, and the dispatcher asks the operator before the tool ever executes.
"""

import pytest

from ai_coding.security.file_state_tracker import FileStateTracker
from ai_coding.security.permission_gate import PermissionGate
from ai_coding.security.sandbox import Sandbox
from ai_coding.tools.edit_tool import EditTool
from ai_coding.tools.read_tool import ReadTool
from ai_coding.tools.write_tool import WriteTool


def _tools(tmp_path, gate=None):
    sb = Sandbox(tmp_path, grantor=gate)
    fst = FileStateTracker()
    return {"read": ReadTool(sb, fst), "write": WriteTool(sb, fst), "edit": EditTool(sb, fst)}


@pytest.mark.parametrize("name", ["read", "write", "edit"])
def test_precheck_refuses_outside_paths(tmp_path, name):
    verdict = _tools(tmp_path)[name].precheck({"path": "/outside/x.py"})
    assert verdict is not None
    assert verdict.success is False
    assert verdict.error == "sandbox_violation"
    assert "approval" in verdict.output


@pytest.mark.parametrize("name", ["read", "write", "edit"])
def test_precheck_allows_inside_paths(tmp_path, name):
    assert _tools(tmp_path)[name].precheck({"path": "app/main.py"}) is None


def test_precheck_defers_to_a_granted_directory(tmp_path):
    gate = PermissionGate(workspace=str(tmp_path))
    gate.grant("read", {"path": "/outside/x.py"})
    assert _tools(tmp_path, gate=gate)["read"].precheck({"path": "/outside/y.py"}) is None


def test_sandbox_violation_message_names_the_way_out(tmp_path):
    from ai_coding.security.sandbox import SandboxViolation

    with pytest.raises(SandboxViolation) as exc:
        Sandbox(tmp_path).resolve("/outside/x")
    assert "outside workspace" in str(exc.value)
    assert "approval" in str(exc.value)
