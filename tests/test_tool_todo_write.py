"""TDD (R1a-1): todo_write — stateless tool mutating the session's TaskState.

Rewritten from the M2 spec: the tool no longer owns an in-memory list (that was
a per-instance singleton shared across sessions, so ``/clear`` could not clear
it).  State now travels through ``ToolContext.task``.
"""

import pytest

from ai_coding.domain.task import TaskState
from ai_coding.tools.base import ToolContext
from ai_coding.tools.todo_write_tool import TodoWriteTool


@pytest.fixture
def tool():
    return TodoWriteTool()


@pytest.fixture
def ctx():
    return ToolContext(task=TaskState(goal="goal"), workspace="/tmp/ws", session_id="sid")


@pytest.mark.asyncio
async def test_add_creates_item_in_ctx_task(tool, ctx):
    r = await tool.execute({"action": "add", "content": "do the thing"}, ctx)
    assert r.success is True
    assert "do the thing" in r.output
    assert [i.content for i in ctx.task.items] == ["do the thing"]
    assert r.data is not None and r.data["action"] == "add" and r.data["open"] == 1


@pytest.mark.asyncio
async def test_add_accepts_depends_on(tool, ctx):
    await tool.execute({"action": "add", "content": "a"}, ctx)
    r = await tool.execute(
        {"action": "add", "content": "b", "depends_on": ["1"]}, ctx
    )
    assert r.success is True
    assert ctx.task.get("2") is not None
    assert ctx.task.get("2").depends_on == ["1"]


@pytest.mark.asyncio
async def test_a_missing_action_is_inferred_from_the_params(tool, ctx):
    """Providers do not enforce ``required``; the measured run omitted it once.
    content → add, id → update, nothing → list."""
    added = await tool.execute({"content": "do the thing"}, ctx)
    listed = await tool.execute({}, ctx)

    assert added.success is True
    assert [item.content for item in ctx.task.items] == ["do the thing"]
    assert listed.success is True


@pytest.mark.asyncio
async def test_update_sets_status_and_evidence(tool, ctx):
    await tool.execute({"action": "add", "content": "task a"}, ctx)
    r = await tool.execute(
        {"action": "update", "id": "1", "status": "completed", "evidence": "tests pass"},
        ctx,
    )
    assert r.success is True
    item = ctx.task.get("1")
    assert item is not None
    assert item.status == "completed"
    assert item.evidence == "tests pass"
    assert "[x] 1. task a" in r.output


@pytest.mark.asyncio
async def test_a_step_cannot_rewrite_the_plan_contract(tool):
    """A step executes the approved plan; it must not rewrite verify/content
    (the runner would execute a command the model authored mid-run)."""
    task = TaskState(goal="goal")
    task.add("one", verify="pytest -q")
    task.add("two", verify="pytest -q")
    ctx = ToolContext(task=task, step_item_id="1")

    verify_change = await tool.execute(
        {"action": "update", "id": "2", "verify": "rm -rf /"}, ctx
    )
    addition = await tool.execute({"action": "add", "content": "three"}, ctx)

    assert verify_change.success is False and verify_change.error == "frozen_verify"
    assert addition.success is False and addition.error == "frozen_plan"


@pytest.mark.asyncio
async def test_a_step_may_still_update_status_and_evidence(tool):
    task = TaskState(goal="goal")
    task.add("one", verify="pytest -q")
    ctx = ToolContext(task=task, step_item_id="1")

    result = await tool.execute(
        {"action": "update", "id": "1", "status": "completed", "evidence": "tests pass"},
        ctx,
    )

    assert result.success is True


@pytest.mark.asyncio
async def test_list_returns_rendered_task(tool, ctx):
    await tool.execute({"action": "add", "content": "task 1"}, ctx)
    await tool.execute({"action": "add", "content": "task 2"}, ctx)
    r = await tool.execute({"action": "list"}, ctx)
    assert r.success is True
    assert "task 1" in r.output and "task 2" in r.output
    assert "进度: 0/2" in r.output


@pytest.mark.asyncio
async def test_clear_is_refused(tool, ctx):
    """`clear` is gone (plan-mode redesign, 2026-10-18).

    Measured in trace ``ec89d5ee``: the model ran ``add -> clear -> add ->
    clear`` for 25 iterations and finished with "无实质性工作" — each clear
    wiped the plan the previous add had written, two clears returned the
    identical ``(无任务项)`` so the model could not tell it had made no
    progress, and the third was refused by ``duplicate_call`` with a message
    ("reuse the earlier result") that is meaningless for an idempotent wipe.

    dsh has no clear either — its ``todo_write`` replaces the whole list
    (``packages/todo/tool-todo/src/index.ts:80``) — and revisions belong to
    ``update``, which the plan prompt already says.
    """
    await tool.execute({"action": "add", "content": "a"}, ctx)
    r = await tool.execute({"action": "clear"}, ctx)
    assert r.success is False
    assert r.error == "unknown_action"
    assert ctx.task.total == 1  # the list was not touched


