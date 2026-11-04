"""``update_plan`` — Codex's plan tool, kept byte-compatible (C-1).

Codex has no plan executor: the model owns the plan through this one tool and
its result is always ``"Plan updated"``.  Every call replaces the whole list.
A step may carry an optional stable ``id``; unmatched steps get a fresh id.
The completion gate uses ``files``/``verify``/explicit override, an intentional
deviation from Codex's minimal schema.
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
            "id": {
                "type": "string",
                "description": "Optional stable step id; it survives reorder/rephrase.",
            },
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
        parsed: list[tuple[str, str, str, str, list[str], bool, str]] = []
        provided_ids: list[str] = []
        for entry in raw:
            if not isinstance(entry, dict):
                return _invalid("each plan entry must be an object")
            step = entry.get("step")
            if not isinstance(step, str) or not step.strip():
                return _invalid("step must be a non-empty string")
            raw_id = entry.get("id")
            step_id = raw_id.strip() if isinstance(raw_id, str) else ""
            if step_id:
                provided_ids.append(step_id)
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
                    step_id,
                    step.strip(),
                    status,
                    verify,
                    files,
                    bool(entry.get("override", False)),
                    str(entry.get("override_reason", "") or "").strip(),
                )
            )
        if len(provided_ids) != len(set(provided_ids)):
            return _invalid("step ids must be unique")

        task = resolved.task
        previous_by_id = {item.id: item for item in task.items}
        content_counts: dict[str, int] = {}
        for item in task.items:
            content_counts[item.content] = content_counts.get(item.content, 0) + 1
        previous_by_content = {
            item.content: item
            for item in task.items
            if content_counts[item.content] == 1
        }
        candidate = TaskState(goal=task.goal)
        written: list[TodoItem] = []
        settled: set[str] = set()
        matched_priors: list[TodoItem | None] = []
        for step_id, step, *_rest in parsed:
            matched_priors.append(
                previous_by_id.get(step_id)
                if step_id
                else previous_by_content.get(step)
            )
        used_ids = set(provided_ids) | {
            prior.id for prior in matched_priors if prior is not None
        }
        used_assigned: set[str] = set()
        next_seq = (
            resolved.effects().sequence if resolved.effects is not None else 0
        )
        for (
            step_id,
            step,
            status,
            verify,
            files,
            _override,
            _reason,
        ), prior in zip(parsed, matched_priors, strict=True):
            if step_id:
                assigned_id = step_id
            elif prior is not None:
                assigned_id = prior.id
            else:
                assigned_id = candidate.next_id()
                while assigned_id in used_ids:
                    assigned_id = str(int(assigned_id) + 1)
            if assigned_id in used_assigned:
                return _invalid("step ids/content must be unique")
            used_assigned.add(assigned_id)
            used_ids.add(assigned_id)
            item = candidate.add(
                step, verify=verify, files=files, item_id=assigned_id
            )
            item.status = cast(TodoStatus, status)
            if (
                status == "completed"
                and prior is not None
                and prior.status == "completed"
                and not files
                and not verify
                and _has_proof(prior)
            ):
                # ``update_plan`` replaces the whole list and only carries
                # step/status; a completed step advanced by a later call must
                # keep its proof/evidence or it is re-gated.  Legacy sessions
                # copy the fields but do not settle: they must get the
                # diagnostic gate (legacy_session_unverified).
                item.files = list(prior.files)
                item.verify = prior.verify
                item.evidence = prior.evidence
                item.started_seq = prior.started_seq
                if not getattr(resolved, "effects_legacy", False):
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
            _step_id, _step, _status, _verify, _files, override, reason = entry
            if not override:
                error = verdict.code or "completion_unverified"
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
                    data={
                        "id": item.id,
                        "reason": verdict.reason,
                        "code": verdict.code,
                    },
                )
            override_verdict = await CompletionGate.override(item, resolved, reason)
            if not override_verdict.ok:
                error = override_verdict.code or "override_denied"
                return ToolResult(
                    success=False,
                    output=f"error: {override_verdict.reason}",
                    error=error,
                    data={"id": item.id},
                )
            item.evidence = override_verdict.evidence

        task.items = written
        return ToolResult(success=True, output="Plan updated")


def _has_proof(item: TodoItem) -> bool:
    """Was this completed item's proof machine-recognized, not just prose?"""
    return bool(
        item.files
        or item.verify
        or (item.evidence or "").startswith(("receipt:", "verify:", "override:"))
    )


def _invalid(reason: str) -> ToolResult:
    return ToolResult(
        success=False,
        output=f"error: invalid plan ({reason})",
        error="invalid_plan",
    )
