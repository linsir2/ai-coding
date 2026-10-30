"""R7e-4: a sub-agent can be given a clean window instead of the parent's view.

`full` is the R1a-5 behaviour and stays the default (the parent's request view plus
its system prompt); `task_only` seeds an empty session and uses the composition
root's base instructions, which is the shape Anthropic's sub-agent guidance
describes - and it means the parent has to put the facts into `task` itself
(docs/r7e-analysis.md 3.4).
"""

from __future__ import annotations

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.subagent import SubAgentRunner
from ai_coding.domain.loop import StepResult
from ai_coding.domain.message import ChatMessage, ToolResult
from ai_coding.domain.subagent import ParentContext
from ai_coding.tools.base import BaseTool, ToolContext
from ai_coding.tools.registry import ToolRegistry


class _Engine(ChatEngine):
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.requests.append({
            "messages": list(messages),
            "instructions": instructions,
            "tools": list(tools),
        })
        return StepResult(text="做完了", tool_calls=[], finish_reason="stop")


class _Tool(BaseTool):
    parallel_safe = False

    def __init__(self, name: str, *, mutates_world: bool) -> None:
        self._name = name
        self.mutates_world = mutates_world

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._name

    @property
    def params_json_schema(self) -> dict[str, object]:
        return {"type": "object", "properties": {}}

    async def execute(self, params, ctx: ToolContext | None = None) -> ToolResult:
        return ToolResult(success=True)


def _runner(engine: _Engine, *, base_instructions: str = "BASE INSTRUCTIONS", **overrides):
    cfg = AIConfig(max_tool_iterations=2, stop_policy="natural", **overrides)
    return SubAgentRunner(
        cfg,
        engine_factory=lambda: engine,
        build_tools=lambda workdir: ToolRegistry(),
        settings=cfg,
        workspace=".",
        base_instructions=base_instructions,
    )


def _parent() -> ParentContext:
    return ParentContext(
        messages=(ChatMessage(role="user", content="parent-said-this", timestamp="t"),),
    )


@pytest.mark.asyncio
async def test_the_default_mode_is_full():
    engine = _Engine()
    result = await _runner(engine).run("do it", parent=_parent())
    contents = [m.content or "" for m in engine.requests[0]["messages"]]
    assert any("parent-said-this" in content for content in contents)
    # r25/v2: the child builds its own prompt from the composition root's base;
    # it does not inherit the parent's assembled text (that duplicated sections).
    instructions = engine.requests[0]["instructions"]
    assert instructions.startswith("BASE INSTRUCTIONS")
    assert "PARENT INSTRUCTIONS" not in instructions
    assert instructions.count("<methods>") == 1
    assert result.context_mode == "full"


@pytest.mark.asyncio
async def test_task_only_starts_with_an_empty_history():
    engine = _Engine()
    await _runner(engine, subagent_context_mode="task_only").run("do it", parent=_parent())
    contents = [m.content or "" for m in engine.requests[0]["messages"]]
    assert not any("parent-said-this" in content for content in contents)
    assert any("do it" in content for content in contents)


@pytest.mark.asyncio
async def test_task_only_uses_the_base_instructions():
    engine = _Engine()
    await _runner(engine, subagent_context_mode="task_only").run("do it", parent=_parent())
    instructions = engine.requests[0]["instructions"]
    assert instructions.startswith("BASE INSTRUCTIONS")
    assert "<methods>" in instructions  # 13 §2.3: 子代理同享（有意）


@pytest.mark.asyncio
async def test_task_only_with_no_base_instructions_has_no_system_prompt():
    """兼容性护栏：既有用例断言 instructions is None，空串不能变成空 PromptAssembler。"""
    engine = _Engine()
    cfg = AIConfig(subagent_context_mode="task_only")
    runner = SubAgentRunner(
        cfg,
        engine_factory=lambda: engine,
        build_tools=lambda workdir: ToolRegistry(),
        settings=cfg,
        workspace=".",
    )
    await runner.run("do it", parent=_parent())
    assert engine.requests[0]["instructions"] is None


@pytest.mark.asyncio
async def test_the_packet_reports_the_mode():
    result = await _runner(_Engine(), subagent_context_mode="task_only").run(
        "t", parent=_parent()
    )
    assert result.context_mode == "task_only"
    assert result.to_dict()["context_mode"] == "task_only"


