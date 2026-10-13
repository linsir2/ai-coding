"""What a compaction must preserve, and what must move the meter.

Three defects were found by measuring a live session, and each one passed the
tests written alongside it. The failure mode was the same in all three: the
assertion checked that a stage *ran*, never that it ran on the right input.

1. ``ContextManager.compress`` snipped first and summarised second, so the
   summariser was handed the ``[snipped 51 messages]`` marker instead of the 51
   messages it stands for. The model was asked to summarise a sentence saying
   that something had been deleted.
2. ``should_compact`` decided on ``estimate_tokens``, a character-count heuristic.
   Measured live: the estimate said ~60k, the provider said 237,157, and a soft
   mark at 38.4k never opened — so compaction could only ever happen after the
   request had already failed.
3. ``last_compacted_turn`` was written by the ``/compact`` command but not by the
   budget-driven path in ``request_assembly``, so the meter kept reporting
   pre-compaction occupancy after an automatic compaction.
"""

import asyncio
import tempfile
from pathlib import Path

from ai_coding.config.models import AIConfig, AppConfig, resolve_context_window
from ai_coding.core.compaction import Compactor
from ai_coding.core.context_compression import ContextManager
from ai_coding.domain.message import ChatMessage
from ai_coding.domain.model_config import ModelConfig
from ai_coding.domain.session import SessionData
from ai_coding.server.handlers import context_tokens

# A string that appears in exactly one message and is dropped by the snip. If it
# reaches the summariser, the summariser is looking at the real history.
SECRET = "HOKKA-7734"

# snip_middle(out, max_messages=50, keep_head=3, keep_tail=20) on 74 messages
# keeps indices 0-2 and 54-73; everything in 3..53 is dropped.
DROPPED_INDEX = 10
KEPT_TAIL_START = 68


def _messages(count: int) -> list[ChatMessage]:
    return [
        ChatMessage(role="user", content=f"第{index}条消息，讨论工程细节" * 30, timestamp="")
        for index in range(count)
    ]


def _with_a_canary() -> list[ChatMessage]:
    messages = _messages(74)
    messages[DROPPED_INDEX] = ChatMessage(
        role="user", content=f"部署口令是 {SECRET}，不要写进日志", timestamp=""
    )
    return messages


def _manager(*, summarizer, root=None, **overrides) -> ContextManager:
    return ContextManager(
        AIConfig(**{"max_context_tokens": 48_000, "max_messages": 50, **overrides}),
        transcripts_root=root or Path(tempfile.mkdtemp()),
        summarizer=summarizer,
    )


def _collector(sink: list[str]):
    async def summarizer(text: str) -> str:
        sink.append(text)
        return "早期对话的摘要"

    return summarizer


# ---- 1. the summariser must be handed the history, not the marker ----------


def test_the_summariser_is_handed_the_messages_the_snip_deleted():
    """The regression this whole file exists for.

    The stage that preserves meaning was running on the placeholder that the
    stage before it had just written. Every earlier assertion checked that the
    summariser was *called*; this one checks what it was handed.
    """
    seen: list[str] = []
    manager = _manager(summarizer=_collector(seen), l4_keep_tail=6)

    asyncio.run(manager.compress(_with_a_canary()))

    assert any(SECRET in text for text in seen), (
        "the dropped message must reach the summariser; a summary written from "
        "the '[snipped N messages]' marker is a summary of nothing"
    )


def test_the_summary_is_built_before_the_snip_removes_the_evidence():
    seen: list[str] = []
    manager = _manager(summarizer=_collector(seen), l4_keep_tail=6)

    asyncio.run(manager.compress(_with_a_canary()))

    assert not any("[snipped" in text for text in seen), (
        "the summariser input must not contain the marker — that is the bug, "
        "not a detail"
    )


def test_the_tail_is_still_summarised_verbatim_when_nothing_was_snipped():
    """The snip is not the only route into L4; a large-but-short history still
    gets summarised, and that path must keep working unchanged."""
    seen: list[str] = []
    manager = _manager(summarizer=_collector(seen), l4_keep_tail=6, max_messages=400)

    messages = _messages(8)
    messages[1] = ChatMessage(role="user", content="x" * 400_000, timestamp="")

    asyncio.run(manager.compress(messages))

    assert any("x" in text for text in seen), "the oversized head still gets summarised"


# ---- 2. the gate must decide on the provider's number ----------------------


def test_the_gate_opens_on_the_provider_count_when_the_estimate_is_blind():
    """Measured live: the estimator said ~60k, the provider said 237,157. The soft
    mark sat at 0.8 x 48,000. A gate that only reads the estimate cannot open."""
    manager = _manager(summarizer=None)
    under_the_estimate_mark = [ChatMessage(role="user", content="a" * 8_000, timestamp="")]

    assert manager.should_compact(under_the_estimate_mark) is False, "by estimate alone: under"

    assert manager.should_compact(under_the_estimate_mark, observed_tokens=237_157) is True, (
        "the provider says the window is full; that is the measurement that counts"
    )


def test_without_a_provider_count_the_gate_decides_on_the_estimate_alone():
    """Ignore case: a brand new session has no usage yet, and the gate must behave
    exactly as it did before this parameter existed."""
    manager = _manager(summarizer=None)
    under = [ChatMessage(role="user", content="a" * 8_000, timestamp="")]

    assert manager.should_compact(under, observed_tokens=0) is manager.should_compact(under)


