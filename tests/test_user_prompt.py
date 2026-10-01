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


@pytest.mark.asyncio
async def test_the_provider_receives_the_question_and_the_options_apart():
    """The modal can only list options if it is handed them, not a blob.

    ``_format_prompt`` flattens question+options into one string, so
    ``AskScreen``'s options parameter could never be filled: it has been
    dead code since it was written.
    """
    seen: list[tuple[object, ...]] = []

    async def provider(*args: object) -> str:
        seen.append(args)
        return "answer"

    callback = CLIUserPromptCallback()
    callback.set_provider(provider)  # type: ignore[arg-type]
    answer = await callback.ask("改 A 还是 B？", options=["A", "B"])

    assert answer.answered is True
    assert answer.text == "answer"
    assert seen == [("改 A 还是 B？", ["A", "B"])]


@pytest.mark.asyncio
async def test_the_stdin_path_still_gets_one_flattened_prompt():
    """Non-UI callers are unchanged: one string, options folded into it.

    The provider signature changed; ``_format_prompt`` did not, so the CLI and
    every test that drives ``input_fn`` see exactly what they saw before.
    """
    prompts: list[str] = []

    def scripted(prompt: str) -> str:
        prompts.append(prompt)
        return "A"

    answer = await CLIUserPromptCallback(input_fn=scripted).ask(
        "改 A 还是 B？", options=["A", "B"]
    )

    assert answer.text == "A"
    assert len(prompts) == 1
    assert "改 A 还是 B？" in prompts[0]
    assert "（可选：A / B）" in prompts[0]
