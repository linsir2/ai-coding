"""TDD regression: framework-agnostic stream-item helpers in agents_sdk_service.

These pure helpers were surfaced by a real DeepSeek integration run (tool-call
arguments leaked into text; tool calls never persisted).  They parse fake SDK
items (dict or object shape) exactly as the live stream emits them, without any
network access.
"""

from types import SimpleNamespace

from ai_coding.ai.agents_sdk_service import (
    _extract_arguments_json,
    _extract_message_text,
    _extract_tool_output,
    _item_call_id,
    _item_tool_name,
)


def test_item_call_id_from_dict_and_obj():
    assert _item_call_id({"call_id": "c1", "type": "function_call"}) == "c1"
    assert _item_call_id({"id": "c2", "type": "function_call"}) == "c2"
    assert _item_call_id(SimpleNamespace(raw_item=SimpleNamespace(call_id="c3"))) == "c3"
    assert _item_call_id(SimpleNamespace(raw_item={})) is None


def test_item_tool_name():
    assert _item_tool_name({"name": "read"}) == "read"
    assert _item_tool_name(SimpleNamespace(raw_item=SimpleNamespace(name="bash"))) == "bash"
    assert _item_tool_name({"name": None}) is None


def test_extract_arguments_json_dict_str_and_obj():
    assert _extract_arguments_json(SimpleNamespace(
        raw_item={"name": "read", "arguments": '{"path": "a.txt"}'}
    )) == '{"path": "a.txt"}'
    # pydantic-like object with arguments as str
    assert _extract_arguments_json(SimpleNamespace(
        raw_item=SimpleNamespace(arguments="{}")
    )) == "{}"


def test_extract_message_text_last_part_wins():
    part1 = {"type": "output_text", "text": "first"}
    part2 = {"type": "output_text", "text": "second"}
    assert _extract_message_text(SimpleNamespace(
        raw_item=SimpleNamespace(content=[part1, part2])
    )) == "firstsecond"
    # model shaped raw_item (pydantic)
    obj_part = SimpleNamespace(text="obj text")
    assert _extract_message_text(SimpleNamespace(
        raw_item=SimpleNamespace(content=[obj_part])
    )) == "obj text"
    # raw is a plain dict
    assert _extract_message_text({"content": "plain"}) == "plain"


def test_extract_tool_output_str_and_structured():
    assert _extract_tool_output(SimpleNamespace(output="hello")) == "hello"
    assert _extract_tool_output(SimpleNamespace(output=None)) == ""
    structured = _extract_tool_output(SimpleNamespace(output={"n": 1}))
    assert '"n"' in structured and "1" in structured
