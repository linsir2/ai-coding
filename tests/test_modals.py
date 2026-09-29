"""Approval and ask_user screens: return values and Esc semantics."""

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Button, Input

from ai_coding.ui.modals import ApprovalScreen, AskScreen


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
