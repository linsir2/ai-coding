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
async def test_approval_shows_a_long_command_in_full():
    """A 300-char command is exactly the case the operator must read."""
    app = _Host()
    command = "pytest -q " + "x" * 300
    async with app.run_test() as pilot:
        app.push_screen(ApprovalScreen("bash", {"command": command}, "read-only"))
        await pilot.pause()
        text = app.screen.query_one("#approval-body").content.plain
        assert command in text


@pytest.mark.asyncio
async def test_approval_renders_nested_params():
    app = _Host()
    params = {"path": "a.py", "meta": {"k": [1, 2, {"z": 3}]}}
    async with app.run_test() as pilot:
        app.push_screen(ApprovalScreen("write", params, "tool 'write' requires confirmation"))
        await pilot.pause()
        text = app.screen.query_one("#approval-body").content.plain
        assert "a.py" in text
        assert "meta" in text


@pytest.mark.asyncio
async def test_approval_empty_params_no_empty_braces():
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(ApprovalScreen("bash", {}, "read-only"))
        await pilot.pause()
        text = app.screen.query_one("#approval-body").content.plain
        assert "{}" not in text


@pytest.mark.asyncio
async def test_approval_bare_enter_denies():
    """A reflexive Enter must not approve.

    The modal is a trust boundary: whichever answer a keypress produces by
    accident has to be the safe one.  The old prompt-toolkit UI was a text
    input where an empty answer fell through to NO, so this is restoring the
    old default rather than inventing one.
    """
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(
            ApprovalScreen("bash", {"command": "ls"}, "read-only"), app.results.append
        )
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.results[-1] == "n"


@pytest.mark.asyncio
async def test_approval_typing_no_denies():
    """Pressing "n" must not be swallowed by a focused button."""
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(
            ApprovalScreen("bash", {"command": "ls"}, "read-only"), app.results.append
        )
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        assert app.results[-1] == "n"


@pytest.mark.asyncio
async def test_approval_typing_yes_allows():
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(
            ApprovalScreen("bash", {"command": "ls"}, "read-only"), app.results.append
        )
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert app.results[-1] == "y"


@pytest.mark.asyncio
async def test_approval_typing_s_stops():
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(
            ApprovalScreen("bash", {"command": "ls"}, "read-only"), app.results.append
        )
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        assert app.results[-1] == "s"


@pytest.mark.asyncio
async def test_approval_starts_focused_on_deny():
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(ApprovalScreen("bash", {"command": "ls"}, "read-only"))
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "no"


@pytest.mark.asyncio
async def test_approval_advertises_the_keys():
    """A keyboard-only user has to be told which keys work."""
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(ApprovalScreen("bash", {"command": "ls"}, "read-only"))
        await pilot.pause()
        hint = app.screen.query_one("#approval-hint").content
        plain = hint.plain if hasattr(hint, "plain") else str(hint)
        assert "y" in plain and "n" in plain and "s" in plain


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
