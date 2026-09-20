"""The agent main loop: orchestrate a single conversation turn.

Flow (M1): append user msg -> run AI over full history -> append assistant msg ->
clean tool pairing -> persist. Hooks (delta handlers) plug in via ``on_delta``.
"""

from __future__ import annotations

from collections.abc import Callable

from ai_coding.ai.base import AIService
from ai_coding.domain.run import assistant_message, user_message
from ai_coding.domain.session import SessionData
from ai_coding.infra.time_utils import now_utc
from ai_coding.service.session_service import SessionService

DeltaSink = Callable[[str], None]


class AgentLoop:
    """Owns the single-turn conversation lifecycle and its persistence."""

    def __init__(self, ai: AIService, sessions: SessionService) -> None:
        self.ai = ai
        self.sessions = sessions

    async def process_input(
        self,
        session: SessionData,
        user_input: str,
        on_delta: DeltaSink | None = None,
    ) -> str:
        session.add_message(user_message(user_input, now_utc()))
        turn = await self.ai.execute_turn(
            session.messages, user_input, token_sink=on_delta
        )
        session.add_message(assistant_message(turn, now_utc()))
        session.ensure_tool_pairing()
        self.sessions.save(session)
        return turn.text or ""


__all__ = ["AgentLoop", "DeltaSink"]
