"""TDD (M2-3): SDK adapter — BaseTool -> FunctionTool + approval wiring.

Offline tests: construct FunctionTool from a fake BaseTool and verify
properties / invoke / needs_approval behavior.
"""

import json

import pytest

from ai_coding.domain.message import ToolResult
from ai_coding.security.approval import (
    ApprovalCallback,
    ApprovalResult,
)
from ai_coding.security.permission_gate import PermissionGate
from ai_coding.tools.base import BaseTool
from ai_coding.tools.sdk_adapter import tool_to_function_tool


class _EchoTool(BaseTool):
    @property
    def name(self):
        return "echo"

    @property
    def description(self):
        return "Echoes back the input text."

    @property
    def params_json_schema(self):
        return {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "text to echo"},
            },
            "required": ["text"],
        }

    async def execute(self, params):
        return ToolResult(success=True, output=f"echo: {params.get('text', '')}")


class _ReadTool(BaseTool):
    @property
    def name(self):
        return "read"

    @property
    def description(self):
        return "read file"

    @property
    def params_json_schema(self):
        return {
            "type": "object",
            "properties": {"path": {"type": "string"}},
        }

    async def execute(self, params):
        return ToolResult(success=True, output="content")


class _BashTool(BaseTool):
    @property
    def name(self):
        return "bash"

    @property
    def description(self):
        return "bash"

    @property
    def params_json_schema(self):
        return {
            "type": "object",
            "properties": {"command": {"type": "string"}},
        }

    async def execute(self, params):
        return ToolResult(success=True, output="done")


class _WriteTool(BaseTool):
    @property
    def name(self):
        return "write"

    @property
    def description(self):
        return "write"

    @property
    def params_json_schema(self):
        return {
            "type": "object",
            "properties": {"path": {"type": "string"}},
        }

    async def execute(self, params):
        return ToolResult(success=True, output="ok")


class _FakeApproval(ApprovalCallback):
    def __init__(self, result: ApprovalResult = ApprovalResult.YES) -> None:
        self.result = result
        self.calls: list[tuple[str, dict, str]] = []

    async def approve(self, tool_name, params, reason):
        self.calls.append((tool_name, params, reason))
        return self.result


def test_adapter_preserves_name_and_desc():
    tool = _EchoTool()
    gate = PermissionGate()
    ft = tool_to_function_tool(tool, gate, approval=None)
    assert ft.name == "echo"
    assert "Echoes back" in ft.description


@pytest.mark.asyncio
async def test_adapter_invoke_returns_output():
    tool = _EchoTool()
    gate = PermissionGate()
    ft = tool_to_function_tool(tool, gate, approval=None)

    class FakeCtx:
        tool_arguments = json.dumps({"text": "hello world"})
        tool_call_id = "call_123"

    result = await ft.on_invoke_tool(FakeCtx(), "call_123")
    assert "echo: hello world" in str(result)


@pytest.mark.asyncio
async def test_adapter_needs_approval_allow():
    gate = PermissionGate()
    ft = tool_to_function_tool(_ReadTool(), gate, approval=None)
    needs = await ft.needs_approval(None, {"path": "f.txt"}, "c1")
    assert needs is False  # ALLOW → does not need approval → proceed


@pytest.mark.asyncio
async def test_adapter_needs_approval_deny():
    gate = PermissionGate()
    ft = tool_to_function_tool(_BashTool(), gate, approval=None)
    # sudo ls → hard deny → needs_approval returns True (blocked)
    needs = await ft.needs_approval(None, {"command": "sudo ls"}, "c1")
    assert needs is True


@pytest.mark.asyncio
async def test_adapter_approval_callback_called_for_warn():
    gate = PermissionGate()
    approval = _FakeApproval(ApprovalResult.YES)
    ft = tool_to_function_tool(_WriteTool(), gate, approval=approval)
    # write → WARN → ask user → YES → does not need approval → proceed
    needs = await ft.needs_approval(None, {"path": "f.txt"}, "c1")
    assert needs is False
    assert len(approval.calls) == 1
    assert approval.calls[0][0] == "write"


@pytest.mark.asyncio
async def test_adapter_approval_no_blocks_tool():
    gate = PermissionGate()
    approval = _FakeApproval(ApprovalResult.NO)
    ft = tool_to_function_tool(_WriteTool(), gate, approval=approval)
    needs = await ft.needs_approval(None, {"path": "f.txt"}, "c1")
    assert needs is True  # user said NO → needs approval → blocked
