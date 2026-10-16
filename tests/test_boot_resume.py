"""Boot auto-resume, step one (11-spec §7.2): the scan.

A restart must find an armed task even when the last round closed cleanly
(openTurn is cleared then, but the task is still armed -- only the marker
scan would miss exactly the common case).
"""

from ai_coding.domain.task import TaskState
from ai_coding.server.world import find_armed_session
from ai_coding.service.session_service import SessionService


def _armed(service: SessionService, title: str, stamp: str):
    session = service.create(title=title)
    task = TaskState(goal=title)
    task.add("step")
    task.armed = True
    task.armed_profile = "plan"
    session.task = task
    session.last_access_time = stamp
    service.save(session)
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
