import pytest

from ai_coding.domain.message import (
    ChatMessage,
    ToolCall,
    ToolCallRef,
    ToolResult,
    tool_call_from_ref,
)


def test_tool_call_ref_fields():
    ref = ToolCallRef(id="1", name="read", arguments='{"path": "a"}')
    assert ref.id == "1"
    assert ref.name == "read"
    assert ref.arguments == '{"path": "a"}'


def test_tool_call_from_ref_args_json():
    ref = ToolCallRef(id="1", name="read", arguments='{"path": "a"}')
    call = tool_call_from_ref(ref)
    assert isinstance(call, ToolCall)
    assert call.id == "1"
    assert call.name == "read"
    assert call.params == {"path": "a"}


def test_tool_call_from_ref_bad_args_defaults_empty():
    ref = ToolCallRef(id="2", name="read", arguments="not-json{")
    call = tool_call_from_ref(ref)
    assert call.params == {}
    assert call.id == "2"


def test_tool_call_from_ref_non_object_defaults_empty():
    ref = ToolCallRef(id="3", name="x", arguments="[1, 2]")
    assert tool_call_from_ref(ref).params == {}


def test_chat_message_user_defaults():
    msg = ChatMessage(role="user", content="hi", timestamp="t0")
    assert msg.tool_call_id is None
    assert msg.tool_name is None
    assert msg.tool_calls is None


def test_chat_message_tool_requires_tool_call_id():
    with pytest.raises(ValueError):
        ChatMessage(role="tool", content="ok", timestamp="t0")


def test_chat_message_assistant_requires_content_or_tool_calls():
    with pytest.raises(ValueError):
        ChatMessage(role="assistant", content=None, timestamp="t0")


def test_chat_message_tool_ok_with_tool_call_id():
    msg = ChatMessage(role="tool", content="ok", timestamp="t0", tool_call_id="1")
    assert msg.tool_call_id == "1"


def test_chat_message_assistant_with_tool_calls_ok():
    msg = ChatMessage(
        role="assistant",
        content=None,
        timestamp="t0",
        tool_calls=[ToolCallRef(id="1", name="read", arguments="{}")],
    )
    assert msg.tool_calls


def test_chat_message_timestamp_preserved():
    msg = ChatMessage(role="user", content="hi", timestamp="2024-01-01T00:00:00")
    assert msg.timestamp == "2024-01-01T00:00:00"


def test_tool_result_success_error():
    assert ToolResult(success=True, output="ok").success is True
    err = ToolResult(success=False, output="boom")
    assert err.success is False
    assert err.output == "boom"
