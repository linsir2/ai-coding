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
    assert prompt.seen == []  # 没问就不该打扰人


def test_the_tool_is_not_parallel_safe():
    assert _tool(UnavailableUserPromptCallback()).parallel_safe is False


def test_the_tool_declares_its_wait_bound():
    tool = _tool(UnavailableUserPromptCallback(), timeout_seconds=30.0)
    assert tool.max_duration_seconds == 45.0  # timeout + 15s 收割余量，与 bash/subAgent 同款
