"""Outcome of a single model turn.

Reserved ``tool_calls`` so M2 can attach tool requests without reshaping the contract.
``tool_outputs`` (M3) allows the session to persist strict-pairing ``tool`` messages.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .loop import StepResult
from .message import ChatMessage, ToolCallRef, ToolOutput


@dataclass(frozen=True)
class TurnResult:
    """The model's response to one user input within one turn."""

    text: str | None = None
    tool_calls: list[ToolCallRef] = field(default_factory=list)
    tool_outputs: list[ToolOutput] = field(default_factory=list)


def user_message(content: str, timestamp: str) -> ChatMessage:
    return ChatMessage(role="user", content=content, timestamp=timestamp)


def assistant_message(turn: TurnResult | StepResult, timestamp: str) -> ChatMessage:
    """Wrap a step/turn result as an assistant message for the session history.

    Both ``TurnResult`` (legacy) and ``StepResult`` (R1a) expose ``text`` and
    ``tool_calls``, so the same shape works for either.
    """
    return ChatMessage(
        role="assistant",
        content=turn.text,
        timestamp=timestamp,
        tool_calls=turn.tool_calls or None,
    )


def wrap_tool_output(
    tool_name: str,
    output: str,
    *,
    success: bool = True,
    data: dict[str, Any] | None = None,
) -> str:
    """Wrap a tool output in the ``<tool_output>`` feedback envelope.

    Invariants (R1a/D9):
      - ``success=True`` and ``data is None`` produces **exactly** the M0–M6
        envelope byte-for-byte, so existing tools, replays and tests are
        unaffected.  This is what makes the R1a migration incremental.
      - a failed result adds ``ok="false"``.
      - structured results add a machine-readable ``<data>`` JSON block, which
        the model can read and the runtime can parse back without regexes.
    """
    ok_attr = "" if success else ' ok="false"'
    head = f'<tool_output tool="{tool_name}"{ok_attr}>'
    blocks: list[str] = []
    if data is not None:
        payload = json.dumps(data, ensure_ascii=False, default=str)
        blocks.append(f"<data>\n{payload}\n</data>")
    if output:
        blocks.append(output)
    return f"{head}\n" + "\n".join(blocks) + "\n</tool_output>"


def tool_message(out: ToolOutput, timestamp: str) -> ChatMessage:
    """Build a strict-pairing ``role=tool`` message from a captured output."""
    return ChatMessage(
        role="tool",
        content=wrap_tool_output(
            out.tool_name,
            out.output,
            success=out.success,
            data=out.data,
        ),
        timestamp=timestamp,
        tool_call_id=out.call_id,
        tool_name=out.tool_name,
    )
