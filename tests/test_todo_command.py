"""TDD (hotel-fix P0-2): /todo emits TaskUpdated card channel, no envelope."""

import pytest
from rich.console import Console

from ai_coding.app.commands import REPLAction, handle_input
from ai_coding.app.console import DialogueConsole
from ai_coding.core.events import TaskUpdated
from ai_coding.domain.session import SessionData
from ai_coding.domain.task import TaskState

pytestmark = pytest.mark.asyncio


class _FakeLoop:
    async def process_input(self, *args, **kwargs):
        return "unused"


class _FakeSessions:
    pass


class _FakeObserver:
    def __init__(self):
        self.events = []

    def on_event(self, event):
        self.events.append(event)


def _session_with_task() -> SessionData:
    session = SessionData(
        session_id="s1", title="", created_time="", last_access_time=""
    )
    task = TaskState(goal="Build feature X")
    task.add("Write tests")
    task.add("Implement feature")
    session.task = task
    return session


def _console():
    c = Console(record=True, width=80)
    return DialogueConsole(c), c


async def test_todo_emits_task_updated_and_no_envelope():
    session = _session_with_task()
    dc, c = _console()
    obs = _FakeObserver()
    action, _ = await handle_input(
        _FakeLoop(), "/todo", session, _FakeSessions(), dc, observer=obs
    )
    assert action is REPLAction.CONTINUE
    assert any(isinstance(e, TaskUpdated) for e in obs.events)
    out = c.export_text()
    assert "<task-state>" not in out
    assert "Write tests" in out


async def test_todo_without_observer_still_prints():
    session = _session_with_task()
    dc, c = _console()
    action, _ = await handle_input(
        _FakeLoop(), "/todo", session, _FakeSessions(), dc, observer=None
    )
    assert action is REPLAction.CONTINUE
    out = c.export_text()
    assert "Write tests" in out


async def test_todo_no_task_list():
    session = SessionData(
        session_id="s1", title="", created_time="", last_access_time=""
    )
    session.task = None
    dc, c = _console()
    obs = _FakeObserver()
    action, _ = await handle_input(
        _FakeLoop(), "/todo", session, _FakeSessions(), dc, observer=obs
    )
    assert action is REPLAction.CONTINUE
    assert not any(isinstance(e, TaskUpdated) for e in obs.events)
    assert "no task" in c.export_text().lower()
