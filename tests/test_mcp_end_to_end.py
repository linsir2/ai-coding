"""The whole MCP path, against a real server, in one turn.

Every other MCP test proves one link: the catalog is pure text, the gate is a
ladder, the dispatcher wraps tools.  None of them proves the *joints* — and the
joints are where a system like this rots quietly:

    scripted engine -> mcp_call -> real stdio subprocess -> ToolResult
                    -> write_back -> the next request's messages

``tests/test_mcp_stdio.py`` spawns a real subprocess, but through
``MCPToolAdapter`` — a class production stopped building in M2 (``core/mcp.py``
says so).  So the class the model actually calls had never been run against a
real server before this file.  Every assertion below is one a reader can check
against the chain in ``docs/data-contracts.md``.

Gated the same way as the other real-subprocess tests; CI runs it with
``AI_CODING_MCP_STDIO=1``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from _mcp_wait import until_mcp_settled

from ai_coding.config.models import AIConfig, MCPConfig, MCPServerConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.mcp import MCPManager, build_mcp_catalog
from ai_coding.domain.loop import StepResult
from ai_coding.domain.message import ToolCallRef
from ai_coding.security.approval import ApprovalCallback, ApprovalResult
from ai_coding.service.session_service import SessionService
from ai_coding.tools.mcp_call import MCPCallTool
from ai_coding.tools.registry import ToolRegistry

GATE = "AI_CODING_MCP_STDIO"
SERVER = Path(__file__).resolve().parent.parent / "scripts" / "mcp_echo_server.py"

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get(GATE) != "1",
        reason=f"real MCP stdio round trip needs a normal OS; set {GATE}=1",
    ),
]


class _ScriptedEngine:
    """Returns pre-scripted steps and records every request it received.

    Records ``tools`` as well as ``messages`` on purpose: "the model can see the
    tool" is a claim about the request that goes to the provider, and this is
    that request.
    """

    def __init__(self, steps):
        self._steps = list(steps)
        self.requests: list[dict] = []

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.requests.append({"messages": list(messages), "tools": list(tools)})
        if not self._steps:
            raise AssertionError("engine called more times than the script allows")
        item = self._steps.pop(0)
        if on_delta is not None and getattr(item, "text", None):
            on_delta(item.text)
        return item

    async def aclose(self) -> None:
        return None


class _RecordingApproval(ApprovalCallback):
    """Answers yes, and remembers what it was asked about and with what."""

    def __init__(self, decision: ApprovalResult = ApprovalResult.YES) -> None:
        self.asked: list[tuple[str, dict, str]] = []
        self._decision = decision

    async def approve(self, tool_name, params, reason):
        self.asked.append((tool_name, dict(params), reason))
        return self._decision


def _config() -> MCPConfig:
    """One real server, launched with the interpreter running these tests."""
    return MCPConfig(
        enabled=True,
        servers=[MCPServerConfig(name="fs", command=[sys.executable, str(SERVER)])],
    )


async def _manager() -> MCPManager:
    manager = MCPManager(_config())
    await manager.open_all()
    await until_mcp_settled(manager)
    return manager


def _echo_call() -> StepResult:
    return StepResult(
        text=None,
        tool_calls=[
            ToolCallRef(
                id="call-1",
                name="mcp_call",
                arguments=json.dumps(
                    {"server": "fs", "tool": "echo", "input": {"text": "ping"}}
                ),
            )
        ],
        finish_reason="tool_calls",
    )


def _loop(engine, sessions, manager, approval) -> AgentLoop:
    """A loop wired the way ``build_world`` wires it, minus the rest of the app."""
    registry = ToolRegistry()
    registry.register(MCPCallTool(manager))
    return AgentLoop(
        engine,
        sessions,
        registry,
        settings=AIConfig(stop_policy="natural"),
        approval=approval,
        catalog_builder=lambda: build_mcp_catalog(manager.available()),
    )


def _request_text(engine, index: int) -> str:
    return "\n".join(str(m.content) for m in engine.requests[index]["messages"])


async def test_a_model_turn_calls_a_real_mcp_tool_and_gets_the_answer_back(tmp_path):
    """One turn end to end: told, called, approved by real name, answered."""
    manager = await _manager()
    try:
        engine = _ScriptedEngine([_echo_call(), StepResult(text="done", finish_reason="stop")])
        approval = _RecordingApproval()
        sessions = SessionService(tmp_path / "sessions")
        session = sessions.create()

        await _loop(engine, sessions, manager, approval).process_input(session, "echo ping")

        # 1. The model was told the tool exists, in the tail of its request.
        first = _request_text(engine, 0)
        assert "<mcp-catalog>" in first
        assert "echo" in first

        # 2. It was offered exactly one tool — the vessel — and that tool's
        #    schema makes server/tool/input callable.
        names = [
            entry["function"]["name"]
            for entry in engine.requests[0]["tools"]
            if isinstance(entry, dict) and "function" in entry
        ]
        assert names == ["mcp_call"]

        # 3. Approval was asked for the *remote* name, not the vessel. This is
        #    the seam where a gate judging "mcp_call" would silently approve
        #    every server on the machine.
        assert approval.asked, "the call never reached the gate"
        asked_name, asked_params, _reason = approval.asked[0]
        assert asked_name == "fs__echo"
        assert asked_params == {"server": "fs", "tool": "echo", "input": {"text": "ping"}}

        # 4. The server's own words came back to the model as a tool result.
        second = _request_text(engine, 1)
        assert "<tool_output" in second
        assert "echo:ping" in second

        # 5. And they were persisted, not merely returned to the caller.
        assert any(
            m.role == "tool" and "echo:ping" in str(m.content) for m in session.messages
        ), "the tool result never reached the session"
    finally:
        await manager.aclose()


async def test_a_denied_call_comes_back_as_a_readable_refusal_with_no_fabricated_answer(tmp_path):
    """The other half of the seam: a refusal must reach the model as text, and
    must not be mistakable for the server's output."""
    manager = await _manager()
    try:
        engine = _ScriptedEngine([_echo_call(), StepResult(text="ok", finish_reason="stop")])
        sessions = SessionService(tmp_path / "sessions")
        loop = _loop(engine, sessions, manager, _RecordingApproval(ApprovalResult.NO))

        await loop.process_input(sessions.create(), "echo ping")

        second = _request_text(engine, 1)
        assert "<tool_output" in second
        assert "echo:ping" not in second, "a denied call must not invent the server's answer"
    finally:
        await manager.aclose()


@pytest.mark.parametrize(
    ("params", "named_back"),
    [
        ({"server": "fs", "tool": "nope", "input": {}}, "echo"),
        ({"server": "nope", "tool": "echo", "input": {}}, "fs"),
    ],
)
async def test_a_hallucinated_pair_is_answered_with_the_real_names(params, named_back):
    """The model guessing is normal, not an error.  What it must get back is
    the truth, so it can correct itself in the next step."""
    manager = await _manager()
    try:
        result = await MCPCallTool(manager).execute(dict(params))

        assert result.success is False
        assert named_back in str(result.output)
    finally:
        await manager.aclose()
