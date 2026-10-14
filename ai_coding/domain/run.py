"""Message helpers and the tool-output envelope.

The envelope is a structure invariant: the compression stages and the pairing
logic both rely on a tool result arriving wrapped in ``<tool_output ...>``, and
``<persisted-output>`` markers inside it drive the L3 stage.

``TurnResult`` (M1) was removed in R1a-6 — ``StepResult`` replaced it, because the
loop owns the turn now and the engine only ever produces one step.
"""

from __future__ import annotations

import json
from typing import Any

from .loop import StepResult
from .message import ChatMessage, ImageRef, ToolOutput


def user_message(
    content: str,
    timestamp: str,
    images: tuple[ImageRef, ...] = (),
    *,
    synthetic: bool = False,
) -> ChatMessage:
    """A ``role="user"`` message.

    ``synthetic`` marks the ones the harness writes into the user's own voice
    (r18/F3).  It is the single factory every user message goes through, so this
    one keyword is what lets the runtime tell the two apart.
    """
    return ChatMessage(
        role="user", content=content, timestamp=timestamp, images=images, synthetic=synthetic
    )


def assistant_message(step: StepResult, timestamp: str) -> ChatMessage:
    """Wrap one completed model step as an assistant message."""
    return ChatMessage(
        role="assistant",
        content=step.text,
        timestamp=timestamp,
        tool_calls=step.tool_calls or None,
        reasoning=step.reasoning,
    )


def wrap_tool_output(
    tool_name: str,
    output: str,
    *,
    success: bool = True,
    data: dict[str, Any] | None = None,
) -> str:
    """Wrap a tool output in the ``<tool_output>`` feedback envelope.

    Invariants:
      - ``success=True`` and ``data is None`` produces exactly the M0–M6
        envelope byte-for-byte, so replays and the L2 stage are unaffected;
      - a failed result adds ``ok="false"``;
      - structured results add a machine-readable ``<data>`` JSON block that the
        model can read and the runtime can parse back without regexes.
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


__all__ = ["user_message", "assistant_message", "wrap_tool_output", "tool_message"]
