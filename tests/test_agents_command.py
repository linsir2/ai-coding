"""TDD (M1b T1): ``/agents`` is the table's front door.

Two answers and one refusal:

* bare ``/agents`` lists the **visible** primaries (a hidden agent is not
  offered) and names any file that failed to load;
* ``/agents <id>`` switches, and switching to plan also turns on the
  read-only overlay — ``/agents plan`` and ``/plan`` must mean the same thing;
* ``/agents <subagent>`` and an unknown id are refused with the real list.
"""

from __future__ import annotations

import pytest
from rich.console import Console

from ai_coding.agents.registry import builtin_catalog
from ai_coding.app.commands import REPLAction, handle_input
from ai_coding.app.console import DialogueConsole
from ai_coding.domain.session import SessionData
from ai_coding.domain.stance import PostureResult
from ai_coding.service.session_service import SessionService


class _Loop:
    """The posture half of the loop, plus the agent table it exposes."""

    def __init__(self, catalog=None, *, busy: bool = False) -> None:
        self.agents = catalog if catalog is not None else builtin_catalog()
        self.busy = busy

    @property
    def turn_running(self) -> bool:
        return self.busy

    def request_posture(
        self, session, *, agent=None, readonly=None, reason="", observer=None
    ):
        target = (
            agent if agent is not None else session.agent,
            bool(readonly) if readonly is not None else session.readonly,
        )
        if target == (session.agent, session.readonly):
            if session.pending_posture:
                session.pending_posture = None
                return PostureResult.CANCELLED
            return PostureResult.NOOP
        if self.busy:
            session.pending_posture = {
                "agent": target[0],
                "readonly": target[1],
                "reason": reason,
            }
            return PostureResult.QUEUED
        session.agent, session.readonly = target
        session.pending_posture = None
        return PostureResult.COMMITTED


def _session() -> SessionData:
    return SessionData(session_id="s", title="", created_time="t", last_access_time="t")


def _sessions(tmp_path):
    return SessionService(tmp_path / "sessions")


def _console():
    rich = Console(record=True, width=120)
    return rich, DialogueConsole(rich)


@pytest.mark.asyncio
async def test_the_list_names_the_visible_primaries_and_the_current_one(tmp_path):
    loop, session = _Loop(), _session()
    rich, console = _console()

    await handle_input(loop, "/agents", session, _sessions(tmp_path), console)

    text = rich.export_text()
    assert "build" in text and "plan" in text
    assert "current: build" in text
    # A subagent is a delegation target, not something a session speaks through.
    assert "explore" in text  # visible list shows subagents too
    assert "* build" in text


@pytest.mark.asyncio
async def test_a_hidden_agent_is_not_offered(tmp_path):
    catalog = builtin_catalog()
    from dataclasses import replace

    hidden = replace(catalog.specs["plan"], hidden=True)
    loop = _Loop(replace(catalog, specs={**catalog.specs, "plan": hidden}))
    rich, console = _console()
    await handle_input(loop, "/agents", _session(), _sessions(tmp_path), console)
    assert "plan" not in rich.export_text()


@pytest.mark.asyncio
async def test_switching_to_a_primary_sets_the_overlay(tmp_path):
    loop, session = _Loop(), _session()
    rich, console = _console()

    await handle_input(loop, "/agents plan", session, _sessions(tmp_path), console)

    assert (session.agent, session.readonly) == ("plan", True)
    assert "已切换到 plan" in rich.export_text()


@pytest.mark.asyncio
async def test_agents_plan_is_the_same_as_plan(tmp_path):
    by_agents, by_plan = _Loop(), _Loop()
    sessions = _sessions(tmp_path)
    rich_a, console_a = _console()
    rich_p, console_p = _console()
    first, second = _session(), _session()

    await handle_input(by_agents, "/agents plan", first, sessions, console_a)
    await handle_input(by_plan, "/plan", second, sessions, console_p)

    assert (first.agent, first.readonly) == (second.agent, second.readonly)


@pytest.mark.asyncio
async def test_a_subagent_is_refused_as_a_session_agent(tmp_path):
    loop, session = _Loop(), _session()
    rich, console = _console()

    action, _ = await handle_input(loop, "/agents explore", session, _sessions(tmp_path), console)

    assert action is REPLAction.CONTINUE
    assert session.agent == "build"
    text = rich.export_text()
    assert "subagent" in text
    assert "build" in text


@pytest.mark.asyncio
async def test_an_unknown_agent_is_refused_with_the_real_list(tmp_path):
    loop, session = _Loop(), _session()
    rich, console = _console()

    await handle_input(loop, "/agents nope", session, _sessions(tmp_path), console)

    text = rich.export_text()
    assert "nope" in text
    assert "build" in text and "plan" in text
    assert session.agent == "build"


@pytest.mark.asyncio
async def test_switching_mid_turn_queues_and_says_so(tmp_path):
    loop, session = _Loop(busy=True), _session()
    rich, console = _console()

    await handle_input(loop, "/agents plan", session, _sessions(tmp_path), console)

    assert session.agent == "build"
    assert session.pending_posture == {
        "agent": "plan",
        "readonly": True,
        "reason": "/agents plan",
    }
    assert "排队" in rich.export_text()
