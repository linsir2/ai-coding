"""Outcome of a single model turn.

Reserved ``tool_calls`` so M2 can attach tool requests without reshaping the contract.
``tool_outputs`` (M3) allows the session to persist strict-pairing ``tool`` messages.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .message import ChatMessage, ToolCallRef, ToolOutput


@dataclass(frozen=True)
class TurnResult:
    """The model's response to one user input within one turn."""

    text: str | None = None
    tool_calls: list[ToolCallRef] = field(default_factory=list)
    tool_outputs: list[ToolOutput] = field(default_factory=list)


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


def wrap_tool_output(tool_name: str, output: str) -> str:
    """Wrap a tool output in the ``<tool_output>`` feedback envelope (invariant #15)."""
    return f"<tool_output tool=\"{tool_name}\">\n{output}\n</tool_output>"


def tool_message(out: ToolOutput, timestamp: str) -> ChatMessage:
    """Build a strict-pairing ``role=tool`` message from a captured output."""
    return ChatMessage(
        role="tool",
        content=wrap_tool_output(out.tool_name, out.output),
        timestamp=timestamp,
        tool_call_id=out.call_id,
        tool_name=out.tool_name,
    )
