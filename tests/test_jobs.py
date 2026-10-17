"""D-1: a long command the model does not wait for.

Three reference harnesses were read before this file was written, and two of
their facts shaped it entirely:

* dsh registers **every** bash call as a job and, when a foreground wait runs
  out of time, promotes it (``tool-bash/src/index.ts:52``) -- and a nonzero
  exit is *reported*, not failed (``shell/tool-bash/src/background.ts``).
* opencode streams background output to a **file** and tells the model the
  path (``tool/plugin/shell.ts:95-97``); its foreground timeout kills and says
  "retry with a larger timeout".
* Claude Code learned the hard way that silently backgrounding a command the
  model did not ask to background is a bug report, not a feature (#31518).

So: a job here is a ledger row plus a process in its own session whose stdout
and stderr go straight to a file.  Not through ``run_process``: that one reads
pipes, and a parent that exits takes the read end with it -- the child would
get EPIPE on its next write and die, which is the opposite of what "the command
keeps running after you close the app" has to mean.

The vocabulary is dsh's: ``completed`` carries the exit code in ``detail``,
because the command running to completion and the command succeeding are two
different sentences.
"""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path

import pytest

from ai_coding.core.jobs import (
    MAX_RUNNING_JOBS,
    JobIdentityUnverifiable,
    JobLimitReached,
    JobRegistry,
)


