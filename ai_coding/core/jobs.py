"""D-1: background bash jobs -- a long command the model does not wait for.

The three reference harnesses were read before this file existed, and their
differences decided its shape:

* dsh registers every bash call as a job and promotes a foreground one when its
  wait runs out (``tool-bash/src/index.ts:52``).  It also draws the line this
  module keeps drawing: a command that exited 3 *ran*, so the job is
  ``completed`` and the exit code lives in ``detail`` (``shell/tool-bash/src/
  background.ts``).
* opencode streams background output into a **file** and hands the model the
  path (``tool/plugin/shell.ts:95-97``); its foreground timeout kills.
* Claude Code shipped a completion notification late (#21191) and shipped
  silent backgrounding as a bug (#31518).  There is no *silent* promotion here:
  a foreground call that outruns its wait keeps running, and the very sentence
  that hands the model the id says so ("it was NOT killed").

Why not ``infra/subprocess_runner.run_process``: that one owns the pipes.  A
parent that exits takes the read end with it, so the child's next write gets
EPIPE and it dies -- the exact opposite of "the build keeps running after you
close the app".  Output therefore goes straight to a file, and the process runs
in its own session (``start_new_session=True``) so nothing this process holds is
something the child needs.

Two things this file refuses to do quietly:

* **No owner fencing** (dsh kills a session's jobs when the session stops).  A
  job here belongs to the workspace and outlives the session that made it, so
  any session in the workspace can see and stop it.  That is the chosen
  semantic, and the tools say so.
* **No signals on a doubt.**  Reattaching after a restart means acting on a pid
  from a file, and pids get reused.  On Linux the start time in
  ``/proc/<pid>/stat`` is compared against the ledger; where that is not
  available the job is readable but not killable, because a wrong signal kills
  somebody else's process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ai_coding.core.events import BackgroundTaskDone, JobChanged
from ai_coding.core.turn_report import emit_degraded
from ai_coding.infra.time_utils import now_utc
from ai_coding.security.process_sandbox import (
    SHELL_ARGV0,
    ProcessSandboxPolicy,
    SandboxBackend,
    SandboxGap,
    spawn_argv,
)

__all__ = [
    "MAX_JOB_LOG_BYTES",
    "WAIT_CANCELLED",
    "WAIT_SETTLED",
    "WAIT_TIMEOUT",
    "MAX_RUNNING_JOBS",
    "NOTICE_TAIL_LINES",
    "LIST_TAIL_LINES",
    "Job",
    "JobIdentityUnverifiable",
    "JobLimitReached",
    "JobRegistry",
]

_log = logging.getLogger(__name__)

#: Running jobs per workspace.  dsh admits ten per owner
#: (``jobs-local/src/index.ts:34``); the bound is not about politeness but about
#: a confused turn starting a hundred dev servers and learning about it from the
#: OOM killer.
MAX_RUNNING_JOBS = 10

#: A log bigger than this is stopped rather than allowed to fill the user's disk.
#: Enforced by the kernel through ``ulimit -f`` in the spawning shell, because a
#: watcher thread polling the file size is not a bound at all: measured, ``yes``
#: wrote **1.59 GB** inside one 2s poll interval.  The watcher stays as a
#: backstop for shells that ignore the limit.
MAX_JOB_LOG_BYTES = 20_000_000

#: ``ulimit -f`` counts *blocks*, and how big one is depends on the shell: POSIX
#: says 512 bytes, dash does that, and **bash counts 1024**.  One constant for
#: both is how the cap silently became 128 KB under bash when eval-08's shell
#: switch landed (measured: the cap test saw exactly 2× the configured limit).
_ULIMIT_BLOCK = 1024 if Path(SHELL_ARGV0).name == "bash" else 512
#: Signal 25 / a shell reporting 128+25: "this process wrote past its file limit".
_SIG_FILE_TOO_BIG = 25

#: Lines of log carried in the completion notice.  Without them the model has to
#: read a file whose size it cannot know, and ``read`` trims nothing (it refuses
#: a 10MB file; a 5MB one arrives whole).
NOTICE_TAIL_LINES = 10
#: Per-row tail in ``job_list`` -- enough to see a build failing, not enough to
#: turn a listing into a log dump.
LIST_TAIL_LINES = 3

#: Grace between asking a job to stop and insisting.
_KILL_GRACE_SECONDS = 2.0
#: How often a foreground wait checks its clock and the Esc flag.
_WAIT_TICK = 0.2
_TAIL_LINE_CHARS = 200

STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_KILLED = "killed"
#: Only ever produced by a rescan: the process is gone and nobody recorded how
#: it ended.  Guessing "completed" would be a lie, so the vocabulary says so.
STATUS_UNKNOWN = "unknown"

#: How a wait ended.  Three outcomes, not two: conflating "cancelled" with
#: "timed out" made a cancelled foreground call report itself as promoted --
#: telling the model a command it had just killed was "still running".
WAIT_SETTLED = "settled"
WAIT_TIMEOUT = "timeout"
WAIT_CANCELLED = "cancelled"


class JobLimitReached(RuntimeError):
    """Too many jobs are already running; the model has to stop one first."""


class JobIdentityUnverifiable(RuntimeError):
    """This platform cannot prove a ledger pid is still the job we started."""


@dataclass
class Job:
    """One background command.  Mutable because the watcher settles it in place."""

    id: str
    command: str
    pid: int
    log_path: str
    started_at: str
    origin_session: str = ""
    status: str = STATUS_RUNNING
    finished_at: str = ""
    exit_code: int | None = None
    detail: str = ""
    #: Set when *we* stopped it (the log cap, or ``job_kill``).  A deliberate
    #: stop must not be reported as the command's own ending -- a killed process
    #: still hands back ``-9``, so the fact has to be recorded where it happens
    #: rather than guessed at settle time.
    stop_reason: str = ""
    #: M-D1: the stable reason code when the process sandbox refused to start
    #: this command (``SandboxGap``).  The job never spawned, so no ledger row
    #: is written; the field is what ``bash_tool`` reads to answer
    #: ``sandbox_unavailable`` (A-4: an environment failure, not a refusal).
    denied_reason: str = ""
    #: True when this process found the row on disk instead of starting it.
    adopted: bool = False
    #: The pid's start time as the kernel counts it; the anti-reuse check.
    start_ticks: int | None = None
    #: Set when the pid is alive but its identity could not be checked.
    identity_unverifiable: bool = False
    proc: subprocess.Popen[bytes] | None = None
    settled: bool = False
    #: Set at settlement.  A foreground call waits on this instead of on the
    #: process, which is what makes a promoted command the same object either way.
    done: threading.Event = field(default_factory=threading.Event)
    #: Whether a settlement has to travel anywhere.  A foreground call that
    #: answers inside its wait already put the result in the ToolResult, so its
    #: notice would be the same command twice; the flag flips on at promotion,
    #: which is the moment nobody holds the result any more.
    announce: bool = True
    announced: bool = False
    #: The emitter this job was started with.  Kept because a job outlives the
    #: turn that started it, and the panel has to hear about it settling after
    #: that turn is over -- the same reason sub-agents keep their parent's emit.
    emit: Callable[[Any], None] | None = None

    def view(self) -> dict[str, Any]:
        """What the ledger and the tools both read: the process handle stays out."""
        return {
            "id": self.id,
            "command": self.command,
            "pid": self.pid,
            "log_path": self.log_path,
            "started_at": self.started_at,
            "origin_session": self.origin_session,
            "status": self.status,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
            "detail": self.detail,
            "adopted": self.adopted,
        }


def _process_start_ticks(pid: int) -> int | None:
    """The kernel's start-time counter for ``pid``; ``None`` where unavailable."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    # Field 22 of /proc/<pid>/stat, counting from 1.  The comm field can hold
    # spaces and parentheses, so split after the last ')'.
    _, _, rest = stat.rpartition(")")
    fields = rest.split()
    try:
        return int(fields[19])
    except (IndexError, ValueError):  # pragma: no cover - a kernel we cannot parse
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:  # pragma: no cover - platform without signals
        return False
    return True


