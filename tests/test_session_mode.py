"""TDD (17 §4-P0): the session mode is a persisted, fail-safe posture.

The rule mirrors ``task``/``interventionState``: written only when not the
default, read back with a fail-safe vocabulary -- an unknown persisted value
must never re-enter the process as a posture the gates do not know.
"""

from __future__ import annotations

from pathlib import Path

from ai_coding.domain.session import KNOWN_MODES, SessionData
from ai_coding.service.session_service import SessionService


def _session() -> SessionData:
    return SessionData(
        session_id="s1", title="", created_time="t", last_access_time="t"
    )


def test_normal_mode_writes_no_key():
    assert _session().mode == "normal"
    assert "mode" not in _session().to_dict()


def test_plan_mode_round_trips_through_the_file(tmp_path: Path):
    sessions = SessionService(tmp_path / "sessions")
    session = sessions.create()
    session.mode = "plan"
    sessions.save(session)

    loaded = sessions.load(session.session_id)
    assert loaded.mode == "plan"
    assert loaded.to_dict()["mode"] == "plan"


def test_unknown_persisted_mode_falls_back_to_normal():
    data = _session().to_dict()
    data["mode"] = "yolo-mode"
    assert SessionData.from_dict(data).mode == "normal"


def test_missing_mode_reads_as_normal():
    assert SessionData.from_dict(_session().to_dict()).mode == "normal"


def test_known_modes_is_the_fail_safe_vocabulary():
    assert KNOWN_MODES == ("normal", "plan")


def test_readonly_defaults_off_and_writes_no_key():
    session = _session()
    assert session.readonly is False
    assert "readonly" not in session.to_dict()


def test_readonly_round_trips_through_the_file(tmp_path: Path):
    sessions = SessionService(tmp_path / "sessions")
    session = sessions.create()
    session.readonly = True
    sessions.save(session)

    loaded = sessions.load(session.session_id)
    assert loaded.readonly is True
    assert loaded.to_dict()["readonly"] is True


def test_missing_readonly_reads_as_false():
    assert SessionData.from_dict(_session().to_dict()).readonly is False


def test_a_fresh_session_is_normal_and_writable():
    """18 §10.4 (R8): step sessions and sub-agent sessions are fresh
    ``SessionData`` -- they must default to normal/writable so execution is
    never born read-only."""
    fresh = _session()
    assert (fresh.mode, fresh.readonly) == ("normal", False)
