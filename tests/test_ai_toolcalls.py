"""TDD (R1a-2): streaming tool-call fragment accumulation.

Pure functions — no network, no fakes beyond plain objects.  These lock the
protocol shape that the OpenAI chat-completions stream actually delivers:
``tool_calls`` arrive as fragments keyed by ``index``, with ``id``/``name`` on
the first fragment and ``arguments`` split across the rest.
"""

from types import SimpleNamespace

from ai_coding.ai.toolcalls import finalize_tool_calls, merge_tool_call_delta


def _delta(index, *, id=None, name=None, arguments=None):
    """A delta shaped like the openai SDK's ChoiceDeltaToolCall."""
    return SimpleNamespace(
        index=index, id=id, function=SimpleNamespace(name=name, arguments=arguments)
    )


def test_single_fragment_full_call():
    acc: dict = {}
    merge_tool_call_delta(acc, _delta(0, id="c1", name="read", arguments='{"path":"a"}'))
    calls = finalize_tool_calls(acc)
    assert len(calls) == 1
    assert calls[0].id == "c1"
    assert calls[0].name == "read"
    assert calls[0].arguments == '{"path":"a"}'


def test_arguments_split_across_three_fragments():
    acc: dict = {}
    merge_tool_call_delta(acc, _delta(0, id="c1", name="read", arguments='{"path"'))
    merge_tool_call_delta(acc, _delta(0, arguments=':"a"'))
    merge_tool_call_delta(acc, _delta(0, arguments="}"))
    calls = finalize_tool_calls(acc)
    assert calls[0].arguments == '{"path":"a"}'


def test_two_parallel_calls_interleaved():
    acc: dict = {}
    merge_tool_call_delta(acc, _delta(0, id="c0", name="read", arguments='{"p'))
    merge_tool_call_delta(acc, _delta(1, id="c1", name="glob", arguments='{"q'))
    merge_tool_call_delta(acc, _delta(0, arguments='ath":"a"}'))
    merge_tool_call_delta(acc, _delta(1, arguments='":"*.py"}'))
    calls = finalize_tool_calls(acc)
    assert [(c.id, c.name) for c in calls] == [("c0", "read"), ("c1", "glob")]
    assert calls[0].arguments == '{"path":"a"}'
    assert calls[1].arguments == '{"q":"*.py"}'


def test_missing_id_synthesizes_call_index():
    acc: dict = {}
    merge_tool_call_delta(acc, _delta(0, name="read", arguments="{}"))
    assert finalize_tool_calls(acc)[0].id == "call_0"


def test_missing_arguments_defaults_to_empty_object():
    acc: dict = {}
    merge_tool_call_delta(acc, _delta(0, id="c1", name="read"))
    assert finalize_tool_calls(acc)[0].arguments == "{}"


def test_finalize_sorts_by_index():
    acc: dict = {}
    merge_tool_call_delta(acc, _delta(2, id="c2", name="three", arguments="{}"))
    merge_tool_call_delta(acc, _delta(0, id="c0", name="one", arguments="{}"))
    merge_tool_call_delta(acc, _delta(1, id="c1", name="two", arguments="{}"))
    assert [c.name for c in finalize_tool_calls(acc)] == ["one", "two", "three"]


def test_unicode_arguments_preserved():
    acc: dict = {}
    merge_tool_call_delta(acc, _delta(0, id="c1", name="write", arguments='{"content":"你好'))
    merge_tool_call_delta(acc, _delta(0, arguments='世界"}'))
    assert finalize_tool_calls(acc)[0].arguments == '{"content":"你好世界"}'


def test_empty_acc_returns_empty_list():
    assert finalize_tool_calls({}) == []


def test_name_fragments_concatenate():
    acc: dict = {}
    merge_tool_call_delta(acc, _delta(0, id="c1", name="to"))
    merge_tool_call_delta(acc, _delta(0, name="do_write"))
    assert finalize_tool_calls(acc)[0].name == "todo_write"


def test_delta_accepts_dict_and_object():
    acc: dict = {}
    merge_tool_call_delta(
        acc, {"index": 0, "id": "c1", "function": {"name": "read", "arguments": "{"}}
    )
    merge_tool_call_delta(acc, _delta(0, arguments="}"))
    calls = finalize_tool_calls(acc)
    assert (calls[0].id, calls[0].name, calls[0].arguments) == ("c1", "read", "{}")


def test_id_set_once_on_repeat():
    acc: dict = {}
    merge_tool_call_delta(acc, _delta(0, id="c1", name="read", arguments="{}"))
    merge_tool_call_delta(acc, _delta(0, id="c1"))
    merge_tool_call_delta(acc, _delta(0, id="other"))
    assert finalize_tool_calls(acc)[0].id == "c1"
