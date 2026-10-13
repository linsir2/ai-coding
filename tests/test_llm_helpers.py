"""TDD: pure parsers in ai_coding.ai.llm_helpers (no network)."""

import pytest

import ai_coding.ai.llm_helpers as llm_helpers
from ai_coding.ai.llm_helpers import _messages_to_text
from ai_coding.domain.message import ChatMessage


def test_messages_to_text_skips_blank_and_appends_conclusion():
    msgs = [
        ChatMessage(role="user", content="你好", timestamp="t1"),
        ChatMessage(
            role="tool", content="  \t ", timestamp="t2", tool_call_id="c1"
        ),
        ChatMessage(role="user", content="", timestamp="t3"),
    ]
    text = _messages_to_text(msgs, "结论")
    assert "[user] 你好" in text
    assert "[tool]" not in text
    assert "[conclusion] 结论" in text


def test_r11c_extract_prompt_asks_for_a_scene_and_says_it_is_optional():
    """The prompt is the only producer of scenes for harvested notes.

    Nothing else in the repo fills `scene` on the harvest path, so if this text
    stops asking for it the whole feature (map, filter, navigation) becomes dead
    code while every test still passes.
    """
    from ai_coding.ai.llm_helpers import _EXTRACT_SYSTEM_PROMPT

    assert "scene" in _EXTRACT_SYSTEM_PROMPT
    # Optional, not mandatory.  The wording moved when M8 added the reuse rule
    # ("归不了组就省略这个字段"), so this pins the *property* — there must still be a
    # way out of filling the field — rather than the old adverb.
    assert "省略" in _EXTRACT_SYSTEM_PROMPT
    # M8: reuse what the memory already groups by, without forbidding a new label.
    assert "<scene-groups>" in _EXTRACT_SYSTEM_PROMPT
    assert "新标签" in _EXTRACT_SYSTEM_PROMPT
    # The pre-R11c contract must survive verbatim for the fields that matter.
    assert "target_id" in _EXTRACT_SYSTEM_PROMPT
    assert "kind ∈ fact|decision|preference|open_thread" in _EXTRACT_SYSTEM_PROMPT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "broken",
    [
        '[{"text": "CI 跑在 GitHub Actions 上",',
        '[\n  {\n    "text": "配置模板是 config.example.yaml",\n'
        '    "kind": "decision",\n    "importance": 4,\n',
    ],
    ids=["single-line", "multi-line"],
)
async def test_the_extractor_never_turns_malformed_json_into_memories(monkeypatch, broken):
    """A malformed extractor answer is a failed harvest, not a legacy one.

    There were two ways to get here and only one had a guard: `_from_lines`
    skipped ``{`` and ``[``, so every field line of a truncated array leaked
    through, and when `parse_candidates` came back empty a second fallback split
    the same text again with no guard at all.  `.memory` collected 12 survivors
    from one real turn, all `kind=fact`.
    """
    async def fake_chat(model_config, system, user, max_tokens):
        return broken

    monkeypatch.setattr(llm_helpers, "_chat", fake_chat)
    extract = llm_helpers.build_extractor(object())

    got = await extract([ChatMessage(role="user", content="约定", timestamp="t")], None)

    assert got == []


# ---- G5-3: a summary that ignores the format is a failure, not a summary ----


def test_the_shape_check_accepts_a_structured_summary():
    from ai_coding.ai.llm_helpers import summary_is_usable

    assert summary_is_usable("## 目标\n完成压缩\n## 已完成\n- 读了 loader")
    assert summary_is_usable("## 关键文件\n- `a.py`")


def test_the_shape_check_refuses_an_unstructured_reply():
    """The failure this exists for: the model answers in prose, the pipeline
    installs it as the early history, and the agent continues on a summary that
    says nothing."""
    from ai_coding.ai.llm_helpers import summary_is_usable

    assert not summary_is_usable("好的，这段对话主要讲了压缩的事情。")
    assert not summary_is_usable("")
    assert not summary_is_usable("   \n  ")


@pytest.mark.asyncio
async def test_a_well_formed_summary_is_taken_on_the_first_call(monkeypatch):
    calls: list[str] = []

    async def fake_chat(model_config, system, user, max_tokens):
        calls.append(user)
        return "## 目标\n压缩历史\n## 待办\n- 无"

    monkeypatch.setattr(llm_helpers, "_chat", fake_chat)

    got = await llm_helpers.build_summarizer(object())("很长的历史")

    assert got is not None and got.startswith("## 目标")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_an_unstructured_reply_is_asked_for_again(monkeypatch):
    """One nudge, matching the reference implementations: cheaper than
    degrading, and bounded so a model that will not comply cannot hold the
    pipeline."""
    replies = iter(["好的，我总结了一下。", "## 目标\n压缩历史\n## 已完成\n- 跑了测试"])
    calls: list[str] = []

    async def fake_chat(model_config, system, user, max_tokens):
        calls.append(user)
        return next(replies)

    monkeypatch.setattr(llm_helpers, "_chat", fake_chat)

    got = await llm_helpers.build_summarizer(object())("很长的历史")

    assert got is not None and "## 已完成" in got
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_reply_that_never_takes_the_shape_is_refused(monkeypatch):
    """Returning ``None`` is what makes the pipeline fall back to the trimmed
    history.  Returning the prose would be worse than no summary at all: it
    would be installed as the record of what happened."""
    async def fake_chat(model_config, system, user, max_tokens):
        return "好的，我总结了一下。"

    monkeypatch.setattr(llm_helpers, "_chat", fake_chat)

    assert await llm_helpers.build_summarizer(object())("很长的历史") is None


@pytest.mark.asyncio
async def test_the_summary_prompt_asks_for_the_shape_that_is_checked(monkeypatch):
    """The prompt is the only thing producing output the check accepts, so a
    prompt that stops naming the sections turns every summary into a failure."""
    seen: list[str] = []

    async def fake_chat(model_config, system, user, max_tokens):
        seen.append(system)
        return "## 目标\nx"

    monkeypatch.setattr(llm_helpers, "_chat", fake_chat)

    await llm_helpers.build_summarizer(object())("历史")

    # The sections belong in the instruction, not in the payload.  Read from the
    # shared constant rather than a literal copy: the invariant this test exists
    # for is that the prompt and the shape check cannot drift apart, and a
    # hardcoded list is exactly how they would drift (R11 replaced the four
    # sections with OpenCode's seven, and this literal did not notice).
    for section in llm_helpers.SUMMARY_SECTIONS:
        assert section in seen[0]
