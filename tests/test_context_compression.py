"""TDD (M3-1): context-compression pure stages — snip / placeholder / persist / sanitize.

Network-free: these stages only reshape ``ChatMessage`` lists and write transcripts
to disk.  The L4/LLM stage and ``ContextManager.compress`` orchestration live in
``test_context_compression_l4.py``.
"""

from ai_coding.core.context_compression import (
    PERSISTED_MARKER,
    persist_large_outputs,
    placeholder_old_results,
    sanitize_pairs,
    snip_middle,
)
from ai_coding.domain.message import ChatMessage, ToolCallRef


def _user(content: str, ts: str = "t") -> ChatMessage:
    return ChatMessage(role="user", content=content, timestamp=ts)


def _assistant(content: str, ts: str = "t") -> ChatMessage:
    return ChatMessage(role="assistant", content=content, timestamp=ts)


def _tool(content: str, call_id: str, tool: str = "bash", ts: str = "t") -> ChatMessage:
    return ChatMessage(
        role="tool",
        content=content,
        timestamp=ts,
        tool_call_id=call_id,
        tool_name=tool,
    )


def _assistant_with_calls(content: str | None, *calls: ToolCallRef) -> ChatMessage:
    return ChatMessage(
        role="assistant",
        content=content,
        timestamp="t",
        tool_calls=list(calls) if calls else None,
    )


# ---------------------------------------------------------------- snip (L1)


def test_snip_belows_threshold_noop():
    msgs = [_user("a"), _user("b"), _user("c")]
    out = snip_middle(msgs, max_messages=50, keep_head=3, keep_tail=20)
    assert out is msgs  # unchanged reference when no snipping


def test_snip_keeps_head_and_tail():
    msgs = [_user(f"m{i}") for i in range(10)]
    out = snip_middle(msgs, max_messages=6, keep_head=2, keep_tail=3)
    assert len(out) == 6  # head2 + placeholder1 + tail3
    assert out[0].content == "m0"
    assert out[1].content == "m1"
    assert out[-1].content == "m9"
    assert out[-2].content == "m8"
    assert out[-3].content == "m7"
    # placeholder carries the snipped count
    assert out[2].role == "user"
    assert "5" in out[2].content  # 10 - 2 - 3 = 5 snipped (10 total, 5 kept visible)


def test_snip_placeholder_is_user_role():
    msgs = [_user("x") for _ in range(8)]
    out = snip_middle(msgs, max_messages=4, keep_head=1, keep_tail=2)
    assert out[1].role == "user"


def test_snip_preserves_order():
    msgs = [_user(f"s{i}") for i in range(9)]
    out = snip_middle(msgs, max_messages=7, keep_head=3, keep_tail=3)
    kept_bodies = [m.content for m in out if m is not out[3]]
    assert kept_bodies == ["s0", "s1", "s2", "s6", "s7", "s8"]


# ------------------------------------------------------- placeholder (L2)


def test_placeholder_empty_when_under_limit():
    msgs = [_tool("r1", "c1"), _tool("r2", "c2"), _tool("r3", "c3")]
    out = placeholder_old_results(msgs, keep_recent=3)
    assert all("Previous" not in (m.content or "") for m in out)


def test_placeholder_marks_older_keeps_recent():
    msgs = [_tool("r1", "c1"), _tool("r2", "c2"), _tool("r3", "c3"), _tool("r4", "c4")]
    out = placeholder_old_results(msgs, keep_recent=2)
    # oldest two get replaced, newest two untouched
    assert out[0].content == "[Previous: used bash]"
    assert out[1].content == "[Previous: used bash]"
    assert out[2].content == "r3"
    assert out[3].content == "r4"
    # pairing fields survive the placeholder
    assert out[0].tool_call_id == "c1"
    assert out[0].role == "tool"


