"""``present_plan`` — the model's plan, submitted for a human review (19 §3.2).

The review blocks *inside the tool*, exactly like ``ask_user``: the answer is
the tool result, so the assistant message and its ``tool`` result stay paired
(the same reason ``user_prompt.py`` exists at all).  The card on the other
side is chosen by ``intent="plan_review"`` — an intent changes presentation,
never the protocol (dsh's lesson).  Answers are a JSON string
``{"choice": "approve" | "keep", "feedback": str}``; plain words like
"开始执行" stay acceptable so the CLI path works without the card.
"""

from __future__ import annotations

import json
from typing import Any

from ai_coding.domain.message import ToolResult
from ai_coding.security.user_prompt import UserPromptCallback
from ai_coding.tools.base import BaseTool, ToolContext

__all__ = ["PresentPlanTool"]

# Room above the wait for the review to be rendered and answered, matching
# ask_user (the dispatcher takes min(this, what is left of the turn)).
_TIMEOUT_GRACE = 15.0

_APPROVED = (
    "计划已批准：执行由系统接管（前端将自动开始执行）。请简短收尾，不要开始执行。"
)
_KEEP = "用户选择继续规划。反馈：{feedback}。修改计划后再次调用 present_plan。"
_KEEP_NO_FEEDBACK = (
    "用户选择继续规划，未给出反馈：请询问要修改什么，或先修订后再次调用 present_plan。"
)
_HOLD = (
    "用户搁置了这次评审：保持计划模式，简要向用户说明计划，然后停下等待进一步指示。"
)
_NO_CHANNEL = (
    "当前没有人工可评审这份计划。保持计划模式，简要收尾并说明计划，等待用户指示。"
)
_NOT_IN_PLAN = (
    "present_plan 只在计划模式可用；当前不在计划模式。"
    "请直接以回答收尾，不要提交计划；需要制定计划时，让用户先进入计划模式（/plan）。"
)


