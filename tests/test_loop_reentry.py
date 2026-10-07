"""One turn at a time, held by the loop rather than by the UI (G1-1).

"Never two turns on one session" used to be a Textual property: an exclusive
worker plus a submit queue. That holds inside one app instance and nowhere
else, so the REPL, a plan step, or any future front end had only the convention
that callers happen to ``await`` in order. Two turns that interleave append their
messages in whatever order they finish, and a tool_call id from one can be
answered after the other has already moved the conversation on.

The tests drive a real ``AgentLoop`` and a real session.  Only the model is
scripted, because what is being pinned is the loop's own serialisation, not the
engine's behaviour.
"""

from __future__ import annotations

import asyncio

import pytest

from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.domain.loop import StepResult
from ai_coding.service.session_service import SessionService
from ai_coding.tools.registry import ToolRegistry


class _ScriptedEngine:
    """Answers after a caller-controlled pause, so overlap is possible at all.

    ``asyncio.gather`` on two immediate returns would serialise itself: the first
    call would run to completion before the second ever started, and the test
    would pass against a loop that had no lock at all.
    """

    def __init__(self, steps: list[StepResult]) -> None:
        self._steps = list(steps)
        self.model_config = type("MC", (), {"name": "m"})()
        self.on_retry = None
        self.on_fallback = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        index = self.calls
        self.calls += 1
        self.entered.set()
        # The first caller waits here; a loop with no lock lets the second in.
        if index == 0:
            await self.release.wait()
        return self._steps[index] if index < len(self._steps) else StepResult(
            text="done", finish_reason="stop"
        )

    async def aclose(self) -> None:
        return None


class _RaisingEngine(_ScriptedEngine):
    """Fails while the gate is held, then answers immediately once told to.

    It does not wait on ``release``: the point is to prove the gate opened again,
    and a wait the test has to satisfy would pass whether or not the lock was
    ever released -- the call would simply block until the timeout.
    """

    def __init__(self) -> None:
        super().__init__([_answer("A")])
        self.fail = True

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        if self.fail:
            self.entered.set()
            self.calls += 1
            raise RuntimeError("model call blew up")
        self.calls += 1
        return _answer("A")


#: Every wait is bounded.  A gate that never opens must fail the test, not wedge
#: the whole suite.
_TIMEOUT = 5.0


def _loop(engine, sessions) -> AgentLoop:
    return AgentLoop(
        engine,
        sessions,
        ToolRegistry(),
        settings=AIConfig(max_tool_iterations=2),
    )


def _answer(text: str) -> StepResult:
    return StepResult(text=text, finish_reason="stop")


@pytest.mark.asyncio
async def test_two_turns_run_one_after_the_other(tmp_path):
    engine = _ScriptedEngine([_answer("A"), _answer("B")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    session = sessions.create()

    first = asyncio.create_task(loop.process_input(session, "one"))
    await asyncio.wait_for(engine.entered.wait(), _TIMEOUT)  # first is in the engine
    second = asyncio.create_task(loop.process_input(session, "two"))

    await asyncio.sleep(0)  # let the second task reach the loop's gate
    assert engine.calls == 1, "the second turn entered the engine while the first was running"

    engine.release.set()
    assert await asyncio.wait_for(first, _TIMEOUT) == "A"
    assert await asyncio.wait_for(second, _TIMEOUT) == "B"


@pytest.mark.asyncio
async def test_the_second_turn_sees_what_the_first_wrote(tmp_path):
    """Queuing is only correct if the second turn reads the first one's result.

    A gate that merely rejected the overlap would pass the ordering test above
    and still leave the model answering without the previous turn's answer.
    """
    engine = _ScriptedEngine([_answer("A"), _answer("B")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    session = sessions.create()

    first = asyncio.create_task(loop.process_input(session, "one"))
    await asyncio.wait_for(engine.entered.wait(), _TIMEOUT)
    second = asyncio.create_task(loop.process_input(session, "two"))
    await asyncio.sleep(0)
    engine.release.set()
    await asyncio.wait_for(asyncio.gather(first, second), _TIMEOUT)

    roles = [m.role for m in session.messages]
    assert roles == ["user", "assistant", "user", "assistant"], roles
    assert session.messages[1].content == "A"
    assert session.messages[3].content == "B"


@pytest.mark.asyncio
async def test_a_single_turn_is_unaffected(tmp_path):
    engine = _ScriptedEngine([_answer("A")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    engine.release.set()

    assert await asyncio.wait_for(
        loop.process_input(sessions.create(), "one"), _TIMEOUT
    ) == "A"
    assert engine.calls == 1


@pytest.mark.asyncio
async def test_the_gate_is_released_when_a_turn_raises(tmp_path):
    """A hand-written acquire/release without ``finally`` would wedge the loop
    for good, and nothing else in the suite would notice.

    The engine raises on its first call, so the failure happens while the gate
    is held -- that is the only way the test can tell a released lock from one
    that leaked.
    """
    engine = _RaisingEngine()
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    session = sessions.create()

    with pytest.raises(RuntimeError):
        await asyncio.wait_for(loop.process_input(session, "boom"), _TIMEOUT)

    engine.fail = False
    engine.release.set()  # the second call goes through _ScriptedEngine's wait
    assert await asyncio.wait_for(loop.process_input(session, "after"), _TIMEOUT) == "A"


@pytest.mark.asyncio
async def test_different_sessions_still_serialise(tmp_path):
    """The gate belongs to the loop, not to a session.

    The loop owns state that two turns share regardless of which session they
    touch -- the turn policy, the dispatcher, the trace recorder and the cancel
    event -- so a session-level gate would let exactly the interleaving this is
    meant to prevent.  Pinned so nobody later "fixes" it into a per-session lock
    without finding out why it is wrong.
    """
    engine = _ScriptedEngine([_answer("A"), _answer("B")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    left, right = sessions.create(), sessions.create()

    first = asyncio.create_task(loop.process_input(left, "one"))
    await asyncio.wait_for(engine.entered.wait(), _TIMEOUT)
    second = asyncio.create_task(loop.process_input(right, "two"))

    await asyncio.sleep(0)
    assert engine.calls == 1, "two sessions on one loop may still not overlap"

    engine.release.set()
    await asyncio.wait_for(asyncio.gather(first, second), _TIMEOUT)
