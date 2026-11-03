"""TDD (P2-A): the one envelope shared by the parent and its children.

``AgentPacket`` is header + ``SubagentTask`` + ``SubagentResult``; the ledger,
the tool data, the background notice/event and the list rows are all projections
of it.  The first two decisions pinned here are the ones v2 got wrong: an
objective-only task must render **verbatim** (no ``[TASK]`` wrapper, so the free
form path is byte-identical), and the flat wire carries the version once.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.subagent import SubAgentRunner
from ai_coding.domain.loop import StepResult
from ai_coding.domain.subagent import (
    SUBAGENT_PACKET_VERSION,
    AgentPacket,
    BackgroundTask,
    PacketHeader,
    SubagentBrief,
    SubagentResult,
    SubagentTask,
)
from ai_coding.tools.registry import ToolRegistry

#: The result keys v2 shipped (without ``v``).  v3 may only add.
RESULT_KEYS_V2 = {
    "status", "conclusion", "todos", "artifacts", "iterations", "workdir",
    "ledger_path", "error", "context_mode", "truncated", "exit_reason",
    "usage", "facts", "schema_valid", "schema_errors", "worktree_branch",
    "commit",
}


def test_task_objective_only_renders_verbatim():
    task = SubagentTask(brief=SubagentBrief(objective="找出登录入口"))

    assert task.render() == "找出登录入口"
    assert "[TASK]" not in task.render()


def test_task_with_sections_uses_the_frozen_envelope():
    brief = SubagentBrief(
        objective="找出登录入口",
        context="目标：给房间加价格",
        scope=("src/auth/",),
        constraints=("只读：不要写文件。",),
    )

    rendered = SubagentTask(brief=brief).render()

    assert rendered == brief.render()
    assert rendered.startswith("[TASK]\n")


def test_task_round_trips_through_dict():
    brief = SubagentBrief(
        objective="找出登录入口",
        context="目标：给房间加价格",
        scope=("src/auth/",),
        constraints=("只读：不要写文件。",),
    )
    task = SubagentTask(
        brief=brief,
        context_mode="task_only",
        agent="explore",
        readonly=True,
        isolate=False,
        output_schema={"type": "object"},
    )

    assert SubagentBrief.from_dict(brief.to_dict()) == brief
    assert SubagentTask.from_dict(task.to_dict()).brief == brief
    assert SubagentTask.from_dict(task.to_dict()) == task


def test_packet_is_flat_and_versioned():
    header = PacketHeader(
        message_id="abc123def456", run_id="run-1", origin_session="s1"
    )
    task = SubagentTask.from_objective("do it")
    result = SubagentResult(status="completed")

    payload = AgentPacket(header=header, task=task, result=result).to_dict()

    assert payload["v"] == SUBAGENT_PACKET_VERSION == 3
    assert payload["message_id"] == "abc123def456"
    assert payload["run_id"] == "run-1"
    assert payload["origin_session"] == "s1"
    assert payload["task"]["brief"]["objective"] == "do it"
    assert RESULT_KEYS_V2 <= set(payload)
    assert {"changed_paths", "commit_error", "degraded"} <= set(payload)


def test_base_commit_is_additive_in_packet_and_row():
    header = PacketHeader(message_id="abc123def456")
    result = SubagentResult(status="completed", base_commit="a" * 40)
    payload = AgentPacket(header=header, task=None, result=result).to_dict()
    assert payload["base_commit"] == "a" * 40

    row = BackgroundTask(
        run_id="r1", status="completed", base_commit="b" * 40
    )
    assert row.to_dict()["baseCommit"] == "b" * 40
    assert BackgroundTask.from_dict(row.to_dict()) == row


def test_background_row_round_trips_write_facts():
    row = BackgroundTask(
        run_id="r1",
        name="probe",
        task="do it",
        status="completed",
        started=1.0,
        ended=2.0,
        worktree_branch="ai_sub_probe",
        commit="abc123",
        changed_paths=("a.py", "b.py"),
        degraded="worktree_unavailable",
    )

    assert BackgroundTask.from_dict(row.to_dict()) == row


class _Engine(ChatEngine):
    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        return StepResult(text="done", finish_reason="stop")


@pytest.mark.asyncio
async def test_header_message_id_names_the_ledger_file(tmp_path):
    runner = SubAgentRunner(
        object(),
        engine_factory=lambda: _Engine(),
        build_tools=lambda _workdir: ToolRegistry(),
        settings=AIConfig(stop_policy="natural"),
        ledger_root=tmp_path,
        workspace=str(tmp_path),
    )

    result = await runner.run("do it")

    assert result.ledger_path is not None
    path = Path(result.ledger_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert path.stem == payload["message_id"]
    assert payload["v"] == 3
