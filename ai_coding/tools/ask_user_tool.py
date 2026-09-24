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


class AskUserTool(BaseTool):
    """Ask the user a question and wait for the answer (bounded by the turn clock)."""

    # One human, one question at a time: never run this beside another tool.
    parallel_safe = False

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
            "'options' to offer concrete choices. In a non-interactive run nobody can "
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
                output="error: 'question' is required",
                error="missing_question",
            )
        raw_options = params.get("options")
        options = (
            tuple(str(option) for option in raw_options)
            if isinstance(raw_options, (list, tuple))
            else ()
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
