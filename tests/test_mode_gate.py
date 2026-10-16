"""TDD (18 §10.1/§10.4): a read-only session refuses mutating tools at the call boundary.

The gate lives in ``ToolDispatcher._decide`` right after the unknown-tool check
and *before* the permission gate: in a read-only session a mutating tool is
refused regardless of its permission tier, approval is never consulted, and the
refusal is a normal ToolResult the model can read.  The input is the single
``readonly`` axis — the prompt-side ``mode`` is deliberately invisible here
(18 §10.1: two orthogonal switches; ``/plan`` composes them at the command
layer, so a plan session with readonly off still passes the gate).  The
attribute is fail-closed — a tool class that never declares itself read-only is
refused by default.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_coding.core.dispatch import ToolDispatcher
from ai_coding.domain.message import ToolCallRef, ToolResult
from ai_coding.domain.task import TaskState
from ai_coding.security.approval import ApprovalResult
from ai_coding.security.file_state_tracker import FileStateTracker
from ai_coding.security.permission_gate import PermissionGate
from ai_coding.security.sandbox import Sandbox
from ai_coding.tools.base import BaseTool, ToolContext
from ai_coding.tools.read_tool import ReadTool
from ai_coding.tools.registry import ToolRegistry
from ai_coding.tools.write_tool import WriteTool


class _FakeTool(BaseTool):
    def __init__(self, name: str, *, mutates_world: bool = True) -> None:
        self._name = name
        self.mutates_world = mutates_world
        self.calls: list[dict] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "fake"

    @property
    def params_json_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, params, ctx=None):  # type: ignore[override]
        self.calls.append(params)
        return ToolResult(success=True, output="ok")


class _FreshTool(BaseTool):
    """Never declares the attribute — the fail-closed probe."""

    @property
    def name(self) -> str:
        return "fresh"

    @property
    def description(self) -> str:
        return "fresh"

    @property
    def params_json_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, params, ctx=None):  # type: ignore[override]
        return ToolResult(success=True, output="ok")


class _Approval:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, str]] = []

    async def approve(self, tool_name, params, reason):
        self.calls.append((tool_name, params, reason))
        return ApprovalResult.YES


def _ref(call_id: str, name: str, arguments: str = "{}") -> ToolCallRef:
    return ToolCallRef(id=call_id, name=name, arguments=arguments)


def _ctx(*, readonly: bool = False, workspace: str = "/tmp/ws") -> ToolContext:
    return ToolContext(
        task=TaskState(goal="g"),
        workspace=workspace,
        session_id="sid",
        readonly=readonly,
    )


def _dispatcher(tools, *, approval=None) -> ToolDispatcher:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return ToolDispatcher(registry, PermissionGate(), approval, max_parallel=4)


@pytest.mark.asyncio
async def test_readonly_refuses_a_mutating_tool_before_approval():
    tool = _FakeTool("write")  # WARN tier — approval would be asked in a writable session
    approval = _Approval()
    outcomes = await _dispatcher([tool], approval=approval).dispatch_batch(
        [_ref("c1", "write")], _ctx(readonly=True)
    )
    result = outcomes[0].result
    assert outcomes[0].denied is True
    assert result.success is False
    assert result.error == "readonly_denied"
    assert "只读" in result.output
    # 内化批次: the denial redirects the model to the real tools and the review
    # channel -- it used to teach the /plan family (the old assertions here
    # pinned "/plan run" etc; the surface decision reversed them).
    assert "read/grep/glob" in result.output
    assert "present_plan" in result.output
    assert "/plan" not in result.output
    assert "/readonly" not in result.output
    assert tool.calls == []       # never executed
    assert approval.calls == []   # approval never spent


@pytest.mark.asyncio
async def test_readonly_allows_a_read_only_tool():
    tool = _FakeTool("read", mutates_world=False)
    outcomes = await _dispatcher([tool]).dispatch_batch(
        [_ref("c1", "read")], _ctx(readonly=True)
    )
    assert outcomes[0].denied is False
    assert tool.calls == [{}]


@pytest.mark.asyncio
async def test_a_writable_session_lets_the_same_tool_execute():
    tool = _FakeTool("write")
    approval = _Approval()
    outcomes = await _dispatcher([tool], approval=approval).dispatch_batch(
        [_ref("c1", "write")], _ctx()
    )
    assert outcomes[0].denied is False
    assert tool.calls == [{}]
    assert len(approval.calls) == 1


@pytest.mark.asyncio
async def test_a_tool_that_never_declared_the_attribute_is_refused_by_default():
    outcomes = await _dispatcher([_FreshTool()]).dispatch_batch(
        [_ref("c1", "fresh")], _ctx(readonly=True)
    )
    assert outcomes[0].denied is True
    assert outcomes[0].result.error == "readonly_denied"


@pytest.mark.asyncio
async def test_a_real_write_tool_is_refused_before_approval_and_touches_nothing(
    tmp_path: Path,
):
    """The real WARN-tier tool through the real gate: refusal comes first, the
    approval modal is never raised, and the file it would have written does not
    exist afterwards — the business effect, not just the verdict."""
    tool = WriteTool(Sandbox(str(tmp_path)), FileStateTracker())
    approval = _Approval()
    outcomes = await _dispatcher([tool], approval=approval).dispatch_batch(
        [_ref("c1", "write", '{"path": "x.txt", "content": "hi"}')],
        _ctx(readonly=True, workspace=str(tmp_path)),
    )
    assert outcomes[0].denied is True
    assert outcomes[0].result.error == "readonly_denied"
    assert approval.calls == []
    assert not (tmp_path / "x.txt").exists()


@pytest.mark.asyncio
async def test_a_real_read_tool_passes_the_gate_and_actually_reads(tmp_path: Path):
    """The whitelist side proven with the real tool: allowed through, and the
    read really happens against the file system."""
    target = tmp_path / "a.txt"
    target.write_text("hello from disk\n", encoding="utf-8")
    tool = ReadTool(Sandbox(str(tmp_path)), FileStateTracker())
    outcomes = await _dispatcher([tool]).dispatch_batch(
        [_ref("c1", "read", '{"path": "a.txt"}')],
        _ctx(readonly=True, workspace=str(tmp_path)),
    )
    assert outcomes[0].denied is False
    assert "hello from disk" in outcomes[0].result.output


def test_the_whitelist_classes_declare_themselves_read_only():
    from ai_coding.tools.ask_user_tool import AskUserTool
    from ai_coding.tools.checkpoint_tool import CheckpointTool
    from ai_coding.tools.glob_tool import GlobTool
    from ai_coding.tools.grep_tool import GrepTool
    from ai_coding.tools.memory_search_tool import MemorySearchTool
    from ai_coding.tools.read_tool import ReadTool
    from ai_coding.tools.skill_tool import SkillTool
    from ai_coding.tools.todo_write_tool import TodoWriteTool

    for cls in (
        ReadTool,
        GlobTool,
        GrepTool,
        TodoWriteTool,
        CheckpointTool,
        AskUserTool,
        SkillTool,
        MemorySearchTool,
    ):
        assert cls.mutates_world is False, cls.__name__


def test_the_mutating_classes_stay_mutating():
    from ai_coding.tools.bash_tool import BashTool
    from ai_coding.tools.edit_tool import EditTool
    from ai_coding.tools.mcp_call import MCPCallTool
    from ai_coding.tools.memory_write_tool import MemoryWriteTool
    from ai_coding.tools.subagent_tool import SubAgentTool
    from ai_coding.tools.write_tool import WriteTool

    for cls in (
        WriteTool,
        EditTool,
        BashTool,
        SubAgentTool,
        MemoryWriteTool,
        MCPCallTool,
    ):
        assert cls.mutates_world is True, cls.__name__
