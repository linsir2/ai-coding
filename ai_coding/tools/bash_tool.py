"""Bash tool — run shell commands with a hard timeout and output truncation.

R2b: execution goes through :mod:`ai_coding.infra.subprocess_runner` (thread +
process-group kill) instead of ``asyncio.create_subprocess_shell``, whose
timeout could never fire while a child held the pipe (docs/r2-analysis.md 1.4-A).
The command runs in ``ctx.workspace`` — the description always promised that,
but the old implementation silently inherited the process CWD, which would send
a sub-agent's shell writes into the parent's tree.
"""

from __future__ import annotations

import asyncio
import itertools
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from ai_coding.core.events import ToolChunk
from ai_coding.core.jobs import WAIT_CANCELLED, WAIT_SETTLED
from ai_coding.core.turn_report import emit_degraded
from ai_coding.domain.message import ToolResult
from ai_coding.infra.subprocess_runner import run_process_async
from ai_coding.infra.text_clip import clip_head_tail
from ai_coding.security.process_sandbox import (
    ProcessSandboxPolicy,
    SandboxBackend,
    SandboxGap,
    spawn_argv,
)
from ai_coding.security.sandbox import Sandbox
from ai_coding.tools.base import BaseTool, ToolContext, resolve_ctx

_DEFAULT_TIMEOUT = 60
_DEFAULT_MAX_OUTPUT = 50_000
#: A-4: the mechanism could not run here (no bwrap / no namespaces).  It is an
#: environment failure, not a human or gate refusal -- "denied" read as "someone
#: said no", and the UI drew it struck through as if permission had been refused.
SANDBOX_UNAVAILABLE = "sandbox_unavailable"
# Per-call widening ceiling: the model may raise a slow command's timeout up to
# this (see `timeout_seconds` in the schema), but never past it.  It lines up
# with the turn wall clock (`ai.turn_timeout_seconds`, 600) and the sub-agent
# ceiling, so one call can never eat more than a turn.  The dispatcher still
# takes min(tool ceiling, turn remaining), so this is a cap, not a promise.
_MAX_TIMEOUT_SECONDS = 600
# The dispatcher's per-call ceiling sits above the command timeout so the runner
# (not the budget watchdog) is what normally stops a runaway command.
_TIMEOUT_GRACE = 15.0


class _LogTail:
    """Replays a running job's log as ``tool.chunk`` events.

    R8c's live output used to come from the subprocess runner reading pipes. A
    job writes to a file instead, so without this the TUI would show nothing
    until the command finished -- the one thing the stream exists to prevent.

    The stream label is "stdout" because a job merges both streams into one
    file; the frontend renders that as ordinary output, which is what a merged
    log is.
    """

    def __init__(self, job: Any, emit: Any, call_id: str) -> None:
        self._job = job
        self._emit = emit
        self._call_id = call_id
        self._offset = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name=f"{self._job.id}-tail", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def _run(self) -> None:
        seq = itertools.count()
        while not self._stop.is_set():
            try:
                with open(self._job.log_path, "rb") as handle:
                    handle.seek(self._offset)
                    blob = handle.read(65536)
            except OSError:
                blob = b""
            if blob:
                self._offset += len(blob)
                self._publish(blob.decode("utf-8", errors="replace"), next(seq))
            else:
                self._stop.wait(0.2)

    def _publish(self, text: str, seq: int) -> None:
        from ai_coding.core.events import ToolChunk

        try:
            self._emit(
                ToolChunk(
                    call_id=self._call_id,
                    tool_name="bash",
                    stream="stdout",
                    text=text,
                    seq=seq,
                )
            )
        except Exception:  # noqa: BLE001 - observation never breaks the command
            pass


