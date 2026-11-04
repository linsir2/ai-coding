"""``ask_user`` — the model asking a human, as a first-class tool (R6c).

The gate asks *us* whether a call may proceed; this asks *them* what we should do.
The answer arrives as the tool's result, which is deliberate: ending a turn "waiting
for the user" would leave an assistant message carrying a ``tool_call`` with no
``tool`` result, and our own pairing defence
(``SessionData.ensure_tool_pairing(strict=True)``) drops exactly that shape before
it is persisted — so the next request would be an illegal transcript
(docs/r6-analysis.md 2.3, D-R6-7).

Not every caller has a human.  A sub-agent runs isolated with nobody to ask, and
``ask`` is a one-shot command, so both get ``UnavailableUserPromptCallback`` and the
tool answers "no_user" with an instruction to decide and say what it assumed —
a missing human is information, not an error to retry.
"""

from __future__ import annotations

from typing import Any

from ai_coding.domain.message import ToolResult
from ai_coding.security.user_prompt import UserPromptCallback
from ai_coding.tools.base import BaseTool, ToolContext

__all__ = ["AskUserTool"]

# Room above the wait for the answer to be rendered and the result to be formed,
# matching bash and subAgent (R2a: "the dispatcher takes min(this, what is left of
# the turn)").
_TIMEOUT_GRACE = 15.0

_DECIDE_ANYWAY = (
    "当前没有人工可回答。请自行选择最合理的做法，明确写出你的假设，然后继续或收尾。"
)
_NO_ANSWER = (
    "没有人给出回答（输入为空或已被关闭）。请自行决定，并明确写出你的假设后收尾。"
)
_PLAN_APPROVAL = (
    "ask_user 不能批准计划。当前仍在 Plan 模式；请直接输出 "
    "<proposed_plan> ... </proposed_plan> 文本块，系统会弹出 "
    "“Implement this plan?” 卡片。只有那张卡片的 Yes 会退出 Plan 模式。"
)
_PLAN_APPROVAL_WHOLE = frozenset(
    {
        "计划可以吗",
        "计划可以执行吗",
        "这个计划可以执行吗",
        "是否同意这个计划",
        "同意这个计划",
        "是否批准这个计划",
        "批准这个计划",
        "确认执行吗",
        "确认执行计划吗",
        "可以开始了吗",
        "按这个计划做吗",
        "现在开始执行吗",
        "可以执行了吗",
        "准备好了吗",
        "shall we proceed",
        "should i proceed",
        "shall i proceed",
    }
)
_PLAN_APPROVAL_ACTIONS = (
    "执行计划",
    "开始执行",
    "按计划执行",
    "按这个计划做",
    "批准计划",
    "同意执行",
    "确认执行",
    "开始了吗",
    "准备好了",
    "implement this plan",
    "proceed",
    "go ahead",
    "start executing",
)
_PLAN_APPROVAL_OPTIONS = (
    "同意",
    "批准",
    "按计划执行",
    "yes",
    "implement this plan",
)
# A question with a choice is a spec question even when it contains an approval
# word ("Should I proceed with Postgres or SQLite?").  The same goes for a
# question about what must happen *before* executing the plan.
_PLAN_SPEC_BLOCKERS = (
    "之前",
    "前要",
    "前先",
    "前需要",
    "要不要",
    "需不需要",
    "还是",
    "哪个",
    "谁",
    " or ",
    "which",
)


def _normalized_question(text: str) -> str:
    return " ".join(text.strip().lower().rstrip("？?！!。. ").split())


def _looks_like_plan_approval(question: str, options: tuple[str, ...]) -> bool:
    """Whether a plan-mode question is asking for whole-plan approval.

    This is deliberately narrow: asking which implementation detail to use is a
    material question and must still reach the human.  Asking "do I execute the
    plan?" is not; the only approval channel is the harness card.  A choice
    marker or a "before the plan" question is never treated as approval.
    """
    q = _normalized_question(question)
    if q in _PLAN_APPROVAL_WHOLE:
        return True
    normalized_options = tuple(_normalized_question(option) for option in options)
    if any(
        option.startswith(_PLAN_APPROVAL_OPTIONS) for option in normalized_options
    ) and any(word in q for word in ("计划", "plan", "执行")):
        return True
    if any(blocker in q for blocker in _PLAN_SPEC_BLOCKERS):
        return False
    asks = question.strip().endswith(("?", "？", "吗", "么"))
    return asks and any(action in q for action in _PLAN_APPROVAL_ACTIONS)



class AskUserTool(BaseTool):
    """Ask the user a question and wait for the answer (bounded by the turn clock)."""

    # One human, one question at a time: never run this beside another tool.
    parallel_safe = False
    mutates_world = False

    def __init__(self, prompt: UserPromptCallback, timeout_seconds: float = 300.0) -> None:
        self._prompt = prompt
        self.timeout_seconds = float(timeout_seconds)

    @property
    def name(self) -> str:
        return "ask_user"

    @property
    def max_duration_seconds(self) -> float | None:
        """Per-call ceiling for the dispatcher (R2a)."""
        return self.timeout_seconds + _TIMEOUT_GRACE

    @property
    def description(self) -> str:
        return (
            "Ask the user a question and wait for their answer. Use it when a decision "
            "is genuinely theirs (which of two files, which of two migrations) and you "
            "cannot settle it from the repository. Provide 'question'; optionally "
            "'options' to offer concrete choices -- write them for THIS situation, the "
            "user can always type their own instead. In a non-interactive run nobody can "
            "answer: you will be told so, and you must decide, state your assumption "
            "and carry on rather than asking again."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question to ask the user; short and specific.",
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional suggested answers to offer.",
                },
            },
            "required": ["question"],
        }

    async def execute(
        self,
        params: dict[str, Any],
        ctx: ToolContext | None = None,
    ) -> ToolResult:
        question = str(params.get("question", "") or "").strip()
        if not question:
            return ToolResult(
                success=False,
                output=(
                    "error: 'question' is required; expected params: "
                    "question (string, required) and optional options (string[])."
                ),
                error="missing_question",
            )
        raw_options = params.get("options")
        options = (
            tuple(str(option) for option in raw_options)
            if isinstance(raw_options, (list, tuple))
            else ()
        )
        # Observed: the model asked "同意按计划执行?" through ask_user, the human
        # said yes, and then nothing happened because only the plan_implement
        # card flips the posture.  Refuse that channel before it reaches a human.
        if getattr(ctx, "mode", "normal") == "plan" and _looks_like_plan_approval(
            question, options
        ):
            return ToolResult(
                success=False,
                output=_PLAN_APPROVAL,
                error="plan_approval_not_ask_user",
                data={"tool": "ask_user", "mode": "plan"},
            )
        try:
            answer = await self._prompt.ask(question, options)
        except Exception as exc:  # noqa: BLE001 — a tool boundary must not leak
            return ToolResult(
                success=False,
                output=f"error: could not ask the user: {exc}. {_DECIDE_ANYWAY}",
                error="user_prompt_error",
                data={"reason": str(exc)},
            )
        if str(answer.reason) == "no_user":
            return ToolResult(
                success=False,
                output=_DECIDE_ANYWAY,
                error="no_user",
                data={"answered": False, "reason": answer.reason},
            )
        if not answer.answered:
            return ToolResult(
                success=False,
                output=_NO_ANSWER,
                error="no_answer",
                data={"answered": False, "reason": answer.reason},
            )
        return ToolResult(
            success=True,
            output=f"用户回答：{answer.text}",
            data={"answered": True, "answer": answer.text},
        )
