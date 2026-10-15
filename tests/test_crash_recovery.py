"""Crash recovery (补充式方案 06, P2-P4, TDD).

The existing session layer is a sound snapshot; it was missing three facts that
only matter when the process dies: whether a turn was open, whether an approval
was pending, and what ``/undo`` removed.  These tests pin the facts and the
behaviour that reads them -- each written Red before the implementation.
"""

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.domain.loop import StepResult
from ai_coding.domain.message import ToolCallRef
from ai_coding.domain.session import SessionData
from ai_coding.service.session_service import SessionService
from ai_coding.tools.registry import ToolRegistry


def _session(session_id: str = "s") -> SessionData:
    return SessionData(
        session_id=session_id, title="", created_time="t", last_access_time="t"
    )


class _ProbeEngine(ChatEngine):
    """Asks for one tool call, then probes the session file on the next step.

    That second step is the first moment a checkpoint has run, which is when
    the open-turn marker is expected to be on disk -- the design rides the
    existing save rather than adding one, so a crash before any checkpoint has
    no output to lose either.
    """

    def __init__(self, service: SessionService, session_id: str) -> None:
        self._service = service
        self._session_id = session_id
        self.calls = 0
        self.open_turn_on_second_step: int | None = -1

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.calls += 1
        if self.calls == 1:
            return StepResult(
                text=None,
                tool_calls=[ToolCallRef(id="c1", name="read", arguments='{"path":"x"}')],
                finish_reason="tool_calls",
            )
        loaded = self._service.load(self._session_id)
        self.open_turn_on_second_step = None if loaded is None else loaded.open_turn
        return StepResult(text="done", finish_reason="stop")


def _loop(engine, service: SessionService, workspace) -> AgentLoop:
    return AgentLoop(
        engine,
        service,
        ToolRegistry(),
        settings=AIConfig(stop_policy="natural", turn_timeout_seconds=0),
        workspace=str(workspace),
    )


@pytest.mark.asyncio
async def test_open_turn_is_on_disk_after_the_first_checkpoint_and_gone_after(tmp_path):
    """The marker has to reach the file *during* the turn, or a crash between
    the last checkpoint and the close would go undetected -- and it must be
    gone once the turn closes, or every later load would cry crash."""
    service = SessionService(root=tmp_path / "sessions")
    probe = _ProbeEngine(service, "s")
    loop = _loop(probe, service, tmp_path)

    session = _session()
    await loop.process_input(session, "go")

    assert probe.open_turn_on_second_step == 1, "the open turn was not persisted mid-turn"
    assert session.open_turn is None, "the in-memory turn stayed open after close"
    loaded = service.load("s")
    assert loaded is not None and loaded.open_turn is None, "the file still says open"


def test_open_turn_on_load_is_reported_as_recovered(tmp_path):
    """A file that says a turn is open is the crash signal; ``load`` derives
    the one-shot notice from it, so the server can say so without guessing."""
    service = SessionService(root=tmp_path / "sessions")
    crashed = _session("crashed")
    crashed.open_turn = 4
    service.save(crashed)

    loaded = service.load("crashed")
    assert loaded is not None
    assert loaded.open_turn == 4
    assert loaded.recovered_interrupted is True

    clean = _session("clean")
    service.save(clean)
    fresh = service.load("clean")
    assert fresh is not None and fresh.recovered_interrupted is False