def test_placeholder_skips_persisted_marker():
    big = f'<{PERSISTED_MARKER} path="transcripts/persisted/x.txt">\npreview\n</persisted-output>'
    msgs = [_tool(big, "c1"), _tool("r2", "c2"), _tool("r3", "c3")]
    out = placeholder_old_results(msgs, keep_recent=2)
    # the persisted-marker output is skipped (not overwritten with Previous)
    assert PERSISTED_MARKER in out[0].content


def test_placeholder_preserves_message_count():
    msgs = [_tool(f"r{i}", f"c{i}") for i in range(5)]
    out = placeholder_old_results(msgs, keep_recent=2)
    assert len(out) == 5


# ----------------------------------------------------------- persist (L3)


def test_persist_writes_large_output_and_replaces_bodies(tmp_path):
    big_body = "x" * 60000  # > per_result_bytes
    small_body = "y" * 100
    msgs = [_tool(small_body, "c1", tool="bash"), _tool(big_body, "c2", tool="bash")]
    # batch total (60100 bytes) > max_result_bytes(60000) triggers persistence
    out = persist_large_outputs(
        msgs,
        root=tmp_path,
        max_result_bytes=60000,
        per_result_bytes=30000,
        preview_len=2000,
    )
    persisted = list((tmp_path / "transcripts" / "persisted").glob("*.txt"))
    assert len(persisted) == 1  # only the >30000 block persisted
    assert persisted[0].read_text(encoding="utf-8") == big_body
    # the large body was replaced (only a short preview remains); the small one untouched
    assert ("x" * 30000) not in out[1].content  # full body no longer inline
    assert PERSISTED_MARKER in out[1].content
    assert out[0].content == small_body
    # pairing + tool name kept
    assert out[1].tool_call_id == "c2"
    assert out[1].tool_name == "bash"


def test_persist_not_triggered_when_batch_under_limit(tmp_path):
    body = "a" * 50000
    msgs = [_tool(body, "c1")]
    out = persist_large_outputs(
        msgs, root=tmp_path, max_result_bytes=1_000_000, per_result_bytes=30000
    )
    assert out == msgs  # unchanged reference
    assert not list((tmp_path / "transcripts" / "persisted").glob("*.txt"))


def test_persist_preview_truncated(tmp_path):
    body = "z" * 50000
    msgs = [_tool(body, "c1")]
    out = persist_large_outputs(
        msgs, root=tmp_path, max_result_bytes=10000, per_result_bytes=30000, preview_len=64
    )
    # preview is only 64 chars inside the persisted-output tag
    inner = out[0].content
    assert "z" * 64 in inner
    assert "z" * 1000 not in inner


# --------------------------------------------------------- sanitize_pairs


def test_sanitize_drops_orphan_tool_results():
    msgs = [_assistant_with_calls(None, ToolCallRef(id="c1", name="bash", arguments="{}"))]
    out = sanitize_pairs(msgs)
    # assistant with no content and no surviving calls is dropped entirely
    assert out == []


def test_sanitize_drops_orphan_calls_from_content_carrier():
    msgs = [
        _assistant_with_calls(
            "text", ToolCallRef(id="c1", name="bash", arguments="{}")
        )
    ]
    out = sanitize_pairs(msgs)
    assert len(out) == 1
    assert out[0].content == "text"
    assert out[0].tool_calls is None  # orphan calls stripped


def test_sanitize_keeps_tool_result_with_matching_call():
    call = ToolCallRef(id="call1", name="bash", arguments="{}")
    msgs = [
        _assistant_with_calls(None, call),
        _tool("result-ok", "call1"),
    ]
    out = sanitize_pairs(msgs)
    assert len(out) == 2
    assert out[1].content == "result-ok"


def test_sanitize_drops_tool_result_without_declared_call():
    msgs = [_tool("orphan-result", "ghost")]
    out = sanitize_pairs(msgs)
    assert out == []


def test_sanitize_keeps_plain_user_and_assistant():
    msgs = [_user("hi"), _assistant("hello")]
    out = sanitize_pairs(msgs)
    assert len(out) == 2
