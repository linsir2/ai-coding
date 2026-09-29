"""The Textual shell: status bar, transcript area, input bar."""

import asyncio
from types import SimpleNamespace

import pytest
from textual.widgets import Input, Markdown, Static

from ai_coding.ui.status_bar import StatusBar
from ai_coding.ui.textual_app import AICodingApp


def _app(**overrides):
    kwargs = {
        "loop": None,
        "session": object(),
        "sessions": object(),
        "workspace": "~/projects/ai-app",
        "model_name": "DeepSeek-V3",
    }
    kwargs.update(overrides)
    return AICodingApp(**kwargs)


@pytest.mark.asyncio
async def test_shell_mounts_with_status_transcript_and_input():
    app = _app()
    async with app.run_test():
        assert app.query_one(StatusBar)
        assert app.query_one("#transcript")
        prompt = app.query_one("#prompt", Input)
        assert prompt.placeholder == "Type a message or enter a command..."
        assert app.query_one("#input-hint").content == "/ for commands"
        assert "● Ready" in app.query_one("#status-right").content.plain
        assert "~/projects/ai-app" in app.query_one("#status-left").content.plain


@pytest.mark.asyncio
async def test_status_bar_switches_to_working():
    app = _app()
    async with app.run_test() as pilot:
        bar = app.query_one(StatusBar)
        bar.set_busy(True)
        await pilot.pause()
        assert "● Working" in app.query_one("#status-right").content.plain


class _FakeLoop:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.workspace = "/tmp/ws"
        self.model_name = "fake-model"

    async def process_input(self, session, text, *, on_delta=None, observer=None):
        self.calls.append(text)
        if on_delta is not None:
            on_delta("## 回答\n\n")
            on_delta("**好**")
        return "## 回答\n\n**好**"

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_submit_runs_the_loop_and_streams_markdown():
    loop = _FakeLoop()
    app = _app(loop=loop)
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "你好"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        assert loop.calls == ["你好"]
        assert "你好" in _all_static_text(app)
        assert "好" in app.query_one(Markdown).source


@pytest.mark.asyncio
async def test_input_is_disabled_while_a_turn_runs():
    loop = _BlockingLoop()
    app = _app(loop=loop)
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "第一次"
        await pilot.press("enter")
        await pilot.pause()
        assert app.query_one("#prompt").disabled
        loop.release()
        await pilot.pause()
        await pilot.pause()
        assert not app.query_one("#prompt").disabled


class _BlockingLoop(_FakeLoop):
    def __init__(self) -> None:
        super().__init__()
        self._gate = asyncio.Event()

    def release(self) -> None:
        self._gate.set()

    async def process_input(self, session, text, *, on_delta=None, observer=None):
        await self._gate.wait()
        return await super().process_input(
            session, text, on_delta=on_delta, observer=observer
        )


def _all_static_text(app) -> str:
    return "\n".join(
        w.content.plain if hasattr(w.content, "plain") else str(w.content)
        for w in app.query(Static)
    )


@pytest.mark.asyncio
async def test_slash_command_runs_through_handle_input(monkeypatch):
    loop = _FakeLoop()
    app = _app(loop=loop)
    seen: list[str] = []

    async def fake_handle_input(loop_, text, session, sessions, console):
        seen.append(text)
        console.info("[status] ok")
        from ai_coding.ui.repl import REPLAction
        return REPLAction.CONTINUE, session

    monkeypatch.setattr("ai_coding.ui.textual_app.handle_input", fake_handle_input)
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/status"
        await pilot.press("enter")
        await pilot.pause()
        assert seen == ["/status"]
        assert "[status] ok" in _all_static_text(app)
        assert loop.calls == []  # slash does not go to the model


@pytest.mark.asyncio
async def test_permissions_switch_re_attaches_the_provider():
    from ai_coding.security.approval import CLIApprovalCallback

    app = _app(loop=_FakeLoop())
    async with app.run_test():
        fresh = CLIApprovalCallback()
        dispatcher = SimpleNamespace(approval=fresh)
        app._ensure_approval_provider(dispatcher)
        assert fresh._provider == app.approval_provider
