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
    assert "不确定" in _EXTRACT_SYSTEM_PROMPT  # optional, not mandatory
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
