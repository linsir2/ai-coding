"""One question, one answer: what counts as a checkpoint.

The compaction list said "the summary is never validated", and that was wrong —
`summary_is_usable` has been there since G5-3. The real defect was that the
question had *two* answers in *two* modules:

    llm_helpers.summary_is_usable   any(section in body)      substring — wide
    core.split_checkpoint          startswith(SUMMARY_...)   prefix    — narrow

and they disagreed. Measured, on the shapes a model actually produces:

    好的，我来总结一下：\\n\\n## 目标\\n…      accepted, NOT recognised
    这个项目的 ## 目标 我已经梳理过了          accepted, NOT recognised

The first is not an edge case — the prompt says "不要前言" and models write one
anyway. It got installed as the checkpoint, and on the next compaction the
merger did not recognise it, so R11's entire feature went silently missing for
that session.

Same shape as M11-0g, where the meter and the gate each answered "which usage
still describes the live context" on their own. The fix is the same: one
definition, two readers. Neither is an enforcement point that can disagree —
both ask.
"""

import asyncio
import tempfile
from pathlib import Path

import ai_coding.ai.llm_helpers as llm_helpers
from ai_coding.ai.llm_helpers import (
    SUMMARY_SECTIONS,
    build_summarizer,
    checkpoint_body,
    summary_within_growth_bound,
)
from ai_coding.config.models import AIConfig
from ai_coding.core.context_compression import (
    CHECKPOINT_MARK,
    ContextManager,
    snip_middle,
    split_checkpoint,
)
from ai_coding.domain.message import ChatMessage

PREAMBLE = "好的，我来总结一下：\n\n"
PROSE = "这个项目的 ## 目标 我已经梳理过了。"

#: A complete reply.  I2 made the acceptance check require the template's shape
#: (``summary_shape_ok``): a reply that stopped after ``## 目标`` used to be
#: installed as the whole record of a session.  This fixture is the "good"
#: answer, so it has to be one.
GOOD = "\n".join(f"{section}\n无" for section in SUMMARY_SECTIONS)

_note = lambda body: ChatMessage(  # noqa: E731
    role="user", content=f"{CHECKPOINT_MARK}\n{body}", timestamp=""
)


def _full() -> str:
    return "\n".join(f"{section}\n无" for section in SUMMARY_SECTIONS)


# ---- R: the disagreement itself, as an invariant ---------------------------


def test_acceptance_and_recognition_never_disagree():
    """The whole defect, written as one assertion. Any shape that the adapter
    would install has to be a shape the compressor can find again next time."""
    candidates = [
        GOOD,
        PREAMBLE + GOOD,
        PROSE,
        _full(),
        "## 决策\n用了 uv",
        "### 已完成\n只有子标题",
        "## 目标  \n尾随空格",
        "",
        "   \n  ",
    ]
    for text in candidates:
        accepted = checkpoint_body(text) is not None
        recognised = split_checkpoint([_note(text)])[0] != ""
        assert accepted == recognised, (
            f"accepted={accepted} but recognised={recognised} for {text[:40]!r}"
        )


def test_a_preamble_is_stripped_rather_than_rejected():
    """Rejecting costs a paid retry and, if it fails again, the summary. The
    cost is asymmetric in the docstring's own terms, and a preamble is not a
    reason to throw away good content."""
    body = checkpoint_body(PREAMBLE + GOOD)

    assert body == GOOD
    assert "好的，我来总结一下" not in (body or "")


def test_prose_that_mentions_a_heading_is_not_a_checkpoint():
    assert checkpoint_body(PROSE) is None


def test_what_gets_written_to_history_is_already_canonical():
    """The seam returns the canonical body, so the note never carries the
    preamble forward into the next merge."""
    captured: list[tuple[str, int]] = []

    async def fake_chat(model_config, system, user, max_tokens):
        captured.append((user, max_tokens))
        return PREAMBLE + GOOD

    original = llm_helpers._chat
    llm_helpers._chat = fake_chat
    try:
        summarize = build_summarizer(object())
        result = asyncio.run(summarize("历史原文"))
    finally:
        llm_helpers._chat = original

    assert result == GOOD
    assert result is not None and result.startswith("## ")


# ---- I: what must not move -----------------------------------------------


def test_a_clean_checkpoint_comes_back_untouched():
    assert checkpoint_body(GOOD) == GOOD


def test_nothing_at_all_is_not_a_checkpoint():
    for empty in ("", "   \n  ", "\n\n"):
        assert checkpoint_body(empty) is None


def test_a_subheading_does_not_open_a_checkpoint():
    """`## ` is a substring of `### `, so a substring test would accept
    `### 已完成` as the whole 工作状态 section. An exact line cannot."""
    assert checkpoint_body("### 已完成\n干完了") is None


def test_trailing_whitespace_does_not_hide_a_heading():
    assert checkpoint_body("## 目标  \n内容") == "## 目标  \n内容"


