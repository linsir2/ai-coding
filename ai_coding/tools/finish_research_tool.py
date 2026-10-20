"""``finish_research`` — the second exit of a plan-mode turn (N1).

A planning turn that answers a research question has nothing to execute, so
it must not be pushed into a plan: the measured malformation was a research
request turned into a plan whose items described work already done and whose
verify commands were invented to match (15 号稿 D1/D2).  This tool is the
other exit — the model states its conclusion, the turn ends, and nothing is
armed, executed or reviewed.

Mutually exclusive with ``present_plan``: the first exit to succeed owns the
turn and the other one is refused (``already_concluded``).  The holder that
carries that fact is the turn's, not the task's — a conclusion is a fact
about one turn, and the task outlives it.
"""

from __future__ import annotations

from typing import Any

from ai_coding.domain.message import ToolResult
from ai_coding.tools.base import BaseTool, ToolContext

__all__ = ["FinishResearchTool"]

_NOT_IN_PLAN = (
    "finish_research 只在计划模式可用；当前不在计划模式。"
    "直接以回答收尾即可，不需要调用本工具。"
)
_ALREADY_CONCLUDED = (
    "本次回合已经有出口了（{done}）：不能再来一个。"
    "请用一句话把结论说清楚，不要再提交计划。"
)
_PLAN_APPROVED = (
    "本次计划已批准，执行由系统接管；请简短收尾，不要开始执行。"
)
_TASK_BUSY = (
    "目标任务（profile={profile}）正在运行；等它结束或先停止它，再收尾。"
)
_MISSING_SUMMARY = (
    "总结不能为空：请用 finish_research 给出结论的要点（一段话即可）。"
)


class FinishResearchTool(BaseTool):
    """Close a plan-mode turn with a conclusion instead of a plan."""

    parallel_safe = False
    mutates_world = False

    @property
    def name(self) -> str:
        return "finish_research"

    @property
    def description(self) -> str:
        return (
            "Close a research task with your conclusion instead of a plan. Use it "
            "when the answer needs no implementation — a survey, an analysis, a "
            "comparison — so that no plan is submitted and nothing is executed. "
            "Give 'summary' as the conclusion itself (what you found and what it "
            "means); it is what the user reads. Mutually exclusive with "
            "present_plan: whichever you call first wins. Only available in plan "
            "mode."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "The conclusion: what you found and what it means.",
                }
            },
            "required": ["summary"],
        }

    async def execute(
        self, params: dict[str, Any], ctx: ToolContext | None = None
    ) -> ToolResult:
        # The gate runs before anything else: the exit belongs to the planning
        # posture, and a missing context fails closed the same way.
        if ctx is None or getattr(ctx, "mode", "normal") != "plan":
            return ToolResult(success=False, output=_NOT_IN_PLAN, error="not_in_plan")
        # The other exit already closed this turn -- one turn, one exit.
        conclusion = getattr(ctx, "conclusion", None)
        done = str(getattr(conclusion, "exit", "") or "")
        if done:
            return ToolResult(
                success=False,
                output=_ALREADY_CONCLUDED.format(done=done),
                error="already_concluded",
            )
        # An approved plan owns the turn: the runner is about to execute it,
        # so a research conclusion would contradict the approval.  A running
        # goal is not this turn's business at all.
        task = ctx.task
        if getattr(task, "armed", False):
            profile = str(getattr(task, "armed_profile", "") or "?")
            if profile == "plan":
                return ToolResult(
                    success=False,
                    output=_PLAN_APPROVED,
                    error="plan_already_approved",
                )
            return ToolResult(
                success=False,
                output=_TASK_BUSY.format(profile=profile),
                error="task_busy",
            )
        summary = str(params.get("summary", "") or "").strip()
        if not summary:
            return ToolResult(success=False, output=_MISSING_SUMMARY, error="missing_summary")
        # The conclusion is the deliverable: it is the tool's output, so it
        # lands in the transcript (the user reads it) and in the model's
        # history.  Nothing else happens -- no items, no arm, no review card.
        if conclusion is not None:
            conclusion.exit = "finish_research"
        return ToolResult(
            success=True,
            output=summary,
            data={"research_finished": True},
        )
