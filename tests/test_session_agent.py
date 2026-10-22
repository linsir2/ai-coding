"""TDD (M1b T1): the session's agent is the authority; ``mode`` is derived.

Two rules shape this:

* **one truth**.  The file carries ``agent`` and nothing else about the
  posture — no mirrored ``mode`` key, so there is no second place for the
  two to disagree about.  An old file that says ``"mode": "plan"`` still
  reads back as the plan agent, so nothing is lost on the way in.
* **the mode cannot be written**.  ``mode`` is derived from the agent, so
  assigning to it would be a second way to say the same thing.  It raises.
"""

from __future__ import annotations

import pytest

from ai_coding.domain.session import SessionData
from ai_coding.service.session_service import SessionService


def _session() -> SessionData:
    return SessionData(session_id="s1", title="", created_time="t", last_access_time="t")


def test_a_fresh_session_speaks_through_build():
    assert _session().agent == "build"
    assert _session().mode == "normal"


def test_the_mode_cannot_be_written():
    """It is derived; writing it would be a second way to say the same thing."""
    session = _session()
    with pytest.raises(AttributeError):
        session.mode = "plan"  # type: ignore[misc]


def test_the_plan_agent_is_a_plan_session():
    session = _session()
    session.agent = "plan"
    assert session.mode == "plan"


def test_the_file_carries_the_agent_and_not_a_mirrored_mode(tmp_path):
    session = _session()
    assert "agent" not in session.to_dict()
    assert "mode" not in session.to_dict()
    session.agent = "plan"
    assert session.to_dict()["agent"] == "plan"
    assert "mode" not in session.to_dict()


def test_an_old_file_that_said_mode_still_reads_as_the_plan_agent():
    data = _session().to_dict()
    data["mode"] = "plan"
    loaded = SessionData.from_dict(data)
    assert loaded.agent == "plan"
    assert loaded.mode == "plan"


def test_an_old_file_with_an_ordinary_mode_reads_as_build():
    data = _session().to_dict()
    data["mode"] = "normal"
    assert SessionData.from_dict(data).agent == "build"


def test_an_unknown_agent_is_kept_as_written():
    """The vocabulary is open — a custom ``.md`` may name anything."""
    session = _session()
    session.agent = "typo"
    assert SessionData.from_dict(session.to_dict()).agent == "typo"
    # ... and it is not a plan session, because plan-ness is one agent's id.
    assert session.mode == "normal"


def test_the_agent_history_round_trips_and_stays_out_of_the_way_when_empty():
    session = _session()
    assert "agentHistory" not in session.to_dict()
    session.agent_history = [
        {"from": "build", "to": "plan", "ts": "2026-10-22T10:00:00Z", "reason": "/plan"}
    ]
    loaded = SessionData.from_dict(session.to_dict())
    assert loaded.agent_history == session.agent_history


def test_a_broken_history_row_is_dropped_rather_than_kept():
    session = _session()
    session.agent_history = [
        {"from": "build", "to": "plan", "ts": "t", "reason": "/plan"},
        {"from": "build"},
        "not a row",
    ]
    loaded = SessionData.from_dict(session.to_dict())
    assert loaded.agent_history == [
        {"from": "build", "to": "plan", "ts": "t", "reason": "/plan"}
    ]


def test_a_new_session_takes_the_default_agent(tmp_path):
    sessions = SessionService(
        tmp_path / "sessions", default_agent="plan", default_readonly=True
    )
    session = sessions.create()
    assert session.agent == "plan"
    assert session.readonly is True
    assert session.mode == "plan"


def test_a_session_keeps_its_agent_across_a_save(tmp_path):
    sessions = SessionService(tmp_path / "sessions")
    session = sessions.create()
    session.agent = "reviewer"
    sessions.save(session)
    assert sessions.load(session.session_id).agent == "reviewer"
