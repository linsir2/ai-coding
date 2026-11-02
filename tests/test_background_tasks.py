"""Background sub-agents (R1-2, TDD): delegation without hostage-taking.

A synchronous ``subAgent`` call burns the parent's whole turn budget waiting
(C2 measured 300s for zero progress).  With ``background=true`` the tool
returns a run id at once; the child runs on, and its completion lands as an
event plus a table row — never as a second tool message (strict pairing
allows exactly one tool message per call).
"""

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.core.events import BackgroundTaskDone
from ai_coding.core.subagent import SubAgentRunner, _notice_text
from ai_coding.domain.loop import StepResult
from ai_coding.domain.subagent import BackgroundTask, SubagentResult
from ai_coding.tools.base import ToolContext
from ai_coding.tools.registry import ToolRegistry
from ai_coding.tools.subagent_tool import SubAgentTool


class _Engine(ChatEngine):
    def __init__(self, text="did it", hang=False, usage=None):
        self._text = text
        self._hang = hang
        self._usage = usage

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        if self._hang:
            await asyncio.sleep(30)
        return StepResult(text=self._text, finish_reason="stop", usage=self._usage)

    async def aclose(self):
        return None


def _runner(engine, **over):
    kwargs = {
        "app_config": object(),
        "engine_factory": lambda: engine,
        "build_tools": lambda wd: ToolRegistry(),
        "timeout_seconds": 600.0,
    }
    kwargs.update(over)
    return SubAgentRunner(**kwargs)


@pytest.mark.asyncio
async def test_spawn_returns_at_once_while_child_runs_on():
    runner = _runner(_Engine(hang=True))
    run_id = runner.spawn_background("sleep a while", name="sleeper",
                                     emit=lambda e: None)
    assert isinstance(run_id, str) and run_id
    rows = runner.task_list()
    assert len(rows) == 1 and rows[0].status == "running"
    assert rows[0].name == "sleeper"
    assert runner.cancel(run_id) is True
    for _ in range(100):
        if runner.task_list()[0].status != "running":
            break
        await asyncio.sleep(0.02)
    assert runner.task_list()[0].status == "cancelled"


@pytest.mark.asyncio
async def test_saturated_pool_rejects_instead_of_queueing_silently():
    runner = _runner(_Engine(), max_concurrent=1)
    # C-5a review: the pool's real bound is the running rows, not whether some
    # task happens to hold the semaphore; occupy one slot the same way a live
    # run does.
    runner._background["live"] = BackgroundTask(
        run_id="live", status="running", started=time.monotonic()
    )
    with pytest.raises(RuntimeError, match="concurrency exhausted"):
        runner.spawn_background("x", emit=lambda e: None)
    # The rejected spawn must not leave a row behind; the live one stays.
    assert [row.run_id for row in runner.task_list()] == ["live"]


@pytest.mark.asyncio
async def test_spawn_that_cannot_create_a_task_settles_the_row(monkeypatch):
    """C-5a review: a row written before ``create_task`` must not stay running."""
    runner = _runner(_Engine())

    class _BadLoop:
        def create_task(self, *_args, **_kwargs):
            raise RuntimeError("loop closed")

    monkeypatch.setattr(
        "ai_coding.core.subagent.asyncio.get_running_loop", lambda: _BadLoop()
    )

    with pytest.raises(RuntimeError, match="failed to start"):
        runner.spawn_background("x", emit=lambda e: None)

    rows = runner.task_list()
    assert len(rows) == 1
    assert rows[0].status == "error"
    assert "loop closed" in (rows[0].error or "")


@pytest.mark.asyncio
async def test_completion_lands_as_event_with_summary():
    seen: list = []
    runner = _runner(_Engine(text="all green"))
    run_id = runner.spawn_background("do it", name="probe", emit=seen.append)
    for _ in range(200):
        if runner.task_list() and runner.task_list()[0].status != "running":
            break
        await asyncio.sleep(0.02)
    row = runner.task_list()[0]
    assert row.status == "completed"
    assert len(seen) == 1 and isinstance(seen[0], BackgroundTaskDone)
    event = seen[0]
    assert event.run_id == run_id and event.name == "probe"
    assert event.status == "completed" and "all green" in event.summary
    assert event.truncated is False