def test_a_provider_count_below_the_mark_does_not_open_the_gate():
    manager = _manager(summarizer=None)
    under = [ChatMessage(role="user", content="a" * 8_000, timestamp="")]

    assert manager.should_compact(under, observed_tokens=1_000) is False


# ---- 2b. where the window itself comes from -------------------------------


def _config(*, configured: int, model_window: int) -> AppConfig:
    app = AppConfig(
        default_model="qwen",
        models={
            "qwen": ModelConfig(
                name="qwen3.7-flash", base_url="http://x", api_key="k", context_window=model_window
            )
        },
    )
    app.ai.max_context_tokens = configured
    return app


def test_an_unconfigured_window_is_read_from_the_model_not_from_a_guessed_default():
    """48,000 was a guess that survived into every fresh install. The model knows
    its own window; the config should not have to restate it."""
    window = resolve_context_window(_config(configured=0, model_window=256_000))

    assert window == 256_000


def test_an_explicit_window_overrides_the_model_metadata():
    window = resolve_context_window(_config(configured=32_000, model_window=256_000))

    assert window == 32_000, "the config is the override, per the decision"


def test_a_model_that_declares_nothing_falls_back_to_the_conservative_default():
    window = resolve_context_window(_config(configured=0, model_window=0))

    assert window == 48_000, "unknown stays conservative rather than optimistic"


# ---- 3. one boundary, written wherever the compaction happens ---------------


def test_a_budget_driven_compaction_leaves_a_boundary_the_meter_can_read():
    seen: list[str] = []
    manager = _manager(summarizer=_collector(seen))
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    session.turn_count = 9
    session.record_turn_usage(9, {"prompt_tokens": 237_157})
    compactor = Compactor(manager, emit=lambda event: None)

    history = asyncio.run(
        compactor.compress(session.messages, deadline=None, force=True, session=session)
    )
    session.messages = history

    assert context_tokens(session) == 0, (
        "an automatic compaction that leaves no boundary makes the meter report a "
        "context that no longer exists"
    )


def test_compacting_nothing_leaves_no_boundary():
    manager = _manager(summarizer=None, max_messages=400)
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    session.record_turn_usage(4, {"prompt_tokens": 9_000})
    compactor = Compactor(manager, emit=lambda event: None)

    asyncio.run(
        compactor.compress(_messages(5), deadline=None, session=session)
    )

    assert context_tokens(session) == 9_000, "no compaction means no boundary to move"


def test_the_explicit_command_no_longer_writes_the_boundary_itself():
    """The boundary belongs to the one place that knows a compaction happened.
    Two writers is how it stayed correct for `/compact` and wrong for turns."""
    manager = _manager(summarizer=_collector([]))
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    session.messages = _with_a_canary()
    compactor = Compactor(manager, emit=lambda event: None)

    report = asyncio.run(compactor.compact_now(session, timeout=10.0))

    assert report["after"] < report["messages"]
    assert session.last_compacted_turn == session.turn_count, (
        "compact_now is the funnel; it must set the boundary even though it is the "
        "command path"
    )

# ---- 5. a compaction must not be the end of the record ---------------------


def test_the_history_a_compaction_dropped_is_still_readable_on_disk(tmp_path):
    """Compression answers "what does the model still see"; it does not answer
    "what did we say". The transcript pane keeps rendering the compacted history,
    so without an archive the only record of those 51 messages is a hash-named
    file nobody can find.
    """
    manager = _manager(summarizer=_collector([]), root=tmp_path)

    asyncio.run(manager.compress(_with_a_canary()))

    archives = sorted((tmp_path / "transcripts" / "archive").glob("*.md"))
    assert archives, "the dropped history must land somewhere a human can find it"
    body = archives[-1].read_text(encoding="utf-8")
    assert SECRET in body, "the archive is the transcript, not a count of it"


def test_an_untouched_history_leaves_no_archive(tmp_path):
    """The archive exists for what a compaction removed. Writing one when nothing
    was removed would fill the directory with copies of the live transcript."""
    manager = _manager(summarizer=_collector([]), root=tmp_path)

    asyncio.run(manager.compress(_messages(10)))

    assert not (tmp_path / "transcripts" / "archive").exists()


def test_the_archive_names_the_turn_it_was_taken_from(tmp_path):
    """A file called summary_<hash>.txt cannot be ordered against the session.
    The archive carries a timestamp so the newest one is obvious on disk."""
    manager = _manager(summarizer=_collector([]), root=tmp_path)

    asyncio.run(manager.compress(_with_a_canary()))

    names = [p.name for p in (tmp_path / "transcripts" / "archive").glob("*.md")]
    assert names and names[-1][:8].isdigit(), f"expected a leading date stamp, got {names}"


def test_the_compaction_boundary_survives_being_written_to_disk():
    """The boundary was written but never persisted, so `/resume` (or a restart)
    brought back the usage rows the compaction had already invalidated and the
    meter read them again — the same stale reading, one session-load later."""
    from ai_coding.domain.session import SessionData as _S

    session = _S(session_id="s", title="", created_time="t", last_access_time="t")
    session.turn_count = 9
    session.record_turn_usage(9, {"prompt_tokens": 237_157})
    session.last_compacted_turn = 9

    reloaded = _S.from_dict(session.to_dict())

    assert reloaded.last_compacted_turn == 9
    assert context_tokens(reloaded) == 0, "a reloaded session must not resurrect old usage"


def test_a_session_that_was_never_compacted_writes_no_key():
    """Every other optional field follows this rule; a file written by a session
    that never compacted stays byte-identical to one from before this existed."""
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")

    assert "lastCompactedTurn" not in session.to_dict()
