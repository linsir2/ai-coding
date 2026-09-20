"""Base tool contract.

Every tool exposes:
  - ``name`` / ``description`` / ``params_json_schema`` — for the LLM to discover it
  - ``execute(params) -> ToolResult`` — the actual implementation

The LLM sees the JSON schema; the SDK parses arguments and passes a dict.
Tools return ``ToolResult`` (M0 domain contract) so all callers treat results uniformly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ai_coding.domain.message import ToolResult


class BaseTool(ABC):
    """Abstract base for all tools (built-in and MCP)."""

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        ...

    @property
    @abstractmethod
    def params_json_schema(self) -> dict[str, Any]:
        """JSON Schema (Draft 2020-12 compatible) for the tool parameters."""
        ...

    @abstractmethod
    async def execute(self, params: dict[str, Any]) -> ToolResult:
        """Execute the tool with already-parsed parameters."""


__all__ = ["BaseTool"]
