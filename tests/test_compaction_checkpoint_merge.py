"""A compaction checkpoint must be *updated*, not rewritten from beside it.

Measured, not assumed: the previous summary was already reaching the summariser
on the second pass — the pipeline was never broken. It arrived as
`[user] [以下为早期对话摘要]…`, which is indistinguishable from a message the
user typed, under a system prompt that said "compress this conversation". So the
model summarised the summary the way it summarises anything else, and every
further compaction rewrote it from scratch with nothing asking it to carry
anything forward.

The fix is framing, not plumbing: split the checkpoint out of the conversation
and label both halves, so the model is told what each one *is* rather than asked
to recognise a marker string.

The three questions this file keeps honest:
- does the split actually happen (R),
- does the unchanged first compaction stay bit-identical (I),
- and does the check cost one extra model call at worst, never an unbounded one.
"""

import asyncio
import tempfile
from pathlib import Path

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
    split_checkpoint,
)
from ai_coding.domain.message import ChatMessage

FIRST_SUMMARY = "## 目标\n把审计做完\n\n## 约束与要求\n不许改测试\n\n## 已完成\n建了索引"
CANARY_OLD = "CANARY-OLD-1111"


def _manager(summarizer, **overrides) -> ContextManager:
    return ContextManager(
        AIConfig(**{"max_context_tokens": 3000, "max_messages": 12, **overrides}),
        transcripts_root=Path(tempfile.mkdtemp()),
        summarizer=summarizer,
    )


def _chat(n: int, tag: str, size: int = 200) -> list[ChatMessage]:
    return [
        ChatMessage(role="user" if i % 2 == 0 else "assistant",
                    content=f"{tag}-{i} " + "内容" * size, timestamp="")
        for i in range(n)
    ]


def _recorder(sink: list[str]):
    async def summarizer(text: str) -> str:
        sink.append(text)
        return FIRST_SUMMARY

    return summarizer


def _checkpointed(sink: list[str]) -> tuple[ContextManager, list[ChatMessage]]:
    """One compaction, so the next call has a checkpoint to find."""
    manager = _manager(_recorder(sink))
    return manager, asyncio.run(manager.compress(_chat(40, "第一批")))


# ---- R: the split has to actually happen -----------------------------------


def test_the_checkpoint_is_labelled_apart_from_the_conversation(tmp_path):
    sink: list[str] = []
    manager, compacted = _checkpointed(sink)
    sink.clear()

    asyncio.run(manager.compress([*compacted, *_chat(30, "第二批")]))

    assert len(sink) == 1, "the second pass must reach the summariser"
    assert "<previous-checkpoint>" in sink[0]
    assert "<conversation-since-checkpoint>" in sink[0]


def test_the_checkpoint_is_no_longer_a_user_message_in_the_middle(tmp_path):
    """This is the defect. The old note rode in as `[user] [以下为早期对话摘要]…`,
    so the model had no way to know it was a verdict rather than a remark."""
    sink: list[str] = []
    manager, compacted = _checkpointed(sink)
    sink.clear()

    asyncio.run(manager.compress([*compacted, *_chat(30, "第二批")]))

    assert f"[user] {CHECKPOINT_MARK}" not in sink[0], (
        "the checkpoint must not be rendered as a user message; that is exactly "
        "what made it unreadable"
    )


def test_the_previous_verdict_reaches_the_summariser_intact():
    sink: list[str] = []
    manager, compacted = _checkpointed(sink)
    sink.clear()

    asyncio.run(manager.compress([*compacted, *_chat(30, "第二批")]))

    assert CANARY_OLD not in FIRST_SUMMARY  # guard the fixture itself
    assert "把审计做完" in sink[0], "the previous verdict rides in whole"
    assert "第一批-" in sink[0], (
        "what happened since the checkpoint rides beside it — first-batch "
        "content outside the verbatim tail the first compaction kept"
    )
    assert "第二批-0" in sink[0], "and the new conversation beside that"


def test_truncation_says_so_instead_of_ending_in_silence():
    """`_MAX_SUMMARY_CHARS` sliced the input with no marker, so a third
    compaction could drop its tail and leave no evidence that it had."""
    from ai_coding.ai.llm_helpers import _MAX_SUMMARY_CHARS

    truncated = "x" * (_MAX_SUMMARY_CHARS + 500)

    assert "[历史过长，此处截断]" in _clip(truncated)


# ---- I: the first compaction must not move ----------------------------------


def test_a_history_with_no_checkpoint_is_untouched():
    messages = _chat(20, "干净")

    checkpoint, rest = split_checkpoint(messages)

    assert checkpoint == ""
    assert rest == messages


def test_the_first_pass_produces_exactly_the_old_text():
    """The I case that matters: with no checkpoint there is nothing to label, so
    the head must be the plain joined transcript it has always been — minus
    exactly the tail that survives verbatim (summarising what stays would be
    paying twice for the same words)."""
    from ai_coding.core.context_compression import _joined_history_text

    sink: list[str] = []
    manager = _manager(_recorder(sink))

    history = _chat(40, "第一批")
    out = asyncio.run(manager.compress(history))

    assert "<previous-checkpoint>" not in sink[0]
    assert "<conversation-since-checkpoint>" not in sink[0]
    kept = len(out) - 1  # the note plus the verbatim tail
    assert kept >= 1, "the tail never vanishes"
    assert sink[0] == _joined_history_text(history[: len(history) - kept])
    assert [m.content for m in out[1:]] == [m.content for m in history[len(history) - kept :]]


