"""TDD (P3-2): four compaction lifecycle events + wire mapping.

OC parity: started(durable) / delta(ephemeral) / ended(durable, with the
compaction request's own usage) / failed(durable).  ContextCompacted stays:
it is "history was rewritten", ended is "the summary job finished".
"""

from __future__ import annotations

import pytest

from ai_coding.core.compaction import Compactor
from ai_coding.core.events import (
    CompactionDelta,
    CompactionEnded,
    CompactionFailed,
    CompactionSkipped,
    CompactionStarted,
    Degraded,
    LoopEvent,
    RecordingObserver,
)
from ai_coding.domain.message import ChatMessage, ToolCallRef
from ai_coding.server.bridge import to_wire


def test_started_carries_reason() -> None:
    for reason in ("auto", "manual", "overflow"):
        wire = to_wire(CompactionStarted(reason=reason))  # type: ignore[arg-type]
        assert wire == {"type": "compaction.started", "data": {"reason": reason}}


def test_delta_carries_text_chunk() -> None:
    wire = to_wire(CompactionDelta(text="## 目标\n"))
    assert wire == {"type": "compaction.delta", "data": {"text": "## 目标\n"}}


def test_ended_carries_counts_and_own_usage() -> None:
    wire = to_wire(
        CompactionEnded(
            reason="auto",
            before_messages=74,
            after_messages=3,
            prompt_tokens=1312,
            completion_tokens=147,
            total_tokens=1896,
            text="## 目标\ndone",
        )
    )
    # Subset, not the whole dict: the per-layer account (``layers``) and the
    # boundary estimates ride on the same event, so an exact-equality assertion
    # would break on every additive field (plan-m-e §四 #4d).
    assert wire is not None
    assert wire["type"] == "compaction.ended"
    data = wire["data"]
    assert data["reason"] == "auto"
    assert data["beforeMessages"] == 74
    assert data["afterMessages"] == 3
    assert data["inputTokens"] == 1312
    assert data["outputTokens"] == 147
    assert data["totalTokens"] == 1896
    assert data["text"] == "## 目标\ndone"
    # The new fields are always present, empty/zero when a delegate reported none.
    assert data["layers"] == []
    assert data["estimatedBefore"] == 0
    assert data["estimatedAfter"] == 0


def test_ended_wire_carries_the_layer_account() -> None:
    """The wire mapping is hand-written, so it is pinned: the front end reads
    camelCase (``beforeTokens``), the trace reads snake_case (``before_tokens``)."""
    from ai_coding.core.events import LayerStat

    wire = to_wire(
        CompactionEnded(
            reason="auto",
            before_messages=10,
            after_messages=3,
            layers=(
                LayerStat(
                    name="L3", removed=0, rewritten=1, skipped=2,
                    before_tokens=90_000, after_tokens=80_000,
                ),
                LayerStat(
                    name="L1", removed=7, rewritten=0, skipped=0,
                    before_tokens=80_000, after_tokens=40_000,
                ),
            ),
            estimated_before=90_000,
            estimated_after=40_000,
        )
    )
    data = wire["data"]
    assert data["layers"] == [
        {
            "name": "L3", "removed": 0, "rewritten": 1, "skipped": 2,
            "beforeTokens": 90_000, "afterTokens": 80_000,
        },
        {
            "name": "L1", "removed": 7, "rewritten": 0, "skipped": 0,
            "beforeTokens": 80_000, "afterTokens": 40_000,
        },
    ]
    assert data["estimatedBefore"] == 90_000
    assert data["estimatedAfter"] == 40_000


def test_failed_carries_reason_and_error() -> None:
    wire = to_wire(CompactionFailed(reason="manual", error="boom"))
    assert wire == {
        "type": "compaction.failed",
        "data": {
            "reason": "manual",
            "error": "boom",
            "inputTokens": 0,
            "outputTokens": 0,
            "totalTokens": 0,
        },
    }


def test_skipped_carries_reason() -> None:
    wire = to_wire(CompactionSkipped(reason="manual"))
    assert wire == {"type": "compaction.skipped", "data": {"reason": "manual"}}


