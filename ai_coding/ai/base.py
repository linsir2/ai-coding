"""AI service port (abstract). Domain-agnostic; SDK-agnostic.

``AgentLoop`` and future Hooks depend only on this interface, so the concrete
engine (OpenAI Agents SDK) can be swapped without touching the core flow.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

from ai_coding.domain.message import ChatMessage
from ai_coding.domain.run import TurnResult


class AIService(ABC):
    """Port for executing a single turn of the conversational engine."""

    @abstractmethod
    async def execute_turn(
        self,
        history: list[ChatMessage],
        user_input: str,
        token_sink: Callable[[str], None] | None = None,
    ) -> TurnResult:
        """Run one turn against the full session history plus the new user input.

        ``token_sink``, when provided, receives streamed text deltas as they arrive
        (used for live CLI output and future streaming Hooks).
        """
        raise NotImplementedError


__all__ = ["AIService"]
