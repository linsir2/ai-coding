"""The Textual shell: status bar, transcript area, input bar."""

import asyncio
from types import SimpleNamespace

import pytest
from textual.command import CommandPalette
from textual.widgets import Input, Static

from ai_coding.ui.status_bar import StatusBar
from ai_coding.ui.textual_app import AICodingApp
from ai_coding.ui.transcript import Prose


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
        assert "好" in app.query_one(Prose).source


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
    # Prose subclasses Static but holds a Rich renderable whose str() is a
    # repr; it is covered by .source assertions, not by static-text scans.
    return "\n".join(
        w.content.plain if hasattr(w.content, "plain") else str(w.content)
        for w in app.query(Static)
        if not isinstance(w, Prose)
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
        # Console deltas ride Turn.stream like model deltas; they land in
        # Prose.source (previously MarkdownParagraph Static children, which
        # is why a Static scan used to see them).
        prose = app.query_one(Prose).source
        assert "计划" in prose
        assert "第一步：写代码" in prose


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


@pytest.mark.asyncio
async def test_escape_sets_the_cancel_event_the_dispatcher_waits_on():
    """Esc must reach the switch ``ToolDispatcher`` already watches.

    The whole chain was already there — ``dispatch.py:171`` holds the event,
    ``deadline.py:132`` abandons the tool when it fires, and
    ``dispatch.py:446-450`` kills the subprocess group on top of that. Nothing
    ever constructed the event or set it, so a long tool call could only be
    escaped by quitting the app.
    """
    app = _app(loop=_FakeLoop())
    async with app.run_test() as pilot:
        assert not app._cancel_event.is_set()
        await pilot.press("escape")
        assert app._cancel_event.is_set()


@pytest.mark.asyncio
async def test_escape_sets_the_event_the_dispatcher_was_given():
    """The app must set the loop's event, not a private replacement.

    ``run_app`` builds the dispatcher first and hands the same instance to the
    app, so Esc has to land on that object.  A fresh ``asyncio.Event()`` here
    would look identical to every other test while silently disabling the
    switch in the real app.
    """
    cancel = asyncio.Event()
    app = _app(loop=_FakeLoop(), cancel_event=cancel)
    assert app._cancel_event is cancel
    async with app.run_test() as pilot:
        await pilot.press("escape")
        assert cancel.is_set()


@pytest.mark.asyncio
async def test_ask_screen_receives_the_options_the_model_offered():
    """The modal's ``options`` parameter must stop being dead code.

    ``AskScreen`` has rendered ``#ask-options`` since it was written, but every
    caller left the tuple empty, so the line never appeared.
    """
    app = _app(loop=_FakeLoop())
    async with app.run_test() as pilot:
        worker = pilot.app.run_worker(
            app.ask_provider("改 A 还是 B？", ["A", "B"]), exclusive=False
        )
        for _ in range(10):
            await pilot.pause()
            if app.screen.__class__.__name__ == "AskScreen":
                break
        assert app.screen.__class__.__name__ == "AskScreen", "modal never opened"
        assert app.screen._options == ("A", "B")  # type: ignore[attr-defined]
        # The options line only renders when the tuple is non-empty.
        assert app.screen.query("#ask-options")  # type: ignore[attr-defined]
        app.screen.query_one("#ask-input", Input).value = "A"
        await pilot.press("enter")
        await worker.wait()

    assert worker.result == "A"


@pytest.mark.asyncio
async def test_ctrl_y_copies_last_tool_output_to_clipboard():
    """`y` (ctrl) copies the most recent finished tool call, params + output."""
    from types import SimpleNamespace

    from textual.widgets import Input

    from ai_coding.core.events import ToolFinished, ToolStarted
    from ai_coding.domain.message import ChatMessage, ToolCallRef, ToolOutput
    from ai_coding.domain.run import tool_message

    full_output = "commit f9fd276\n 3 files changed"
    session = SimpleNamespace(
        messages=[
            ChatMessage(
                role="assistant", content=None, timestamp="",
                tool_calls=[ToolCallRef(id="c1", name="bash",
                                        arguments='{"command": "git log -1 --stat"}')],
            ),
            tool_message(
                ToolOutput(call_id="c1", tool_name="bash", output=full_output),
                timestamp="",
            ),
        ]
    )

    class _Loop:
        async def process_input(self, session, text, *, on_delta=None, observer=None):
            assert observer is not None
            observer.on_event(ToolStarted(call_id="c1", tool_name="bash",
                                         params={"command": "git log -1 --stat"}))
            observer.on_event(ToolFinished(call_id="c1", tool_name="bash",
                                           success=True, output_preview="done"))
            if on_delta is not None:
                on_delta("answer")
            return "answer"

        async def aclose(self):
            return None

    app = _app(loop=_Loop(), session=session)
    async with app.run_test(size=(80, 24)) as pilot:
        app.query_one("#prompt", Input).value = "commit?"
        await pilot.press("enter")
        for _ in range(6):
            await pilot.pause()
        await pilot.press("ctrl+y")
        await pilot.pause()
        copied = app.clipboard
        assert "$ git log -1 --stat" in copied
        assert "3 files changed" in copied


@pytest.mark.asyncio
async def test_ctrl_y_with_no_tool_says_so():
    app = _app(loop=_FakeLoop())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("ctrl+y")
        await pilot.pause()
        assert "没有可复制的工具输出" in _all_static_text(app)
