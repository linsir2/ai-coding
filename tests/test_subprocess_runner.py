"""TDD (R2b): the single place this project starts a subprocess.

Replaces ``asyncio.create_subprocess_*`` + ``communicate()``, whose timeout was
unreachable while another process held the pipe (docs/r2-analysis.md 1.4-A) and
which depended on SIGCHLD wakeups the environment can drop (1.3).

The runner is thread-based on purpose: a synchronous ``communicate(timeout)``
cannot be defeated by a stalled cancellation, and ``killpg`` reaches the whole
process group (``sleep 30 & sleep 30`` included).
"""

import asyncio
import os
import pathlib
import signal
import time

import pytest

from ai_coding.infra.subprocess_runner import (
    ProcessOutcome,
    run_process,
    run_process_async,
)


def test_echo_returns_stdout():
    outcome = run_process(["echo", "hi"], timeout=5)
    assert outcome.timed_out is False
    assert outcome.error is None
    assert outcome.returncode == 0
    assert outcome.stdout == "hi\n"


def test_shell_command_runs_through_a_shell():
    outcome = run_process("echo shell-hi && echo err >&2", shell=True, timeout=5)
    assert outcome.returncode == 0
    assert "shell-hi" in outcome.stdout
    assert "err" in outcome.stderr


def test_nonzero_exit_is_reported_not_raised():
    outcome = run_process("exit 3", shell=True, timeout=5)
    assert outcome.returncode == 3
    assert outcome.timed_out is False
    assert outcome.error is None


def test_spawn_failure_is_an_outcome_not_an_exception():
    outcome = run_process(["definitely-not-a-real-binary-xyz"], timeout=5)
    assert outcome.error is not None
    assert outcome.returncode is None
    assert outcome.timed_out is False


def test_cwd_is_honoured(tmp_path: pathlib.Path):
    outcome = run_process(["pwd"], cwd=str(tmp_path), timeout=5)
    assert os.path.realpath(outcome.stdout.strip()) == os.path.realpath(str(tmp_path))


def test_env_is_honoured():
    outcome = run_process(
        ["sh", "-c", "printf %s $AI_CODING_PROBE"],
        env={"PATH": os.environ.get("PATH", ""), "AI_CODING_PROBE": "xyz"},
        timeout=5,
    )
    assert outcome.stdout == "xyz"


