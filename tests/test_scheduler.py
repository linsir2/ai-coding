"""P2-C1: Scheduler is one facade over runner, fan-out, slots and registry."""

import asyncio
import time
from dataclasses import replace

import pytest

from ai_coding.core.execution_slots import (
    ExecutionSlotPool,
    enter_child,
    holding_slot,
)
from ai_coding.core.research_fanout import ResearchPacket, ResearchQuestion
from ai_coding.core.scheduler import IdempotencyConflict, Scheduler
from ai_coding.core.task_journal import TaskJournal
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


class _MergeQueue:
    def __init__(self, *, fail_with=None) -> None:
        self.tickets = []
        self.fail_with = fail_with
        self.closed = False

    def enqueue(self, ticket):
        if self.fail_with is not None:
            raise self.fail_with("nope")
        self.tickets.append(ticket)
        ticket.status = "queued"
        if ticket.on_update is not None:
            ticket.on_update(ticket)

    async def wait(self, ticket):
        await asyncio.sleep(0)
        ticket.status = "merged"
        ticket.ended = time.monotonic()
        ticket.outcome = {
            "status": "merged",
            "branch": ticket.branch,
            "base_commit": ticket.base_commit,
            "commit": "c" * 40,
            "main_head": "d" * 40,
            "verified": False,
            "attempts": 1,
        }
        if ticket.on_update is not None:
            ticket.on_update(ticket)
        ticket.done.set()
        return ticket.outcome

    async def aclose(self):
        self.closed = True


def _scheduler(runner=None, fanout=None, slots=None, merge_queue=None):
    runner = runner or _Runner()
    registry = TaskRegistry(runner)
    return Scheduler(
        runner,
        fanout=fanout or _FanOut(),
        slots=slots or ExecutionSlotPool(1),
        registry=registry,
        merge_queue=merge_queue,
    ), runner


@pytest.mark.asyncio
async def test_merge_uses_queue_and_registers_a_merge_view():
    runner = _Runner()
    runner.rows = [
        BackgroundTask(
            run_id="run_a", name="probe", status="completed", started=1.0,
            ended=2.0, worktree_branch="ai_sub_x",
            base_commit="a" * 40, commit="b" * 40,
        )
    ]
    merge_queue = _MergeQueue()
    scheduler, _ = _scheduler(runner, merge_queue=merge_queue)

    out = await scheduler.merge("run_a")

    assert out["status"] == "merged"
    assert len(merge_queue.tickets) == 1
    view = scheduler.tasks().get("merge:ai_sub_x")
    assert view is not None and view.kind == "merge"
    assert view.branch == "ai_sub_x"
    assert view.commit == "c" * 40
    assert scheduler.merge_status("ai_sub_x")["status"] == "merged"
    assert scheduler.merge_status("missing") == {"status": "not_found"}


@pytest.mark.asyncio
async def test_merge_refuses_unsettled_and_commit_error_sources():
    runner = _Runner()
    runner.rows = [
        BackgroundTask(run_id="run_live", name="live", status="running",
                       worktree_branch="ai_sub_live"),
    ]
    scheduler, _ = _scheduler(runner, merge_queue=_MergeQueue())
    assert (await scheduler.merge("run_live"))["status"] == "not_settled"

    runner.rows = [
        BackgroundTask(
            run_id="run_bad", name="bad", status="completed",
            worktree_branch="ai_sub_bad", commit_error="commit failed",
        )
    ]
    scheduler, _ = _scheduler(runner, merge_queue=_MergeQueue())
    assert (await scheduler.merge("run_bad"))["status"] == "commit_failed"


@pytest.mark.asyncio
async def test_merge_enqueue_failure_does_not_hang_or_leave_running_view():
    from ai_coding.core.merge_queue import MergeQueueFull

    runner = _Runner()
    runner.rows = [
        BackgroundTask(run_id="run_a", name="probe", status="completed",
                       worktree_branch="ai_sub_x"),
    ]
    scheduler, _ = _scheduler(
        runner, merge_queue=_MergeQueue(fail_with=MergeQueueFull)
    )

    out = await asyncio.wait_for(scheduler.merge("run_a"), timeout=1.0)

    assert out["status"] == "busy"
    view = scheduler.tasks().get("merge:ai_sub_x")
    assert view is not None
    assert view.status == "busy"
    assert view.is_running is False


