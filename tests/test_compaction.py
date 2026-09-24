"""TDD (R5): compression and the explicit ``/compact`` in one unit.

Two methods used to wrap the same dependency in the same deadline pattern with
the same degradation text.  The interesting parts are the duck-typing (an older
context object only implements ``compress``) and the rule that a *failed*
compaction never rewrites the caller's history (docs/r5-analysis.md 3.3).
"""

import pytest

from ai_coding.core.compaction import Compactor
from ai_coding.core.events import Degraded, RecordingObserver
from ai_coding.domain.message import ChatMessage
from ai_coding.infra.deadline import DeadlineExpired


class _ModernContext:
    """The R3b shape: compress only when the budget says so."""

    def __init__(self) -> None:
        self.calls: list[list[ChatMessage]] = []
        self.forced: list[bool] = []

    async def compress_if_needed(self, history, *, force=False, on_summary=None):  # type: ignore[no-untyped-def]
        self.calls.append(list(history))
        self.forced.append(force)
        if on_summary is not None:
            on_summary("a page summary")
        return [ChatMessage(role="user", content="compacted", timestamp="")]


class _LegacyContext:
    """The pre-R3b shape: only ``compress`` exists."""

    def __init__(self) -> None:
        self.calls = 0

    async def compress(self, history):  # type: ignore[no-untyped-def]
        self.calls += 1
        return [ChatMessage(role="user", content="legacy", timestamp="")]


class _TimingOutContext:
    async def compress_if_needed(self, history, *, force=False, on_summary=None):  # type: ignore[no-untyped-def]
        raise DeadlineExpired("context_compress", 0.01)


class _BrokenContext:
    async def compress_if_needed(self, history, *, force=False, on_summary=None):  # type: ignore[no-untyped-def]
        raise RuntimeError("compressor exploded")


class _Session:
    def __init__(self, messages) -> None:  # type: ignore[no-untyped-def]
        self.messages = list(messages)


def _compactor(context=None):
    observer = RecordingObserver()
    return Compactor(context, emit=observer.on_event), observer


def _history() -> list[ChatMessage]:
    return [ChatMessage(role="user", content="hello", timestamp="")]


@pytest.mark.asyncio
async def test_compress_prefers_the_budget_aware_entry_point():
    context = _ModernContext()
    compactor, _ = _compactor(context)
    summaries: list[str] = []

    out = await compactor.compress(_history(), deadline=None, on_summary=summaries.append)

    assert [m.content for m in out] == ["compacted"]
    assert summaries == ["a page summary"]
    assert len(context.calls) == 1


@pytest.mark.asyncio
async def test_compress_still_works_with_a_legacy_context_object():
    context = _LegacyContext()
    compactor, _ = _compactor(context)

    out = await compactor.compress(_history(), deadline=None)

    assert [m.content for m in out] == ["legacy"]
    assert context.calls == 1


@pytest.mark.asyncio
async def test_without_a_context_the_history_passes_through_untouched():
    compactor, observer = _compactor(None)
    history = _history()
    assert await compactor.compress(history, deadline=None) is history
    assert observer.events == []


@pytest.mark.asyncio
async def test_a_timed_out_compression_falls_back_and_says_so():
    compactor, observer = _compactor(_TimingOutContext())
    history = _history()

    out = await compactor.compress(history, deadline=None)

    assert out is history
    assert [e.component for e in observer.of_type(Degraded)] == ["context_compress"]


@pytest.mark.asyncio
async def test_an_unexpected_compression_failure_falls_back_and_says_so():
    """The loop must not die because the compressor did."""
    compactor, observer = _compactor(_BrokenContext())
    history = _history()

    out = await compactor.compress(history, deadline=None)

    assert out is history
    assert [e.component for e in observer.of_type(Degraded)] == ["context_compress"]


@pytest.mark.asyncio
async def test_compact_now_replaces_the_stored_history_and_reports_it():
    context = _ModernContext()
    compactor, _ = _compactor(context)
    session = _Session(_history() * 4)

    report = await compactor.compact_now(session, timeout=5.0)

    assert report == {"messages": 4, "after": 1, "removed": 3}
    assert [m.content for m in session.messages] == ["compacted"]
    assert context.forced == [True], "the explicit path must force a page"


@pytest.mark.asyncio
async def test_compact_now_leaves_the_session_alone_when_it_fails():
    compactor, _ = _compactor(_BrokenContext())
    session = _Session(_history() * 3)
    before = list(session.messages)

    report = await compactor.compact_now(session, timeout=5.0)

    assert report == {"messages": 3, "after": 3, "removed": 0}
    assert session.messages == before


@pytest.mark.asyncio
async def test_compact_now_is_a_no_op_for_an_empty_session_or_no_context():
    compactor, _ = _compactor(_ModernContext())
    empty = _Session([])
    assert await compactor.compact_now(empty, timeout=5.0) == {
        "messages": 0,
        "after": 0,
        "removed": 0,
    }

    contextless, _ = _compactor(None)
    session = _Session(_history())
    assert await contextless.compact_now(session, timeout=5.0) == {
        "messages": 1,
        "after": 1,
        "removed": 0,
    }
