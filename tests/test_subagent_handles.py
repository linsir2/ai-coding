"""C-5b-1: addressable sub-agent handles, child sessions, and sidecars.

Only the *foundation* is exercised here: a background spawn opens a handle
(session + loop) and persists it under ``.ai-coding/agents``; the synchronous
``run()`` path stays ephemeral; addressing (``resolve_run_id``) is handles-first
so a handle survives the pruning of its projection row.  No new tools exist yet.
"""

import asyncio
import json
from pathlib import Path

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.subagent import SubAgentRunner
from ai_coding.core.worktree import WorktreeError
from ai_coding.domain.loop import StepResult
from ai_coding.domain.subagent import BackgroundTask, ParentContext, SubagentTask
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


@pytest.mark.asyncio
async def test_a_readonly_spawn_marks_the_child_and_its_handle_readonly(tmp_path):
    runner = _runner(tmp_path)
    run_id = runner.spawn_background(
        "scan the tree", name="probe", readonly=True, emit=lambda _event: None
    )

    handle = runner._handles[run_id]
    assert handle.readonly is True
    assert handle.session.readonly is True

    await _settle(runner, run_id)


@pytest.mark.asyncio
async def test_resume_keeps_a_readonly_child_readonly(tmp_path):
    runner = _runner(tmp_path)
    run_id = runner.spawn_background(
        "scan the tree", name="probe", readonly=True, emit=lambda _event: None
    )
    await _settle(runner, run_id)
    await runner.close(run_id)

    status, resumed = await runner.resume(run_id)

    assert status == "idle"
    assert resumed == run_id
    handle = runner._handles[run_id]
    assert handle.readonly is True
    assert handle.session.readonly is True


class _IsolatingWorktreeManager:
    """A manager that can isolate and records every worktree it creates."""

    def __init__(self, path) -> None:
        self.path = path
        self.created: list[str] = []
        self.removed: list[str] = []
        self.commit_result: tuple[str | None, str | None] = ("abc123456789", None)

    async def can_isolate(self) -> bool:
        return True

    async def create(self, name: str):
        from types import SimpleNamespace

        self.created.append(name)
        return SimpleNamespace(path=self.path, branch="ai_sub_probe")

    async def commit_if_dirty(
        self, path: str, message: str
    ) -> tuple[str | None, str | None]:
        return self.commit_result

    async def remove(self, path: str | Path) -> None:
        self.removed.append(str(path))


class _NonIsolatingWorktreeManager(_IsolatingWorktreeManager):
    async def can_isolate(self) -> bool:
        return False


class _FailingCreateWorktreeManager(_IsolatingWorktreeManager):
    async def create(self, name: str):
        raise WorktreeError("cannot create a worktree here")


@pytest.mark.asyncio
async def test_a_readonly_background_run_never_creates_a_worktree(tmp_path):
    """The isolation decision lives in the runner: ``isolate=True`` plus
    ``readonly=True`` must still read the live workspace."""
    manager = _IsolatingWorktreeManager(tmp_path / "wt")
    runner = _runner(tmp_path)
    run_id = runner.spawn_background(
        "scan the tree", name="probe", worktree_manager=manager, isolate=True,
        readonly=True, emit=lambda _event: None,
    )
    await _settle(runner, run_id)

    handle = runner._handles[run_id]
    assert manager.created == []
    assert handle.worktree_path is None


@pytest.mark.asyncio
async def test_a_writable_spawn_keeps_the_default_readonly_false(tmp_path):
    runner = _runner(tmp_path)
    run_id = runner.spawn_background("edit the tree", emit=lambda _event: None)

    handle = runner._handles[run_id]
    assert handle.readonly is False
    assert handle.session.readonly is False

    await _settle(runner, run_id)


@pytest.mark.asyncio
async def test_a_non_isolatable_workspace_degrades_a_background_run(tmp_path):
    manager = _NonIsolatingWorktreeManager(tmp_path / "wt")
    runner = _runner(tmp_path)
    run_id = runner.spawn_background(
        "edit the tree", name="probe", worktree_manager=manager,
        emit=lambda _e: None,
    )
    row = await _settle(runner, run_id)

    assert row.degraded == "worktree_unavailable"
    assert manager.created == []
    assert runner._handles[run_id].worktree_path is None


@pytest.mark.asyncio
async def test_a_worktree_create_failure_degrades_a_background_run(tmp_path):
    manager = _FailingCreateWorktreeManager(tmp_path / "wt")
    runner = _runner(tmp_path)
    run_id = runner.spawn_background(
        "edit the tree", name="probe", worktree_manager=manager,
        emit=lambda _e: None,
    )
    row = await _settle(runner, run_id)

    assert row.degraded == "worktree_create_failed"
    assert runner._handles[run_id].worktree_path is None


@pytest.mark.asyncio
async def test_readonly_and_non_isolated_runs_do_not_report_degradation(tmp_path):
    manager = _NonIsolatingWorktreeManager(tmp_path / "wt")
    runner = _runner(tmp_path)
    readonly = runner.spawn_background(
        "scan the tree", name="probe", worktree_manager=manager,
        readonly=True, emit=lambda _e: None,
    )
    plain = runner.spawn_background(
        "scan the tree", name="plain", worktree_manager=manager,
        isolate=False, emit=lambda _e: None,
    )

    assert (await _settle(runner, readonly)).degraded is None
    assert (await _settle(runner, plain)).degraded is None


