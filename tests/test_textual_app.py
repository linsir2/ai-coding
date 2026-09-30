"""The Textual shell: status bar, transcript area, input bar."""

import asyncio
from types import SimpleNamespace

import pytest
from textual.command import CommandPalette
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
async def test_bare_resume_opens_the_session_picker():
    """Bare ``/resume`` must offer a picker, not print an ID list."""
    from ai_coding.domain.session import SessionData

    session = SessionData(
        session_id="0123456789abcdef0123456789abcdef",
        title="",
        created_time="2026-09-30T10:00:00Z",
        last_access_time="2026-09-30T10:44:00Z",
        message_count=5,
        first_preview="Reproduce the payment failure, then add a regression test.",
    )
    app = _app(loop=_FakeLoop(), sessions=SimpleNamespace(list_sessions=lambda: [session]))
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/resume"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        assert CommandPalette.is_open(app)


@pytest.mark.asyncio
async def test_picking_a_session_switches_to_it():
    """Selecting an entry resumes that exact session, not a prefix guess."""
    from ai_coding.domain.session import SessionData

    session = SessionData(
        session_id="0123456789abcdef0123456789abcdef",
        title="",
        created_time="2026-09-30T10:00:00Z",
        last_access_time="2026-09-30T10:44:00Z",
        message_count=5,
        first_preview="Reproduce the payment failure, then add a regression test.",
    )
    app = _app(
        loop=_FakeLoop(),
        sessions=SimpleNamespace(
            list_sessions=lambda: [session],
            resolve=lambda prefix: [session] if session.session_id.startswith(prefix) else [],
        ),
    )
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/resume"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()
        assert app._session is session


@pytest.mark.asyncio
async def test_resume_with_nothing_saved_says_so_instead_of_opening_an_empty_picker():
    """An empty picker looks like the app ignored the command."""
    loop = _FakeLoop()
    app = _app(loop=loop, sessions=SimpleNamespace(list_sessions=lambda: []))
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/resume"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        assert not CommandPalette.is_open(app)
        assert "no saved sessions" in _all_static_text(app)


@pytest.mark.asyncio
async def test_escaping_the_picker_keeps_the_current_session():
    from ai_coding.domain.session import SessionData

    session = SessionData(
        session_id="0123456789abcdef0123456789abcdef",
        title="",
        created_time="2026-09-30T10:00:00Z",
        last_access_time="2026-09-30T10:44:00Z",
        message_count=5,
        first_preview="Reproduce the payment failure, then add a regression test.",
    )
    current = object()
    app = _app(
        loop=_FakeLoop(),
        session=current,
        sessions=SimpleNamespace(list_sessions=lambda: [session]),
    )
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/resume"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        await pilot.pause()
        assert app._session is current


@pytest.mark.asyncio
async def test_resume_with_an_id_still_works():
    """The typed form keeps working; only the bare form becomes a picker."""
    from ai_coding.domain.session import SessionData
    from ai_coding.ui.repl import REPLAction

    target = SessionData(session_id="a" * 32, title="", created_time="", last_access_time="")
    seen: list[str] = []

    async def fake_handle_input(loop_, text, session, sessions, console):
        seen.append(text)
        return REPLAction.CONTINUE, session

    app = _app(loop=_FakeLoop(), sessions=SimpleNamespace(resolve=lambda p: [target]))
    import ai_coding.ui.textual_app as ta

    original = ta.handle_input
    ta.handle_input = fake_handle_input
    try:
        async with app.run_test() as pilot:
            app.query_one("#prompt").value = f"/resume {target.session_id[:12]}"
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            assert seen == [f"/resume {target.session_id[:12]}"]
            assert not CommandPalette.is_open(app)
    finally:
        ta.handle_input = original


@pytest.mark.asyncio
async def test_status_bar_set_model_updates_display():
    app = _app()
    async with app.run_test() as pilot:
        bar = app.query_one(StatusBar)
        bar.set_model("NewModel-X")
        await pilot.pause()
        assert "NewModel-X" in app.query_one("#status-left").content.plain


@pytest.mark.asyncio
async def test_permissions_switch_re_attaches_the_provider():
    from ai_coding.security.approval import CLIApprovalCallback

    app = _app(loop=_FakeLoop())
    async with app.run_test():
        fresh = CLIApprovalCallback()
        dispatcher = SimpleNamespace(approval=fresh)
        app._ensure_approval_provider(dispatcher)
        assert fresh._provider == app.approval_provider


@pytest.mark.asyncio
async def test_run_command_re_attaches_the_approval_provider(monkeypatch):
    from ai_coding.security.approval import CLIApprovalCallback
    from ai_coding.ui.repl import REPLAction

    loop = _FakeLoop()
    stale = CLIApprovalCallback()  # what /permissions confirm installs: no provider
    loop.dispatcher = SimpleNamespace(approval=stale)
    app = _app(loop=loop)

    async def fake_handle_input(loop_, text, session, sessions, console):
        return REPLAction.CONTINUE, session

    monkeypatch.setattr("ai_coding.ui.textual_app.handle_input", fake_handle_input)
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/status"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        assert stale._provider == app.approval_provider


@pytest.mark.asyncio
async def test_textual_console_delta_streams_into_transcript(monkeypatch):
    from ai_coding.ui.repl import REPLAction

    loop = _FakeLoop()
    app = _app(loop=loop)
    seen: list[str] = []

    async def fake_handle_input(loop_, text, session, sessions, console):
        seen.append(text)
        console.delta("## 计划\n\n")
        console.delta("第一步：写代码")
        console._end_stream()
        return REPLAction.CONTINUE, session

    monkeypatch.setattr("ai_coding.ui.textual_app.handle_input", fake_handle_input)
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/review"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        assert seen == ["/review"]
        text = _all_static_text(app)
        assert "计划" in text
        assert "第一步：写代码" in text


@pytest.mark.asyncio
async def test_plan_asks_before_running(monkeypatch):
    loop = _FakeLoop()
    app = _app(loop=loop)

    async def fake_wait(screen):
        return "add tests"

    monkeypatch.setattr(app, "push_screen_wait", fake_wait)
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/plan"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()
        assert len(loop.calls) == 1
        assert "add tests" in loop.calls[0]
        assert "implementation plan" in loop.calls[0]


@pytest.mark.asyncio
async def test_plan_escape_does_nothing(monkeypatch):
    loop = _FakeLoop()
    app = _app(loop=loop)

    async def fake_wait(screen):
        return ""

    monkeypatch.setattr(app, "push_screen_wait", fake_wait)
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/plan"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()
        assert loop.calls == []


@pytest.mark.asyncio
async def test_input_is_disabled_while_a_command_runs(monkeypatch):
    from ai_coding.ui.repl import REPLAction

    loop = _FakeLoop()
    app = _app(loop=loop)
    gate = asyncio.Event()

    async def fake_handle_input(loop_, text, session, sessions, console):
        await gate.wait()
        return REPLAction.CONTINUE, session

    monkeypatch.setattr("ai_coding.ui.textual_app.handle_input", fake_handle_input)
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/status"
        await pilot.press("enter")
        await pilot.pause()
        assert app.query_one("#prompt").disabled
        gate.set()
        await pilot.pause()
        await pilot.pause()
        assert not app.query_one("#prompt").disabled
