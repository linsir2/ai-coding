"""TDD (B0): the fan-out layer — N read-only children, one merged report."""

import pytest

from ai_coding.core.research_fanout import (
    RESEARCH_SCHEMA,
    ResearchFanOut,
    ResearchQuestion,
    render_report,
    total_usage,
)
from ai_coding.domain.subagent import ParentContext, SubagentResult


class _Runner:
    def __init__(self, results):
        self._results = list(results)
        self.calls: list[dict] = []

    async def run(self, task, name="sub-agent", **kwargs):
        self.calls.append({"task": task, "name": name, **kwargs})
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _result(summary="found it", files=("a.py",), findings=("f1",), usage=None, status="completed"):
    return SubagentResult(
        status=status,
        conclusion=summary,
        facts={"summary": summary, "key_files": list(files), "findings": list(findings)},
        usage=usage,
    )


@pytest.mark.asyncio
async def test_three_questions_run_as_read_only_task_only_children():
    runner = _Runner([_result(), _result(), _result()])
    fanout = ResearchFanOut(runner)
    questions = [
        ResearchQuestion("q1", "路由在哪注册？", ("app/",)),
        ResearchQuestion("q2", "测试怎么跑？"),
        ResearchQuestion("q3", "接口谁在用？"),
    ]

    packets = await fanout.run(
        questions,
        shared_context="目标：给房间加价格",
        parent=ParentContext(permissions=()),
        parent_span="call-1",
    )

    assert [packet.id for packet in packets] == ["q1", "q2", "q3"]
    assert all(call["agent"] == "explore" for call in runner.calls)
    assert all(call["context_mode"] == "task_only" for call in runner.calls)
    assert all(call["readonly"] is True for call in runner.calls)
    # No worktree: a readonly child must read the live working tree (a HEAD
    # snapshot hid the untracked file a real run was asked about).
    assert all(call["isolate"] is False for call in runner.calls)
    assert all(call["output_schema"] == RESEARCH_SCHEMA for call in runner.calls)
    assert all(call["parent_span"] == "call-1" for call in runner.calls)
    assert "目标：给房间加价格" in runner.calls[0]["task"]
    assert "路由在哪注册？" in runner.calls[0]["task"]
    assert "app/" in runner.calls[0]["task"]
    assert runner.calls[0]["name"] == "research:q1"


@pytest.mark.asyncio
async def test_one_child_failure_does_not_sink_the_others():
    runner = _Runner([
        _result(),
        SubagentResult(status="timeout", error="child_timeout", truncated=True),
        _result(),
    ])
    packets = await ResearchFanOut(runner).run([
        ResearchQuestion("q1", "一"), ResearchQuestion("q2", "二"), ResearchQuestion("q3", "三"),
    ])

    assert [packet.status for packet in packets] == ["completed", "timeout", "completed"]
    assert packets[1].error == "child_timeout"
    assert packets[1].truncated is True
    assert packets[0].summary == "found it"


@pytest.mark.asyncio
async def test_an_exception_becomes_an_error_packet():
    runner = _Runner([_result(), RuntimeError("boom")])
    packets = await ResearchFanOut(runner).run([
        ResearchQuestion("q1", "一"), ResearchQuestion("q2", "二"),
    ])
    assert packets[1].status == "error"
    assert "boom" in packets[1].error


@pytest.mark.asyncio
async def test_packets_are_capped_and_say_so():
    runner = _Runner([_result(
        summary="x" * 900,
        files=tuple(f"f{i}.py" for i in range(12)),
        findings=tuple(f"f{i}" for i in range(9)),
    )])
    packet = (await ResearchFanOut(runner).run([ResearchQuestion("q1", "一")]))[0]
    assert len(packet.summary) <= 600
    assert len(packet.key_files) == 8
    assert len(packet.findings) == 5
    # A cap this module applied must be visible: a silent cut reads as a
    # complete answer and the model never learns something was dropped.
    assert packet.truncated is True


@pytest.mark.asyncio
async def test_a_child_that_hit_its_ceiling_is_not_reported_as_an_error():
    runner = _Runner([
        SubagentResult(
            status="max_iterations", conclusion="partial but useful",
            exit_reason="max_iterations reached", facts={"summary": "partial", "key_files": []},
        )
    ])
    packet = (await ResearchFanOut(runner).run([ResearchQuestion("q1", "一")]))[0]
    assert packet.status == "max_iterations"
    assert packet.error == ""


def test_render_report_marks_each_question_and_dedupes_files():
    from ai_coding.core.research_fanout import ResearchPacket

    packets = [
        ResearchPacket("q1", "completed", "first", ("a.py", "b.py")),
        ResearchPacket("q2", "timeout", "", ("b.py", "c.py"), error="child_timeout"),
    ]
    report = render_report(packets)
    assert "## q1 · completed" in report
    assert "## q2 · timeout" in report
    assert "child_timeout" in report
    assert "相关文件" in report
    assert report.count("b.py") == 1


def test_total_usage_sums_keywise_and_returns_none_when_nobody_reported():
    from ai_coding.core.research_fanout import ResearchPacket

    assert total_usage([]) is None
    assert total_usage([ResearchPacket("q1", "completed", usage=None)]) is None
    assert total_usage([
        ResearchPacket("q1", "completed", usage={"prompt_tokens": 10, "completion_tokens": 5}),
        ResearchPacket(
            "q2", "completed",
            usage={"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        ),
    ]) == {"prompt_tokens": 30, "completion_tokens": 15, "total_tokens": 30}

# ---- r25: the frozen brief and the return verdict ---------------------------


@pytest.mark.asyncio
async def test_the_child_receives_the_frozen_brief_envelope():
    runner = _Runner([_result()])
    await ResearchFanOut(runner).run(
        [ResearchQuestion("q1", "路由在哪注册？", ("app/",))],
        shared_context="目标：给房间加价格",
    )

    task = runner.calls[0]["task"]
    assert task.startswith("[TASK]\n路由在哪注册？")
    assert "[CONTEXT]\n目标：给房间加价格" in task
    assert "[SCOPE]\n- app/" in task
    assert "[CONSTRAINTS]\n- 只读" in task
    # The format instruction belongs to the runner's schema trailer, not the brief.
    assert "[response format" not in task


@pytest.mark.asyncio
async def test_an_unfit_brief_is_refused_before_the_child_runs():
    from ai_coding.domain.subagent import OBJECTIVE_MAX

    runner = _Runner([])
    packets = await ResearchFanOut(runner).run(
        [ResearchQuestion("q1", "x" * (OBJECTIVE_MAX + 1))]
    )

    assert packets[0].status == "error"
    assert "invalid_brief" in packets[0].error
    assert runner.calls == []


@pytest.mark.asyncio
async def test_a_schema_failure_survives_into_the_packet_and_the_report():
    runner = _Runner([
        SubagentResult(
            status="completed",
            conclusion='{"summary": 123}',
            facts=None,
            schema_valid=False,
            schema_errors=("key 'summary' must be string, got int",),
        )
    ])
    packets = await ResearchFanOut(runner).run([ResearchQuestion("q1", "一")])

    assert packets[0].schema_valid is False
    assert packets[0].to_dict()["schema_valid"] is False

    report = render_report(packets)
    assert "结构校验未过" in report
    assert "must be string" in report