@pytest.mark.asyncio
async def test_merge_without_a_queue_is_unavailable():
    scheduler, _ = _scheduler()
    assert (await scheduler.merge("anything"))["status"] == "unavailable"


def test_recover_keeps_child_recovered_and_merge_merge(tmp_path):
    journal = TaskJournal(tmp_path)
    journal.append("s1", "running", task_id="child-1", kind="child",
                   name="child")
    journal.append("s1", "merge_queued", task_id="merge:ai_sub_x",
                   kind="merge", branch="ai_sub_x", name="merge:ai_sub_x",
                   base_commit="a" * 40)
    scheduler, _ = _scheduler()
    scheduler._journal = journal

    scheduler.recover()

    views = {view.id: view for view in scheduler.tasks().list()}
    assert views["child-1"].kind == "recovered"
    assert views["merge:ai_sub_x"].kind == "merge"
    assert views["merge:ai_sub_x"].status == "needs_reconciliation"


@pytest.mark.asyncio
async def test_run_registers_the_result_base_commit():
    scheduler, runner = _scheduler()
    runner.run_result = replace(runner.run_result, base_commit="c" * 40)
    request = TaskRequest.for_child(_task(), name="probe")

    await scheduler.run(request)

    view = scheduler.tasks().get(request.id)
    assert view is not None
    assert view.base_commit == "c" * 40
    assert view.to_dict()["baseCommit"] == "c" * 40


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


# ---- P2-C2: journal, fence, stop, recovery, idempotency --------------------


def _journaled(tmp_path):
    runner = _Runner()
    return Scheduler(
        runner,
        fanout=_FanOut(),
        slots=ExecutionSlotPool(4),
        registry=TaskRegistry(runner),
        journal=TaskJournal(tmp_path),
    ), runner


@pytest.mark.asyncio
async def test_recover_marks_open_tasks_and_never_reruns_them(tmp_path):
    scheduler, runner = _journaled(tmp_path)
    journal = scheduler.journal
    journal.append("s1", "queued", task_id="open-1")
    journal.append("s1", "settled", task_id="done-1")

    views = scheduler.recover()

    assert [view.id for view in views] == ["open-1"]
    assert views[0].needs_reconciliation is True
    assert runner.run_calls == []
    assert scheduler.tasks().get("open-1") is not None


@pytest.mark.asyncio
async def test_a_late_settlement_is_rejected_after_stop(tmp_path):
    scheduler, runner = _journaled(tmp_path)
    request = TaskRequest.for_child(_task(), name="probe")
    request_id, run_id = await scheduler.spawn(
        request, parent=None, observer=None, emit=None
    )
    scheduler.stop(request_id)

    assert scheduler.accepts(request_id, 1) is False
    assert scheduler.tasks().get(request_id) is None
    assert runner.cancelled or runner.spawn_calls


@pytest.mark.asyncio
async def test_duplicate_spawn_with_the_same_key_and_payload_returns_the_same_run(
    tmp_path,
):
    scheduler, runner = _journaled(tmp_path)
    first = TaskRequest.for_child(
        _task(), name="probe", idempotency_key="call-1", payload_digest="d1",
    )
    second = TaskRequest.for_child(
        _task(), name="probe", idempotency_key="call-1", payload_digest="d1",
    )

    rid1, run1 = await scheduler.spawn(first, parent=None, observer=None, emit=None)
    rid2, run2 = await scheduler.spawn(second, parent=None, observer=None, emit=None)

    assert (rid2, run2) == (rid1, run1)
    third = TaskRequest.for_child(
        _task(), name="probe", idempotency_key="call-1", payload_digest="other",
    )
    with pytest.raises(IdempotencyConflict):
        await scheduler.spawn(third, parent=None, observer=None, emit=None)


