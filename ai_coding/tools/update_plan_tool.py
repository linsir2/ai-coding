"""``update_plan`` — Codex's plan tool, kept byte-compatible (C-1).

Codex has no plan executor: the model owns the plan through this one tool and
its result is always ``"Plan updated"``.  Every call replaces the whole list
(ids are reassigned 1..N), and the only fields carried are ``step``/``status``
-- plus an optional ``verify`` proof behind ``plan_verify_items``, an opt-in
deviation that lets a completed step be checked at the item boundary.
"""

from __future__ import annotations

from typing import Any, cast

from ai_coding.domain.message import ToolResult
from ai_coding.domain.task import TodoItem, TodoStatus
from ai_coding.tools.base import BaseTool, ToolContext, resolve_ctx

__all__ = ["UpdatePlanTool"]

_STATUSES = ("pending", "in_progress", "completed")
_PLAN_MODE = "update_plan is unavailable in Plan mode; present the plan as text."


class UpdatePlanTool(BaseTool):
    """Replace the session plan with the model's ``step``/``status`` list."""

    parallel_safe = False
    mutates_world = False
    plan_only = False

    def __init__(self, *, verify_items: bool = False) -> None:
        #: C-1: whether the advertised schema also accepts a per-step ``verify``.
        #: The runtime decision still reads ``ctx.plan_verify_items`` (the
        #: context is what the loop stamps); this only shapes the tool table.
        self._verify_items = bool(verify_items)

    @property
    def name(self) -> str:
        return "update_plan"

    @property
    def description(self) -> str:
        return "更新计划步骤与状态；完成时用 completed。"

    @property
    def params_json_schema(self) -> dict[str, Any]:
        step_properties: dict[str, Any] = {
            "step": {"type": "string"},
            "status": {"type": "string", "enum": list(_STATUSES)},
        }
        if self._verify_items:
            step_properties["verify"] = {"type": "string"}
        return {
            "type": "object",
            "properties": {
                "explanation": {"type": "string"},
                "plan": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": step_properties,
                        "required": ["step", "status"],
                    },
                },
            },
            "required": ["plan"],
        }

    async def execute(
        self,
        params: dict[str, Any],
        ctx: ToolContext | None = None,
    ) -> ToolResult:
        resolved = resolve_ctx(ctx)
        if str(getattr(resolved, "mode", "normal") or "normal") == "plan":
            return ToolResult(success=False, output=_PLAN_MODE, error="plan_mode")

        raw = params.get("plan")
        if not isinstance(raw, list) or not raw:
            return _invalid("plan must be a non-empty array")
        verify_enabled = bool(getattr(resolved, "plan_verify_items", False))
        parsed: list[tuple[str, str, str]] = []
        for entry in raw:
            if not isinstance(entry, dict):
                return _invalid("each plan entry must be an object")
            step = entry.get("step")
            if not isinstance(step, str) or not step.strip():
                return _invalid("step must be a non-empty string")
            status = entry.get("status")
            if not isinstance(status, str) or status not in _STATUSES:
                return _invalid(
                    "status must be one of pending, in_progress, completed"
                )
            verify = ""
            if verify_enabled and isinstance(entry.get("verify"), str):
                verify = entry["verify"].strip()
            parsed.append((step.strip(), status, verify))

        task = resolved.task
        task.items.clear()
        written: list[tuple[TodoItem, str]] = []
        for step, status, verify in parsed:
            item = task.add(step, verify=verify)
            item.status = cast(TodoStatus, status)
            written.append((item, verify))

        verify_item = getattr(resolved, "verify_item", None)
        if verify_enabled and verify_item is not None:
            for item, verify in written:
                if item.status != "completed" or not verify:
                    continue
                passed, tail = await verify_item(item.id, item.content, verify)
                if not passed:
                    item.status = "in_progress"
                    return ToolResult(
                        success=False,
                        output=f"verify failed: {tail}",
                        error="verify_failed",
                    )
        return ToolResult(success=True, output="Plan updated")


def _invalid(reason: str) -> ToolResult:
    return ToolResult(
        success=False,
        output=f"error: invalid plan ({reason})",
        error="invalid_plan",
    )
