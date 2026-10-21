"""The exploration budget has a mouth: the exit is asked for before it is gone.

A ceiling alone only says where a turn dies.  What made the production
session 135cc71c go wrong was not the ceiling -- 19 rounds out of 30 -- but
*when* the exit was asked for: the guard's nudge came in round 20, after the
model had already written its answer in prose, and the plan it submitted then
hit the workspace gate and sent the turn into a random retry loop.

So the ask comes earlier: with three rounds left, the model is told to hand
something over (present_plan or finish_research) while it is still exploring
and still has something to hand over.

What these tests pin:

  R2  the ask arrives at the budget's edge (round 13 of 16), as a synthetic
      user message carrying the guard's own text;
  R3  it is asked once -- the same bookkeeping the guard uses, so the two
      never stack;
  R4  the guard does not ask again afterwards, and the turn ends at the
      ceiling with the terminal line;
  R5  the ceiling still ends the turn the way it did (MAX_ITERATIONS + the
      note);
  R6  an ordinary turn is never asked for an exit;
  R7  when the guard spoke first, the early ask stays quiet.
"""

from __future__ import annotations

from typing import Any

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.plan_runner import PLANNING_MAX_ITERATIONS, continuations_disabled
from ai_coding.core.turn_books import TurnBooks
from ai_coding.core.turn_policy import PLAN_NUDGE, PLAN_UNSUBMITTED_NOTE
from ai_coding.domain.loop import StepResult, TurnStatus
from ai_coding.domain.message import ToolCallRef, ToolResult
from ai_coding.security.approval import AutoApproveCallback
from ai_coding.service.session_service import SessionService
from ai_coding.tools.base import BaseTool
from ai_coding.tools.registry import ToolRegistry

#: The round the ask lands on: the exploration budget (16) minus the three
#: rounds kept for handing something over.
ASK_ROUND = 13


class _Peek(BaseTool):
    """A read-only tool: the model can call it forever and change nothing."""

    parallel_safe = True
    mutates_world = False

    @property
    def name(self) -> str:
        return "peek"

    @property
    def description(self) -> str:
        return "look at something"

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, params: dict[str, Any], ctx: Any = None) -> ToolResult:
        return ToolResult(success=True, output="looked")


def _tool_step(call_id: str) -> StepResult:
    return StepResult(
        text=None,
        tool_calls=[ToolCallRef(id=call_id, name="peek", arguments="{}")],
        finish_reason="tool_calls",
    )


def _text_step(text: str) -> StepResult:
    return StepResult(text=text, finish_reason="stop")


class _ScriptedEngine(ChatEngine):
    """Hands out the scripted steps, one per model call.

    When the script runs out it keeps calling the tool, so a turn that should
    have ended shows up in the assertions instead of in an error.
    """

    def __init__(self, steps: list[StepResult]) -> None:
        self._steps = list(steps)
        self.calls = 0
        #: How many of the guard's nudge messages each model call could see.
        self.nudges_seen: list[int] = []

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.calls += 1
        self.nudges_seen.append(
            sum(1 for message in messages if (message.content or "") == PLAN_NUDGE)
        )
        if self._steps:
            return self._steps.pop(0)
        return _tool_step(f"spare-{self.calls}")

    async def aclose(self) -> None:
        return None


def _loop(engine: ChatEngine, sessions: SessionService, **kwargs: Any) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(_Peek())
    kwargs.setdefault("settings", AIConfig())
    return AgentLoop(
        engine,
        sessions,
        registry,
        approval=AutoApproveCallback(),
        workspace=".",
        **kwargs,
    )


def _plan_session(sessions: SessionService):
    session = sessions.create()
    session.mode, session.readonly = "plan", True
    return session


def _plan_nudges(session: Any) -> list[Any]:
    """The ask in the transcript: the guard's text, as a user message."""
    return [
        message
        for message in session.messages
        if (message.content or "") == PLAN_NUDGE
    ]


def _capture_books(monkeypatch: pytest.MonkeyPatch) -> list[TurnBooks]:
    """Keep the turn's own books, so the test can read what they counted."""
    captured: list[TurnBooks] = []

    class _Spy(TurnBooks):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            captured.append(self)

    monkeypatch.setattr("ai_coding.core.agent_loop.TurnBooks", _Spy)
    return captured


async def _planning_round(
    tmp_path: Any, steps: list[StepResult]
) -> tuple[_ScriptedEngine, AgentLoop, Any]:
    """A planning round on the production budget, with a model that explores."""
    engine = _ScriptedEngine(steps)
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    session = _plan_session(sessions)

    with continuations_disabled(loop, max_iterations=PLANNING_MAX_ITERATIONS):
        await loop.process_input(session, "调研这个分支")

    return engine, loop, session


# ---- the ask, at the budget's edge ------------------------------------------


