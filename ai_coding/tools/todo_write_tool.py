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
    mutates_world = False

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
            "list shows the current list. "
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
                    "enum": ["add", "update", "list"],
                    "description": "The action to perform.",
                },
                "content": {"type": "string", "description": "Item text (for 'add')."},
                "id": {"type": "string", "description": "Item id (for 'update')."},
                "status": {
                    "type": "string",
                    # r24/C: ``skipped`` is a runner verdict, not something the
                    # model may choose -- a model able to skip items can skip
                    # itself to "done".  Kept out of the advertised enum and
                    # refused in ``_update`` as well.
                    "enum": sorted(VALID_STATUSES - {"skipped"}),
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
                "verify": {
                    "type": "string",
                    "description": (
                        "Shell command proving this item (for 'add'/'update'). "
                        "The plan runner turns it into an acceptance contract "
                        "for the step's turn; empty means not machine-checkable."
                    ),
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
            # 19 §3.5 (Q6): completion is the execution stage's mark — the
            # planning posture may author the list, not finish it.  Measured
            # (session 13e303d4): the model marked item 1 completed mid-planning
            # and the run report then counted it as work "done outside the run".
            if (
                ctx is not None
                and ctx.readonly
                and str(params.get("status", "")).strip().lower() == "completed"
            ):
                return ToolResult(
                    success=False,
                    output=(
                        "error: 只读/规划阶段不能把条目标记为 completed——完成标记由执行阶段产生。"
                        "请保持 pending，用 present_plan 提交计划；执行交给运行器。"
                    ),
                    error="readonly_completion",
                    data={"action": "update", "reason": "readonly_session"},
                )
            return self._update(params, task)
        if action == "list":
            return self._ok(task, "list")
        return ToolResult(
            success=False,
            output=f"error: unknown action '{action}' (valid: add, update, list)",
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
        item = task.add(content, depends_on=deps, verify=str(params.get("verify", "") or ""))
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
        verify = params.get("verify")
        normalised = None if status is None else str(status).strip().lower()
        if normalised == "skipped":
            # r24/C: only the plan executor gives an item up, and only when a
            # dependency failed.  A model that can write ``skipped`` can end a
            # plan by skipping its remaining work.
            return ToolResult(
                success=False,
                output=(
                    "error: skipped 由执行器在依赖失败时设置，模型不能直接写。"
                    "做不了本项时请标 blocked，并说明缺什么。"
                ),
                error="runner_only_status",
                data={"action": "update", "id": item_id},
            )
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
                verify=None if verify is None else str(verify),
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