@pytest.mark.asyncio
async def test_a_completion_is_queued_where_the_model_can_read_it():
    """P0-2: the event reaches the operator, but the model had no sink at all --
    the tool result tells it "check /tasks", which is a human command.  The same
    settlement therefore puts a line on the harness lane."""
    from ai_coding.core.steering import SteeringQueue

    notices = SteeringQueue()
    runner = _runner(_Engine(text="all green"), notices=notices)
    run_id = runner.spawn_background("do it", name="probe", emit=lambda _event: None)
    for _ in range(200):
        if runner.task_list() and runner.task_list()[0].status != "running":
            break
        await asyncio.sleep(0.02)

    lines = notices.drain()
    assert len(lines) == 1
    line = lines[0]
    assert "[子任务完成]" in line
    assert "probe" in line
    assert run_id[:12] in line
    assert "all green" in line


@pytest.mark.asyncio
async def test_a_cancelled_before_start_run_is_announced_too():
    """``_reap`` is the other settlement path (a task cancelled before its first
    step never runs the supervised body): the model must learn about it on the
    same lane, or it waits forever for a run that will never speak."""
    from ai_coding.core.steering import SteeringQueue

    notices = SteeringQueue()
    runner = _runner(_Engine(hang=True), notices=notices)
    run_id = runner.spawn_background("do it", name="probe", emit=lambda _event: None)
    assert runner.cancel(run_id) is True
    for _ in range(200):
        if notices.pending:
            break
        await asyncio.sleep(0.02)

    assert "[子任务" in notices.drain()[0]


@pytest.mark.asyncio
async def test_no_notice_lane_is_not_an_error():
    """The CLI and the tests build runners without a world: the queue is
    optional, and a completion must still settle."""
    runner = _runner(_Engine(text="all green"))
    runner.spawn_background("do it", name="probe", emit=lambda _event: None)
    for _ in range(200):
        if runner.task_list() and runner.task_list()[0].status != "running":
            break
        await asyncio.sleep(0.02)

    assert runner.task_list()[0].status == "completed"


@pytest.mark.asyncio
async def test_child_spend_is_recorded_on_the_row():
    seen: list = []
    usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
    runner = _runner(_Engine(text="done", usage=usage))
    runner.spawn_background("do it", emit=seen.append)
    for _ in range(200):
        if runner.task_list()[0].status != "running":
            break
        await asyncio.sleep(0.02)
    # 19 §3.5: the ledger adds the window reading (last step's prompt) beside
    # the provider numbers; the row carries it too.
    assert runner.task_list()[0].usage == {**usage, "context_tokens": 100}


@pytest.mark.asyncio
async def test_cancel_unknown_id_is_false():
    runner = _runner(_Engine())
    assert runner.cancel("nope") is False


def test_background_task_row_round_trips():
    row = BackgroundTask(run_id="abc123", name="n", task="t", status="running",
                         started=1.0)
    assert BackgroundTask.from_dict(row.to_dict()).run_id == "abc123"


@pytest.mark.asyncio
async def test_tool_background_param_spawns_and_returns_a_handle():
    seen_observers: list = []

    class _CannedRunner:
        timeout_seconds = 600.0
        context_mode = "full"

        async def run(self, task, name="sub-agent", **kwargs):
            raise AssertionError("sync path must not run")

        def spawn_background(self, task, name="sub-agent", **kwargs):
            seen_observers.append(kwargs.get("observer"))
            return "run-1"

    tool = SubAgentTool(_CannedRunner())
    result = await tool.execute({"task": "do it", "background": True},
                                ToolContext(emit=lambda e: None))
    assert result.success is True
    assert "run-1" in result.output and result.data["run_id"] == "run-1"
    assert result.data["background"] is True
    assert seen_observers[0] is not None  # child events still fan in


@pytest.mark.asyncio
async def test_tool_defaults_to_the_synchronous_path():
    from ai_coding.domain.subagent import SubagentResult

    class _CannedRunner:
        timeout_seconds = 600.0
        context_mode = "full"

        async def run(self, task, name="sub-agent", **kwargs):
            return SubagentResult(status="completed", conclusion="done", iterations=1)

    tool = SubAgentTool(_CannedRunner())
    result = await tool.execute({"task": "do it"}, ToolContext(emit=lambda e: None))
    assert result.success is True and "done" in result.output





