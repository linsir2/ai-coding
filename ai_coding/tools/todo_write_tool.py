"""todo_write tool — maintains the session's ``TaskState`` (R1a).

Unlike the M2 version this tool is **stateless**: the list lives in the session
and travels through ``ToolContext.task``.  That fixes a real defect — the old
in-memory list was a singleton shared across sessions (``repl`` builds the tool
stack once and ``/clear`` reused it), so todos leaked between sessions.

Every mutation returns the rendered list so the model sees the full plan at the
point of change; the loop additionally injects the render on every iteration.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ai_coding.core.completion import CompletionGate
from ai_coding.domain.message import ToolResult
from ai_coding.domain.task import VALID_STATUSES, Blockers, TaskState
from ai_coding.tools.base import BaseTool, ToolContext, resolve_ctx


class TodoWriteTool(BaseTool):
    """Maintain the session task list (add / update / list / clear)."""

    # Two ``todo_write`` calls in one assistant message would race over the
    # same ``TaskState``; serialising the batch is the cheap fix.
    parallel_safe = False
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
            "A `completed` item needs proof: pass `files` naming the files you "
            "wrote/edited (each needs a current receipt) or `verify` with a shell "
            "command. `evidence` is a note for the user and does NOT satisfy "
            "completion by itself -- a completion with evidence alone is refused."
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
                    "description": (
                        "Note for the user about the work (for 'update'). "
                        "Prose alone does NOT satisfy completion: also pass "
                        "`files` or `verify`."
                    ),
                },
                "depends_on": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ids this item waits on (for 'add').",
                },
                "verify": {
                    "type": "string",
                    "description": "Shell command proving this item (for 'add'/'update').",
                },
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Files this item is expected to create/change. Completed "
                        "requires a current receipt for each one."
                    ),
                },
                "override": {
                    "type": "boolean",
                    "description": (
                        "Explicitly complete without a receipt/verify; the first "
                        "use in a session asks the operator."
                    ),
                },
                "override_reason": {
                    "type": "string",
                    "description": "Why this completion is unverified.",
                },
            },
            "required": ["action"],
        }

    async def execute(
        self,
        params: dict[str, Any],
        ctx: ToolContext | None = None,
    ) -> ToolResult:
        resolved = resolve_ctx(ctx)
        task = resolved.task
        action = str(params.get("action", "")).strip().lower()
        if not action:
            # Providers do not enforce ``required``.  The shape of the params
            # says what was meant: content → add, id → update, nothing → list.
            if "content" in params:
                action = "add"
            elif "id" in params:
                action = "update"
            else:
                action = "list"

        if action == "add":
            return self._add(params, task, ctx)
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
                        "请保持 pending，用 <proposed_plan> 提交计划；执行交给系统。"
                    ),
                    error="readonly_completion",
                    data={"action": "update", "reason": "readonly_session"},
                )
            return await self._update(params, task, ctx)
        if action == "list":
            return self._ok(task, "list")
        return ToolResult(
            success=False,
            output=f"error: unknown action '{action}' (valid: add, update, list)",
            error="unknown_action",
        )

    # ---- actions ----------------------------------------------------------

    def _add(
        self,
        params: dict[str, Any],
        task: TaskState,
        ctx: ToolContext | None,
    ) -> ToolResult:
        content = str(params.get("content", "")).strip()
        if not content:
            return ToolResult(
                success=False,
                output="error: 'content' is required for 'add'",
                error="missing_content",
            )
        raw_status = str(params.get("status") or "").strip().lower()
        if raw_status == "skipped":
            return ToolResult(
                success=False,
                output=(
                    "error: skipped 是系统在依赖失败时设置的终态，模型不能直接写。"
                    "做不了本项时请标 blocked，并说明缺什么。"
                ),
                error="runner_only_status",
                data={"action": "add"},
            )
        if raw_status == "completed":
            # Completing needs a receipt/verify gate; ``add`` has no prior item
            # to carry them, so the model must add then update.
            return ToolResult(
                success=False,
                output=(
                    "error: add creates a pending item; add it first, then "
                    "update it to completed with files/verify"
                ),
                error="invalid_status",
                data={"action": "add", "status": raw_status},
            )
        if raw_status not in ("", "pending", "in_progress"):
            return ToolResult(
                success=False,
                output="error: 'status' must be pending or in_progress for 'add'",
                error="invalid_status",
                data={"action": "add", "status": raw_status},
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
        raw_files = params.get("files") or []
        files = [str(path) for path in raw_files] if isinstance(raw_files, list) else []
        item = task.add(
            content,
            depends_on=deps,
            verify=str(params.get("verify", "") or ""),
            files=files,
        )
        if raw_status == "in_progress":
            item.status = "in_progress"
        resolved = resolve_ctx(ctx)
        if resolved.effects is not None:
            # Any later effect must be after the item boundary.  Work that was
            # already done before the item was added is still provable: the
            # completion gate lowers this boundary to a real receipt (see
            # CompletionGate.receipt_floor).
            item.started_seq = resolved.effects().sequence + 1
        return self._ok(task, "add", item_id=item.id)

    async def _update(
        self,
        params: dict[str, Any],
        task: TaskState,
        ctx: ToolContext | None,
    ) -> ToolResult:
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
        raw_files = params.get("files")
        files = (
            [str(path) for path in raw_files]
            if isinstance(raw_files, list)
            else None
        )
        normalised = None if status is None else str(status).strip().lower()
        if normalised == "skipped":
            # r24/C: ``skipped`` is a harness verdict (a dependency failed), not
            # something the model may choose.  A model that can write it can
            # end a plan by skipping its remaining work.
            return ToolResult(
                success=False,
                output=(
                    "error: skipped 是系统在依赖失败时设置的终态，模型不能直接写。"
                    "做不了本项时请标 blocked，并说明缺什么。"
                ),
                error="runner_only_status",
                data={"action": "update", "id": item_id},
            )
        if self._enforce and normalised in {"in_progress", "completed"}:
            blockers = task.blockers(item_id)
            if blockers.any:
                return self._blocked_result(item_id, blockers)
        resolved = resolve_ctx(ctx)
        item = task.get(item_id)
        if item is None:
            return self._unknown_id(task, item_id)
        completion_evidence = None if evidence is None else str(evidence)
        sequence = resolved.effects().sequence if resolved.effects is not None else 0
        new_start = sequence + 1
        if normalised == "in_progress" and item.status == "in_progress" and item.started_seq > 0:
            new_start = item.started_seq
        if normalised == "completed" and item.started_seq == 0:
            # A legacy/direct item has no boundary; require an effect produced
            # after this point.  New items get the boundary at add time.
            new_start = sequence + 1
        if normalised == "completed":
            # Validate the fields this same call supplies, not the old item.
            # Otherwise a new files/verify pair is either rejected (not applied
            # yet) or silently bypassed (old values are checked).
            probe = replace(
                item,
                files=item.files if files is None else files,
                verify=item.verify if verify is None else str(verify),
                started_seq=(
                    item.started_seq if item.started_seq > 0 else new_start
                ),
            )
            # Work done before this item was created still has a real receipt.
            # Lower the boundary to it, exactly as ``update_plan`` does; without
            # this a ``write -> add -> complete`` sequence was rejected even
            # though the file had just been written.
            floor = CompletionGate.receipt_floor(probe, resolved)
            if floor is not None and floor < probe.started_seq:
                probe = replace(probe, started_seq=floor)
            if item.status == "completed":
                return self._ok(task, "update", item_id=item_id)
            verdict = await CompletionGate.check(probe, resolved)
            if not verdict.ok:
                override = bool(params.get("override", False))
                reason = str(params.get("override_reason", "") or "").strip()
                if not override:
                    item.completion_failures += 1
                    if item.completion_failures >= 2:
                        item.status = "blocked"
                        item.evidence = f"completion blocked: {verdict.reason}"
                        return ToolResult(
                            success=False,
                            output=(
                                f"error: todo #{item_id} completion blocked after "
                                f"two unverified attempts: {verdict.reason}. "
                                "It stays visible as blocked, not abandoned: add "
                                "the receipt/verify (or override with a reason) "
                                "and complete it again."
                            ),
                            error="completion_blocked",
                            data={
                                "id": item_id,
                                "reason": verdict.reason,
                                "code": "completion_blocked",
                                "recoverable": True,
                            },
                        )
                    return ToolResult(
                        success=False,
                        output=(
                            f"error: todo #{item_id} has no current receipt/verify: "
                            f"{verdict.reason}. Use files with a successful write/edit, "
                            "a verify command, or override=true with a reason."
                        ),
                        error=verdict.code or "completion_unverified",
                        data={
                            "id": item_id,
                            "reason": verdict.reason,
                            "code": verdict.code,
                        },
                    )
                override_verdict = await CompletionGate.override(probe, resolved, reason)
                if not override_verdict.ok:
                    error = override_verdict.code or "override_denied"
                    return ToolResult(
                        success=False,
                        output=f"error: {override_verdict.reason}",
                        error=error,
                        data={"id": item_id},
                    )
                completion_evidence = completion_evidence or override_verdict.evidence
            else:
                completion_evidence = completion_evidence or verdict.evidence
            item.completion_failures = 0
        started_seq = new_start if normalised == "in_progress" else None
        try:
            task.update(
                item_id,
                status=normalised,
                content=None if content is None else str(content),
                evidence=completion_evidence,
                verify=None if verify is None else str(verify),
                files=files,
                started_seq=started_seq if normalised == "in_progress" else None,
            )
        except KeyError:
            return self._unknown_id(task, item_id)
        except ValueError as exc:
            return ToolResult(
                success=False,
                output=f"error: {exc}",
                error="invalid_status",
                data={"action": "update", "id": item_id},
            )
        return self._ok(task, "update", item_id=item_id)

    @staticmethod
    def _unknown_id(task: TaskState, item_id: str) -> ToolResult:
        """R4-5: say which ids exist; "not found" alone makes the model guess.

        Measured: a live round spent 20 todo_write calls, two of them retrying
        ``id=t1`` while the list actually used ``1``/``2``.
        """
        ids = [item.id for item in task.items]
        valid = ", ".join(ids[:10]) + (" …" if len(ids) > 10 else "") if ids else "（清单为空）"
        return ToolResult(
            success=False,
            output=(
                f"error: todo #{item_id} not found. 现有 id：{valid}。"
                "用 `action=list` 查看完整清单；新条目用 `action=add` 创建。"
            ),
            error="unknown_id",
            data={"action": "update", "id": item_id, "valid_ids": ids},
        )

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
