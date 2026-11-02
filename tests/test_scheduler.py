"""P2-C1: Scheduler is one facade over runner, fan-out, slots and registry."""

import asyncio
import time

import pytest

from ai_coding.core.execution_slots import (
    ExecutionSlotPool,
    enter_child,
    holding_slot,
)
from ai_coding.core.research_fanout import ResearchPacket, ResearchQuestion
from ai_coding.core.scheduler import Scheduler
from ai_coding.core.task_registry import TaskRegistry
from ai_coding.domain.orchestration import TaskRequest
from ai_coding.domain.subagent import (
    BackgroundTask,
    SubAgentPoolFull,
    SubagentResult,
    SubagentTask,
)


def _task(text="do it"):
    return SubagentTask.from_objective(text)


class _Runner:
    def __init__(self) -> None:
        self.run_calls: list[dict] = []
        self.spawn_calls: list[dict] = []
        self.sent: list[tuple[str, str]] = []
        self.interrupted: list[str] = []
        self.resumed: list[str] = []
        self.closed: list[str] = []
        self.cancelled: list[str] = []
        self.waits: list[tuple[str, int]] = []
        self.rows: list[BackgroundTask] = []
        self.close_info_result: dict = {}
        self.run_result = SubagentResult(
            status="completed", conclusion="done", usage={"total_tokens": 7},
            ledger_path="/l.json", changed_paths=("a.py",),
        )

    async def run(self, task, name="sub-agent", **kwargs):
        self.run_calls.append({"task": task, "name": name, **kwargs})
        if kwargs.get("observer") is not None:
            assert holding_slot() is True, "a scheduler run owns the slot"
        return self.run_result

    def spawn_background(self, task, name="sub-agent", **kwargs):
        self.spawn_calls.append({"task": task, "name": name, **kwargs})
        return "run_000000000001"

    async def send_message(self, target, text):
        self.sent.append((target, text))
        return "queued", target

    async def interrupt(self, target):
        self.interrupted.append(target)
        return "running", "interrupted"

    async def resume(self, target):
        self.resumed.append(target)
        return "idle", target

    async def close(self, target):
        self.closed.append(target)
        return True

    def close_info(self, target):
        return dict(self.close_info_result)

    def cancel(self, target):
        self.cancelled.append(target)
        return True

    async def wait(self, target, timeout_ms=30_000):
        self.waits.append((target, timeout_ms))
        return "completed", 100

    def task_list(self):
        return list(self.rows)

    def resolve_run_id(self, prefix):
        matches = [row.run_id for row in self.rows if row.run_id.startswith(prefix)]
        return matches[0] if len(matches) == 1 else None

    def resolve_target(self, target):
        resolved = self.resolve_run_id(target)
        if resolved is None:
            return "unknown", "", []
        return "ok", resolved, []


class _FanOut:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def run(self, questions, **kwargs):
        self.calls.append({"questions": questions, **kwargs})
        return [
            ResearchPacket(
                id=question.id,
                status="completed",
                summary="summary",
                usage={"total_tokens": 3},
                truncated=True,
            )
            for question in questions
        ]


def _scheduler(runner=None, fanout=None, slots=None):
    runner = runner or _Runner()
    registry = TaskRegistry(runner)
    return Scheduler(
        runner,
        fanout=fanout or _FanOut(),
        slots=slots or ExecutionSlotPool(1),
        registry=registry,
    ), runner


@pytest.mark.asyncio
async def test_run_owns_a_slot_and_registers_its_final_view():
    scheduler, runner = _scheduler()
    seen: list[int] = []
    original = runner.run

    async def run(task, name="sub-agent", **kwargs):
        seen.append(scheduler.slots.active)
        return await original(task, name=name, **kwargs)

    runner.run = run  # type: ignore[method-assign]
    request = TaskRequest.for_child(_task(), name="probe", parent_span="call-1")

    result = await scheduler.run(
        request, parent="parent", observer=object(), emit=lambda _e: None
    )

    assert result is runner.run_result
    assert seen == [1]
    assert scheduler.slots.active == 0
    view = scheduler.tasks().get(request.id)
    assert view is not None
    assert view.origin_session == request.origin_session
    assert view.kind == "child"
    assert view.status == "completed"
    assert view.changed_paths == ("a.py",)
    call = runner.run_calls[0]
    assert call["parent"] == "parent"
    assert call["observer"] is not None
    assert call["parent_span"] == "call-1"


@pytest.mark.asyncio
async def test_run_without_an_observer_uses_emit():
    scheduler, runner = _scheduler()
    request = TaskRequest.for_child(_task())
    await scheduler.run(request, parent=None, observer=None, emit=lambda _e: None)
    assert runner.run_calls[0]["observer"] is not None


@pytest.mark.asyncio
async def test_spawn_reserves_a_slot_and_returns_both_ids():
    scheduler, runner = _scheduler()
    request = TaskRequest.for_child(_task(), name="probe", parent_span="call-1")

    request_id, run_id = await scheduler.spawn(
        request, parent=None, observer=object(), emit=lambda _e: None
    )

    assert request_id == request.id
    assert run_id == "run_000000000001"
    assert scheduler.slots.active == 1
    assert runner.spawn_calls[0]["name"] == "probe"
    assert runner.spawn_calls[0]["emit"] is not None
    # The background row is the live projection; spawn must not leave a
    # second, permanent "running" view behind.
    assert scheduler.tasks().get(request.id) is None
    assert scheduler.tasks().list() == []


