"""TDD (R4b): a rewritten call must pass the permission gate again.

Guards and hooks may rewrite parameters, which hands them a way around the gate:
turn ``ls`` into ``rm -rf /`` and the hard-deny table is worthless unless the new
arguments are re-checked.  This is the single most security-relevant rule in R4b
(docs/r4-analysis.md 3.2, step A5).
"""


import pytest

from ai_coding.core.dispatch import ToolDispatcher
from ai_coding.core.guard import (
    GuardChain,
    GuardDecision,
    GuardVerdict,
    TurnView,
)
from ai_coding.domain.message import ToolCallRef, ToolResult
from ai_coding.security.permission_gate import Decision, PermissionDecision, PermissionGate
from ai_coding.tools.base import BaseTool, ToolContext
from ai_coding.tools.registry import ToolRegistry


class _CountingGate(PermissionGate):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, dict]] = []

    def check(self, tool_name, params, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append((tool_name, dict(params)))
        if "rm -rf /" in str(params.get("command", "")):
            return Decision(PermissionDecision.DENY, "bash command blocked")
        return Decision(PermissionDecision.ALLOW, "")


class _Bash(BaseTool):
    """Records what it was actually asked to run."""

    parallel_safe = True

    def __init__(self) -> None:
        self.commands: list[str] = []

    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        return "fake bash"

    @property
    def params_json_schema(self) -> dict:
        return {"type": "object", "properties": {"command": {"type": "string"}}}

    async def execute(self, params, ctx=None):  # type: ignore[override]
        self.commands.append(str(params.get("command", "")))
        return ToolResult(success=True, output="ok")


class _Rewriter:
    name = "rewriter"

    def __init__(self, command: str) -> None:
        self._command = command

    def check(self, call, ctx, view):  # type: ignore[no-untyped-def]
        return GuardDecision(
            GuardVerdict.REWRITE, params={"command": self._command}, guard=self.name
        )


def _dispatcher(gate, guards):
    tool = _Bash()
    registry = ToolRegistry()
    registry.register(tool)
    return ToolDispatcher(registry, gate, guards=guards), tool


@pytest.mark.asyncio
async def test_a_rewrite_into_a_denied_command_is_still_denied():
    gate = _CountingGate()
    dispatcher, tool = _dispatcher(gate, GuardChain([_Rewriter("rm -rf /")]))

    outcomes = await dispatcher.dispatch_batch(
        [ToolCallRef(id="c1", name="bash", arguments='{"command":"ls"}')],
        ToolContext(workspace="/tmp/ws"),
    )

    assert outcomes[0].result.success is False
    assert outcomes[0].denied is True
    assert tool.commands == [], "the dangerous command must never run"
    assert len(gate.calls) == 2, "the rewritten parameters must be re-checked"
    assert gate.calls[1][1]["command"] == "rm -rf /"


@pytest.mark.asyncio
async def test_a_benign_rewrite_runs_and_is_recorded():
    gate = _CountingGate()
    dispatcher, tool = _dispatcher(gate, GuardChain([_Rewriter("ls -la")]))

    outcomes = await dispatcher.dispatch_batch(
        [ToolCallRef(id="c1", name="bash", arguments='{"command":"ls"}')],
        ToolContext(workspace="/tmp/ws"),
    )

    result = outcomes[0].result
    assert tool.commands == ["ls -la"]
    assert result.data["rewritten_from"] == {"command": "ls"}
    assert result.data["rewritten_by"] == "guard:rewriter"
    assert "adjusted before execution" in result.output


@pytest.mark.asyncio
async def test_without_a_rewrite_the_gate_is_consulted_once():
    gate = _CountingGate()
    dispatcher, tool = _dispatcher(gate, GuardChain([]))

    await dispatcher.dispatch_batch(
        [ToolCallRef(id="c1", name="bash", arguments='{"command":"ls"}')],
        ToolContext(workspace="/tmp/ws"),
    )

    assert tool.commands == ["ls"]
    assert len(gate.calls) == 1


@pytest.mark.asyncio
async def test_a_denying_guard_never_reaches_the_gate_or_the_tool():
    class _Deny:
        name = "nope"

        def check(self, call, ctx, view):  # type: ignore[no-untyped-def]
            return GuardDecision(GuardVerdict.DENY, reason="not today", guard=self.name)

    gate = _CountingGate()
    dispatcher, tool = _dispatcher(gate, GuardChain([_Deny()]))

    outcomes = await dispatcher.dispatch_batch(
        [ToolCallRef(id="c1", name="bash", arguments='{"command":"ls"}')],
        ToolContext(workspace="/tmp/ws"),
    )

    assert outcomes[0].result.error == "guard_denied"
    assert outcomes[0].result.data["guard"] == "nope"
    assert tool.commands == []
    assert len(gate.calls) == 1  # the gate still ran first (it was ALLOW)


@pytest.mark.asyncio
async def test_guard_view_is_passed_through():
    seen: list[TurnView] = []

    class _ViewSpy:
        name = "spy"

        def check(self, call, ctx, view):  # type: ignore[no-untyped-def]
            seen.append(view)
            return GuardDecision(GuardVerdict.ALLOW)

    dispatcher, _ = _dispatcher(_CountingGate(), GuardChain([_ViewSpy()]))
    view = TurnView(iteration=3, tokens_spent=1234)

    await dispatcher.dispatch_batch(
        [ToolCallRef(id="c1", name="bash", arguments='{"command":"ls"}')],
        ToolContext(workspace="/tmp/ws"),
        view=view,
    )

    assert seen == [view]
