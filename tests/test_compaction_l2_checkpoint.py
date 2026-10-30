"""B: the checkpoint path must reclaim old tool results too (microcompact).

The first compaction already runs L2 on the verbatim tail; the checkpoint path
skipped it on purpose (eval-08: eliding a 12k tail down to 1-3k made the model
re-read the same files).  What that skip missed is the *steady state*: after the
first summary every request carries ``old tools + tail`` verbatim, so the tail
grows back to the ceiling within a dozen iterations.

The rule these tests pin: the same ``keep_recent_tool_results`` window applies to
the checkpoint path, and anything inside it must survive byte-for-byte.
"""

import tempfile
from pathlib import Path

import pytest

from ai_coding.config.models import AIConfig
from ai_coding.core.context_compression import (
    ContextManager,
    _anchor_of,
    install_checkpoint,
)
from ai_coding.domain.message import ChatMessage, ToolCallRef

WINDOW = 48_000
KEEP_RECENT = 8


def _manager() -> ContextManager:
    return ContextManager(
        AIConfig(**{"max_context_tokens": WINDOW, "keep_recent_tool_results": KEEP_RECENT}),
        transcripts_root=Path(tempfile.mkdtemp()),
        summarizer=None,
    )


def _history(tool_count: int, chars: int = 4_000) -> list[ChatMessage]:
    calls = [ToolCallRef(id=f"c{i}", name="bash", arguments="{}") for i in range(tool_count)]
    messages = [
        ChatMessage(role="user", content="开始", timestamp=""),
        ChatMessage(role="assistant", content="好的", timestamp=""),
        ChatMessage(role="user", content="继续", timestamp=""),
        ChatMessage(role="assistant", content=None, timestamp="", tool_calls=calls),
    ]
    for i in range(tool_count):
        messages.append(
            ChatMessage(
                role="tool",
                content=f"输出{i}:" + "x" * chars,
                timestamp="",
                tool_call_id=f"c{i}",
                tool_name="bash",
            )
        )
    return messages


#: The checkpoint boundary must land on a whole exchange: covering the assistant
#: that declared the tool calls orphans the results and ``sanitize_pairs`` drops
#: them -- a fixture that does that tests the wrong layer.
CHECKPOINT_CONSUMED = 3


def _with_checkpoint(messages: list[ChatMessage]) -> tuple[ContextManager, object]:
    session = _session(messages)
    install_checkpoint(
        session,
        "早期对话的摘要",
        consumed=CHECKPOINT_CONSUMED,
        anchor=_anchor_of(messages[:CHECKPOINT_CONSUMED]),
    )
    return _manager(), session


def _session(messages):  # type: ignore[no-untyped-def]
    from ai_coding.domain.session import SessionData

    session = SessionData(
        session_id="s1", title="", created_time="2026-01-01T00:00:00Z", last_access_time=""
    )
    session.messages = list(messages)
    return session


def _l2(manager: ContextManager):  # type: ignore[no-untyped-def]
    return next(stat for stat in manager.last_layer_stats if stat.name == "L2")


@pytest.mark.asyncio
async def test_the_checkpoint_path_reclaims_tool_results_older_than_the_window():
    messages = _history(tool_count=20)
    manager, session = _with_checkpoint(messages)

    await manager.compress(list(messages), session=session, page=False)

    l2 = _l2(manager)
    assert l2.rewritten >= 1, "twenty tool results with a window of eight must elide some"
    assert l2.after_tokens < l2.before_tokens


@pytest.mark.asyncio
async def test_the_checkpoint_path_keeps_the_recent_tool_results_verbatim():
    messages = _history(tool_count=6)
    manager, session = _with_checkpoint(messages)

    out = await manager.compress(list(messages), session=session, page=False)

    assert _l2(manager).rewritten == 0, "everything fits the keep window"
    bodies = [m.content for m in out if m.role == "tool"]
    assert bodies == [m.content for m in messages if m.role == "tool"], (
        "a body inside the window was rewritten"
    )
