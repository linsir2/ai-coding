"""Read-only task views (P2-C1).

Three readers (``/tasks``, ``list_agents``, ``subagent_list``) format different
output, but they must answer from one source.  :class:`TaskRegistry` merges the
runner's background projection with the in-memory views the scheduler
registers for synchronous runs and fan-out questions.

No new table: C1 has no journal, so there is no reconciliation state either.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Any

from ai_coding.domain.subagent import BackgroundTask

__all__ = ["TaskRegistry", "TaskView"]

#: How many settled views the in-memory lane keeps (running views never pruned).
MAX_SETTLED_VIEWS = 100


@dataclass
class TaskView:
    """One run as a reader sees it (a projection, never an authority)."""

    id: str
    run_id: str = ""
    name: str = "sub-agent"
    kind: str = "child"
    status: str = "running"
    started: float = 0.0
    ended: float | None = None
    usage: dict[str, int] | None = None
    ledger_path: str | None = None
    branch: str | None = None
    commit: str | None = None
    #: P2-C3: the commit the source worktree was created from, and whether a
    #: merge ticket was verified.  Both are additive; child views leave them
    #: at their defaults.
    base_commit: str | None = None
    verified: bool | None = None
    changed_paths: tuple[str, ...] = ()
    conflicts: tuple[dict[str, Any], ...] = ()
    commit_error: str | None = None
    degraded: str | None = None
    error: str | None = None
    truncated: bool = False
    origin_session: str = ""
    #: P2-C2: a journal-open task recovered after a restart.  Shown to the
    #: operator, never auto-run again, and never pruned as an old success.
    needs_reconciliation: bool = False

    @property
    def is_running(self) -> bool:
        # A merge ticket is live while queued or merging.  Treating those as
        # settled would let ``_prune`` remove the row a waiter is still reading.
        return self.status in ("running", "queued", "merging")

    @property
    def is_settled(self) -> bool:
        return not self.is_running and not self.needs_reconciliation

    def elapsed(self, now: float | None = None) -> float:
        end = self.ended if self.ended is not None else (now or time.monotonic())
        return max(0.0, end - self.started)

    def to_dict(self) -> dict[str, Any]:
        """camelCase for the wire/tool data (the pre-P2-C shape)."""
        return {
            "id": self.id,
            "runId": self.run_id,
            "name": self.name,
            "kind": self.kind,
            "status": self.status,
            "started": self.started,
            "ended": self.ended,
            "usage": dict(self.usage) if self.usage is not None else None,
            "ledgerPath": self.ledger_path,
            "branch": self.branch,
            "commit": self.commit,
            "baseCommit": self.base_commit,
            "verified": self.verified,
            "changedPaths": list(self.changed_paths),
            "conflicts": [
                {**entry, "paths": list(entry.get("paths") or ())}
                for entry in self.conflicts
            ],
            "commitError": self.commit_error,
            "degraded": self.degraded,
            "error": self.error,
            "truncated": self.truncated,
            "originSession": self.origin_session,
            "needsReconciliation": self.needs_reconciliation,
        }

    @classmethod
    def from_background(
        cls, row: BackgroundTask, *, origin_session: str = ""
    ) -> TaskView:
        return cls(
            id=row.run_id,
            run_id=row.run_id,
            name=row.name,
            kind="background",
            status=row.status,
            started=row.started,
            ended=row.ended,
            usage=dict(row.usage) if row.usage is not None else None,
            ledger_path=row.ledger_path,
            branch=row.worktree_branch,
            commit=row.commit,
            base_commit=row.base_commit,
            changed_paths=tuple(row.changed_paths),
            commit_error=row.commit_error,
            degraded=row.degraded,
            error=row.error,
            origin_session=origin_session,
        )


def _merge(view: TaskView, row: TaskView) -> TaskView:
    """One row for a scheduler view and its live background projection.

    The scheduler's id/kind/origin identify *who asked*; the background row is
    the live projection and owns every fact that can change after creation.
    """
    return replace(
        row,
        id=view.id,
        run_id=row.run_id or view.run_id,
        name=view.name or row.name,
        kind=view.kind,
        origin_session=view.origin_session or row.origin_session,
        base_commit=row.base_commit or view.base_commit,
        verified=row.verified if row.verified is not None else view.verified,
        conflicts=view.conflicts or row.conflicts,
        needs_reconciliation=view.needs_reconciliation or row.needs_reconciliation,
    )


class TaskRegistry:
    """Merges the runner's background rows with scheduler-registered views."""

    def __init__(self, runner: Any) -> None:
        self._runner = runner
        self._views: dict[str, TaskView] = {}

    # ---- writes (scheduler bookkeeping) -----------------------------------

    def register(self, view: TaskView) -> None:
        self._views[view.id] = view
        self._prune()

    def update(self, view: TaskView | str, **changes: Any) -> TaskView | None:
        """Replace a whole view, or update the named one field by field."""
        if isinstance(view, TaskView):
            existing = self._views.get(view.id)
            if existing is not None and view.started == 0.0:
                view = replace(view, started=existing.started)
            self.register(view)
            return view
        existing = self._views.get(view)
        if existing is None:
            return None
        updated = replace(existing, **changes)
        self.register(updated)
        return updated

    def _prune(self) -> None:
        settled = sorted(
            (view for view in self._views.values() if view.is_settled),
            key=lambda view: view.ended if view.ended is not None else view.started,
        )
        overflow = len(settled) - MAX_SETTLED_VIEWS
        for view in settled[: max(0, overflow)]:
            self._views.pop(view.id, None)

    # ---- reads -------------------------------------------------------------

    def list(self) -> list[TaskView]:
        """Background rows merged with scheduler views (runner order first)."""
        rows: list[TaskView] = []
        index_by_run: dict[str, int] = {}
        lister = getattr(self._runner, "task_list", None)
        announce = getattr(self._runner, "_announce", {}) or {}
        if callable(lister):
            for row in lister():
                origin = ""
                context = announce.get(row.run_id)
                if isinstance(context, tuple) and len(context) >= 2:
                    origin = str(context[1] or "")
                index_by_run[row.run_id] = len(rows)
                rows.append(TaskView.from_background(row, origin_session=origin))
        for view in sorted(self._views.values(), key=lambda item: item.started):
            if view.run_id and view.run_id in index_by_run:
                index = index_by_run[view.run_id]
                rows[index] = _merge(view, rows[index])
            else:
                rows.append(view)
        return rows

    def get(self, target: str) -> TaskView | None:
        """Resolve by request id, run-id prefix, or unique name."""
        if not target:
            return None
        views = self.list()
        for view in views:
            if view.id == target or view.run_id == target:
                return view
        matches = [view for view in views if view.run_id.startswith(target)]
        if len(matches) == 1:
            return matches[0]
        matches = [view for view in views if view.id.startswith(target)]
        if len(matches) == 1:
            return matches[0]
        matches = [view for view in views if view.name == target]
        return matches[0] if len(matches) == 1 else None
