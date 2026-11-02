"""TDD (R1a-5): subAgent tool — delegate with the parent's context, return a packet.

Rewritten from the M4 spec: the tool now hands the runner the parent turn's
request view and system prompt (so the sub-agent sees the same context), and it
turns the structured packet into a ToolResult with both readable text and
machine-readable data.
"""

import pytest

from ai_coding.domain.message import ChatMessage, ToolArtifact
from ai_coding.domain.subagent import SubagentResult
from ai_coding.tools.base import ToolContext
from ai_coding.tools.subagent_tool import SubAgentTool


class _FakeRunner:
    def __init__(self, result: SubagentResult | None = None) -> None:
        self.result = result or SubagentResult(status="completed", conclusion="done")
        self.calls: list[dict] = []

    async def run(
        self, task, name, *, worktree_manager=None, parent=None, parent_span="",
        observer=None, origin_session="",
    ):
        self.calls.append(
            {
                "task": task,
                "name": name,
                "worktree_manager": worktree_manager,
                "parent": parent,
                "isolate": task.isolate,
                "readonly": task.readonly,
                "agent": task.agent,
                "output_schema": task.output_schema,
            }
        )
        return self.result


def _ctx() -> ToolContext:
    return ToolContext(
        messages=(ChatMessage(role="user", content="parent view", timestamp="t"),),
        instructions="PARENT INSTRUCTIONS",
        workspace="/tmp/ws",
        session_id="sid",
    )


@pytest.mark.asyncio
async def test_subagent_tool_passes_parent_context():
    runner = _FakeRunner()
    tool = SubAgentTool(runner=runner, worktree_manager="wt/1", isolate=False)

    await tool.execute({"task": "analyze this", "name": "analyst"}, _ctx())

    call = runner.calls[0]
    assert call["task"].render() == "analyze this"
    assert call["name"] == "analyst"
    assert call["worktree_manager"] == "wt/1"
    assert call["isolate"] is False
    assert call["parent"] is not None
    # r25/v2: only the request view and the rule layer travel; the child builds
    # its own system prompt.
    assert [m.content for m in call["parent"].messages] == ["parent view"]
    assert not hasattr(call["parent"], "instructions")