@pytest.mark.asyncio
async def test_the_exit_is_asked_for_before_the_budget_is_gone(tmp_path):
    """R2: with three rounds left, the model is told to hand something over.

    The ask is the guard's own text, carried by a synthetic user message --
    one instruction, not two.  The model had twelve rounds of exploration
    before the question, and the question reached it on its thirteenth call.
    """
    engine, _loop_, session = await _planning_round(
        tmp_path, [_tool_step(f"c{index}") for index in range(20)]
    )

    nudges = _plan_nudges(session)
    assert len(nudges) == 1, "the exit was asked for exactly once"
    assert nudges[0].content == PLAN_NUDGE
    assert nudges[0].synthetic is True, "the ask is the harness's, not the human's"
    assert engine.nudges_seen.index(1) + 1 == ASK_ROUND, (
        "the ask arrives at the budget's edge, not at the ceiling"
    )


@pytest.mark.asyncio
async def test_the_exit_is_asked_for_only_once(tmp_path, monkeypatch):
    """R3: one ask, then silence.

    The early ask spends the same remedy the guard spends, so the model is
    never nagged twice about the same thing -- and the guard does not offer a
    second one afterwards.
    """
    books = _capture_books(monkeypatch)
    engine, _loop_, session = await _planning_round(
        tmp_path, [_tool_step(f"c{index}") for index in range(20)]
    )

    assert len(_plan_nudges(session)) == 1, "the exit was asked for only once"
    assert books, "the turn kept no books"
    assert books[0].plan_nudged is True, "the ask was counted"
    # three more rounds ran after the ask, and none of them asked again
    assert sum(1 for seen in engine.nudges_seen if seen) >= 3, (
        "the model had three rounds to hand something over"
    )


@pytest.mark.asyncio
async def test_the_guard_does_not_ask_again_after_the_early_ask(tmp_path):
    """R4: the ask and the guard share one ledger.

    The early ask already spent the remedy, so when the budget runs out the
    guard buys no further round -- the turn ends there, with the line that
    says what was left undone.
    """
    engine, loop, session = await _planning_round(
        tmp_path, [_tool_step(f"c{index}") for index in range(20)]
    )

    assert len(_plan_nudges(session)) == 1, "the guard did not ask a second time"
    outcome = loop.last_outcome
    assert outcome is not None
    # 15 rounds ran: 12 of exploration, the ask (a round of its own, spent
    # asking), and 3 more to hand something over.  The guard bought no
    # further round at the ceiling -- the remedy was spent once.
    assert outcome.iterations == PLANNING_MAX_ITERATIONS - 1, (
        "the turn ended at the ceiling without buying another round"
    )
    assert outcome.note == PLAN_UNSUBMITTED_NOTE


@pytest.mark.asyncio
async def test_the_ceiling_still_ends_the_turn_with_the_note(tmp_path):
    """R5: the ceiling still ends a planning turn the way it did.

    The ask changed when the exit is requested, not what happens when the
    budget runs out: a turn that runs out without a submission ends with
    MAX_ITERATIONS and the line that says no plan was submitted.
    """
    _engine, loop, _session = await _planning_round(
        tmp_path, [_tool_step(f"c{index}") for index in range(20)]
    )

    outcome = loop.last_outcome
    assert outcome is not None
    assert outcome.status is TurnStatus.MAX_ITERATIONS
    assert outcome.note == PLAN_UNSUBMITTED_NOTE


# ---- outside the planning posture -------------------------------------------


@pytest.mark.asyncio
async def test_an_ordinary_turn_is_never_asked_for_an_exit(tmp_path):
    """R6: outside the planning posture nothing changes.

    The ask belongs to the planning round: an ordinary turn runs on the
    budget from the settings and is never asked for a plan -- not at round
    13, not at any other round.
    """
    engine = _ScriptedEngine([_tool_step(f"c{index}") for index in range(30)])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions, settings=AIConfig(max_tool_iterations=20))
    session = sessions.create()

    await loop.process_input(session, "继续")

    assert _plan_nudges(session) == [], "an ordinary turn is never asked for a plan"
    assert loop.policy.config.max_iterations == 20, (
        "the ordinary budget still comes from the settings"
    )
    assert engine.calls >= ASK_ROUND, "the turn really did run past the ask round"


@pytest.mark.asyncio
async def test_the_guard_speaks_first_and_the_early_ask_stays_quiet(tmp_path):
    """R7: when the guard spoke first, the early ask does not follow.

    The model stopped on its own in round 5, so the guard asked for the exit
    there.  The rounds after that run into the budget's edge -- and no second
    ask arrives: the remedy was spent once, and it was spent by the guard.
    """
    steps = [_tool_step(f"c{index}") for index in range(4)]
    steps.append(_text_step("分析完了。"))  # round 5: the model stops on its own
    steps += [_tool_step(f"d{index}") for index in range(20)]  # and keeps exploring

    engine, _loop_, session = await _planning_round(tmp_path, steps)

    assert len(_plan_nudges(session)) == 1, "the model was asked once, not twice"
    # the ask came from the guard (it stopped the model in round 5, so the
    # question reached the model on its sixth call) -- not from the budget
    assert engine.nudges_seen.index(1) + 1 == 6, (
        "the ask came from the guard in round 5, not from the budget's edge"
    )
