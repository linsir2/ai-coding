"""P2-C1: the three readers answer from one read-only registry."""

import time

from ai_coding.core.task_registry import MAX_SETTLED_VIEWS, TaskRegistry, TaskView
from ai_coding.domain.subagent import BackgroundTask


class _Runner:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.announce = {}

    def task_list(self):
        return list(self.rows)


def _row(run_id="run_a", *, status="running", name="bg", started=100.0, ended=None):
    return BackgroundTask(
        run_id=run_id,
        name=name,
        status=status,
        started=started,
        ended=ended,
        usage={"total_tokens": 123} if status != "running" else None,
        ledger_path="/ws/x.json" if status != "running" else None,
        worktree_branch="ai_sub_x" if status != "running" else None,
        commit="abcdef" if status != "running" else None,
        changed_paths=("a.py",),
        commit_error="nope" if status != "running" else None,
        degraded="worktree_unavailable" if status != "running" else None,
        error="boom" if status == "error" else None,
    )


def test_a_background_row_is_a_view_with_camel_case():
    registry = TaskRegistry(_Runner([_row(status="completed")]))
    views = registry.list()
    assert len(views) == 1
    view = views[0]
    assert view.kind == "background"
    assert view.id == "run_a"
    data = view.to_dict()
    assert data["runId"] == "run_a"
    assert data["branch"] == "ai_sub_x"
    assert data["changedPaths"] == ["a.py"]
    assert data["commitError"] == "nope"
    assert data["degraded"] == "worktree_unavailable"


def test_child_and_fanout_views_are_listed_from_memory():
    registry = TaskRegistry(_Runner())
    child = TaskView(id="req-1", name="probe", kind="child", status="completed",
                     started=1.0, ended=2.0, usage={"total_tokens": 1})
    fanout = TaskView(id="q1", name="research:q1", kind="fanout",
                      status="timeout", started=3.0, ended=4.0,
                      error="child_timeout", truncated=True)
    registry.register(child)
    registry.register(fanout)

    views = registry.list()
    assert [view.id for view in views] == ["req-1", "q1"]
    assert views[0].kind == "child"
    assert views[1].kind == "fanout"
    assert views[1].status == "timeout"
    assert views[1].truncated is True


def test_a_spawn_view_is_merged_with_its_background_projection():
    registry = TaskRegistry(_Runner([_row(status="completed")]))
    registry.register(
        TaskView(id="req-1", run_id="run_a", name="spawned", kind="child",
                 status="running", started=99.0, origin_session="s1")
    )
    views = registry.list()
    assert len(views) == 1
    assert views[0].id == "req-1"
    assert views[0].run_id == "run_a"
    assert views[0].kind == "child"
    assert views[0].status == "completed"
    assert views[0].branch == "ai_sub_x"
    assert views[0].origin_session == "s1"


def test_get_resolves_request_id_run_prefix_and_name():
    registry = TaskRegistry(_Runner([_row()]))
    registry.register(
        TaskView(id="req-abc", run_id="run_a", name="spawned", kind="child",
                 status="running", started=100.0)
    )

    assert registry.get("req-abc") is not None
    assert registry.get("run_a") is not None
    assert registry.get("req-a") is not None
    assert registry.get("run_") is not None
    # A name that matches exactly one view resolves too.
    assert registry.get("spawned") is not None
    assert registry.get("nope") is None


def test_update_keeps_the_id_and_replaces_the_fields():
    registry = TaskRegistry(_Runner())
    view = TaskView(id="req-1", name="probe", kind="child", status="running",
                    started=1.0)
    registry.register(view)
    updated = registry.update("req-1", status="completed", ended=2.0)
    assert updated is not None
    assert updated.id == "req-1"
    assert updated.status == "completed"
    assert registry.get("req-1").status == "completed"

    replacement = TaskView(id="req-2", name="other", kind="fanout",
                           status="completed", started=3.0, ended=4.0)
    registry.update(replacement)
    assert registry.get("req-2") is not None


def test_settled_views_are_capped_but_running_views_are_not_pruned():
    registry = TaskRegistry(_Runner())
    started = time.monotonic()
    for index in range(MAX_SETTLED_VIEWS + 5):
        registry.register(
            TaskView(
                id=f"old-{index}",
                name="old",
                kind="child",
                status="completed",
                started=started + index,
                ended=started + index + 1,
            )
        )
    registry.register(
        TaskView(id="live", name="live", kind="child", status="running",
                 started=started)
    )
    views = registry.list()
    assert len([view for view in views if view.status != "running"]) == MAX_SETTLED_VIEWS
    assert any(view.id == "live" for view in views)
    assert not any(view.id == "old-0" for view in views)