@pytest.mark.asyncio
async def test_table_prunes_oldest_terminal_rows_first(monkeypatch):
    import ai_coding.core.subagent as subagent_mod

    monkeypatch.setattr(subagent_mod, "_MAX_BACKGROUND_ROWS", 2)
    runner = _runner(_Engine(text="done"))

    async def settle():
        for _ in range(200):
            if all(r.status != "running" for r in runner.task_list()):
                return
            await asyncio.sleep(0.02)

    runner.spawn_background("one", name="two", emit=lambda e: None)
    await settle()
    runner.spawn_background("two", name="three", emit=lambda e: None)
    await settle()
    runner.spawn_background("three", name="four", emit=lambda e: None)
    await settle()
    rows = runner.task_list()
    # C-5b-1 handles-first: a settled row whose handle is still addressable is
    # protected from pruning (the id must keep resolving after the row goes).
    assert len(rows) == 3
    # Once the handles are dropped (close in a later phase), the old bound
    # applies again: the oldest terminal rows go first.
    runner._handles.clear()
    runner._prune_background()
    rows = runner.task_list()
    assert len(rows) == 2
    assert [r.name for r in rows] == ["three", "four"]
    assert all(r.status != "running" for r in rows)


# ---- P2-A: row / notice / event are one projection ------------------------


def test_notice_carries_facts_changes_branch_and_degraded():
    result = SubagentResult(
        status="completed",
        conclusion="did it",
        facts={"summary": "ok", "key_files": ["a.py"]},
        changed_paths=("a.py", "b.py"),
        worktree_branch="ai_sub_probe",
        commit="abcdef123456",
        commit_error="git commit: no identity",
        degraded="worktree_unavailable",
        ledger_path="/tmp/x.json",
    )

    line = _notice_text("run-1", "probe", result)

    assert "facts:" in line
    assert "改动 2 个文件" in line
    assert "分支 ai_sub_probe（未合并）" in line
    assert "提交失败" in line
    assert "降级：未隔离" in line
    assert "完整回包 /tmp/x.json" in line


@pytest.mark.asyncio
async def test_completion_event_carries_a_details_projection():
    seen: list = []
    runner = _runner(_Engine(text="done"))
    runner._background["run-1"] = BackgroundTask(  # noqa: SLF001
        run_id="run-1", name="probe", status="running", started=time.monotonic()
    )
    runner._announce["run-1"] = (seen.append, "origin", "probe")  # noqa: SLF001
    result = SubagentResult(
        status="completed",
        conclusion="the whole prose stays in the ledger",
        changed_paths=("a.py",),
        worktree_branch="ai_sub_probe",
        commit="abcdef123456",
        degraded="worktree_unavailable",
        ledger_path="/tmp/ledgers/abc123def456.json",
    )

    runner._finish_background("run-1", result, seen.append, "origin")  # noqa: SLF001

    assert len(seen) == 1 and isinstance(seen[0], BackgroundTaskDone)
    details = seen[0].details
    assert details is not None
    assert details["v"] == 3
    assert details["messageId"] == "abc123def456"
    assert details["changed_paths"] == ["a.py"]
    assert details["worktree_branch"] == "ai_sub_probe"
    assert details["commit"] == "abcdef123456"
    assert details["degraded"] == "worktree_unavailable"
    # A projection, not the packet: the conclusion and the task stay out.
    assert "conclusion" not in details
    assert "task" not in details


@pytest.mark.asyncio
async def test_row_projects_the_packet():
    runner = _runner(_Engine(text="done"))
    runner._background["run-1"] = BackgroundTask(  # noqa: SLF001
        run_id="run-1", name="probe", status="running", started=time.monotonic()
    )
    result = SubagentResult(
        status="completed",
        conclusion="did it",
        changed_paths=("a.py", "b.py"),
        worktree_branch="ai_sub_probe",
        commit="abcdef123456",
        commit_error="git commit: no identity",
        degraded="worktree_unavailable",
        ledger_path="/tmp/x.json",
    )

    runner._finish_background(  # noqa: SLF001
        "run-1", result, lambda _event: None, "origin"
    )

    row = runner.task_list()[0]
    assert row.changed_paths == ("a.py", "b.py")
    assert row.worktree_branch == "ai_sub_probe"
    assert row.commit == "abcdef123456"
    assert row.commit_error == "git commit: no identity"
    assert row.degraded == "worktree_unavailable"


class _Committer:
    """A persistent worktree manager that always commits one sha."""

    async def can_isolate(self) -> bool:
        return True

    async def create(self, name: str):
        return SimpleNamespace(path="/tmp/child", branch="ai_sub_probe")

    async def commit_if_dirty(
        self, path: str, message: str
    ) -> tuple[str | None, str | None]:
        return "abcdef123456", None

    async def remove(self, path: str) -> None:
        return None


