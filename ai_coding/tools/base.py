"""Base tool contract.

Every tool exposes:
  - ``name`` / ``description`` / ``params_json_schema`` — for the LLM to discover it
  - ``execute(params) -> ToolResult`` — the actual implementation

The LLM sees the JSON schema; the SDK parses arguments and passes a dict.
Tools return ``ToolResult`` (M0 domain contract) so all callers treat results uniformly.
"""

from __future__ import annotations

import threading
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
    # R4a: the id of the call this execution belongs to.  The dispatcher stamps
    # one context per call (the batch shares everything else), which is what lets
    # a trace hang a sub-agent's spans under the subAgent call that spawned it
    # (docs/r4-analysis.md 4.6).
    call_id: str = ""
    # R7e-1: the session's turn counter, so a note the model writes can record where
    # it came from (`source_turn` means turn, R6d).
    turn: int = 0
    # R8a: how a tool tells the running turn "remember this line for later"
    # (``checkpoint_write``).  ``None`` outside a loop that owns a turn book.
    note_checkpoint: Callable[[str], None] | None = None
    # R8c: a thread-safe "stop" flag for tools that own a subprocess (bash hands
    # it to the process runner).  The dispatcher sets it when the user cancels, so
    # the child dies even though the awaiting task was abandoned.
    kill_event: threading.Event | None = None


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
    def origin(self) -> str:
        """Where this tool came from: ``"local"``, or an MCP server's name (R7e-3).

        The tool surface is paid for on every model step, so a cost warning has to
        name the expensive part.  The default keeps every built-in tool out of it.
        """
        return "local"

    @property
    def max_duration_seconds(self) -> float | None:
        """Per-call ceiling for the dispatcher (R2a), in seconds.

        ``None`` means the tool declares no bound of its own, so only the turn
        deadline applies.  Tools that spawn subprocesses or sub-agents override
        this with ``timeout + grace``; the dispatcher takes the tighter of this
        and what is left of the turn.
        """
        return None

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

    def precheck(self, params: dict[str, Any]) -> ToolResult | None:
        """Cheap refusal for calls that cannot execute — checked before approval.

        The dispatcher runs this *before* asking the user: a call the sandbox
        will reject must not prompt for consent (hotel audit, root cause R2).
        Default ``None`` means "no opinion"; file tools override it with the
        same ``Sandbox`` they execute against, so the two can never disagree.
        """
        return None


__all__ = ["BaseTool", "ToolContext", "resolve_ctx"]
