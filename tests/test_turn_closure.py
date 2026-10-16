"""Turn closure: length-cutoff visibility (W1) and usage persistence (W3).

W1: a step cut by ``max_tokens`` returns ``finish_reason="length"`` with a
half sentence; the loop used to file that as an ordinary ``COMPLETED`` and the
transcript showed a mysteriously cut answer.  The loop must flag the outcome
so the UI can say so (a bare ``stop`` is indistinguishable from done, so only
``length`` is detectable — anything else stays silent).

W3: ``TurnOutcome.usage`` only lived in memory (filled by the reporter after
the session save), so a past cut could never be forensically confirmed.  Each
turn now appends one row to the session file.
"""

import json

import pytest

from ai_coding.domain.loop import StepResult, TurnOutcome, TurnStatus
from ai_coding.service.session_service import SessionService
from tests.test_agent_loop_react import ScriptedEngine, _loop


def _session(sessions) -> object:
    session = sessions.create()
    return session


@pytest.mark.asyncio
async def test_outcome_defaults_to_not_truncated():
    outcome = TurnOutcome(status=TurnStatus.COMPLETED, text="done")
    assert outcome.truncated_by_length is False


@pytest.mark.asyncio
async def test_length_cut_step_marks_outcome(tmp_path):
    engine = ScriptedEngine([StepResult(text="half a sentence", finish_reason="length")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    await loop.process_input(_session(sessions), "hi")
    outcome = loop.closer.last_outcome
    assert outcome is not None
    assert outcome.status is TurnStatus.COMPLETED  # only the flag is new
    assert outcome.truncated_by_length is True


@pytest.mark.asyncio
async def test_natural_stop_leaves_flag_clear(tmp_path):
    engine = ScriptedEngine([StepResult(text="done", finish_reason="stop")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    await loop.process_input(_session(sessions), "hi")
    assert loop.closer.last_outcome is not None
    assert loop.closer.last_outcome.truncated_by_length is False


@pytest.mark.asyncio
async def test_turn_usage_persisted_to_session_file(tmp_path):
    engine = ScriptedEngine(
        [
            StepResult(
                text="done",
                finish_reason="stop",
                usage={"prompt_tokens": 10, "completion_tokens": 5,
                       "total_tokens": 15},
            )
        ]
    )
    root = tmp_path / "sessions"
    sessions = SessionService(root)
    loop = _loop(engine, sessions)
    session = _session(sessions)
    await loop.process_input(session, "hi")
    data = json.load(open(root / f"{session.session_id}.json"))
    assert data["turnUsages"] == [
        # 19 §3.5: the row carries the window reading (last step's prompt).
        {"turn": 1, "promptTokens": 10, "completionTokens": 5,
         "totalTokens": 15, "contextTokens": 10}
    ]


@pytest.mark.asyncio
async def test_turn_without_usage_writes_no_row(tmp_path):
    engine = ScriptedEngine([StepResult(text="done", finish_reason="stop")])
    root = tmp_path / "sessions"
    sessions = SessionService(root)
    loop = _loop(engine, sessions)
    session = _session(sessions)
    await loop.process_input(session, "hi")
    data = json.load(open(root / f"{session.session_id}.json"))
    assert "turnUsages" not in data
