"""T1: file tools refuse outside-workspace calls before anyone is asked to approve."""

import pytest

from ai_coding.security.file_state_tracker import FileStateTracker
from ai_coding.security.sandbox import Sandbox, SandboxViolation
from ai_coding.tools.edit_tool import EditTool
from ai_coding.tools.read_tool import ReadTool
from ai_coding.tools.write_tool import WriteTool


def _tools(tmp_path):
    sb = Sandbox(tmp_path)
    fst = FileStateTracker()
    return {"read": ReadTool(sb, fst), "write": WriteTool(sb, fst), "edit": EditTool(sb, fst)}


@pytest.mark.parametrize("name", ["read", "write", "edit"])
def test_precheck_refuses_outside_paths(tmp_path, name):
    verdict = _tools(tmp_path)[name].precheck({"path": "/outside/x.py"})
    assert verdict is not None
    assert verdict.success is False
    assert verdict.error == "sandbox_violation"
    assert "bash" in verdict.output


@pytest.mark.parametrize("name", ["read", "write", "edit"])
def test_precheck_allows_inside_paths(tmp_path, name):
    assert _tools(tmp_path)[name].precheck({"path": "app/main.py"}) is None


def test_sandbox_violation_message_names_the_way_out(tmp_path):
    with pytest.raises(SandboxViolation) as exc:
        Sandbox(tmp_path).resolve("/outside/x")
    assert "outside workspace" in str(exc.value)
    assert "bash" in str(exc.value)
