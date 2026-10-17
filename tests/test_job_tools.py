"""D-1: what the model can do about a background job once it has started one.

Two tools, chosen by what a model actually needs afterwards: *what is running*
and *stop that*. dsh also ships ``job_output`` with a blocking ``wait``; that one
was cut on purpose -- a cursor plus a wait plus a timeout is the heaviest piece
of dsh's job surface, and this project answers the same question two cheaper
ways: the completion notice carries the tail of the log, and ``job_list`` carries
three lines per row.

Every assertion here is about what the model is *told*, because a job registry
that lies is worse than one that is missing: "no such job", "already finished",
"cannot verify that pid" -- each of those is a sentence the model can act on.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ai_coding.core.jobs import JobRegistry
from ai_coding.tools.base import ToolContext
from ai_coding.tools.job_tools import JobKillTool, JobListTool


class _Notices:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def put(self, text: str) -> bool:
        self.lines.append(text)
        return True

    def pending(self) -> int:
        return len(self.lines)

    def drain(self) -> list[str]:
        taken, self.lines = self.lines, []
        return taken


def _ctx(
    tmp_path: Path, registry: JobRegistry | None, workspace: Path | None = None
) -> ToolContext:
    return ToolContext(jobs=registry, workspace=str(workspace or tmp_path))


def _killpid(pid: int) -> None:
    import signal

    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


# ---- job_list --------------------------------------------------------------


@pytest.mark.asyncio
async def test_listing_an_empty_workspace_says_so_rather_than_printing_nothing(tmp_path):
    """An empty table teaches the model nothing; one sentence does."""
    registry = JobRegistry(root=tmp_path / "jobs")

    result = await JobListTool().execute({}, _ctx(tmp_path, registry))

    assert result.success is True
    assert "no background job" in result.output


@pytest.mark.asyncio
async def test_listing_names_the_id_the_command_the_status_and_where_the_log_is(tmp_path):
    """Everything the model needs to decide whether it still cares about a job."""
    registry = JobRegistry(root=tmp_path / "jobs")
    job = registry.start(
        "echo starting; sleep 30",
        cwd=str(tmp_path),
        session_id="s1",
        emit=lambda e: None,
    )
    try:
        result = await JobListTool().execute({}, _ctx(tmp_path, registry))

        assert job.id in result.output
        assert "running" in result.output
        assert job.log_path in result.output
        assert "starting" in result.output, "the row carries a peek at the log, not just a path"
    finally:
        registry.kill(job.id, reason="test over")


@pytest.mark.asyncio
async def test_a_job_from_an_earlier_run_is_labelled_as_inherited(tmp_path):
    """There is no session fencing here: a job outlives the conversation that
    started it and is shared with every other session in this workspace. The
    model has to be told, or it will treat a stranger's dev server as its own."""
    first = JobRegistry(root=tmp_path / "jobs")
    job = first.start("sleep 30", cwd=str(tmp_path), session_id="old", emit=lambda e: None)
    first.forget(job.id)

    second = JobRegistry(root=tmp_path / "jobs")
    try:
        output = (await JobListTool().execute({}, _ctx(tmp_path, second))).output

        assert job.id in output
        assert "earlier run" in output or "inherited" in output
    finally:
        _killpid(job.pid)


@pytest.mark.asyncio
async def test_listing_stays_bounded_and_says_when_it_truncated(tmp_path):
    """A workspace with a year of jobs must not turn one tool call into a log."""
    root = tmp_path / "jobs"
    root.mkdir(parents=True)
    for index in range(30):
        (root / f"job_old{index:03d}.json").write_text(
            json.dumps(
                {
                    "id": f"job_old{index:03d}",
                    "command": f"old job {index}",
                    "pid": 999_999,
                    "log_path": str(root / f"job_old{index:03d}.log"),
                    "started_at": "2026-01-01T00:00:00Z",
                    "origin_session": "s",
                    "status": "completed",
                    "finished_at": "2026-01-01T00:05:00Z",
                    "exit_code": 0,
                    "detail": "exit 0",
                }
            ),
            encoding="utf-8",
        )

    registry = JobRegistry(root=root)

    output = (await JobListTool().execute({}, _ctx(tmp_path, registry))).output

    rows = [line for line in output.splitlines() if line.startswith("job_old")]
    assert len(rows) == 20, "the listing must not grow with the ledger"
    assert "10 older finished job" in output, "and it must say how many rows it left out"