class JobRegistry:
    """Every background job in this workspace, whoever started it.

    Lives on the workspace rather than the session: a job survives the process
    (see the module docstring), so the thing that outlives the session has to be
    owned by something that does too.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        notices: Any | None = None,
        policy: ProcessSandboxPolicy | None = None,
        backend: SandboxBackend | None = None,
        roots: Sequence[Path] | Callable[[], Sequence[Path]] = (),
    ) -> None:
        self._root = Path(root)
        self._notices = notices
        # M-D1: the process sandbox.  ``start`` is the one wrap point for a job
        # (``bash_tool`` does not wrap a job-class call), and it shares the
        # backend instance with ``BashTool`` so the two agree on whether the
        # mechanism exists.
        self._policy = policy
        self._backend = backend
        # E.2: the writable roots may be a **live** source (``Sandbox.live_roots``
        # or any callable), read at ``start`` time -- a snapshot would let a
        # directory the file tools may write become one a background job may not.
        self._roots = roots if callable(roots) else tuple(Path(p) for p in roots)
        self._jobs: dict[str, Job] = {}
        # Reentrant on purpose: _publish holds it across a snapshot *and* the
        # emit scan so one event is one atomic view of the table, and snapshot()
        # takes the same lock on the way through.  A plain Lock would deadlock
        # the moment a publish called a reader.
        self._lock = threading.RLock()
        self._rescan()

    # ---- starting ----------------------------------------------------------

    def start(
        self,
        command: str,
        *,
        cwd: str,
        session_id: str,
        emit: Callable[[Any], None] | None = None,
        announce: bool = True,
    ) -> Job:
        """Spawn ``command`` and return immediately; the caller does not wait.

        Raises :class:`OSError` when the process cannot be spawned at all -- no
        ledger row is written for a command that never ran -- and
        :class:`JobLimitReached` when the workspace is already full.

        M-D1: a :class:`SandboxGap` is **not** raised.  The command was refused
        by the process sandbox, so it becomes a settled failed job carrying the
        stable reason code in ``denied_reason`` -- no ledger row, no watcher, no
        process.  ``bash_tool`` reads the field and answers
        ``sandbox_unavailable`` (A-4).
        """
        with self._lock:
            if self._running_count() >= MAX_RUNNING_JOBS:
                raise JobLimitReached(
                    f"background job limit reached for this workspace "
                    f"(limit: {MAX_RUNNING_JOBS}); use job_kill to stop an unneeded "
                    f"job, wait for one to finish, then retry"
                )
        degraded = (
            (lambda reason: emit_degraded(emit, "process_sandbox", reason))
            if emit is not None
            else None
        )
        try:
            roots = self._roots() if callable(self._roots) else self._roots
            argv = spawn_argv(
                _bounded(command), self._policy, self._backend, roots, emit=degraded
            )
        except SandboxGap as exc:
            job = Job(
                id=f"job_{uuid.uuid4().hex[:8]}",
                command=command,
                pid=0,
                log_path="",
                started_at=now_utc(),
                origin_session=session_id,
                status=STATUS_FAILED,
                # A-4: the command never ran because the mechanism is absent;
                # "denied" read as a human decision.  The stable reason code
                # stays in ``denied_reason`` for machines, this line is for the
                # operator (jobs panel / /tasks).
                detail=f"未执行 · {exc.reason}",
                denied_reason=exc.reason,
                settled=True,
                announce=False,
                emit=emit,
            )
            job.done.set()
            return job
        self._root.mkdir(parents=True, exist_ok=True)
        job_id = f"job_{uuid.uuid4().hex[:8]}"
        log_path = self._root / f"{job_id}.log"
        handle = open(log_path, "wb")  # noqa: SIM115 - the child needs the fd open
        try:
            proc = subprocess.Popen(
                argv,
                shell=False,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env={**os.environ, "AI_CODING_JOB": job_id},
                # M-D2 §D.3: the seccomp filter is an inherited descriptor.
                pass_fds=tuple(getattr(self._backend, "seccomp_fds", ())),
            )
        finally:
            handle.close()  # the child holds its own duplicate; ours is done
        job = Job(
            id=job_id,
            command=command,
            pid=proc.pid,
            log_path=str(log_path),
            started_at=now_utc(),
            origin_session=session_id,
            start_ticks=_process_start_ticks(proc.pid),
            proc=proc,
            announce=announce,
            emit=emit,
        )
        with self._lock:
            self._jobs[job_id] = job
        self._write_ledger(job)
        threading.Thread(
            target=self._watch, args=(job, emit), name=f"{job_id}-watch", daemon=True
        ).start()
        self._publish(emit=emit)
        return job

    # ---- the watcher -------------------------------------------------------

    def _watch(self, job: Job, emit: Callable[[Any], None] | None) -> None:
        """Wait for the process, and stop it if its log outgrows the cap.

        A daemon thread on purpose: when this process exits the thread dies with
        it and the command carries on, which is the semantic the user chose.
        """
        proc = job.proc
        assert proc is not None
        while True:
            try:
                proc.wait(timeout=2.0)
                break
            except subprocess.TimeoutExpired:
                pass
            try:
                size = Path(job.log_path).stat().st_size
            except OSError:  # pragma: no cover - the log vanished mid-run
                break
            if size > MAX_JOB_LOG_BYTES:
                _kill_group(proc.pid)
                job.stop_reason = f"日志超过 {MAX_JOB_LOG_BYTES} 字节上限被中止"
                job.detail = job.stop_reason
                try:
                    proc.wait(timeout=_KILL_GRACE_SECONDS)
                except subprocess.TimeoutExpired:  # pragma: no cover - trapped SIGTERM
                    _kill_group(proc.pid)
                    proc.wait(timeout=_KILL_GRACE_SECONDS)
                break
        self._settle(job, emit=emit)

    def _settle(self, job: Job, *, emit: Callable[[Any], None] | None = None) -> None:
        """Record the ending exactly once, then tell the model and the operator.

        Idempotent because two callers can reach it -- the watcher, and a kill
        of an adopted job that nobody is watching -- and one command must produce
        one notice and one toast.
        """
        if job.settled:
            return
        job.settled = True
        code = job.proc.returncode if job.proc is not None else None
        if code is not None:
            job.exit_code = code
        if job.status == STATUS_RUNNING:
            if job.exit_code is not None and abs(job.exit_code) in (
                _SIG_FILE_TOO_BIG,
                128 + _SIG_FILE_TOO_BIG,
            ):
                # The kernel stopped the write, not the command giving up.
                job.stop_reason = f"日志超过 {MAX_JOB_LOG_BYTES} 字节上限被中止"
                job.detail = job.stop_reason
            if job.stop_reason:
                # We stopped it.  A killed process still reports -9, so asking
                # the exit code would dress our decision up as the command's.
                job.status = STATUS_KILLED
                job.detail = job.stop_reason
            elif job.exit_code == 0:
                job.status = STATUS_COMPLETED
                job.detail = job.detail or "exit 0"
            elif job.exit_code is not None:
                # dsh's line: the command ran to completion and said 3.  The job
                # is not an outage, and calling it one teaches the model to
                # apologise for red tests.
                job.status = STATUS_COMPLETED
                job.detail = job.detail or f"exit {job.exit_code}"
            else:
                job.status = STATUS_FAILED
        job.finished_at = now_utc()
        self._write_ledger(job)

        summary = f"{job.command.strip()[:60]} · {job.detail or job.status}"
        if job.announce and not job.announced:
            job.announced = True
            self._announce(job, summary, emit)
        self._publish(emit=emit if emit is not None else job.emit)
        # Last, on purpose: a waiter that wakes on this may discard the row the
        # instant it does.  Setting it before the ledger write let the watcher
        # recreate the file the discard had just removed -- a phantom completed
        # row for a command the foreground call had already consumed (found as
        # a rare full-suite failure, pinned by the settle-before-wake test).
        job.done.set()

    def _announce(self, job: Job, summary: str, emit: Callable[[Any], None] | None) -> None:
        """Put the ending where the model and the operator will both see it."""
        self._notice(job, summary)
        if emit is not None:
            try:
                emit(
                    BackgroundTaskDone(
                        run_id=job.id,
                        name="bash",
                        status=job.status,
                        summary=summary,
                        truncated=len(summary) > 200,
                        origin_session=job.origin_session,
                        ledger_path=job.log_path,
                    )
                )
            except Exception:  # noqa: BLE001 - observation never breaks settlement
                _log.debug("background job event failed for %s", job.id, exc_info=True)

    def _notice(self, job: Job, summary: str) -> None:
        """Hand the ending to the turn that is running, or the next one."""
        if self._notices is None:
            return
        tail = self.read_tail(job, NOTICE_TAIL_LINES)
        line = f"[job {job.id}] {summary} · 日志 {job.log_path}"
        if tail:
            line += "\n" + tail
        try:
            # wake=True: an idle session should look at this on its own (D-1c).
            self._notices.put(line, wake=True)
        except Exception:  # noqa: BLE001 - a settlement already recorded must stand
            _log.debug("background job notice failed for %s", job.id, exc_info=True)

    # ---- reading -----------------------------------------------------------

    def snapshot(self) -> list[dict[str, Any]]:
        """The rows a panel renders: what ``job_list`` prints, minus the prose."""
        return [
            {
                "id": job.id,
                "command": job.command,
                "status": job.status,
                "pid": job.pid,
                "log_path": job.log_path,
                "detail": job.detail,
                "originSession": job.origin_session,
                "adopted": job.adopted,
                "startedAt": job.started_at,
            }
            for job in self.list()
        ]

    def _publish(self, *, emit: Callable[[Any], None] | None = None) -> None:
        """Tell whoever is watching the panel.  Best-effort like every event here:
        a failure to render must not stop a command.

        The sink travels as an argument, never read out of the live table: the
        waiter discards a foreground row between ``done.set()`` and this call,
        so a lookup here would find an empty table exactly when there is the
        most to say.  Callers without a sink to name fall back to whichever
        turn's sink the table still holds.  One critical section for the read
        and the scan, or the event could name a job that arrived after the rows
        were copied.
        """
        with self._lock:
            rows = tuple(self.snapshot())
            if emit is None:
                emit = next(
                    (job.emit for job in self._jobs.values() if job.emit is not None),
                    None,
                )
        if emit is None:
            return
        try:
            emit(JobChanged(jobs=rows))
        except Exception:  # noqa: BLE001 — observation never breaks a job
            _log.debug("job.changed emit failed", exc_info=True)

    def get(self, job_id: str) -> Job | None:
        """One job by id, or ``None`` when this workspace has no such job."""
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        """Running first (oldest first), then settled newest first."""
        with self._lock:
            jobs = list(self._jobs.values())
        running = sorted(
            (job for job in jobs if job.status == STATUS_RUNNING), key=lambda j: j.started_at
        )
        settled = sorted(
            (job for job in jobs if job.status != STATUS_RUNNING),
            key=lambda j: j.finished_at or j.started_at,
            reverse=True,
        )
        return running + settled

    def read_tail(self, job: Job, lines: int) -> str:
        """The last few lines of a log, without reading the whole file.

        A build log can be tens of megabytes; seeking to the end is the only way
        to show a model what just happened without handing it the file.
        """
        try:
            with open(job.log_path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                window = min(size, 64 * 1024)
                handle.seek(size - window)
                blob = handle.read(window)
        except OSError:
            return ""
        tail = blob.decode("utf-8", errors="replace").splitlines()[-max(1, lines) :]
        return "\n".join(line[:_TAIL_LINE_CHARS] for line in tail)

    # ---- stopping ----------------------------------------------------------

    def kill(self, job_id: str, *, reason: str) -> Job:
        """Stop a running job and say why.

        Idempotent in the way a caller needs: killing a job that already ended
        returns its real ending instead of claiming a kill that did not happen.
        """
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job.status != STATUS_RUNNING:
            return job
        if job.adopted and not self._identity_matches(job):
            if job.identity_unverifiable:
                raise JobIdentityUnverifiable(
                    f"job {job_id} was started by an earlier run and this platform "
                    f"cannot verify that pid {job.pid} is still it; refusing to "
                    f"signal a process we cannot prove we own"
                )
        if not _pid_alive(job.pid):
            job.status = STATUS_UNKNOWN
            job.detail = "进程已不在（退出码未知）"
            job.finished_at = now_utc()
            self._write_ledger(job)
            return job
        # Record the decision *before* the signal.  The watcher thread settles
        # this same process, and it can wake the moment the process dies -- a
        # stop_reason written after the signal arrives is a race, and the loser
        # reports a kill we caused as the command's own "exit -15".
        job.stop_reason = f"已停止：{reason}" if reason else "已停止"
        job.detail = job.stop_reason
        _kill_group(job.pid, signal.SIGTERM)
        if job.proc is not None:
            try:
                job.proc.wait(timeout=_KILL_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                _kill_group(job.pid)
                try:
                    job.proc.wait(timeout=_KILL_GRACE_SECONDS)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    _log.warning("job %s survived SIGKILL", job_id)
        else:
            _wait_gone(job.pid, _KILL_GRACE_SECONDS)
            if _pid_alive(job.pid):  # pragma: no cover - a trapped SIGTERM
                _kill_group(job.pid)
        # Settle here rather than leaving it to the watcher.  ``kill`` has just
        # waited for the process to go, so everything the settlement needs is
        # known; returning before the row said ``killed`` meant the tool could
        # tell the model "stopped" while the ledger still read ``running`` and
        # the notice had not been queued yet.  The idempotence guard makes the
        # watcher's own later call a no-op, so nobody speaks twice.
        self._settle(job)
        return job

    async def wait(
        self,
        job: Job,
        *,
        timeout: float,
        cancel: threading.Event | None = None,
        kill_on_cancel: bool = True,
        tick: float = _WAIT_TICK,
    ) -> str:
        """Wait for ``job``: :data:`WAIT_SETTLED`, :data:`WAIT_TIMEOUT` or
        :data:`WAIT_CANCELLED`.

        A wait that times out does not stop anything -- that is the promotion
        path.  ``cancel`` is the dispatcher's Esc flag, polled on the same tick
        the subprocess runner uses, so the two paths agree on when they notice.

        ``kill_on_cancel`` separates the two callers.  A foreground bash call
        owns its command and Esc is meant to stop it; ``job_output`` only asked
        to wait, and a job belongs to the workspace, so Esc there stops the
        waiting and leaves the command alone.
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if job.done.is_set():
                return WAIT_SETTLED
            if remaining <= 0:
                return WAIT_TIMEOUT
            if cancel is not None and cancel.is_set():
                if kill_on_cancel:
                    self.kill(job.id, reason="cancelled by the user")
                return WAIT_CANCELLED
            if job.done.wait(timeout=min(tick, remaining)):
                return WAIT_SETTLED
            await asyncio.sleep(0)

    def announce(self, job_id: str, *, emit: Callable[[Any], None] | None = None) -> None:
        """Start announcing ``job_id``: from now on nobody holds its result.

        Called at promotion.  A job that settled in the instant between the wait
        timing out and this call would otherwise end up announced to nobody --
        the flag is checked once, so the flip has to close that window itself.
        """
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None or job.announced:
            return
        job.announce = True
        if job.settled:
            job.announced = True
            self._announce(job, f"{job.command.strip()[:60]} · {job.detail or job.status}", emit)
            self._write_ledger(job)

    def discard(self, job_id: str) -> None:
        """Forget a job whose result the caller already has, log and all.

        This is the foreground path's exit: a command that finished inside its
        own wait leaves nothing behind, exactly as it left nothing behind before
        jobs existed.  dsh does the same when a foreground call settles before
        the wait ends (``registry.remove``).
        """
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if job is None:
            return
        for path in (job.log_path, self._root / f"{job.id}.json"):
            try:
                Path(path).unlink()
            except OSError:
                pass
        self._publish(emit=job.emit)

    def read_head(self, job: Job, limit: int) -> tuple[str, int]:
        """The first ``limit`` characters of a log, and its real length.

        Head, not tail: that is what the subprocess runner returned, and a build
        log's head is where the error usually is.
        """
        try:
            size = Path(job.log_path).stat().st_size
            with open(job.log_path, "rb") as handle:
                blob = handle.read(limit + 1)
        except OSError:
            return "", 0
        text = blob[:limit].decode("utf-8", errors="replace")
        return text, size

    def forget(self, job_id: str) -> None:
        """Drop an in-process handle without touching the process.

        Only used by the tests to stand in for interpreter exit; production has
        no reason to forget a job it is still running.
        """
        with self._lock:
            self._jobs.pop(job_id, None)

    # ---- the other process -------------------------------------------------

    def _rescan(self) -> None:
        """Pick up rows a previous run left behind.

        A row saying ``running`` is a claim about a pid, and a pid outlives its
        meaning.  It is checked against the kernel's start time where that is
        knowable, and treated as unknown -- never as killable -- where it is not.
        """
        if not self._root.is_dir():
            return
        for path in sorted(self._root.glob("job_*.json")):
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # Crash-safe writes make this unlikely; a hand-edited file must
                # still not keep the product from starting.
                _log.warning("ignoring unreadable job ledger %s", path.name)
                continue
            job = Job(
                id=str(row.get("id", path.stem)),
                command=str(row.get("command", "")),
                pid=int(row.get("pid", 0)),
                log_path=str(row.get("log_path", path.with_suffix(".log").name)),
                started_at=str(row.get("started_at", "")),
                origin_session=str(row.get("origin_session", "")),
                status=str(row.get("status", STATUS_UNKNOWN)),
                finished_at=str(row.get("finished_at", "")),
                exit_code=row.get("exit_code"),
                detail=str(row.get("detail", "")),
                start_ticks=row.get("start_ticks"),
            )
            if job.status == STATUS_RUNNING:
                if not _pid_alive(job.pid):
                    job.status = STATUS_UNKNOWN
                    job.detail = "进程已不在（退出码未知）"
                elif job.start_ticks is None or _process_start_ticks(job.pid) != job.start_ticks:
                    job.identity_unverifiable = True
                    if _process_start_ticks(job.pid) is not None:
                        # The kernel answered, and the answer is not our pid: it
                        # was reused.  Do not present it as running.
                        job.status = STATUS_UNKNOWN
                        job.detail = "该 pid 已被别的进程占用"
            job.adopted = True
            job.settled = job.status != STATUS_RUNNING
            with self._lock:
                self._jobs[job.id] = job

    def _identity_matches(self, job: Job) -> bool:
        if job.start_ticks is None:
            job.identity_unverifiable = True
            return False
        return _process_start_ticks(job.pid) == job.start_ticks

    # ---- plumbing ----------------------------------------------------------

    def _running_count(self) -> int:
        return sum(1 for job in self._jobs.values() if job.status == STATUS_RUNNING)

    def _write_ledger(self, job: Job) -> None:
        """One file per job, replaced atomically: a reader never sees half a row."""
        payload = json.dumps(job.view() | {"start_ticks": job.start_ticks}, ensure_ascii=False)
        temp = self._root / f".{job.id}.tmp"
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            temp.write_text(payload, encoding="utf-8")
            os.replace(temp, self._root / f"{job.id}.json")
        except OSError:
            # Best-effort, like every other artifact this project writes: losing
            # the row must not take the command down with it.
            _log.debug("could not write the ledger for %s", job.id, exc_info=True)


def _bounded(command: str) -> str:
    """``command`` with a kernel-enforced ceiling on the log it may write.

    No ``exec``: it only takes a simple command, and ``for`` loops / ``&&``
    chains are exactly what a long-running job looks like.  So the tracked pid
    is the shell that sets the limit, and a group kill still reaches the command
    underneath it.  The exit status the shell reports is the command's.
    """
    blocks = max(1, MAX_JOB_LOG_BYTES // _ULIMIT_BLOCK)
    return f"ulimit -f {blocks}; {command}"


def _kill_group(pid: int, sig: int = signal.SIGKILL) -> None:
    """Signal the whole group -- a build spawns children that must die too."""
    try:
        os.killpg(os.getpgid(pid), sig)
        return
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        os.kill(pid, sig)
    except OSError:  # pragma: no cover - already gone
        pass


def _wait_gone(pid: int, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(0.05)
