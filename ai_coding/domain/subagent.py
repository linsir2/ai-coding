"""Sub-agent contracts (R1a-5).

A sub-agent runs the **same loop** as its parent, seeded with the parent turn's
request view and system prompt, and returns a structured packet rather than a
bare string.  Its internal reasoning is deliberately not returned
(docs/r1-analysis.md D10).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .message import ChatMessage, ToolArtifact, ToolCallRef


@dataclass(frozen=True)
class SubagentTurn:
    """A single sub-agent turn: an optional conclusion and pending tool calls."""

    text: str = ""
    tool_calls: list[ToolCallRef] = field(default_factory=list)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


@dataclass(frozen=True)
class ParentContext:
    """What the parent turn was looking at when it delegated.

    ``messages`` is the parent request view (compressed, *before* its volatile
    task block) and ``instructions`` is the assembled system prompt — so the
    sub-agent sees exactly what the parent saw on that step.
    """

    messages: tuple[ChatMessage, ...] = ()
    instructions: str | None = None


@dataclass(frozen=True)
class SubagentResult:
    """The packet a sub-agent returns to its caller."""

    status: str = "completed"  # completed | max_iterations | timeout | error
    conclusion: str | None = None
    todos: tuple[dict[str, Any], ...] = ()
    artifacts: tuple[ToolArtifact, ...] = ()
    iterations: int = 0
    workdir: str | None = None
    ledger_path: str | None = None
    error: str | None = None
    #: Which window the child ran with (R7e-4): ``full`` or ``task_only``.
    context_mode: str = "full"

    @property
    def text(self) -> str | None:
        """Backwards-compatible alias for :attr:`conclusion` (M4 call sites)."""
        return self.conclusion

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "conclusion": self.conclusion,
            "todos": [dict(entry) for entry in self.todos],
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "iterations": self.iterations,
            "workdir": self.workdir,
            "ledger_path": self.ledger_path,
            "error": self.error,
            "context_mode": self.context_mode,
        }


__all__ = ["SubagentTurn", "ParentContext", "SubagentResult"]
