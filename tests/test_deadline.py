"""TDD (R2a): hard deadlines — how this project waits on the outside world.

``asyncio.wait_for`` cannot bound a coroutine that is slow (or unwilling) to
acknowledge cancellation: ``wait_for`` awaits the cancellation before it raises.
``proc.communicate()`` is exactly such a coroutine when another process still
holds the pipe, which is why the old tool timeout could never fire
(docs/r2-analysis.md section 1.4-A).

``run_with_deadline`` abandons instead of waiting, and keeps a heartbeat tick so
a lost wakeup (docs/r2-analysis.md section 1.3) cannot stall the loop.
"""

import asyncio
import threading
import time

import pytest

from ai_coding.infra.deadline import (
    Deadline,
    DeadlineExpired,
    RunCancelled,
    remaining_of,
    run_with_deadline,
)

# ---- R5: the "how long is left" helper had two copies -----------------------


def test_remaining_of_returns_none_without_a_deadline():
    assert remaining_of(None) is None


def test_remaining_of_never_goes_negative():
    assert remaining_of(Deadline.after(0.5)) > 0
    assert remaining_of(Deadline.after(-5)) == 0.0


@pytest.mark.asyncio
async def test_passes_through_result_without_deadline():
    async def work() -> str:
        await asyncio.sleep(0)
        return "done"

    assert await run_with_deadline(work(), timeout=None, label="work") == "done"


@pytest.mark.asyncio
async def test_passes_through_result_within_deadline():
    async def work() -> str:
        await asyncio.sleep(0.01)
        return "fast"

    assert await run_with_deadline(work(), timeout=5, label="work") == "fast"


@pytest.mark.asyncio
async def test_expires_and_raises_deadline_expired():
    started = time.monotonic()
    with pytest.raises(DeadlineExpired) as excinfo:
        await run_with_deadline(asyncio.sleep(30), timeout=0.05, label="sleepy")
    assert time.monotonic() - started < 1.0
    assert excinfo.value.label == "sleepy"
    assert excinfo.value.timeout == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_does_not_wait_for_cancellation_completion():
    """The whole point: a stubborn coroutine must not delay the deadline."""
    abandoned: list[asyncio.Task] = []

    async def slow_to_cancel() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            await asyncio.sleep(0.2)  # deliberately ignores the cancel for a while
            raise

    started = time.monotonic()
    with pytest.raises(DeadlineExpired):
        await run_with_deadline(
            slow_to_cancel(),
            timeout=0.05,
            label="stubborn",
            on_abandon=abandoned.append,
        )
    assert time.monotonic() - started < 0.5
    assert len(abandoned) == 1
    assert not abandoned[0].done()  # it was abandoned, not awaited

    # Let it finish so the test leaves no half-cancelled task behind.
    await asyncio.gather(*abandoned, return_exceptions=True)


@pytest.mark.asyncio
async def test_non_positive_timeout_expires_immediately():
    ran = False

    async def work() -> None:
        nonlocal ran
        ran = True

    with pytest.raises(DeadlineExpired):
        await run_with_deadline(work(), timeout=0, label="none")
    assert ran is False


@pytest.mark.asyncio
async def test_heartbeat_delivers_cross_thread_completion():
    """A completion signalled from another thread must not wait for a timer.

    Regression guard for the lost-wakeup environment (docs/r2-analysis.md 1.3):
    without the heartbeat the loop only wakes at the deadline itself.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()

    def worker() -> None:
        time.sleep(0.1)
        loop.call_soon_threadsafe(future.set_result, "from-thread")

    threading.Thread(target=worker, daemon=True).start()
    started = time.monotonic()
    assert await run_with_deadline(future, timeout=2.0, label="thread") == "from-thread"
    assert time.monotonic() - started < 1.5


@pytest.mark.asyncio
async def test_propagates_inner_exception():
    async def boom() -> None:
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        await run_with_deadline(boom(), timeout=1.0, label="boom")


@pytest.mark.asyncio
async def test_leaves_no_heartbeat_task_behind():
    await run_with_deadline(asyncio.sleep(0), timeout=1.0, label="quick")
    await asyncio.sleep(0.1)
    current = asyncio.current_task()
    assert asyncio.all_tasks() - {current} == set()


def test_deadline_value_object():
    deadline = Deadline.after(30)
    assert 29 < deadline.remaining() <= 30
    assert deadline.expired is False
    assert Deadline.after(0).expired is True
    assert Deadline.after(-1).remaining() < 0


# ---- R8c: cancellation is a second reason to abandon an await ---------------


@pytest.mark.asyncio
async def test_r8_a_set_cancel_event_abandons_the_await():
    cancel = asyncio.Event()
    started = time.monotonic()

    async def slow() -> None:
        await asyncio.sleep(30)

    async def trip() -> None:
        await asyncio.sleep(0.05)
        cancel.set()

    with pytest.raises(RunCancelled) as caught:
        await asyncio.gather(
            run_with_deadline(slow(), timeout=None, label="tool:bash", cancel=cancel),
            trip(),
        )

    assert caught.value.label == "tool:bash"
    assert time.monotonic() - started < 5.0


@pytest.mark.asyncio
async def test_r8_an_unset_cancel_event_changes_nothing():
    cancel = asyncio.Event()
    deadline = Deadline.after(5)
    value = await run_with_deadline(
        asyncio.sleep(0, result="done"), timeout=deadline.remaining(), label="quick",
        cancel=cancel,
    )
    assert value == "done"


@pytest.mark.asyncio
async def test_r8_a_cancelled_wait_leaves_no_tasks_behind():
    cancel = asyncio.Event()
    cancel.set()
    with pytest.raises(RunCancelled):
        await run_with_deadline(asyncio.sleep(30), timeout=None, label="slow", cancel=cancel)
    await asyncio.sleep(0.1)
    current = asyncio.current_task()
    assert asyncio.all_tasks() - {current} == set()


@pytest.mark.asyncio
async def test_external_cancellation_reaches_the_inner_task():
    """If the caller is cancelled, the awaited work must be cancelled too.

    Otherwise the inner task is orphaned and keeps running.  Measured live:
    a bash `sleep 60` outlived its aborted turn (Esc) because the dispatcher's
    ``run_with_deadline`` dropped the tool task instead of cancelling it, and
    the subprocess thread — which no cancellation reaches — never got the kill.
    """
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def work() -> None:
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    outer = asyncio.ensure_future(
        # An (unset) cancel event forces the ``asyncio.wait`` path — the one
        # that orphans the inner task without the fix.  ``timeout=None`` alone
        # would take the ``await task`` path, which forwards cancellation for
        # free and would pass even on the broken code.
        run_with_deadline(
            work(), timeout=None, label="tool:bash", cancel=asyncio.Event()
        )
    )
    await asyncio.wait_for(started.wait(), timeout=2.0)
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer
    await asyncio.wait_for(cancelled.wait(), timeout=2.0)