class BashTool(BaseTool):
    """Execute shell commands and return stdout+stderr as output."""

    def __init__(
        self,
        timeout_seconds: int = _DEFAULT_TIMEOUT,
        max_output_chars: int = _DEFAULT_MAX_OUTPUT,
        max_timeout_seconds: int = _MAX_TIMEOUT_SECONDS,
        *,
        policy: ProcessSandboxPolicy | None = None,
        backend: SandboxBackend | None = None,
        roots: Sequence[Path] | Sandbox | Callable[[], Sequence[Path]] = (),
    ) -> None:
        self._max_timeout = max(1, int(max_timeout_seconds))
        # The default can never exceed the ceiling: two yardsticks for one
        # command would teach the model that limits are negotiable prose.
        self._timeout = max(1, min(int(timeout_seconds), self._max_timeout))
        self._max_output = max_output_chars
        # M-D1: policy and mechanism are separate, and both default to "no
        # opinion" so a stack built without them behaves exactly as before.
        # The composition root passes the real pair; a job-class call does NOT
        # wrap here -- ``JobRegistry.start`` is the one wrap point (double
        # wrapping would mean two layers of bwrap).
        self._policy = policy
        self._backend = backend
        # E.1: the writable roots are a **live** source (a Sandbox, or a
        # callable such as ``Sandbox.live_roots``), read on every call -- a
        # snapshot would let a directory the file tools may write become one a
        # bash command may not.  ``_roots`` stays as the snapshot view for
        # assertions and debugging (tests read it directly).
        self._roots_source = roots
        self._roots = self._snapshot_roots()

    def _snapshot_roots(self) -> tuple[Path, ...]:
        """The writable roots as they are **now**, from the live source."""
        source = self._roots_source
        if isinstance(source, Sandbox):
            return source.live_roots()
        if callable(source):
            return tuple(source())
        return tuple(Path(root) for root in source)

    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        return (
            "Run a shell command and return its output. "
            "The command runs in the workspace directory. "
            f"Timeout is {self._timeout}s by default; pass timeout_seconds "
            f"(up to {self._max_timeout}s) to widen it for one slow call. "
            f"Output is truncated after {self._max_output} characters. "
            "A command that outlives the turn should go to the background "
            "(run_in_background), where you are notified when it finishes -- "
            "do not poll it and do not sleep waiting for it."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute.",
                },
                # The self-widening knob: when a long command times out, the
                # model retries with a larger value instead of wrapping the
                # command in `timeout(1)` (whose margin it used to guess from
                # the description above, and guess short).  A new value is a
                # new guard fingerprint, so widening retries are not denied as
                # duplicate calls.
                "run_in_background": {
                    "type": "boolean",
                    "description": (
                        "Run the command in the background and return "
                        "immediately -- for builds, test suites and dev servers "
                        "that outlive a turn. You do not need to end the command "
                        "with '&'. You will be notified when it finishes; do not "
                        "poll and do not sleep waiting for it, and do not start a "
                        "second copy of a job that is already running. "
                        "timeout_seconds does not apply to a background command; "
                        "job_list shows what is running and job_kill stops it."
                    ),
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": (
                        "Per-call timeout in seconds "
                        f"(1..{self._max_timeout}). Defaults to "
                        f"{self._timeout}. Set higher for long commands "
                        "(full test suites, builds); if a run times out, "
                        "retry with a larger value instead of wrapping the "
                        "command in `timeout(1)`."
                    ),
                },
            },
            "required": ["command"],
        }

    def _start_background(
        self, command: str, params: dict[str, Any], ctx: ToolContext | None
    ) -> ToolResult:
        """Hand the command to the job table and return its id at once.

        Falls back to the foreground path when no registry is attached (the bare
        CLI, a sub-agent stack): running it in the foreground and saying so is
        better than pretending the switch worked.
        """
        context = resolve_ctx(ctx)
        registry = getattr(context, "jobs", None)
        if registry is None:
            return ToolResult(
                success=False,
                output=(
                    "error: run_in_background is not available here (no job "
                    "registry on this tool stack); run the command in the "
                    "foreground, or set a larger timeout_seconds"
                ),
                error="no_jobs",
            )
        from ai_coding.core.jobs import JobLimitReached

        try:
            job = registry.start(
                command,
                cwd=context.workspace or ".",
                session_id=context.session_id,
                emit=context.emit,
            )
        except JobLimitReached as exc:
            return ToolResult(success=False, output=f"error: {exc}", error="job_limit")
        denied = getattr(job, "denied_reason", "")
        if denied:
            return ToolResult(
                success=False,
                output=f"error: {denied}",
                error=SANDBOX_UNAVAILABLE,
                data={"reason": denied},
            )
        return ToolResult(
            success=True,
            output=(
                f"job {job.id} started in the background (pid {job.pid}). "
                f"You will be notified when it finishes -- do not poll and do "
                f"not sleep waiting for it; keep working on independent steps. "
                f"Its output streams to: {job.log_path}"
            ),
            data={"job_id": job.id, "pid": job.pid, "log": job.log_path},
        )

    async def _run_as_job(
        self,
        command: str,
        effective: int,
        ctx: ToolContext | None,
        registry: Any,
    ) -> ToolResult:
        """One command as one job, waited on for ``effective`` seconds.

        Three endings, and each one says which it is:

        * settled in time -- projected into the same ToolResult shape the
          foreground path has always produced, then discarded: a command that
          answered does not leave a job behind;
        * the wait ran out -- **promoted**. The command keeps running and the
          model gets the id. dsh's default (``promoteOnTimeout``); the older
          text here told the model to retry with a larger timeout, which after
          this change would send it around the same build again;
        * the user cancelled -- killed and reported as cancelled, which the
          pipe-based path never managed to say (it reported "exited with code
          None").
        """
        context = resolve_ctx(ctx)
        job = registry.start(
            command,
            cwd=self._workspace_dir(ctx) or ".",
            session_id=context.session_id,
            emit=context.emit,
            # Silent while the caller is still waiting: the ToolResult carries
            # the outcome.  Promotion turns it on, because from that moment the
            # result has nobody but a notice to reach the model through.
            announce=False,
        )
        denied = getattr(job, "denied_reason", "")
        if denied:
            return ToolResult(
                success=False,
                output=f"error: {denied}",
                error=SANDBOX_UNAVAILABLE,
                data={"reason": denied},
            )
        pump = _LogTail(job, context.emit, context.call_id)
        pump.start()
        started = time.monotonic()
        try:
            outcome = await registry.wait(job, timeout=effective, cancel=context.kill_event)
        except asyncio.CancelledError:
            # Esc abandons the awaiting task as well as setting the flag; the
            # command has to go with it, or a promoted job outlives the turn the
            # user cancelled.
            registry.kill(job.id, reason="cancelled by the user")
            pump.stop()
            raise
        duration = time.monotonic() - started
        text, size = registry.read_head(job, self._max_output)
        # Cancelled counts as settled: the job is already killed and settled, and
        # reporting a timeout here would promote a dead command.
        if outcome in (WAIT_SETTLED, WAIT_CANCELLED):
            pump.stop()
            # The result is in hand and the model's answer is about to be built;
            # a record of a command that already answered would be litter.
            registry.discard(job.id)
            return self._settled_result(job, text, size, effective, duration)
        # Promotion: the pump keeps reading, because the model may still be
        # watching this command in the UI when it finally ends.
        registry.announce(job.id, emit=context.emit)
        return ToolResult(
            success=True,
            output=(
                f"still running after {effective}s as job {job.id} "
                f"(pid {job.pid}); it was NOT killed. Its output so far is at "
                f"{job.log_path} and keeps growing. You will be notified when "
                f"it finishes -- do not poll, do not sleep, and do not start it "
                f"again; carry on with independent work or stop it with job_kill."
            ),
            data={
                "promoted": True,
                "job_id": job.id,
                "pid": job.pid,
                "log": job.log_path,
                "timeout_seconds": effective,
                "duration_seconds": round(duration, 3),
            },
        )

    def _settled_result(
        self,
        job: Any,
        text: str,
        size: int,
        effective: int,
        duration: float,
    ) -> ToolResult:
        """Project a finished job into the ToolResult the model has always seen."""
        output = text
        if size > self._max_output:
            output = output + f"\n... [truncated, total {size} chars]"
        data: dict[str, Any] = {
            "exit_code": job.exit_code,
            "timed_out": False,
            "duration_seconds": round(duration, 3),
            "timeout_seconds": effective,
        }
        if job.stop_reason:
            cancelled = "cancelled" in job.stop_reason or "停止" in job.stop_reason
            return ToolResult(
                success=False,
                output=f"{job.stop_reason}\n{output}" if output else job.stop_reason,
                error="cancelled" if cancelled else "killed",
                data=data,
            )
        if job.exit_code == 0:
            return ToolResult(success=True, output=output, data=data)
        return ToolResult(
            success=False,
            output=output or f"command exited with code {job.exit_code}",
            error="nonzero_exit",
            data=data,
        )

    @property
    def max_duration_seconds(self) -> float:
        """Per-call ceiling for the dispatcher (R2a): the *widest* timeout the
        tool will honour, plus grace — not the default.  Otherwise a call the
        model legitimately widened would be killed by the dispatcher first and
        the knob would be a lie."""
        return self._max_timeout + _TIMEOUT_GRACE

    async def execute(
        self, params: dict[str, Any], ctx: ToolContext | None = None
    ) -> ToolResult:
        command = str(params.get("command", ""))
        if not command.strip():
            return ToolResult(success=False, output="error: empty command")

        # D-1: the same command without the waiting. Everything below this branch
        # is the foreground path, untouched.
        if params.get("run_in_background") is True:
            return self._start_background(command, params, ctx)

        # Self-widening: the model names its own budget per call.  Strict on
        # purpose — a bool, a string or a non-positive number is a contract
        # misunderstanding, and silently accepting it would teach the model
        # that the knob is decorative.
        requested = params.get("timeout_seconds", None)
        if requested is None:
            effective = self._timeout
        elif (
            isinstance(requested, bool)
            or not isinstance(requested, int)
            or requested < 1
        ):
            return ToolResult(
                success=False,
                output=(
                    f"error: invalid timeout_seconds={requested!r}; "
                    f"pass an integer number of seconds (1..{self._max_timeout})"
                ),
                error="invalid_params",
                data={"timeout_seconds": requested},
            )
        else:
            effective = min(requested, self._max_timeout)

        # D-1b: with a registry, every call is a job and this one waits on its
        # own -- dsh's shape, and the only shape in which a command that outruns
        # its timeout is still running afterwards.  Without one the pipe-based
        # runner below stays, which is the seam dsh keeps too ("Without a
        # registry the tool is foreground-only").
        registry = getattr(resolve_ctx(ctx), "jobs", None)
        if registry is not None:
            return await self._run_as_job(command, effective, ctx, registry)

        # R8c: stream what the command says while it says it.  Chunks are
        # observation only - the ToolResult below is what the model ever sees.
        counter = itertools.count()
        live = resolve_ctx(ctx)

        def _chunk(stream: str, text: str) -> None:
            live.emit(
                ToolChunk(
                    call_id=live.call_id,
                    tool_name="bash",
                    stream=stream,
                    text=text,
                    seq=next(counter),
                )
            )

        # M-D1: the command becomes an argv through the one shared helper, and
        # the spawn is ``shell=False``.  A SandboxGap is *sandbox_unavailable*
        # (A-4), not ``denied`` and not a tool error: letting it reach the
        # dispatcher's ``except Exception`` would report ``tool_error`` and lose
        # the reason code, and "denied" said a human had refused the call.
        try:
            argv = spawn_argv(
                command,
                self._policy,
                self._backend,
                self._snapshot_roots(),
                emit=lambda reason: emit_degraded(live.emit, "process_sandbox", reason),
            )
        except SandboxGap as exc:
            return ToolResult(
                success=False,
                output=f"error: {exc.reason}",
                error=SANDBOX_UNAVAILABLE,
                data={"reason": exc.reason},
            )

        outcome = await run_process_async(
            argv,
            shell=False,
            cwd=self._workspace_dir(ctx),
            timeout=effective,
            max_output_chars=self._max_output,
            on_chunk=_chunk,
            # R8c: when the user cancels, the dispatcher sets this flag and the
            # runner kills the process group - waiting is not enough (D-10).
            cancel=live.kill_event,
            # M-D2 §D.3: the seccomp filter is an inherited descriptor.
            pass_fds=getattr(self._backend, "seccomp_fds", ()),
        )

        if outcome.error is not None:
            return ToolResult(
                success=False,
                output=f"failed to start command: {outcome.error}",
                error="spawn_failed",
                data={
                    "duration_seconds": round(outcome.duration_seconds, 3),
                    "timeout_seconds": effective,
                },
            )

        output = (outcome.stdout + outcome.stderr) if outcome.stderr else outcome.stdout
        data: dict[str, Any] = {
            "exit_code": outcome.returncode,
            "timed_out": outcome.timed_out,
            "duration_seconds": round(outcome.duration_seconds, 3),
            # Observability for the widening loop (and its tests): what budget
            # this call actually ran under.  Additive only — the error codes
            # and every other key are untouched.
            "timeout_seconds": effective,
        }

        if outcome.timed_out:
            # Partial output is kept: it often says where the command got stuck.
            # The note teaches the exit: widen and retry while headroom
            # remains, split once the ceiling is reached.  This text is the
            # loop's only way out, so it names both the knob and the number — a
            # bare "timed out" is what the model used to poll against until the
            # iteration budget died.  It used to end with "run it in the
            # background and poll", which this harness cannot do: there is no
            # background job, no job id, and nothing to poll.  Advice the model
            # cannot follow is how a stall turns into a budget death (Q2).
            if effective < self._max_timeout:
                note = (
                    f"timeout: command exceeded {effective}s limit; retry with "
                    f"a larger timeout_seconds (up to {self._max_timeout}s)"
                )
            else:
                note = (
                    f"timeout: command exceeded {effective}s limit (already at "
                    f"the {self._max_timeout}s ceiling); split the command into "
                    "smaller steps or narrow its scope — there is no "
                    "background-job mechanism here to run it in and poll"
                )
            return ToolResult(
                success=False,
                output=f"{note}\n{output}" if output else note,
                error="timeout",
                data=data,
            )

        if len(output) > self._max_output:
            # Both ends: the head identifies what ran, the tail carries the
            # verdict (a build's last lines, pytest's ``FAILED`` list).  A
            # head-only clip is how the model came to see "0 errors" from a
            # command whose error was in the last 20 lines (eval-08).
            output = clip_head_tail(
                output, self._max_output, note=f"命令共输出 {len(output)} 字符"
            )

        if outcome.returncode == 0:
            return ToolResult(success=True, output=output, data=data)
        return ToolResult(
            success=False,
            output=output or f"command exited with code {outcome.returncode}",
            error="nonzero_exit",
            data=data,
        )

    @staticmethod
    def _workspace_dir(ctx: ToolContext | None) -> str | None:
        """``ctx.workspace`` when it is a real directory, else inherit the CWD."""
        workspace = resolve_ctx(ctx).workspace
        if workspace and Path(workspace).is_dir():
            return workspace
        return None


__all__ = ["BashTool"]