def test_a_clean_history_still_splits_to_nothing():
    messages = [ChatMessage(role="user", content="普通消息", timestamp="")]

    assert split_checkpoint(messages) == ("", messages)


def test_the_marker_alone_is_not_enough():
    """`split_checkpoint` gained a shape check in R11. It must still defer to
    the shared definition rather than keeping a third copy of it."""
    assert split_checkpoint([_note(PROSE)]) == ("", [_note(PROSE)])


# ---- P: the machinery around it still works -------------------------------


def test_a_reply_without_the_shape_is_retried_with_the_nudge():
    calls: list[str] = []

    async def fake_chat(model_config, system, user, max_tokens):
        calls.append(user)
        return "好的，这段对话主要讲了压缩" if len(calls) == 1 else GOOD

    original = llm_helpers._chat
    llm_helpers._chat = fake_chat
    try:
        result = asyncio.run(build_summarizer(object())("历史"))
    finally:
        llm_helpers._chat = original

    assert result == GOOD
    assert len(calls) == 2
    assert calls[1] != calls[0], "the retry must say what was wrong, not repeat the ask"


def test_two_bad_replies_give_up_rather_than_loop():
    calls: list[str] = []

    async def fake_chat(model_config, system, user, max_tokens):
        calls.append(user)
        return "还是不行"

    original = llm_helpers._chat
    llm_helpers._chat = fake_chat
    try:
        result = asyncio.run(build_summarizer(object())("历史"))
    finally:
        llm_helpers._chat = original

    assert result is None
    assert len(calls) == 2


def test_the_growth_bound_still_gates_a_merge():
    """Untouched by this change, and worth pinning here because it is the other
    thing that stands between the model and the history."""
    assert summary_within_growth_bound(0, "x" * 99_000) is True
    assert summary_within_growth_bound(5_000, "x" * 99_000) is False


def test_a_merger_still_recognises_a_note_written_before_this_change():
    """Sessions compacted by the previous commit carry preambles, because
    nothing stripped them. Tolerating them here is the upgrade path; rejecting
    them would silently downgrade every existing session to first-compaction
    semantics."""
    legacy = _note(PREAMBLE + GOOD)

    checkpoint, rest = split_checkpoint(
        [legacy, ChatMessage(role="user", content="之后的", timestamp="")]
    )

    assert checkpoint == GOOD
    assert len(rest) == 1


def test_the_compressor_still_installs_exactly_one_checkpoint():
    def manager() -> ContextManager:
        async def summarizer(text: str) -> str:
            return PREAMBLE + GOOD

        return ContextManager(
            AIConfig(max_context_tokens=3000, max_messages=12),
            transcripts_root=Path(tempfile.mkdtemp()),
            summarizer=summarizer,
        )

    history = [
        ChatMessage(role="user" if i % 2 == 0 else "assistant",
                    content=f"第{i}轮 " + "内容" * 200, timestamp="")
        for i in range(40)
    ]
    for _ in range(3):
        history = asyncio.run(manager().compress([*history, *_history_tail()]))

    assert sum(1 for m in history if CHECKPOINT_MARK in (m.content or "")) == 1


def _history_tail() -> list[ChatMessage]:
    return [
        ChatMessage(role="user" if i % 2 == 0 else "assistant",
                    content=f"新增{i}轮 " + "内容" * 200, timestamp="")
        for i in range(30)
    ]

# ---- the trim must never duplicate -----------------------------------------


def test_a_trim_that_would_overlap_head_and_tail_does_not_trim():
    """Measured live: with max_messages=12 and 15 messages, snip_middle copied
    the head — `第34轮`/`第35轮` each appeared twice — and a checkpoint at the
    head was installed twice, so the session carried two verdicts. It also
    printed `[snipped -7 messages]`, a negative count.

    Pre-existing, not from R11: `git stash` reproduces it. Default config never
    reaches the case (max_messages=400 > keep_head+keep_tail=23), but a smaller
    max_messages does, and duplication is data corruption where going over budget
    is only temporary.
    """
    messages = [ChatMessage(role="user", content=f"m{i}", timestamp="") for i in range(15)]

    out = snip_middle(messages, max_messages=12, keep_head=3, keep_tail=20)

    assert out == messages


def test_a_history_with_a_real_middle_is_still_trimmed():
    """The I case: the ordinary shape must not move. 40 messages, a middle that
    genuinely exists, one placeholder, head and tail intact."""
    messages = [ChatMessage(role="user", content=f"m{i}", timestamp="") for i in range(40)]

    out = snip_middle(messages, max_messages=12, keep_head=3, keep_tail=20)

    assert out[:3] == messages[:3]
    assert out[-20:] == messages[-20:]
    assert len(out) == 24
    assert "[snipped" in out[3].content


def test_a_history_within_the_budget_is_left_alone():
    messages = [ChatMessage(role="user", content=f"m{i}", timestamp="") for i in range(5)]

    assert snip_middle(messages, max_messages=12, keep_head=3, keep_tail=20) == messages