@pytest.mark.asyncio
async def test_full_mode_does_not_reuse_the_parent_prompt():
    """r25/v2: an assembled prompt belongs to one tool surface, not the child's.

    Reusing it made the child append project/skills/methods a second time and
    advertise tools it does not carry (measured 617 -> 1140 chars).
    """
    engine = _Engine()
    runner = _runner(engine)
    assert runner.context_mode == "full"
    await runner.run("t", parent=_parent())
    instructions = engine.requests[0]["instructions"]
    assert instructions.startswith("BASE INSTRUCTIONS")
    assert "PARENT INSTRUCTIONS" not in instructions
    assert instructions.count("<methods>") == 1
    # The parent's *messages* are what full mode inherits.
    contents = [m.content or "" for m in engine.requests[0]["messages"]]
    assert any("parent-said-this" in content for content in contents)


@pytest.mark.asyncio
async def test_task_only_without_a_parent_uses_the_base_instructions():
    engine = _Engine()
    await _runner(engine, subagent_context_mode="task_only").run("t")
    assert engine.requests[0]["instructions"].startswith("BASE INSTRUCTIONS")


# ---- B0: per-call overrides (fan-out must not copy the parent N times) ------


@pytest.mark.asyncio
async def test_a_per_call_task_only_override_wins_over_the_global_full():
    engine = _Engine()
    result = await _runner(engine).run(
        "调查路由", parent=_parent(), context_mode="task_only"
    )
    contents = [m.content or "" for m in engine.requests[0]["messages"]]
    assert not any("parent-said-this" in content for content in contents)
    assert any("调查路由" in content for content in contents)
    assert result.context_mode == "task_only"


@pytest.mark.asyncio
async def test_an_unknown_per_call_mode_falls_back_to_the_global_one():
    engine = _Engine()
    result = await _runner(engine).run("t", parent=_parent(), context_mode="bogus")
    assert result.context_mode == "full"
    contents = [m.content or "" for m in engine.requests[0]["messages"]]
    assert any("parent-said-this" in content for content in contents)


@pytest.mark.asyncio
async def test_a_readonly_child_is_not_advertised_mutating_tools():
    import json

    engine = _Engine()
    registry = ToolRegistry()
    registry.register(_Tool("read", mutates_world=False))
    registry.register(_Tool("write", mutates_world=True))
    cfg = AIConfig(max_tool_iterations=2, stop_policy="natural")
    runner = SubAgentRunner(
        cfg,
        engine_factory=lambda: engine,
        build_tools=lambda workdir: registry,
        settings=cfg,
        workspace=".",
    )

    await runner.run("t", parent=_parent(), readonly=True)

    tools = json.dumps(engine.requests[0]["tools"], ensure_ascii=False)
    assert '"read"' in tools
    assert '"write"' not in tools


class _Project:
    def load(self) -> str:
        return "PROJECT RULES"


@pytest.mark.asyncio
async def test_full_mode_adds_the_project_section_exactly_once():
    engine = _Engine()
    cfg = AIConfig(max_tool_iterations=2, stop_policy="natural")
    runner = SubAgentRunner(
        cfg,
        engine_factory=lambda: engine,
        build_tools=lambda workdir: ToolRegistry(),
        settings=cfg,
        workspace=".",
        base_instructions="BASE INSTRUCTIONS",
        project=_Project(),
    )

    await runner.run("t", parent=_parent())

    instructions = engine.requests[0]["instructions"]
    assert instructions.count("PROJECT RULES") == 1
    assert instructions.count("<methods>") == 1


@pytest.mark.asyncio
async def test_task_only_keeps_the_clean_window_without_the_project_section():
    engine = _Engine()
    cfg = AIConfig(
        max_tool_iterations=2, stop_policy="natural", subagent_context_mode="task_only"
    )
    runner = SubAgentRunner(
        cfg,
        engine_factory=lambda: engine,
        build_tools=lambda workdir: ToolRegistry(),
        settings=cfg,
        workspace=".",
        base_instructions="BASE INSTRUCTIONS",
        project=_Project(),
    )

    await runner.run("t", parent=_parent())

    instructions = engine.requests[0]["instructions"]
    assert "PROJECT RULES" not in instructions
    contents = [m.content or "" for m in engine.requests[0]["messages"]]
    assert not any("parent-said-this" in content for content in contents)