@pytest.mark.asyncio
async def test_add_returns_error_on_blank_content(tool, ctx):
    r = await tool.execute({"action": "add", "content": "   "}, ctx)
    assert r.success is False
    assert r.error == "missing_content"
    assert ctx.task.total == 0


@pytest.mark.asyncio
async def test_update_returns_error_on_bad_status(tool, ctx):
    await tool.execute({"action": "add", "content": "a"}, ctx)
    r = await tool.execute({"action": "update", "id": "1", "status": "done"}, ctx)
    assert r.success is False
    assert r.error == "invalid_status"
    assert ctx.task.get("1") is not None and ctx.task.get("1").status == "pending"


@pytest.mark.asyncio
async def test_update_returns_error_on_unknown_id(tool, ctx):
    r = await tool.execute({"action": "update", "id": "99", "status": "completed"}, ctx)
    assert r.success is False
    assert r.error == "unknown_id"


@pytest.mark.asyncio
async def test_unknown_action_fails(tool, ctx):
    r = await tool.execute({"action": "bogus"}, ctx)
    assert r.success is False
    assert "unknown action" in r.output.lower()


@pytest.mark.asyncio
async def test_tool_is_stateless_across_contexts(tool):
    """Each context has its own task list — nothing leaks between sessions."""
    ctx_a = ToolContext(task=TaskState(), session_id="a")
    ctx_b = ToolContext(task=TaskState(), session_id="b")
    await tool.execute({"action": "add", "content": "only in a"}, ctx_a)
    r = await tool.execute({"action": "list"}, ctx_b)
    assert ctx_b.task.total == 0
    assert "only in a" not in r.output


@pytest.mark.asyncio
async def test_execute_without_ctx_does_not_crash(tool):
    """Migration tolerance: legacy call sites may still omit ctx."""
    r = await tool.execute({"action": "list"})
    assert r.success is True


# ---- R9a: the gate (docs/r9-analysis.md 3.1) ----


def _gate_tool() -> TodoWriteTool:
    return TodoWriteTool(enforce_dependencies=True)


@pytest.mark.asyncio
async def test_r9_add_refuses_an_unknown_dependency(ctx):
    r = await _gate_tool().execute(
        {"action": "add", "content": "b", "depends_on": ["9"]}, ctx
    )
    assert r.success is False
    assert r.error == "unknown_dependency"
    assert ctx.task.items == []


@pytest.mark.asyncio
async def test_r9_add_refuses_a_self_dependency(ctx):
    r = await _gate_tool().execute(
        {"action": "add", "content": "a", "depends_on": ["1"]}, ctx
    )
    assert r.success is False
    assert r.error == "self_dependency"
    assert ctx.task.items == []


@pytest.mark.asyncio
async def test_r9_add_deduplicates_dependencies(ctx):
    await _gate_tool().execute({"action": "add", "content": "a"}, ctx)
    r = await _gate_tool().execute(
        {"action": "add", "content": "b", "depends_on": ["1", "1"]}, ctx
    )
    assert r.success is True
    assert ctx.task.get("2").depends_on == ["1"]


@pytest.mark.asyncio
async def test_r9_completing_a_blocked_item_is_refused(ctx):
    tool = _gate_tool()
    await tool.execute({"action": "add", "content": "dep"}, ctx)
    await tool.execute({"action": "add", "content": "next", "depends_on": ["1"]}, ctx)
    r = await tool.execute({"action": "update", "id": "2", "status": "completed"}, ctx)
    assert r.success is False
    assert r.error == "blocked_by_dependencies"
    assert r.data is not None and r.data["open"] == ["1"]
    assert ctx.task.get("2").status == "pending"  # state untouched


@pytest.mark.asyncio
async def test_r9_starting_a_blocked_item_is_refused_too(ctx):
    tool = _gate_tool()
    await tool.execute({"action": "add", "content": "dep"}, ctx)
    await tool.execute({"action": "add", "content": "next", "depends_on": ["1"]}, ctx)
    r = await tool.execute({"action": "update", "id": "2", "status": "in_progress"}, ctx)
    assert r.error == "blocked_by_dependencies"
    assert ctx.task.get("2").status == "pending"


