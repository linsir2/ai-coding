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

from ai_coding.domain.message import ChatMessage, ToolCallRef


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


# --------------------------------------------------------------------------
# R1a-2: native chat-completions mapping (used by the self-owned loop).
#
# The functions above are SDK Responses-shaped (``function_call`` items) and
# remain because ``agents_sdk_service`` still uses them; they are removed in
# R1a-6 together with the SDK path.  Everything below is the OpenAI standard
# shape: assistant tool calls live in a ``tool_calls`` array on the assistant
# message, tool results are ``role="tool"`` messages carrying ``tool_call_id``.
# --------------------------------------------------------------------------


def tool_call_to_dict(ref: ToolCallRef) -> dict[str, Any]:
    """Map one domain tool-call reference onto OpenAI's function-call shape."""
    return {
        "id": ref.id,
        "type": "function",
        "function": {"name": ref.name, "arguments": ref.arguments or "{}"},
    }


def to_chat_message(msg: ChatMessage) -> dict[str, Any]:
    """Convert one domain message into a native chat-completions message dict.

    An assistant message that carries tool calls keeps ``content=None`` when it
    has no prose: that is the standard shape for a tool-call turn, and emitting
    an empty string instead would put a fake blank reply into the history.
    """
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
            "tool_calls": [tool_call_to_dict(c) for c in msg.tool_calls],
        }
    return {"role": "assistant", "content": msg.content or ""}


def history_to_chat_messages(history: list[ChatMessage]) -> list[dict[str, Any]]:
    """Convert a full history to native chat messages, preserving order."""
    return [to_chat_message(msg) for msg in history]


__all__ = [
    "chat_message_to_input",
    "chat_message_to_inputs",
    "history_to_inputs",
    "tool_call_to_dict",
    "to_chat_message",
    "history_to_chat_messages",
]
