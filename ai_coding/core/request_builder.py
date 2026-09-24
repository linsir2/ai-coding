"""Assembling one request: the volatile tail and the tool context (R5).

The tail is the small region of a request that *is allowed* to change between
iterations — the memory constraint block, the task-state block, and whatever a
``turn.start`` hook injected.  Everything before it (the system prompt and the
history) stays byte-identical, which is what keeps the provider's prefix cache
alive (docs/r3-analysis.md 1.13).

Because that is a property worth protecting, the tail gets a name, a declared
order and its own tests instead of being thirty inline lines inside an already
229-line method.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ai_coding.domain.message import ChatMessage
from ai_coding.domain.session import SessionData
from ai_coding.domain.task import TaskState
from ai_coding.tools.base import ToolContext

__all__ = ["TailBlock", "build_tail", "tool_context"]


@dataclass(frozen=True)
class TailBlock:
    """One volatile block of a request, with the label it is reported under."""

    name: str  # "memory" | "task" | "hook"
    message: ChatMessage

    @property
    def chars(self) -> int:
        return len(self.message.content or "")


def build_tail(
    blocks: Sequence[TailBlock | None],
) -> tuple[list[ChatMessage], tuple[tuple[str, int], ...]]:
    """Turn tail blocks into ``(messages, sizes)``.

    Order is the caller's declaration order — never sorted, never merged — so the
    request is a deterministic function of what the loop decided to include, and
    ``ContextAssembled.tail_blocks`` can be read against the message list.
    """
    messages: list[ChatMessage] = []
    sizes: list[tuple[str, int]] = []
    for block in blocks:
        if block is None:
            continue
        messages.append(block.message)
        sizes.append((block.name, block.chars))
    return messages, tuple(sizes)


def tool_context(
    *,
    task: TaskState,
    workspace: str,
    session: SessionData,
    history: Sequence[ChatMessage],
    instructions: str | None,
    emit: Callable[[Any], None],
) -> ToolContext:
    """The per-invocation context a tool receives (the dispatcher stamps call ids)."""
    return ToolContext(
        task=task,
        workspace=workspace,
        session_id=session.session_id,
        messages=tuple(history),
        instructions=instructions,
        emit=emit,
    )
