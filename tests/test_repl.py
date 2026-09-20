"""TDD (M5-2): REPL core — one-input handling, offline with injected fakes.

The REPL is a thin loop over :func:`handle_input`; here we verify the *decision*
logic (plain text → run the loop, slash → react) without touching the network.
Rich console output is captured via ``record=True``.
"""

import pytest
from rich.console import Console

from ai_coding.ui.console import DialogueConsole
from ai_coding.ui.repl import REPLAction, handle_input

pytestmark = pytest.mark.asyncio


def _sd(sid: str):
    from ai_coding.domain.session import SessionData

    return SessionData(
        session_id=sid, title="", created_time="", last_access_time=""
    )


class _FakeLoop:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.reply = "assistant answer"

    async def process_input(self, session, text, on_delta=None):
        self.calls.append((text, on_delta))
        return self.reply


class _FakeSessions:
    def __init__(self) -> None:
        self.items = [_sd("s1"), _sd("s2")]

    def create(self, title: str = ""):
        s = _sd(f"new{len(self.items)}")
        self.items.append(s)
        return s

    def load(self, session_id):
        return next((s for s in self.items if s.session_id == session_id), None)


def _console_capture() -> tuple[DialogueConsole, Console]:
    c = Console(record=True, width=80)
    return DialogueConsole(c), c


def _session_text(c: Console) -> str:
    return c.export_text()


async def test_handle_plain_text_runs_loop():
    loop = _FakeLoop()
    sessions = _FakeSessions()
    dc, _ = _console_capture()

    action, session = await handle_input(
        loop, "tell me", sessions.items[0], sessions, dc
    )

    assert action is REPLAction.CONTINUE
    assert len(loop.calls) == 1
    assert loop.calls[0][0] == "tell me"
    assert callable(loop.calls[0][1])  # the rich delta sink was wired


async def test_handle_quit_does_not_run_loop():
    loop = _FakeLoop()
    sessions = _FakeSessions()
    dc, _ = _console_capture()

    action, session = await handle_input(loop, "/quit", sessions.items[0], sessions, dc)

    assert action is REPLAction.QUIT
    assert loop.calls == []


async def test_handle_clear_starts_new_session():
    loop = _FakeLoop()
    sessions = _FakeSessions()
    dc, _ = _console_capture()
    old = sessions.items[0]

    action, new_session = await handle_input(loop, "/clear", old, sessions, dc)

    assert action is REPLAction.NEW_SESSION
    assert new_session is not old
    assert loop.calls == []


async def test_handle_help_prints_and_continues():
    loop = _FakeLoop()
    sessions = _FakeSessions()
    dc, c = _console_capture()

    action, _ = await handle_input(loop, "/help", sessions.items[0], sessions, dc)

    assert action is REPLAction.CONTINUE
    assert loop.calls == []
    assert "quit" in _session_text(c)


async def test_handle_status_prints_session_info():
    loop = _FakeLoop()
    sessions = _FakeSessions()
    dc, c = _console_capture()
    session = sessions.items[0]
    from ai_coding.domain.message import ChatMessage

    session.add_message(ChatMessage(role="user", content="hi", timestamp=""))

    action, _ = await handle_input(loop, "/status", session, sessions, dc)

    assert action is REPLAction.CONTINUE
    assert loop.calls == []
    out = _session_text(c)
    assert "1" in out  # one message reported by /status
