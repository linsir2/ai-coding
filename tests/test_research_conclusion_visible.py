"""23: the research conclusion must reach the user -- as a first-class event.

N1 gave a plan-mode turn two exits and made the research exit carry the
conclusion as the tool's own output.  That was enough for the *model* (the
summary lands in its history) and for the *guard* (the turn ends), but not
for the *human*: a tool result is the one channel in the system that is
lossy by design -- 500 chars in the event, 120 on the screen -- and
``finish_research`` streams no chunks, so the card cannot even be opened.
Measured on a real session: a 4116-char conclusion showed 120 chars.

So the conclusion gets its own channel: a first-class event carrying the
full text, drawn as markdown prose.  This file pins the backend half --
the event exists, it carries the whole summary, it is not sent when the
exit was refused, and it reaches the wire.
"""

from __future__ import annotations

import json

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.events import ResearchConcluded
from ai_coding.domain.loop import StepResult
from ai_coding.domain.message import ToolCallRef
from ai_coding.security.approval import AutoApproveCallback
from ai_coding.service.session_service import SessionService
from ai_coding.tools.finish_research_tool import FinishResearchTool
from ai_coding.tools.registry import ToolRegistry

#: A conclusion longer than the tool-output preview limit (500), so a test
#: that only checked a short summary would pass even with the truncation.
LONG_SUMMARY = "## 结论\n\n" + ("计划模式把意图、条目、证明命令分开建模。\n" * 120)


# ---- R1: the tool sends the conclusion -------------------------------------


@pytest.mark.asyncio
async def test_a_successful_conclusion_is_announced_with_its_full_text():
    """The conclusion is a deliverable, so it gets its own channel.

    The tool output is the *lossy* channel (500 chars in the event, 120 on
    the screen); the deliverable rides a first-class event instead, and it
    rides it whole.
    """
    tool = FinishResearchTool()
    seen: list[ResearchConcluded] = []
    ctx = _ctx(emit=seen.append)

    result = await tool.execute({"summary": LONG_SUMMARY}, ctx)

    assert result.success is True
    assert len(seen) == 1
    # The announcement carries the very text the tool returns: one conclusion,
    # one wording (the model reads the output, the human reads the event).
    assert seen[0].summary == result.output
    # Not truncated: the whole conclusion is on the event.
    assert len(seen[0].summary) > 500


@pytest.mark.asyncio
async def test_a_refused_conclusion_announces_nothing():
    """A refusal is not a conclusion -- nothing is announced."""
    tool = FinishResearchTool()
    seen: list[ResearchConcluded] = []
    ctx = _ctx(emit=seen.append)

    result = await tool.execute({"summary": "   "}, ctx)

    assert result.success is False
    assert seen == []


# ---- R3/R4: the wire -------------------------------------------------------


def test_the_conclusion_reaches_the_wire_with_its_full_text():
    from ai_coding.server.bridge import to_wire

    wire = to_wire(ResearchConcluded(summary=LONG_SUMMARY))

    assert wire == {
        "type": "research.concluded",
        "data": {"summary": LONG_SUMMARY},
    }
    assert len(wire["data"]["summary"]) > 500


# ---- the loop seam: the event is sent exactly once per conclusion ----------


@pytest.mark.asyncio
async def test_the_loop_announces_the_conclusion_once(tmp_path):
    """One conclusion, one announcement -- the event is the turn's, not the
    tool call's, so it must not fire twice for one exit."""
    engine = _ScriptedEngine([_call("c1", "finish_research", {"summary": LONG_SUMMARY})])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    session = _plan_session(sessions)
    events: list = []
    loop.observer = _Collecting(events)

    await loop.process_input(session, "看看计划模式怎么设计的")

    announced = [e for e in events if isinstance(e, ResearchConcluded)]
    assert len(announced) == 1
    assert announced[0].summary.strip() == LONG_SUMMARY.strip()


# ---- helpers ----------------------------------------------------------------


def _ctx(*, emit=None, mode: str = "plan"):
    from ai_coding.domain.task import TaskState
    from ai_coding.tools.base import ToolContext, TurnConclusion

    return ToolContext(
        task=TaskState(goal="g"),
        workspace="/tmp/ws",
        session_id="sid",
        mode=mode,
        conclusion=TurnConclusion(),
        emit=emit if emit is not None else (lambda _event: None),
    )


class _Collecting:
    """An observer that keeps every event."""

    def __init__(self, events: list) -> None:
        self.events = events

    def on_event(self, event) -> None:
        self.events.append(event)


class _ScriptedEngine(ChatEngine):
    def __init__(self, steps: list) -> None:
        self._steps = list(steps)

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        if not self._steps:
            raise AssertionError("engine called more times than the script allows")
        item = self._steps.pop(0)
        if on_delta is not None and item.text:
            on_delta(item.text)
        return item

    async def aclose(self) -> None:
        return None


def _call(call_id: str, name: str, args: dict) -> StepResult:
    return StepResult(
        text=None,
        tool_calls=[ToolCallRef(id=call_id, name=name, arguments=json.dumps(args))],
        finish_reason="tool_calls",
    )


def _loop(engine: ChatEngine, sessions: SessionService) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(FinishResearchTool())
    return AgentLoop(
        engine,
        sessions,
        registry,
        approval=AutoApproveCallback(),
        settings=AIConfig(),
        workspace=".",
    )


def _plan_session(sessions: SessionService):
    session = sessions.create()
    session.mode, session.readonly = "plan", True
    return session
