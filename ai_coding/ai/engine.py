"""ChatEngine port — one model step at a time (R1a-2).

The orchestration layer owns the iteration loop; an engine only ever performs a
**single** model call.  ``messages`` already contains the current user turn, so
an engine must never append anything of its own — that was the source of the
double user-turn bug in the SDK path.

This module depends only on ``domain``.  Tool schemas arrive pre-built as plain
dicts, so the ``ai`` layer never imports the ``tools`` layer (that would create
a cycle: tools -> security -> ... and core -> ai).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from ai_coding.domain.loop import StepResult
from ai_coding.domain.message import ChatMessage

DeltaSink = Callable[[str], None]


class EngineError(RuntimeError):
    """A model-call failure the loop must see (never swallowed)."""


class ChatEngine(ABC):
    """Port for executing exactly one step of a conversation."""

    @abstractmethod
    async def step(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]],
        instructions: str | None = None,
        on_delta: DeltaSink | None = None,
    ) -> StepResult:
        """Run one model call over ``messages`` and return what it produced.

        ``messages`` is the full conversation *including* the current user turn.
        ``tools`` are OpenAI function schemas (empty list = no tools offered).
        ``instructions`` is the system prompt for this request, kept separate so
        the caller's persisted history stays pure conversation.
        ``on_delta`` receives streamed text deltas as they arrive.
        """

    async def aclose(self) -> None:
        """Release engine resources.  Default: nothing to release."""
        return None


__all__ = ["ChatEngine", "EngineError", "DeltaSink"]
