"""SDK adapter — convert a ``BaseTool`` into an SDK ``FunctionTool``.

This is the second file (alongside ``agents_sdk_service.py``) that imports
from ``agents``.  Everything else stays SDK-agnostic.
"""

from __future__ import annotations

import json
from typing import Any

from agents.tool import FunctionTool

from ai_coding.security.approval import ApprovalCallback, approve_from_gate_decision
from ai_coding.security.permission_gate import PermissionGate
from ai_coding.tools.base import BaseTool


def tool_to_function_tool(
    tool: BaseTool,
    gate: PermissionGate,
    approval: ApprovalCallback | None = None,
) -> FunctionTool:
    """Wrap a domain ``BaseTool`` as an SDK ``FunctionTool`` with approval gate."""

    async def _invoke(ctx: Any, call_id: str) -> str:
        """Execute the tool and return its output string.

        The SDK provides ``ctx.tool_arguments`` as a raw JSON string; we parse
        it into a dict and forward to ``tool.execute``.
        """
        try:
            params = json.loads(ctx.tool_arguments) if isinstance(
                ctx.tool_arguments, str
            ) else {}
            if not isinstance(params, dict):
                params = {}
        except (json.JSONDecodeError, TypeError, AttributeError):
            params = {}

        result = await tool.execute(params)
        return result.output

    async def _needs_approval(
        ctx: Any, params: dict[str, Any], call_id: str
    ) -> bool:
        """Return True if the tool call needs approval (= must be blocked).

        Semantics match the SDK contract:
          - True  → needs approval → don't execute
          - False → no approval needed → proceed
        """
        decision = gate.check(tool.name, params)
        ok = await approve_from_gate_decision(
            decision, tool.name, params, approval
        )
        return not ok  # invert: ok → False (no approval needed), blocked → True

    return FunctionTool(
        name=tool.name,
        description=tool.description,
        params_json_schema=tool.params_json_schema,
        on_invoke_tool=_invoke,
        needs_approval=_needs_approval,
        strict_json_schema=False,
    )


def registry_to_sdk_tools(
    registry: Any,
    gate: PermissionGate,
    approval: ApprovalCallback | None = None,
) -> list[FunctionTool]:
    """Convert every tool in a ``ToolRegistry`` to SDK ``FunctionTool`` s."""
    from ai_coding.tools.registry import ToolRegistry

    if not isinstance(registry, ToolRegistry):
        raise TypeError("registry must be a ToolRegistry")
    return [
        tool_to_function_tool(t, gate, approval) for t in registry.all_tools
    ]


__all__ = ["tool_to_function_tool", "registry_to_sdk_tools"]