class _Clock:
    """A notice queue you can look at without draining the real one."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def put(self, text: str, *, wake: bool = False) -> bool:
        # `wake` is ignored here: this double stands in for the queue, not for
        # the UI that wakes a session (D-1c).
        self.lines.append(text)
        return True

    def pending(self) -> int:
        return len(self.lines)

    def drain(self) -> list[str]:
        taken, self.lines = self.lines, []
        return taken


def _wait_until(predicate, timeout: float = 10.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


def _registry(tmp_path: Path, notices=None) -> JobRegistry:
    return JobRegistry(root=tmp_path / "jobs", notices=notices if notices is not None else _Clock())


# ---- starting one ----------------------------------------------------------


def test_a_started_job_returns_at_once_with_its_id_and_log_path(tmp_path):
    """The model asked not to wait, so waiting is the bug.  It gets an id it can
    kill and a path it can read, and the command is still running."""
    registry = _registry(tmp_path)

    job = registry.start(
        "sleep 30", cwd=str(tmp_path), session_id="s1", emit=lambda event: None
    )

    assert job.id.startswith("job_")
    assert job.pid > 0
    assert Path(job.log_path).is_file()
    assert job.status == "running"
    assert _alive(job.pid), "the command must still be running after start returned"
    registry.kill(job.id, reason="test over")


def test_output_lands_in_the_log_while_the_command_runs(tmp_path):
    """A file, not a pipe: the point is that the log outlives this process."""
    registry = _registry(tmp_path)

    job = registry.start(
        "echo hello-from-the-job", cwd=str(tmp_path), session_id="s1", emit=lambda e: None
    )
    _wait_until(lambda: job.status != "running", what="the job to settle")

    assert "hello-from-the-job" in Path(job.log_path).read_text(encoding="utf-8")


def test_the_parent_may_exit_without_killing_the_job(tmp_path):
    """The whole point of Q4.  ``start_new_session`` puts the child in its own
    session and the log is a file, so nothing this process owns is something the
    child depends on."""
    registry = _registry(tmp_path)
    job = registry.start("sleep 30", cwd=str(tmp_path), session_id="s1", emit=lambda e: None)

    registry.forget(job.id)  # every in-process handle gone, as at interpreter exit

    assert _alive(job.pid), "the child must survive the parent dropping it"
    _kill_pid(job.pid)


def test_a_job_that_never_started_leaves_no_row(tmp_path):
    """A ledger row for a command that never ran is a lie the next run reads."""
    registry = _registry(tmp_path)

    with pytest.raises(OSError):
        registry.start("echo x",
            cwd=str(tmp_path / "no-such-dir"), session_id="s1", emit=lambda e: None)
    assert registry.list() == []
    assert not list((tmp_path / "jobs").glob("*.json"))


# ---- settling --------------------------------------------------------------


def test_a_finished_job_announces_exactly_once_with_its_exit_status(tmp_path):
    """Claude Code shipped without this and had to add it (#21191): a job the
    model cannot hear about finishing is a job it will guess about."""
    notices = _Clock()
    registry = _registry(tmp_path, notices)

    job = registry.start("exit 3", cwd=str(tmp_path), session_id="s1", emit=lambda e: None)
    _wait_until(lambda: job.status != "running", what="settlement")

    _wait_until(lambda: notices.pending() == 1, what="the notice")
    assert len(notices.lines) == 1, "one completion, one notice -- not one per poll"
    assert "exit 3" in notices.lines[0], notices.lines[0]


def test_a_nonzero_exit_is_reported_not_failed(tmp_path):
    """dsh's rule (``background.ts``): the command ran; the command failed.
    Collapsing the two teaches the model that a red test suite is an outage."""
    registry = _registry(tmp_path)

    job = registry.start("exit 3", cwd=str(tmp_path), session_id="s1", emit=lambda e: None)
    _wait_until(lambda: job.status != "running", what="settlement")

    assert job.status == "completed"
    assert "3" in job.detail


def test_the_notice_carries_the_tail_so_the_model_needs_no_second_call(tmp_path):
    """Audit P2: writing output to a file is only half a design.  ``read`` has no
    output cap (it refuses a 10MB file, it does not trim a 5MB one), so without
    the tail in the notice a log read is a context flood."""
    notices = _Clock()
    registry = _registry(tmp_path, notices)

    registry.start(
        "for i in 1 2 3 4 5; do echo MARK-$i; done",
        cwd=str(tmp_path),
        session_id="s1",
        emit=lambda e: None,
    )
    _wait_until(lambda: notices.pending() == 1, what="the notice")

    assert "MARK-5" in notices.lines[0], notices.lines[0]


def test_the_operator_sees_the_same_settlement_as_an_event(tmp_path):
    """The TUI already knows how to render ``background.done``; a bash job that
    skips it is a job the operator cannot see finish."""
    from ai_coding.core.events import BackgroundTaskDone

    seen: list[object] = []
    registry = _registry(tmp_path)

    job = registry.start("exit 0", cwd=str(tmp_path), session_id="s1", emit=seen.append)
    _wait_until(lambda: job.status != "running", what="settlement")

    done = [event for event in seen if isinstance(event, BackgroundTaskDone)]
    assert done and done[0].name == "bash" and done[0].run_id == job.id


def test_settlement_is_idempotent(tmp_path):
    """Two settlers would mean two notices and two toasts for one command."""
    notices = _Clock()
    registry = _registry(tmp_path, notices)
    job = registry.start("exit 0", cwd=str(tmp_path), session_id="s1", emit=lambda e: None)
    # Wait for the notice, not just the status: _settle writes the status
    # before it queues the notice, and under load the poll can land between
    # the two.  Waiting on the status alone made this test a race.
    _wait_until(lambda: notices.pending() == 1, what="the notice")

    registry._settle(job, emit=lambda e: None)  # the late watcher callback
    registry._settle(job, emit=lambda e: None)

    assert notices.pending() == 1


# ---- bounds ----------------------------------------------------------------


def test_a_job_whose_log_passes_the_cap_is_killed_and_says_so(tmp_path, monkeypatch):
    """``yes`` would fill the user's disk.  The cap is enforced by the watcher
    (not by RLIMIT_FSIZE, which needs ``preexec_fn`` -- unsafe in a threaded
    program), and the reason reaches the model instead of a silent truncation."""
    from ai_coding.core import jobs as jobs_module

    monkeypatch.setattr(jobs_module, "MAX_JOB_LOG_BYTES", 64 * 1024)
    notices = _Clock()
    registry = _registry(tmp_path, notices)

    job = registry.start(
        "yes abcdefghijklmnopqrstuvwxyz",
        cwd=str(tmp_path),
        session_id="s1",
        emit=lambda e: None,
    )
    _wait_until(lambda: job.status != "running", timeout=20.0, what="the cap to stop it")

    assert job.status == "killed"
    assert "日志" in job.detail or "log" in job.detail
    assert Path(job.log_path).stat().st_size <= 64 * 1024 + 4096


def test_the_running_job_count_is_capped_with_an_honest_error(tmp_path):
    """dsh admits ten per owner (``jobs-local/src/index.ts:34``).  Without a cap
    a confused turn can start a hundred dev servers and find out from the OOM
    killer."""
    registry = _registry(tmp_path)
    started = [
        registry.start("sleep 30", cwd=str(tmp_path), session_id="s1", emit=lambda e: None)
        for _ in range(MAX_RUNNING_JOBS)
    ]

    with pytest.raises(JobLimitReached) as raised:
        registry.start("sleep 30", cwd=str(tmp_path), session_id="s1", emit=lambda e: None)

    assert str(MAX_RUNNING_JOBS) in str(raised.value)
    for job in started:
        registry.kill(job.id, reason="test over")


# ---- across processes ------------------------------------------------------


def test_the_ledger_survives_so_the_next_run_can_see_the_job(tmp_path):
    registry = _registry(tmp_path)
    job = registry.start("sleep 30", cwd=str(tmp_path), session_id="s1", emit=lambda e: None)

    row = json.loads((tmp_path / "jobs" / f"{job.id}.json").read_text(encoding="utf-8"))
    assert row["status"] == "running" and row["pid"] == job.pid

    registry.forget(job.id)
    _kill_pid(job.pid)


def test_a_rescan_adopts_a_job_that_is_still_running(tmp_path):
    """What "survives a restart" has to mean: the next process sees it, can name
    it, and can stop it."""
    first = _registry(tmp_path)
    job = first.start("sleep 30", cwd=str(tmp_path), session_id="old-session", emit=lambda e: None)
    first.forget(job.id)

    second = _registry(tmp_path)

    adopted = [row for row in second.list() if row.id == job.id]
    assert adopted and adopted[0].adopted, "the running job must reappear, marked as inherited"
    second.kill(job.id, reason="cleanup")
    assert not _alive(job.pid)


def test_a_rescan_calls_a_reused_pid_dead(tmp_path):
    """The safety case for "survives a restart": pid 4242 in a ledger row may be
    some other program by now.  The row here points at *this* process, which is
    very much alive and emphatically not the job -- killing on that ledger row
    would kill the test runner."""
    first = _registry(tmp_path)
    job = first.start("sleep 30", cwd=str(tmp_path), session_id="old", emit=lambda e: None)
    first.forget(job.id)
    _kill_pid(job.pid)

    row_path = tmp_path / "jobs" / f"{job.id}.json"
    row = json.loads(row_path.read_text(encoding="utf-8"))
    row["pid"] = os.getpid()  # a pid that is very much alive, and not ours
    row_path.write_text(json.dumps(row), encoding="utf-8")

    second = _registry(tmp_path)

    stale = next(row for row in second.list() if row.id == job.id)
    assert stale.status == "unknown", "a reused pid must read as unknown, never as running"
    assert "占用" in stale.detail or "reused" in stale.detail

    returned = second.kill(job.id, reason="must not happen")

    assert returned.status == "unknown", "killing a row we cannot vouch for says so"
    assert _alive(os.getpid()), "no signal may be sent on a pid we could not verify"


def test_a_pid_this_platform_cannot_verify_is_readable_but_not_killable(tmp_path, monkeypatch):
    """No ``/proc`` means no start time to compare, and a wrong SIGTERM kills
    somebody's build.  Refusing is the honest answer; pretending is not."""
    from ai_coding.core import jobs as jobs_module

    first = _registry(tmp_path)
    job = first.start("sleep 30", cwd=str(tmp_path), session_id="old", emit=lambda e: None)
    first.forget(job.id)

    monkeypatch.setattr(jobs_module, "_process_start_ticks", lambda pid: None)
    second = _registry(tmp_path)

    inherited = next(row for row in second.list() if row.id == job.id)
    assert inherited.status == "running", "it may well still be running; we just cannot prove it"
    with pytest.raises(JobIdentityUnverifiable):
        second.kill(job.id, reason="nope")
    assert _alive(job.pid), "the refusal must not have signalled anything"
    _kill_pid(job.pid)


def test_a_corrupt_ledger_row_does_not_stop_the_next_run(tmp_path):
    """Crash-safe writes make this unlikely; a hand-edited or truncated file must
    still not stop the product from starting."""
    (tmp_path / "jobs").mkdir(parents=True)
    (tmp_path / "jobs" / "job_broken.json").write_text("{not json", encoding="utf-8")

    registry = _registry(tmp_path)

    assert registry.list() == []


# ---- helpers ---------------------------------------------------------------


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _kill_pid(pid: int) -> None:
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


# ---- audit: the registry under concurrency and big files --------------------


def test_read_head_reports_the_real_length_even_when_it_trims(tmp_path):
    """The truncation marker says "total N chars"; N has to be the file, not the
    window we happened to read.  Audited: it used to be the window."""
    registry = _registry(tmp_path)
    job = registry.start("echo hi", cwd=str(tmp_path), session_id="s", emit=lambda e: None)
    _wait_until(lambda: job.status != "running", what="settlement")

    import pathlib as _pathlib

    _pathlib.Path(job.log_path).write_bytes(b"x" * 5000)
    text, size = registry.read_head(job, 100)

    assert size == 5000, f"the total must be the file, got {size}"
    assert len(text) == 100
    registry.discard(job.id)


def test_concurrent_reads_and_mutations_never_raise(tmp_path):
    """_publish runs on watcher threads while the table mutates elsewhere;
    iterating it unguarded hands the caller a RuntimeError ("dictionary changed
    size during iteration") instead of a panel.

    No processes involved: the race is in the dict, so the test mutates the
    dict straight from this thread while publishers spin beside it.  Thousands
    of overlaps in about a second -- the guarded code cannot fail it.
    """
    import sys
    import threading

    from ai_coding.core.jobs import Job

    registry = _registry(tmp_path)
    # Shrink the GIL switch window so a mutation is likely to land inside one
    # publish pass.  Pure dict work, no spawns: nothing here can starve.
    old_interval = sys.getswitchinterval()
    sys.setswitchinterval(0.0001)
    stop = threading.Event()
    errors: list[BaseException] = []

    def publish_loop() -> None:
        try:
            while not stop.is_set():
                registry._publish()
        except BaseException as exc:  # noqa: BLE001 - collected, asserted below
            errors.append(exc)

    def seed(index: int) -> Job:
        return Job(
            id=f"job_x{index:05d}", command="seed", pid=0, log_path="",
            started_at="", emit=None,
        )

    threads = [threading.Thread(target=publish_loop) for _ in range(8)]
    for thread in threads:
        thread.start()
    try:
        # Every mutation takes the lock, exactly like every production path
        # (start/discard/kill/rescan): the lock serializes lock-holders, and a
        # thread that skips it is not a race, it is a bug in the test.
        for index in range(2000):
            with registry._lock:
                registry._jobs[f"job_x{index:05d}"] = seed(index)
                if index % 2:
                    registry._jobs.pop(f"job_x{index - 1:05d}", None)
    finally:
        stop.set()
    for thread in threads:
        thread.join(timeout=30)
    sys.setswitchinterval(old_interval)
    assert not errors, f"concurrent access raised: {errors!r}"


def test_a_settled_foreground_job_publishes_its_ending_and_its_absence(tmp_path):
    """The panel bug, pinned: a foreground command that answers inside its wait
    produced exactly one JobChanged (the start) and then silence.  The settle
    publish read its emit out of the live table, but the waiter discards the
    row between `done.set()` and the publish -- and the discard's own publish
    found an empty table, so it said nothing either.  The panel read "1
    running" forever for a command that had finished."""
    seen: list[tuple[str, list[tuple[str | None, str | None]]]] = []

    def rec(event: object) -> None:
        rows = getattr(event, "jobs", None) or ()
        seen.append(
            (
                type(event).__name__,
                [(row.get("id"), row.get("status")) for row in rows],
            )
        )

    registry = _registry(tmp_path)
    job = registry.start(
        "echo done-quick", cwd=str(tmp_path), session_id="s", emit=rec,
    )
    _wait_until(lambda: job.status != "running", what="settlement")

    registry.discard(job.id)

    starts = [
        rows for kind, rows in seen
        if kind == "JobChanged" and any(status == "running" for _, status in rows)
    ]
    assert starts, f"the start publish is missing: {seen}"
    empties = [rows for kind, rows in seen if kind == "JobChanged" and rows == []]
    assert empties, f"the empty publish after discard is missing: {seen}"
