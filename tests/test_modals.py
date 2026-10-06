"""Approval and ask_user screens: return values and Esc semantics."""

from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Button, Input

from ai_coding.ui.modals import ApprovalScreen, AskScreen


class _Host(App[str]):
    # The real app's stylesheet: the button-height and params assertions are
    # about our CSS and our compose(), not about Textual's widget defaults.
    CSS_PATH = str(Path(__file__).resolve().parents[1] / "ai_coding/ui/app.tcss")

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


@pytest.mark.asyncio
async def test_approval_buttons_are_one_row_tall():
    """Textual `Button` 默认 `border: tall`，1 行内容 + 上下边框 = 3 行。

    实测三个按钮各占 3 行（`▔▔▔` / Allow / `▁▁▁`），一个确认弹窗白吃 3 行。
    """
    app = _Host()
    async with app.run_test(size=(80, 24)) as pilot:
        app.push_screen(ApprovalScreen("bash", {"command": "ls"}, "read-only"))
        await pilot.pause()
        for button in app.screen.query(Button):
            assert button.outer_size.height == 1, (
                f"按钮 {button.label!r} 高 {button.outer_size.height}，应为 1"
            )


@pytest.mark.asyncio
async def test_approval_shows_params_as_key_value_lines():
    """弹窗把 params 直接 `str()`，屏幕上出现 Python dict repr。

    实测：`{'command': 'ls -la'}`。用户要判断的是"这条命令会做什么"，不是数据结构的
    括号引号；每个 key 一行、值截断，才是要读的信息。
    """
    app = _Host()
    async with app.run_test(size=(80, 24)) as pilot:
        app.push_screen(ApprovalScreen("bash", {"command": "ls -la"}, "read-only"))
        await pilot.pause()
        text = app.screen.query_one("#approval-body").content.plain
        assert "command = ls -la" in text, text
        assert "{" not in text, f"弹窗仍在显示 dict repr：{text}"


@pytest.mark.asyncio
async def test_approval_shows_edit_as_a_diff():
    """edit 的 old/new 当普通参数显示看不出"改了什么"；diff 才是要读的信息。

    断言整行而不是子串：`lineterm=""` 会吃掉 `--- 原` 后的换行，所有 diff 行
    粘成一行，子串断言照样通过。
    """
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(ApprovalScreen(
            "edit",
            {"path": "a.py", "old_string": "old line", "new_string": "new line"},
            "tool 'edit' requires confirmation",
        ))
        await pilot.pause()
        text = app.screen.query_one("#approval-body").content.plain
        lines = text.splitlines()
        assert "a.py" in text
        assert "-old line" in lines, lines
        assert "+new line" in lines, lines
        assert "old_string =" not in text, f"edit 仍在显示原始参数：{text}"


@pytest.mark.asyncio
async def test_approval_edit_diff_spans_multiple_lines():
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(ApprovalScreen(
            "edit",
            {
                "path": "a.py",
                "old_string": "one\ntwo\nthree",
                "new_string": "one\nTWO\nthree",
            },
            "",
        ))
        await pilot.pause()
        lines = app.screen.query_one("#approval-body").content.plain.splitlines()
        assert "-two" in lines, lines
        assert "+TWO" in lines, lines
        assert "-one" not in lines, "未改动的行不该出现在 +/- 里"


@pytest.mark.asyncio
async def test_approval_always_button_returns_a():
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(
            ApprovalScreen("edit", {"path": "a.py", "old_string": "x", "new_string": "y"}, ""),
            app.results.append,
        )
        await pilot.pause()
        await pilot.click("#always")
        await pilot.pause()
        assert app.results[-1] == "a"


@pytest.mark.asyncio
async def test_approval_always_button_is_advertised():
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(
            ApprovalScreen("edit", {"path": "a.py", "old_string": "x", "new_string": "y"}, "")
        )
        await pilot.pause()
        assert app.screen.query_one("#always", Button) is not None
        hint = app.screen.query_one("#approval-hint").content
        plain = hint.plain if hasattr(hint, "plain") else str(hint)
        assert "a always" in plain


@pytest.mark.asyncio
async def test_approval_hides_always_for_bash():
    """bash 的 always 等于放弃所有命令审查，所以不给这个按钮。"""
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(ApprovalScreen("bash", {"command": "ls"}, "read-only"))
        await pilot.pause()
        assert not app.screen.query("#always")
        hint = app.screen.query_one("#approval-hint").content
        plain = hint.plain if hasattr(hint, "plain") else str(hint)
        assert "a always" not in plain


@pytest.mark.asyncio
async def test_write_still_shows_key_value_lines():
    """write 没有 old_string，做不出 diff；保持 key=value（嵌套参数测试依赖它）。"""
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(ApprovalScreen("write", {"path": "a.py", "content": "body"}, ""))
        await pilot.pause()
        text = app.screen.query_one("#approval-body").content.plain
        assert "path = a.py" in text
        assert "content = body" in text


@pytest.mark.asyncio
async def test_approval_edit_surfaces_replace_all():
    """replace_all 把一次替换变成整文件重写，diff 本身看不出这件事。"""
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(ApprovalScreen(
            "edit",
            {"path": "a.py", "old_string": "x", "new_string": "y", "replace_all": True},
            "",
        ))
        await pilot.pause()
        assert "replace_all" in app.screen.query_one("#approval-body").content.plain

        app.pop_screen()
        await pilot.pause()
        app.push_screen(ApprovalScreen(
            "edit",
            {"path": "a.py", "old_string": "x", "new_string": "y", "replace_all": False},
            "",
        ))
        await pilot.pause()
        assert "replace_all" not in app.screen.query_one("#approval-body").content.plain


@pytest.mark.asyncio
async def test_approval_a_does_nothing_for_bash():
    """The `a` key is bound on every tool, so hiding the Always button is not
    enough: an excluded tool has to refuse the key too, or `a` auto-approves a
    bash command without ever recording it.
    """
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(
            ApprovalScreen("bash", {"command": "ls"}, "read-only"), app.results.append
        )
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        assert app.results == [], "bash 弹窗不应被 `a` 关闭"
        assert app.screen.__class__.__name__ == "ApprovalScreen"
