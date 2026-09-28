"""todo_write tool — maintains the session's ``TaskState`` (R1a).

Unlike the M2 version this tool is **stateless**: the list lives in the session
and travels through ``ToolContext.task``.  That fixes a real defect — the old
in-memory list was a singleton shared across sessions (``repl`` builds the tool
stack once and ``/clear`` reused it), so todos leaked between sessions.

Every mutation returns the rendered list so the model sees the full plan at the
point of change; the loop additionally injects the render on every iteration.
"""

from __future__ import annotations

from typing import Any

from ai_coding.domain.message import ToolResult
from ai_coding.domain.task import VALID_STATUSES, Blockers, TaskState
from ai_coding.tools.base import BaseTool, ToolContext, resolve_ctx


class TodoWriteTool(BaseTool):
    """Maintain the session task list (add / update / list / clear)."""

    parallel_safe = True

    def __init__(self, *, enforce_dependencies: bool = True) -> None:
        #: R9a: refuse to start/finish an item whose dependencies are unmet, and
        #: refuse to create dependencies that break the list's invariant.  The
        #: switch exists for legacy lists (docs/r9-analysis.md D-R9-3).
        self._enforce = bool(enforce_dependencies)

    @property
    def name(self) -> str:
        return "todo_write"

    @property
    def description(self) -> str:
        return (
            "Maintain your task list for the current goal. This list is your plan: "
            "it is shown back to you on every step, and the run is not considered "
            "finished while items are still open, so keep it accurate. "
            "Actions: add (content, optional depends_on) appends a pending item; "
            "update (id, optional status/evidence/content) patches an item, where "
            "status is one of pending/in_progress/completed/blocked/dropped; "
            "list shows the current list; clear drops all items. "
            "Keep items small and actionable, and mark an item completed only with "
            "evidence (the file, command or finding that proves it)."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "update", "list", "clear"],
                    "description": "The action to perform.",
                },
                "content": {"type": "string", "description": "Item text (for 'add')."},
                "id": {"type": "string", "description": "Item id (for 'update')."},
                "status": {
                    "type": "string",
                    "enum": sorted(VALID_STATUSES),
                    "description": "New status (for 'update').",
                },
                "evidence": {
                    "type": "string",
                    "description": "What proves the item is done (for 'update').",
                },
                "depends_on": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ids this item waits on (for 'add').",
                },
            },
            "required": ["action"],
        }

    async def execute(
        self,
        params: dict[str, Any],
        ctx: ToolContext | None = None,
    ) -> ToolResult:
        task = resolve_ctx(ctx).task
        action = str(params.get("action", "")).strip().lower()

        if action == "add":
            return self._add(params, task)
        if action == "update":
            return self._update(params, task)
        if action == "list":
            return self._ok(task, "list")
        if action == "clear":
            task.clear()
            return self._ok(task, "clear")
        return ToolResult(
            success=False,
            output=f"error: unknown action '{action}' (valid: add, update, list, clear)",
            error="unknown_action",
        )

    # ---- actions ----------------------------------------------------------

    def _add(self, params: dict[str, Any], task: TaskState) -> ToolResult:
        content = str(params.get("content", "")).strip()
        if not content:
            return ToolResult(
                success=False,
                output="error: 'content' is required for 'add'",
                error="missing_content",
            )
        raw_deps = params.get("depends_on") or []
        deps = [str(d) for d in raw_deps] if isinstance(raw_deps, list) else []
        if self._enforce:
            new_id = task.next_id()
            cleaned: list[str] = []
            for dep in deps:
                target = dep.strip()
                if not target:
                    continue
                if target == new_id:
                    return ToolResult(
                        success=False,
                        output=(
                            f"error: item {new_id} cannot depend on itself; "
                            "drop it from depends_on or add it after the dependency exists"
                        ),
                        error="self_dependency",
                        data={"action": "add", "id": new_id, "depends_on": [target]},
                    )
                if task.get(target) is None:
                    return ToolResult(
                        success=False,
                        output=(
                            f"error: dependency '{target}' is not in the list; "
                            "add it first (ids are assigned in order), or drop it "
                            "from depends_on"
                        ),
                        error="unknown_dependency",
                        data={"action": "add", "depends_on": [target]},
                    )
                if target not in cleaned:
                    cleaned.append(target)
            deps = cleaned
        item = task.add(content, depends_on=deps)
        return self._ok(task, "add", item_id=item.id)

    def _update(self, params: dict[str, Any], task: TaskState) -> ToolResult:
        item_id = str(params.get("id", "")).strip()
        if not item_id:
            return ToolResult(
                success=False,
                output="error: 'id' is required for 'update'",
                error="missing_id",
            )
        status = params.get("status")
        content = params.get("content")
        evidence = params.get("evidence")
        normalised = None if status is None else str(status).strip().lower()
        if self._enforce and normalised in {"in_progress", "completed"}:
            blockers = task.blockers(item_id)
            if blockers.any:
                return self._blocked_result(item_id, blockers)
        try:
            task.update(
                item_id,
                status=normalised,
                content=None if content is None else str(content),
                evidence=None if evidence is None else str(evidence),
            )
        except KeyError:
            return ToolResult(
                success=False,
                output=f"error: todo #{item_id} not found",
                error="unknown_id",
                data={"action": "update", "id": item_id},
            )
        except ValueError as exc:
            return ToolResult(
                success=False,
                output=f"error: {exc}",
                error="invalid_status",
                data={"action": "update", "id": item_id},
            )
        return self._ok(task, "update", item_id=item_id)

    @staticmethod
    def _blocked_result(item_id: str, blockers: Blockers) -> ToolResult:
        reasons: list[str] = []
        if blockers.open:
            reasons.append("未完成的依赖: " + ", ".join(blockers.open))
        if blockers.unknown:
            reasons.append("已不存在的依赖: " + ", ".join(blockers.unknown))
        if blockers.cycle:
            reasons.append("依赖成环: " + " → ".join(blockers.cycle))
        return ToolResult(
            success=False,
            output=(
                f"error: todo #{item_id} 还不能开始/完成（{'；'.join(reasons)}）。"
                "出路：先 update 那些依赖为 completed；若某个依赖已作废，"
                "update(id=<它>, status=dropped) 解锁；若链条成环，"
                "把环里任意一项 dropped。"
            ),
            error="blocked_by_dependencies",
            data={
                "id": item_id,
                "open": list(blockers.open),
                "unknown": list(blockers.unknown),
                "cycle": list(blockers.cycle),
            },
        )

    @staticmethod
    def _ok(task: TaskState, action: str, item_id: str | None = None) -> ToolResult:
        data: dict[str, Any] = {
            "action": action,
            "total": task.total,
            "done": task.done_count,
            "open": len(task.open_items()),
        }
        if item_id is not None:
            data["id"] = item_id
        return ToolResult(success=True, output=task.render(), data=data)


__all__ = ["TodoWriteTool"]