@pytest.mark.asyncio
async def test_resume_reuses_the_tool_registry_so_the_file_tracker_survives(tmp_path):
    """C-5b real-run finding: a rebuilt registry forgets what was written."""
    from ai_coding.domain.message import ToolResult
    from ai_coding.tools.base import BaseTool

    class _Marker(BaseTool):
        parallel_safe = True
        mutates_world = False

        @property
        def name(self) -> str:
            return "marker"

        @property
        def description(self) -> str:
            return "marker"

        @property
        def params_json_schema(self) -> dict:
            return {"type": "object", "properties": {}}

        async def execute(self, params, ctx=None):  # noqa: ANN001
            return ToolResult(success=True, output="ok")

    registry = ToolRegistry()
    marker = _Marker()
    registry.register(marker)
    runner = _runner(tmp_path, build_tools=lambda _workdir: registry)
    run_id = runner.spawn_background("x", name="probe", emit=lambda _e: None)
    await _settle(runner, run_id)
    handle = runner._handles[run_id]
    first = handle.loop.tools.get("marker")

    # Simulate the interrupt path: the loop is gone, the handle is not.
    loop = handle.loop
    handle.loop = None
    await loop.aclose()

    await runner.resume(run_id)

    assert handle.loop is not None
    assert handle.loop.tools.get("marker") is first
    assert handle.registry is registry


# ---- P2-A: close/resume carry the write facts ------------------------------


@pytest.mark.asyncio
async def test_close_persists_commit_before_removing(tmp_path):
    manager = _IsolatingWorktreeManager(tmp_path / "wt")
    # The first turn finds nothing to commit, so only ``close`` can produce a
    # sha -- the meta file must not already contain one when the test starts.
    manager.commit_result = (None, None)
    runner = _runner(tmp_path)
    run_id = runner.spawn_background(
        "edit the tree", name="probe", worktree_manager=manager,
        emit=lambda _e: None,
    )
    await _settle(runner, run_id)
    meta = json.loads(_meta_path(tmp_path, run_id).read_text(encoding="utf-8"))
    assert meta["commit"] is None

    seen_at_remove: list[dict] = []

    async def remove_after_meta(path):
        seen_at_remove.append(
            json.loads(_meta_path(tmp_path, run_id).read_text(encoding="utf-8"))
        )
        manager.removed.append(str(path))

    manager.remove = remove_after_meta  # type: ignore[method-assign]
    manager.commit_result = ("abc123456789", None)

    await runner.close(run_id)

    meta = json.loads(_meta_path(tmp_path, run_id).read_text(encoding="utf-8"))
    assert meta["worktreeBranch"] == "ai_sub_probe"
    assert meta["commit"] == "abc123456789"
    assert manager.removed == [str(tmp_path / "wt")]
    # ``close`` writes the sidecar *before* deleting the worktree: at removal
    # time the sha is already durable.
    assert seen_at_remove[0]["commit"] == "abc123456789"
    assert seen_at_remove[0]["worktreeBranch"] == "ai_sub_probe"
    info = runner.close_info(run_id)
    assert info["commit"] == "abc123456789"
    assert info["worktreeBranch"] == "ai_sub_probe"


@pytest.mark.asyncio
async def test_close_records_a_commit_failure_in_meta(tmp_path):
    manager = _IsolatingWorktreeManager(tmp_path / "wt")
    manager.commit_result = (None, None)
    runner = _runner(tmp_path)
    run_id = runner.spawn_background(
        "edit the tree", name="probe", worktree_manager=manager,
        emit=lambda _e: None,
    )
    await _settle(runner, run_id)

    manager.commit_result = (None, "git commit: no identity")
    await runner.close(run_id)

    meta = json.loads(_meta_path(tmp_path, run_id).read_text(encoding="utf-8"))
    assert meta["commitError"] == "git commit: no identity"
    info = runner.close_info(run_id)
    assert info["commitError"] == "git commit: no identity"


@pytest.mark.asyncio
async def test_resume_rebuilds_task_packet_and_schema(tmp_path):
    schema = {"type": "object", "required": ["summary"]}
    packet = SubagentTask.from_objective(
        "do it", context_mode="task_only", output_schema=schema
    )
    runner = _runner(tmp_path)
    run_id = runner.spawn_background(packet, name="probe", emit=lambda _e: None)
    await _settle(runner, run_id)
    await runner.close(run_id)

    status, resumed = await runner.resume(run_id)

    assert status == "idle" and resumed == run_id
    handle = runner._handles[run_id]
    assert handle.request == packet
    assert handle.output_schema == schema


@pytest.mark.asyncio
async def test_resume_tolerates_old_meta_without_task_packet(tmp_path):
    runner = _runner(tmp_path)
    run_id = runner.spawn_background("do it", name="probe", emit=lambda _e: None)
    await _settle(runner, run_id)
    await runner.close(run_id)

    meta_path = _meta_path(tmp_path, run_id)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.pop("taskPacket", None)
    meta.pop("outputSchema", None)
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    status, resumed = await runner.resume(run_id)

    assert status == "idle" and resumed == run_id
    handle = runner._handles[run_id]
    assert handle.request is None
    assert handle.output_schema is None
