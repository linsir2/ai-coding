"""A7: where per-workspace state lives.

The world handed the session service no root, so it fell back to its default —
``Path("sessions")``, resolved against whatever directory the process happened
to start in.  Traces already resolved against the workspace
(``cli.py::_make_recorder``); sessions were the exception, and they are the
half that holds the user's conversations.

These tests build a real world (a minimal config, no network) rather than
asserting a path expression: the wiring is the thing that was wrong, and an
expression would have passed before the fix too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_coding.server.world import build_world

CONFIG = """models:
  fake:
    name: fake
    base_url: http://127.0.0.1:9/v1
    api_key: k
    streaming: true
    max_tokens: 128
default_model: fake
ai:
  max_iterations: 2
"""


async def _world(tmp_path: Path):
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG, encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    world = await build_world(str(config), str(workspace), None)
    return world, workspace


@pytest.mark.asyncio
async def test_the_session_root_is_inside_the_workspace(tmp_path):
    world, workspace = await _world(tmp_path)
    try:
        assert Path(world.sessions.root) == (workspace / ".ai-coding" / "sessions").resolve()
    finally:
        await world.aclose()


@pytest.mark.asyncio
async def test_a_session_lands_under_the_workspace_not_the_cwd(tmp_path, monkeypatch):
    """The consequence, not the expression: start somewhere else entirely and
    the conversation still belongs to the workspace."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    world, workspace = await _world(tmp_path)
    try:
        session = world.sessions.create("hello")
        world.sessions.save(session)
        written = list((workspace / ".ai-coding" / "sessions").glob("*.json"))
        assert [p.name for p in written] == [f"{session.session_id}.json"]
        assert list(elsewhere.glob("sessions/*.json")) == []
    finally:
        await world.aclose()
