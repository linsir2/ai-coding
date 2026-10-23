"""M4a regression pins: todo ownership and delegated permissions.

The M4 design audit found both behaviours already implemented.  These tests
make the two boundaries explicit so a future refactor cannot quietly turn a
child's private list into the parent's, or a child's agent allow into a way
around a parent's deny.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_coding.agents.registry import builtin_catalog
from ai_coding.config.models import AIConfig, AppConfig
from ai_coding.core.subagent import SubAgentRunner
from ai_coding.domain.agents import SUBAGENT, AgentCatalog, AgentSpec
from ai_coding.domain.loop import StepResult
from ai_coding.domain.message import ToolCallRef
from ai_coding.domain.subagent import ParentContext
from ai_coding.domain.task import TaskState
from ai_coding.security.permission_gate import PermissionDecision, PermissionGate
from ai_coding.security.permissions import Effect, Rule


class _Engine:
    def __init__(self, steps: list[StepResult]) -> None:
        self._steps = list(steps)

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        return self._steps.pop(0) if self._steps else StepResult(
            text="done", finish_reason="stop"
        )

    async def aclose(self) -> None:
        return None


def _app_config() -> AppConfig:
    return AppConfig(
        models={},
        default_model="",
        ai=AIConfig(stop_policy="natural", max_tool_iterations=4),
    )


def _runner(
    tmp_path: Path,
    engine: _Engine,
    *,
    agents: AgentCatalog | None = None,
) -> SubAgentRunner:
    from ai_coding.tools.builder import build_tool_registry

    return SubAgentRunner(
        _app_config(),
        engine_factory=lambda: engine,
        build_tools=lambda wd: build_tool_registry(
            _app_config(), wd or str(tmp_path), None, include_subagent=False
        ),
        workspace=str(tmp_path),
        timeout_seconds=30.0,
        settings=AIConfig(stop_policy="natural", max_tool_iterations=4),
        agents=agents,
    )


def _tool_call(name: str, arguments: str) -> StepResult:
    return StepResult(
        tool_calls=[ToolCallRef(id="call-1", name=name, arguments=arguments)],
        finish_reason="tool_calls",
    )


@pytest.mark.asyncio
async def test_a_child_writes_its_own_task_list_but_not_the_parents(tmp_path):
    """A todo belongs to one owning agent session, not to a global task."""
    parent_task = TaskState(goal="parent goal")
    parent_task.add("parent item", item_id="1")
    before = parent_task.to_dict()
    engine = _Engine(
        [
            _tool_call(
                "todo_write",
                '{"action":"add","content":"child item"}',
            ),
            StepResult(text="done", finish_reason="stop"),
        ]
    )

    result = await _runner(tmp_path, engine, agents=builtin_catalog()).run(
        "child work", isolate=False, agent="general"
    )

    assert [entry["content"] for entry in result.todos] == ["child item"]
    assert parent_task.to_dict() == before


@pytest.mark.asyncio
async def test_the_childs_list_returns_in_the_parents_report(tmp_path):
    """The child's list is reported back; the parent's own list is untouched."""
    parent_task = TaskState(goal="parent goal")
    parent_task.add("parent item", item_id="1")
    engine = _Engine(
        [
            _tool_call(
                "todo_write",
                '{"action":"add","content":"child item"}',
            ),
            StepResult(text="done", finish_reason="stop"),
        ]
    )

    result = await _runner(tmp_path, engine, agents=builtin_catalog()).run(
        "child work", isolate=False, agent="general"
    )

    assert [entry["content"] for entry in result.todos] == ["child item"]
    assert len(parent_task.items) == 1
    assert parent_task.items[0].content == "parent item"


@pytest.mark.asyncio
async def test_a_child_inherits_the_parents_session_rules(tmp_path):
    """A parent's live deny reaches the work it delegates."""
    engine = _Engine(
        [
            _tool_call("write", '{"path":"blocked.txt","content":"x"}'),
            StepResult(text="done", finish_reason="stop"),
        ]
    )
    parent_rules = (Rule(action="write", resource="*", effect=Effect.DENY),)

    await _runner(tmp_path, engine, agents=builtin_catalog()).run(
        "write it",
        isolate=False,
        agent="general",
        parent=ParentContext(permissions=parent_rules),
    )

    assert not (tmp_path / "blocked.txt").exists()


@pytest.mark.asyncio
async def test_a_child_still_applies_its_own_agent_rules(tmp_path):
    """Inheritance adds the parent layer; it does not erase the child's own."""
    engine = _Engine(
        [
            _tool_call("write", '{"path":"explore.txt","content":"x"}'),
            StepResult(text="done", finish_reason="stop"),
        ]
    )

    await _runner(tmp_path, engine, agents=builtin_catalog()).run(
        "try to write", isolate=False, agent="explore"
    )

    assert not (tmp_path / "explore.txt").exists()


@pytest.mark.asyncio
async def test_a_child_agent_rule_cannot_reopen_a_parent_deny(tmp_path):
    """The parent's boundary is later in the chain than the child's allow."""
    catalog = AgentCatalog(
        specs={
            "build": AgentSpec(id="build", name="Build"),
            "writer": AgentSpec(
                id="writer",
                name="Writer",
                kind=SUBAGENT,
                permissions=(
                    {"action": "edit", "resource": "src/*", "effect": "allow"},
                ),
            ),
        }
    )
    engine = _Engine(
        [
            _tool_call("write", '{"path":"src/reopened.txt","content":"x"}'),
            StepResult(text="done", finish_reason="stop"),
        ]
    )
    parent_rules = (Rule(action="write", resource="*", effect=Effect.DENY),)

    await _runner(tmp_path, engine, agents=catalog).run(
        "write it",
        isolate=False,
        agent="writer",
        parent=ParentContext(permissions=parent_rules),
    )

    assert not (tmp_path / "src" / "reopened.txt").exists()


@pytest.mark.asyncio
async def test_a_child_deny_beats_a_parent_session_allow(tmp_path):
    """An agent narrowing must remain absolute even when the parent allows."""
    catalog = AgentCatalog(
        specs={
            "build": AgentSpec(id="build", name="Build"),
            "limited": AgentSpec(
                id="limited",
                name="Limited",
                kind=SUBAGENT,
                permissions=(
                    {"action": "write", "resource": "*", "effect": "deny"},
                ),
            ),
        }
    )
    engine = _Engine(
        [
            _tool_call("write", '{"path":"denied.txt","content":"x"}'),
            StepResult(text="done", finish_reason="stop"),
        ]
    )
    parent_rules = (Rule(action="write", resource="*", effect=Effect.ALLOW),)

    await _runner(tmp_path, engine, agents=catalog).run(
        "write it",
        isolate=False,
        agent="limited",
        parent=ParentContext(permissions=parent_rules),
    )

    assert not (tmp_path / "denied.txt").exists()


def test_a_plan_session_cannot_spawn_a_subagent():
    """Plan is a policy posture, so the gate must refuse delegation itself."""
    rules = builtin_catalog().rules("plan")
    decision = PermissionGate().check(
        "subagent",
        {"task": "delegate"},
        agent_rules=rules,
        posture="plan",
    )
    assert decision.decision is PermissionDecision.DENY
