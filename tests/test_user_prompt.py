"""The question channel (R6c): a human, or an honest "there is no human"."""

from __future__ import annotations

import pytest

from ai_coding.security.user_prompt import (
    CLIUserPromptCallback,
    UnavailableUserPromptCallback,
)


@pytest.mark.asyncio
async def test_cli_callback_returns_the_typed_answer():
    prompts: list[str] = []

    def scripted(prompt: str) -> str:
        prompts.append(prompt)
        return "  改 A  "

    answer = await CLIUserPromptCallback(input_fn=scripted).ask(
        "改 A 还是 B？", options=["A", "B"]
    )
    assert answer.answered is True
    assert answer.text == "改 A"  # 前后空白被去掉
    assert "改 A 还是 B？" in prompts[0]
    assert "A" in prompts[0] and "B" in prompts[0]  # 选项要显示出来


@pytest.mark.asyncio
async def test_an_empty_answer_is_not_an_answer():
    answer = await CLIUserPromptCallback(input_fn=lambda _prompt: "   ").ask("在吗？")
    assert answer.answered is False
    assert answer.reason == "empty"


@pytest.mark.asyncio
async def test_eof_is_unavailable_not_a_crash():
    def boom(_prompt: str) -> str:
        raise EOFError

    answer = await CLIUserPromptCallback(input_fn=boom).ask("在吗？")
    assert answer.answered is False
    assert answer.reason == "eof"


@pytest.mark.asyncio
async def test_unavailable_callback_never_blocks():
    answer = await UnavailableUserPromptCallback().ask("在吗？")
    assert answer.answered is False
    assert answer.reason == "no_user"
    assert answer.text == ""


@pytest.mark.asyncio
async def test_the_default_reader_is_the_builtin_input():
    """Today's behaviour: `ask` and any non-REPL caller keep using input()."""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("builtins.input", lambda _prompt="": "typed")
    try:
        answer = await CLIUserPromptCallback().ask("在吗？")
    finally:
        monkeypatch.undo()
    assert answer.answered is True
    assert answer.text == "typed"


@pytest.mark.asyncio
async def test_without_a_reader_the_input_path_still_works():
    callback = CLIUserPromptCallback(input_fn=lambda _prompt: "老路径")
    answer = await callback.ask("在吗？")
    assert answer.text == "老路径"
