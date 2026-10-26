"""TDD (G4b-1): "always" travels from the human to the policy, in the core.

These cases used to live in ``test_textual_app.py`` and poked ``app._grants``.
That was the defect stated as a test: the grant lived in one front end, so the
REPL could never make one, the trace never saw one, and the policy layer had no
idea any existed.  The behaviour has not changed -- the layer that owns it has,
so the tests moved with it.

Only the model is absent here; the gate, the dispatcher and the callback are the
real ones.
"""

from __future__ import annotations

import pytest

from ai_coding.core.dispatch import ApprovalVerdict, ToolDispatcher
from ai_coding.domain.message import ToolCallRef
from ai_coding.security.approval import ApprovalResult
from ai_coding.security.permission_gate import PermissionDecision, PermissionGate
from ai_coding.tools.base import BaseTool, ToolContext
from ai_coding.tools.registry import ToolRegistry


class _Recorder:
    """An approval callback that answers with a scripted result and counts asks."""

    def __init__(self, answer: ApprovalResult) -> None:
        self.answer = answer
        self.asked: list[tuple[str, dict]] = []

    async def approve(self, tool_name, params, reason) -> ApprovalResult:
        self.asked.append((tool_name, dict(params)))
        return self.answer


class _Edit(BaseTool):
    """Enough of a write-ish tool to exercise the WARN path."""

    parallel_safe = False

    @property
    def name(self) -> str:
        return "edit"

    @property
    def description(self) -> str:
        return "edit a file"

    @property
    def params_json_schema(self) -> dict:
        return {"type": "object", "properties": {"path": {"type": "string"}}}

    async def execute(self, params, ctx: ToolContext | None = None):
        from ai_coding.domain.message import ToolResult

        return ToolResult(success=True, output=f"edited {params.get('path')}")


def _dispatcher(gate: PermissionGate, approval) -> ToolDispatcher:
    registry = ToolRegistry()
    registry.register(_Edit())
    return ToolDispatcher(registry, gate, approval)


def _ref(call_id: str, name: str, **params) -> ToolCallRef:
    import json

    return ToolCallRef(id=call_id, name=name, arguments=json.dumps(params))


async def _decide(dispatcher, ref: ToolCallRef):
    """Drive one call through the real public entry point.

    ``dispatch_batch`` is what ``AgentLoop._run_tools`` calls, so these exercise
    the path production uses rather than a private shortcut.
    """
    outcomes = await dispatcher.dispatch_batch([ref], ToolContext())
    outcome = outcomes[0]
    return outcome.result, outcome.denied, outcome.halted


@pytest.mark.asyncio
async def test_answering_always_records_the_grant_in_the_gate():
    gate = PermissionGate()
    approval = _Recorder(ApprovalResult.ALWAYS)
    dispatcher = _dispatcher(gate, approval)

    _, denied, _ = await _decide(dispatcher, _ref("c1", "edit", path="src/a.py"))

    assert not denied
    assert approval.asked == [("edit", {"path": "src/a.py"})]
    assert gate.grants == ("edit:src/a.py@build",)
    assert gate.check("edit", {"path": "src/a.py"}, posture="build") == (
        PermissionDecision.ALLOW
    )


@pytest.mark.asyncio
async def test_answering_yes_records_nothing():
    """The difference between "this once" and "remember it" is the whole
    feature; collapsing them would make every y a standing permission."""
    gate = PermissionGate()
    dispatcher = _dispatcher(gate, _Recorder(ApprovalResult.YES))

    await _decide(dispatcher, _ref("c1", "edit", path="src/a.py"))

    assert gate.grants == ()


@pytest.mark.asyncio
async def test_a_granted_call_never_reaches_the_callback():
    """What used to be the TUI's private short-circuit: the gate answers ALLOW,
    so the dispatcher never asks and no modal appears."""
    gate = PermissionGate(grants={"edit:src/a.py"})
    approval = _Recorder(ApprovalResult.NO)
    dispatcher = _dispatcher(gate, approval)

    _, denied, _ = await _decide(dispatcher, _ref("c1", "edit", path="src/a.py"))

    assert not denied
    assert approval.asked == []
    assert gate.check("edit", {"path": "src/a.py"}) == PermissionDecision.ALLOW


