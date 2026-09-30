"""TDD (M5-2): REPL core — one-input handling, offline with injected fakes.

The REPL is a thin loop over :func:`handle_input`; here we verify the *decision*
logic (plain text → run the loop, slash → react) without touching the network.
Rich console output is captured via ``record=True``.
"""

from typing import Any

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

    def resolve(self, prefix):
        return [s for s in self.items if s.session_id.startswith(prefix)]


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


def _model_settings() -> Any:
    return type(
        "S",
        (),
        {
            "models": {"gpt-4": "GPT-4", "claude": "Claude"},
            "default_model": "gpt-4",
        },
    )()


async def test_handle_model_lists_models():
    loop = _FakeLoop()
    loop.settings = _model_settings()
    sessions = _FakeSessions()
    dc, c = _console_capture()
    action, _ = await handle_input(loop, "/model", sessions.items[0], sessions, dc)
    assert action is REPLAction.CONTINUE
    out = _session_text(c)
    assert "gpt-4" in out and "claude" in out


async def test_handle_model_switches():
    loop = _FakeLoop()
    loop.settings = _model_settings()
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


async def test_handle_resume_shows_real_message_counts():
    """The list used to say msgs=0 for everything, because it counted the
    bodies it had just stripped."""
    loop = _FakeLoop()
    sessions = _FakeSessions()
    target = sessions.items[0]
    from ai_coding.domain.message import ChatMessage

    for i in range(3):
        target.add_message(ChatMessage(role="user", content=f"m{i}", timestamp=""))
    target.message_count = 3
    sessions.items[1].message_count = 0
    dc, c = _console_capture()

    await handle_input(loop, "/resume", sessions.items[1], sessions, dc)

    out = _session_text(c)
    assert "msgs=3" in out
    assert "msgs=0" in out


async def test_handle_resume_accepts_the_prefix_it_printed():
    """12 hex chars is what the list shows; that is what a user can type."""
    loop = _FakeLoop()
    sessions = _FakeSessions()
    target = _sd("abcdef0123456789fedcba9876543210")
    sessions.items.append(target)
    dc, c = _console_capture()

    action, session = await handle_input(
        loop, "/resume abcdef012345", sessions.items[1], sessions, dc
    )

    assert action is REPLAction.NEW_SESSION
    assert session is target
    assert loop.calls == []  # a resume must not also become a model turn


async def test_handle_resume_refuses_an_ambiguous_prefix():
    """Two matches must not resolve to whichever came first."""
    loop = _FakeLoop()
    sessions = _FakeSessions()
    sessions.items.append(_sd("aaaaaaaa1111"))
    sessions.items.append(_sd("aaaaaaaa2222"))
    dc, c = _console_capture()

    action, session = await handle_input(
        loop, "/resume aaaaaaaa", sessions.items[0], sessions, dc
    )

    assert action is REPLAction.CONTINUE
    assert session is sessions.items[0]
    assert loop.calls == []
    out = _session_text(c)
    assert "aaaaaaaa1111" in out and "aaaaaaaa2222" in out


async def test_handle_permissions_shows_current():
    approval = type("A", (), {"__repr__": lambda s: "CLIApprovalCallback"})()
    loop = _FakeLoop()
    loop.dispatcher = type("D", (), {"approval": approval})()
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
    expected = (
        "/model", "/resume", "/permissions", "/todo",
        "/skills", "/plan", "/review", "/config",
    )
    for cmd in expected:
        assert cmd in out


class _FakeTrace:
    def __init__(self, store):
        self.store = store
        self.current_path = None


class _BudgetLoop:
    def __init__(self, store):
        self.trace = _FakeTrace(store)


class _NoTraceLoop:
    pass


def _session(sid):
    return type("S", (), {"session_id": sid})()


def test_budget_says_when_no_turn_has_run(tmp_path):
    from ai_coding.core.trace import TraceStore
    from ai_coding.ui.repl import _budget_report

    loop = _BudgetLoop(TraceStore(tmp_path))
    out = _budget_report(loop, _session("never-ran"))
    assert "还没跑过回合" in out


def test_budget_says_when_zero_tokens(tmp_path):
    from ai_coding.core.trace import TraceStore
    from ai_coding.ui.repl import _budget_report

    store = TraceStore(tmp_path)
    store.update_summary(
        "zero",
        {"turns": 2, "steps": 5, "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0},
    )
    out = _budget_report(_BudgetLoop(store), _session("zero"))
    assert "尚无 token 用量" in out


def test_budget_renders_real_usage(tmp_path):
    from ai_coding.core.trace import TraceStore
    from ai_coding.ui.repl import _budget_report

    store = TraceStore(tmp_path)
    store.update_summary(
        "good",
        {
            "turns": 3,
            "steps": 9,
            "prompt_tokens": 30585,
            "completion_tokens": 798,
            "cached_tokens": 12288,
        },
    )
    out = _budget_report(_BudgetLoop(store), _session("good"))
    assert "3 turns" in out and "31,383 tok" in out


def test_budget_says_when_tracing_is_off(tmp_path):
    from ai_coding.ui.repl import _budget_report

    out = _budget_report(_NoTraceLoop(), _session("x"))
    assert "tracing is off" in out


async def test_unknown_close_command_gets_a_suggestion():
    """A typo must not become a model turn."""
    loop = _FakeLoop()
    sessions = _FakeSessions()
    dc, c = _console_capture()

    action, _ = await handle_input(loop, "/resum", sessions.items[0], sessions, dc)

    assert action is REPLAction.CONTINUE
    assert loop.calls == []  # not sent to the model
    out = _session_text(c)
    assert "did you mean" in out and "/resume" in out


async def test_a_path_is_still_sent_to_the_model():
    """/path/to/file is not a command; it is text the model may need."""
    loop = _FakeLoop()
    sessions = _FakeSessions()
    dc, _ = _console_capture()

    action, _ = await handle_input(loop, "/path/to/file", sessions.items[0], sessions, dc)

    assert action is REPLAction.CONTINUE
    assert loop.calls[0][0] == "/path/to/file"


async def test_a_distant_unknown_command_is_still_sent_to_the_model():
    """/zzz is not close to anything, so it stays text."""
    loop = _FakeLoop()
    sessions = _FakeSessions()
    dc, _ = _console_capture()

    action, _ = await handle_input(loop, "/zzz", sessions.items[0], sessions, dc)

    assert action is REPLAction.CONTINUE
    assert loop.calls[0][0] == "/zzz"


async def test_unknown_command_at_distance_two_gets_a_suggestion():
    """``/stat`` is two edits from ``/status`` — still a typo, not a path."""
    loop = _FakeLoop()
    sessions = _FakeSessions()
    dc, c = _console_capture()

    action, _ = await handle_input(loop, "/stat", sessions.items[0], sessions, dc)

    assert action is REPLAction.CONTINUE
    assert loop.calls == []
    out = _session_text(c)
    assert "did you mean" in out and "/status" in out
