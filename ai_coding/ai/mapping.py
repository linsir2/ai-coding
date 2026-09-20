"""Domain ChatMessage -> OpenAI Agents SDK input items (pure, testable).

The SDK's ``RunItemStreamEvent``/Responses API accepts two dict shapes:
  - user / assistant : ``{"role", "content"}``
  - tool (function output) : ``{"type": "function_call_output", "call_id", "output"}``
"""

from __future__ import annotations

from typing import Any

from ai_coding.domain.message import ChatMessage


def chat_message_to_input(msg: ChatMessage) -> dict[str, Any]:
    """Convert one domain message to an SDK input item dict."""
    if msg.role == "tool":
        return {
            "type": "function_call_output",
            "call_id": msg.tool_call_id or "",
            "output": msg.content or "",
        }
    # user / assistant: assistant without content carries only tool calls in M2+;
    # for M1 there is no tool wiring, so emit empty text rather than dropping it.
    return {"role": msg.role, "content": msg.content or ""}


def history_to_inputs(history: list[ChatMessage]) -> list[dict[str, Any]]:
    """Convert a full session history preserving order."""
    return [chat_message_to_input(m) for m in history]


__all__ = ["chat_message_to_input", "history_to_inputs"]