@pytest.mark.asyncio
async def test_a_workspace_with_no_registry_says_the_tools_are_not_wired(tmp_path):
    """The same honesty C-1 uses for a missing transcript: a confident empty
    answer would be a lie about a tool that was never given a registry."""
    result = await JobListTool().execute({}, _ctx(tmp_path, None))

    assert result.success is False
    assert result.error == "no_jobs"


# ---- job_kill --------------------------------------------------------------


@pytest.mark.asyncio
async def test_killing_an_unknown_id_is_an_error_not_a_silent_success(tmp_path):
    registry = JobRegistry(root=tmp_path / "jobs")

    result = await JobKillTool().execute({"job_id": "job_nope"}, _ctx(tmp_path, registry))

    assert result.success is False
    assert result.error == "unknown_job"
    assert "job_nope" in result.output


@pytest.mark.asyncio
async def test_killing_a_job_that_already_finished_reports_its_real_ending(tmp_path):
    """The failure this guards against is the model retrying ``job_kill`` and
    reading a cheerful 'stopped' while the process ran on five minutes ago."""
    registry = JobRegistry(root=tmp_path / "jobs")
    job = registry.start(
        "exit 3", cwd=str(tmp_path), session_id="s1", emit=lambda e: None
    )
    import time

    deadline = time.monotonic() + 10
    while job.status == "running" and time.monotonic() < deadline:
        time.sleep(0.05)

    result = await JobKillTool().execute({"job_id": job.id}, _ctx(tmp_path, registry))

    assert result.success is True
    assert "already" in result.output
    assert "3" in result.output, "the real ending, not a fake stop"


@pytest.mark.asyncio
async def test_killing_a_running_job_stops_it_and_says_why(tmp_path):
    registry = JobRegistry(root=tmp_path / "jobs")
    job = registry.start(
        "sleep 30", cwd=str(tmp_path), session_id="s1", emit=lambda e: None
    )

    result = await JobKillTool().execute(
        {"job_id": job.id, "reason": "dev server no longer needed"},
        _ctx(tmp_path, registry),
    )

    assert result.success is True
    assert job.status == "killed"
    assert "dev server" in job.detail, "the reason the model gave is the reason it reads back"
    try:
        os.kill(job.pid, 0)
        raise AssertionError("the process is still alive")
    except (ProcessLookupError, PermissionError):
        pass


@pytest.mark.asyncio
async def test_killing_a_job_this_platform_cannot_verify_is_refused_outright(tmp_path, monkeypatch):
    """No ``/proc`` means no start time, and a wrong SIGTERM kills somebody
    else's build. Refusing is the answer; guessing is not."""
    from ai_coding.core import jobs as jobs_module

    first = JobRegistry(root=tmp_path / "jobs")
    job = first.start("sleep 30", cwd=str(tmp_path), session_id="old", emit=lambda e: None)
    first.forget(job.id)
    monkeypatch.setattr(jobs_module, "_process_start_ticks", lambda pid: None)

    second = JobRegistry(root=tmp_path / "jobs")
    try:
        result = await JobKillTool().execute(
            {"job_id": job.id}, _ctx(tmp_path, second)
        )

        assert result.success is False
        assert result.error == "unverifiable_job"
        os.kill(job.pid, 0)  # still alive: nothing was signalled
    finally:
        _killpid(job.pid)


@pytest.mark.asyncio
async def test_killing_without_a_registry_is_an_error_not_a_no_op(tmp_path):
    result = await JobKillTool().execute({"job_id": "job_x"}, _ctx(tmp_path, None))

    assert result.success is False
    assert result.error == "no_jobs"