@pytest.mark.asyncio
async def test_an_empty_key_is_not_deduplicated(tmp_path):
    scheduler, runner = _journaled(tmp_path)
    first = TaskRequest.for_child(_task(), name="a")
    second = TaskRequest.for_child(_task(), name="b")

    _rid1, run1 = await scheduler.spawn(first, parent=None, observer=None, emit=None)
    _rid2, run2 = await scheduler.spawn(second, parent=None, observer=None, emit=None)

    assert run1 != run2 or len(runner.spawn_calls) == 2


@pytest.mark.asyncio
async def test_cancel_tree_stops_children(tmp_path):
    scheduler, runner = _journaled(tmp_path)
    parent = TaskRequest.for_child(_task(), name="parent")
    parent_id, _ = await scheduler.spawn(
        parent, parent=None, observer=None, emit=None
    )
    child = TaskRequest.for_child(
        _task(), name="child", parent_id=parent_id,
    )
    await scheduler.spawn(child, parent=None, observer=None, emit=None)

    stopped = await scheduler.cancel_tree(parent_id)
    assert stopped >= 1
    assert scheduler._records[parent_id].state in {"stopped", "cancelled"}


def test_runner_event_writes_base_and_commit_error_to_the_journal(tmp_path):
    journal = TaskJournal(tmp_path)
    scheduler, _ = _scheduler()
    scheduler._journal = journal
    request = TaskRequest.for_child(_task(), name="probe")
    record = scheduler._begin(request, kind="child")

    scheduler.on_runner_event(
        record.task_id, record.run_id, "completed", record.fence,
        branch="ai_sub_x", commit="b" * 40, base_commit="a" * 40,
        commit_error="commit failed",
    )

    state = journal.records("")[0]
    assert state.branch == "ai_sub_x"
    assert state.commit == "b" * 40
    assert state.base_commit == "a" * 40
    assert state.commit_error == "commit failed"
    assert state.last_event == "settled"
    assert state.is_terminal is True


def test_a_settled_child_can_open_a_follow_up_turn(tmp_path):
    """``send_message`` on an idle child starts a new turn on the *same* handle.

    The first turn settles the record, and ``on_turn_start`` used to reject it
    through ``accepts()`` (which answers "is this a late settlement?").  The
    caller got ``running`` while the turn returned ``stale task`` and the
    message was drained unread.  Only the fence decides turn admission.
    """
    scheduler, _ = _scheduler()
    scheduler._journal = TaskJournal(tmp_path)
    request = TaskRequest.for_child(_task(), name="probe")
    record = scheduler._begin(request, kind="child")

    scheduler.on_runner_event(
        record.task_id, record.run_id, "completed", record.fence,
    )
    assert record.state == "settled"

    assert scheduler.on_turn_start(record.task_id, record.fence, turn=2) is True
    assert record.state == "running"
    # A different claim still owns nothing here: the fence is the admission.
    assert scheduler.on_turn_start(record.task_id, record.fence + 1, turn=2) is False
    # The follow-up settles like any other turn (the record is not terminal).
    scheduler.on_runner_event(
        record.task_id, record.run_id, "completed", record.fence,
    )
    assert record.state == "settled"
    # settled -> running -> settled folds to a closed task, not a pending one.
    assert scheduler.journal.records("")[0].last_event == "settled"
    assert scheduler.journal.records("")[0].is_terminal is True


@pytest.mark.asyncio
async def test_resume_does_not_report_a_finished_child_as_stopped():
    """``resume`` never starts a turn (Codex semantics); a settled first turn is
    exactly the state a child is resumed from, so it must reach the runner."""
    scheduler, runner = _scheduler()
    request = TaskRequest.for_child(_task(), name="probe")
    record = scheduler._begin(request, kind="child")
    scheduler.on_runner_event(
        record.task_id, record.run_id, "completed", record.fence,
    )

    status, _run_id = await scheduler.resume(record.task_id)

    assert status == "idle"
    assert runner.resumed, "the runner owns the resume; the scheduler must not short-circuit it"


