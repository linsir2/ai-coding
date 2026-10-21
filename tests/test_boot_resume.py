"""Boot auto-resume, step one (11-spec §7.2): the scan.

A restart must find an armed task even when the last round closed cleanly
(openTurn is cleared then, but the task is still armed -- only the marker
scan would miss exactly the common case).
"""

import json

import pytest
from rich.console import Console

from ai_coding.domain.task import TaskState
from ai_coding.server.world import find_armed_session
from ai_coding.service.session_service import SessionService


def _patch(service: SessionService, session, mutate):
    """Edit the persisted JSON in place -- R07's save stamps last_access_time
    with "now", so a test that needs a controlled stamp writes it directly."""
    path = service.root / f"{session.session_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _armed(service: SessionService, title: str, stamp: str):
    session = service.create(title=title)
    task = TaskState(goal=title)
    task.add("step")
    task.armed = True
    task.armed_profile = "plan"
    session.task = task
    service.save(session)
    _patch(service, session, lambda data: data.update({"lastAccessTime": stamp}))
    return session


def test_the_newest_armed_session_wins(tmp_path):
    service = SessionService(root=tmp_path / "sessions")
    older = _armed(service, "older", "2026-01-01T00:00:00Z")
    newer = _armed(service, "newer", "2026-01-02T00:00:00Z")

    found = find_armed_session(service)

    assert found is not None and found.session_id == newer.session_id
    assert found.session_id != older.session_id


def test_unarmed_sessions_do_not_qualify(tmp_path):
    service = SessionService(root=tmp_path / "sessions")
    service.save(service.create(title="plain"))

    assert find_armed_session(service) is None


def test_an_armed_session_with_an_unknown_profile_does_not_win(tmp_path):
    """R07: a stale armed task whose profile no runner can drive used to win
    the scan (it was the newest) and then be dropped in initialize -- which
    swallowed the one recovery chance a newer *valid* session still had."""
    service = SessionService(root=tmp_path / "sessions")
    real = _armed(service, "real", "2026-01-01T00:00:00Z")
    weird = _armed(service, "weird", "2026-01-02T00:00:00Z")
    _patch(service, weird, lambda data: data["task"].update({"armedProfile": "banana"}))

    found = find_armed_session(service)

    assert found is not None and found.session_id == real.session_id


# ---- D1 (20 §批次 4a): a plan the user gave up must stay given up ------------


@pytest.mark.asyncio
async def test_a_plan_given_up_is_not_picked_up_by_the_boot_resume(tmp_path):
    """Giving up has to survive the restart.

    The scan reads the pair (armed + a profile a runner can drive), so a
    withdrawn approval -- the flag gone and the profile gone with it --
    leaves the next boot nothing to pick up.  Half a withdrawal (the flag
    alone) would leave a profile that owns nothing.
    """
    from ai_coding.app.commands import handle_input
    from ai_coding.app.console import DialogueConsole

    class _Loop:
        """The posture half of the command layer's contract."""

        def request_posture(self, session, *, mode=None, readonly=None):
            session.mode = mode or session.mode
            session.readonly = bool(readonly)
            return True

    service = SessionService(root=tmp_path / "sessions")
    session = _armed(service, "plan", "2026-01-01T00:00:00Z")
    console = DialogueConsole(Console(record=True, width=80))

    assert find_armed_session(service) is not None, "the armed plan is the resume's"

    await handle_input(_Loop(), "/plan off", session, service, console)

    assert find_armed_session(service) is None, (
        "a plan the user gave up must not come back on the next boot"
    )
