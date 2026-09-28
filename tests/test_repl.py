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

    def list_sessions(self):
        return list(self.items)


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


async def test_handle_model_lists_models():
    loop = _FakeLoop()
    loop.settings = type("S", (), {"models": {"gpt-4": "GPT-4", "claude": "Claude"}, "default_model": "gpt-4"})()
    sessions = _FakeSessions()
    dc, c = _console_capture()
    action, _ = await handle_input(loop, "/model", sessions.items[0], sessions, dc)
    assert action is REPLAction.CONTINUE
    out = _session_text(c)
    assert "gpt-4" in out and "claude" in out


async def test_handle_model_switches():
    loop = _FakeLoop()
    loop.settings = type("S", (), {"models": {"gpt-4": "GPT-4", "claude": "Claude"}, "default_model": "gpt-4"})()
    loop.set_model = lambda name: None
    sessions = _FakeSessions()
    dc, c = _console_capture()
    action, _ = await handle_input(loop, "/model claude", sessions.items[0], sessions, dc)
    assert action is REPLAction.CONTINUE
    out = _session_text(c)
    assert "claude" in out


async def test_handle_resume_lists_sessions():
    loop = _FakeLoop()
    sessions = _FakeSessions()
    dc, c = _console_capture()
    action, _ = await handle_input(loop, "/resume", sessions.items[0], sessions, dc)
    assert action is REPLAction.CONTINUE
    out = _session_text(c)
    assert "s1" in out or "s2" in out


async def test_handle_resume_loads_session():
    loop = _FakeLoop()
    sessions = _FakeSessions()
    target = sessions.items[0]
    dc, c = _console_capture()
    action, session = await handle_input(loop, "/resume s1", sessions.items[1], sessions, dc)
    assert action is REPLAction.NEW_SESSION
    assert session is target


async def test_handle_permissions_shows_current():
    loop = _FakeLoop()
    loop.dispatcher = type("D", (), {"approval": type("A", (), {"__repr__": lambda s: "CLIApprovalCallback"})()})()
    sessions = _FakeSessions()
    dc, c = _console_capture()
    action, _ = await handle_input(loop, "/permissions", sessions.items[0], sessions, dc)
    assert action is REPLAction.CONTINUE
    out = _session_text(c)
    assert "CLIApprovalCallback" in out or "approval" in out.lower()


async def test_handle_todo_renders_task_state():
    from ai_coding.domain.task import TaskState, TodoItem
    loop = _FakeLoop()
    sessions = _FakeSessions()
    session = sessions.items[0]
    task = TaskState(goal="Build feature X")
    task.add(TodoItem(id="1", content="Write tests", status="pending"))
    task.add(TodoItem(id="2", content="Implement feature", status="in_progress"))
    session.task = task
    dc, c = _console_capture()
    action, _ = await handle_input(loop, "/todo", session, sessions, dc)
    assert action is REPLAction.CONTINUE
    out = _session_text(c)
    assert "Build feature X" in out
    assert "Write tests" in out


async def test_handle_todo_no_task():
    loop = _FakeLoop()
    sessions = _FakeSessions()
    dc, c = _console_capture()
    action, _ = await handle_input(loop, "/todo", sessions.items[0], sessions, dc)
    assert action is REPLAction.CONTINUE
    out = _session_text(c)
    assert "no task" in out.lower() or "empty" in out.lower() or "0" in out


async def test_handle_skills_lists_catalog():
    loop = _FakeLoop()
    loop.skills = type("SR", (), {"catalog_text": lambda self: "- test-skill: A test skill"})()
    sessions = _FakeSessions()
    dc, c = _console_capture()
    action, _ = await handle_input(loop, "/skills", sessions.items[0], sessions, dc)
    assert action is REPLAction.CONTINUE
    out = _session_text(c)
    assert "test-skill" in out


async def test_handle_plan_sends_prompt():
    loop = _FakeLoop()
    sessions = _FakeSessions()
    dc, _ = _console_capture()
    action, _ = await handle_input(loop, "/plan", sessions.items[0], sessions, dc)
    assert action is REPLAction.CONTINUE
    assert len(loop.calls) == 1
    assert "plan" in loop.calls[0][0].lower() or "implementation" in loop.calls[0][0].lower()


async def test_handle_review_sends_prompt():
    loop = _FakeLoop()
    sessions = _FakeSessions()
    dc, _ = _console_capture()
    action, _ = await handle_input(loop, "/review", sessions.items[0], sessions, dc)
    assert action is REPLAction.CONTINUE
    assert len(loop.calls) == 1
    assert "review" in loop.calls[0][0].lower() or "审查" in loop.calls[0][0].lower()


async def test_handle_config_shows_settings():
    loop = _FakeLoop()
    loop.settings = type("S", (), {
        "default_model": "gpt-4",
        "max_context_tokens": 48000,
        "max_tool_iterations": 10,
        "stop_policy": "todo_aware",
    })()
    sessions = _FakeSessions()
    dc, c = _console_capture()
    action, _ = await handle_input(loop, "/config", sessions.items[0], sessions, dc)
    assert action is REPLAction.CONTINUE
    out = _session_text(c)
    assert "gpt-4" in out
    assert "48000" in out


async def test_help_includes_new_commands():
    loop = _FakeLoop()
    sessions = _FakeSessions()
    dc, c = _console_capture()
    action, _ = await handle_input(loop, "/help", sessions.items[0], sessions, dc)
    assert action is REPLAction.CONTINUE
    out = _session_text(c)
    for cmd in ("/model", "/resume", "/permissions", "/todo", "/skills", "/plan", "/review", "/config"):
        assert cmd in out
