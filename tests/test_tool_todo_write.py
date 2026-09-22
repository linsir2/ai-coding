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
async def test_list_returns_rendered_task(tool, ctx):
    await tool.execute({"action": "add", "content": "task 1"}, ctx)
    await tool.execute({"action": "add", "content": "task 2"}, ctx)
    r = await tool.execute({"action": "list"}, ctx)
    assert r.success is True
    assert "task 1" in r.output and "task 2" in r.output
    assert "进度: 0/2" in r.output


@pytest.mark.asyncio
async def test_clear_empties_task(tool, ctx):
    await tool.execute({"action": "add", "content": "a"}, ctx)
    r = await tool.execute({"action": "clear"}, ctx)
    assert r.success is True
    assert ctx.task.total == 0


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
