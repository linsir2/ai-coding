"""Callbacks use TUI provider when set."""

import asyncio

from ai_coding.security.approval import CLIApprovalCallback
from ai_coding.security.user_prompt import CLIUserPromptCallback


def test_user_prompt_set_provider():
    callback = CLIUserPromptCallback()
    calls = []

    async def fake_provider(prompt):
        calls.append(prompt)
        return "user answer"

    callback.set_provider(fake_provider)
    result = asyncio.run(callback.ask("What?"))
    assert result.answered is True
    assert result.text == "user answer"
    assert len(calls) == 1


def test_user_prompt_provider_exception_fails_safe():
    callback = CLIUserPromptCallback()

    async def bad_provider(prompt):
        raise RuntimeError("boom")

    callback.set_provider(bad_provider)
    result = asyncio.run(callback.ask("What?"))
    assert result.answered is False


def test_approval_set_provider():
    callback = CLIApprovalCallback()
    calls = []

    async def fake_provider(prompt):
        calls.append(prompt)
        return "y"

    callback.set_provider(fake_provider)
    result = asyncio.run(callback.approve("bash", {"cmd": "ls"}, "WARN"))
    assert result.value == "yes"
    assert len(calls) == 1


def test_approval_provider_exception_fails_closed():
    callback = CLIApprovalCallback()

    async def bad_provider(prompt):
        raise RuntimeError("boom")

    callback.set_provider(bad_provider)
    result = asyncio.run(callback.approve("bash", {"cmd": "ls"}, "WARN"))
    assert result.value == "no"
