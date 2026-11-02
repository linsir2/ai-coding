"""The one scheduling facade for delegated runs (P2-C1).

Scheduler owns no execution machinery: the runner still runs children, the
research fan-out still builds its briefs and reports, the worktree manager is
the runner's, and the registry is read-only.  What this module adds is the
*identity* of a delegated run (``TaskRequest``), one admission point, and one
place that records the final task view.

Deliberately absent: journal, leases, fences, timeout policy and merges (C2/C3).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, cast

from ai_coding.core.execution_slots import (
    ExecutionSlotPool,
    enter_child,
    holding_slot,
)
from ai_coding.core.observers import EmitObserver
from ai_coding.core.research_fanout import ResearchQuestion
from ai_coding.core.task_registry import TaskRegistry, TaskView
from ai_coding.domain.orchestration import TaskRequest
from ai_coding.domain.subagent import (
    BackgroundTask,
    SubAgentPoolFull,
    SubagentResult,
)

__all__ = ["Scheduler"]


class Scheduler:
    """Facade over the runner, the fan-out, the slot pool and the registry."""

    def __init__(
        self,
        runner: Any,
        fanout: Any = None,
        worktree_manager: Any = None,
        slots: ExecutionSlotPool | None = None,
        registry: TaskRegistry | None = None,
    ) -> None:
        self._runner = runner
        self._fanout = (
            fanout if fanout is not None else self._default_fanout(runner)
        )
        self._worktree_manager = worktree_manager
        if slots is None:
            # Prefer the runner's own pool: a facade built around an existing
            # runner must not give it a second, independent capacity table.
            slots = getattr(runner, "slots", None) or ExecutionSlotPool(
                max(1, int(getattr(runner, "max_concurrent", 4) or 4))
            )
        self.slots = slots
        self._registry = registry if registry is not None else TaskRegistry(runner)
        #: request id -> run id.  The background row is the projection, but its
        #: id is the run id; this map is how a caller's request id resolves back
        #: to the handle without storing a second permanent task view.
        self._request_runs: dict[str, str] = {}

    # ---- properties -------------------------------------------------------

    @property
    def runner(self) -> Any:
        return self._runner

    # ---- delegated runs ---------------------------------------------------

    async def run(
        self,
        request: TaskRequest,
        *,
        parent: Any = None,
        observer: Any = None,
        emit: Any = None,
    ) -> SubagentResult:
        """Queue for one slot, run the child under it, then record the view."""
        nested = holding_slot()
        slot = None if nested else await self.slots.acquire(request.id)
        started = time.monotonic()
        if observer is None and callable(emit):
            observer = EmitObserver(emit)
        try:
            with enter_child():
                result: SubagentResult = await self._runner.run(
                    request.task,
                    request.name,
                    worktree_manager=self._worktree_manager,
                    parent=parent,
                    parent_span=request.parent_span,
                    observer=observer,
                    origin_session=request.origin_session,
                )
        except asyncio.CancelledError:
            self._register_result(request, started, None, status="cancelled",
                                  error="cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 — the view must not stay running
            self._register_result(
                request, started, None, status="error", error=str(exc)
            )
            raise
        else:
            self._register_result(request, started, result)
            return result
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
        slot = self.slots.try_reserve(request.id)
        if slot is None:
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
            )
        except BaseException:
            slot.release()
            raise
        # No separate creation view: the runner's background row is the live
        # projection and the only source readers need.  A second view would
        # outlive a pruned/closed row and read as a phantom "running" task.
        # The id pair still has to resolve for wait/stop, so remember it here.
        self._request_runs[request.id] = run_id
        if len(self._request_runs) > 200:
            self._request_runs.pop(next(iter(self._request_runs)))
        return request.id, run_id

    async def fanout(
        self,
        questions: list[ResearchQuestion],
        shared_context: str = "",
        *,
        parent: Any = None,
        observer: Any = None,
        parent_span: str = "",
    ) -> list[Any]:
        """Run the existing fan-out and project one view per question."""
        started = time.monotonic()
        # Question ids are only unique within one call.  Prefix with the
        # invocation so two concurrent fan-outs cannot overwrite each other's
        # rows in the process-wide registry.
        token = parent_span or uuid.uuid4().hex[:12]
        view_ids = {question.id: f"fanout:{token}:{question.id}" for question in questions}
        for question in questions:
            self._registry.register(
                TaskView(
                    id=view_ids[question.id],
                    name=f"research:{question.id}",
                    kind="fanout",
                    status="running",
                    started=started,
                )
            )
        try:
            packets = await self._fanout.run(
                questions,
                shared_context=shared_context,
                parent=parent,
                observer=observer,
                parent_span=parent_span,
            )
        except asyncio.CancelledError:
            for question in questions:
                self._registry.update(
                    view_ids[question.id], status="cancelled",
                    ended=time.monotonic(),
                )
            raise
        except Exception as exc:  # noqa: BLE001 — the views must not stay running
            ended = time.monotonic()
            for question in questions:
                self._registry.update(
                    view_ids[question.id], status="error", error=str(exc),
                    ended=ended,
                )
            raise
        ended = time.monotonic()
        for packet in packets:
            view_id = view_ids.get(packet.id) or f"fanout:{token}:{packet.id}"
            view = TaskView(
                id=view_id,
                name=f"research:{packet.id}",
                kind="fanout",
                status=packet.status,
                started=started,
                ended=ended,
                usage=dict(packet.usage) if packet.usage is not None else None,
                error=packet.error or None,
                truncated=packet.truncated,
            )
            existing = self._registry.get(view_id)
            if existing is not None and existing.kind == "fanout":
                view.started = existing.started
            self._registry.register(view)
        return cast(list[Any], packets)

    # ---- control API (delegated to the runner) -----------------------------

    async def send(self, target: str, text: str) -> tuple[str, str]:
        return cast(
            tuple[str, str],
            await self._runner.send_message(self._run_id_for(target), text),
        )

    async def interrupt(self, target: str) -> tuple[str, str]:
        return cast(
            tuple[str, str], await self._runner.interrupt(self._run_id_for(target))
        )

    async def resume(self, target: str) -> tuple[str, str]:
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
        """Kill a background run; distinct from ``interrupt`` (a turn cut)."""
        return bool(self._runner.cancel(self._run_id_for(target)))

    def resolve_target(self, target: str) -> tuple[str, str, list[str]]:
        """Address a child by request id, run-id prefix, or unique name."""
        mapped = self._request_runs.get(target)
        if mapped:
            classify = getattr(self._runner, "resolve_target", None)
            if callable(classify):
                return cast(tuple[str, str, list[str]], classify(mapped))
            return "ok", mapped, []
        view = self._registry.get(target)
        if view is not None and view.run_id:
            run_id = self._resolved_run_id(view.run_id)
            classify = getattr(self._runner, "resolve_target", None)
            if callable(classify):
                kind, resolved, candidates = classify(run_id)
                if kind != "unknown":
                    return cast(tuple[str, str, list[str]], (kind, resolved, candidates))
            return "ok", run_id, []
        classify = getattr(self._runner, "resolve_target", None)
        if callable(classify):
            return cast(tuple[str, str, list[str]], classify(target))
        return "unknown", "", []

    def resolve_run_id(self, prefix: str) -> str | None:
        """Resolve a request id or a printed run-id prefix to the full run id."""
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

    def task_list(self) -> list[BackgroundTask]:
        """The runner's background projection (kept for old readers)."""
        lister = getattr(self._runner, "task_list", None)
        return list(lister()) if callable(lister) else []

    def tasks(self) -> TaskRegistry:
        """The read-only registry every reader formats from."""
        return self._registry

    # ---- internals ---------------------------------------------------------

    @staticmethod
    def _default_fanout(runner: Any) -> Any:
        from ai_coding.core.research_fanout import ResearchFanOut

        return ResearchFanOut(runner)

    def _run_id_for(self, target: str) -> str:
        return self.resolve_run_id(target) or target

    def _resolved_run_id(self, run_id: str) -> str:
        resolve = getattr(self._runner, "resolve_run_id", None)
        if callable(resolve):
            return str(resolve(run_id) or run_id)
        return run_id

    def _register_result(
        self,
        request: TaskRequest,
        started: float,
        result: SubagentResult | None,
        *,
        status: str | None = None,
        error: str | None = None,
    ) -> None:
        ended = time.monotonic()
        self._registry.register(
            TaskView(
                id=request.id,
                run_id="",
                name=request.name,
                kind=request.kind,
                status=status or (result.status if result is not None else "error"),
                started=started,
                ended=ended,
                usage=(
                    dict(result.usage)
                    if result is not None and result.usage is not None
                    else None
                ),
                ledger_path=result.ledger_path if result is not None else None,
                branch=result.worktree_branch if result is not None else None,
                commit=result.commit if result is not None else None,
                changed_paths=(
                    tuple(result.changed_paths) if result is not None else ()
                ),
                conflicts=(
                    tuple(result.conflicts) if result is not None else ()
                ),
                commit_error=result.commit_error if result is not None else None,
                degraded=result.degraded if result is not None else None,
                error=error if result is None else (result.error or None),
                truncated=bool(result.truncated) if result is not None else False,
                origin_session=request.origin_session,
            )
        )
