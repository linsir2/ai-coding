"""TDD (M1-1 / M3-1): domain ChatMessage -> SDK input-item mapping (pure functions)."""

from ai_coding.ai.mapping import chat_message_to_input, chat_message_to_inputs
from ai_coding.ai.mapping import history_to_inputs
from ai_coding.domain.message import ChatMessage, ToolCallRef


def test_user_to_dict():
    msg = ChatMessage(role="user", content="hi", timestamp="t0")
    assert chat_message_to_input(msg) == {"role": "user", "content": "hi"}


def test_assistant_to_dict_content():
    msg = ChatMessage(role="assistant", content="hello", timestamp="t0")
    assert chat_message_to_input(msg) == {"role": "assistant", "content": "hello"}


def test_assistant_without_content_takes_tool_calls_empty_text():
    msg = ChatMessage(
        role="assistant",
        content=None,
        timestamp="t0",
        tool_calls=[ToolCallRef(id="1", name="read", arguments="{}")],
    )
    # M3: assistant tool calls are emitted as function_call item(s), not empty text.
    assert chat_message_to_inputs(msg) == [
        {"type": "function_call", "call_id": "1", "name": "read", "arguments": "{}"}
    ]


def test_assistant_single_item_convenience_raises_for_multi_item():
    msg = ChatMessage(
        role="assistant",
        content="text",
        timestamp="t0",
        tool_calls=[
            ToolCallRef(id="1", name="read", arguments="{}"),
            ToolCallRef(id="2", name="bash", arguments="{}"),
        ],
    )
    import pytest

    # expands to 3 items (2 calls + merged text) -> convenience must refuse
    with pytest.raises(ValueError):
        chat_message_to_input(msg)


def test_assistant_with_content_and_tool_calls_merge_items():
    msg = ChatMessage(
        role="assistant",
        content="text",
        timestamp="t9",
        tool_calls=[ToolCallRef(id="c1", name="bash", arguments="{}")],
    )
    inputs = chat_message_to_inputs(msg)
    # function_call first, then a merged assistant text item (avoids API-rejected
    # back-to-back assistant messages)
    assert inputs[0] == {
        "type": "function_call",
        "call_id": "c1",
        "name": "bash",
        "arguments": "{}",
    }
    assert inputs[1] == {
        "type": "message",
        "role": "assistant",
        "id": "asst_t9",
        "content": [{"type": "output_text", "text": "text"}],
    }


def test_multiple_tool_calls_expand_to_multiple_items():
    msg = ChatMessage(
        role="assistant",
        content=None,
        timestamp="t",
        tool_calls=[
            ToolCallRef(id="a", name="read", arguments="{}"),
            ToolCallRef(id="b", name="glob", arguments='{"pattern":"*.py"}'),
        ],
    )
    inputs = chat_message_to_inputs(msg)
    assert len(inputs) == 2
    assert inputs[0]["call_id"] == "a"
    assert inputs[1]["call_id"] == "b"
    assert inputs[1]["arguments"] == '{"pattern":"*.py"}'


def test_tool_to_dict_with_tool_call_id():
    msg = ChatMessage(
        role="tool",
        content="ok",
        timestamp="t0",
        tool_call_id="call_1",
    )
    assert chat_message_to_input(msg) == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": "ok",
    }


def test_tool_without_call_id_still_maps_zero():
    # defensive: never raise even if a stray tool message lacks its id
    msg = ChatMessage(role="tool", content="ok", timestamp="t0", tool_call_id="c")
    out = chat_message_to_input(msg)
    assert out["call_id"] == "c"


def test_history_to_inputs_order_and_flatten():
    msgs = [
        ChatMessage(role="user", content="a", timestamp="t0"),
        ChatMessage(
            role="assistant",
            content="b",
            timestamp="t1",
            tool_calls=[ToolCallRef(id="x", name="read", arguments="{}")],
        ),
        ChatMessage(
            role="tool", content="c", timestamp="t2", tool_call_id="x", tool_name="read"
        ),
    ]
    inputs = history_to_inputs(msgs)
    assert inputs[0] == {"role": "user", "content": "a"}
    assert inputs[1] == {"type": "function_call", "call_id": "x", "name": "read", "arguments": "{}"}
    # merged assistant text item follows the calls
    assert inputs[2] == {
        "type": "message",
        "role": "assistant",
        "id": "asst_t1",
        "content": [{"type": "output_text", "text": "b"}],
    }
    assert inputs[3] == {"type": "function_call_output", "call_id": "x", "output": "c"}
    # total items > messages (flattened)
    assert len(inputs) == 4
