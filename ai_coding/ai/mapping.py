"""Domain ChatMessage -> OpenAI Agents SDK input items (pure, testable).

The SDK's ``Runner``/Responses API accepts several input dict shapes:
  - user / plain assistant : ``{"role", "content"}``
  - assistant tool calls   : ``{"type": "function_call", "call_id", "name", "arguments"}``
  - tool (function output) : ``{"type": "function_call_output", "call_id", "output"}``

IMPORTANT (M3): an assistant message that carries tool calls must be emitted as
``function_call`` item(s), otherwise the paired ``function_call_output`` messages
become orphans and the Chat Completions API rejects the replay with a 400.  Text on
the same assistant message is merged (after the call items) into a single assistant
message — emitting two assistant messages is rejected by the API.
"""

from __future__ import annotations

from typing import Any

from ai_coding.domain.message import ChatMessage


def chat_message_to_inputs(msg: ChatMessage) -> list[dict[str, Any]]:
    """Convert one domain message to the (possibly several) SDK input item dicts."""
    if msg.role == "tool":
        return [
            {
                "type": "function_call_output",
                "call_id": msg.tool_call_id or "",
                "output": msg.content or "",
            }
        ]
    if msg.role == "user":
        return [{"role": "user", "content": msg.content or ""}]

    # assistant: tool calls first, then optional text (merged into one assistant message).
    items: list[dict[str, Any]] = []
    if msg.tool_calls:
        for call in msg.tool_calls:
            items.append(
                {
                    "type": "function_call",
                    "call_id": call.id,
                    "name": call.name,
                    "arguments": call.arguments or "{}",
                }
            )
        if msg.content:
            items.append(
                {
                    "type": "message",
                    "role": "assistant",
                    "id": f"asst_{msg.timestamp}",
                    "content": [{"type": "output_text", "text": msg.content}],
                }
            )
    elif msg.content:
        # plain assistant text keeps the simple easy-input shape.
        items = [{"role": "assistant", "content": msg.content}]
    else:
        # assistant with neither content nor calls — keep a valid minimal message.
        items = [{"role": "assistant", "content": ""}]
    return items


def chat_message_to_input(msg: ChatMessage) -> dict[str, Any]:
    """Single-item convenience: raise when a message expands to multiple items."""
    items = chat_message_to_inputs(msg)
    if len(items) != 1:
        raise ValueError(
            f"message maps to {len(items)} input items; use chat_message_to_inputs"
        )
    return items[0]


def history_to_inputs(history: list[ChatMessage]) -> list[dict[str, Any]]:
    """Convert a full session history to SDK input items, preserving order."""
    return [item for msg in history for item in chat_message_to_inputs(msg)]


__all__ = ["chat_message_to_input", "chat_message_to_inputs", "history_to_inputs"]