@pytest.mark.asyncio
async def test_merge_source_uses_the_journal_after_restart(tmp_path):
    journal = TaskJournal(tmp_path)
    journal.append(
        "s1", "settled", task_id="child-1", kind="child", name="probe",
        branch="ai_sub_x", commit="b" * 40, base_commit="a" * 40,
    )
    scheduler, _ = _scheduler(merge_queue=_MergeQueue())
    scheduler._journal = journal

    out = await scheduler.merge("child-1")

    assert out["status"] == "merged"
    ticket = scheduler._merge_queue.tickets[0]
    assert ticket.branch == "ai_sub_x"
    assert ticket.base_commit == "a" * 40


@pytest.mark.asyncio
async def test_merge_source_falls_back_to_meta_when_the_journal_lacks_branch(tmp_path):
    journal = TaskJournal(tmp_path)
    journal.append("s1", "settled", task_id="child-1", kind="child",
                   name="probe", run_id="run_a")
    scheduler, runner = _scheduler(merge_queue=_MergeQueue())
    scheduler._journal = journal
    runner.close_info_result = {
        "worktreeBranch": "ai_sub_meta", "baseCommit": "a" * 40,
    }

    out = await scheduler.merge("child-1")

    assert out["status"] == "merged"
    assert scheduler._merge_queue.tickets[0].branch == "ai_sub_meta"


# ---- SUB-02: shutdown owns the children; stop owns its task ------------------


@pytest.mark.asyncio
async def test_shutdown_closes_only_non_terminal_children():
    runner = _Runner()
    runner.rows = [
        BackgroundTask(run_id="run_a", name="a", status="running", started=1.0),
        BackgroundTask(run_id="run_b", name="b", status="completed", started=2.0),
        BackgroundTask(run_id="run_c", name="c", status="cancelled", started=3.0),
    ]
    scheduler, _ = _scheduler(runner)

    await scheduler.shutdown()

    assert runner.closed == ["run_a"]


@pytest.mark.asyncio
async def test_shutdown_cancels_an_in_flight_turn():
    runner = _Runner()
    started = asyncio.Event()

    async def hang(task, name="sub-agent", **kwargs):
        runner.run_calls.append({"task": task, "name": name, **kwargs})
        started.set()
        await asyncio.Event().wait()

    runner.run = hang  # type: ignore[method-assign]
    scheduler, _ = _scheduler(runner)
    request = TaskRequest.for_child(_task())
    turn = asyncio.create_task(
        scheduler.run(request, parent=None, observer=object(), emit=None)
    )
    await started.wait()

    await scheduler.shutdown()

    with pytest.raises(asyncio.CancelledError):
        await turn
    assert scheduler._tasks == {}
    assert scheduler.slots.active == 0


@pytest.mark.asyncio
async def test_shutdown_rejects_new_admissions():
    scheduler, runner = _scheduler()

    await scheduler.shutdown()

    with pytest.raises(RuntimeError, match="closed"):
        await scheduler.run(TaskRequest.for_child(_task()))
    with pytest.raises(RuntimeError, match="closed"):
        await scheduler.spawn(
            TaskRequest.for_child(_task()), parent=None, observer=None, emit=None
        )
    with pytest.raises(RuntimeError, match="closed"):
        await scheduler.fanout([ResearchQuestion("q1", "a")])
    assert runner.run_calls == []
    assert runner.spawn_calls == []
    assert scheduler.records == []


@pytest.mark.asyncio
async def test_shutdown_is_idempotent():
    scheduler, runner = _scheduler()

    await scheduler.shutdown()
    await scheduler.shutdown()

    assert scheduler._closed is True
    assert scheduler._tasks == {}
    assert runner.closed == []


