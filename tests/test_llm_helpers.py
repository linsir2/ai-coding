"""TDD: pure parsers in ai_coding.ai.llm_helpers (no network)."""

from ai_coding.ai.llm_helpers import _messages_to_text, _parse_memory_lines
from ai_coding.domain.message import ChatMessage


def test_parse_memory_lines_strips_prefixes():
    raw = "- 用户偏好Python\n• 简洁代码\n 项目约定：后端用FastAPI\n\n# 装饰\n"
    out = _parse_memory_lines(raw)
    assert "用户偏好Python" in out
    assert "简洁代码" in out
    assert "项目约定：后端用FastAPI" in out
    # empty/short lines dropped
    assert all(len(x) >= 2 for x in out)


def test_parse_memory_lines_empty():
    assert _parse_memory_lines("") == []
    assert _parse_memory_lines("\n\n  \n") == []


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
