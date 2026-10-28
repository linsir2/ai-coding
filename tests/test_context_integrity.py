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
    ContextManager,
    _anchor_of,
    _history_tokens,
    install_checkpoint,
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
        "summary_delta_tokens": 0,  # any non-empty delta earns a summary
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
    missing = [i for i in range(20) if f"KEEP-{i} " not in body]
    assert missing == [], "a failed summary must leave the history untouched"


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
