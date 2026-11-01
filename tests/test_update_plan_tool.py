"""C-1: ``update_plan`` — Codex's plan tool, byte-compatible.

Defaults (``plan_verify_items=False``) must match Codex exactly: the schema is
``{explanation?, plan[{step,status}]}``, the whole list is replaced with ids
1..N, and the tool result is the fixed text ``"Plan updated"``.  The opt-in
verify switch is the one documented deviation.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ai_coding.domain.task import TaskState
from ai_coding.tools.base import ToolContext
from ai_coding.tools.builder import build_tool_registry
from ai_coding.tools.update_plan_tool import UpdatePlanTool


def _task_with_old_items() -> TaskState:
    task = TaskState(goal="old goal")
    task.add("old one")
    task.add("old two")
    return task


def _tool_cfg(workspace: str) -> SimpleNamespace:
    return SimpleNamespace(
        tools=SimpleNamespace(
            bash=SimpleNamespace(enabled=True, timeout_seconds=60),
            read=SimpleNamespace(enabled=True, max_file_size=1 << 20),
            write=SimpleNamespace(enabled=True),
            edit=SimpleNamespace(enabled=True),
            glob=SimpleNamespace(enabled=True),
        ),
        ai=SimpleNamespace(subagent_worktree_isolation=True),
    )


def test_schema_matches_codex_exactly() -> None:
    schema = UpdatePlanTool().params_json_schema
    assert set(schema) == {"type", "properties", "required"}
    assert set(schema["properties"]) == {"explanation", "plan"}
    assert schema["required"] == ["plan"]

    plan = schema["properties"]["plan"]
    assert plan["type"] == "array"
    assert plan["minItems"] == 1
    step = plan["items"]
    assert set(step) == {"type", "properties", "required"}
    assert set(step["properties"]) == {"step", "status"}
    assert step["required"] == ["step", "status"]
    assert step["properties"]["status"]["enum"] == [
        "pending",
        "in_progress",
        "completed",
    ]


@pytest.mark.asyncio
async def test_result_is_the_fixed_text() -> None:
    task = TaskState()
    ctx = ToolContext(task=task)
    result = await UpdatePlanTool().execute(
        {"plan": [{"step": "read the design", "status": "in_progress"}]}, ctx
    )

    assert result.success is True
    assert result.output == "Plan updated"
    assert result.error is None


@pytest.mark.asyncio
async def test_it_replaces_the_whole_list_with_ids_1_to_n() -> None:
    task = _task_with_old_items()
    ctx = ToolContext(task=task)
    result = await UpdatePlanTool().execute(
        {
            "explanation": "start",
            "plan": [
                {"step": "first step", "status": "completed"},
                {"step": "second step", "status": "in_progress"},
                {"step": "third step", "status": "pending"},
            ],
        },
        ctx,
    )

    assert result.success is True
    assert [item.id for item in task.items] == ["1", "2", "3"]
    assert [item.content for item in task.items] == [
        "first step",
        "second step",
        "third step",
    ]
    assert [item.status for item in task.items] == [
        "completed",
        "in_progress",
        "pending",
    ]
    # Full replacement: the pre-existing items are gone.
    assert all(item.content != "old one" for item in task.items)


@pytest.mark.asyncio
async def test_plan_mode_refuses_and_leaves_the_task_alone() -> None:
    task = _task_with_old_items()
    ctx = ToolContext(task=task, mode="plan")
    result = await UpdatePlanTool().execute(
        {"plan": [{"step": "write the plan", "status": "pending"}]}, ctx
    )

    assert result.success is False
    assert result.error == "plan_mode"
    assert result.output == (
        "update_plan is unavailable in Plan mode; present the plan as text."
    )
    assert [item.content for item in task.items] == ["old one", "old two"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "plan",
    [
        [{"step": "ok", "status": "blocked"}],
        [{"step": "ok", "status": "done"}],
        [{"step": "", "status": "pending"}],
        [{"step": "   ", "status": "pending"}],
        [{"step": 42, "status": "pending"}],
        [{"status": "pending"}],
        ["not an object"],
        [],
    ],
)
async def test_invalid_plans_are_refused(plan: list[Any]) -> None:
    task = TaskState()
    ctx = ToolContext(task=task)
    result = await UpdatePlanTool().execute({"plan": plan}, ctx)

    assert result.success is False
    assert result.error == "invalid_plan"


@pytest.mark.asyncio
async def test_verify_runs_for_completed_steps_when_enabled() -> None:
    calls: list[tuple[str, str, str]] = []

    async def verify_item(item_id: str, statement: str, command: str) -> tuple[bool, str]:
        calls.append((item_id, statement, command))
        return True, "ok"

    task = TaskState()
    ctx = ToolContext(task=task, plan_verify_items=True, verify_item=verify_item)
    result = await UpdatePlanTool().execute(
        {
            "plan": [
                {"step": "done step", "status": "completed", "verify": "true"},
                {"step": "todo step", "status": "pending"},
            ]
        },
        ctx,
    )

    assert result.success is True
    assert result.output == "Plan updated"
    assert calls == [("1", "done step", "true")]


@pytest.mark.asyncio
async def test_a_failed_verify_reverts_that_item_to_in_progress() -> None:
    async def failing_verify(
        item_id: str, statement: str, command: str
    ) -> tuple[bool, str]:
        return False, "exit 1"

    task = TaskState()
    ctx = ToolContext(task=task, plan_verify_items=True, verify_item=failing_verify)
    result = await UpdatePlanTool().execute(
        {
            "plan": [
                {"step": "done step", "status": "completed", "verify": "false"},
                {"step": "other step", "status": "completed"},
            ]
        },
        ctx,
    )

    assert result.success is False
    assert result.error == "verify_failed"
    assert result.output == "verify failed: exit 1"
    assert task.items[0].status == "in_progress"
    # The other item keeps the status it was written with.
    assert task.items[1].status == "completed"


@pytest.mark.asyncio
async def test_verify_is_not_run_when_the_switch_is_off() -> None:
    calls: list[tuple[str, str, str]] = []

    async def verify_item(item_id: str, statement: str, command: str) -> tuple[bool, str]:
        calls.append((item_id, statement, command))
        return False, "should not run"

    task = TaskState()
    ctx = ToolContext(task=task, verify_item=verify_item)
    result = await UpdatePlanTool().execute(
        {"plan": [{"step": "done step", "status": "completed", "verify": "false"}]},
        ctx,
    )

    assert result.success is True
    assert calls == []
    assert task.items[0].status == "completed"


def test_codex_registers_update_plan_and_no_present_plan(tmp_path) -> None:
    cfg = _tool_cfg(str(tmp_path))
    registry = build_tool_registry(cfg, str(tmp_path), None, user_prompt=object())

    names = set(registry.list_names())
    assert "update_plan" in names
    assert "present_plan" not in names
