"""TDD (M2-4): todo_write tool — in-memory structured todo list."""

import pytest

from ai_coding.tools.todo_write_tool import TodoWriteTool


@pytest.fixture
def tool():
    return TodoWriteTool()


@pytest.mark.asyncio
async def test_todo_add(tool):
    r = await tool.execute({"action": "add", "content": "do the thing"})
    assert r.success is True
    assert "do the thing" in r.output


@pytest.mark.asyncio
async def test_todo_update_status(tool):
    await tool.execute({"action": "add", "content": "task a"})
    r = await tool.execute({"action": "update", "id": "1", "status": "completed"})
    assert r.success is True
    # list should show it completed
    lr = await tool.execute({"action": "list"})
    assert "completed" in lr.output


@pytest.mark.asyncio
async def test_todo_list(tool):
    await tool.execute({"action": "add", "content": "task 1"})
    await tool.execute({"action": "add", "content": "task 2"})
    r = await tool.execute({"action": "list"})
    assert r.success is True
    assert "task 1" in r.output
    assert "task 2" in r.output


@pytest.mark.asyncio
async def test_todo_unknown_action_fails(tool):
    r = await tool.execute({"action": "bogus"})
    assert r.success is False
    assert "unknown action" in r.output.lower()


@pytest.mark.asyncio
async def test_todo_add_missing_content(tool):
    r = await tool.execute({"action": "add"})
    assert r.success is False


@pytest.mark.asyncio
async def test_todo_ids_are_incrementing(tool):
    await tool.execute({"action": "add", "content": "a"})
    await tool.execute({"action": "add", "content": "b"})
    r = await tool.execute({"action": "list"})
    assert "1" in r.output
    assert "2" in r.output
