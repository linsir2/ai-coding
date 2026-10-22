"""The rule layer reaches the two places that used to sit outside it (R20/R21).

Acceptance commands run a shell without going through dispatch, and a sub-agent
gets a fresh session.  Both now see the same rules: a denied proof command
fails instead of running, and a parent's session layer is in force in its child.
"""

from __future__ import annotations

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.acceptance import verify_items
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.subagent import SubAgentRunner
from ai_coding.domain.acceptance import AcceptanceItem
from ai_coding.domain.loop import StepResult
from ai_coding.domain.message import ToolCallRef, ToolResult
from ai_coding.domain.session import SessionData
from ai_coding.domain.subagent import ParentContext
from ai_coding.security.permissions import Effect, Rule
from ai_coding.service.session_service import SessionService
from ai_coding.tools.base import BaseTool, ToolContext
from ai_coding.tools.registry import ToolRegistry
from ai_coding.tools.subagent_tool import SubAgentTool


def _item(verify: str) -> AcceptanceItem:
    return AcceptanceItem(id="AC-1", statement="it holds", verify=verify)


# ---- R20: the proof shell asks the rules before it runs ----------------------


@pytest.mark.asyncio
async def test_a_rule_denied_proof_command_does_not_run(tmp_path):
    marker = tmp_path / "ran.marker"

    items = await verify_items(
        [_item(f"touch {marker}")],
        str(tmp_path),
        deny_reason=lambda action, params: "denied by rule: bash *",
    )

    assert items[0].passed is False
    assert "denied by rule: bash *" in items[0].output
    assert not marker.exists()


@pytest.mark.asyncio
async def test_a_proof_command_the_rules_allow_still_runs(tmp_path):
    marker = tmp_path / "ran.marker"

    items = await verify_items(
        [_item(f"touch {marker}")],
        str(tmp_path),
        deny_reason=lambda action, params: "",
    )

    assert items[0].passed is True
    assert marker.exists()


class _Text(ChatEngine):
    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        return StepResult(text="done", finish_reason="stop")


def _loop(tmp_path) -> AgentLoop:
    return AgentLoop(
        _Text(),
        SessionService(root=tmp_path / "sessions"),
        ToolRegistry(),
        settings=AIConfig(stop_policy="natural"),
        workspace=str(tmp_path),
    )


@pytest.mark.asyncio
async def test_the_loop_hands_its_session_rules_to_the_proof_shell(tmp_path):
    """The callback must be wired, not just implemented: session deny wins."""
    marker = tmp_path / "ran.marker"
    loop = _loop(tmp_path)
    session = SessionData(
        session_id="s",
        title="",
        created_time="t",
        last_access_time="t",
        permissions=[{"action": "bash", "resource": "*", "effect": "deny"}],
        acceptance=[_item(f"touch {marker}")],
    )

    await loop.process_input(session, "go")

    assert session.acceptance[0].passed is False
    assert "denied by rule" in session.acceptance[0].output
    assert not marker.exists()
    await loop.aclose()


# ---- R21: the child inherits the parent's session layer ---------------------


class _Tool(BaseTool):
    def __init__(self, name: str) -> None:
        self._name = name
        self.calls: list[dict] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "fake"

    @property
    def params_json_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, params, ctx=None):  # type: ignore[override]
        self.calls.append(dict(params))
        return ToolResult(success=True, output="ok")


class _Engine(ChatEngine):
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self._steps = [
            StepResult(
                tool_calls=[ToolCallRef(id="c1", name="reader", arguments="{}")],
                finish_reason="tool_calls",
            ),
            StepResult(text="done", finish_reason="stop"),
        ]

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.requests.append({"messages": list(messages), "tools": list(tools)})
        return self._steps.pop(0) if self._steps else StepResult(text="done")

    async def aclose(self) -> None:
        return None


def _runner(engine: _Engine, tool: _Tool) -> SubAgentRunner:
    registry = ToolRegistry()
    registry.register(tool)
    return SubAgentRunner(
        object(),
        engine_factory=lambda: engine,
        build_tools=lambda workdir: registry,
        settings=AIConfig(stop_policy="natural"),
        timeout_seconds=10,
    )


@pytest.mark.asyncio
async def test_a_parent_deny_is_in_force_in_the_child():
    reader = _Tool("reader")
    parent = ParentContext(
        permissions=(Rule("reader", "*", Effect.DENY),),
    )

    engine = _Engine()
    result = await _runner(engine, reader).run("t", parent=parent)

    assert reader.calls == []
    assert engine.requests[0]["tools"] == []
    assert result.status == "completed"


@pytest.mark.asyncio
async def test_without_the_rule_the_child_would_have_run_it():
    """The control: the harness auto-approves, so only the inherited deny can
    stop the call -- a missing wire would show up as an execution."""
    reader = _Tool("reader")
    parent = ParentContext()

    result = await _runner(_Engine(), reader).run("t", parent=parent)

    assert reader.calls == [{}]
    assert result.status == "completed"


class _FakeRunner:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def run(self, task, name, *, parent=None, **kwargs):
        self.calls.append({"task": task, "name": name, "parent": parent})
        from ai_coding.domain.subagent import SubagentResult

        return SubagentResult(status="completed", conclusion="done")


@pytest.mark.asyncio
async def test_the_subagent_tool_carries_permissions_even_without_a_transcript():
    permissions = (Rule("reader", "*", Effect.DENY),)
    runner = _FakeRunner()
    tool = SubAgentTool(runner=runner)

    await tool.execute(
        {"task": "t"},
        ToolContext(workspace="/tmp/ws", session_id="sid", permissions=permissions),
    )

    parent = runner.calls[0]["parent"]
    assert parent is not None
    assert parent.permissions == permissions
