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


class _ThinkingOnlyEngine(ChatEngine):
    """A reasoning model that spends its budget thinking and then stops."""

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        return StepResult(
            text=None,
            reasoning="让我先确认一下 1+1 等于几。",
            finish_reason="stop",
        )


@pytest.mark.asyncio
async def test_a_step_with_no_content_at_all_is_still_an_error(tmp_path):
    """The other half: genuinely empty is still empty. Thinking out loud does
    not make a silent step a good one."""
    class _Silent(ChatEngine):
        async def step(self, messages, *, tools, instructions=None, on_delta=None):
            return StepResult(text=None, finish_reason="stop")

    loop = AgentLoop(
        _Silent(), SessionService(root=tmp_path / "sessions"), ToolRegistry(),
        settings=AIConfig(stop_policy="natural"),
        workspace=str(tmp_path),
    )
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    rec = RecordingObserver()
    await loop.process_input(session, "go", observer=rec)

    finished = [e for e in rec.events if isinstance(e, TurnFinished)]
    assert finished[0].outcome.status is TurnStatus.ERROR


class _ThinksThenAnswers(ChatEngine):
    """Stops after thinking, then answers when the turn comes back to it."""

    def __init__(self) -> None:
        self.calls = 0

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.calls += 1
        if self.calls == 1:
            return StepResult(
                text=None, reasoning="让我先想想这个问题。", finish_reason="stop"
            )
        return StepResult(text="最终答案", finish_reason="stop")


@pytest.mark.asyncio
async def test_thinking_only_gets_one_chance_to_finish_before_it_is_an_error(tmp_path):
    """M7: the industry answer, and the only one that is not OpenCode's bug.

    Measured against a real provider, the same model answered the same question
    with and without MCP connected — the step just ran out of room. Every
    framework that has looked at this agrees: retry, then fail. Cline retries
    up to three times with backoff; openclaw asks for a visible answer and
    retries; OpenHands classifies REASONING_ONLY apart from EMPTY and nudges
    the loop. Claude Code and OpenCode simply end the turn — and OpenCode's own
    tracker closes both of those reports as defects (#37372 completed,
    #48599 not_planned).

    One retry, then the error. The cap is the whole point: unbounded retry on
    an empty finish is its own failure mode (opencode#37735: 2517 requests).
    """
    engine = _ThinksThenAnswers()
    loop = AgentLoop(
        engine, SessionService(root=tmp_path / "sessions"), ToolRegistry(),
        settings=AIConfig(stop_policy="natural"),
        workspace=str(tmp_path),
    )
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    rec = RecordingObserver()
    await loop.process_input(session, "1+1 等于几？", observer=rec)

    assert engine.calls == 2, "the model was not asked to finish its answer"
    finished = [e for e in rec.events if isinstance(e, TurnFinished)]
    assert finished[0].outcome.status is TurnStatus.COMPLETED, finished[0].outcome.reason


class _AlwaysThinks(ChatEngine):
    def __init__(self) -> None:
        self.calls = 0

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.calls += 1
        return StepResult(text=None, reasoning="还是在想。", finish_reason="stop")


@pytest.mark.asyncio
async def test_a_model_that_only_thinks_stops_after_one_retry(tmp_path):
    """The other half: the retry is capped, so a model that thinks forever ends
    the turn instead of spending the operator's tokens on it."""
    engine = _AlwaysThinks()
    loop = AgentLoop(
        engine, SessionService(root=tmp_path / "sessions"), ToolRegistry(),
        settings=AIConfig(stop_policy="natural"),
        workspace=str(tmp_path),
    )
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    rec = RecordingObserver()
    await loop.process_input(session, "1+1 等于几？", observer=rec)

    assert engine.calls == 2, f"expected one retry, the model was called {engine.calls} times"
    finished = [e for e in rec.events if isinstance(e, TurnFinished)]
    assert finished[0].outcome.status is TurnStatus.ERROR
    assert "没有回答" in finished[0].outcome.reason


@pytest.mark.asyncio
async def test_a_truncated_thinking_run_is_not_retried_as_a_thoughtful_one(tmp_path):
    """``finish_reason: length`` is a different failure — the answer ran out of
    budget, not the model out of things to say. Anthropic's own guidance is to
    treat it as failed and retry with a bigger budget; retrying it here with the
    same budget would only reproduce the truncation, so it stays an error."""
    class _Truncated(ChatEngine):
        def __init__(self) -> None:
            self.calls = 0

        async def step(self, messages, *, tools, instructions=None, on_delta=None):
            self.calls += 1
            return StepResult(text=None, reasoning="想了很久", finish_reason="length")

    engine = _Truncated()
    loop = AgentLoop(
        engine, SessionService(root=tmp_path / "sessions"), ToolRegistry(),
        settings=AIConfig(stop_policy="natural"),
        workspace=str(tmp_path),
    )
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    rec = RecordingObserver()
    await loop.process_input(session, "写一篇长文", observer=rec)

    assert engine.calls == 1
    finished = [e for e in rec.events if isinstance(e, TurnFinished)]
    assert finished[0].outcome.status is TurnStatus.ERROR
