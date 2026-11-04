"""``update_plan`` — Codex's plan tool, kept byte-compatible (C-1).

Codex has no plan executor: the model owns the plan through this one tool and
its result is always ``"Plan updated"``.  Every call replaces the whole list
(ids are reassigned 1..N), and the only fields carried are ``step``/``status``
-- plus an optional ``verify`` proof behind ``plan_verify_items``, an opt-in
deviation that lets a completed step be checked at the item boundary.
"""

from __future__ import annotations

from typing import Any, cast

from ai_coding.core.completion import CompletionGate
from ai_coding.domain.message import ToolResult
from ai_coding.domain.task import TaskState, TodoItem, TodoStatus
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
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Files proving this step.",
            },
            "override": {"type": "boolean"},
            "override_reason": {"type": "string"},
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
        parsed: list[tuple[str, str, str, list[str], bool, str]] = []
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
            raw_files = entry.get("files") or []
            files = (
                [str(path) for path in raw_files]
                if isinstance(raw_files, list)
                else []
            )
            parsed.append(
                (
                    step.strip(),
                    status,
                    verify,
                    files,
                    bool(entry.get("override", False)),
                    str(entry.get("override_reason", "") or "").strip(),
                )
            )

        task = resolved.task
        previous = {item.content: item for item in task.items}
        previous_by_index = list(task.items)
        candidate = TaskState(goal=task.goal)
        written: list[TodoItem] = []
        settled: set[str] = set()
        next_seq = (
            resolved.effects().sequence if resolved.effects is not None else 0
        )
        for index, (step, status, verify, files, _override, _reason) in enumerate(parsed):
            item = candidate.add(step, verify=verify, files=files)
            item.status = cast(TodoStatus, status)
            prior = previous.get(step)
            if prior is None and index < len(previous_by_index):
                prior = previous_by_index[index]
            if status == "completed" and prior is not None and prior.status == "completed":
                if not files and not verify:
                    # ``update_plan`` replaces the whole list and only carries
                    # step/status; a completed step advanced by a later call
                    # must keep its proof/evidence or it is re-gated.
                    item.files = list(prior.files)
                    item.verify = prior.verify
                    item.evidence = prior.evidence
                    item.started_seq = prior.started_seq
                    settled.add(item.id)
            if status == "in_progress":
                # Reasserting an in-progress step must not move the proof
                # boundary past receipts produced while it was running; a
                # reopen (completed -> in_progress) starts a new boundary.
                item.started_seq = (
                    prior.started_seq
                    if prior is not None
                    and prior.status == "in_progress"
                    and prior.started_seq > 0
                    else next_seq + 1
                )
            elif status == "completed" and item.id not in settled:
                item.started_seq = (
                    prior.started_seq
                    if prior is not None and prior.started_seq > 0
                    else next_seq
                )
            written.append(item)

        for item, entry in zip(written, parsed, strict=True):
            if item.status != "completed" or item.id in settled:
                continue
            verdict = await CompletionGate.check(item, resolved)
            if verdict.ok:
                item.evidence = verdict.evidence
                continue
            _step, _status, _verify, _files, override, reason = entry
            if not override:
                error = (
                    "verify_failed"
                    if verdict.reason.startswith("verify failed:")
                    else "completion_unverified"
                )
                return ToolResult(
                    success=False,
                    output=(
                        verdict.reason
                        if error == "verify_failed"
                        else f"error: plan step '{item.content}' has no current "
                        f"receipt/verify: {verdict.reason}. Add files/verify, "
                        "or override with a reason."
                    ),
                    error=error,
                    data={"id": item.id, "reason": verdict.reason},
                )
            override_verdict = await CompletionGate.override(item, resolved, reason)
            if not override_verdict.ok:
                if "reason is required" in override_verdict.reason:
                    error = "missing_override_reason"
                elif "needs operator" in override_verdict.reason:
                    error = "override_unavailable"
                else:
                    error = "override_denied"
                return ToolResult(
                    success=False,
                    output=f"error: {override_verdict.reason}",
                    error=error,
                    data={"id": item.id},
                )
            item.evidence = override_verdict.evidence

        task.items = written
        return ToolResult(success=True, output="Plan updated")


def _invalid(reason: str) -> ToolResult:
    return ToolResult(
        success=False,
        output=f"error: invalid plan ({reason})",
        error="invalid_plan",
    )
