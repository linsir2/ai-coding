"""P2-C1: one execution-slot pool for tools, children and fan-out."""

import asyncio

import pytest

from ai_coding.core.execution_slots import (
    ExecutionSlotPool,
    enter_child,
    holding_slot,
    slot_held,
)


@pytest.mark.asyncio
async def test_acquire_and_release_are_idempotent():
    pool = ExecutionSlotPool(1)
    slot = await pool.acquire("first")
    assert pool.active == 1
    slot.release()
    slot.release()  # a second release must not free someone else's slot
    assert pool.active == 0


@pytest.mark.asyncio
async def test_try_reserve_refuses_a_full_pool_and_recovers_on_release():
    pool = ExecutionSlotPool(1)
    slot = pool.try_reserve("first")
    assert slot is not None
    assert pool.try_reserve("second") is None
    slot.release()
    again = pool.try_reserve("second")
    assert again is not None and pool.active == 1
    again.release()


@pytest.mark.asyncio
async def test_a_waiter_gets_the_slot_the_release_hands_back():
    pool = ExecutionSlotPool(1)
    first = await pool.acquire("first")
    order: list[str] = []

    async def waiter() -> None:
        slot = await pool.acquire("waiter")
        order.append("acquired")
        slot.release()

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    assert order == []
    first.release()
    await asyncio.wait_for(task, timeout=5)
    assert order == ["acquired"]
    assert pool.active == 0


@pytest.mark.asyncio
async def test_a_cancelled_waiter_does_not_leak_a_slot():
    pool = ExecutionSlotPool(1)
    first = await pool.acquire("first")
    task = asyncio.create_task(pool.acquire("waiter"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    first.release()
    again = pool.try_reserve("next")
    assert again is not None


def test_enter_child_marks_only_its_own_block():
    assert holding_slot() is False
    with enter_child():
        assert holding_slot() is True
        with enter_child():
            assert holding_slot() is True
        assert holding_slot() is True
    assert holding_slot() is False
    assert slot_held.get() is False


def test_max_slots_is_at_least_one():
    pool = ExecutionSlotPool(0)
    assert pool.max_slots == 1


@pytest.mark.asyncio
async def test_keyed_slots_expose_a_read_only_active_view():
    pool = ExecutionSlotPool(2)
    slot = pool.try_reserve("child", key="run_a")
    assert slot is not None
    assert pool.active_keys == {"run_a"}
    slot.release()
    assert pool.active_keys == set()
