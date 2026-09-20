"""TDD (M1-1): domain ChatMessage -> SDK input-item mapping (pure functions)."""

from ai_coding.ai.mapping import chat_message_to_input, history_to_inputs
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
    # M1 has no tool wiring; an assistant message with only tool_calls maps to empty text.
    assert chat_message_to_input(msg) == {"role": "assistant", "content": ""}


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


def test_history_to_inputs_order():
    msgs = [
        ChatMessage(role="user", content="a", timestamp="t0"),
        ChatMessage(role="assistant", content="b", timestamp="t1"),
        ChatMessage(role="tool", content="c", timestamp="t2", tool_call_id="x"),
    ]
    inputs = history_to_inputs(msgs)
    assert inputs[0] == {"role": "user", "content": "a"}
    assert inputs[1] == {"role": "assistant", "content": "b"}
    assert inputs[2]["type"] == "function_call_output"
