"""ask_user: a blocking question whose absence of a human is an answer (R6c)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from ai_coding.security.user_prompt import (
    UnavailableUserPromptCallback,
    UserAnswer,
    UserPromptCallback,
)
from ai_coding.tools.ask_user_tool import AskUserTool


class _FixedPrompt:
    """A scripted human that records what it was asked."""

    def __init__(self, answer: UserAnswer) -> None:
        self.answer = answer
        self.seen: list[tuple[str, tuple[str, ...]]] = []

    async def ask(self, question: str, options: Sequence[str] = ()) -> UserAnswer:
        self.seen.append((question, tuple(options)))
        return self.answer


class _Boom:
    """A prompt channel that blows up (no tty, closed stdin, …)."""

    async def ask(self, question: str, options: Sequence[str] = ()) -> UserAnswer:
        raise RuntimeError("no tty")


def _tool(prompt: UserPromptCallback, **kwargs: Any) -> AskUserTool:
    return AskUserTool(prompt, **kwargs)


@pytest.mark.asyncio
async def test_answered_question_is_structured_data():
    prompt = _FixedPrompt(UserAnswer(answered=True, text="改 A"))
    result = await _tool(prompt).execute(
        {"question": "改 A 还是 B？", "options": ["A", "B"]}
    )
    assert result.success is True
    assert result.data["answered"] is True
    assert result.data["answer"] == "改 A"
    assert "改 A" in result.output
    assert prompt.seen == [("改 A 还是 B？", ("A", "B"))]  # options 传到了通道


@pytest.mark.asyncio
async def test_no_human_tells_the_model_to_decide():
    result = await _tool(UnavailableUserPromptCallback()).execute({"question": "改哪个？"})
    assert result.success is False
    assert result.error == "no_user"
    assert "假设" in result.output  # 教模型写清假设后自己收尾


@pytest.mark.asyncio
async def test_a_silent_human_is_not_an_answer_either():
    prompt = _FixedPrompt(UserAnswer(answered=False, reason="eof"))
    result = await _tool(prompt).execute({"question": "改哪个？"})
    assert result.success is False
    assert result.error == "no_answer"
    assert "假设" in result.output


@pytest.mark.asyncio
async def test_callback_failure_is_a_tool_failure():
    result = await _tool(_Boom()).execute({"question": "在吗？"})
    assert result.success is False
    assert result.error == "user_prompt_error"
    assert result.data["reason"] == "no tty"


@pytest.mark.asyncio
async def test_missing_question_is_rejected():
    prompt = _FixedPrompt(UserAnswer(answered=True, text="x"))
    result = await _tool(prompt).execute({"question": "   "})
    assert result.success is False
    assert result.error == "missing_question"
    assert "question" in result.output
    assert "options" in result.output
    assert prompt.seen == []  # 没问就不该打扰人


def test_the_schema_names_the_required_param_the_model_kept_guessing():
    """Measured: a deferred schema let the model send ``query`` instead of
    ``question``; the refusal now names the expected params, and the tool is
    not deferred (see DEFERRED_TOOL_NAMES)."""
    tool = _tool(UnavailableUserPromptCallback())
    schema = tool.params_json_schema
    assert schema["required"] == ["question"]
    assert set(schema["properties"]) == {"question", "options"}
    assert tool.defer is False


def test_the_tool_is_not_parallel_safe():
    assert _tool(UnavailableUserPromptCallback()).parallel_safe is False


def test_the_tool_declares_its_wait_bound():
    tool = _tool(UnavailableUserPromptCallback(), timeout_seconds=30.0)
    assert tool.max_duration_seconds == 45.0  # timeout + 15s 收割余量，与 bash/subAgent 同款


@pytest.mark.asyncio
async def test_plan_mode_ask_user_cannot_stand_in_for_plan_approval():
    """Observed: the model asked "同意按计划执行?" through ask_user; the human
    said yes, but the harness only exits plan mode on the plan_implement card."""
    from ai_coding.tools.base import ToolContext

    prompt = _FixedPrompt(UserAnswer(answered=True, text="同意，按计划执行"))
    result = await _tool(prompt).execute(
        {
            "question": "以下是我整理出的计划，请确认是否同意执行？",
            "options": ["同意，按计划执行", "有修改意见"],
        },
        ToolContext(mode="plan"),
    )

    assert result.success is False
    assert result.error == "plan_approval_not_ask_user"
    assert "<proposed_plan>" in result.output
    assert prompt.seen == []


@pytest.mark.asyncio
async def test_plan_mode_ask_user_still_allows_a_material_spec_question():
    from ai_coding.tools.base import ToolContext

    prompt = _FixedPrompt(UserAnswer(answered=True, text="/healthz"))
    result = await _tool(prompt).execute(
        {"question": "健康检查端点用 /health 还是 /healthz？"},
        ToolContext(mode="plan"),
    )

    assert result.success is True
    assert prompt.seen


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "计划可以吗？",
        "确认执行吗？",
        "可以开始了吗？",
        "按这个计划做吗？",
    ],
)
async def test_common_plan_approval_phrasings_are_refused(question):
    from ai_coding.tools.base import ToolContext

    prompt = _FixedPrompt(UserAnswer(answered=True, text="是"))
    result = await _tool(prompt).execute(
        {"question": question}, ToolContext(mode="plan")
    )

    assert result.error == "plan_approval_not_ask_user"
    assert prompt.seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "Should we proceed with Postgres or SQLite?",
        "proceed_flag 的默认值用 true 还是 false？",
        "计划的第 2 步用 Postgres 还是 SQLite？",
        "执行计划前要先备份吗？",
    ],
)
async def test_material_plan_questions_still_reach_the_human(question):
    from ai_coding.tools.base import ToolContext

    prompt = _FixedPrompt(UserAnswer(answered=True, text="用 Postgres"))
    result = await _tool(prompt).execute(
        {"question": question}, ToolContext(mode="plan")
    )

    assert result.success is True
    assert prompt.seen


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "按这个计划，先跑迁移还是先改接口？",
        "计划里的 approve 接口用 POST 还是 GET？",
    ],
)
async def test_plan_detail_questions_are_not_mistaken_for_approval(question):
    from ai_coding.tools.base import ToolContext

    prompt = _FixedPrompt(UserAnswer(answered=True, text="先跑迁移"))
    result = await _tool(prompt).execute(
        {"question": question}, ToolContext(mode="plan")
    )

    assert result.success is True
    assert prompt.seen


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "现在开始执行吗？",
        "可以执行了吗？",
        "准备好了吗？",
        "shall we proceed?",
        "是否同意这个计划？",
        "这个计划可以执行吗？",
    ],
)
async def test_more_approval_shapes_are_refused(question):
    from ai_coding.tools.base import ToolContext

    prompt = _FixedPrompt(UserAnswer(answered=True, text="是"))
    result = await _tool(prompt).execute(
        {"question": question}, ToolContext(mode="plan")
    )
    assert result.error == "plan_approval_not_ask_user"
    assert prompt.seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "开始执行计划前要先备份吗？",
        "确认执行计划前要不要先备份？",
        "批准计划前需要谁签字？",
        "Should I proceed with Postgres or SQLite?",
        "Shall I proceed with the migration now or after the API change?",
    ],
)
async def test_approval_words_in_spec_questions_do_not_block(question):
    from ai_coding.tools.base import ToolContext

    prompt = _FixedPrompt(UserAnswer(answered=True, text="先备份"))
    result = await _tool(prompt).execute(
        {"question": question}, ToolContext(mode="plan")
    )
    assert result.success is True
    assert prompt.seen
