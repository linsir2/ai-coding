"""Per-step time accounting (R0-4, TDD): abandoned work is counted, not silent.

On a hard task the binding scarcity is wall-clock, not iterations: a model
step abandoned at the deadline currently leaves no event and no books entry
(H2 recorded a 179s step vanishing).  The transcript keeps the streamed
pixels; the books must keep the count.
"""

import asyncio

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.events import Degraded, RecordingObserver, TurnFinished
from ai_coding.core.turn_books import TurnBooks
from ai_coding.domain.loop import StepResult, TurnStatus
from ai_coding.domain.session import SessionData
from ai_coding.service.session_service import SessionService
from ai_coding.tools.registry import ToolRegistry


def test_books_records_step_latencies_and_abandonments():
    books = TurnBooks()
    books.note_step("a", latency_ms=120)
    books.note_step("b", latency_ms=80)
    books.note_abandoned(latency_ms=179000)
    assert books.step_latencies_ms == [120, 80, 179000]
    assert books.abandoned_steps == 1
    assert books.last_step_latency_ms == 179000
    # Old call style keeps working (kwarg defaults).
    books.note_step("c")
    assert books.step_latencies_ms[-1] == 0


class _SlowEngine(ChatEngine):
    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        await asyncio.sleep(30)
        return StepResult(text="never", finish_reason="stop")


@pytest.mark.asyncio
async def test_abandoned_model_step_is_recorded_and_the_verdict_stands(tmp_path):
    """A step cut by the clock must leave a Degraded trace; the turn still
    ends TIMEOUT exactly as before (accounting, not behaviour change)."""
    loop = AgentLoop(
        _SlowEngine(), SessionService(root=tmp_path / "sessions"), ToolRegistry(),
        settings=AIConfig(stop_policy="natural", turn_timeout_seconds=0.05),
        workspace=str(tmp_path),
    )
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    rec = RecordingObserver()
    await loop.process_input(session, "go", observer=rec)

    degraded = [e for e in rec.events if isinstance(e, Degraded)]
    assert len(degraded) == 1
    assert degraded[0].component == "model_step"
    finished = [e for e in rec.events if isinstance(e, TurnFinished)]
    assert finished and finished[0].outcome.status is TurnStatus.TIMEOUT
