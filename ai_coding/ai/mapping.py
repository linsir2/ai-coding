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

from collections.abc import Callable
from typing import Any

from ai_coding.domain.message import (
    ChatMessage,
    ImageRef,
    ToolCallRef,
    normalize_arguments,
)

#: Turns one message image into a data URL, or None when it cannot be read
#: (r17/T5).  The caller that owns the workspace supplies it for a call, so
#: this module stays pure and the engine stays ignorant of where files live.
ImageLoader = Callable[[ImageRef], str | None]


def tool_call_to_dict(ref: ToolCallRef) -> dict[str, Any]:
    """Map one domain tool-call reference onto OpenAI's function-call shape.

    ``arguments`` is repaired here rather than upstream because this is the last
    point before the provider sees it: a truncated string (``max_tokens`` cut a
    ``write`` mid-JSON) makes the *entire* later request a 400, so one bad step
    poisons the whole session (R21 §10).  The call itself was already refused by
    the dispatcher; this only keeps the history sendable.
    """
    return {
        "id": ref.id,
        "type": "function",
        "function": {"name": ref.name, "arguments": normalize_arguments(ref.arguments)},
    }


def to_chat_message(
    msg: ChatMessage, *, image_loader: ImageLoader | None = None
) -> dict[str, Any]:
    """Convert one domain message into a native chat-completions message dict.

    A user message with images becomes the content-parts form
    (``[{type: text}, {type: image_url, ...}]``); the loader resolves each
    reference.  An unreadable image degrades to a text part naming the failure
    -- a broken image must not fail the whole request (r17/T5).
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
        if not msg.images:
            return {"role": "user", "content": msg.content or ""}
        parts: list[dict[str, Any]] = []
        text = msg.content or ""
        if text:
            parts.append({"type": "text", "text": text})
        for ref in msg.images:
            data_url = image_loader(ref) if image_loader is not None else None
            if data_url:
                parts.append({"type": "image_url", "image_url": {"url": data_url}})
            else:
                parts.append({"type": "text", "text": f"（图片 {ref.name} 无法读取）"})
        return {"role": "user", "content": parts}

    if msg.tool_calls:
        return {
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [tool_call_to_dict(call) for call in msg.tool_calls],
        }
    return {"role": "assistant", "content": msg.content or ""}


def history_to_chat_messages(
    history: list[ChatMessage], *, image_loader: ImageLoader | None = None
) -> list[dict[str, Any]]:
    """Convert a full history to native chat messages, preserving order."""
    return [to_chat_message(msg, image_loader=image_loader) for msg in history]


__all__ = ["tool_call_to_dict", "to_chat_message", "history_to_chat_messages"]
