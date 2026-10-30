"""TDD (B0): the `parallel_research` tool contract."""

import pytest

from ai_coding.domain.message import ChatMessage
from ai_coding.domain.subagent import SubagentResult
from ai_coding.tools.base import ToolContext
from ai_coding.tools.parallel_research_tool import ParallelResearchTool


class _Runner:
    def __init__(self, results=None, *, timeout_seconds=1200.0, max_concurrent=4):
        self._results = list(results or [])
        self.calls: list[dict] = []
        self.timeout_seconds = timeout_seconds
        self.max_concurrent = max_concurrent

    async def run(self, task, name="sub-agent", **kwargs):
        self.calls.append({"task": task, "name": name, **kwargs})
        if self._results:
            return self._results.pop(0)
        return SubagentResult(
            status="completed", conclusion="ok",
            facts={"summary": "ok", "key_files": [], "findings": []},
            usage={"prompt_tokens": 10, "completion_tokens": 5},
        )


def _ctx() -> ToolContext:
    return ToolContext(
        workspace=".",
        session_id="sid",
        messages=(ChatMessage(role="user", content="parent", timestamp="t"),),
        instructions="INSTRUCTIONS",
        call_id="call-1",
    )


@pytest.mark.asyncio
async def test_two_questions_run_and_merge():
    runner = _Runner()
    tool = ParallelResearchTool(runner)
    result = await tool.execute(
        {
            "questions": [
                {"id": "q1", "question": "路由在哪注册？", "scope": ["app/"]},
                {"question": "测试怎么跑？"},
            ],
            "context": "目标：加价格",
        },
        _ctx(),
    )
    assert result.success is True
    assert "## q1 · completed" in result.output
    assert "## q2 · completed" in result.output
    assert len(runner.calls) == 2
    assert runner.calls[0]["agent"] == "explore"
    assert runner.calls[0]["readonly"] is True
    assert runner.calls[0]["context_mode"] == "task_only"
    assert runner.calls[0]["isolate"] is False
    assert result.data is not None
    assert result.data["questions"] == 2 and result.data["completed"] == 2
    assert result.data["usage"] == {"prompt_tokens": 20, "completion_tokens": 10}
    # U7: the child's trace must reach the parent's — one span, one observer,
    # one parent context; otherwise the fan-out is a black box.
    assert runner.calls[0]["parent_span"] == "call-1"
    assert runner.calls[0]["parent"] is not None
    assert runner.calls[0]["parent"].instructions == "INSTRUCTIONS"
    assert runner.calls[0]["observer"] is not None


def test_schema_advertises_two_to_four_questions():
    tool = ParallelResearchTool(_Runner())
    schema = tool.params_json_schema
    assert schema["properties"]["questions"]["minItems"] == 2
    assert schema["properties"]["questions"]["maxItems"] == 4
    assert "context" in schema["properties"]
    assert tool.parallel_safe is True
    assert tool.mutates_world is False
    assert tool.max_duration_seconds == 1215.0


def test_the_ceiling_covers_a_second_lane_when_concurrency_is_low():
    """4 questions behind a 2-slot semaphore need two rounds, not one."""
    tool = ParallelResearchTool(_Runner(timeout_seconds=1200.0, max_concurrent=2))
    assert tool.max_duration_seconds == 2430.0


@pytest.mark.asyncio
async def test_one_question_is_refused_with_guidance():
    tool = ParallelResearchTool(_Runner())
    r = await tool.execute({"questions": [{"question": "只有一个"}]}, _ctx())
    assert r.success is False
    assert r.error == "too_few_questions"
    assert "subagent" in r.output


@pytest.mark.asyncio
async def test_five_questions_are_refused():
    tool = ParallelResearchTool(_Runner())
    r = await tool.execute(
        {"questions": [{"question": f"q{i}"} for i in range(5)]}, _ctx()
    )
    assert r.success is False
    assert r.error == "too_many_questions"


@pytest.mark.asyncio
async def test_duplicate_explicit_ids_are_refused():
    tool = ParallelResearchTool(_Runner())
    r = await tool.execute(
        {"questions": [{"id": "x", "question": "a"}, {"id": "x", "question": "b"}]},
        _ctx(),
    )
    assert r.success is False
    assert r.error == "duplicate_question_id"


@pytest.mark.asyncio
async def test_long_context_is_refused_not_truncated():
    tool = ParallelResearchTool(_Runner())
    r = await tool.execute(
        {"questions": [{"question": "a"}, {"question": "b"}], "context": "c" * 1100},
        _ctx(),
    )
    assert r.success is False
    assert r.error == "context_too_long"


@pytest.mark.asyncio
async def test_a_long_question_is_refused_not_truncated():
    tool = ParallelResearchTool(_Runner())
    r = await tool.execute(
        {"questions": [{"question": "x" * 900}, {"question": "b"}]}, _ctx()
    )
    assert r.success is False
    assert r.error == "bad_questions"


@pytest.mark.asyncio
async def test_all_children_failing_is_a_failed_tool_result():
    runner = _Runner([
        SubagentResult(status="timeout", error="child_timeout"),
        SubagentResult(status="error", error="boom"),
    ])
    tool = ParallelResearchTool(runner)
    r = await tool.execute(
        {"questions": [{"id": "q1", "question": "a"}, {"id": "q2", "question": "b"}]},
        _ctx(),
    )
    assert r.success is False
    assert r.error == "all_children_failed"
    assert "child_timeout" in r.output and "boom" in r.output


@pytest.mark.asyncio
async def test_a_partial_result_still_succeeds():
    runner = _Runner([
        SubagentResult(status="completed", conclusion="ok",
                       facts={"summary": "ok", "key_files": []}),
        SubagentResult(status="timeout", error="child_timeout"),
    ])
    tool = ParallelResearchTool(runner)
    r = await tool.execute(
        {"questions": [{"id": "q1", "question": "a"}, {"id": "q2", "question": "b"}]},
        _ctx(),
    )
    assert r.success is True
    assert r.data is not None and r.data["completed"] == 1


def test_permission_gate_allows_the_tool_without_asking():
    from ai_coding.security.permission_gate import PermissionDecision, PermissionGate

    gate = PermissionGate()
    assert gate.check("parallel_research", {}).decision is PermissionDecision.ALLOW


def test_the_plan_agent_denies_the_tool():
    from ai_coding.agents.registry import builtin_catalog
    from ai_coding.domain.agents import PLAN_AGENT_ID

    spec = builtin_catalog().get(PLAN_AGENT_ID)
    assert spec is not None
    rows = [row for row in spec.permissions if row.get("action") == "parallel_research"]
    assert rows and all(row.get("effect") == "deny" for row in rows)