def test_five_events_are_loop_events() -> None:
    events: list[LoopEvent] = [
        CompactionStarted(reason="auto"),  # type: ignore[arg-type]
        CompactionDelta(text="x"),
        CompactionEnded(
            reason="auto",
            before_messages=1,
            after_messages=1,
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
        ),
        CompactionFailed(reason="auto", error="e"),
        CompactionSkipped(reason="auto"),  # type: ignore[arg-type]
    ]
    assert len(events) == 5


# ---- Compactor funnel: reason + Started/Ended/Failed + delta ----------------


def _history(n: int = 3) -> list[ChatMessage]:
    return [ChatMessage(role="user", content=f"m{i}", timestamp="") for i in range(n)]


class _OkContext:
    def __init__(self, usage: dict | None = None) -> None:
        self.seen_reasons: list[str] = []
        self.usage = usage or {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}
        self.last_summary_usage: dict | None = dict(self.usage)

    async def compress_if_needed(  # type: ignore[no-untyped-def]
        self,
        history,
        *,
        force=False,
        on_summary=None,
        on_delta=None,
        observed_tokens=0,
        reason="auto",
    ):
        self.seen_reasons.append(reason)
        if on_delta is not None:
            on_delta("## 目标\n")
        if on_summary is not None:
            on_summary("## 目标\n done")
        return [ChatMessage(role="user", content="compacted", timestamp="")]


def _compactor(context=None):
    observer = RecordingObserver()
    return Compactor(context, emit=observer.on_event), observer


@pytest.mark.asyncio
async def test_funnel_emits_started_then_ended_with_reason_and_usage() -> None:
    ctx = _OkContext()
    compactor, observer = _compactor(ctx)
    summaries: list[str] = []

    out = await compactor.compress(
        _history(), deadline=None, reason="manual", on_summary=summaries.append
    )

    assert [m.content for m in out] == ["compacted"]
    started = observer.of_type(CompactionStarted)
    ended = observer.of_type(CompactionEnded)
    assert [s.reason for s in started] == ["manual"]
    assert len(ended) == 1
    assert (ended[0].before_messages, ended[0].after_messages) == (3, 1)
    assert ended[0].total_tokens == 6
    assert ctx.seen_reasons == ["manual"]
    # The caller's on_summary still fires (books.summaries), and Ended
    # carries the same body for the transcript row.
    assert summaries == ["## 目标\n done"]
    assert ended[0].text == "## 目标\n done"


@pytest.mark.asyncio
async def test_funnel_forwards_summary_deltas() -> None:
    ctx = _OkContext()
    compactor, observer = _compactor(ctx)

    await compactor.compress(_history(), deadline=None, reason="auto")

    assert [d.text for d in observer.of_type(CompactionDelta)] == ["## 目标\n"]


@pytest.mark.asyncio
async def test_funnel_failure_emits_failed_plus_degraded_and_keeps_history() -> None:
    class _Broken:
        async def compress_if_needed(self, history, **_: object):  # type: ignore[no-untyped-def]
            raise RuntimeError("compressor exploded")

    compactor, observer = _compactor(_Broken())
    history = _history()

    out = await compactor.compress(history, deadline=None, reason="overflow")

    assert out is history
    failed = observer.of_type(CompactionFailed)
    assert len(failed) == 1 and failed[0].reason == "overflow"
    assert "exploded" in failed[0].error
    assert [e.component for e in observer.of_type(Degraded)] == ["context_compress"]


@pytest.mark.asyncio
async def test_funnel_without_context_emits_nothing() -> None:
    compactor, observer = _compactor(None)
    history = _history()

    assert await compactor.compress(history, deadline=None, reason="auto") is history
    assert observer.events == []


