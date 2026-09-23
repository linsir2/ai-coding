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

from ai_coding.infra.deadline import Deadline, DeadlineExpired, run_with_deadline


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