def test_only_the_head_checkpoint_counts():
    """A user pasting the marker into their message must not be mistaken for our
    own checkpoint — the string is public and a pasted log can contain it."""
    fake = ChatMessage(role="user", content=f"{CHECKPOINT_MARK}\n用户自己贴的日志", timestamp="")

    checkpoint, rest = split_checkpoint([fake, *_chat(4, "后面")])

    assert checkpoint == ""
    assert fake in rest


def test_a_short_history_is_not_sliced_into_pieces():
    messages = _chat(3, "短")

    assert split_checkpoint(messages)[1] == messages


# ---- three compactions leave one checkpoint, not three ----------------------


def test_repeated_compaction_never_stacks_checkpoints():
    sink: list[str] = []
    manager = _manager(_recorder(sink))
    history = _chat(40, "第一批")

    for round_index in range(3):
        history = asyncio.run(manager.compress([*history, *_chat(30, f"第{round_index + 2}批")]))

    assert sum(1 for m in history if CHECKPOINT_MARK in (m.content or "")) == 1


# ---- the growth bound the user asked for -----------------------------------


def test_a_summary_that_runs_away_from_its_predecessor_is_rejected():
    assert summary_within_growth_bound(len("x" * 100), "y" * 5000) is False


def test_a_legitimate_merge_survives_the_first_merge():
    """The floor exists for this. A seven-section template is fatter than the
    four-section one it replaces, so a first merge can legitimately more than
    double its predecessor — rejecting that would drop the summary entirely."""
    previous = 200

    assert summary_within_growth_bound(previous, "y" * 500) is True


def test_the_first_summary_is_unbounded_because_it_has_no_predecessor():
    assert summary_within_growth_bound(0, "y" * 5000) is True


# ---- the section contract ---------------------------------------------------


def test_the_template_carries_the_eight_sections():
    """I2 adds ``## 已尝试且失败``: eval-08's full-stack task re-tried one root
    cause five times because nothing carried "I already moved that table and it
    still failed" forward.  Claude Code's template has the same section."""
    assert SUMMARY_SECTIONS == (
        "## 目标",
        "## 约束与要求",
        "## 决策",
        "## 工作状态",
        "## 已尝试且失败",
        "## 下一步",
        "## 关键文件",
        "## 重要上下文",
    )


def test_a_reply_with_any_section_still_counts_as_a_summary():
    # `summary_is_usable` was replaced by `checkpoint_body` (R14): one
    # definition, read by the summariser and by the compressor, instead of a
    # substring test and a prefix test that disagreed. The three cases below are
    # unchanged — only the name they ask is.
    assert checkpoint_body("## 决策\n用了 uv") == "## 决策\n用了 uv"
    assert checkpoint_body("好的，我总结了一下") is None
    assert checkpoint_body("") is None


def test_the_merged_prompt_names_both_halves_and_the_merge_rules():
    """The prompt is the whole mechanism, so it is pinned as a contract rather
    than trusted to stay right while the code around it changes."""
    captured: list[tuple[str, str]] = []

    async def fake_chat(model_config, system, user, max_tokens):
        captured.append((system, user))
        return "## 目标\n合并后的结论"

    import ai_coding.ai.llm_helpers as helpers

    original = helpers._chat
    helpers._chat = fake_chat
    try:
        summarize = build_summarizer(object())
        asyncio.run(
            summarize("<previous-checkpoint>\n旧结论\n</previous-checkpoint>", previous="旧结论")
        )
    finally:
        helpers._chat = original

    system, user = captured[0]
    assert "<previous-checkpoint>" in system and "<conversation-since-checkpoint>" in system
    assert "更新" in system, "the model must be told this is an update, not a fresh summary"
    assert "旧结论" in user
    assert user.count("旧结论") >= 1


def _clip(text: str) -> str:
    from ai_coding.ai.llm_helpers import _clip_for_summary

    return _clip_for_summary(text)

def test_the_work_state_subheadings_sit_under_their_parent_in_the_prompt():
    """A live run appended them to whichever section came last. The prompt
    listed all seven headings first and the three sub-headings after them, so
    the model read the sub-headings as trailing, not nested — and produced

        ## 重要上下文
        ### 已完成
        ### 进行中
        ### 阻塞

    which reads as one section and leaves 工作状态 empty. Order in a template
    is structure, not formatting, so it is pinned here."""
    from ai_coding.ai.llm_helpers import _SUMMARY_SYSTEM_PROMPT

    body = _SUMMARY_SYSTEM_PROMPT
    parent = body.index("## 工作状态")
    first_child = body.index("### 已完成")
    next_section = body.index("## 下一步")

    assert parent < first_child < next_section, (
        "the sub-headings belong between 工作状态 and the next top-level section"
    )