def _wait_for_group_teardown(pgid: int, timeout: float = 3.0) -> bool:
    """Wait for a process group to disappear, then report whether it did.

    ``os.killpg(pgid, 0)`` can keep succeeding for a moment after the last member
    is gone — the group id itself is torn down slightly later.  Measured: in ~1 of
    8 runs the immediate check failed while ``/proc`` showed the group had no live
    members at all, and a probe 0.5s later raised ``ProcessLookupError``.  So the
    meaningful assertion is "nothing is left running", not "the kernel has already
    forgotten the group id".
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def test_timeout_kills_the_whole_process_group(tmp_path: pathlib.Path):
    """``sleep 30 & sleep 30`` holds the pipe; the old implementation hung here."""
    pidfile = tmp_path / "pgid"
    command = f"echo $$ > {pidfile}; sleep 30 & sleep 30"

    started = time.monotonic()
    outcome = run_process(command, shell=True, timeout=0.3, kill_grace=1.0)
    elapsed = time.monotonic() - started

    assert outcome.timed_out is True
    assert elapsed < 8.0, f"runner took {elapsed:.1f}s; the deadline is not hard"

    pgid = int(pidfile.read_text(encoding="utf-8").strip())
    # The group must end up empty (grandchildren included).  Waiting for the
    # kernel to drop the group *id* is a different, racier claim — see the helper.
    assert _wait_for_group_teardown(pgid), "the whole process group must be gone"


def test_partial_output_before_timeout_is_kept():
    outcome = run_process(
        "echo before; sleep 30", shell=True, timeout=0.3, kill_grace=1.0
    )
    assert outcome.timed_out is True
    assert "before" in outcome.stdout


def test_output_truncation():
    outcome = run_process(
        "printf '0123456789abcdef'", shell=True, timeout=5, max_output_chars=10
    )
    assert outcome.returncode == 0
    assert "truncated" in outcome.stdout
    assert len(outcome.stdout) < 100


def test_exit_signal_yields_negative_returncode():
    outcome = run_process("kill -TERM $$", shell=True, timeout=5)
    assert outcome.returncode == -signal.SIGTERM


@pytest.mark.asyncio
async def test_async_variant_runs_in_a_thread_and_bounds_the_wait():
    outcome = await run_process_async(["echo", "async-hi"], timeout=5)
    assert isinstance(outcome, ProcessOutcome)
    assert outcome.stdout.strip() == "async-hi"

    started = time.monotonic()
    timed_out = await run_process_async(
        "sleep 30", shell=True, timeout=0.3, kill_grace=1.0
    )
    assert timed_out.timed_out is True
    assert time.monotonic() - started < 8.0


@pytest.mark.asyncio
async def test_async_variant_does_not_stall_on_a_quiet_loop():
    """Heartbeat guard: this must return promptly even with no other timers."""
    started = time.monotonic()
    outcome = await run_process_async(["echo", "tick"], timeout=5)
    assert outcome.stdout.strip() == "tick"
    assert time.monotonic() - started < 2.0


@pytest.mark.asyncio
async def test_async_variant_honours_a_deadline_bound():
    """The outer hard deadline is strictly larger than the inner one."""
    started = time.monotonic()
    outcome = await run_process_async("sleep 30", shell=True, timeout=0.2, kill_grace=0.2)
    assert outcome.timed_out is True
    assert time.monotonic() - started < 5.0
    await asyncio.sleep(0)


# ---- R8c-S1: the runner can speak while it works (docs/r8-analysis.md 3.3) ----


def test_r8_chunks_arrive_while_the_command_is_still_running():
    """The old implementation only ever returned at the end (2.4s of silence)."""
    stamps: list[tuple[float, str, str]] = []
    started = time.monotonic()

    outcome = run_process(
        "for i in 1 2 3; do echo step-$i; sleep 0.3; done",
        shell=True,
        timeout=10,
        on_chunk=lambda stream, text: stamps.append(
            (time.monotonic() - started, stream, text)
        ),
    )
    elapsed = time.monotonic() - started

    assert outcome.returncode == 0
    assert [text for _at, _stream, text in stamps] == ["step-1\n", "step-2\n", "step-3\n"]
    assert all(stream == "stdout" for _at, stream, _text in stamps)
    assert stamps[0][0] < 0.3, "the first line must not wait for the command to finish"
    assert stamps[0][0] < elapsed


def test_r8_streaming_does_not_change_the_outcome():
    plain = run_process("echo hi && echo err >&2", shell=True, timeout=5)
    streamed = run_process(
        "echo hi && echo err >&2", shell=True, timeout=5, on_chunk=lambda *_: None
    )
    assert (streamed.stdout, streamed.stderr, streamed.returncode) == (
        plain.stdout,
        plain.stderr,
        plain.returncode,
    )


def test_r8_a_chunk_callback_that_raises_does_not_break_the_run():
    def boom(*_args: object) -> None:
        raise RuntimeError("observer")

    outcome = run_process("echo hi", shell=True, timeout=5, on_chunk=boom)
    assert outcome.returncode == 0
    assert outcome.stdout == "hi\n"


def test_r8_stdin_data_still_reaches_the_child():
    """Command hooks feed their context through stdin (R4c)."""
    outcome = run_process("cat", shell=True, timeout=5, stdin_data="from-stdin\n")
    assert outcome.returncode == 0
    assert outcome.stdout == "from-stdin\n"


def test_r8_output_after_the_shell_exits_is_not_truncated():
    """A grandchild holding the pipe must not cost us what we already read."""
    outcome = run_process(
        "echo first; (sleep 0.4; echo late) & exit 0", shell=True, timeout=5
    )
    assert "first" in outcome.stdout
    assert "late" in outcome.stdout, "the reader must drain before returning"


@pytest.mark.asyncio
async def test_r8_the_async_variant_streams_too():
    seen: list[str] = []

    outcome = await run_process_async(
        "echo async-hi",
        shell=True,
        timeout=5,
        on_chunk=lambda _stream, text: seen.append(text),
    )

    assert outcome.stdout == "async-hi\n"
    assert "".join(seen) == "async-hi\n"
