"""P2-C3: the model-facing merge tool delegates to Scheduler.merge."""

from __future__ import annotations

import pytest

from ai_coding.tools.merge_agent_tool import MergeAgentTool


class _Scheduler:
    def __init__(self, outcome=None) -> None:
        self.calls = []
        self.outcome = outcome or {"status": "merged", "commit": "c" * 40}

    async def merge(self, target, verify_cmd=None, *, session_id="", workspace=""):
        self.calls.append((target, verify_cmd, session_id, workspace))
        return dict(self.outcome)


def test_tool_contract_is_control_and_no_dispatcher_claim():
    tool = MergeAgentTool(scheduler=_Scheduler())
    assert tool.name == "merge_agent"
    assert tool.permission_name({}) == "merge_agent"
    assert tool.mutates_world_for({}) is True
    assert tool.parallel_safe is False
    assert tool.occupies_slot is False
    assert tool.write_paths_for({}, None) is None


@pytest.mark.asyncio
async def test_merged_result_is_success_and_carries_data():
    scheduler = _Scheduler()
    tool = MergeAgentTool(scheduler=scheduler)
    result = await tool.execute(
        {"target": "run-1", "verify_cmd": "pytest -q"}, ctx=None
    )

    assert result.success is True
    assert scheduler.calls == [("run-1", "pytest -q", "", "")]
    assert result.data["status"] == "merged"
    assert result.data["commit"] == "c" * 40


@pytest.mark.asyncio
async def test_nothing_to_merge_is_success_with_a_clear_message():
    tool = MergeAgentTool(
        scheduler=_Scheduler({"status": "merge_skipped", "error": "nothing_to_merge"})
    )
    result = await tool.execute({"target": "run-1"}, ctx=None)
    assert result.success is True
    assert "nothing" in result.output.lower()


@pytest.mark.asyncio
async def test_failure_maps_to_tool_error_and_preserves_target():
    tool = MergeAgentTool(scheduler=_Scheduler({"status": "conflict",
                                                "conflicting_paths": ["a.py"]}))
    result = await tool.execute({"target": "run-1"}, ctx=None)
    assert result.success is False
    assert result.detail == "conflict"
    assert result.data["conflicting_paths"] == ["a.py"]


@pytest.mark.asyncio
async def test_blank_target_is_refused_without_calling_scheduler():
    scheduler = _Scheduler()
    tool = MergeAgentTool(scheduler=scheduler)
    result = await tool.execute({"target": "  "}, ctx=None)
    assert result.success is False
    assert scheduler.calls == []


@pytest.mark.asyncio
async def test_no_scheduler_is_unavailable():
    tool = MergeAgentTool(scheduler=None)
    result = await tool.execute({"target": "run-1"}, ctx=None)
    assert result.success is False
    assert result.detail == "unavailable"
