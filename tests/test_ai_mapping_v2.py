"""TDD (R1a-2): domain ChatMessage -> native chat-completions message.

Replaces the SDK-shaped mapping (``function_call`` input items).  The new shape
is the OpenAI standard: assistant messages carry a ``tool_calls`` array, tool
messages carry ``tool_call_id``.

Backwards compatibility is part of the spec: the legacy
``chat_message_to_input(s)`` / ``history_to_inputs`` helpers must keep working
because ``agents_sdk_service`` still uses them until R1a-6.
"""

from ai_coding.ai.mapping import (
    history_to_chat_messages,
    history_to_inputs,
    to_chat_message,
    tool_call_to_dict,
)
from ai_coding.domain.message import ChatMessage, ToolCallRef


def _user(content: str) -> ChatMessage:
    return ChatMessage(role="user", content=content, timestamp="t")


def test_user_message_maps_to_role_content():
    assert to_chat_message(_user("hi")) == {"role": "user", "content": "hi"}


def test_system_message_maps():
    msg = ChatMessage(role="system", content="be nice", timestamp="t")
    assert to_chat_message(msg) == {"role": "system", "content": "be nice"}


def test_assistant_text_only():
    msg = ChatMessage(role="assistant", content="done", timestamp="t")
    mapped = to_chat_message(msg)
    assert mapped == {"role": "assistant", "content": "done"}
    assert "tool_calls" not in mapped


def test_assistant_with_tool_calls_emits_native_tool_calls():
    msg = ChatMessage(
        role="assistant",
        content=None,
        timestamp="t",
        tool_calls=[ToolCallRef(id="c1", name="read", arguments='{"path":"a"}')],
    )
    mapped = to_chat_message(msg)
    assert mapped["role"] == "assistant"
    assert isinstance(mapped["tool_calls"], list)
    assert len(mapped["tool_calls"]) == 1


def test_assistant_tool_calls_shape_is_openai_standard():
    ref = ToolCallRef(id="c1", name="read", arguments='{"path":"a"}')
    mapped = tool_call_to_dict(ref)
    assert mapped["id"] == "c1"
    assert mapped["type"] == "function"
    assert mapped["function"]["name"] == "read"
    assert mapped["function"]["arguments"] == '{"path":"a"}'


def test_assistant_calls_without_text_sets_content_none():
    msg = ChatMessage(
        role="assistant",
        content=None,
        timestamp="t",
        tool_calls=[ToolCallRef(id="c1", name="read", arguments="{}")],
    )
    assert to_chat_message(msg)["content"] is None


def test_tool_message_uses_tool_call_id():
    msg = ChatMessage(
        role="tool",
        content="<tool_output tool=\"read\">\nbody\n</tool_output>",
        timestamp="t",
        tool_call_id="c1",
        tool_name="read",
    )
    mapped = to_chat_message(msg)
    assert mapped["role"] == "tool"
    assert mapped["tool_call_id"] == "c1"
    assert "body" in mapped["content"]


def test_history_order_preserved():
    history = [_user("one"), _user("two"), _user("three")]
    assert [m["content"] for m in history_to_chat_messages(history)] == [
        "one",
        "two",
        "three",
    ]


def test_empty_arguments_become_empty_json_object():
    ref = ToolCallRef(id="c1", name="read", arguments="")
    assert tool_call_to_dict(ref)["function"]["arguments"] == "{}"


def test_legacy_helpers_still_available():
    """R1a-6 removes these; until then agents_sdk_service depends on them."""
    legacy = history_to_inputs([_user("hi")])
    assert legacy == [{"role": "user", "content": "hi"}]