@pytest.mark.asyncio
async def test_compact_now_threads_manual_reason() -> None:
    ctx = _OkContext()
    compactor, observer = _compactor(ctx)

    class _Session:
        def __init__(self, messages) -> None:  # type: ignore[no-untyped-def]
            self.messages = list(messages)
            self.turn_count = 9

    session = _Session(_history(4))
    report = await compactor.compact_now(session, timeout=5.0, reason="manual")

    assert report["removed"] == 3
    assert [s.reason for s in observer.of_type(CompactionStarted)] == ["manual"]
    assert observer.of_type(CompactionEnded)[0].reason == "manual"


@pytest.mark.asyncio
async def test_funnel_defaults_to_auto_reason_through_a_real_context_manager(tmp_path) -> None:
    """I1: the budget-driven path passes no reason, so the funnel must default
    to auto.  Uses the real ContextManager (streaming fake summarizer), not
    the _OkContext stub above, so the on_delta/usage plumbing is exercised
    for real."""
    from ai_coding.core.context_compression import ContextManager

    async def streaming_fake(text, previous=None, on_delta=None, usage_sink=None):
        if on_delta is not None:
            on_delta("## 目标\n")
            on_delta("done")
        if usage_sink is not None:
            usage_sink.append({"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12})
        return "## 目标\ndone"

    from types import SimpleNamespace

    settings = SimpleNamespace(
        max_context_tokens=5,
        max_messages=20,
        snip_keep_head=2,
        snip_keep_tail=6,
        keep_recent_tool_results=2,
        per_result_persist_bytes=1000,
    )
    context = ContextManager(settings, transcripts_root=tmp_path, summarizer=streaming_fake)
    compactor, observer = _compactor(context)
    history = _history(8)

    out = await compactor.compress(list(history), deadline=None, observed_tokens=10_000)

    assert out is not history
    started = observer.of_type(CompactionStarted)
    ended = observer.of_type(CompactionEnded)
    assert [s.reason for s in started] == ["auto"]
    assert [d.text for d in observer.of_type(CompactionDelta)] == ["## 目标\n", "done"]
    assert len(ended) == 1 and ended[0].reason == "auto"
    assert ended[0].total_tokens == 12
    assert ended[0].text == "## 目标\ndone"


# ---- M-E3: the per-layer account (plan-m-e §C.1) ---------------------------


@pytest.mark.asyncio
async def test_compaction_ended_reports_per_layer_stats(tmp_path) -> None:
    """W5: ``CompactionEnded.layers`` carries the pipeline's per-layer account.

    The plumbing is the point — ``ContextManager`` produces ``last_layer_stats``
    and ``Compactor._compress_once`` reads it.  Without that one line the field
    is always empty, which is exactly the gap plan-m-e P0-1 closed.
    """
    from types import SimpleNamespace

    from ai_coding.core.context_compression import ContextManager

    async def summarizer(text, previous=None, on_delta=None, usage_sink=None):
        return "## 目标\n摘要"

    settings = SimpleNamespace(
        max_context_tokens=256_000,
        max_messages=10,
        snip_keep_head=2,
        snip_keep_tail=3,
        keep_recent_tool_results=1,
        per_result_persist_bytes=30_000,
        l4_tail_tokens=15_000,
    )
    context = ContextManager(settings, transcripts_root=tmp_path, summarizer=summarizer)
    compactor, observer = _compactor(context)
    history = _history(30)

    await compactor.compress(history, deadline=None, force=True)

    ended = observer.of_type(CompactionEnded)
    assert len(ended) == 1
    layers = ended[0].layers
    # Pipeline order, not numeric: L3 -> L1 -> L2 -> L4.
    assert [stat.name for stat in layers] == ["L3", "L1", "L2", "L4"]
    # Adjacent layers are continuous: the previous exit is the next entry.
    for previous, following in zip(layers, layers[1:], strict=False):
        assert following.before_tokens == previous.after_tokens
    # The telescoped delta is the boundary delta.
    total_delta = sum(stat.after_tokens - stat.before_tokens for stat in layers)
    assert total_delta == ended[0].estimated_after - ended[0].estimated_before
    assert ended[0].estimated_before == layers[0].before_tokens
    assert ended[0].estimated_after == layers[-1].after_tokens
    # L1 actually removed messages here (30 -> 6); L4 removed the head into a note.
    assert layers[1].removed > 0
    assert layers[3].removed > 0


@pytest.mark.asyncio
async def test_a_pipeline_that_does_not_summarise_has_no_l4_stat(tmp_path) -> None:
    """A layer that did not run is absent, not all-zero (plan-m-e §C.1 P2-1)."""
    from types import SimpleNamespace

    from ai_coding.core.context_compression import ContextManager

    settings = SimpleNamespace(
        max_context_tokens=256_000,
        max_messages=10,
        snip_keep_head=2,
        snip_keep_tail=3,
        keep_recent_tool_results=1,
        per_result_persist_bytes=30_000,
        l4_tail_tokens=15_000,
    )
    # A summarizer that returns None degrades L4 to "no change", but the layer
    # still ran; a summarizer that is absent means it never ran.
    context = ContextManager(settings, transcripts_root=tmp_path, summarizer=None)
    compactor, observer = _compactor(context)

    await compactor.compress(_history(30), deadline=None, force=True)

    ended = observer.of_type(CompactionEnded)
    assert len(ended) == 1
    assert "L4" not in [stat.name for stat in ended[0].layers]


@pytest.mark.asyncio
async def test_l3_persisted_and_skipped_are_observable(tmp_path) -> None:
    """M-E1/M-E3: L3's semantics are observable, not just commented.

    A trailing large output is persisted (``rewritten``); a trailing batch
    under the gate is examined and left alone (``skipped``).  Both land on
    ``CompactionEnded`` — "what did L3 do" stops being a claim in a docstring.
    """
    from types import SimpleNamespace

    from ai_coding.core.context_compression import ContextManager

    def _settings() -> SimpleNamespace:
        # A small window: the 16k floor only kicks in at >= 32k, so the ceiling
        # is a straight 10% here (12000 -> 10800), and L3's gate is window/2.
        return SimpleNamespace(
            max_context_tokens=12_000,
            max_messages=6,
            snip_keep_head=3,
            snip_keep_tail=3,
            keep_recent_tool_results=3,
            per_result_persist_bytes=5_000,
            l4_tail_tokens=15_000,
        )

    def _trailing_history(body: str) -> list[ChatMessage]:
        # The long user message opens the budget gate; the trailing tool result
        # is what L3 sees.
        return [
            ChatMessage(role="user", content="z" * 40_000, timestamp=""),
            ChatMessage(
                role="assistant",
                content=None,
                timestamp="",
                tool_calls=[ToolCallRef(id="c1", name="bash", arguments="{}")],
            ),
            ChatMessage(
                role="tool", content=body, timestamp="", tool_call_id="c1", tool_name="bash"
            ),
        ]

    def _snipped_history() -> list[ChatMessage]:
        # Enough messages that L1 fires (so the funnel emits Ended), while the
        # trailing batch stays under L3's gate.
        messages = [ChatMessage(role="user", content="z" * 40_000, timestamp="")]
        for index in range(8):
            messages.append(
                ChatMessage(
                    role="assistant",
                    content=None,
                    timestamp="",
                    tool_calls=[ToolCallRef(id=f"c{index}", name="bash", arguments="{}")],
                )
            )
            messages.append(
                ChatMessage(
                    role="tool",
                    content="y" * 5_000,
                    timestamp="",
                    tool_call_id=f"c{index}",
                    tool_name="bash",
                )
            )
        return messages

    compactor, observer = _compactor(ContextManager(_settings(), transcripts_root=tmp_path))
    await compactor.compress(_trailing_history("x" * 40_000), deadline=None)
    l3 = next(s for s in observer.of_type(CompactionEnded)[0].layers if s.name == "L3")
    assert (l3.rewritten, l3.skipped) == (1, 0)

    compactor, observer = _compactor(ContextManager(_settings(), transcripts_root=tmp_path))
    await compactor.compress(_snipped_history(), deadline=None)
    l3 = next(s for s in observer.of_type(CompactionEnded)[0].layers if s.name == "L3")
    assert (l3.rewritten, l3.skipped) == (0, 1)
