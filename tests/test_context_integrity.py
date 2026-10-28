"""I2: a compaction must see *everything*, and must never silently drop a message.

The defect this file exists for (eval-08 stage E, T2): compression was a
stateless pure function recomputed from the full history on every request, its
input was the first 60,000 characters of a flattened transcript, and the
previous checkpoint was never rendered into the prompt.  The result was a
checkpoint that froze at the opening of the conversation while the model was
told "conversation truncated" — 17 summary calls, 68.8% of the turn's prompt
tokens, spent re-summarising the same 18,050 tokens.

The invariant every test below defends is plan §3.0:

    every message is either in the request verbatim, or covered by the
    checkpoint (index < consumed) -- never in neither.

Red-green discipline: each test must fail against the pre-I2 pipeline.  The
cases are numbered as in docs/plan-i2-context-integrity.md §5.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

from ai_coding.ai.llm_helpers import SUMMARY_SECTIONS, checkpoint_body, summary_shape_ok
from ai_coding.config.models import AIConfig
from ai_coding.core.context_compression import (
    CHECKPOINT_MARK,
    OMISSION_MARK,
    ContextManager,
    _anchor_of,
    _history_tokens,
    _is_checkpoint_note,
    hard_trim,
    install_checkpoint,
    sanitize_pairs,
)
from ai_coding.domain.loop import TurnOutcome, TurnStatus
from ai_coding.domain.message import ChatMessage, ToolCallRef
from ai_coding.domain.turn_digest import (
    build_digest,
    render_working_memory,
)

FIRST_SUMMARY = "\n".join(f"{section}\n上一段" for section in SUMMARY_SECTIONS)
OLD_MARKER = "OLD-CONTENT-9999"


# ---- fixtures -------------------------------------------------------------


def _session():
    """The four fields the compressor reads/writes, and nothing else."""
    return SimpleNamespace(
        compaction_checkpoint="",
        compaction_consumed=0,
        compaction_anchor="",
        compaction_failures=0,
    )


def _manager(summarizer, **overrides) -> ContextManager:
    settings = {
        "max_context_tokens": 48_000,
        "max_messages": 400,
        "snip_keep_head": 3,
        "snip_keep_tail": 20,
        "keep_recent_tool_results": 8,
        # 1, not 0: zero is rejected by the config (it would silently disable
        # branch C).  Anything non-empty clears a threshold of 1.
        "summary_delta_tokens": 1,
        **overrides,
    }
    return ContextManager(
        AIConfig(**settings),
        transcripts_root=Path(overrides.pop("root", None) or _tmp_root()),
        summarizer=summarizer,
    )


_TMP: list[Path] = []


def _tmp_root() -> Path:
    import tempfile

    path = Path(tempfile.mkdtemp())
    _TMP.append(path)
    return path


def _chat(n: int, tag: str, *, size: int = 30, start: int = 0) -> list[ChatMessage]:
    return [
        ChatMessage(
            role="user" if i % 2 == 0 else "assistant",
            content=f"{tag}-{i} " + "x" * size,
            timestamp="",
        )
        for i in range(start, start + n)
    ]


def _recorder(sink: list[str], reply: str | None = FIRST_SUMMARY):
    async def summarizer(text: str) -> str | None:
        sink.append(text)
        return reply

    return summarizer


def _text_of(messages: list[ChatMessage]) -> str:
    return "\n".join(message.content or "" for message in messages)


# ---- #1 / #2: the delta, and the previous checkpoint ----------------------


def test_a_second_compaction_summarizes_only_the_delta():
    """The second pass must not re-summarise what the checkpoint already covers."""
    calls: list[str] = []
    manager = _manager(_recorder(calls))
    session = _session()
    history = _chat(40, "第一批")

    asyncio.run(manager.compress(history, session=session, force=True))
    assert calls, "the first compaction must reach the summariser"
    calls.clear()

    history = [*history, *_chat(30, "第二批", start=40)]
    asyncio.run(manager.compress(history, session=session))

    assert len(calls) == 1
    assert "第二批-" in calls[0], "the new work must reach the summariser"
    assert "第一批-0 " not in calls[0], (
        "the opening of the conversation is already inside the checkpoint; "
        "summarising it again is the defect this plan exists for"
    )


def test_the_previous_checkpoint_reaches_the_prompt():
    calls: list[str] = []
    manager = _manager(_recorder(calls))
    session = _session()
    history = _chat(40, "第一批")

    asyncio.run(manager.compress(history, session=session, force=True))
    calls.clear()

    asyncio.run(manager.compress([*history, *_chat(30, "第二批", start=40)], session=session))

    assert "<previous-checkpoint>" in calls[0]
    assert "<conversation-since-checkpoint>" in calls[0]
    assert "## 目标" in calls[0], "the previous verdict rides in whole"


# ---- #3: a small delta is carried, not summarised ------------------------


def test_a_small_delta_is_carried_verbatim_not_summarized():
    """Below the threshold the delta rides into the request as-is.

    This is the branch that stops "one summary per iteration"; without it a
    long turn pays the summariser on every request even when it learned
    nothing new.
    """
    calls: list[str] = []
    manager = _manager(_recorder(calls), summary_delta_tokens=4_000, l4_tail_tokens=30)
    session = _session()
    history = _chat(16, "m", size=10)
    install_checkpoint(
        session, FIRST_SUMMARY, consumed=5, anchor=_anchor_of(history[:5])
    )

    out = asyncio.run(manager.compress(history, session=session))

    assert calls == [], "a small delta must not cost a model call"
    body = _text_of(out)
    assert "m-5 " in body and "m-6 " in body, "the delta is carried verbatim"
    assert body.startswith(CHECKPOINT_MARK), "the checkpoint still stands in for the head"


# ---- #4: coverage -- nothing disappears between the layers ---------------


def test_no_message_disappears_when_l1_inserts_a_placeholder():
    """60 messages, a 50-message cap: request ∪ summary == all 60."""
    calls: list[str] = []
    manager = _manager(_recorder(calls), max_messages=50)
    history = _chat(60, "MSG")

    out = asyncio.run(manager.compress(history))

    covered = _text_of(out) + "\n" + "\n".join(calls)
    missing = [i for i in range(60) if f"MSG-{i} " not in covered]
    assert missing == [], f"messages {missing} reached neither the request nor the summary"


# ---- #5: a rewritten history invalidates the checkpoint ------------------


def test_a_rewritten_history_invalidates_the_checkpoint():
    calls: list[str] = []
    manager = _manager(_recorder(calls))
    session = _session()
    history = _chat(40, "第一批")

    asyncio.run(manager.compress(history, session=session, force=True))
    calls.clear()

    rewritten = list(history)
    rewritten[0] = ChatMessage(role="user", content="REWRITTEN-0 " + "y" * 30, timestamp="")
    asyncio.run(
        manager.compress([*rewritten, *_chat(30, "第二批", start=40)], session=session, force=True)
    )

    assert len(calls) == 1
    assert "REWRITTEN-0" in calls[0], (
        "the history no longer matches the anchor, so the checkpoint must be "
        "rebuilt from the whole history -- not carried forward"
    )


# ---- #6: /compact's note is a checkpoint too ----------------------------


def test_a_compact_note_is_picked_up_as_the_checkpoint():
    """``/compact`` replaces ``session.messages`` with ``[note, *tail]``.

    The session fields know nothing about that rewrite, so the note at index 0
    is the only evidence -- and without reading it the next request would
    re-summarise the whole history from scratch.
    """
    calls: list[str] = []
    manager = _manager(_recorder(calls))
    note = ChatMessage(
        role="user", content=f"{CHECKPOINT_MARK}\n{FIRST_SUMMARY}", timestamp=""
    )
    history = [
        note,
        *[ChatMessage(role="user", content=f"新-{i}", timestamp="") for i in range(12)],
    ]

    asyncio.run(manager.compress(history, session=_session(), force=True))

    assert len(calls) == 1
    assert OLD_MARKER not in calls[0]
    assert CHECKPOINT_MARK not in calls[0], (
        "the note stands for the old history; feeding it back as conversation "
        "is how a checkpoint gets summarised into itself"
    )
    assert "新-" in calls[0], "the work since the note is summarised"


# ---- #7: an oversized delta is chunked, never clipped -------------------


def test_an_oversized_delta_is_summarized_in_chunks_not_truncated():
    calls: list[str] = []
    manager = _manager(_recorder(calls), max_context_tokens=200_000)
    history = _chat(8, "BIG", size=25_000)

    out = asyncio.run(manager.compress(history, force=True))

    assert len(calls) >= 3, f"150k+ characters cannot fit one call; got {len(calls)}"
    covered = "\n".join(calls) + "\n" + _text_of(out)
    missing = [i for i in range(8) if f"BIG-{i} " not in covered]
    assert missing == [], f"messages {missing} were dropped instead of chunked"


# ---- #8: the summariser sees tool structure -----------------------------


def test_the_summary_input_keeps_tool_structure():
    """A flat ``[role] body`` view cannot tell "ran the tests" from "meant to"."""
    calls: list[str] = []
    manager = _manager(_recorder(calls), l4_tail_tokens=20)
    history = [
        ChatMessage(role="user", content="跑一下测试", timestamp=""),
        ChatMessage(
            role="assistant",
            content="",
            timestamp="",
            tool_calls=(
                ToolCallRef(id="c1", name="bash", arguments='{"command": "pytest -q"}'),
            ),
        ),
        ChatMessage(
            role="tool",
            content="3 failed",
            timestamp="",
            tool_name="bash",
            tool_call_id="c1",
        ),
        ChatMessage(role="user", content="继续", timestamp=""),
        ChatMessage(role="assistant", content="好的", timestamp=""),
        ChatMessage(role="user", content="最后", timestamp=""),
    ]

    asyncio.run(manager.compress(history, force=True))

    assert calls
    assert "[tool:bash]" in calls[0]
    assert 'bash({"command": "pytest -q"})' in calls[0]


# ---- #9 / #10: the quality gate ----------------------------------------


def test_a_summary_missing_sections_is_rejected():
    assert summary_shape_ok("## 目标\n找一个 bug") is False
    # Five sections in order, but not starting at the top: a reply that picked
    # up the template mid-way is not a checkpoint of this conversation.
    assert (
        summary_shape_ok(
            "## 决策\n无\n## 工作状态\n无\n## 已尝试且失败\n无\n## 下一步\n无\n## 关键文件\n无"
        )
        is False
    )


def test_a_preamble_before_the_first_section_is_still_accepted():
    body = checkpoint_body("好的，我总结了一下。\n\n" + FIRST_SUMMARY)
    assert body is not None
    assert summary_shape_ok(body) is True


# ---- #14: the driver's pytest feedback ----------------------------------


def _pytest_digest():
    script = Path(__file__).resolve().parent.parent / "scripts" / "eval_fullstack_task.py"
    spec = importlib.util.spec_from_file_location("_eval_fullstack_task", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module._pytest_digest


def test_pytest_feedback_carries_the_failed_list():
    lines = [
        "============================= test session starts =============================",
        "collected 18 items",
        "=========================== FAILURES ===========================",
        "__________________________ test_case_0 __________________________",
        ">       assert 1 == 0",
        "E       assert 1 == 0",
        "tests/test_api.py:5: AssertionError",
        "========================= warnings summary =========================",
        "  /x/y.py:3: PytestDeprecationWarning: something old",
        "========================= short test summary info ==========================",
        *[f"FAILED tests/test_api.py::test_case_{i} - AssertionError" for i in range(18)],
        "======================== 18 failed in 0.42s ========================",
    ]

    digest = _pytest_digest()("\n".join(lines))

    assert digest.count("FAILED ") == 18, "the model needs the index of every failure"
    assert "PytestDeprecationWarning" not in digest
    assert "test_case_0" in digest, "the first traceback explains the first failure"
    assert "18 failed" in digest


# ---- #15: the attempts ledger ------------------------------------------


def test_the_digest_records_what_was_attempted():
    outcome = TurnOutcome(
        text=None, status=TurnStatus.MAX_ITERATIONS, reason="budget", iterations=9
    )
    digest = build_digest(
        turn=3,
        user_input="修 SQL",
        outcome=outcome,
        failures=[
            ("bash", "nonzero_exit", "18 failed"),
            ("edit", "no_match", ""),
        ],
    )

    assert len(digest.attempts) == 2
    assert "bash → nonzero_exit" in digest.attempts[0]
    block = render_working_memory([digest], current_turn=4, compacted=True)
    assert "试过:" in block
    assert "bash → nonzero_exit" in block


# ---- #16: the estimate counts what the request carries ------------------


def test_history_tokens_counts_tool_arguments():
    bare = [ChatMessage(role="assistant", content="ok", timestamp="")]
    write = [
        ChatMessage(
            role="assistant",
            content="ok",
            timestamp="",
            tool_calls=(ToolCallRef(id="c1", name="write", arguments="x" * 10_000),),
        )
    ]

    assert _history_tokens(write) - _history_tokens(bare) >= 2_000


# ---- #17: a failed summary drops nothing -------------------------------


def test_a_failed_summary_does_not_drop_the_delta():
    calls: list[str] = []
    manager = _manager(_recorder(calls, reply=None), l4_tail_tokens=30)
    history = _chat(20, "KEEP", size=20)
    session = _session()
    install_checkpoint(
        session, FIRST_SUMMARY, consumed=5, anchor=_anchor_of(history[:5])
    )

    out = asyncio.run(manager.compress(history, session=session))

    assert calls, "the summariser was asked"
    body = _text_of(out)
    # The old checkpoint still stands for messages[:5]; everything after it must
    # be verbatim -- the failed call may not swallow the delta.
    assert body.startswith(CHECKPOINT_MARK)
    missing = [i for i in range(5, 20) if f"KEEP-{i} " not in body]
    assert missing == [], "a failed summary must leave the delta untouched"


def test_a_tail_that_starts_before_the_boundary_is_cut_at_the_boundary():
    """``split_tail`` can cut *inside* the covered prefix.

    Its fallback anchors on the latest user message; when that sits before
    ``consumed`` the "tail" contains messages the checkpoint already stands
    for, and ``delta`` would come out empty -- the checkpoint then never
    advances while the same messages are re-sent every request.
    """
    calls: list[str] = []
    manager = _manager(_recorder(calls), l4_tail_tokens=10_000)
    history = [
        ChatMessage(role="user", content="U-0", timestamp=""),
        ChatMessage(role="assistant", content="U-1", timestamp=""),
        ChatMessage(role="user", content="U-2", timestamp=""),
        ChatMessage(role="assistant", content="U-3", timestamp=""),
        ChatMessage(role="assistant", content="U-4", timestamp=""),
        ChatMessage(role="assistant", content="U-5", timestamp=""),
    ]
    session = _session()
    install_checkpoint(
        session, FIRST_SUMMARY, consumed=4, anchor=_anchor_of(history[:4])
    )

    out = asyncio.run(manager.compress(history, session=session))

    body = _text_of(out)
    assert "U-2" not in body and "U-3" not in body, "covered by the checkpoint"
    assert "U-4" in body and "U-5" in body


def test_paging_says_what_it_dropped_and_archives_it(tmp_path):
    """The one place a message can still disappear: the request must fit.

    Paging only runs after the summary failed and the history is still over
    target, so what it removes is *not* covered by the checkpoint.  Dropping is
    unavoidable; dropping silently is not -- the model would keep reasoning as
    if it had read those messages.
    """
    calls: list[str] = []
    manager = _manager(
        _recorder(calls, reply=None),
        root=str(tmp_path),
        max_context_tokens=2_000,
        l4_tail_tokens=200,
    )
    history = _chat(40, "D", size=300)
    session = _session()
    install_checkpoint(
        session, FIRST_SUMMARY, consumed=4, anchor=_anchor_of(history[:4])
    )

    out = asyncio.run(manager.compress_if_needed(history, session=session))

    body = _text_of(out)
    assert body.count(OMISSION_MARK) == 1, "one call, one declaration"
    assert OMISSION_MARK in body, "a silent drop is the bug this test exists for"
    archived = list((tmp_path / "transcripts" / "archive").glob("*.md"))
    assert archived, "the dropped messages must stay reachable"
    text = "\n".join(p.read_text(encoding="utf-8") for p in archived)
    assert "D-4 " in text, "the first message paging removed is on disk"


def test_a_first_compaction_does_not_claim_to_cover_the_verbatim_tail():
    """The checkpoint's ``consumed`` must stop where the verbatim tail starts.

    Measured on eval-08 T2: L2 rebuilt the tail's objects, so
    ``_delta_by_identity`` matched nothing and ``_cut_index`` returned
    ``len(l4_source)`` -- a checkpoint claiming to cover everything, after
    which the next request carried only the note (323 tokens) and the model
    re-read the same files four times.
    """
    calls: list[str] = []
    manager = _manager(
        _recorder(calls), l4_tail_tokens=200, keep_recent_tool_results=1
    )
    history: list[ChatMessage] = []
    for i in range(6):
        history.append(ChatMessage(role="user", content=f"U{i}", timestamp=""))
        history.append(
            ChatMessage(
                role="assistant",
                content="",
                timestamp="",
                tool_calls=(ToolCallRef(id=f"c{i}", name="bash", arguments="{}"),),
            )
        )
        history.append(
            ChatMessage(
                role="tool",
                content=f"R{i} " + "x" * 800,
                timestamp="",
                tool_name="bash",
                tool_call_id=f"c{i}",
            )
        )
    session = _session()

    out = asyncio.run(manager.compress(history, session=session, force=True))

    consumed = session.compaction_consumed
    assert 0 < consumed < len(history), "the tail must stay outside the checkpoint"
    body = _text_of(out)
    for index, message in enumerate(history):
        if index < consumed:
            continue
        marker = (message.content or "").split(" ")[0]
        assert marker in body, (
            f"message {index} is outside the checkpoint but not in the request"
        )


def test_a_tool_heavy_turn_keeps_its_tail_budget():
    """One user message + 30 steps must not collapse the tail to one message.

    The old rule backtracks to a user boundary unconditionally, and an agent
    turn has exactly one user message -- at index 0.  Measured on eval-08 T2
    (after the checkpoint fix): a 12k tail budget left 323-2,600 tokens in the
    request, so the model re-read ``agent_loop.py`` four times.
    """
    manager = _manager(_recorder([]), max_context_tokens=48_000, l4_tail_tokens=12_000)
    history: list[ChatMessage] = [ChatMessage(role="user", content="找缺陷", timestamp="")]
    for i in range(15):
        history.append(
            ChatMessage(
                role="assistant",
                content="",
                timestamp="",
                tool_calls=(ToolCallRef(id=f"c{i}", name="bash", arguments='{"command":"x"}'),),
            )
        )
        history.append(
            ChatMessage(
                role="tool",
                content=f"R{i} " + "x" * 4_000,
                timestamp="",
                tool_name="bash",
                tool_call_id=f"c{i}",
            )
        )

    head, tail = manager.split_tail(history)

    assert _history_tokens(tail) >= 6_000, "at least half the 12k budget must survive"
    assert tail[0].role != "tool", "the tail must not start orphaned"
    assert head, "something must still be summarised"


def test_the_omission_count_ignores_rebuilt_messages():
    """``sanitize_pairs`` rebuilds assistant carriers; a survivor is not a drop."""
    manager = _manager(_recorder([]))
    carrier = ChatMessage(
        role="assistant",
        content="",
        timestamp="",
        tool_calls=(ToolCallRef(id="c1", name="bash", arguments="{}"),),
    )
    result = ChatMessage(
        role="tool", content="R", timestamp="", tool_name="bash", tool_call_id="c1"
    )
    before = [*_chat(10, "X"), carrier, result]
    after = sanitize_pairs([carrier, result])
    assert after[0] is not carrier, "the rebuild really happens"

    out = manager._announce_omissions(before, after)

    assert out[0].content.startswith(OMISSION_MARK)
    assert "已省略 10 条" in out[0].content, (
        "the carrier survived; counting it as dropped inflates the number the "
        "model reads and archives a message that is still in the request"
    )


def test_a_user_message_quoting_the_checkpoint_marker_is_not_a_note():
    """``hard_trim`` pins what ``split_checkpoint`` calls a note -- nothing else."""
    quoted = ChatMessage(
        role="user", content=f"{CHECKPOINT_MARK} 这段是用户粘的，不是摘要", timestamp=""
    )
    assert _is_checkpoint_note(quoted) is False

    real = ChatMessage(role="user", content=f"{CHECKPOINT_MARK}\n{FIRST_SUMMARY}", timestamp="")
    assert _is_checkpoint_note(real) is True


# ---- #18: what L4 consumed stays searchable ----------------------------


def test_the_l4_consumed_head_is_archived_for_history_search(tmp_path):
    calls: list[str] = []
    manager = _manager(_recorder(calls), root=str(tmp_path), l4_tail_tokens=20)
    history = _chat(10, "ARCH", size=20)

    asyncio.run(manager.compress(history, force=True))

    archived = list((tmp_path / "transcripts" / "archive").glob("*.md"))
    assert archived, "history_search promises the originals are on disk"
    body = "\n".join(path.read_text(encoding="utf-8") for path in archived)
    assert "ARCH-0 " in body


# ---- review findings (v3) ----------------------------------------------


def test_a_tool_heavy_tail_keeps_the_message_that_declares_its_call():
    """The end of a tool-heavy turn is ``… assistant(c1), tool1``.

    ``split_tail`` backtracks to a user boundary; when the last user message is
    older than the tail budget the tail collapses onto a single ``tool``
    message, whose declaring assistant sits in the head.  ``sanitize_pairs``
    then deletes it -- the newest result would reach neither the request nor
    the summary.
    """
    history = [
        ChatMessage(role="user", content="开始", timestamp=""),
        ChatMessage(
            role="assistant",
            content="",
            timestamp="",
            tool_calls=(ToolCallRef(id="c1", name="bash", arguments='{"command":"ls"}'),),
        ),
        ChatMessage(
            role="tool", content="RESULT-LATEST", timestamp="", tool_name="bash",
            tool_call_id="c1",
        ),
    ]

    # A tail budget of 1 token cannot even hold the tool result.
    out = asyncio.run(
        _manager(_recorder([]), l4_tail_tokens=1).compress(history, force=True)
    )

    assert "RESULT-LATEST" in _text_of(out), (
        "the newest tool result must survive sanitize_pairs; an orphaned tail "
        "is a deletion, not a compaction"
    )
    assert all(m.role != "tool" or m.tool_call_id == "c1" for m in out)
    assert out == sanitize_pairs(out)


def test_a_hard_trim_keeps_the_checkpoint_note():
    """The note is one message standing for the whole head."""
    note = ChatMessage(role="user", content=f"{CHECKPOINT_MARK}\n{FIRST_SUMMARY}", timestamp="")
    tail = _chat(10, "T", size=10)

    trimmed = hard_trim([note, *tail], keep_tail=3)

    assert trimmed and trimmed[0].content.startswith(CHECKPOINT_MARK)
    assert len(trimmed) == 4


def test_the_checkpoint_path_honours_the_circuit_breaker():
    """Three failures must stop the retries -- in *this* branch too."""
    calls: list[str] = []
    manager = _manager(_recorder(calls, reply=None), l4_tail_tokens=30)
    history = _chat(20, "B", size=20)
    session = _session()
    session.compaction_failures = 3
    install_checkpoint(
        session, FIRST_SUMMARY, consumed=5, anchor=_anchor_of(history[:5])
    )

    out = asyncio.run(manager.compress(history, session=session))

    assert calls == [], "an open breaker must not spend another summary call"
    assert "B-5 " in _text_of(out), "and the delta is still carried verbatim"


def test_a_history_that_fits_the_tail_is_not_sent_twice():
    """A single-user history makes ``split_tail`` hand everything back.

    The fallback anchors the verbatim tail on the latest user message; with
    only one user message that is index 0, so ``tail`` becomes the whole
    history.  Sending it *and* the checkpoint would pay for ``messages[:used]``
    twice and break branch C's "only the delta".
    """
    calls: list[str] = []
    manager = _manager(_recorder(calls), l4_tail_tokens=10_000)
    history = [
        ChatMessage(role="user", content="S-0 唯一的指令", timestamp=""),
        *[
            ChatMessage(role="assistant", content=f"S-{i} 继续", timestamp="")
            for i in range(1, 6)
        ],
    ]
    session = _session()
    install_checkpoint(
        session, FIRST_SUMMARY, consumed=4, anchor=_anchor_of(history[:4])
    )

    out = asyncio.run(manager.compress(history, session=session))

    body = _text_of(out)
    assert "S-0 " not in body, "the checkpoint already stands for messages[:4]"
    assert "S-4 " in body and "S-5 " in body


def test_a_snip_placeholder_in_the_tail_does_not_shift_the_covered_prefix():
    """``len(source) - len(tail)`` counted the placeholder as a real message.

    With L1's marker inside the verbatim tail the subtraction landed two
    messages early: 14 and 15 were summarised, but the recorded ``consumed``
    said they were not covered, so the next request summarised them again (and
    a checkpoint with a slightly different boundary could drop them outright).
    """
    calls: list[str] = []
    manager = _manager(
        _recorder(calls), max_messages=8, snip_keep_tail=4, l4_tail_tokens=40
    )
    history = _chat(20, "P", size=20)
    session = _session()

    out = asyncio.run(manager.compress(history, session=session, force=True))

    consumed = session.compaction_consumed
    assert consumed > 0
    body = _text_of(out)
    for index, message in enumerate(history):
        if index < consumed:
            continue
        marker = (message.content or "").split(" ")[0]
        assert marker in body, (
            f"message {index} is outside the checkpoint but not in the request"
        )
