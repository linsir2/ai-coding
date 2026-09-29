"""Approval and ask_user screens: return values and Esc semantics."""

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Button, Input

from ai_coding.ui.modals import ApprovalScreen, AskScreen, parse_approval_prompt


class _Host(App[str]):
    def compose(self) -> ComposeResult:
        yield Button("open", id="open")

    def __init__(self) -> None:
        super().__init__()
        self.results: list[str] = []


@pytest.mark.asyncio
async def test_approval_returns_the_parser_strings():
    app = _Host()
    async with app.run_test() as pilot:
        for button_id, expected in (("#yes", "y"), ("#no", "n"), ("#stop", "s")):
            app.push_screen(
                ApprovalScreen("bash", {"command": "ls"}, "read-only"),
                app.results.append,
            )
            await pilot.pause()
            await pilot.click(button_id)
            await pilot.pause()
            assert app.results[-1] == expected


@pytest.mark.asyncio
async def test_approval_shows_tool_and_reason():
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(ApprovalScreen("bash", {"command": "ls"}, "read-only"))
        await pilot.pause()
        text = app.screen.query_one("#approval-body").content.plain
        assert "bash" in text
        assert "read-only" in text


@pytest.mark.asyncio
async def test_approval_empty_params_no_empty_braces():
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(ApprovalScreen("bash", {}, "read-only"))
        await pilot.pause()
        text = app.screen.query_one("#approval-body").content.plain
        assert "{}" not in text


@pytest.mark.asyncio
async def test_approval_escape_fails_closed():
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(
            ApprovalScreen("bash", {"command": "ls"}, "read-only"), app.results.append
        )
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.results[-1] == "n"


@pytest.mark.asyncio
async def test_approval_unknown_button_id_fails_closed():
    screen = ApprovalScreen("bash", {"command": "ls"}, "read-only")
    dismissed: list[str] = []
    screen.dismiss = dismissed.append  # type: ignore[method-assign]

    class _UnknownButtonEvent:
        class button:
            id = None

    screen.on_button_pressed(_UnknownButtonEvent())  # type: ignore[arg-type]
    assert dismissed == ["n"]


def test_parse_approval_prompt_extracts_fields():
    prompt = (
        "[bash] bash command requires confirmation\n"
        "  params: {'command': 'ls -la'}\n"
        "Allow? [y/N/s] "
    )
    tool_name, reason, params = parse_approval_prompt(prompt)
    assert tool_name == "bash"
    assert reason == "bash command requires confirmation"
    assert params == {"command": "ls -la"}


def test_parse_approval_prompt_drops_allow_suffix():
    prompt = "[read] read-only\n  params: {}\nAllow? [y/N/s] "
    tool_name, reason, params = parse_approval_prompt(prompt)
    assert tool_name == "read"
    assert reason == "read-only"
    assert "Allow?" not in reason
    assert params == {}


def test_parse_approval_prompt_truncated_params_become_empty():
    prompt = "[bash] run\n  params: {'command': 'ls -la && echo hello wor\nAllow? [y/N/s] "
    tool_name, reason, params = parse_approval_prompt(prompt)
    assert tool_name == "bash"
    assert params == {}


@pytest.mark.asyncio
async def test_ask_returns_typed_answer():
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(AskScreen("Which file?", ("a.py", "b.py")), app.results.append)
        await pilot.pause()
        app.screen.query_one("#ask-input", Input).value = "a.py"
        await pilot.click("#ask-send")
        await pilot.pause()
        assert app.results[-1] == "a.py"


@pytest.mark.asyncio
async def test_ask_question_with_brackets_is_not_treated_as_markup():
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(AskScreen("Which file? [/path]"), app.results.append)
        await pilot.pause()
        content = app.screen.query_one("#ask-question").content
        assert "[/path]" in (content.plain if hasattr(content, "plain") else str(content))


@pytest.mark.asyncio
async def test_ask_escape_returns_empty_string():
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(AskScreen("Which file?"), app.results.append)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.results[-1] == ""


@pytest.mark.asyncio
async def test_ask_answer_does_not_leak_to_the_apps_submit_handler():
    """A modal answer must never bubble up as a main-prompt submission."""
    seen: list[str] = []

    class _HostApp(_Host):
        def on_input_submitted(self, event: Input.Submitted) -> None:
            seen.append(event.value)

    app = _HostApp()
    async with app.run_test() as pilot:
        app.push_screen(AskScreen("Which file?"), app.results.append)
        await pilot.pause()
        app.screen.query_one("#ask-input", Input).value = "a.py"
        await pilot.press("enter")
        await pilot.pause()
        assert app.results[-1] == "a.py"
        assert seen == []
