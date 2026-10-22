"""M1a slice 3: the session layer.

A rule an operator adds with ``/permissions deny`` belongs to one session:
it must survive a reload, ride the next request, and never leak into the
process-wide gate.  The gate is shared by every session, so putting the
rules *in* the gate would make one session's decision another session's --
that is why the layer travels in ``ToolContext`` instead.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from rich.console import Console

from ai_coding.app.commands import REPLAction, handle_input
from ai_coding.app.console import DialogueConsole
from ai_coding.core.dispatch import ToolDispatcher
from ai_coding.domain.message import ToolCallRef
from ai_coding.domain.session import SessionData
from ai_coding.security.approval import ApprovalResult
from ai_coding.security.permission_gate import PermissionDecision, PermissionGate
from ai_coding.security.permissions import (
    Effect,
    FileRules,
    Rule,
    rule_to_dict,
    rules_from_dicts,
)
from ai_coding.tools.base import BaseTool, ToolContext
from ai_coding.tools.registry import ToolRegistry


def _session() -> SessionData:
    return SessionData(session_id="s1", title="", created_time="", last_access_time="")


def _deny(action: str, resource: str = "*", **kwargs: Any) -> Rule:
    return Rule(action=action, resource=resource, effect=Effect.DENY, **kwargs)


class _Sessions:
    def __init__(self) -> None:
        self.saved: list[SessionData] = []

    def save(self, session: SessionData) -> None:
        self.saved.append(session)


class _Loop:
    def __init__(self, gate: PermissionGate) -> None:
        self.dispatcher = SimpleNamespace(gate=gate, approval=None)


def _capture() -> tuple[DialogueConsole, Console]:
    console = Console(record=True, width=80)
    return DialogueConsole(console), console


async def _run(loop: _Loop, command: str, session: SessionData, sessions: _Sessions):
    console, recorded = _capture()
    action, next_session = await handle_input(loop, command, session, sessions, console)
    assert action is REPLAction.CONTINUE
    assert next_session is session
    return recorded.export_text()


# ---- persistence -------------------------------------------------------------


def test_session_rules_are_written_and_read_back():
    session = _session()
    session.permissions = [
        {"action": "edit", "resource": "*.env", "effect": "deny", "reason": "secrets"},
    ]

    data = session.to_dict()
    restored = SessionData.from_dict(data)

    assert data["permissions"] == session.permissions
    assert restored.permissions == session.permissions


def test_an_empty_session_layer_writes_no_key():
    assert "permissions" not in _session().to_dict()


def test_a_broken_rule_is_skipped_with_a_reason_not_a_crash():
    rules, errors = rules_from_dicts(
        [
            {"action": "edit", "resource": "*.env", "effect": "deny"},
            {"action": "read"},
            {"action": "write", "resource": "*", "effect": "sometimes"},
        ]
    )

    assert rules == (_deny("edit", "*.env"),)
    assert len(errors) == 2
    assert "rule 2" in errors[0]
    assert "rule 3" in errors[1]


def test_a_rule_roundtrips_through_its_session_form():
    rule = _deny("edit", "*.env", reason="secrets", postures=("build",))

    assert rules_from_dicts([rule_to_dict(rule)]) == ((rule,), ())


# ---- commands ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_permissions_deny_writes_the_session_layer_and_it_decides():
    gate = PermissionGate()
    session = _session()
    sessions = _Sessions()

    await _run(_Loop(gate), "/permissions deny edit *.env", session, sessions)

    rules, errors = rules_from_dicts(session.permissions)
    assert errors == ()
    assert gate.check("edit", {"path": ".env"}, session_rules=rules).decision is (
        PermissionDecision.DENY
    )
    assert sessions.saved == [session]
    assert gate.check("edit", {"path": ".env"}).decision is PermissionDecision.WARN


@pytest.mark.asyncio
async def test_permissions_allow_writes_an_allow_rule():
    session = _session()
    sessions = _Sessions()

    await _run(_Loop(PermissionGate()), "/permissions allow write src/*", session, sessions)

    rules, errors = rules_from_dicts(session.permissions)
    assert errors == ()
    assert PermissionGate().check(
        "write", {"path": "src/a.py", "content": "x"}, session_rules=rules
    ).decision is PermissionDecision.ALLOW


@pytest.mark.asyncio
async def test_permissions_reset_only_clears_the_session_layer():
    gate = PermissionGate(grants=["edit:src/a.py"])
    session = _session()
    session.permissions = [rule_to_dict(_deny("edit"))]
    sessions = _Sessions()

    await _run(_Loop(gate), "/permissions reset", session, sessions)

    assert session.permissions == []
    assert gate.grants == ("edit:src/a.py",)


@pytest.mark.asyncio
async def test_permissions_reads_the_layers_and_the_errors():
    rule = _deny("edit", "*.env")
    gate = PermissionGate(
        rules=FileRules(
            user=(rule,),
            project=(rule,),
            policy=(_deny("bash"),),
            errors=("user file: broken yaml",),
        )
    )
    session = _session()
    session.permissions = [rule_to_dict(_deny("read"))]

    text = await _run(_Loop(gate), "/permissions", session, _Sessions())

    for layer in ("base", "standing", "user", "project", "session", "policy"):
        assert layer in text
    assert "user file: broken yaml" in text
    assert "[permissions] session rules:" in text
    assert "deny  read *" in text


# ---- the request carries the layer ------------------------------------------


class _Edit(BaseTool):
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


class _Read(BaseTool):
    parallel_safe = True

    @property
    def name(self) -> str:
        return "read"

    @property
    def description(self) -> str:
        return "read a file"

    @property
    def params_json_schema(self) -> dict:
        return {"type": "object", "properties": {"path": {"type": "string"}}}

    async def execute(self, params, ctx: ToolContext | None = None):
        from ai_coding.domain.message import ToolResult

        return ToolResult(success=True, output="read")


class _Always:
    def __init__(self) -> None:
        self.asked: list[str] = []

    async def approve(self, tool_name, params, reason):
        self.asked.append(tool_name)
        return ApprovalResult.ALWAYS


def _dispatcher(*tools: BaseTool) -> ToolDispatcher:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return ToolDispatcher(registry, PermissionGate(), _Always())


def _ref(call_id: str, name: str, **params: Any) -> ToolCallRef:
    return ToolCallRef(id=call_id, name=name, arguments=json.dumps(params))


@pytest.mark.asyncio
async def test_dispatch_checks_the_context_s_own_rules():
    dispatcher = _dispatcher(_Edit())
    ctx = ToolContext(permissions=(_deny("edit", "*.env"),))

    outcomes = await dispatcher.dispatch_batch([_ref("c1", "edit", path=".env")], ctx)

    assert outcomes[0].denied
    assert dispatcher.approval.asked == []


@pytest.mark.asyncio
async def test_dispatch_uses_the_session_posture():
    rule = _deny("read", "*", postures=("build",))
    dispatcher = _dispatcher(_Read())

    allowed = await dispatcher.dispatch_batch(
        [_ref("c1", "read", path="a.py")],
        ToolContext(permissions=(rule,), mode="plan"),
    )

    denied = await dispatcher.dispatch_batch(
        [_ref("c2", "read", path="a.py")],
        ToolContext(permissions=(rule,), mode="normal"),
    )

    assert not allowed[0].denied
    assert denied[0].denied
