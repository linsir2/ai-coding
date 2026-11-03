"""The scheduling facade for delegated runs (P2-C1/C2).

C1 made one entry point, one slot pool and one read-only registry.  C2 adds
task-level durability: a journal, claim/lease/fence, stop/cancel trees,
recovery and idempotency keys.  The runner still executes children; this module
owns the *identity and lifecycle* around them.

Deliberately absent: an execution queue (C2 claims synchronously and gives each
call a cancellable task handle instead), merges (C3), cross-process leases.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, cast

from ai_coding.core.execution_slots import (
    ExecutionSlotPool,
    enter_child,
    holding_slot,
)
from ai_coding.core.merge_queue import (
    MergeJournalError,
    MergeQueueClosed,
    MergeQueueFull,
    MergeTicket,
)
from ai_coding.core.observers import EmitObserver
from ai_coding.core.research_fanout import ResearchQuestion
from ai_coding.core.task_journal import TERMINAL_EVENTS, TaskJournal
from ai_coding.core.task_registry import TaskRegistry, TaskView
from ai_coding.domain.orchestration import TaskRequest
from ai_coding.domain.subagent import (
    BackgroundTask,
    SubAgentPoolFull,
    SubagentResult,
)

__all__ = ["IdempotencyConflict", "Scheduler"]

#: The task that is currently executing; delegated children record it as
#: ``parent_id`` so a cancel tree can find them without guessing by call id.
current_task_id: ContextVar[str] = ContextVar("current_task_id", default="")

#: How often the one reaper checks leases (fractions of the ttl, clamped).
_MIN_REAP_SECONDS = 10.0
_MAX_REAP_SECONDS = 120.0


class IdempotencyConflict(RuntimeError):
    """The same idempotency key arrived with a different payload."""


@dataclass
class _MergeSource:
    """What ``Scheduler.merge`` resolved; ``error`` short-circuits the queue."""

    branch: str = ""
    base_commit: str | None = None
    task_id: str = ""
    run_id: str = ""
    name: str = ""
    session_id: str = ""
    commit: str | None = None
    commit_error: str | None = None
    error: str = ""


@dataclass
class _Record:
    task_id: str
    parent_id: str
    kind: str
    state: str = "queued"
    name: str = ""
    fence: int = 0
    lease_until: float = 0.0
    turn: int = 0
    session_id: str = ""
    run_id: str = ""
    key: str = ""
    digest: str = ""
    base_commit: str | None = None
    commit_error: str | None = None
    request: TaskRequest | None = None
    task: asyncio.Task[Any] | None = None
    started: float = field(default_factory=time.monotonic)


class Scheduler:
    """Facade over the runner, the fan-out, the slot pool and the journal."""

    def __init__(
        self,
        runner: Any,
        fanout: Any = None,
        worktree_manager: Any = None,
        slots: ExecutionSlotPool | None = None,
        registry: TaskRegistry | None = None,
        journal: TaskJournal | None = None,
        merge_queue: Any = None,
    ) -> None:
        self._runner = runner
        self._fanout = fanout if fanout is not None else self._default_fanout(runner)
        self._worktree_manager = worktree_manager
        if slots is None:
            slots = getattr(runner, "slots", None) or ExecutionSlotPool(
                max(1, int(getattr(runner, "max_concurrent", 4) or 4))
            )
        self.slots = slots
        self._registry = registry if registry is not None else TaskRegistry(runner)
        workspace = getattr(runner, "workspace", None) or "."
        self._journal = journal if journal is not None else TaskJournal(workspace)
        self._records: dict[str, _Record] = {}
        self._request_runs: dict[str, str] = {}
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._idem: dict[str, dict[str, Any]] = {}
        self._reaper: asyncio.Task[None] | None = None
        self._closed = False
        #: P2-C3: the one merge queue (None in tests/stacks without C3).
        self._merge_queue = merge_queue
        self._merge_inflight: dict[str, MergeTicket] = {}
        #: The runner calls back through this attribute (set by the builder).
        self.runner = runner

    # ---- properties -------------------------------------------------------

    @property
    def journal(self) -> TaskJournal:
        return self._journal

    @property
    def records(self) -> list[_Record]:
        return list(self._records.values())

    # ---- delegated runs ---------------------------------------------------

    async def run(
        self,
        request: TaskRequest,
        *,
        parent: Any = None,
        observer: Any = None,
        emit: Any = None,
    ) -> SubagentResult:
        """Run one child in a cancellable task under one slot."""
        cached = self._idem_lookup(request)
        if cached is not None:
            if cached.get("result") is not None:
                return cast(SubagentResult, cached["result"])
            raise IdempotencyConflict(
                f"idempotency key {request.idempotency_key!r} is already in flight"
            )
        record = self._begin(request, kind="child")
        started = time.monotonic()
        if observer is None and callable(emit):
            observer = EmitObserver(emit)
        token = current_task_id.set(request.id)
        task = asyncio.create_task(
            self._execute_child(request, record, parent, observer),
            name=f"task:{request.id}",
        )
        record.task = task
        self._tasks[request.id] = task
        try:
            result = await task
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._settle(record, status="cancelled", error="cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 — view must not stay running
            self._settle(record, status="error", error=str(exc))
            raise
        finally:
            current_task_id.reset(token)
            self._tasks.pop(request.id, None)
        self._settle(record, result=result)
        self._register_result(request, started, result)
        return result

    async def _execute_child(
        self,
        request: TaskRequest,
        record: _Record,
        parent: Any,
        observer: Any,
    ) -> SubagentResult:
        slot = None if holding_slot() else await self.slots.acquire(request.id)
        try:
            record.state = "running"
            self._journal.append(
                record.session_id, "running",
                task_id=record.task_id, parent_id=record.parent_id,
                kind=record.kind, fence=record.fence, run_id="",
                turn=record.turn, key=record.key, digest=record.digest,
            )
            with enter_child():
                return cast(
                    SubagentResult,
                    await self._runner.run(
                        request.task,
                        request.name,
                        worktree_manager=self._worktree_manager,
                        parent=parent,
                        parent_span=request.parent_span,
                        observer=observer,
                        task_id=request.id,
                        fence=record.fence,
                    ),
                )
        finally:
            if slot is not None:
                slot.release()

    async def spawn(
        self,
        request: TaskRequest,
        *,
        parent: Any = None,
        observer: Any = None,
        emit: Any = None,
    ) -> tuple[str, str]:
        """Reserve a slot, start a background child, return its two ids."""
        cached = self._idem_lookup(request)
        if cached is not None:
            if cached.get("run_id"):
                return cast(tuple[str, str], (
                    cached.get("request_id", request.id), cached["run_id"],
                ))
            raise IdempotencyConflict(
                f"idempotency key {request.idempotency_key!r} is already in flight"
            )
        record = self._begin(request, kind="child")
        slot = self.slots.try_reserve(request.id)
        if slot is None:
            self._settle(record, status="error", error="pool full")
            raise SubAgentPoolFull(
                "sub-agent concurrency exhausted: "
                f"{self.slots.max_slots} execution slots busy; "
                "retry later or raise ai.max_execution_slots"
            )
        try:
            run_id = self._runner.spawn_background(
                request.task,
                request.name,
                worktree_manager=self._worktree_manager,
                parent=parent,
                parent_span=request.parent_span,
                observer=observer,
                emit=emit,
                origin_session=request.origin_session,
                slot=slot,
                task_id=request.id,
                fence=record.fence,
            )
        except BaseException:
            slot.release()
            self._settle(record, status="error", error="start failed")
            raise
        record.run_id = run_id
        record.state = "running"
        self._request_runs[request.id] = run_id
        self._idem_put(
            record.key, record.digest, request_id=request.id, run_id=run_id,
        )
        if len(self._request_runs) > 200:
            self._request_runs.pop(next(iter(self._request_runs)))
        self._journal.append(
            record.session_id, "running",
            task_id=record.task_id, parent_id=record.parent_id,
            kind=record.kind, fence=record.fence, run_id=run_id,
            key=record.key, digest=record.digest,
        )
        return request.id, run_id

    async def fanout(
        self,
        questions: list[ResearchQuestion],
        shared_context: str = "",
        *,
        parent: Any = None,
        observer: Any = None,
        parent_span: str = "",
        origin_session: str = "",
        idempotency_key: str = "",
    ) -> list[Any]:
        """Run the existing fan-out as one cancellable task and journal it."""
        token = parent_span or uuid.uuid4().hex[:12]
        parent_record = self._begin_fanout(token, origin_session, idempotency_key)
        keys = {
            question.id: f"{idempotency_key}:{question.id}" if idempotency_key else ""
            for question in questions
        }
        digests = {
            question.id: _digest(question.question, question.scope, shared_context)
            for question in questions
        }
        cached = self._cached_fanout(keys, digests)
        if cached is not None:
            return cached
        records: dict[str, _Record] = {}
        for question in questions:
            record = self._begin(
                _fanout_request(question, token, origin_session, keys[question.id],
                                digests[question.id]),
                kind="fanout",
            )
            records[question.id] = record
        self._set_views_running(questions, token)
        task = asyncio.create_task(
            self._fanout.run(
                questions, shared_context=shared_context, parent=parent,
                observer=observer, parent_span=parent_span,
            ),
            name=f"fanout:{token}",
        )
        self._tasks[token] = task
        parent_record.task = task
        set_token = current_task_id.set(token)
        try:
            packets = await task
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            for question in questions:
                self._settle(records[question.id], status="cancelled",
                             error="cancelled")
            self._settle(parent_record, status="cancelled", error="cancelled")
            raise
        except Exception as exc:  # noqa: BLE001
            for question in questions:
                self._settle(records[question.id], status="error", error=str(exc))
            self._settle(parent_record, status="error", error=str(exc))
            raise
        finally:
            current_task_id.reset(set_token)
            self._tasks.pop(token, None)
        ended = time.monotonic()
        for packet in packets:
            packet_record = records.get(packet.id)
            if packet_record is not None:
                self._settle(
                    packet_record, status=packet.status, error=packet.error,
                    usage=dict(packet.usage) if packet.usage is not None else None,
                    truncated=packet.truncated,
                )
            view_id = f"fanout:{token}:{packet.id}"
            self._registry.register(
                TaskView(
                    id=view_id,
                    name=f"research:{packet.id}",
                    kind="fanout",
                    status=packet.status,
                    started=ended,
                    ended=ended,
                    usage=dict(packet.usage) if packet.usage is not None else None,
                    error=packet.error or None,
                    truncated=packet.truncated,
                    origin_session=origin_session,
                )
            )
        self._settle(parent_record, status="completed")
        self._store_fanout(keys, digests, packets, token)
        return cast(list[Any], packets)

    # ---- explicit merge ---------------------------------------------------

    async def merge(
        self, target: str, verify_cmd: str | None = None, *, session_id: str = ""
    ) -> dict[str, Any]:
        """Queue one explicit merge and wait for its terminal outcome."""
        if self._closed:
            return {"status": "unavailable"}
        if self._merge_queue is None:
            return {"status": "unavailable"}
        source = self._merge_source(target, session_id)
        if source.error:
            return {"status": source.error}
        branch = source.branch
        existing = self._merge_inflight.get(branch)
        if existing is not None:
            await existing.done.wait()
            return existing.outcome or {"status": existing.status}

        ticket = MergeTicket(
            key=branch,
            source_task_id=source.task_id,
            source_run_id=source.run_id,
            source_name=source.name,
            branch=branch,
            base_commit=source.base_commit,
            verify_cmd=(verify_cmd or "").strip() or None,
            session_id=source.session_id or session_id,
        )
        ticket.on_update = self._sync_merge_view
        self._merge_inflight[branch] = ticket
        self._sync_merge_view(ticket)
        try:
            self._merge_queue.enqueue(ticket)
        except MergeQueueClosed:
            self._fail_ticket(ticket, "unavailable", "closed")
            return ticket.outcome or {"status": "unavailable"}
        except MergeQueueFull:
            self._fail_ticket(ticket, "busy", "busy")
            return ticket.outcome or {"status": "busy"}
        except MergeJournalError:
            self._fail_ticket(ticket, "journal_failed", "journal_failed")
            return ticket.outcome or {"status": "journal_failed"}
        except Exception as exc:  # noqa: BLE001 — a bad queue must not hang the caller
            self._fail_ticket(ticket, "merge_failed", f"enqueue: {exc}")
            return ticket.outcome or {"status": "merge_failed"}

        return cast(dict[str, Any], await self._merge_queue.wait(ticket))

    def merge_status(self, target: str) -> dict[str, Any]:
        """The read-only status of one merge ticket (``/tasks stop`` uses it)."""
        target = (target or "").strip()
        views = [
            view for view in self._registry.list() if view.kind == "merge"
        ]
        exact = [
            view for view in views
            if view.id == target or view.name == target or view.branch == target
        ]
        matches = exact or [
            view for view in views
            if view.run_id and view.run_id.startswith(target)
        ]
        if len(matches) == 1:
            return matches[0].to_dict()
        if len(matches) > 1:
            return {"status": "ambiguous"}
        return {"status": "not_found"}

    def _merge_source(self, target: str, session_id: str) -> _MergeSource:
        """Resolve a child task to its branch/base; never guesses across sessions."""
        target = (target or "").strip()
        if not target:
            return _MergeSource(error="not_found")

        views = [
            view for view in self._registry.list() if view.kind != "merge"
        ]
        if session_id:
            views = [
                view for view in views
                if not view.origin_session or view.origin_session == session_id
            ]
        exact = [
            view for view in views
            if view.id == target or view.run_id == target or view.name == target
            or view.branch == target
        ]
        matches = exact or [
            view for view in views if view.run_id and view.run_id.startswith(target)
        ]
        view = matches[0] if len(matches) == 1 else None
        if len(matches) > 1:
            return _MergeSource(error="ambiguous")

        records = [
            record for record in self._journal.records()
            if record.kind == "child"
        ]
        if session_id:
            records = [
                record for record in records
                if not record.session_id or record.session_id == session_id
            ]
        record_matches = [
            record for record in records
            if record.task_id == target or record.run_id == target
            or record.name == target or record.branch == target
        ]
        if not exact and not record_matches:
            record_matches = [
                record for record in records
                if record.run_id and record.run_id.startswith(target)
            ]
        record = (
            record_matches[0] if len(record_matches) == 1 else None
        )
        if len(record_matches) > 1:
            return _MergeSource(error="ambiguous")
        if view is None and record is None:
            return _MergeSource(error="not_found")

        run_id = (view.run_id if view is not None else "") or (
            record.run_id if record is not None else ""
        )
        close_info: dict[str, Any] = {}
        if run_id:
            info = getattr(self._runner, "close_info", None)
            if callable(info):
                try:
                    close_info = dict(info(run_id) or {})
                except Exception:  # noqa: BLE001 — meta is best-effort
                    close_info = {}

        if record is not None and not record.is_terminal:
            return _MergeSource(error="not_settled")
        if (
            record is None and view is not None
            and (view.is_running or view.needs_reconciliation)
        ):
            return _MergeSource(error="not_settled")

        commit_error = (
            (view.commit_error if view is not None else None)
            or (record.commit_error if record is not None else None)
            or close_info.get("commitError")
        )
        if commit_error:
            return _MergeSource(error="commit_failed")

        branch = (
            (view.branch if view is not None else None)
            or (record.branch if record is not None else None)
            or close_info.get("worktreeBranch")
        )
        if not branch:
            return _MergeSource(error="nothing_to_merge")
        base_commit = (
            (view.base_commit if view is not None else None)
            or (record.base_commit if record is not None else None)
            or close_info.get("baseCommit")
        )
        return _MergeSource(
            branch=str(branch),
            base_commit=base_commit,
            task_id=(
                (record.task_id if record is not None else "")
                or (view.id if view is not None else "")
            ),
            run_id=run_id,
            name=(
                (view.name if view is not None else "")
                or (record.name if record is not None else "")
            ),
            session_id=(
                (view.origin_session if view is not None else "")
                or (record.session_id if record is not None else "")
                or session_id
            ),
            commit=(
                (view.commit if view is not None else None)
                or (record.commit if record is not None else None)
                or close_info.get("commit")
            ),
        )

    def _sync_merge_view(self, ticket: MergeTicket) -> None:
        outcome = ticket.outcome or {}
        view = TaskView(
            id=f"merge:{ticket.branch}",
            run_id="",
            name=f"merge:{ticket.branch}",
            kind="merge",
            status=ticket.status,
            started=ticket.started,
            ended=ticket.ended,
            branch=ticket.branch,
            base_commit=ticket.base_commit,
            commit=outcome.get("commit"),
            verified=outcome.get("verified"),
            error=outcome.get("error"),
            origin_session=ticket.session_id,
        )
        self._registry.register(view)
        if ticket.status not in ("queued", "merging"):
            self._merge_inflight.pop(ticket.branch, None)

    def _fail_ticket(
        self, ticket: MergeTicket, status: str, error: str
    ) -> None:
        if ticket.done.is_set():
            return
        ticket.status = status
        ticket.ended = time.monotonic()
        ticket.outcome = {"status": status, "error": error,
                          "attempts": ticket.attempts}
        self._journal.append(
            ticket.session_id, "merge_failed",
            task_id=f"merge:{ticket.branch}",
            parent_id=ticket.source_task_id,
            kind="merge", name=f"merge:{ticket.branch}",
            branch=ticket.branch, base_commit=ticket.base_commit,
            run_id=ticket.source_run_id, key=ticket.key, error=error,
        )
        self._sync_merge_view(ticket)
        ticket.done.set()

    # ---- control API (delegated to the runner) -----------------------------

    async def send(self, target: str, text: str, key: str = "") -> tuple[str, str]:
        digest = _digest(text)
        cached = self._idem_get(key, digest)
        if cached is not None:
            return cast(tuple[str, str], (cached.get("status", "queued"), target))
        result = cast(
            tuple[str, str],
            await self._runner.send_message(self._run_id_for(target), text),
        )
        self._idem_put(key, digest, status=result[0])
        return result

    async def interrupt(self, target: str) -> tuple[str, str]:
        return cast(
            tuple[str, str], await self._runner.interrupt(self._run_id_for(target))
        )

    async def resume(self, target: str) -> tuple[str, str]:
        record = self._record_for(target)
        if record is not None and record.state in TERMINAL_EVENTS:
            return "stopped", record.run_id or target
        return cast(
            tuple[str, str], await self._runner.resume(self._run_id_for(target))
        )

    async def close(self, target: str) -> bool:
        return bool(await self._runner.close(self._run_id_for(target)))

    async def wait(self, target: str, timeout_ms: int = 30_000) -> tuple[str, int]:
        return cast(
            tuple[str, int],
            await self._runner.wait(self._run_id_for(target), timeout_ms),
        )

    def close_info(self, target: str) -> dict[str, Any]:
        return cast(dict[str, Any], dict(self._runner.close_info(
            self._run_id_for(target)
        )))

    def stop(self, target: str) -> bool:
        """Kill a run for good; distinct from ``interrupt`` (a turn cut).

        Synchronous because ``/tasks stop`` is a command handler.  The terminal
        journal latch and the fence bump happen immediately; the runner's
        ``stop_run`` is scheduled on the running loop (or falls back to the
        synchronous ``cancel`` outside one).
        """
        record = self._record_for(target)
        run_id = self._request_runs.get(target) or self._resolve_run_id(target)
        task = self._tasks.get(record.task_id) if record is not None else None
        if task is not None and not task.done():
            task.cancel()
        if record is not None:
            self._close_record(record, "stopped")
        if run_id:
            stop_run = getattr(self._runner, "stop_run", None)
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if callable(stop_run) and loop is not None:
                loop.create_task(stop_run(run_id))
            else:
                self._runner.cancel(run_id)
        return bool(run_id or record is not None)

    async def cancel_tree(self, root: str) -> int:
        """Stop every task whose ``parent_id`` chain starts at ``root``."""
        return await self._cancel_tree(root, close=False)

    async def close_tree(self, root: str) -> int:
        """Close every task in the tree (keeps them explicitly resumable)."""
        if self._record_for(root) is None and not self._descendants(root):
            # A handle created outside a journaled request (tests, an old
            # process): keep the pre-C2 single-run close semantics.
            return 1 if await self._runner.close(self._run_id_for(root)) else 0
        return await self._cancel_tree(root, close=True)

    async def _cancel_tree(self, root: str, *, close: bool) -> int:
        children = self._descendants(root)
        count = 0
        for record in children:
            if record.state in TERMINAL_EVENTS:
                continue
            if close:
                self._close_record(record, "closed")
                await self._run_close(record)
            else:
                self.stop(record.task_id)
            count += 1
        root_record = self._record_for(root)
        if root_record is not None and root_record.state not in TERMINAL_EVENTS:
            if close:
                self._close_record(root_record, "closed")
                await self._run_close(root_record)
            else:
                self.stop(root_record.task_id)
            count += 1
        return count

    async def _run_close(self, record: _Record) -> None:
        if record.run_id:
            close = getattr(self._runner, "close", None)
            if callable(close):
                await close(record.run_id)

    # ---- registry / journal callbacks -------------------------------------

    def accepts(self, task_id: str, fence: int) -> bool:
        """Whether a late settlement still owns the task (fence gate)."""
        if not task_id:
            return True
        record = self._records.get(task_id)
        if record is None:
            return True
        return record.fence == fence and record.state not in TERMINAL_EVENTS

    def on_turn_start(self, task_id: str, fence: int, turn: int) -> bool:
        if not self.accepts(task_id, fence):
            return False
        record = self._records.get(task_id)
        if record is None:
            return True
        record.turn = turn
        record.state = "running"
        self._journal.append(
            record.session_id, "running", task_id=task_id,
            parent_id=record.parent_id, kind=record.kind, fence=fence,
            run_id=record.run_id, turn=turn, key=record.key, digest=record.digest,
        )
        return True

    def on_runner_event(
        self,
        task_id: str,
        run_id: str,
        status: str,
        fence: int = 0,
        *,
        pending: bool = False,
        branch: str | None = None,
        commit: str | None = None,
        base_commit: str | None = None,
        commit_error: str | None = None,
    ) -> bool:
        """Record a runner settlement; False means the fence went stale."""
        record = self._record_for(task_id or run_id)
        if record is not None and not self.accepts(record.task_id, fence):
            return False
        # A completed/error/timeout/max_iterations result is a *settlement*;
        # only an interrupted turn is the recoverable non-terminal case.
        # Mapping every non-terminal status to ``interrupted`` made a finished
        # background child look unfinished after a restart (C3 needs journal to
        # be the merge index, so that had to be fixed at the root).
        event = (
            status if status in TERMINAL_EVENTS
            else "interrupted" if status == "interrupted"
            else "settled"
        )
        if record is not None:
            record.run_id = run_id or record.run_id
            self._settle(
                record, status=event, branch=branch, commit=commit,
                base_commit=base_commit, commit_error=commit_error,
            )
            if pending:
                self._journal.append(
                    record.session_id, "pending", task_id=record.task_id,
                    parent_id=record.parent_id, kind=record.kind,
                    name=record.name, fence=record.fence, run_id=record.run_id,
                    key=record.key, digest=record.digest,
                )
        return True

    # ---- recovery ----------------------------------------------------------

    def recover(self) -> list[TaskView]:
        """Mark journal-open tasks as needs_reconciliation; never re-run them."""
        recovered: list[TaskView] = []
        for state in self._journal.pending():
            if not state.task_id or state.task_id in self._records:
                continue
            is_merge = state.kind == "merge"
            view = TaskView(
                id=state.task_id,
                run_id=state.run_id,
                # A recovered child stays "recovered" so the sub-agent readers
                # still see it; only a merge ticket is a merge row.
                name=state.name or (
                    state.task_id if is_merge else state.task_id[:12]
                ),
                kind="merge" if is_merge else "recovered",
                status="needs_reconciliation",
                started=0.0,
                ended=time.monotonic(),
                origin_session=state.session_id,
                branch=state.branch,
                commit=state.commit,
                base_commit=state.base_commit,
                needs_reconciliation=True,
                error="recovered after restart; inspect before retrying",
            )
            self._registry.register(view)
            recovered.append(view)
        return recovered

    # ---- internals --------------------------------------------------------

    def _begin(self, request: TaskRequest, *, kind: str) -> _Record:
        record = _Record(
            task_id=request.id,
            parent_id=request.parent_id or current_task_id.get(),
            kind=kind,
            name=request.name,
            session_id=request.origin_session,
            key=request.idempotency_key,
            digest=request.payload_digest,
            request=request,
        )
        self._records[record.task_id] = record
        self._claim(record)
        self._ensure_reaper()
        return record

    def _begin_fanout(
        self, token: str, origin_session: str, key: str
    ) -> _Record:
        record = _Record(
            task_id=token,
            parent_id=current_task_id.get(),
            kind="fanout",
            name="fanout",
            session_id=origin_session,
            key=key,
            digest=key,
        )
        self._records[token] = record
        self._claim(record)
        self._ensure_reaper()
        return record

    def _claim(self, record: _Record) -> None:
        record.fence += 1
        record.state = "claimed"
        ttl = self._ttl()
        record.lease_until = time.monotonic() + ttl
        self._journal.append(
            record.session_id, "queued", task_id=record.task_id,
            parent_id=record.parent_id, kind=record.kind, name=record.name,
            fence=record.fence, run_id=record.run_id, key=record.key,
            digest=record.digest,
        )
        self._journal.append(
            record.session_id, "claimed", task_id=record.task_id,
            parent_id=record.parent_id, kind=record.kind, name=record.name,
            fence=record.fence, run_id=record.run_id, key=record.key,
            digest=record.digest,
            claim={"owner": "scheduler", "lease_until": record.lease_until,
                   "fence": record.fence},
        )

    def _ttl(self) -> float:
        timeout = float(getattr(self._runner, "timeout_seconds", 0.0) or 1200.0)
        return max(60.0, 2.0 * timeout)

    def _settle(
        self,
        record: _Record,
        *,
        result: SubagentResult | None = None,
        status: str | None = None,
        error: str | None = None,
        usage: dict[str, int] | None = None,
        truncated: bool = False,
        branch: str | None = None,
        commit: str | None = None,
        base_commit: str | None = None,
        commit_error: str | None = None,
    ) -> None:
        if record.state in TERMINAL_EVENTS:
            return  # a stop/close already closed this task; never re-open it
        status = status or (result.status if result is not None else "settled")
        event = status if status in TERMINAL_EVENTS else "settled"
        record.state = event
        if base_commit is not None:
            record.base_commit = base_commit
        if commit_error is not None:
            record.commit_error = commit_error
        self._journal.append(
            record.session_id, event, task_id=record.task_id,
            parent_id=record.parent_id, kind=record.kind, name=record.name,
            fence=record.fence, run_id=record.run_id, key=record.key,
            digest=record.digest,
            error=error or (result.error if result is not None else None),
            branch=branch or (result.worktree_branch if result is not None else None),
            commit=commit or (result.commit if result is not None else None),
            base_commit=base_commit or (
                result.base_commit if result is not None else None
            ),
            commit_error=commit_error or (
                result.commit_error if result is not None else None
            ),
        )
        if record.request is not None and result is not None:
            self._idem_put(
                record.key, record.digest, request_id=record.task_id,
                run_id=record.run_id, result=result,
            )

    def _register_result(
        self, request: TaskRequest, started: float, result: SubagentResult
    ) -> None:
        ended = time.monotonic()
        self._registry.register(
            TaskView(
                id=request.id,
                name=request.name,
                kind=request.kind,
                status=result.status,
                started=started,
                ended=ended,
                usage=dict(result.usage) if result.usage is not None else None,
                ledger_path=result.ledger_path,
                branch=result.worktree_branch,
                commit=result.commit,
                base_commit=result.base_commit,
                changed_paths=tuple(result.changed_paths),
                conflicts=tuple(result.conflicts),
                commit_error=result.commit_error,
                degraded=result.degraded,
                error=result.error or None,
                truncated=bool(result.truncated),
                origin_session=request.origin_session,
            )
        )

    def _set_views_running(
        self, questions: list[ResearchQuestion], token: str
    ) -> None:
        started = time.monotonic()
        for question in questions:
            self._registry.register(
                TaskView(
                    id=f"fanout:{token}:{question.id}",
                    name=f"research:{question.id}",
                    kind="fanout",
                    status="running",
                    started=started,
                )
            )

    def _record_for(self, target: str) -> _Record | None:
        if not target:
            return None
        record = self._records.get(target)
        if record is not None:
            return record
        for record in self._records.values():
            if record.run_id and record.run_id == target:
                return record
        for record in self._records.values():
            if record.run_id and record.run_id.startswith(target):
                return record
        return None

    def _resolve_run_id(self, prefix: str) -> str | None:
        if not prefix:
            return None
        mapped = self._request_runs.get(prefix)
        if mapped:
            return self._resolved_run_id(mapped)
        view = self._registry.get(prefix)
        if view is not None and view.run_id:
            return self._resolved_run_id(view.run_id)
        resolve = getattr(self._runner, "resolve_run_id", None)
        return resolve(prefix) if callable(resolve) else None

    def resolve_run_id(self, prefix: str) -> str | None:
        """Resolve a request id, journal key or printed run-id prefix."""
        record = self._record_for(prefix)
        if record is not None and record.run_id:
            return self._resolved_run_id(record.run_id)
        return self._resolve_run_id(prefix)

    def _run_id_for(self, target: str) -> str:
        return self.resolve_run_id(target) or target

    def _resolved_run_id(self, run_id: str) -> str:
        resolve = getattr(self._runner, "resolve_run_id", None)
        return str(resolve(run_id) or run_id) if callable(resolve) else run_id

    def resolve_target(self, target: str) -> tuple[str, str, list[str]]:
        """Address a child by request id, run-id prefix, or unique name."""
        mapped = self._request_runs.get(target)
        if mapped:
            classify = getattr(self._runner, "resolve_target", None)
            if callable(classify):
                return cast(tuple[str, str, list[str]], classify(mapped))
            return "ok", mapped, []
        record = self._record_for(target)
        if record is not None and record.run_id:
            classify = getattr(self._runner, "resolve_target", None)
            if callable(classify):
                return cast(tuple[str, str, list[str]], classify(record.run_id))
            return "ok", record.run_id, []
        classify = getattr(self._runner, "resolve_target", None)
        if callable(classify):
            return cast(tuple[str, str, list[str]], classify(target))
        return "unknown", "", []

    def _idem_lookup(self, request: TaskRequest) -> dict[str, Any] | None:
        key = request.idempotency_key
        digest = request.payload_digest
        if not key:
            return None
        cached = self._idem.get(key)
        if cached is None:
            return None
        if digest and cached.get("digest") and digest != cached["digest"]:
            raise IdempotencyConflict(
                f"idempotency key {key!r} arrived with a different payload"
            )
        return cached

    def _idem_get(self, key: str, digest: str) -> dict[str, Any] | None:
        if not key:
            return None
        cached = self._idem.get(key)
        if cached is None:
            return None
        if digest and cached.get("digest") and digest != cached["digest"]:
            raise IdempotencyConflict(
                f"idempotency key {key!r} arrived with a different payload"
            )
        return cached

    def _idem_put(self, key: str, digest: str, **payload: Any) -> None:
        if not key:
            return
        entry = self._idem.setdefault(key, {})
        entry.update(payload)
        entry["digest"] = digest
        if len(self._idem) > 200:
            self._idem.pop(next(iter(self._idem)))

    def _cached_fanout(
        self, keys: dict[str, str], digests: dict[str, str]
    ) -> list[Any] | None:
        rows = [self._idem.get(key) for key in keys.values() if key]
        if not rows or any(row is None for row in rows):
            return None
        for question_id, key in keys.items():
            row = self._idem[key]
            if row.get("digest") and row["digest"] != digests[question_id]:
                raise IdempotencyConflict(
                    f"fanout question {question_id!r} arrived with a different payload"
                )
        first = rows[0] if rows else None
        packets = first.get("packets") if first is not None else None
        return cast(list[Any], packets) if packets is not None else None

    def _store_fanout(
        self,
        keys: dict[str, str],
        digests: dict[str, str],
        packets: list[Any],
        token: str,
    ) -> None:
        by_id = {packet.id: packet for packet in packets}
        for question_id, key in keys.items():
            self._idem_put(
                key, digests[question_id], request_id=f"fanout:{token}:{question_id}",
                packets=[by_id.get(question_id)] if by_id.get(question_id) else [],
            )

    def _descendants(self, root: str) -> list[_Record]:
        out: list[_Record] = []
        frontier = [root]
        seen: set[str] = set()
        while frontier:
            parent = frontier.pop()
            for record in self._records.values():
                if record.parent_id == parent and record.task_id not in seen:
                    seen.add(record.task_id)
                    out.append(record)
                    frontier.append(record.task_id)
        return out

    def _close_record(self, record: _Record, event: str) -> None:
        record.fence += 1
        record.state = event
        self._journal.append(
            record.session_id, event, task_id=record.task_id,
            parent_id=record.parent_id, kind=record.kind, fence=record.fence,
            run_id=record.run_id, key=record.key, digest=record.digest,
        )

    # ---- reaper ------------------------------------------------------------

    def _ensure_reaper(self) -> None:
        if self._closed or self._reaper is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._reaper = loop.create_task(self._reap_loop(), name="task-reaper")

    async def _reap_loop(self) -> None:
        interval = min(
            _MAX_REAP_SECONDS, max(_MIN_REAP_SECONDS, self._ttl() / 3.0)
        )
        while not self._closed:
            await asyncio.sleep(interval)
            try:
                self._renew()
            except Exception:  # noqa: BLE001 — one bad pass must not kill the driver
                continue

    def _renew(self) -> None:
        now = time.monotonic()
        for record in list(self._records.values()):
            if record.state in TERMINAL_EVENTS:
                continue
            record.lease_until = now + self._ttl()
            self._journal.append(
                record.session_id, "renewed", task_id=record.task_id,
                parent_id=record.parent_id, kind=record.kind,
                fence=record.fence, run_id=record.run_id,
                claim={"owner": "scheduler", "lease_until": record.lease_until,
                       "fence": record.fence},
            )
            self._journal.append(
                record.session_id, record.state, task_id=record.task_id,
                parent_id=record.parent_id, kind=record.kind,
                fence=record.fence, run_id=record.run_id,
            )

    async def aclose(self) -> None:
        self._closed = True
        if self._reaper is not None:
            self._reaper.cancel()
            await asyncio.gather(self._reaper, return_exceptions=True)
            self._reaper = None
        if self._merge_queue is not None:
            try:
                await self._merge_queue.aclose()
            except Exception:  # noqa: BLE001 — shutdown is best-effort
                pass

    @staticmethod
    def _default_fanout(runner: Any) -> Any:
        from ai_coding.core.research_fanout import ResearchFanOut

        return ResearchFanOut(runner)

    def task_list(self) -> list[BackgroundTask]:
        lister = getattr(self._runner, "task_list", None)
        return list(lister()) if callable(lister) else []

    def tasks(self) -> TaskRegistry:
        return self._registry


# ---- helpers ---------------------------------------------------------------


def _digest(*parts: Any) -> str:
    """A stable digest that never includes a random id."""
    text = json.dumps([str(part) for part in parts], ensure_ascii=False,
                      sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fanout_request(
    question: ResearchQuestion,
    token: str,
    origin_session: str,
    key: str,
    digest: str,
) -> TaskRequest:
    from ai_coding.domain.subagent import SubagentBrief, SubagentTask

    return TaskRequest.for_child(
        SubagentTask(brief=SubagentBrief(
            objective=question.question, scope=tuple(question.scope),
        )),
        name=f"research:{question.id}",
        origin_session=origin_session,
        parent_id=token,
        idempotency_key=key,
        payload_digest=digest,
    )
