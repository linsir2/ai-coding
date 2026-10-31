"""F2: the summarizer must not be fed the todo table or harness nudges.

Trace 2026-10-31 (session f1d881ee, t8): every checkpoint echoed the live todo
list ("1 波 14 项") and the step seed, because ``summary_chunks`` rendered a
``todo_write`` result (``<tool_output tool="todo_write">`` wrapping
``<task-state>``) verbatim and labelled every synthetic harness line ``[user]``.
A checkpoint that repeats the task list is not a summary; it is the list again,
and it makes the next model re-read the plan instead of continuing the work.

Rules pinned here:
* a ``todo_write`` result contributes no body to the summary;
* harness nudges (``[continue]`` …) are ``[harness]``, never ``[user]`` -- the
  merge prompt lets only *user* messages rewrite 目标/约束;
* the real user message and the plan seed stay ``[user]``.
"""

from ai_coding.core.context_compression import summary_chunks
from ai_coding.domain.message import ChatMessage


def _msg(role: str, body: str, *, synthetic: bool = False, tool_name: str = "") -> ChatMessage:
    return ChatMessage(
        role=role,
        content=body,
        timestamp="",
        synthetic=synthetic,
        tool_name=tool_name,
        tool_call_id="c1" if role == "tool" else "",
    )


def _joined(messages, cap: int = 10_000) -> str:
    return "\n".join(summary_chunks(messages, cap))


def test_a_todo_write_result_body_never_reaches_the_summary():
    history = [
        _msg("user", "把层设计跑完"),
        _msg(
            "tool",
            '<tool_output tool="todo_write">\n<task-state>\n'
            "目标: 探索\n进度: 3/14\n[ ] 4. 读取 security/...\n</task-state>\n</tool_output>",
            tool_name="todo_write",
        ),
    ]

    text = _joined(history)

    assert "<task-state>" not in text
    assert "[ ] 4." not in text
    assert "[user] 把层设计跑完" in text


def test_harness_nudges_are_labelled_harness_not_user():
    history = [
        _msg("user", "真正的要求"),
        _msg("user", "[continue] 上一轮到了迭代上限，继续把剩下的工作做完。", synthetic=True),
        _msg("user", "[task-gate] 还有未完成任务项。", synthetic=True),
    ]

    text = _joined(history)

    assert "[harness] [continue]" in text
    assert "[harness] [task-gate]" in text
    assert "[user] [continue]" not in text
    assert "[user] 真正的要求" in text


def test_the_plan_seed_remains_a_user_message():
    """The step seed is synthetic but it *is* the task statement; relabelling it
    harness would drop the objective from the first checkpoint."""
    history = [
        _msg("user", "[计划标题] 探索各层设计\n\n1. Step 2a…", synthetic=True),
    ]

    text = _joined(history)

    assert "[user] [计划标题]" in text
    assert "[harness] [计划标题]" not in text


def test_a_tool_result_that_merely_mentions_the_task_state_is_kept():
    """Only the todo_write tool's own output is the table; another tool's text
    that happens to quote it is evidence and stays."""
    history = [
        _msg(
            "tool",
            '<tool_output tool="bash">\ncat task.json → <task-state>…',
            tool_name="bash",
        ),
    ]

    text = _joined(history)

    assert "[tool:bash]" in text
