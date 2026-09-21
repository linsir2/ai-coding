"""Core message and tool-call contracts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["user", "assistant", "tool", "system"]


@dataclass(frozen=True)
class ToolCallRef:
    """The model's raw tool-call reference: ``arguments`` is a JSON *string*."""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ToolCall:
    """A resolved tool call with its arguments parsed into a mapping."""

    id: str
    name: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    """The outcome produced by executing a tool, ready to be fed back."""

    success: bool
    output: str


@dataclass(frozen=True)
class ToolOutput:
    """A resolved tool's output paired with its originating call id.

    Captured from the SDK's ``tool_output`` stream event so the session can
    persist ``role=tool`` messages (strict-pairing contract of the Java shape).
    """

    call_id: str
    tool_name: str
    output: str


@dataclass(frozen=True)
class ToolExecution:
    """Pairing unit of a resolved tool call and its result."""

    tool_call: ToolCall
    result: ToolResult


def tool_call_from_ref(ref: ToolCallRef) -> ToolCall:
    """Parse ``ref.arguments`` JSON into ``params``; never raise on bad JSON."""
    try:
        parsed = json.loads(ref.arguments)
        params = parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        params = {}
    return ToolCall(id=ref.id, name=ref.name, params=params)


@dataclass
class ChatMessage:
    """A single conversation message.

    ``content`` is ``None`` for assistant messages that only carry tool calls.
    """

    role: Role
    content: str | None
    timestamp: str
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_calls: list[ToolCallRef] | None = None

    def __post_init__(self) -> None:
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("role='tool' messages must carry a tool_call_id")
        if self.role == "assistant" and not self.content and not self.tool_calls:
            raise ValueError("assistant messages must carry content or tool_calls")
