"""M-C3: the boot-to-boot resume gate (TDD, red first).

The only breaker today is ``resume_count >= 5``; a resume that produced no
round, no real message and no settled step is indistinguishable from one that
did.  So the gate compares a fingerprint **boot to boot**: unchanged across two
consecutive boots means the resume did nothing, and the task is disarmed with a
``ResumeStalled`` report.  A synthetic resume note is not progress.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

from ai_coding.core.events import ResumeStalled
from ai_coding.domain.run import user_message
from ai_coding.domain.session import SessionData
from ai_coding.domain.task import TaskState
from ai_coding.server.handlers import make_handlers, resume_fingerprint
from ai_coding.service.session_service import SessionService


class _Observer:
    def __init__(self) -> None:
        self.events: list = []

    def on_event(self, event) -> None:
        self.events.append(event)


class _Loop:
    """The handler-facing slice of the loop: posture + the event sink."""

    def __init__(self) -> None:
        self.observer = _Observer()
        self.bound: SessionData | None = None

    def bind_session(self, session) -> None:
        self.bound = session

    def commit_posture(
        self, session, *, agent=None, readonly=None, reason="", observer=None
    ) -> None:
        session.agent = agent if agent is not None else session.agent
        session.readonly = bool(readonly) if readonly is not None else session.readonly
        session.pending_posture = None


class _Future:
    def add_done_callback(self, _cb) -> None:
        return None


class _Service:
    def __init__(self) -> None:
        self.events: list = []

    def notify(self, method, params) -> None:
        self.events.append((method, params))

    def spawn(self, coro) -> _Future:
        coro.close()  # the run itself is not what these tests observe
        return _Future()


def _world(tmp_path):
    loop = _Loop()
    world = SimpleNamespace(
        sessions=SessionService(tmp_path / "sessions"),
        engine=SimpleNamespace(
            model_config=SimpleNamespace(name="fake", base_url="https://x/v1")
        ),
        notices=None,
        mcp=None,
        config=SimpleNamespace(
            models={"fake": SimpleNamespace(name="fake")},
            ai=SimpleNamespace(
                max_context_tokens=48000,
                max_tool_iterations=10,
                stop_policy="todo_aware",
            ),
            default_model="fake",
        ),
        loop=loop,
        workspace=str(tmp_path),
        cancel=threading.Event(),
        approval=None,
        user_prompt=None,
    )
    service = _Service()
    handlers = make_handlers(world, service)
    return world, handlers


def _armed(world):
    session = world.sessions.create(title="armed")
    task = TaskState(goal="g")
    task.add("step")
    task.armed = True
    task.armed_profile = "plan"
    session.task = task
    world.sessions.save(session)
    return session


def _boot(world, handlers, session_id):
    world.pending_resume = world.sessions.load(session_id)
    return handlers["initialize"]({})


def _set_fingerprint(world, session_id, fingerprint, *, no_progress=0):
    session = world.sessions.load(session_id)
    session.task.resume_fingerprint = fingerprint
    session.task.no_progress_resumes = no_progress
    world.sessions.save(session)


def _current_fingerprint(world, session_id):
    session = world.sessions.load(session_id)
    return resume_fingerprint(session.task, session)


# ---- the fingerprint itself ------------------------------------------------


def test_a_synthetic_resume_message_is_not_progress():
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    task = TaskState(goal="g")
    session.task = task
    before = resume_fingerprint(task, session)

    session.add_message(user_message("[resume] 上一步在等审批", "t", synthetic=True))
    assert resume_fingerprint(task, session) == before

    session.add_message(user_message("real words", "t"))
    assert resume_fingerprint(task, session) != before


def test_the_fingerprint_tracks_rounds_and_steps():
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    task = TaskState(goal="g")
    session.task = task
    before = resume_fingerprint(task, session)

    task.rounds_run = 3
    task.tokens_used = 120
    assert resume_fingerprint(task, session) != before


# ---- the gate --------------------------------------------------------------


def test_one_stalled_resume_does_not_stop_the_task(tmp_path):
    world, handlers = _world(tmp_path)
    session = _armed(world)
    _set_fingerprint(world, session.session_id, _current_fingerprint(world, session.session_id))

    hello = _boot(world, handlers, session.session_id)

    assert hello["resuming"] is True
    reloaded = world.sessions.load(session.session_id)
    assert reloaded.task.armed is True
    assert reloaded.task.no_progress_resumes == 1
    assert world.loop.observer.events == []


def test_two_stalled_resumes_stop_the_task(tmp_path):
    world, handlers = _world(tmp_path)
    session = _armed(world)
    _set_fingerprint(
        world, session.session_id, _current_fingerprint(world, session.session_id),
        no_progress=1,
    )

    hello = _boot(world, handlers, session.session_id)

    assert hello["resuming"] is False
    reloaded = world.sessions.load(session.session_id)
    assert reloaded.task.armed is False
    assert "无进展" in str(hello.get("resumeNote") or "")
    events = [e for e in world.loop.observer.events if isinstance(e, ResumeStalled)]
    assert len(events) == 1
    assert events[0].no_progress == 2
    # the trace is bound to the session before the event is emitted
    assert world.loop.bound is not None
    assert world.loop.bound.session_id == session.session_id


def test_progress_resets_the_counter(tmp_path):
    world, handlers = _world(tmp_path)
    session = _armed(world)
    _set_fingerprint(
        world, session.session_id, _current_fingerprint(world, session.session_id),
        no_progress=1,
    )
    # a round completed since: the fingerprint moved
    moved = world.sessions.load(session.session_id)
    moved.task.rounds_run = 4
    world.sessions.save(moved)

    hello = _boot(world, handlers, session.session_id)

    assert hello["resuming"] is True
    reloaded = world.sessions.load(session.session_id)
    assert reloaded.task.no_progress_resumes == 0
    assert reloaded.task.armed is True
    assert world.loop.observer.events == []


def test_a_stalled_resume_still_counts_against_the_boot_breaker(tmp_path):
    """The fingerprint gate is a second breaker, not a replacement: the
    ``resume_count`` breaker keeps its own meaning."""
    world, handlers = _world(tmp_path)
    session = _armed(world)
    moved = world.sessions.load(session.session_id)
    moved.task.resume_count = 5
    world.sessions.save(moved)

    hello = _boot(world, handlers, session.session_id)

    assert hello["resuming"] is False
    assert "上限" in str(hello.get("resumeNote") or "")
    assert world.sessions.load(session.session_id).task.armed is False
