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

    async def fake_provider(tool_name, params, reason):
        calls.append((tool_name, params, reason))
        return "y"

    callback.set_provider(fake_provider)
    result = asyncio.run(callback.approve("bash", {"cmd": "ls"}, "WARN"))
    assert result.value == "yes"
    assert len(calls) == 1


def test_approval_provider_receives_the_params_untruncated():
    """The provider is the UI: it must see what the gate decided on.

    The prompt string truncates ``params`` to 120 chars, and that truncation
    used to be the *only* thing the modal got, so a long ``bash`` command was
    approved without ever being displayed.  The structured call carries the
    dict across the boundary instead of a string the UI has to re-parse.
    """
    callback = CLIApprovalCallback()
    seen: list[object] = []

    async def fake_provider(tool_name, params, reason):
        seen.append(params)
        return "n"

    callback.set_provider(fake_provider)
    command = "pytest -q " + "x" * 300
    asyncio.run(callback.approve("bash", {"command": command}, "WARN"))
    assert seen == [{"command": command}]
    assert len(str(seen[0])) > 120


def test_approval_provider_sees_nested_params_intact():
    callback = CLIApprovalCallback()
    seen: list[object] = []

    async def fake_provider(tool_name, params, reason):
        seen.append(params)
        return "n"

    callback.set_provider(fake_provider)
    params = {"path": "a.py", "meta": {"k": [1, 2, {"z": 3}]}}
    asyncio.run(callback.approve("write", params, "WARN"))
    assert seen == [params]


def test_approval_stdin_prompt_is_unchanged_by_the_structured_provider():
    """The non-UI path still reads a formatted line from stdin, 120-char cut and all.

    The long case matters: 117 + "..." is the exact constant, so a change to the
    cutoff has to fail here rather than silently reshaping every CLI prompt.
    """
    from ai_coding.security.approval import CLIApprovalCallback as C

    assert C._format_prompt("bash", {"command": "ls"}, "read-only") == (
        "[bash] read-only\n  params: {'command': 'ls'}\nAllow? [y/N/s] "
    )
    long = C._format_prompt("bash", {"command": "x" * 300}, "read-only")
    params_line = next(line for line in long.splitlines() if "params:" in line)
    # str(params)[:117] + "..." — 13 chars are "{'command': '", so 104 x's fit
    # and the closing brace is the 118th character, i.e. it is cut.
    assert params_line == "  params: {'command': '" + "x" * 104 + "..."
    assert long.endswith("Allow? [y/N/s] ")


def test_approval_provider_exception_fails_closed():
    callback = CLIApprovalCallback()

    async def bad_provider(tool_name, params, reason):
        raise RuntimeError("boom")

    callback.set_provider(bad_provider)
    result = asyncio.run(callback.approve("bash", {"cmd": "ls"}, "WARN"))
    assert result.value == "no"
