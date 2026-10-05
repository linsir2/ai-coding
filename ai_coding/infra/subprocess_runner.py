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
    #: R8c: the user cancelled this run (Esc).  Not a timeout: the clock had time.
    cancelled: bool = False
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
    on_chunk: Callable[[str, str], None] | None = None,
    cancel: threading.Event | None = None,
) -> ProcessOutcome:
    """Run ``command`` with a hard timeout.  Never raises for process failures.

    ``stdin_data`` is fed to the child and its stdin is then closed.  R4c needs it
    for command hooks, which receive their context as one JSON line; everything
    else keeps the old behaviour (stdin closed immediately).

    R8c: ``on_chunk(stream, text)`` is called from the reader threads as output
    arrives, while the command is still running.  It is observation only - the
    returned :class:`ProcessOutcome` is the same either way, a raising callback is
    swallowed, and nothing here changes the request/history.

    ``cancel`` (a :class:`threading.Event`, set by the dispatcher when the user
    hits Esc) kills the process group and returns early, so cancelling stops the
    command instead of merely stopping the wait for it.
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

    # R8c: the child's stdin is ours to write and close - ``communicate()`` used to
    # do it for us.  A closed stdin is the default (DEVNULL when there is no data).
    if payload is not None and proc.stdin is not None:
        try:
            proc.stdin.write(payload)
            proc.stdin.close()
        except OSError:  # pragma: no cover - the child died before reading
            pass

    buffers: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
    lock = threading.Lock()

    def _reader(stream: Any, name: str) -> None:
        """Park on one pipe and publish what arrives (never raises)."""
        try:
            handle = stream.fileno()
            while True:
                data = os.read(handle, 4096)  # returns as soon as anything arrives
                if not data:
                    break
                with lock:
                    buffers[name].append(data)
                if on_chunk is not None:
                    try:
                        on_chunk(name, _decode(data))
                    except Exception:  # noqa: BLE001 - observation is optional
                        pass
        except Exception:  # noqa: BLE001 - EOF, closed pipes, a dying fd
            pass

    readers = [
        threading.Thread(
            target=_reader, args=(proc.stdout, "stdout"), name=f"{THREAD_NAME}-out", daemon=True
        ),
        threading.Thread(
            target=_reader, args=(proc.stderr, "stderr"), name=f"{THREAD_NAME}-err", daemon=True
        ),
    ]
    for reader in readers:
        reader.start()

    timed_out = False
    cancelled = False
    deadline = started + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            _kill_group(proc)
            break
        if cancel is not None and cancel.is_set():
            cancelled = True
            _kill_group(proc)
            break
        try:
            proc.wait(timeout=min(HEARTBEAT_SECONDS, remaining))
            break
        except subprocess.TimeoutExpired:
            continue

    if timed_out or cancelled:
        # Reap the group leader: a zombie keeps the process group alive, which
        # would both leak the process and make "is the group gone?" lie (measured
        # in the R8c spike).  The caller still sees ``returncode=None``, as before.
        try:
            proc.wait(timeout=kill_grace)
        except subprocess.TimeoutExpired:  # pragma: no cover - escaped the group
            _log.debug("process %s did not die after killpg", proc.pid)

    # R8c: the process is gone, but a grandchild may still hold the pipe.  Give the
    # readers a bounded window to drain it - returning early here would truncate
    # output that the old ``communicate()`` would have delivered.
    for reader in readers:
        reader.join(timeout=kill_grace)
    if any(reader.is_alive() for reader in readers):
        # Something escaped the process group and still holds the pipes: abandon
        # them, the thread must return, and partial output still helps.
        _log.debug("process %s: readers still blocked; abandoning their pipes", proc.pid)
        _close_pipes(proc)
        for reader in readers:
            reader.join(timeout=0.5)

    with lock:
        stdout_bytes = b"".join(buffers["stdout"])
        stderr_bytes = b"".join(buffers["stderr"])
    returncode: int | None = None if (timed_out or cancelled) else proc.returncode

    stdout = _decode(stdout_bytes)
    stderr = _decode(stderr_bytes)
    return ProcessOutcome(
        returncode=returncode,
        stdout=_truncate(stdout, max_output_chars, len(stdout)),
        stderr=_truncate(stderr, max_output_chars, len(stderr)),
        timed_out=timed_out,
        cancelled=cancelled,
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
    on_chunk: Callable[[str, str], None] | None = None,
    cancel: threading.Event | None = None,
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
    # R8c: the readers live in the worker thread, so handing a chunk back has to
    # cross threads - the same ``call_soon_threadsafe`` path R2b already uses for
    # the outcome itself.
    loop = asyncio.get_running_loop()
    deliver: Callable[[str, str], None] | None = None
    if on_chunk is not None:
        def _deliver(stream: str, text: str) -> None:
            loop.call_soon_threadsafe(on_chunk, stream, text)

        deliver = _deliver
    kwargs: dict[str, Any] = {
        "command": command,
        "shell": shell,
        "cwd": cwd,
        "env": env,
        "stdin_data": stdin_data,
        "timeout": timeout,
        "kill_grace": kill_grace,
        "max_output_chars": max_output_chars,
        "on_chunk": deliver,
        "cancel": cancel,
    }
    try:
        return await run_with_deadline(
            _start_process_thread(kwargs),
            timeout=budget,
            label="process",
            tick=tick,
        )
    except asyncio.CancelledError:
        # The wait was cancelled (Esc-abort cancels the awaiting task rather
        # than firing `cancel`): stop the command too, not just the wait.
        # The work lives in a daemon thread that no cancellation reaches, so
        # without this a `sleep 30` outlives its turn and its late chunks land
        # in dead transcript.  Re-raised: cancellation is not ours to swallow.
        if cancel is not None:
            cancel.set()
        raise
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
