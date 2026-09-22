"""Base tool contract.

Every tool exposes:
  - ``name`` / ``description`` / ``params_json_schema`` — for the LLM to discover it
  - ``execute(params) -> ToolResult`` — the actual implementation

The LLM sees the JSON schema; the SDK parses arguments and passes a dict.
Tools return ``ToolResult`` (M0 domain contract) so all callers treat results uniformly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ai_coding.domain.message import ChatMessage, ToolResult
from ai_coding.domain.task import TaskState


def _noop_emit(_event: Any) -> None:
    """Default event sink for contexts built outside the loop."""


@dataclass
class ToolContext:
    """Per-invocation context handed to ``BaseTool.execute``.

    Tools stay stateless singletons (the tool stack is built once and reused
    across sessions); everything session-scoped travels through here.  This is
    also how ``subAgent`` gets the parent turn's context (docs/r1-analysis.md D10).
    """

    task: TaskState = field(default_factory=TaskState)
    workspace: str = "."
    session_id: str = ""
    messages: tuple[ChatMessage, ...] = ()
    instructions: str | None = None
    emit: Callable[[Any], None] = _noop_emit


def resolve_ctx(ctx: ToolContext | None) -> ToolContext:
    """Return ``ctx`` or a throwaway default.

    Migration aid: during R1a tools may still be called without a context (legacy
    call sites and unit tests).  From R1a-3 on, ``ToolDispatcher`` always passes a
    real one.
    """
    return ctx if ctx is not None else ToolContext()


class BaseTool(ABC):
    """Abstract base for all tools (built-in and MCP)."""

    # Conservative default: NOT safe to run concurrently with sibling calls in
    # the same assistant message.  Read-only tools opt in by setting True.
    parallel_safe: bool = False

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
    async def execute(
        self,
        params: dict[str, Any],
        ctx: ToolContext | None = None,
    ) -> ToolResult:
        """Execute the tool with already-parsed parameters.

        ``ctx`` carries session-scoped state (task list, workspace, session id,
        parent-turn view).  It is optional only for migration; new call sites
        should always pass one.
        """


__all__ = ["BaseTool", "ToolContext", "resolve_ctx"]
