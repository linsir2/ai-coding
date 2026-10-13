"""The compaction policy: when to compact, and what must survive it.

M11. Measured on a real session: ``/compact`` on 74 messages produced a bare
``[snipped 51 messages]`` and asked the model nothing. The cause was ordering,
not thresholds — ``snip_middle`` cut 74 -> 24 messages first, which dropped the
estimate under the water mark, so the LLM-summary stage, the only stage that
preserves meaning, could no longer fire.

A snip without a summary is deletion wearing a placeholder's clothes, and a
compaction that drops messages is precisely the case that must not lose them.
"""

import asyncio

from ai_coding.config.models import AIConfig
from ai_coding.core.context_compression import ContextManager
from ai_coding.domain.message import ChatMessage
from ai_coding.domain.session import SessionData, TurnUsage
from ai_coding.server.handlers import context_tokens


def _messages(count: int, size: int = 30) -> list[ChatMessage]:
    return [
        ChatMessage(role="user", content=f"第{index}条消息，讨论工程细节" * size, timestamp="")
        for index in range(count)
    ]


def _manager(tmp_path, *, summarizer, **overrides):
    return ContextManager(
        AIConfig(max_context_tokens=48_000, max_messages=50, **overrides),
        transcripts_root=tmp_path,
        summarizer=summarizer,
    )


def test_a_snip_that_drops_messages_always_asks_the_model_for_a_summary(tmp_path):
    calls: list[int] = []

    async def summarizer(text: str) -> str:
        calls.append(len(text))
        return "早期对话的摘要"

    manager = _manager(tmp_path, summarizer=summarizer)
    messages = _messages(74)

    out = asyncio.run(manager.compress(messages))

    assert calls, "dropping messages must call the summariser"
    assert out[0].content.startswith("[以下为早期对话摘要]")
    assert not any("snipped" in (m.content or "") for m in out), (
        "a summary replaces the snip placeholder; keeping both reads as two "
        "different losses"
    )


def test_the_summary_keeps_the_recent_tail_intact(tmp_path):
    async def summarizer(text: str) -> str:
        return "早期对话的摘要"

    manager = _manager(tmp_path, summarizer=summarizer, l4_keep_tail=6)
    messages = _messages(74)

    out = asyncio.run(manager.compress(messages))

    assert len(out) == 1 + 6, "the summary plus the kept tail"
    assert out[-1].content.startswith("第73条"), "the newest turn survives verbatim"
    assert out[-6].content.startswith("第68条"), "and the whole kept tail, oldest first"


def test_no_summariser_still_degrades_to_the_placeholder_rather_than_raising(tmp_path):
    """Without a summariser there is nothing better to say, so the placeholder is
    honest — but it is the fallback, not the default path."""
    manager = _manager(tmp_path, summarizer=None)
    messages = _messages(74)

    out = asyncio.run(manager.compress(messages))

    assert any("snipped" in (m.content or "") for m in out)


def test_a_history_short_enough_to_keep_never_asks_the_model(tmp_path):
    """The reason the fix is scoped to 'dropped messages': below both thresholds
    nothing is lost, and spending a summarisation call on it would be the model
    paying for a no-op on every turn."""
    calls: list[int] = []

    async def summarizer(text: str) -> str:
        calls.append(len(text))
        return "不该被调用"

    manager = _manager(tmp_path, summarizer=summarizer)
    messages = _messages(10)

    out = asyncio.run(manager.compress(messages))

    assert calls == []
    assert len(out) == 10

# ---- M11: the context meter must follow the compaction boundary ------------


def _usage_row(turn: int, prompt: int) -> TurnUsage:
    return TurnUsage(turn=turn, prompt_tokens=prompt, completion_tokens=10, total_tokens=prompt + 10)


def test_the_meter_ignores_usage_measured_before_the_last_compaction():
    """Measured live: after `/compact` the sidebar still read 237,157 / 100%,
    because it reported the last turn's prompt tokens and a compaction produces
    no turn. The pre-compaction number describes a context that no longer exists —
    reporting it is a false statement about how full the window is.

    The rule: usage from a turn that happened before the boundary describes the
    old context and must not be presented as the new one's occupancy."""
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    for turn in (1, 2, 3):
        session.record_turn_usage(turn, {"prompt_tokens": 1000 * turn})
    session.last_compacted_turn = 3

    tokens = context_tokens(session)

    assert tokens == 0, "no turn has been measured since the compaction"


def test_the_meter_reports_the_newest_usage_once_a_turn_has_run_again():
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    for turn in (1, 2, 3):
        session.record_turn_usage(turn, {"prompt_tokens": 1000 * turn})
    session.last_compacted_turn = 3
    session.record_turn_usage(4, {"prompt_tokens": 40_000})

    assert context_tokens(session) == 40_000


def test_with_no_compaction_the_meter_is_unchanged():
    """The ordinary path must keep reporting the last turn, whatever it was."""
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    session.record_turn_usage(1, {"prompt_tokens": 5_000})
    session.record_turn_usage(2, {"prompt_tokens": 9_000})

    assert context_tokens(session) == 9_000


def test_a_session_with_no_usage_reports_zero_rather_than_raising():
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")

    assert context_tokens(session) == 0


# ---- M11: the meter must not flatter a wrong configuration -----------------


def test_the_water_mark_is_a_fraction_of_the_configured_window():
    """Measured live: `max_context_tokens` was 48,000 against a 256k model, so the
    soft mark sat at 38k — 15% of the real capacity — and the meter reported
    237,157 tokens as a flat 100%. The configuration was the defect; the policy
    is unchanged."""
    import tempfile
    from pathlib import Path

    from ai_coding.core.context_compression import ContextManager

    cfg = AIConfig(max_context_tokens=256_000, compress_soft_ratio=0.8)
    manager = ContextManager(cfg, transcripts_root=Path(tempfile.mkdtemp()), summarizer=None)

    # Sized against the mark itself, not by a round number: 0.8 x 256,000 is
    # 204,800 tokens, and the estimator is ~1 token per 4 ASCII characters.
    def sized(tokens: int) -> list[ChatMessage]:
        return [ChatMessage(role="user", content="a" * (tokens * 4), timestamp="")]

    assert manager.should_compact(sized(100_000)) is False, "under the mark"
    assert manager.should_compact(sized(250_000)) is True, "over the mark"


def test_compact_if_needed_leaves_a_history_under_the_mark_untouched():
    """The reason the predictive check is cheap: below the mark nothing is lost,
    so rewriting the history would only cost the prompt-cache prefix."""
    import tempfile
    from pathlib import Path

    from ai_coding.core.context_compression import ContextManager

    calls: list[int] = []

    async def summarizer(text: str) -> str:
        calls.append(len(text))
        return "摘要"

    cfg = AIConfig(max_context_tokens=256_000, max_messages=50)
    manager = ContextManager(
        cfg, transcripts_root=Path(tempfile.mkdtemp()), summarizer=summarizer
    )
    messages = _messages(20)

    out = asyncio.run(manager.compress_if_needed(messages))

    assert out is messages, "an untouched history must come back as the same list"
    assert calls == []
