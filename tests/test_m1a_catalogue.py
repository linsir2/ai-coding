"""M1a slice 4: a wholly denied action leaves the catalogue.

The gate already refuses a call.  This slice is about not offering it: a
tool the rules deny *entirely* (``edit * deny``) must disappear from the
request's schema list and from the prompt's method section -- otherwise the
model spends a call discovering a refusal the request could have told it.

A scoped denial (``edit *.env deny``) is different on purpose: the tool
stays, and the refusal happens at dispatch where the model can read which
path it hit.
"""

from __future__ import annotations

from typing import Any

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.domain.session import SessionData
from ai_coding.security.permission_gate import PermissionGate
from ai_coding.security.permissions import Effect, Rule
from ai_coding.service.session_service import SessionService
from ai_coding.tools.base import BaseTool, ToolContext
from ai_coding.tools.registry import ToolRegistry


def _deny(action: str, resource: str = "*", **kwargs: Any) -> Rule:
    return Rule(action=action, resource=resource, effect=Effect.DENY, **kwargs)


class _Engine(ChatEngine):
    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        raise AssertionError("the catalogue tests never run a turn")


class _Tool(BaseTool):
    def __init__(self, name: str, *, mutates: bool = False) -> None:
        self._name = name
        self.mutates_world = mutates

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._name

    @property
    def params_json_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, params, ctx: ToolContext | None = None):
        from ai_coding.domain.message import ToolResult

        return ToolResult(success=True, output=self._name)


def _loop(tmp_path, tools: list[BaseTool], gate: PermissionGate | None = None) -> AgentLoop:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return AgentLoop(
        _Engine(),
        SessionService(root=tmp_path / "sessions"),
        registry,
        gate=gate or PermissionGate(),
        settings=AIConfig(stop_policy="natural"),
        workspace=str(tmp_path),
    )


def _session(rules: list[dict[str, Any]] | None = None) -> SessionData:
    session = SessionData(
        session_id="s", title="", created_time="t", last_access_time="t"
    )
    session.permissions = list(rules or ())
    return session


def _rule(action: str, resource: str = "*") -> dict[str, Any]:
    return {"action": action, "resource": resource, "effect": "deny"}


# ---- the predicate -----------------------------------------------------------


def test_a_whole_action_denial_hides_the_tool():
    gate = PermissionGate()

    assert gate.hides("edit", session_rules=(_deny("edit"),))
    assert not gate.hides("edit", session_rules=(_deny("edit", "*.env"),))


def test_a_later_allow_reopens_the_catalogue():
    gate = PermissionGate()

    assert not gate.hides(
        "edit",
        session_rules=(_deny("edit"), Rule("edit", "*", Effect.ALLOW)),
    )


def test_hiding_follows_the_posture():
    gate = PermissionGate()
    rules = (_deny("edit", postures=("build",)),)

    assert gate.hides("edit", session_rules=rules, posture="build")
    assert not gate.hides("edit", session_rules=rules, posture="plan")


# ---- the surface -------------------------------------------------------------


def test_the_surface_drops_a_wholly_denied_tool(tmp_path):
    loop = _loop(tmp_path, [_Tool("read"), _Tool("edit", mutates=True)])

    names = [tool.name for tool in loop._surface_tools("normal", False, (_deny("edit"),))]

    assert names == ["read"]


def test_a_scoped_denial_keeps_the_tool(tmp_path):
    loop = _loop(tmp_path, [_Tool("edit", mutates=True)])

    names = [tool.name for tool in loop._surface_tools("normal", False, (_deny("edit", "*.env"),))]

    assert names == ["edit"]


def test_the_catalogue_name_is_lowercased_like_the_gate(tmp_path):
    loop = _loop(tmp_path, [_Tool("subAgent")])

    names = [tool.name for tool in loop._surface_tools("normal", False, (_deny("subagent"),))]

    assert names == []


def test_a_denial_of_the_mcp_vessel_hides_every_remote_tool(tmp_path):
    loop = _loop(tmp_path, [_Tool("mcp_call")])

    names = [tool.name for tool in loop._surface_tools("normal", False, (_deny("mcp_call"),))]

    assert names == []


# ---- the request cache -------------------------------------------------------


def test_a_session_rule_change_rebuilds_the_schema_list(tmp_path):
    loop = _loop(tmp_path, [_Tool("read"), _Tool("edit", mutates=True)])
    session = _session()

    first = loop._surface_schemas(session)
    assert {item["function"]["name"] for item in first} == {"read", "edit"}

    session.permissions = [_rule("edit")]
    second = loop._surface_schemas(session)

    assert {item["function"]["name"] for item in second} == {"read"}
    assert second is not first


def test_the_same_rules_are_cached(tmp_path):
    loop = _loop(tmp_path, [_Tool("read")])
    session = _session([_rule("edit")])

    first = loop._surface_schemas(session)
    second = loop._surface_schemas(session)

    assert second is first
