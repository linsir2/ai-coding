"""Outcome of a single model turn.

Reserved ``tool_calls`` so M2 can attach tool requests without reshaping the contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .message import ChatMessage, ToolCallRef


@dataclass(frozen=True)
class TurnResult:
    """The model's response to one user input within one turn."""

    text: str | None = None
    tool_calls: list[ToolCallRef] = field(default_factory=list)


def user_message(content: str, timestamp: str) -> ChatMessage:
    return ChatMessage(role="user", content=content, timestamp=timestamp)


def assistant_message(turn: TurnResult, timestamp: str) -> ChatMessage:
    """Wrap a ``TurnResult`` as an assistant message for the session history."""
    return ChatMessage(
        role="assistant",
        content=turn.text,
        timestamp=timestamp,
        tool_calls=turn.tool_calls or None,
    )