@pytest.mark.asyncio
async def test_a_grant_does_not_cover_a_different_file():
    gate = PermissionGate()
    approval = _Recorder(ApprovalResult.ALWAYS)
    dispatcher = _dispatcher(gate, approval)
    await _decide(dispatcher, _ref("c1", "edit", path="src/a.py"))

    approval.answer = ApprovalResult.NO
    _, denied, _ = await _decide(dispatcher, _ref("c2", "edit", path="src/b.py"))

    assert denied, "a grant on one file must not cover another"
    assert [call for call, _ in approval.asked] == ["edit", "edit"]


@pytest.mark.asyncio
async def test_an_always_on_bash_approves_once_and_remembers_nothing():
    """bash is excluded by policy: the answer still allows the call (the human
    said so) but leaves no standing permission behind."""
    gate = PermissionGate()
    registry = ToolRegistry()

    class _Bash(BaseTool):
        parallel_safe = False

        @property
        def name(self) -> str:
            return "bash"

        @property
        def description(self) -> str:
            return "run"

        @property
        def params_json_schema(self) -> dict:
            return {"type": "object", "properties": {"command": {"type": "string"}}}

        async def execute(self, params, ctx: ToolContext | None = None):
            from ai_coding.domain.message import ToolResult

            return ToolResult(success=True, output="ran")

    registry.register(_Bash())
    approval = _Recorder(ApprovalResult.ALWAYS)
    dispatcher = ToolDispatcher(registry, gate, approval)

    _, denied, _ = await _decide(dispatcher, _ref("c1", "bash", command="ls"))

    assert not denied
    assert gate.grants == ()
    assert gate.check("bash", {"command": "ls"}) == PermissionDecision.WARN


def test_the_verdict_vocabulary_is_unchanged_for_callers():
    """``ALWAYS`` maps to ``APPROVED``: a caller that only needs "may this run"
    must not be forced to handle a state it cannot act on differently."""
    assert set(ApprovalVerdict) == {
        ApprovalVerdict.APPROVED,
        ApprovalVerdict.DECLINED,
        ApprovalVerdict.STOPPED,
        ApprovalVerdict.TIMED_OUT,
    }


@pytest.mark.asyncio
async def test_revoking_a_grant_puts_the_question_back():
    """T7: "always" is a permission that can be taken back.  A revoke must
    return the call to the approval path, not leave the old answer standing."""
    gate = PermissionGate()
    approval = _Recorder(ApprovalResult.ALWAYS)
    dispatcher = _dispatcher(gate, approval)

    _, denied, _ = await _decide(dispatcher, _ref("c1", "edit", path="src/a.py"))
    assert not denied
    assert gate.grants == ("edit:src/a.py@build",)

    assert gate.revoke("edit:src/a.py@build")
    approval.answer = ApprovalResult.NO
    _, denied, _ = await _decide(dispatcher, _ref("c2", "edit", path="src/a.py"))

    assert denied
    assert [call for call, _ in approval.asked] == ["edit", "edit"]


@pytest.mark.asyncio
async def test_the_gate_and_the_prompt_see_the_real_call_not_the_vessel():
    """M2: one dispatcher (mcp_call) carries every remote call, but the gate
    must judge fs__write_file — otherwise the prompt reads "mcp_call" and the
    operator has no idea what they are approving.  ``permission_name`` is the
    seam: the registry name and the authorized name are deliberately different
    things, exactly like OpenCode asserting the leaf inside execute."""

    class _Remote(BaseTool):
        parallel_safe = False

        @property
        def name(self) -> str:
            return "mcp_call"

        @property
        def description(self) -> str:
            return "call a remote MCP tool"

        @property
        def params_json_schema(self) -> dict:
            return {"type": "object", "properties": {"server": {"type": "string"}}}

        def permission_name(self, params: dict) -> str:
            return f"{params.get('server')}__{params.get('tool')}"

        async def execute(self, params, ctx: ToolContext | None = None):
            from ai_coding.domain.message import ToolResult

            return ToolResult(success=True, output="remote ok")

    registry = ToolRegistry()
    registry.register(_Remote())
    gate = PermissionGate()
    approval = _Recorder(ApprovalResult.YES)

    dispatcher = ToolDispatcher(registry, gate, approval)
    await _decide(dispatcher, _ref("c1", "mcp_call", server="fs", tool="write_file"))

    assert approval.asked == [("fs__write_file", {"server": "fs", "tool": "write_file"})]
    assert gate.grants == ()