@pytest.mark.asyncio
async def test_background_turn_commits_before_it_settles():
    """P2-A: the settlement must not precede the commit.

    A background run used to commit only at ``close``: the event said "done"
    while the work lived solely in a directory the next close would delete.
    """
    seen: list = []
    runner = _runner(_Engine(text="done"))
    runner.spawn_background(
        "do it", name="probe", emit=seen.append, worktree_manager=_Committer()
    )
    for _ in range(200):
        if runner.task_list() and runner.task_list()[0].status != "running":
            break
        await asyncio.sleep(0.02)

    row = runner.task_list()[0]
    assert row.status == "completed"
    assert row.commit == "abcdef123456"
    assert row.worktree_branch == "ai_sub_probe"
    assert len(seen) == 1
    details = seen[0].details
    assert details is not None
    assert details["commit"] == "abcdef123456"
    assert details["worktree_branch"] == "ai_sub_probe"


class _NoGit:
    """A configured manager whose root is not a git repository."""

    async def can_isolate(self) -> bool:
        return False

    async def create(self, name: str):
        raise AssertionError("create() must not be called")


@pytest.mark.asyncio
async def test_non_git_background_turn_degrades_the_ledger_too(tmp_path):
    """P2-A: a non-git child has no branch, so the old branch-gated refresh
    never ran and the ledger kept ``degraded: null`` while the row said
    ``worktree_unavailable``.  Both projections must agree now.
    """
    runner = _runner(
        _Engine(text="done"), workspace=str(tmp_path), ledger_root=tmp_path
    )
    run_id = runner.spawn_background(
        "do it", name="probe", emit=lambda _e: None, worktree_manager=_NoGit()
    )
    row = await _wait_settled(runner, run_id)

    assert row.degraded == "worktree_unavailable"
    assert row.ledger_path is not None
    payload = json.loads(Path(row.ledger_path).read_text(encoding="utf-8"))
    assert payload["degraded"] == row.degraded
    # The refresh merges result keys; the header and task survive.
    assert payload["message_id"] == Path(row.ledger_path).stem
    assert payload["task"]["brief"]["objective"] == "do it"
    assert payload["run_id"] == run_id


class _TwiceCommitter(_Committer):
    """Commits on the first turn, finds nothing to commit on the second."""

    def __init__(self) -> None:
        self.calls = 0

    async def commit_if_dirty(
        self, path: str, message: str
    ) -> tuple[str | None, str | None]:
        self.calls += 1
        if self.calls == 1:
            return "abcdef123456", None
        return None, None


async def _wait_settled(runner, run_id: str) -> BackgroundTask:
    for _ in range(200):
        for row in runner.task_list():
            if row.run_id == run_id and row.status != "running":
                return row
        await asyncio.sleep(0.02)
    raise AssertionError(f"run {run_id} never settled")


@pytest.mark.asyncio
async def test_a_second_turn_without_changes_keeps_the_first_turns_facts(tmp_path):
    """P2-A review: turn 2 reports ``(None, None)``; that must not erase turn 1's
    sha from the handle, the row, the sidecar or ``close_info``."""
    runner = _runner(
        _Engine(text="done"), workspace=str(tmp_path), ledger_root=tmp_path
    )
    manager = _TwiceCommitter()
    run_id = runner.spawn_background(
        "do it", name="probe", emit=lambda _e: None, worktree_manager=manager
    )
    row = await _wait_settled(runner, run_id)
    assert row.commit == "abcdef123456"
    meta_path = tmp_path / ".ai-coding" / "agents" / "meta" / f"{run_id}.json"
    assert json.loads(meta_path.read_text(encoding="utf-8"))["commit"] == "abcdef123456"

    status, _ = await runner.send_message(run_id, "again")
    assert status == "running"
    row = await _wait_settled(runner, run_id)

    handle = runner._handles[run_id]  # noqa: SLF001
    assert handle.commit == "abcdef123456"
    assert handle.worktree_branch == "ai_sub_probe"
    assert row.commit == "abcdef123456"
    assert row.worktree_branch == "ai_sub_probe"
    assert json.loads(meta_path.read_text(encoding="utf-8"))["commit"] == "abcdef123456"
    assert runner.close_info(run_id)["commit"] == "abcdef123456"
