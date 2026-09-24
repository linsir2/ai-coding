"""Bounded subprocess execution (R2b).

The project used to call ``asyncio.create_subprocess_*`` and await
``proc.communicate()``.  Two things were wrong with that:

* its timeout was **unreachable** whenever another process still held the pipe —
  ``asyncio.wait_for`` waits for the cancellation to finish before it raises
  (docs/r2-analysis.md 1.4-A);
* the completion signal (SIGCHLD, delivered through the event loop's self-pipe)
  can be dropped by the environment, in which case the loop only notices at the
  next timer (1.3).

A synchronous ``communicate(timeout=...)`` inside a worker thread has neither
problem.  ``start_new_session=True`` puts the child in its own process group, so
on timeout ``killpg`` reaches grandchildren too (``sleep 30 & sleep 30``), and
the thread always returns — even if something escaped the group and still holds
the pipes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ai_coding.infra.deadline import HEARTBEAT_SECONDS, DeadlineExpired, run_with_deadline

__all__ = ["DEFAULT_KILL_GRACE", "ProcessOutcome", "run_process", "run_process_async"]

DEFAULT_KILL_GRACE = 5.0

THREAD_NAME = "ai-coding-subprocess"

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProcessOutcome:
    """Everything a caller needs to know about one finished (or killed) process."""

    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    error: str | None = None  # the process could not be started at all
    duration_seconds: float = 0.0


def _truncate(text: str, limit: int | None, total: int) -> str:
    if limit is None or len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, total {total} chars]"


def _decode(raw: bytes | str | None) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    """SIGKILL the child's process group, falling back to the child alone."""
    if hasattr(os, "killpg"):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.kill()
    except OSError:  # pragma: no cover - the process is already gone
        pass


def _close_pipes(proc: subprocess.Popen[bytes]) -> None:
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:  # pragma: no cover - closing is best-effort
                pass


def run_process(
    command: Sequence[str] | str,
    *,
    shell: bool = False,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    stdin_data: bytes | str | None = None,
    timeout: float = 60.0,
    kill_grace: float = DEFAULT_KILL_GRACE,
    max_output_chars: int | None = None,
) -> ProcessOutcome:
    """Run ``command`` with a hard timeout.  Never raises for process failures.

    ``stdin_data`` is fed to the child and its stdin is then closed.  R4c needs it
    for command hooks, which receive their context as one JSON line; everything
    else keeps the old behaviour (stdin closed immediately).
    """
    started = time.monotonic()
    payload: bytes | None = None
    if stdin_data is not None:
        payload = stdin_data.encode("utf-8") if isinstance(stdin_data, str) else stdin_data

    def _elapsed() -> float:
        return time.monotonic() - started

    try:
        proc = subprocess.Popen(
            command,
            shell=shell,
            cwd=cwd,
            env=None if env is None else dict(env),
            stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        return ProcessOutcome(
            returncode=None,
            error=f"{type(exc).__name__}: {exc}",
            duration_seconds=_elapsed(),
        )

    timed_out = False
    try:
        stdout_bytes, stderr_bytes = proc.communicate(input=payload, timeout=timeout)
        returncode: int | None = proc.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        _kill_group(proc)
        try:
            stdout_bytes, stderr_bytes = proc.communicate(input=payload, timeout=kill_grace)
        except subprocess.TimeoutExpired:
            # Something escaped the process group and still holds the pipes.
            # Abandon them: the thread must return, and partial output still helps.
            _log.debug("process %s survived killpg; abandoning its pipes", proc.pid)
            _close_pipes(proc)
            stdout_bytes = getattr(exc, "output", None) or b""
            stderr_bytes = getattr(exc, "stderr", None) or b""
        returncode = None

    stdout = _decode(stdout_bytes)
    stderr = _decode(stderr_bytes)
    return ProcessOutcome(
        returncode=returncode,
        stdout=_truncate(stdout, max_output_chars, len(stdout)),
        stderr=_truncate(stderr, max_output_chars, len(stderr)),
        timed_out=timed_out,
        duration_seconds=_elapsed(),
    )


async def run_process_async(
    command: Sequence[str] | str,
    *,
    shell: bool = False,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    stdin_data: bytes | str | None = None,
    timeout: float = 60.0,
    kill_grace: float = DEFAULT_KILL_GRACE,
    max_output_chars: int | None = None,
    tick: float = HEARTBEAT_SECONDS,
) -> ProcessOutcome:
    """Async wrapper: the work happens in a daemon thread under a hard deadline.

    Deliberately **not** ``asyncio.to_thread``: that would create the loop's
    shared default executor, and ``loop.shutdown_default_executor()`` awaits a
    future resolved from a helper thread — a cross-thread wakeup, which is the
    very thing an environment can drop (docs/r2-analysis.md 1.3).  A private
    daemon thread has no such coupling with the loop, so a stuck process can
    never block loop (or interpreter) shutdown, and a leaked thread cannot keep
    the process alive.
    """
    # The outer bound is strictly larger than the inner one, so the inner
    # (killpg-backed) timeout is what normally fires and the outer is a backstop.
    budget = timeout + kill_grace + 1.0
    kwargs: dict[str, Any] = {
        "command": command,
        "shell": shell,
        "cwd": cwd,
        "env": env,
        "stdin_data": stdin_data,
        "timeout": timeout,
        "kill_grace": kill_grace,
        "max_output_chars": max_output_chars,
    }
    try:
        return await run_with_deadline(
            _start_process_thread(kwargs),
            timeout=budget,
            label="process",
            tick=tick,
        )
    except DeadlineExpired:
        _log.warning("process runner exceeded its outer deadline; thread abandoned")
        return ProcessOutcome(
            returncode=None,
            timed_out=True,
            error="abandoned by the outer deadline",
        )


def _start_process_thread(kwargs: dict[str, Any]) -> asyncio.Future[ProcessOutcome]:
    """Run :func:`run_process` in a daemon thread and deliver it to the loop."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future[ProcessOutcome] = loop.create_future()

    def _deliver(exc: BaseException | None, value: ProcessOutcome | None) -> None:
        if future.done():
            return
        if exc is not None:
            future.set_exception(exc)
        else:
            future.set_result(value)  # type: ignore[arg-type]

    def _target() -> None:
        try:
            outcome = run_process(**kwargs)
        except BaseException as exc:  # noqa: BLE001 — delivered to the awaiting caller
            _deliver_from_thread(loop, _deliver, exc, None)
        else:
            _deliver_from_thread(loop, _deliver, None, outcome)

    threading.Thread(target=_target, name=THREAD_NAME, daemon=True).start()
    return future


def _deliver_from_thread(
    loop: asyncio.AbstractEventLoop,
    callback: Callable[[BaseException | None, ProcessOutcome | None], None],
    exc: BaseException | None,
    value: ProcessOutcome | None,
) -> None:
    """Hand the result back to the loop; a closed loop means nobody is waiting."""
    try:
        loop.call_soon_threadsafe(callback, exc, value)
    except RuntimeError:  # pragma: no cover - the loop is already gone
        return