class PresentPlanTool(BaseTool):
    """Submit the plan for review; the answer decides approve / keep planning."""

    parallel_safe = False
    mutates_world = False

    def __init__(self, prompt: UserPromptCallback, timeout_seconds: float = 300.0) -> None:
        self._prompt = prompt
        self.timeout_seconds = float(timeout_seconds)

    @property
    def name(self) -> str:
        return "present_plan"

    @property
    def max_duration_seconds(self) -> float | None:
        """Per-call ceiling for the dispatcher (R2a)."""
        return self.timeout_seconds + _TIMEOUT_GRACE

    @property
    def description(self) -> str:
        return (
            "Present your plan for the user's review and, on approval, hand execution to "
            "the runner. Use it at the end of planning. Provide 'plan' as a concise "
            "markdown summary (a title first) for the human, and 'items' as the "
            "executable steps — one entry per step, each with its 'verify' shell "
            "command when the outcome is machine-checkable. Approval makes the system "
            "execute the items automatically — do not execute them yourself; 'keep "
            "planning' brings their feedback back, so revise and call this tool again. "
            "Only available in plan mode."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "plan": {
                    "type": "string",
                    "description": "The plan as markdown: a title and a concise summary.",
                },
                "items": {
                    "type": "array",
                    "description": (
                        "The executable steps, in order. Approval writes these into "
                        "the task list the runner executes; do not also write them "
                        "with todo_write."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": "One step, small and actionable.",
                            },
                            "verify": {
                                "type": "string",
                                "description": (
                                    "Shell command proving this step; omit when the "
                                    "outcome is not machine-checkable."
                                ),
                            },
                        },
                        "required": ["content"],
                    },
                },
            },
            "required": ["plan", "items"],
        }

    async def execute(
        self, params: dict[str, Any], ctx: ToolContext | None = None
    ) -> ToolResult:
        # R03: plan-only, and never over a task another profile owns --
        # approval replaces the list and arms it, so a running goal/step task
        # must be out of reach.  Both refusals fire before parsing or review;
        # a missing context fails closed the same way.
        if ctx is None or getattr(ctx, "mode", "normal") != "plan":
            return ToolResult(success=False, output=_NOT_IN_PLAN, error="not_in_plan")
        task = ctx.task
        if getattr(task, "armed", False) and task.armed_profile != "plan":
            return ToolResult(
                success=False,
                output=(
                    f"目标任务（profile={task.armed_profile or '?'}）正在运行；"
                    "等它结束或先停止它，再提交计划。"
                ),
                error="task_busy",
            )
        plan = str(params.get("plan", "") or "").strip()
        if not plan:
            return ToolResult(
                success=False,
                output="error: 'plan' is required",
                error="missing_plan",
            )
        # P1: the steps ride this call (trace ec89d5ee measured the one-add-per-
        # iteration burn).  Validated before the human is asked -- a submission
        # that cannot execute must not become a review card.
        items, invalid = _parse_items(params.get("items"))
        if invalid is not None:
            return ToolResult(
                success=False,
                output=f"error: item {invalid} has no 'content'",
                error="invalid_items",
            )
        if not items:
            return ToolResult(
                success=False,
                output=(
                    "计划尚未写成条目：请把全部可执行步骤放进本次调用的 items 数组"
                    "（每步一条，含 verify 命令），然后再次调用 present_plan。"
                ),
                error="no_plan_items",
            )
        try:
            answer = await self._prompt.ask(
                "批准这个计划并开始执行？",
                ("开始执行", "继续规划"),
                detail=plan,
                intent="plan_review",
            )
        except Exception as exc:  # noqa: BLE001 — a tool boundary must not leak
            return ToolResult(
                success=False,
                output=f"error: could not ask the user: {exc}. {_NO_CHANNEL}",
                error="user_prompt_error",
                data={"reason": str(exc)},
            )
        if str(answer.reason) == "no_user":
            return ToolResult(
                success=False,
                output=_NO_CHANNEL,
                error="no_user",
                data={"approved": False, "reason": answer.reason},
            )
        if not answer.answered:
            # Dismissed (esc / empty): information for the model, not a retryable error.
            return ToolResult(
                success=True,
                output=_HOLD,
                data={"approved": False, "dismissed": True},
            )

        choice, feedback = _parse_answer(answer.text)
        if choice == "approve":
            # 11-plan T4 (Q1=A): approval IS the consent — arm the live task
            # here so the closing turn's own save carries it to disk and a
            # crash before the run dispatch still auto-resumes on boot.
            # (R03: the gates above guarantee ctx/task; the old defensive
            # None-checks are gone.)
            # P1: approval is the one write point, and it REPLACES the
            # list -- dsh's shape (``tool-todo/src/index.ts:80`` sends the
            # whole table).  Appending would let a model that pre-wrote
            # items with todo_write leave orphan steps behind, and the
            # runner executes everything open.  The goal stays: it belongs
            # to the session/episode, not to this list.
            #
            # Stale step receipts are not this tool's business: the runner
            # drops the ones whose statement is no longer on the list
            # (``plan_runner._drop_foreign_receipts``).
            ctx.task.items.clear()
            for content, verify in items:
                ctx.task.add(content, verify=verify)
            ctx.task.armed = True
            ctx.task.armed_profile = "plan"
            return ToolResult(success=True, output=_APPROVED, data={"approved": True})
        if feedback:
            return ToolResult(
                success=True,
                output=_KEEP.format(feedback=feedback),
                data={"approved": False},
            )
        return ToolResult(success=True, output=_KEEP_NO_FEEDBACK, data={"approved": False})


def _parse_items(raw: Any) -> tuple[list[tuple[str, str]], int | None]:
    """``(items, invalid_index)`` — ``(content, verify)`` pairs, or the index of
    the first entry missing its ``content`` (``None`` when every entry is valid).

    Lenient about shape on purpose: the model may hand back the JSON string it
    was asked for, and a malformed entry means this submission cannot run -- the
    refusal names the entry rather than raising through the tool boundary.
    """
    if not isinstance(raw, list):
        return [], None
    parsed: list[tuple[str, str]] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            return [], index
        content = str(entry.get("content", "") or "").strip()
        if not content:
            return [], index
        parsed.append((content, str(entry.get("verify", "") or "").strip()))
    return parsed, None


def _parse_answer(text: str) -> tuple[str, str]:
    """(choice, feedback): JSON first, plain words as the fallback (CLI path)."""
    raw = text.strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            choice = str(data.get("choice", "") or "")
            feedback = str(data.get("feedback", "") or "").strip()
            if choice in ("approve", "keep"):
                return choice, feedback
        except ValueError:
            pass
    if raw == "开始执行":
        return "approve", ""
    return "keep", raw