@pytest.mark.asyncio
async def test_a_nested_run_inside_an_owned_slot_does_not_deadlock():
    scheduler, runner = _scheduler(slots=ExecutionSlotPool(1))
    outer = scheduler.slots.try_reserve("outer")
    assert outer is not None
    with enter_child():
        result = await scheduler.run(
            TaskRequest.for_child(_task()), parent=None, observer=object(), emit=None
        )
    assert result is runner.run_result
    assert scheduler.slots.active == 1  # only the outer reservation
    outer.release()
    assert scheduler.slots.active == 0


@pytest.mark.asyncio
async def test_two_fanouts_with_the_same_question_ids_do_not_collide():
    scheduler, _runner = _scheduler()
    questions = [ResearchQuestion("q1", "a"), ResearchQuestion("q2", "b")]

    await scheduler.fanout(questions, "shared", parent_span="call-1")
    await scheduler.fanout(questions, "shared", parent_span="call-2")

    views = [view for view in scheduler.tasks().list() if view.kind == "fanout"]
    assert {view.id for view in views} == {
        "fanout:call-1:q1", "fanout:call-1:q2",
        "fanout:call-2:q1", "fanout:call-2:q2",
    }


@pytest.mark.asyncio
async def test_a_full_pool_refuses_spawn_and_releases_nothing():
    scheduler, runner = _scheduler(slots=ExecutionSlotPool(1))
    taken = scheduler.slots.try_reserve("other")
    assert taken is not None
    request = TaskRequest.for_child(_task())

    with pytest.raises(SubAgentPoolFull):
        await scheduler.spawn(request, parent=None, observer=None, emit=None)

    assert runner.spawn_calls == []
    assert scheduler.slots.active == 1
    taken.release()
    assert scheduler.slots.active == 0


@pytest.mark.asyncio
async def test_spawn_failure_releases_the_reserved_slot():
    scheduler, runner = _scheduler()

    def boom(*_args, **_kwargs):
        raise RuntimeError("cannot start")

    runner.spawn_background = boom  # type: ignore[method-assign]
    request = TaskRequest.for_child(_task())

    with pytest.raises(RuntimeError, match="cannot start"):
        await scheduler.spawn(request, parent=None, observer=None, emit=None)

    assert scheduler.slots.active == 0


@pytest.mark.asyncio
async def test_fanout_registers_one_view_per_question_without_copying_content():
    scheduler, _runner = _scheduler()
    questions = [ResearchQuestion("q1", "a"), ResearchQuestion("q2", "b")]

    packets = await scheduler.fanout(
        questions, "shared", parent=None, observer=object(), parent_span="call-1"
    )

    assert [packet.id for packet in packets] == ["q1", "q2"]
    views = [view for view in scheduler.tasks().list() if view.kind == "fanout"]
    assert {view.id for view in views} == {"fanout:call-1:q1", "fanout:call-1:q2"}
    q1 = next(view for view in views if view.id == "fanout:call-1:q1")
    assert q1.status == "completed"
    assert q1.usage == {"total_tokens": 3}
    assert q1.truncated is True
    assert scheduler.slots.active == 0
    call = scheduler._fanout.calls[0]
    assert call["shared_context"] == "shared"
    assert call["parent_span"] == "call-1"


@pytest.mark.asyncio
async def test_control_api_delegates_and_resolves_request_ids():
    scheduler, runner = _scheduler()
    runner.rows = [BackgroundTask(run_id="run_000000000001", status="running",
                                  started=time.monotonic())]
    request = TaskRequest.for_child(_task(), name="probe")
    request_id, run_id = await scheduler.spawn(request, parent=None, observer=None,
                                               emit=None)

    assert scheduler.resolve_run_id(request_id) == run_id
    assert scheduler.resolve_target(request_id)[0] == "ok"

    status, sent_run = await scheduler.send(request_id, "hi")
    assert (status, sent_run) == ("queued", run_id)
    assert runner.sent == [(run_id, "hi")]

    previous, state = await scheduler.interrupt(request_id)
    assert (previous, state) == ("running", "interrupted")
    assert runner.interrupted == [run_id]

    resumed = await scheduler.resume(request_id)
    assert resumed == ("idle", run_id)
    assert runner.resumed == [run_id]

    closed = await scheduler.close(request_id)
    assert closed is True
    assert runner.closed == [run_id]

    waited = await scheduler.wait(request_id, 500)
    assert waited == ("completed", 100)
    assert runner.waits == [(run_id, 500)]

    runner.close_info_result = {"commit": "abcdef"}
    assert scheduler.close_info(request_id) == {"commit": "abcdef"}

    assert scheduler.stop(request_id) is True
    assert runner.cancelled == [run_id]
    assert [row.run_id for row in scheduler.task_list()] == [run_id]


@pytest.mark.asyncio
async def test_run_is_queued_not_refused_when_the_pool_is_full():
    scheduler, _runner = _scheduler(slots=ExecutionSlotPool(1))
    held = await scheduler.slots.acquire("other")
    request = TaskRequest.for_child(_task())

    async def release_soon():
        await asyncio.sleep(0.02)
        held.release()

    releaser = asyncio.create_task(release_soon())
    result = await asyncio.wait_for(
        scheduler.run(request, parent=None, observer=object(), emit=None), timeout=5
    )
    await releaser
    assert result.status == "completed"
    assert scheduler.slots.active == 0
