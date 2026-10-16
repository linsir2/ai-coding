"""G5-1: a provider that rejects the context gets one compaction, not a dead turn.

This is the only failure in the system where the operator has already paid the
full cost of the turn and would lose all of it: the history is too long for the
provider, and until now the loop treated that as fatal -- it never compacted,
even though compaction is exactly what the situation calls for.

The tests drive a real ``OpenAIChatEngine`` over a fake client, so the error the
loop reacts to is the one the engine really raises rather than one this file
invented.  Only the HTTP layer is faked.
"""

from __future__ import annotations

import pytest

from ai_coding.ai.openai_engine import OpenAIChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.context_compression import ContextManager
from ai_coding.core.events import CompactionStarted, Degraded, RecordingObserver
from ai_coding.domain.model_config import ModelConfig
from ai_coding.domain.session import SessionData
from ai_coding.service.session_service import SessionService
from ai_coding.tools.registry import ToolRegistry
from tests.test_openai_engine import (
    _Resp,
    _SequencedClient,
    _text_chunk,
)


def _overflow_error():
    """What a provider returns when the prompt does not fit."""
    import openai

    return openai.BadRequestError(
        "This model's maximum context length is 65536 tokens. "
        "However, you requested 70000 tokens",
        response=_Resp(400),
        body=None,
    )


def _bad_request_error():
    """A 400 that compaction cannot fix: the request itself is unacceptable."""
    import openai

    return openai.BadRequestError("invalid tool schema", response=_Resp(400), body=None)


class _CountingEngine(OpenAIChatEngine):
    """A real engine that records the history handed to each attempt."""

    def __init__(self, client) -> None:
        super().__init__(
            ModelConfig(name="m", base_url="http://x", api_key="k", retry_base_seconds=0.0),
            client=client,
        )
        self.histories: list[list] = []

    async def step(self, messages, **kwargs):
        self.histories.append(list(messages))
        return await super().step(messages, **kwargs)


def _settings(**over) -> AIConfig:
    """A budget the history sits *under*, which is the situation that fails.

    The preventive path only compacts above the soft water mark, so if it had
    already fired there would be nothing left for the reactive path to do -- and
    the test would pass against a loop that never reacted at all.  The real
    failure is the one this models: the estimate said there was room and the
    provider disagreed (measured: the estimate runs 1.36x low, §G5-6).
    """
    base = {
        "stop_policy": "natural",
        "max_context_tokens": 2000,
        "max_messages": 4,
        "snip_keep_head": 1,
        "snip_keep_tail": 1,
        "compress_target_ratio": 0.5,
    }
    base.update(over)
    return AIConfig(**base)


def _session_with_history(sessions: SessionService, pairs: int = 8) -> SessionData:
    """A history long enough that compaction has something to do."""
    from ai_coding.domain.message import ChatMessage

    session = sessions.create()
    for i in range(pairs):
        session.add_message(
            ChatMessage(
                role="user",
                content=f"第 {i} 轮的问题，内容足够长以便触发压缩水位线" * 3,
                timestamp="t",
            )
        )
        session.add_message(
            ChatMessage(
                role="assistant",
                content=f"第 {i} 轮的回答，同样足够长以便触发压缩水位线" * 3,
                timestamp="t",
            )
        )
    return session


def _loop(tmp_path, engine, *, settings=None, context="default", observer=None) -> AgentLoop:
    settings = settings or _settings()
    if context == "default":
        context = ContextManager(settings, transcripts_root=tmp_path)
    return AgentLoop(
        engine,
        SessionService(tmp_path / "sessions"),
        ToolRegistry(),
        context=context,
        settings=settings,
        workspace=str(tmp_path),
        observer=observer,
    )


@pytest.mark.asyncio
async def test_a_rejected_context_is_compacted_and_the_step_retried(tmp_path):
    """The turn survives: the provider said "too long", so the loop makes it
    shorter and asks again instead of throwing the turn away."""
    client = _SequencedClient([_overflow_error(), [_text_chunk("好了", finish_reason="stop")]])
    engine = _CountingEngine(client)
    loop = _loop(tmp_path, engine)
    sessions = SessionService(tmp_path / "sessions")
    session = _session_with_history(sessions)

    assert await loop.process_input(session, "继续") == "好了"
    assert len(engine.histories) == 2, "the step was not retried after compacting"


@pytest.mark.asyncio
async def test_the_retry_asks_with_the_compacted_history(tmp_path):
    """Retrying with the same messages would earn the same rejection; the whole
    point is that the second request is smaller than the first."""
    client = _SequencedClient([_overflow_error(), [_text_chunk("好了", finish_reason="stop")]])
    engine = _CountingEngine(client)
    loop = _loop(tmp_path, engine)
    sessions = SessionService(tmp_path / "sessions")
    session = _session_with_history(sessions)

    await loop.process_input(session, "继续")

    first, second = engine.histories
    assert len(second) < len(first), f"{len(second)} !< {len(first)}"