@pytest.mark.asyncio
async def test_r9_after_the_dependency_completes_the_item_can_finish(ctx):
    tool = _gate_tool()
    await tool.execute({"action": "add", "content": "dep"}, ctx)
    await tool.execute({"action": "add", "content": "next", "depends_on": ["1"]}, ctx)
    await tool.execute({"action": "update", "id": "1", "status": "completed"}, ctx)
    r = await tool.execute({"action": "update", "id": "2", "status": "completed"}, ctx)
    assert r.success is True
    assert ctx.task.get("2").status == "completed"


@pytest.mark.asyncio
async def test_r9_dropping_the_dependency_unblocks(ctx):
    tool = _gate_tool()
    await tool.execute({"action": "add", "content": "dep"}, ctx)
    await tool.execute({"action": "add", "content": "next", "depends_on": ["1"]}, ctx)
    await tool.execute({"action": "update", "id": "1", "status": "dropped"}, ctx)
    r = await tool.execute({"action": "update", "id": "2", "status": "completed"}, ctx)
    assert r.success is True


@pytest.mark.asyncio
async def test_r9_pending_blocked_and_dropped_are_never_gated(ctx):
    tool = _gate_tool()
    await tool.execute({"action": "add", "content": "dep"}, ctx)
    await tool.execute({"action": "add", "content": "next", "depends_on": ["1"]}, ctx)
    for status in ("pending", "blocked", "dropped"):
        r = await tool.execute({"action": "update", "id": "2", "status": status}, ctx)
        assert r.success is True, status


@pytest.mark.asyncio
async def test_r9_the_gate_can_be_switched_off(ctx):
    tool = TodoWriteTool(enforce_dependencies=False)
    await tool.execute({"action": "add", "content": "dep"}, ctx)
    await tool.execute({"action": "add", "content": "next", "depends_on": ["1"]}, ctx)
    r = await tool.execute({"action": "update", "id": "2", "status": "completed"}, ctx)
    assert r.success is True  # today's behaviour


# ---- 19 §3.5 (Q6): completion is the execution stage's mark ------------------


@pytest.fixture
def readonly_ctx():
    task = TaskState(goal="goal")
    task.add("step")
    return ToolContext(task=task, workspace="/tmp/ws", session_id="sid", readonly=True)


@pytest.mark.asyncio
async def test_readonly_refuses_marking_an_item_completed(tool, readonly_ctx):
    """The planning posture may author the list, not finish it (19 Q6)."""
    r = await tool.execute(
        {"action": "update", "id": "1", "status": "completed"}, readonly_ctx
    )
    assert r.success is False
    assert r.error == "readonly_completion"
    assert readonly_ctx.task.get("1").status == "pending"


@pytest.mark.asyncio
async def test_readonly_still_allows_planning_mutations(tool, readonly_ctx):
    added = await tool.execute({"action": "add", "content": "next"}, readonly_ctx)
    assert added.success is True
    progress = await tool.execute(
        {"action": "update", "id": "1", "status": "in_progress"}, readonly_ctx
    )
    assert progress.success is True


# ---- r24/C: skipped is a runner verdict, never the model's ------------------


@pytest.mark.asyncio
async def test_model_cannot_mark_an_item_skipped(tool, ctx):
    await tool.execute({"action": "add", "content": "work"}, ctx)
    r = await tool.execute({"action": "update", "id": "1", "status": "skipped"}, ctx)
    assert r.success is False
    assert r.error == "runner_only_status"
    assert ctx.task.get("1").status == "pending"


def test_skipped_is_not_advertised_in_the_schema(tool):
    enum = tool.params_json_schema["properties"]["status"]["enum"]
    assert "skipped" not in enum
    assert "pending" in enum and "blocked" in enum


@pytest.mark.asyncio
async def test_plan_executing_freezes_the_contract_but_not_status(tool, ctx):
    """Plan-as-Goal has no per-item scope: the whole turn is the execution."""
    await tool.execute({"action": "add", "content": "a"}, ctx)
    ctx.plan_executing = True

    added = await tool.execute({"action": "add", "content": "b"}, ctx)
    changed_verify = await tool.execute(
        {"action": "update", "id": "1", "verify": "true"}, ctx
    )
    completed = await tool.execute(
        {"action": "update", "id": "1", "status": "completed", "evidence": "done"}, ctx
    )

    assert added.error == "frozen_plan"
    assert changed_verify.error == "frozen_verify"
    assert completed.success is True
    assert ctx.task.get("1").status == "completed"
