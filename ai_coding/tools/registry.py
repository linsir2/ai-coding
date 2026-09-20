"""Tool registry — holds all available tools and provides lookup."""

from __future__ import annotations

from ai_coding.tools.base import BaseTool


class ToolRegistry:
    """A registry of named tools.

    Tools are registered once; duplicate names are rejected to prevent silent
    overrides.  M2 uses a single owner; M3 may add owner-scoped registration
    for subagents and MCP.
    """

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """Register a tool. Raises ``ValueError`` if the name is already taken."""
        if tool.name in self._tools:
            raise ValueError(
                f"tool '{tool.name}' is already registered; refusing to overwrite"
            )
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool | None:
        """Look up a tool by name, or return None if absent."""
        return self._tools.get(name)

    def list_names(self) -> list[str]:
        """Return all registered tool names (sorted for determinism)."""
        return sorted(self._tools.keys())

    @property
    def all_tools(self) -> list[BaseTool]:
        """Return all registered tools (sorted by name)."""
        return [self._tools[name] for name in sorted(self._tools.keys())]


__all__ = ["ToolRegistry"]
