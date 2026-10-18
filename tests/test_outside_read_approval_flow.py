"""B: an outside-workspace call reaches the operator instead of dying at precheck.

``core/dispatch.py`` ran ``precheck`` *before* the WARN branch, so a call the
sandbox would refuse never reached the approval prompt -- there was no way to
say yes to a read outside the workspace.  This pins the new order: ask first,
refuse only if the operator did not approve.
"""

import pytest

from ai_coding.core.dispatch import ToolDispatcher
from ai_coding.domain.message import ToolCallRef
from ai_coding.domain.task import TaskState
from ai_coding.security.approval import ApprovalResult
from ai_coding.security.file_state_tracker import FileStateTracker
from ai_coding.security.permission_gate import PermissionGate
from ai_coding.security.sandbox import Sandbox
from ai_coding.tools.base import ToolContext
from ai_coding.tools.read_tool import ReadTool
from ai_coding.tools.registry import ToolRegistry


class _Answer:
    def __init__(self, answer: ApprovalResult) -> None:
        self.answer = answer
        self.calls: list[tuple[str, dict, str]] = []

    async def approve(self, tool_name, params, reason):
        self.calls.append((tool_name, params, reason))
        return self.answer


def _ref(arguments: str) -> ToolCallRef:
    return ToolCallRef(id="c1", name="read", arguments=arguments)


def _ctx(workspace: str) -> ToolContext:
    return ToolContext(task=TaskState(), workspace=workspace, session_id="sid")


def _stack(tmp_path, answer, *, workspace=None):
    workspace = str(workspace or tmp_path)
    gate = PermissionGate(workspace=workspace)
    sandbox = Sandbox(workspace, grantor=gate)
    registry = ToolRegistry()
    registry.register(ReadTool(sandbox, FileStateTracker()))
    return ToolDispatcher(registry, gate, _Answer(answer)), workspace


@pytest.mark.asyncio
async def test_an_approved_outside_read_runs(tmp_path):
    """The whole point: say yes, and the file is read."""
    outside = tmp_path.parent / "outside_file.py"
    outside.write_text("secret sauce")
    dispatcher, workspace = _stack(tmp_path, ApprovalResult.YES)
    outcomes = await dispatcher.dispatch_batch(
        [_ref(f'{{"path": "{outside}"}}')], _ctx(workspace)
    )
    assert outcomes[0].result.success is True
    assert "secret sauce" in outcomes[0].result.output


@pytest.mark.asyncio
async def test_a_declined_outside_read_is_refused(tmp_path):
    outside = tmp_path.parent / "outside_file.py"
    outside.write_text("nope")
    dispatcher, workspace = _stack(tmp_path, ApprovalResult.NO)
    outcomes = await dispatcher.dispatch_batch(
        [_ref(f'{{"path": "{outside}"}}')], _ctx(workspace)
    )
    assert outcomes[0].result.success is False
    assert outcomes[0].result.error in ("sandbox_violation", "not_approved")


@pytest.mark.asyncio
async def test_the_operator_is_actually_asked(tmp_path):
    """The prompt must arrive.  It never did: precheck returned first."""
    outside = tmp_path.parent / "outside_file.py"
    outside.write_text("x")
    dispatcher, workspace = _stack(tmp_path, ApprovalResult.YES)
    approval = dispatcher.approval
    await dispatcher.dispatch_batch([_ref(f'{{"path": "{outside}"}}')], _ctx(workspace))
    assert approval.calls, "the outside read was never put to the operator"
    assert "outside workspace" in approval.calls[0][2]


@pytest.mark.asyncio
async def test_always_remembers_the_directory(tmp_path):
    """One answer, then the whole directory: the second file is not asked about
    again.  This is what "always" means for a read (opencode grants the
    directory, ``06-opencode长任务机制.md:20``)."""
    base = tmp_path.parent / "granted_dir"
    base.mkdir(exist_ok=True)
    (base / "a.py").write_text("a")
    (base / "b.py").write_text("b")
    dispatcher, workspace = _stack(tmp_path, ApprovalResult.ALWAYS)
    first = await dispatcher.dispatch_batch(
        [_ref(f'{{"path": "{base / "a.py"}"}}')], _ctx(workspace)
    )
    assert first[0].result.success is True
    asked_after_first = len(dispatcher.approval.calls)
    second = await dispatcher.dispatch_batch(
        [_ref(f'{{"path": "{base / "b.py"}"}}')], _ctx(workspace)
    )
    assert second[0].result.success is True
    assert len(dispatcher.approval.calls) == asked_after_first


@pytest.mark.asyncio
async def test_an_inside_read_is_not_asked_about(tmp_path):
    """B changes outside reads only; ordinary work must not gain a prompt."""
    (tmp_path / "inside.py").write_text("fine")
    dispatcher, workspace = _stack(tmp_path, ApprovalResult.NO)
    outcomes = await dispatcher.dispatch_batch(
        [_ref('{"path": "inside.py"}')], _ctx(workspace)
    )
    assert outcomes[0].result.success is True
    assert dispatcher.approval.calls == []