class _SpawnRunner:
    """Only the background path: spawn returns an id; nothing else is touched."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def spawn_background(self, task, name, **kwargs):
        self.calls.append({"task": task, "name": name, **kwargs})
        return "run_1234567890ab"


@pytest.mark.asyncio
async def test_the_start_message_points_at_the_list_tool_not_a_human_command():
    """C-3b: the old handle said "check /tasks" -- a command the model cannot
    run.  It now names the lane that exists for it: results arrive on their
    own, and subagent_list answers "what is still running"."""
    tool = SubAgentTool(runner=_SpawnRunner())

    result = await tool.execute({"task": "do it", "background": True}, _ctx())

    assert result.success is True
    assert "subagent_list" in result.output
    assert "/tasks" not in result.output


@pytest.mark.asyncio
async def test_subagent_tool_returns_structured_data():
    runner = _FakeRunner(
        SubagentResult(
            status="completed",
            conclusion="all good",
            todos=({"id": "1", "content": "step", "status": "pending"},),
            iterations=3,
            ledger_path="transcripts/subagents/abc.json",
        )
    )
    tool = SubAgentTool(runner=runner)

    result = await tool.execute({"task": "t"}, _ctx())

    assert result.success is True
    data = result.data
    assert data is not None
    assert data["status"] == "completed"
    assert data["iterations"] == 3
    assert data["todos"] == [{"id": "1", "content": "step", "status": "pending"}]
    assert data["ledger_path"] == "transcripts/subagents/abc.json"
    # the full conclusion travels as rendered text, not duplicated in data
    assert "conclusion" not in data
    assert data["conclusion_chars"] == len("all good")


@pytest.mark.asyncio
async def test_subagent_tool_renders_conclusion_and_open_items():
    runner = _FakeRunner(
        SubagentResult(
            status="completed",
            conclusion="I fixed the parser",
            todos=({"id": "2", "content": "write tests", "status": "pending"},),
            iterations=4,
        )
    )
    tool = SubAgentTool(runner=runner)

    result = await tool.execute({"task": "t", "name": "dev"}, _ctx())

    assert "I fixed the parser" in result.output
    assert "write tests" in result.output
    assert "dev" in result.output


@pytest.mark.asyncio
async def test_subagent_tool_passes_artifacts():
    runner = _FakeRunner(
        SubagentResult(
            status="completed",
            conclusion="done",
            artifacts=(ToolArtifact(path="parser.py", description="rewritten"),),
        )
    )
    tool = SubAgentTool(runner=runner)

    result = await tool.execute({"task": "t"}, _ctx())

    assert [a.path for a in result.artifacts] == ["parser.py"]
    assert "parser.py" in result.output


@pytest.mark.asyncio
async def test_subagent_missing_task_fails():
    runner = _FakeRunner()
    tool = SubAgentTool(runner=runner)

    result = await tool.execute({}, _ctx())

    assert result.success is False
    assert "task" in result.output.lower()
    assert runner.calls == []


@pytest.mark.asyncio
async def test_subagent_timeout_is_failure():
    runner = _FakeRunner(SubagentResult(status="timeout", error="exceeded 600s"))
    tool = SubAgentTool(runner=runner)

    result = await tool.execute({"task": "t"}, _ctx())

    assert result.success is False
    assert result.data is not None
    assert result.data["status"] == "timeout"
    assert "timeout" in result.output.lower()


@pytest.mark.asyncio
async def test_subagent_runner_error_is_tool_failure():
    class _BoomRunner:
        async def run(self, task, name, **kwargs):
            raise RuntimeError("boom")

    tool = SubAgentTool(runner=_BoomRunner())

    result = await tool.execute({"task": "t"}, _ctx())

    assert result.success is False
    assert result.error == "subagent_error"
    assert "boom" in result.output


@pytest.mark.asyncio
async def test_subagent_max_iterations_is_still_success():
    """A partial result beats discarding the work entirely."""
    runner = _FakeRunner(SubagentResult(status="max_iterations", conclusion="partial"))
    tool = SubAgentTool(runner=runner)

    result = await tool.execute({"task": "t"}, _ctx())

    assert result.success is True
    assert "partial" in result.output


def test_subagent_tool_declares_a_per_call_ceiling_above_the_runner_budget():
    """R2a: the dispatcher needs a ceiling to bound a stuck delegation."""

    class _BoundedRunner(_FakeRunner):
        timeout_seconds = 600.0

    assert SubAgentTool(runner=_BoundedRunner()).max_duration_seconds == pytest.approx(615.0)
    # A runner that does not advertise a budget leaves only the turn deadline.
    assert SubAgentTool(runner=_FakeRunner()).max_duration_seconds is None


# ---- R7e-4: the description has to say which window the child gets -----------


def test_the_description_promises_shared_context_in_full_mode():
    runner = _FakeRunner()
    runner.context_mode = "full"
    text = SubAgentTool(runner=runner).description
    assert "sees this conversation's messages" in text
    assert "builds its own system prompt" in text
    assert "cannot see" not in text


def test_the_description_warns_that_a_clean_window_cannot_see_the_conversation():
    runner = _FakeRunner()
    runner.context_mode = "task_only"
    text = SubAgentTool(runner=runner).description
    assert "cannot see" in text
    assert "task" in text


# ---- P1b: the read-only spawn switch ---------------------------------------


def test_the_schema_exposes_a_boolean_readonly_switch():
    schema = SubAgentTool(runner=_FakeRunner()).params_json_schema
    assert schema["properties"]["readonly"]["type"] == "boolean"


@pytest.mark.asyncio
async def test_readonly_is_forwarded_to_the_synchronous_runner():
    runner = _FakeRunner()
    tool = SubAgentTool(runner=runner, isolate=True)

    await tool.execute({"task": "scan", "readonly": True}, _ctx())

    call = runner.calls[0]
    assert call["readonly"] is True
    # The tool forwards the configured flag untouched; the runner decides that
    # a read-only child reads the live workspace, not a worktree snapshot.
    assert call["isolate"] is True


@pytest.mark.asyncio
async def test_a_writable_run_keeps_the_configured_isolation():
    runner = _FakeRunner()
    tool = SubAgentTool(runner=runner, isolate=True)

    await tool.execute({"task": "edit"}, _ctx())

    call = runner.calls[0]
    assert call["readonly"] is False
    assert call["isolate"] is True


@pytest.mark.asyncio
async def test_readonly_is_forwarded_to_the_background_runner():
    runner = _SpawnRunner()
    tool = SubAgentTool(runner=runner, isolate=True)

    await tool.execute({"task": "scan", "background": True, "readonly": True}, _ctx())

    call = runner.calls[0]
    assert call["task"].readonly is True
    # The runner, not the tool, turns this into "no worktree".
    assert call["task"].isolate is True


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["true", "false", 1, [], {}])
async def test_a_non_boolean_readonly_is_refused_before_the_runner(value):
    runner = _FakeRunner()
    tool = SubAgentTool(runner=runner)

    result = await tool.execute({"task": "t", "readonly": value}, _ctx())

    assert result.success is False
    assert result.error == "invalid_readonly"
    assert runner.calls == []


# ---- P2-A: small write facts, no whole packet ------------------------------


@pytest.mark.asyncio
async def test_data_carries_small_write_facts():
    runner = _FakeRunner(
        SubagentResult(
            status="completed",
            conclusion="done",
            changed_paths=("a.py", "b.py"),
            commit="abcdef123456",
            commit_error="git commit: no identity",
            degraded="worktree_unavailable",
        )
    )
    tool = SubAgentTool(runner=runner)

    result = await tool.execute({"task": "t"}, _ctx())

    data = result.data
    assert data is not None
    assert data["changed_paths"] == ["a.py", "b.py"]
    assert data["commit"] == "abcdef123456"
    assert data["commit_error"] == "git commit: no identity"
    assert data["degraded"] == "worktree_unavailable"
    # The whole packet must not be duplicated inside <data>.
    assert "packet" not in data
    assert "task" not in data


@pytest.mark.asyncio
async def test_render_warns_on_commit_failure_and_degraded():
    from ai_coding.tools.subagent_tool import _render

    result = SubagentResult(
        status="completed",
        conclusion="done",
        changed_paths=("a.py",),
        worktree_branch="ai_sub_probe",
        commit_error="git commit: no identity",
        degraded="worktree_create_failed",
    )

    text = _render(result, "probe")

    assert "提交失败" in text
    assert "git commit: no identity" in text
    assert "未隔离，已在主工作区执行" in text
    assert "worktree_create_failed" in text


# ---- P2-B2: a write overlap the parent can see ---------------------------


_OVERLAP = (
    {
        "key": "run-abcdef123456",
        "origin_session": "sess-1",
        "paths": ("a.py", "b.py"),
    },
)


@pytest.mark.asyncio
async def test_data_carries_the_write_overlap():
    runner = _FakeRunner(
        SubagentResult(
            status="completed",
            conclusion="done",
            changed_paths=("a.py", "b.py"),
            conflicts=_OVERLAP,
        )
    )
    tool = SubAgentTool(runner=runner)

    result = await tool.execute({"task": "t"}, _ctx())

    data = result.data
    assert data is not None
    assert data["conflicts"] == [
        {
            "key": "run-abcdef123456",
            "origin_session": "sess-1",
            "paths": ["a.py", "b.py"],
        }
    ]


@pytest.mark.asyncio
async def test_render_names_the_overlapping_run_and_paths():
    from ai_coding.tools.subagent_tool import _render

    result = SubagentResult(
        status="completed",
        conclusion="done",
        changed_paths=("a.py", "b.py"),
        conflicts=_OVERLAP,
    )

    text = _render(result, "probe")

    assert "⚠ 与 run-abcdef12 改动重叠: a.py,b.py（未合并）" in text
