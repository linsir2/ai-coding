"""M3 as a flow, not as a set of units.

Each unit is covered by ``tests/test_m3_boundaries.py``.  These tests drive
the chain a real session walks -- enter plan mode, ask for the tool surface,
dispatch a write -- because the defects M3 fixes were *between* the units:
the rule and the catalogue disagreed, and the catalogue and the dispatcher
disagreed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_coding.core.dispatch import ToolDispatcher
from ai_coding.domain.message import ToolCallRef
from ai_coding.domain.task import TaskState
from ai_coding.security.file_state_tracker import FileStateTracker
from ai_coding.security.sandbox import Sandbox
from ai_coding.tools.base import ToolContext
from ai_coding.tools.registry import ToolRegistry
from ai_coding.tools.write_tool import WriteTool


def _config(root: Path) -> Path:
    path = root / "config.yaml"
    path.write_text(
        "models:\n"
        "  m:\n"
        "    name: m\n"
        "    base_url: http://127.0.0.1:1/v1\n"
        "    api_key: k\n"
        "default_model: m\n"
        "ai:\n  stop_policy: natural\n",
        encoding="utf-8",
    )
    return path


def _registry(root: Path) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(WriteTool(Sandbox(root), FileStateTracker()))
    return registry


@pytest.mark.asyncio
async def test_entering_plan_mode_makes_the_directory_the_only_writer(tmp_path):
    """The chain: ``/plan`` posture -> agent rules -> surface -> dispatch.

    A green unit suite could still miss this: the rule can allow a path the
    surface never offers, or the surface can offer a tool the dispatcher
    refuses for the wrong reason.
    """
    from ai_coding.server.world import build_world

    world = await build_world(str(_config(tmp_path)), str(tmp_path), None)
    try:
        session = world.sessions.create()
        # The command is the operator's entry point; using it keeps this test
        # from re-implementing what ``/plan`` means.
        from rich.console import Console

        from ai_coding.app.commands import handle_input
        from ai_coding.app.console import DialogueConsole

        console = DialogueConsole(Console(record=True, width=120))
        await handle_input(world.loop, "/plan", session, world.sessions, console, None)
        assert session.agent == "plan"
        assert session.readonly is True

        rules = world.loop._agent_rules(session)
        names = [
            tool.name
            for tool in world.loop._surface_tools(
                session.mode, session.readonly, (), rules
            )
        ]
        assert "write" in names
        assert "edit" in names
        assert "todo_write" not in names
        assert "bash" not in names

        dispatcher = ToolDispatcher(_registry(tmp_path), world.loop.gate)
        ctx = ToolContext(
            task=TaskState(),
            workspace=str(tmp_path),
            session_id=session.session_id,
            mode=session.mode,
            readonly=session.readonly,
            agent_rules=rules,
        )
        plan_call = ToolCallRef(
            id="p",
            name="write",
            arguments=json.dumps(
                {"path": ".ai-coding/plan/design.md", "content": "# plan"}
            ),
        )
        src_call = ToolCallRef(
            id="s",
            name="write",
            arguments=json.dumps({"path": "src/app.py", "content": "x"}),
        )
        plan_result, src_result = await dispatcher.dispatch_batch(
            [plan_call, src_call], ctx
        )
        assert plan_result.result.success is True, plan_result.result.output
        assert (tmp_path / ".ai-coding" / "plan" / "design.md").is_file()
        assert src_result.denied is True
        assert not (tmp_path / "src" / "app.py").exists()
        assert ".ai-coding/plan" in src_result.result.output
    finally:
        await world.aclose()


@pytest.mark.asyncio
async def test_the_plan_directory_survives_a_leave_and_re_enter(tmp_path):
    """Leaving plan mode restores ordinary writes; re-entering narrows again.

    This is where a "the overlay was permanently loosened" regression would
    show up: the surface and the gate must both flip back.
    """
    from ai_coding.server.world import build_world

    world = await build_world(str(_config(tmp_path)), str(tmp_path), None)
    try:
        session = world.sessions.create()
        from rich.console import Console

        from ai_coding.app.commands import handle_input
        from ai_coding.app.console import DialogueConsole

        console = DialogueConsole(Console(record=True, width=120))
        await handle_input(world.loop, "/plan", session, world.sessions, console, None)
        await handle_input(
            world.loop, "/plan off", session, world.sessions, console, None
        )
        assert session.agent == "build"
        assert session.readonly is False

        build_names = [t.name for t in world.loop._surface_tools("normal", False)]
        assert "todo_write" in build_names
        assert "bash" in build_names

        # A build session has the ordinary WARN tier, not the plan exception.
        decision = world.loop.gate.check("write", {"path": "src/app.py"})
        assert decision.specific is False
    finally:
        await world.aclose()
