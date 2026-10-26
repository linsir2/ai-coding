"""TDD (M3-1): context-compression pure stages — snip / placeholder / persist / sanitize.

Network-free: these stages only reshape ``ChatMessage`` lists and write transcripts
to disk.  The L4/LLM stage and ``ContextManager.compress`` orchestration live in
``test_context_compression_l4.py``.
"""

import pytest

from ai_coding.core.context_compression import (
    PERSISTED_MARKER,
    ContextManager,
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


def test_placeholder_marks_older_keeps_recent(tmp_path):
    """R7b restated this contract: an elided body is an envelope, not a blank.

    The old assertion pinned the exact text ``[Previous: used bash]``, which was
    precisely the defect — the body was dropped with no way back.  The intent is
    unchanged (older results are marked, the recent ones are left alone, pairing
    survives); what the mark *is* changed (docs/r7-analysis.md 3.2, Q2).
    """
    old = "OLD-" + "x" * 900 + "-TAIL"
    msgs = [_tool(old, "c1"), _tool(old, "c2"), _tool("r3", "c3"), _tool("r4", "c4")]
    out = placeholder_old_results(msgs, keep_recent=2, elide_root=tmp_path)
    # oldest two become recoverable envelopes, newest two untouched
    assert out[0].content.startswith("[Previous: used bash")
    assert "full output: " in out[0].content
    assert out[1].content.startswith("[Previous: used bash")
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


def test_snip_can_lose_the_instruction_and_pin_brings_it_back():
    from ai_coding.core.context_compression import _ensure_pinned

    instruction = _user("审查一下这个项目")
    msgs = [_user(f"m{i}") for i in range(60)]
    msgs.insert(5, instruction)

    out = snip_middle(msgs, max_messages=10, keep_head=3, keep_tail=4)
    assert instruction not in out  # the T15 failure, reproduced

    out = _ensure_pinned(out, [instruction])
    assert out[-1] is instruction
    assert sum(1 for m in out if m.content == "审查一下这个项目") == 1


# ------------------------------------------------- L3 semantics (M-E1, W1)


def test_l3_semantics_are_locked(tmp_path):
    """W1 (plan-m-e M-E1): L3 is a mid-turn guard, not a compression layer.

    ``persist_large_outputs`` takes only the trailing run of
    ``role == "tool"`` messages.  So a large output an assistant reply follows
    is **not** persisted, while the same output trailing is.  Locking this is
    the whole point: the semantics used to live only in a comment, and the
    next reader would "fix" it into a layer that persists everything.
    """
    big = "x" * 60000
    # Not trailing: an assistant reply after the large output empties the batch.
    not_trailing = [_tool(big, "c1"), _assistant("answered")]
    out = persist_large_outputs(
        not_trailing, root=tmp_path, max_result_bytes=1000, per_result_bytes=1000
    )
    assert out[0].content == big
    assert PERSISTED_MARKER not in out[0].content
    assert not list((tmp_path / "transcripts" / "persisted").glob("*.txt"))

    # Trailing: the same large output is persisted.
    trailing = [_tool(big, "c1")]
    out = persist_large_outputs(
        trailing, root=tmp_path, max_result_bytes=1000, per_result_bytes=1000
    )
    assert PERSISTED_MARKER in out[0].content
    assert out[0].tool_call_id == "c1"


# ------------------------------------- pipeline order + atomic group (M-E2)


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
    from types import SimpleNamespace

    return SimpleNamespace(**base)


def _paired_history(turns: int = 12) -> list[ChatMessage]:
    """A realistic tool-heavy history: user, then (assistant call, tool result)."""
    messages: list[ChatMessage] = []
    for index in range(turns):
        messages.append(_user(f"第 {index} 步"))
        messages.append(
            _assistant_with_calls(None, ToolCallRef(id=f"c{index}", name="bash", arguments="{}"))
        )
        messages.append(_tool(f"RESULT-{index}", f"c{index}"))
    return messages


def _assert_strictly_paired(messages: list[ChatMessage]) -> None:
    declared = {
        call.id
        for message in messages
        if message.role == "assistant"
        for call in (message.tool_calls or ())
    }
    results = {m.tool_call_id for m in messages if m.role == "tool"}
    assert results <= declared, "an orphan tool result survived the pipeline"
    assert declared <= results, "a tool call survived without its result"


@pytest.mark.asyncio
async def test_the_pipeline_order_is_l3_l1_l2_l4(tmp_path, monkeypatch):
    """W3 (plan-m-e M-E2): the layer order is a hard invariant.

    L2 runs before L4, so L4's summary can fold the envelopes L2 just made
    (measured: the envelopes are gone afterwards).  The two stages are injected
    differently — L2 is a module-level function, L4 a bound method — so each
    appends a tag to a shared list and the assertion is the tag order.

    ``force=True`` is required: without it ``should_summarise`` may be False
    (L4 never runs) and the order assertion would be vacuous.
    """
    import ai_coding.core.context_compression as cc

    calls: list[str] = []
    real_placeholder = cc.placeholder_old_results
    real_run_l4 = cc.ContextManager._run_l4

    def _tagged_placeholder(messages, *args, **kwargs):
        calls.append("L2")
        return real_placeholder(messages, *args, **kwargs)

    async def _tagged_run_l4(self, messages, **kwargs):
        calls.append("L4")
        return await real_run_l4(self, messages, **kwargs)

    monkeypatch.setattr(cc, "placeholder_old_results", _tagged_placeholder)
    monkeypatch.setattr(cc.ContextManager, "_run_l4", _tagged_run_l4)

    async def _summary(text, previous=None, on_delta=None, usage_sink=None):
        return "## 目标\n摘要"

    manager = ContextManager(
        _settings(), transcripts_root=tmp_path, summarizer=_summary
    )
    await manager.compress(_paired_history(8), force=True)

    assert calls == ["L2", "L4"]


@pytest.mark.asyncio
async def test_no_orphan_tool_result_survives_a_full_pipeline_compress(tmp_path):
    """W4 (plan-m-e M-E2): the pipeline's exit is always strictly paired.

    ``sanitize_pairs`` is the structural guarantee, but the stage-level tests
    never ran the whole pipeline; this does, so a future stage that drops a
    call without its result (or vice versa) cannot pass unnoticed.  The
    pipeline-level lock is what the "adsorb to a user message" fix was downgraded
    to — the structural invariant already holds, this makes it a test.
    """
    manager = ContextManager(_settings(), transcripts_root=tmp_path)

    out = await manager.compress(_paired_history(12), force=True)

    _assert_strictly_paired(out)
