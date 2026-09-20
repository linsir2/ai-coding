"""Sub-agent turn contracts.

A sub-agent runs against an isolated conversation and returns only its final
conclusion (intermediate tool activity is discarded), mirroring the original
``SubAgent`` semantics.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .message import ToolCallRef


@dataclass(frozen=True)
class SubagentTurn:
    """A single sub-agent turn: an optional conclusion and pending tool calls."""

    text: str = ""
    tool_calls: list[ToolCallRef] = field(default_factory=list)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


@dataclass(frozen=True)
class SubagentResult:
    """The final conclusion of a sub-agent (``None`` when none was produced)."""

    text: str | None