@pytest.mark.asyncio
async def test_the_compacted_history_is_written_back_to_the_session(tmp_path):
    """A one-shot retry would leave the session oversized, so the *next*
    iteration and every later turn would be rejected again."""
    client = _SequencedClient([_overflow_error(), [_text_chunk("好了", finish_reason="stop")]])
    engine = _CountingEngine(client)
    loop = _loop(tmp_path, engine)
    sessions = SessionService(tmp_path / "sessions")
    session = _session_with_history(sessions)

    await loop.process_input(session, "继续")

    # The turn appends its own user turn either way, so length alone proves
    # nothing -- what proves it is that the middle of the old history is gone.
    # (The head is kept by design: ``snip_keep_head`` is 1.)
    kept = [m for m in session.messages if "第 3 轮的问题" in (m.content or "")]
    assert not kept, "the pre-overflow history was not compacted away"


@pytest.mark.asyncio
async def test_it_attempts_at_most_twice_per_turn(tmp_path):
    """11-spec §7.3 (sanctioned rewrite): the wall is two rescue passes, not
    one.  Here the first pass shrinks and the retry is issued; the second
    pass finds nothing left to remove, so ``removed == 0`` ends the turn
    honestly -- two histories, not a third attempt at a summary of a
    summary.  (The companion pin in test_overflow_safety drives a trimmer
    that always shrinks and shows the third rejection is the truth.)"""
    client = _SequencedClient([_overflow_error(), _overflow_error(), _overflow_error()])
    engine = _CountingEngine(client)
    loop = _loop(tmp_path, engine)
    sessions = SessionService(tmp_path / "sessions")
    session = _session_with_history(sessions)

    # The loop never raises for a model failure: it ends the turn with an ERROR
    # outcome and returns the text the operator sees.
    text = await loop.process_input(session, "继续")

    assert "错误" in text or "error" in text.lower(), text
    assert len(engine.histories) == 2, f"compacted more than twice: {len(engine.histories)} calls"


@pytest.mark.asyncio
async def test_a_bad_request_that_is_not_an_overflow_is_left_alone(tmp_path):
    """A 400 about a malformed request is not fixed by shrinking the history,
    and rewriting it would be pure loss."""
    client = _SequencedClient([_bad_request_error()])
    engine = _CountingEngine(client)
    loop = _loop(tmp_path, engine)
    sessions = SessionService(tmp_path / "sessions")
    session = _session_with_history(sessions)

    text = await loop.process_input(session, "继续")

    assert "错误" in text or "error" in text.lower(), text
    assert len(engine.histories) == 1
    kept = [m for m in session.messages if "第 0 轮的问题" in (m.content or "")]
    assert kept, "the history was rewritten for a failure compaction cannot fix"


@pytest.mark.asyncio
async def test_nothing_to_compact_means_no_retry(tmp_path):
    """With no compressor wired there is nothing to try, and the original error
    is the truth -- the loop must not pretend it recovered."""
    client = _SequencedClient([_overflow_error()])
    engine = _CountingEngine(client)
    loop = _loop(tmp_path, engine, context=None)
    sessions = SessionService(tmp_path / "sessions")
    session = _session_with_history(sessions)

    text = await loop.process_input(session, "继续")

    assert "错误" in text or "error" in text.lower(), text
    assert len(engine.histories) == 1


@pytest.mark.asyncio
async def test_the_recovery_is_visible_in_the_trace(tmp_path):
    """The operator's context was rewritten without them asking.  It has to be
    discoverable afterwards, which means an event, not a silent success."""
    client = _SequencedClient([_overflow_error(), [_text_chunk("好了", finish_reason="stop")]])
    engine = _CountingEngine(client)
    observer = RecordingObserver()
    loop = _loop(tmp_path, engine, observer=observer)
    sessions = SessionService(tmp_path / "sessions")
    session = _session_with_history(sessions)

    await loop.process_input(session, "继续")

    degraded = list(observer.of_type(Degraded))
    assert any("too long" in e.reason or "context" in e.reason.lower() for e in degraded), degraded


@pytest.mark.asyncio
async def test_the_reactive_compaction_is_reported_as_overflow(tmp_path):
    """P3-2: the reactive path must say why it compacted.  The budget path
    passes no reason (funnel defaults to auto); only the provider-rejection
    path says overflow."""
    client = _SequencedClient([_overflow_error(), [_text_chunk("好了", finish_reason="stop")]])
    engine = _CountingEngine(client)
    observer = RecordingObserver()
    loop = _loop(tmp_path, engine, observer=observer)
    sessions = SessionService(tmp_path / "sessions")
    session = _session_with_history(sessions)

    await loop.process_input(session, "继续")

    started = list(observer.of_type(CompactionStarted))
    assert [s.reason for s in started] == ["overflow"]
