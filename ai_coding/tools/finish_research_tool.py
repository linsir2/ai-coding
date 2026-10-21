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

import re
from typing import Any

from ai_coding.core.request_assembly import human_utterance
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
_IMPLEMENTATION_REQUEST = (
    "本次请求要的是实现（{verb}），不是研究结论：研究出口只回答"
    "「找出/评估/调研」这类不需要动手的请求。请改用 present_plan 交一份"
    "可执行计划（每步一条，含 verify 命令），人批准后由系统执行。"
)

#: The implementation verbs (the anti-abuse backstop).  Small on purpose: the
#: prompt gives the model the real judgement (it reads the request and knows
#: whether the answer needs code), and this only catches the obvious "do this"
#: requests.  A research question must never be pushed back into a plan --
#: that malformation is the one this whole exit exists to kill (15 号稿
#: D1/D2).
#:
#: Chinese first (the harness speaks the user's language), English second
#: (mixed input is the norm).  The English ones are matched on word
#: boundaries, so "implementation" is not a request to implement.
_IMPLEMENTATION_VERBS_CN: tuple[str, ...] = (
    "实现",
    "新增",
    "加上",
    "加一个",
    "加个",
    "修改",
    "改成",
    "改一下",
    "改代码",
    "修复",
    "重构",
    "写一个",
    "删除",
)
_IMPLEMENTATION_VERBS_EN: tuple[str, ...] = (
    "implement",
    "add",
    "fix",
    "refactor",
    "delete",
    "remove",
)
_EN_VERB_RE = re.compile(
    r"\b(?:" + "|".join(_IMPLEMENTATION_VERBS_EN) + r")(?:ed|es|s|ing|d)?\b",
    re.IGNORECASE,
)
#: The Chinese verbs are matched as plain substrings (Chinese has no word
#: boundaries), longest first so "加一个" wins over "加个".
_CN_VERB_RE = re.compile(
    "|".join(
        re.escape(verb)
        for verb in sorted(_IMPLEMENTATION_VERBS_CN, key=len, reverse=True)
    )
)

#: The clause boundary.  A request is read clause by clause, because the
#: negation is scoped to the clause it sits in ("不要改代码，只要调研" asks
#: for nothing; "只加一个函数" asks for the work).
_CLAUSE_SPLIT = re.compile(r"[，。；！？、,;!?\n\r：:]+")

#: "Don't" markers.  A clause that carries one next to the verb is not asking
#: for the work -- it is asking not to have it, which is exactly what a
#: research request says ("调研…，不要提出实现改动").
_NEGATION_CN: tuple[str, ...] = ("不", "别", "没", "无", "勿", "甭", "莫", "禁")
_NEGATION_EN = re.compile(
    r"\b(?:not|never|no|without|avoid|don'?t|do not|no need)\b", re.IGNORECASE
)

#: The Chinese word for "implement" is also a noun ("这个实现" is a thing to
#: look at, not a thing to do).  A demonstrative right before the verb means
#: the noun reading, so it is not a request for work -- the English side gets
#: the same effect from its word boundary ("the implementation" is not
#: "implement").
_NOUN_USE_CN: tuple[str, ...] = ("这个", "那个", "该", "其", "原", "现有", "当前", "之前")


def _has_negation(text: str) -> bool:
    """Does this piece of the clause say "don't"?"""
    return any(marker in text for marker in _NEGATION_CN) or bool(
        _NEGATION_EN.search(text)
    )


def _implementation_verb(request: str) -> str | None:
    """The implementation verb this request carries, if any.

    Deterministic and small: the point is not to read the request (the model
    does that) but to catch the blunt "implement this" case before the turn
    turns into a plan nobody asked for.  A negated mention does not count.
    """
    for clause in _CLAUSE_SPLIT.split(request):
        for pattern in (_EN_VERB_RE, _CN_VERB_RE):
            for match in pattern.finditer(clause):
                prefix = clause[: match.start()]
                if prefix.endswith(_NOUN_USE_CN):
                    continue
                if _has_negation(prefix) or _has_negation(clause[match.end():]):
                    continue
                return match.group(0)
    return None


class FinishResearchTool(BaseTool):
    """Close a plan-mode turn with a conclusion instead of a plan."""

    parallel_safe = False
    mutates_world = False
    #: N1: the exit belongs to the planning posture.  Outside it the tool is
    #: not advertised at all (the tool table is a contract), and the mode
    #: gate below refuses any call that slips through.
    plan_only = True

    @property
    def name(self) -> str:
        return "finish_research"

    @property
    def description(self) -> str:
        return (
            "Close a research task with your conclusion instead of a plan. Use it "
            "when the request needs no implementation — a survey, an analysis, a "
            "comparison — so that no plan is submitted and nothing is executed. A "
            "request that asks for changes (implement, modify, add, fix, refactor) "
            "needs a plan, not a conclusion: submit it with present_plan instead. "
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
        # The anti-abuse backstop: this exit answers requests that need no
        # implementation.  A request that asks for work belongs to
        # present_plan -- a plan the human never wanted is worse than no
        # plan at all (15 号稿 D1/D2).
        verb = _implementation_verb(human_utterance(ctx.session_messages))
        if verb:
            return ToolResult(
                success=False,
                output=_IMPLEMENTATION_REQUEST.format(verb=verb),
                error="implementation_request",
            )
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
