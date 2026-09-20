"""TDD (M1-3): AgentLoop — one turn of history->AI->rewrite->persist, offline via FakeAIService."""

import pytest

from ai_coding.ai.base import AIService
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.domain.run import TurnResult
from ai_coding.service.session_service import SessionService


class FakeAIService(AIService):
    """Deterministic stub capturing the history it received."""

    def __init__(self, reply: str = "hello") -> None:
        self.reply = reply
        self.last_history: list | None = None
        self.seen_deltas: list[str] = []

    async def execute_turn(self, history, user_input, token_sink=None):
        self.last_history = list(history)
        if token_sink:
            token_sink("he")
            token_sink("llo")
        return TurnResult(text=self.reply)


@pytest.mark.asyncio
async def test_process_input_appends_and_persists(tmp_path):
    ai = FakeAIService(reply="world")
    svc = SessionService(tmp_path / "sessions")
    loop = AgentLoop(ai, svc)
    session = svc.create(title="t")

    text = await loop.process_input(session, "hi")

    assert text == "world"
    # session has user + assistant
    roles = [m.role for m in session.messages]
    assert roles == ["user", "assistant"]
    assert session.messages[0].content == "hi"
    assert session.messages[1].content == "world"
    # persisted to disk
    persisted = svc.load(session.session_id)
    assert persisted is not None and len(persisted.messages) == 2


@pytest.mark.asyncio
async def test_returns_final_text(tmp_path):
    ai = FakeAIService(reply="final")
    loop = AgentLoop(ai, SessionService(tmp_path / "sessions"))
    out = await loop.process_input(svc_create(loop), "hi")
    assert out == "final"


@pytest.mark.asyncio
async def test_delta_callback_called(tmp_path):
    ai = FakeAIService()
    loop = AgentLoop(ai, SessionService(tmp_path / "sessions"))
    deltas: list[str] = []
    await loop.process_input(svc_create(loop), "hi", on_delta=deltas.append)
    assert deltas == ["he", "llo"]


@pytest.mark.asyncio
async def test_history_includes_user(tmp_path):
    ai = FakeAIService()
    loop = AgentLoop(ai, SessionService(tmp_path / "sessions"))
    await loop.process_input(svc_create(loop), "ask-me")
    assert len(ai.last_history) == 1
    assert ai.last_history[0].role == "user"
    assert ai.last_history[0].content == "ask-me"


def svc_create(loop: AgentLoop):
    return loop.sessions.create()
