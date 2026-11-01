"""C-5b: a child turn's iteration cap (industry parity).

Codex, opencode, dsh and Claude Code run sub-agents with **no iteration cap**;
gemini-cli is the counter-example (30 turns).  A child is bounded by its wall
clock, by compaction, and by the model stopping.  These tests pin that our child
no longer inherits the parent's ``max_tool_iterations`` (the bug a real run
found: a parent at 10 cut a six-file task in half).
"""

import asyncio

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.subagent import SubAgentRunner
from ai_coding.core.turn_policy import TurnPolicyConfig
from ai_coding.domain.loop import StepResult
from ai_coding.tools.registry import ToolRegistry


def test_zero_or_negative_iterations_means_no_cap():
    assert TurnPolicyConfig.from_ai_config(
        AIConfig(max_tool_iterations=0)
    ).max_iterations == -1
    assert TurnPolicyConfig.from_ai_config(
        AIConfig(max_tool_iterations=-1)
    ).max_iterations == -1


def test_a_positive_cap_is_kept():
    assert TurnPolicyConfig.from_ai_config(
        AIConfig(max_tool_iterations=7)
    ).max_iterations == 7


def _runner(tmp_path, **over) -> SubAgentRunner:
    kwargs = {
        "app_config": object(),
        "engine_factory": lambda: _Engine(),
        "build_tools": lambda _workdir: ToolRegistry(),
        "ledger_root": tmp_path,
        "workspace": str(tmp_path),
        "timeout_seconds": 30.0,
        "settings": AIConfig(stop_policy="natural"),
    }
    kwargs.update(over)
    return SubAgentRunner(**kwargs)


class _Engine(ChatEngine):
    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        return StepResult(text="done", finish_reason="stop")

    async def aclose(self) -> None:
        return None


def test_a_child_defaults_to_no_iteration_cap(tmp_path):
    runner = _runner(tmp_path, settings=AIConfig(max_tool_iterations=10))

    child = runner._subagent_settings()

    assert child.max_tool_iterations == -1
    # The parent keeps its own cap; only the child is unbounded.
    assert runner._settings.max_tool_iterations == 10


def test_an_explicit_child_cap_is_honoured(tmp_path):
    runner = _runner(
        tmp_path,
        settings=AIConfig(
            max_tool_iterations=10, subagent_max_tool_iterations=30
        ),
    )

    assert runner._subagent_settings().max_tool_iterations == 30


@pytest.mark.asyncio
async def test_a_long_child_turn_is_not_cut_by_the_parent_cap(tmp_path):
    """20 tool batches with the parent at 3: the child must still finish."""

    class _TwentySteps(ChatEngine):
        def __init__(self) -> None:
            self.calls = 0

        async def step(self, messages, *, tools, instructions=None, on_delta=None):
            self.calls += 1
            return StepResult(text="ok", finish_reason="stop")

        async def aclose(self) -> None:
            return None

    engine = _TwentySteps()
    runner = _runner(
        tmp_path,
        engine_factory=lambda: engine,
        settings=AIConfig(max_tool_iterations=3, stop_policy="natural"),
    )

    run_id = runner.spawn_background("long", name="long", emit=lambda _e: None)
    for _ in range(300):
        rows = [row for row in runner.task_list() if row.run_id == run_id]
        if rows and rows[0].status != "running":
            break
        await asyncio.sleep(0.02)

    row = [row for row in runner.task_list() if row.run_id == run_id][0]
    assert row.status == "completed", row
