"""F1: a checkpoint must shape *every* request, not only the compacting one.

Trace 2026-10-31 (session f1d881ee, t8) measured the defect exactly:

    i27 est 195,841 → i28 21,864 → i29 201,556 → i30 19,720 → …

Six cycles in one item.  The gate reads the provider's number for the request
that was just sent; after a compacted request that number is small, so the next
iteration's gate stays shut -- and the assembler then returns the *raw* history,
which is still 200k.  The checkpoint existed the whole time; it just was never
applied below the watermark.

Rules pinned here:
* with a checkpoint, a below-watermark request returns ``note + tail``;
* the raw head never comes back while the anchor still matches;
* the view emits no compaction events and pays no summary call;
* an anchor mismatch (undo/edit) falls back to the raw history.
"""

import tempfile
from pathlib import Path

import pytest

from ai_coding.config.models import AIConfig
from ai_coding.core.compaction import Compactor
from ai_coding.core.context_compression import (
    CHECKPOINT_MARK,
    ContextManager,
    _anchor_of,
    _history_tokens,
    install_checkpoint,
)
from ai_coding.domain.message import ChatMessage
from ai_coding.domain.session import SessionData

WINDOW = 48_000
SUMMARY = "## 目标\n继续把层设计跑完\n\n## 下一步\n读 security"


def _manager() -> ContextManager:
    return ContextManager(
        AIConfig(**{"max_context_tokens": WINDOW}),
        transcripts_root=Path(tempfile.mkdtemp()),
        summarizer=None,
    )


def _chat(n: int, tag: str = "old") -> list[ChatMessage]:
    return [
        ChatMessage(
            role="user" if index % 2 == 0 else "assistant",
            content=f"{tag}-{index}",
            timestamp="",
        )
        for index in range(n)
    ]


def _session(messages: list[ChatMessage]) -> SessionData:
    session = SessionData(
        session_id="s1",
        title="",
        created_time="2026-01-01T00:00:00Z",
        last_access_time="",
    )
    session.messages = list(messages)
    return session


def _with_checkpoint(consumed: int = 100) -> tuple[ContextManager, SessionData, list[ChatMessage]]:
    manager = _manager()
    history = _chat(consumed)
    session = _session(history)
    install_checkpoint(
        session, SUMMARY, consumed=consumed, anchor=_anchor_of(history[:consumed])
    )
    history = [*history, *_chat(5, tag="new")]
    session.messages = list(history)
    return manager, session, history


def test_a_below_watermark_request_returns_the_checkpoint_view():
    manager, session, history = _with_checkpoint()

    view = manager.checkpoint_view(history, session=session)

    assert view is not None
    assert len(view) == 6, "one note + the five messages after the checkpoint"
    assert (view[0].content or "").startswith(CHECKPOINT_MARK)
    assert [m.content for m in view[1:]] == [f"new-{i}" for i in range(5)]
    assert _history_tokens(view) < _history_tokens(history)


def test_the_raw_head_never_returns_while_the_anchor_matches():
    manager, session, history = _with_checkpoint()

    first = manager.checkpoint_view(history, session=session)
    second = manager.checkpoint_view(history, session=session)

    assert first is not None and second is not None
    assert [m.content for m in first] == [m.content for m in second]


def test_an_anchor_mismatch_falls_back_to_the_raw_history():
    manager, session, history = _with_checkpoint()
    history[0] = ChatMessage(role="user", content="edited after the checkpoint", timestamp="")

    view = manager.checkpoint_view(history, session=session)

    assert view is None, "the checkpoint no longer describes this history"


@pytest.mark.asyncio
async def test_compress_if_needed_applies_the_view_below_the_watermark():
    manager, session, history = _with_checkpoint()

    out = await manager.compress_if_needed(history, observed_tokens=1_000, session=session)

    assert out is not history
    assert (out[0].content or "").startswith(CHECKPOINT_MARK)
    assert len(out) == 6


@pytest.mark.asyncio
async def test_the_compactor_returns_the_view_without_emitting_compaction_events():
    manager, session, history = _with_checkpoint()
    events = []
    compactor = Compactor(manager, emit=events.append)

    out = await compactor.compress(
        history, deadline=None, observed_tokens=1_000, session=session
    )

    assert len(out) == 6 and (out[0].content or "").startswith(CHECKPOINT_MARK)
    assert events == [], "a view is not a compaction"
    assert compactor.last_was_view is True
    assert Compactor.did_compact(history, out) is True


def test_pinned_messages_ride_into_the_view():
    manager, session, history = _with_checkpoint()
    pinned = [ChatMessage(role="user", content="PINNED-INSTRUCTION", timestamp="")]

    view = manager.checkpoint_view(history, session=session, pinned=pinned)

    assert view is not None
    assert any(m.content == "PINNED-INSTRUCTION" for m in view)
