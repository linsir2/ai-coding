"""TDD (M1b T1): a sub-agent runs as a named agent, or says why not.

The target is resolved by the **runner**, because the table lives there.  The
tool forwards what the model asked for verbatim: ``None`` means "not
specified", which is what lets a loop with no table keep its pre-M1b
behaviour while an explicit target on such a loop fails loudly.

The child's permissions are the OC shape (F27): it inherits the parent's
**session** rules and uses **its own** agent's rules.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ai_coding.agents.registry import builtin_catalog
from ai_coding.config.models import AIConfig
from ai_coding.core.subagent import SubAgentRunner
from ai_coding.domain.loop import StepResult
from ai_coding.tools.base import ToolContext
from ai_coding.tools.subagent_tool import SubAgentTool


class _Engine:
    def __init__(self) -> None:
        self.seen: list[list[Any]] = []

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.seen.append(list(messages))
        return StepResult(text="child done", finish_reason="stop")

    async def aclose(self) -> None:
        return None


def _runner(tmp_path: Path, *, agents: Any | None = None) -> SubAgentRunner:
    from ai_coding.tools.builder import build_tool_registry

    return SubAgentRunner(
        AIConfig(),
        engine_factory=_Engine,
        build_tools=lambda wd: build_tool_registry(
            _app_config(), wd or str(tmp_path), None, include_subagent=False
        ),
        workspace=str(tmp_path),
        timeout_seconds=30.0,
        agents=agents,
    )


def _app_config() -> Any:
    from ai_coding.config.models import AppConfig

    return AppConfig(
        models={},
        default_model="",
        ai=AIConfig(stop_policy="natural", max_tool_iterations=2),
    )


@pytest.mark.asyncio
async def test_the_default_target_is_general(tmp_path: Path):
    runner = _runner(tmp_path, agents=builtin_catalog())
    result = await runner.run("look", isolate=False)
    assert result.status == "completed"


@pytest.mark.asyncio
async def test_an_unknown_target_is_refused_and_lists_the_real_ones(tmp_path: Path):
    runner = _runner(tmp_path, agents=builtin_catalog())
    result = await runner.run("look", isolate=False, agent="nope")
    assert result.status == "error"
    assert "nope" in (result.error or "")
    assert "explore" in (result.error or "")


@pytest.mark.asyncio
async def test_a_primary_agent_cannot_be_spawned(tmp_path: Path):
    runner = _runner(tmp_path, agents=builtin_catalog())
    result = await runner.run("look", isolate=False, agent="plan")
    assert result.status == "error"
    assert "primary" in (result.error or "")


@pytest.mark.asyncio
async def test_an_explicit_target_without_a_table_says_so(tmp_path: Path):
    runner = _runner(tmp_path, agents=None)
    result = await runner.run("look", isolate=False, agent="explore")
    assert result.status == "error"
    assert "not available" in (result.error or "")


@pytest.mark.asyncio
async def test_a_default_run_without_a_table_keeps_the_old_behaviour(tmp_path: Path):
    runner = _runner(tmp_path, agents=None)
    result = await runner.run("look", isolate=False)
    assert result.status == "completed"


# ---- the tool side ----------------------------------------------------------


class _RecordingRunner:
    context_mode = "full"
    timeout_seconds = 1.0

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run(
        self, task, name, *, worktree_manager=None, isolate=True, parent=None,
        parent_span="", observer=None, output_schema=None, agent=None,
    ):
        from ai_coding.domain.subagent import SubagentResult

        self.calls.append({"task": task, "name": name, "agent": agent})
        return SubagentResult(status="completed", conclusion="ok")


@pytest.mark.asyncio
async def test_the_tool_forwards_the_target_verbatim():
    runner = _RecordingRunner()
    tool = SubAgentTool(runner=runner, isolate=False)
    await tool.execute({"task": "x"}, ToolContext())
    await tool.execute({"task": "x", "agent": "explore"}, ToolContext())
    assert runner.calls[0]["agent"] is None
    assert runner.calls[1]["agent"] == "explore"


def test_the_schema_describes_agent_and_name_as_different_things():
    tool = SubAgentTool(runner=_RecordingRunner())
    props = tool.params_json_schema["properties"]
    assert "agent" in props
    assert "agent" in props["agent"]["description"]
    assert "id" in props["name"]["description"]


# ---- the child's rules ------------------------------------------------------


@pytest.mark.asyncio
async def test_the_child_uses_its_own_agent_and_inherits_the_parent_session_rules(
    tmp_path: Path,
):
    runner = _runner(tmp_path, agents=builtin_catalog())
    from ai_coding.core.subagent import ParentContext
    from ai_coding.security.permissions import Effect, Rule

    parent_rules = (Rule(action="read", resource=".env", effect=Effect.DENY),)
    seen_tools: list[list[dict]] = []

    class _SpyEngine(_Engine):
        async def step(self, messages, *, tools, instructions=None, on_delta=None):
            seen_tools.append(list(tools))
            return await super().step(
                messages, tools=tools, instructions=instructions, on_delta=on_delta
            )

    runner._engine_factory = _SpyEngine  # noqa: SLF001 — the child's engine
    await runner.run(
        "look",
        isolate=False,
        agent="explore",
        parent=ParentContext(messages=(), instructions="", permissions=parent_rules),
    )

    # The child's **tool list** is the observable: it speaks as explore, so
    # the mutating tools are not in it.  Asserting on the table alone would
    # pass even if the child's session said "build" and carried no agent
    # layer at all — which is exactly the hole this case exists to close.
    assert seen_tools, "the child never called the model"
    names = {
        tool.get("function", {}).get("name")
        for tool in seen_tools[0]
        if isinstance(tool, dict)
    }
    assert "read" in names, f"the child lost its read tool: {sorted(names)}"
    for hidden in ("write", "edit", "bash"):
        assert hidden not in names, (
            f"{hidden} was offered to an explore child: {sorted(names)}"
        )


def test_resolving_a_target_rejects_a_primary(tmp_path: Path):
    runner = _runner(tmp_path, agents=builtin_catalog())
    spec, problem = runner._resolve_target("plan")  # noqa: SLF001
    assert spec is None
    assert "primary" in problem


def test_the_default_subagent_is_general(tmp_path: Path):
    runner = _runner(tmp_path, agents=builtin_catalog())
    spec, problem = runner._resolve_target(None)  # noqa: SLF001
    assert problem == ""
    assert spec is not None and spec.id == "general"
