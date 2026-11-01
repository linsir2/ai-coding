"""C-5b-1: addressable sub-agent handles, child sessions, and sidecars.

Only the *foundation* is exercised here: a background spawn opens a handle
(session + loop) and persists it under ``.ai-coding/agents``; the synchronous
``run()`` path stays ephemeral; addressing (``resolve_run_id``) is handles-first
so a handle survives the pruning of its projection row.  No new tools exist yet.
"""

import asyncio
import json

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.subagent import SubAgentRunner
from ai_coding.domain.loop import StepResult
from ai_coding.domain.subagent import BackgroundTask, ParentContext
from ai_coding.security.permissions import Effect, Rule, rule_to_dict
from ai_coding.service.session_service import SessionService
from ai_coding.tools.registry import ToolRegistry


class _Engine(ChatEngine):
    def __init__(self, text: str = "did it") -> None:
        self._text = text

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        return StepResult(text=self._text, finish_reason="stop")

    async def aclose(self) -> None:
        return None


def _runner(tmp_path, **over) -> SubAgentRunner:
    kwargs = {
        "app_config": object(),
        "engine_factory": lambda: _Engine(),
        "build_tools": lambda _workdir: ToolRegistry(),
        "ledger_root": tmp_path,
        "workspace": str(tmp_path),
        "timeout_seconds": 30.0,
        "settings": AIConfig(stop_policy="natural"),
    }
    kwargs.update(over)
    return SubAgentRunner(**kwargs)


async def _settle(runner: SubAgentRunner, run_id: str) -> BackgroundTask:
    for _ in range(200):
        for row in runner.task_list():
            if row.run_id == run_id and row.status != "running":
                return row
        await asyncio.sleep(0.02)
    raise AssertionError(f"run {run_id} never settled")


def _session_path(tmp_path, session_id: str):
    return tmp_path / ".ai-coding" / "agents" / "sessions" / f"{session_id}.json"


def _meta_path(tmp_path, run_id: str):
    return tmp_path / ".ai-coding" / "agents" / "meta" / f"{run_id}.json"


@pytest.mark.asyncio
async def test_spawn_registers_a_handle_and_persists_session_and_sidecar(tmp_path):
    runner = _runner(tmp_path)
    run_id = runner.spawn_background(
        "inspect the tree", name="probe", emit=lambda _event: None
    )

    assert run_id in runner._handles
    handle = runner._handles[run_id]
    assert handle.session.session_id.startswith("sub-")
    assert _session_path(tmp_path, handle.session.session_id).is_file()
    assert _meta_path(tmp_path, run_id).is_file()

    row = await _settle(runner, run_id)
    assert row.status == "completed"
    assert row.session_id == handle.session.session_id
    assert row.to_dict()["sessionId"] == handle.session.session_id


@pytest.mark.asyncio
async def test_turn_end_saves_the_child_conversation_and_refreshes_the_sidecar(tmp_path):
    rule = Rule(action="edit", resource="*.env", effect=Effect.DENY)
    parent = ParentContext(permissions=(rule,))
    runner = _runner(tmp_path)
    run_id = runner.spawn_background(
        "count the files", name="probe", parent=parent, emit=lambda _event: None
    )
    handle = runner._handles[run_id]

    await _settle(runner, run_id)

    saved = json.loads(
        _session_path(tmp_path, handle.session.session_id).read_text(encoding="utf-8")
    )
    user_texts = [
        str(row.get("content") or "")
        for row in saved["messages"]
        if row.get("role") == "user"
    ]
    assert any("count the files" in text for text in user_texts)

    meta = json.loads(_meta_path(tmp_path, run_id).read_text(encoding="utf-8"))
    assert meta["sessionId"] == handle.session.session_id
    assert meta["status"] == "idle"
    assert meta["lastStatus"] == "completed"
    assert meta["permissions"] == [rule_to_dict(rule)]
    assert meta["version"] == 1


@pytest.mark.asyncio
async def test_synchronous_run_leaves_no_handle_and_no_files(tmp_path):
    runner = _runner(tmp_path)
    result = await runner.run("one shot", name="ephemeral")

    assert result.status == "completed"
    assert runner._handles == {}
    # 5.3: the synchronous path is an ephemeral handle -- nothing under agents/.
    assert not (tmp_path / ".ai-coding" / "agents").exists()


@pytest.mark.asyncio
async def test_resolve_keeps_working_after_the_row_is_pruned(tmp_path):
    runner = _runner(tmp_path)
    run_id = runner.spawn_background("do it", emit=lambda _event: None)
    await _settle(runner, run_id)

    # The table row is a projection; the handle is the source of truth.
    runner._background.pop(run_id)

    assert runner.resolve_run_id(run_id) == run_id
    assert runner.resolve_run_id(run_id[:8]) == run_id
    assert runner.cancel(run_id) is False


@pytest.mark.asyncio
async def test_prune_never_drops_a_row_that_still_has_a_handle(tmp_path, monkeypatch):
    import ai_coding.core.subagent as subagent_mod

    monkeypatch.setattr(subagent_mod, "_MAX_BACKGROUND_ROWS", 1)
    runner = _runner(tmp_path)

    first = runner.spawn_background("one", name="first", emit=lambda _e: None)
    await _settle(runner, first)
    second = runner.spawn_background("two", name="second", emit=lambda _e: None)
    await _settle(runner, second)

    assert {row.run_id for row in runner.task_list()} == {first, second}


@pytest.mark.asyncio
async def test_a_settled_run_is_waitable_and_no_longer_cancellable(tmp_path):
    runner = _runner(tmp_path)
    run_id = runner.spawn_background("do it", emit=lambda _event: None)
    await _settle(runner, run_id)

    status, _elapsed = await runner.wait(run_id, timeout_ms=1_000)
    assert status == "completed"
    assert runner.cancel(run_id) is False


def test_an_injected_session_store_is_used_as_is(tmp_path):
    store = SessionService(tmp_path / "custom" / "sessions")
    runner = _runner(tmp_path, session_store=store)

    assert runner._session_store is store
    # Sidecars are siblings of the session root, never in it (a ``*.json``
    # there would be read as a phantom session).
    assert runner._meta_root == tmp_path / "custom" / "meta"
