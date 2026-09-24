"""TDD (R4a): every tool invocation gets its own context stamped with its call id.

The dispatcher hands one context to a whole batch, but a trace needs to know
*which* call each execution belonged to — that is what links a sub-agent's span
tree back to the ``subAgent`` call that spawned it (docs/r4-analysis.md 4.6).
"""

import pytest

from ai_coding.core.dispatch import ToolDispatcher
from ai_coding.domain.message import ToolCallRef, ToolResult
from ai_coding.security.permission_gate import PermissionGate
from ai_coding.tools.base import BaseTool, ToolContext
from ai_coding.tools.registry import ToolRegistry


class _RecordingTool(BaseTool):
    parallel_safe = True

    def __init__(self) -> None:
        self.seen: list[str] = []

    @property
    def name(self) -> str:
        return "read"

    @property
    def description(self) -> str:
        return "records the call id it was given"

    @property
    def params_json_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, params, ctx=None) -> ToolResult:
        assert ctx is not None
        self.seen.append(ctx.call_id)
        return ToolResult(success=True, output="ok")


@pytest.mark.asyncio
async def test_each_call_in_a_batch_gets_its_own_call_id():
    tool = _RecordingTool()
    registry = ToolRegistry()
    registry.register(tool)
    dispatcher = ToolDispatcher(registry, PermissionGate(), max_parallel=2)

    await dispatcher.dispatch_batch(
        [
            ToolCallRef(id="c1", name="read", arguments="{}"),
            ToolCallRef(id="c2", name="read", arguments="{}"),
        ],
        ToolContext(workspace="."),
    )

    assert sorted(tool.seen) == ["c1", "c2"]


def test_tool_context_defaults_to_an_empty_call_id():
    assert ToolContext().call_id == ""
