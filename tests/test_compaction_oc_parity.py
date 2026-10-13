"""TDD (P3-2 refactor): OpenCode-parity compaction — ceiling, forced L4,
token-budgeted tail, honest funnel.

Locks the OC rules adopted in the compact-layer refactor:
1. ``compaction_ceiling``: window - max(10%, 16k when window >= 32k).
2. ``force`` reaches L4: manual/overflow always summarizes (empty input
   excepted — no content, no summary call).
3. Tail is token-budgeted (min(tail_tokens, window//4)), backtracked to a
   user boundary, never empty.
4. The funnel reports content truth: unchanged content returns the original
   object and emits Failed (with whatever usage streamed), never a
   phantom 29->29 Ended.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ai_coding.core.compaction import Compactor
from ai_coding.core.context_compression import (
    ContextManager,
    compaction_ceiling,
    estimate_tokens,
)
from ai_coding.core.events import (
    CompactionEnded,
    CompactionFailed,
    CompactionSkipped,
    CompactionStarted,
    RecordingObserver,
)
from ai_coding.domain.message import ChatMessage


def _settings(**overrides):
    base = {
        "max_context_tokens": 256_000,
        "max_messages": 400,
        "snip_keep_head": 3,
        "snip_keep_tail": 20,
        "keep_recent_tool_results": 3,
        "per_result_persist_bytes": 30_000,
        "l4_tail_tokens": 15_000,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _msgs(n: int, chars: int = 50) -> list[ChatMessage]:
    out = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        out.append(ChatMessage(role=role, content=f"m{i}:" + "x" * chars, timestamp=""))
    return out


# ---- 1. ceiling ------------------------------------------------------------


@pytest.mark.parametrize(
    ("window", "expected"),
    [
        (256_000, 230_400),
        (200_000, 180_000),
        (48_000, 32_000),
        (32_000, 16_000),
        (31_999, 28_800),  # below 32k: straight 10%, no 16k floor
        (10_000, 9_000),
        (400, 360),
    ],
)
def test_ceiling_leaves_a_reserve(window: int, expected: int) -> None:
    assert compaction_ceiling(window) == expected


def test_should_compact_fires_at_the_ceiling(tmp_path) -> None:
    manager = ContextManager(_settings(max_context_tokens=400), transcripts_root=tmp_path)

    def sized(tokens: int) -> list[ChatMessage]:
        return [ChatMessage(role="user", content="a" * (tokens * 4), timestamp="")]

    assert manager.should_compact(sized(300)) is False  # under 360
    assert manager.should_compact(sized(400)) is True  # over 360


# ---- 2. force reaches L4 ----------------------------------------------------


@pytest.mark.asyncio
async def test_force_summarises_a_small_history(tmp_path) -> None:
    """The screenshot defect: 29 short messages vs a 256k window never
    reached the summarizer, so /compact was a billed no-op."""
    seen: list[str] = []

    async def summarizer(text, previous=None, on_delta=None, usage_sink=None):
        seen.append(text)
        return "## 目标\n小会话摘要"

    manager = ContextManager(_settings(), transcripts_root=tmp_path, summarizer=summarizer)
    history = _msgs(8)

    out = await manager.compress_if_needed(history, force=True)

    assert len(seen) == 1
    assert out is not history
    assert "小会话摘要" in (out[0].content or "")


@pytest.mark.asyncio
async def test_force_with_no_content_makes_no_summary_call(tmp_path) -> None:
    """A second /compact with nothing new must not burn two model calls
    on an empty prompt."""
    calls: list[str] = []

    async def summarizer(text, previous=None, on_delta=None, usage_sink=None):
        calls.append(text)
        return "## 目标\n摘要"

    manager = ContextManager(_settings(), transcripts_root=tmp_path, summarizer=summarizer)

    await manager.compress_if_needed([], force=True)

    assert calls == []


# ---- 3. token-budgeted tail --------------------------------------------------


def test_tail_is_budgeted_not_counted(tmp_path) -> None:
    manager = ContextManager(
        _settings(max_context_tokens=4_000, l4_tail_tokens=200), transcripts_root=tmp_path
    )
    history = _msgs(20, chars=200)  # ~50 tokens each

    head, tail = manager.split_tail(history)

    assert len(tail) >= 1, "the tail never vanishes"
    assert tail[0].role == "user", "question-answer pairs stay whole"
    assert [m.content for m in tail] == [(history[18].content), (history[19].content)]
    assert estimate_tokens("\n".join(m.content or "" for m in tail)) <= 200
    assert len(head) + len(tail) == len(history)


def test_tail_keeps_at_least_one_message_when_the_budget_is_tiny(tmp_path) -> None:
    manager = ContextManager(
        _settings(max_context_tokens=5, l4_tail_tokens=15_000), transcripts_root=tmp_path
    )
    history = _msgs(4)

    _, tail = manager.split_tail(history)

    assert len(tail) == 1
    assert tail[0] is history[-1]


# ---- 4. honest funnel ---------------------------------------------------------


def _compactor(context=None):
    observer = RecordingObserver()
    return Compactor(context, emit=observer.on_event), observer


class _SilentContext:
    """A no-op delegate with no attempt bookkeeping at all (legacy injection):
    content comes back equal and, lacking ``last_summary_attempted``, the
    funnel reads the documented default — a skip, not a failure."""

    last_summary_usage: dict | None = None

    async def compress_if_needed(  # type: ignore[no-untyped-def]
        self, history, **_: object
    ):
        return list(history)


@pytest.mark.asyncio
async def test_unchanged_content_with_no_summary_attempt_is_skipped() -> None:
    """Semantics decided 2026-10-14: a no-op where nothing was even worth
    summarising (single exchange / nothing since the checkpoint) is not a
    failure — it is a skip, rendered muted.  The original list identity is
    preserved so every downstream did_compact judge agrees."""
    compactor, observer = _compactor(_SilentContext())
    history = _msgs(8)

    out = await compactor.compress(history, deadline=None, reason="manual", force=True)

    assert out is history
    assert observer.of_type(CompactionStarted)
    assert observer.of_type(CompactionEnded) == []
    assert observer.of_type(CompactionFailed) == []
    skipped = observer.of_type(CompactionSkipped)
    assert len(skipped) == 1 and skipped[0].reason == "manual"


@pytest.mark.asyncio
async def test_a_single_exchange_compaction_skips_without_billing(tmp_path) -> None:
    """The tmux repro: /compact on one exchange must not call the model and
    must report the skip (muted), not a red failure."""
    calls: list[str] = []

    async def counting(text, previous=None, on_delta=None, usage_sink=None):
        calls.append(text)
        return "## 目标\n摘要"

    manager = ContextManager(_settings(), transcripts_root=tmp_path, summarizer=counting)
    compactor, observer = _compactor(manager)
    history = [ChatMessage(role="user", content="你好", timestamp=""),
               ChatMessage(role="assistant", content="你好！", timestamp="")]

    out = await compactor.compress(history, deadline=None, reason="manual", force=True)

    assert calls == []
    assert out is history
    assert len(observer.of_type(CompactionSkipped)) == 1
    assert observer.of_type(CompactionFailed) == []


@pytest.mark.asyncio
async def test_concurrent_compactions_run_the_summarizer_once(tmp_path) -> None:
    """Two /compact at once (RPC): the lock serialises them, and the second
    finds nothing new — one model call, two honest reports."""
    import asyncio

    calls: list[str] = []

    async def slow(text, previous=None, on_delta=None, usage_sink=None):
        calls.append(text)
        await asyncio.sleep(0.05)
        return "## 目标\n摘要"

    manager = ContextManager(_settings(), transcripts_root=tmp_path, summarizer=slow)
    compactor, _observer = _compactor(manager)

    class _Session:
        def __init__(self, messages) -> None:  # type: ignore[no-untyped-def]
            self.messages = list(messages)
            self.turn_count = 1

    session = _Session(_msgs(8))
    first, second = await asyncio.gather(
        compactor.compact_now(session, timeout=5.0),
        compactor.compact_now(session, timeout=5.0),
    )

    assert len(calls) == 1, "the second compaction must not pay for another summary"
    reports = sorted((r["removed"] for r in (first, second)), reverse=True)
    assert reports[0] > 0, "one of the two did the work"
    assert reports[1] == 0, "the other found nothing new"


@pytest.mark.asyncio
async def test_failed_carries_whatever_usage_streamed(tmp_path) -> None:
    async def streaming_then_bad_shape(text, previous=None, on_delta=None, usage_sink=None):
        # Faithful to build_summarizer: usage streams per attempt, but a
        # shape rejection resolves to None — _run_l4 trusts its summarizer
        # to have self-checked (the only production injection does).
        if usage_sink is not None:
            usage_sink.append({"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12})
        return None

    manager = ContextManager(_settings(), transcripts_root=tmp_path,
                             summarizer=streaming_then_bad_shape)
    compactor, observer = _compactor(manager)

    out = await compactor.compress(_msgs(8), deadline=None, reason="manual", force=True)

    failed = observer.of_type(CompactionFailed)
    assert len(failed) == 1
    assert (failed[0].prompt_tokens, failed[0].total_tokens) == (9, 12)
    assert out is not None
