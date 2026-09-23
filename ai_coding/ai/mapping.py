"""Domain ``ChatMessage`` -> native chat-completions message (pure, testable).

The shape is the OpenAI standard one:

  - user / system   : ``{"role", "content"}``
  - assistant       : ``{"role", "content", "tool_calls": [...]}`` — ``content``
                      stays ``None`` on a tool-only turn, which is the correct
                      representation of "no prose, just calls"
  - tool (result)   : ``{"role": "tool", "tool_call_id", "content"}``

This replaced the M1-M3 SDK mapping (``function_call`` input items), whose whole
purpose was working around the Responses API shape; with native ``tool_calls``
that class of replay bugs (assistant calls missing from a replayed history) is
gone by construction.
"""

from __future__ import annotations

from typing import Any

from ai_coding.domain.message import ChatMessage, ToolCallRef


def tool_call_to_dict(ref: ToolCallRef) -> dict[str, Any]:
    """Map one domain tool-call reference onto OpenAI's function-call shape."""
    return {
        "id": ref.id,
        "type": "function",
        "function": {"name": ref.name, "arguments": ref.arguments or "{}"},
    }


def to_chat_message(msg: ChatMessage) -> dict[str, Any]:
    """Convert one domain message into a native chat-completions message dict."""
    if msg.role == "tool":
        return {
            "role": "tool",
            "tool_call_id": msg.tool_call_id or "",
            "content": msg.content or "",
        }
    if msg.role == "system":
        return {"role": "system", "content": msg.content or ""}
    if msg.role == "user":
        return {"role": "user", "content": msg.content or ""}

    if msg.tool_calls:
        return {
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [tool_call_to_dict(call) for call in msg.tool_calls],
        }
    return {"role": "assistant", "content": msg.content or ""}


def history_to_chat_messages(history: list[ChatMessage]) -> list[dict[str, Any]]:
    """Convert a full history to native chat messages, preserving order."""
    return [to_chat_message(msg) for msg in history]


__all__ = ["tool_call_to_dict", "to_chat_message", "history_to_chat_messages"]
