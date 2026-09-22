"""``BaseTool`` -> OpenAI function-calling schema (R1a).

The engine needs plain JSON schemas; the SDK-shaped adapter that used to do this
(``tools/sdk_adapter.py``) went away with the OpenAI Agents SDK.  Keeping this a
pure function makes it trivially testable and keeps protocol details out of the
tool implementations.
"""

from __future__ import annotations

from typing import Any

from ai_coding.tools.base import BaseTool
from ai_coding.tools.registry import ToolRegistry


def tool_schema(tool: BaseTool) -> dict[str, Any]:
    """Return the ``{"type": "function", ...}`` schema for one tool."""
    raw = tool.params_json_schema
    params: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    params.setdefault("type", "object")
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": params,
        },
    }


def tool_schemas(registry: ToolRegistry) -> list[dict[str, Any]]:
    """Return schemas for every registered tool, in ``ToolRegistry`` order.

    ``ToolRegistry.all_tools`` is name-sorted, so the emitted list is stable;
    a stable tool list keeps request bodies (and therefore server-side prompt
    caching) stable across turns.
    """
    return [tool_schema(tool) for tool in registry.all_tools]


def tool_schemas_from_tools(tools: list[BaseTool]) -> list[dict[str, Any]]:
    """Return schemas for an explicit tool list, preserving its order."""
    return [tool_schema(tool) for tool in tools]


__all__ = ["tool_schema", "tool_schemas", "tool_schemas_from_tools"]