@pytest.mark.asyncio
async def test_stop_tracks_its_stop_task():
    class _StoppingRunner(_Runner):
        def __init__(self) -> None:
            super().__init__()
            self.stopped: list[str] = []

        async def stop_run(self, run_id: str) -> bool:
            self.stopped.append(run_id)
            await asyncio.sleep(0)
            return True

    runner = _StoppingRunner()
    scheduler, _ = _scheduler(runner)
    request = TaskRequest.for_child(_task(), name="probe")
    request_id, run_id = await scheduler.spawn(
        request, parent=None, observer=None, emit=None
    )

    assert scheduler.stop(request_id) is True
    assert f"stop:{run_id}" in scheduler._tasks
    for _ in range(50):
        if f"stop:{run_id}" not in scheduler._tasks:
            break
        await asyncio.sleep(0.01)
    assert runner.stopped == [run_id]
    assert f"stop:{run_id}" not in scheduler._tasks


# ---- SUB-03: one idempotency key, one run -----------------------------------


def _keyed_request(key="call-1", digest="d1"):
    return TaskRequest.for_child(
        _task(), name="probe", idempotency_key=key, payload_digest=digest,
    )


@pytest.mark.asyncio
async def test_concurrent_runs_with_the_same_key_start_only_once():
    runner = _Runner()
    started = asyncio.Event()
    release = asyncio.Event()

    async def block(task, name="sub-agent", **kwargs):
        runner.run_calls.append({"task": task, "name": name, **kwargs})
        started.set()
        await release.wait()
        return runner.run_result

    runner.run = block  # type: ignore[method-assign]
    scheduler, _ = _scheduler(runner)
    turn = asyncio.create_task(
        scheduler.run(_keyed_request(), parent=None, observer=object(), emit=None)
    )
    await started.wait()

    with pytest.raises(IdempotencyConflict):
        await scheduler.run(_keyed_request(), parent=None, observer=object(), emit=None)

    release.set()
    result = await turn

    assert result.status == "completed"
    assert len(runner.run_calls) == 1


@pytest.mark.asyncio
async def test_a_failed_run_releases_the_key_for_retry():
    """Guard for the SUB-03 release path (green before, red if ``_idem_forget``
    is ever dropped)."""
    runner = _Runner()
    calls: list[int] = []

    async def boom(task, name="sub-agent", **kwargs):
        calls.append(1)
        raise RuntimeError("boom")

    runner.run = boom  # type: ignore[method-assign]
    scheduler, _ = _scheduler(runner)

    with pytest.raises(RuntimeError, match="boom"):
        await scheduler.run(_keyed_request(), parent=None, observer=object(), emit=None)
    with pytest.raises(RuntimeError, match="boom"):
        await scheduler.run(_keyed_request(), parent=None, observer=object(), emit=None)

    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_cancelled_run_releases_the_key_for_retry():
    """Guard for the SUB-03 release path on cancellation."""
    runner = _Runner()
    calls: list[int] = []
    started = asyncio.Event()
    hang = asyncio.Event()

    async def block(task, name="sub-agent", **kwargs):
        calls.append(1)
        if len(calls) == 1:
            runner.run_calls.append({"task": task, "name": name, **kwargs})
            started.set()
            await hang.wait()
        return runner.run_result

    runner.run = block  # type: ignore[method-assign]
    scheduler, _ = _scheduler(runner)
    turn = asyncio.create_task(
        scheduler.run(_keyed_request(), parent=None, observer=object(), emit=None)
    )
    await started.wait()
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn

    result = await scheduler.run(
        _keyed_request(), parent=None, observer=object(), emit=None
    )

    assert result.status == "completed"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_completed_run_still_returns_the_cached_result():
    """Regression guard: the settle path must keep answering from the cache."""
    scheduler, runner = _scheduler()

    first = await scheduler.run(_keyed_request(), parent=None, observer=object(),
                                emit=None)
    second = await scheduler.run(_keyed_request(), parent=None, observer=object(),
                                 emit=None)

    assert first is second is runner.run_result
    assert len(runner.run_calls) == 1
