"""Plan mode is a planning mode, not a read-only flag.

The design said it from the start (19 §4): "[规划轮] ``/plan <topic>`` 或
模式行下的普通 prompt".  The second half never landed -- the front end sent
the operator's words as an ordinary prompt, so the model answered instead of
planning, and the guard could only ask for a plan after the work was done.

These tests pin the half that landed.
"""

from __future__ import annotations

from typing import Any

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.app.commands import run_human_turn
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.events import IterationStarted, RecordingObserver
from ai_coding.core.plan_runner import PLAN_MARK, plan_only
from ai_coding.core.turn_policy import TurnPolicy, TurnPolicyConfig
from ai_coding.domain.loop import StepResult
from ai_coding.domain.session import SessionData
from ai_coding.domain.stance import PostureResult
from ai_coding.domain.task import TaskState
from ai_coding.security.approval import AutoApproveCallback
from ai_coding.service.session_service import SessionService
from ai_coding.tools.registry import ToolRegistry


class _Console:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def info(self, message: str) -> None:  # pragma: no cover - not used here
        pass

    def rule(self, title: str = "") -> None:  # pragma: no cover - not used here
        pass

    def delta(self, token: str) -> None:  # pragma: no cover - not used here
        pass


class _Loop:
    """A loop double that records the turns it was asked to run."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.budgets: list[int] = []
        self.workspace = "."
        self.extra_workspace_roots: tuple[str, ...] = ()
        self.policy = TurnPolicy(TurnPolicyConfig(max_auto_continuations=3))

    async def process_input(
        self,
        session: SessionData,
        text: str,
        on_delta: Any = None,
        observer: Any = None,
        **kwargs: Any,
    ) -> str:
        self.calls.append((text, session.mode))
        self.budgets.append(self.policy.config.max_iterations)
        session.add_message(_user(text))
        return "done"

    def request_posture(self, session: SessionData, **kwargs: Any) -> Any:
        self.commit_posture(session, **kwargs)
        return PostureResult.COMMITTED

    def commit_posture(
        self,
        session: SessionData,
        *,
        agent: str | None = None,
        readonly: bool | None = None,
        reason: str = "",
        observer: Any = None,
    ) -> None:
        session.agent = agent if agent is not None else session.agent
        session.readonly = bool(readonly) if readonly is not None else session.readonly
        session.pending_posture = None


def _user(content: str, *, synthetic: bool = False) -> Any:
    from ai_coding.domain.run import user_message
    from ai_coding.infra.time_utils import now_utc

    return user_message(content, now_utc(), synthetic=synthetic)


def _session(mode: str = "normal") -> SessionData:
    session = SessionData(session_id="s1", title="t", created_time="", last_access_time="")
    # M1b: the agent is the authority; ``mode`` is derived from it.
    session.agent = "plan" if mode == "plan" else "build"
    return session


# ---- the routing: a human utterance is a planning round in plan mode ----


@pytest.mark.asyncio
async def test_a_plan_mode_utterance_is_a_planning_round(monkeypatch):
    """Plan mode + a human message -> the planning round, not an ordinary turn."""
    import ai_coding.core.plan_runner as plan_runner

    seen: dict[str, Any] = {}

    async def fake_plan_only(loop: Any, session: Any, *, topic: str, **kwargs: Any) -> Any:
        seen["topic"] = topic
        return plan_runner.PlanReport()

    monkeypatch.setattr(plan_runner, "plan_only", fake_plan_only)
    # the helper imports it lazily inside the function
    monkeypatch.setattr("ai_coding.core.plan_runner.plan_only", fake_plan_only)

    loop = _Loop()
    session = _session("plan")
    console = _Console()
    await run_human_turn(loop, session, "请你调研这个分支", console=console)

    assert seen["topic"] == "请你调研这个分支", "the operator's words are the topic"
    assert loop.calls == [], "an ordinary turn must not have run"


@pytest.mark.asyncio
async def test_an_ordinary_utterance_is_an_ordinary_turn():
    """Outside plan mode nothing changes: the loop runs the text as it always did."""
    loop = _Loop()
    session = _session("normal")
    answer = await run_human_turn(loop, session, "hello", console=_Console())

    assert answer == "done"
    assert loop.calls == [("hello", "normal")]


# ---- what the planning round writes down ----


@pytest.mark.asyncio
async def test_the_planning_round_keeps_the_operators_words_and_marks_the_instruction():
    """The transcript shows what the operator said, not what the harness said."""
    loop = _Loop()
    session = _session("plan")
    await plan_only(loop, session, topic="请你调研这个分支")

    messages = session.messages
    assert messages, "the planning round wrote nothing"
    assert messages[0].role == "user"
    assert messages[0].content == "请你调研这个分支", "the operator's own words come first"
    assert messages[1].content.startswith(PLAN_MARK), "the instruction is marked as the harness's"
    assert "Create a detailed implementation plan" in messages[1].content
    assert "请你调研这个分支" in messages[1].content, "the topic rides in the instruction"


@pytest.mark.asyncio
async def test_the_planning_round_resets_the_book_and_raises_the_budget():
    """A new plan starts a fresh book, and the planning budget is the planning one.

    The ordinary turn budget comes from the settings; a planning round gets
    its own exploration budget and no auto continuation, because a plan is
    written by thinking, not by being dragged along.
    """
    from ai_coding.core.plan_runner import PLANNING_MAX_ITERATIONS

    loop = _Loop()
    session = _session("plan")
    session.task = None
    await plan_only(loop, session, topic="主题")

    assert session.task is not None
    assert session.task.goal == "主题"
    assert loop.budgets == [PLANNING_MAX_ITERATIONS], (
        "the planning round runs on the planning budget"
    )


# ---- the exploration budget, on the wire ------------------------------------


class _ScriptedEngine(ChatEngine):
    """Hands out the scripted steps, one per model call."""

    def __init__(self, steps: list) -> None:
        self._steps = list(steps)

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        if not self._steps:
            raise AssertionError("engine called more times than the script allows")
        return self._steps.pop(0)

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_the_planning_round_runs_on_a_sixteen_round_budget(tmp_path):
    """R1: the planning round's ceiling is the exploration budget: 16 rounds.

    The wire says so: every ``IterationStarted`` of a planning round carries
    ``max_iterations=16`` -- not the settings' 10, and not the 30 the
    planning round used to get (30 was never a budget anyone chose; it was
    the number that was there).

    Measured: a good model submits a plan in 4 rounds (live check), while the
    runs that never submitted burned 15-19 -- 16 sits between the two.
    """
    engine = _ScriptedEngine([StepResult(text="还在看", finish_reason="stop")] * 4)
    sessions = SessionService(tmp_path / "sessions")
    observer = RecordingObserver()
    loop = AgentLoop(
        engine,
        sessions,
        ToolRegistry(),
        approval=AutoApproveCallback(),
        settings=AIConfig(),
        workspace=".",
        observer=observer,
    )
    session = sessions.create()
    session.agent = "plan"

    await plan_only(loop, session, topic="调研这个分支")

    started = observer.of_type(IterationStarted)
    assert started, "the planning round ran no iteration at all"
    assert {event.max_iterations for event in started} == {16}, (
        "the planning round runs on the exploration budget of 16"
    )


# ---- the front door ----


@pytest.mark.asyncio
async def test_the_workspace_gate_still_stops_a_planning_round(monkeypatch):
    """A topic that reaches outside the workspace is refused, and the operator is told."""

    loop = _Loop()
    session = _session("plan")

    # /etc is outside any workspace; the gate refuses before any model call.
    from ai_coding.core.plan_runner import outside_workspace_paths

    offenders = outside_workspace_paths(["/etc/passwd"], ".", extra_roots=())
    if not offenders:  # pragma: no cover - the gate is the point of the test
        pytest.skip("the workspace gate did not flag /etc/passwd")

    report = await plan_only(loop, session, topic="/etc/passwd")
    assert report.stopped, "an outside target stops the planning round"
    assert loop.calls == [], "nothing was run"


# ---- the transcript ----


def test_the_planning_instruction_is_not_the_operators_speech():
    """The instruction the model acts on is the harness's, and is marked so.

    The mark is what lets the transcript show the operator's words alone:
    the replay skips a message that carries it.
    """
    assert PLAN_MARK == "[plan] "
    # A prefix, like the other harness marks (``[notice] ``, ``[steering] ``):
    # the replay recognises a message by what it starts with.
    assert PLAN_MARK.endswith(" ")
    assert PLAN_MARK.startswith("[")


@pytest.mark.asyncio
async def test_a_new_planning_round_does_not_discard_an_approved_plan():
    """An armed plan is a pending commitment; a new round must not drop it.

    ``begin_plan`` swaps the whole task book (a new plan's step ids collide
    with the old plan's receipts), so a planning round that starts while a
    plan is armed would silently un-approve it -- and the run would never
    happen.  The refusal reuses the shape the workspace gate already uses.
    """
    loop = _Loop()
    session = _session("plan")
    session.task = TaskState(goal="旧主题")
    session.task.add("写代码", verify="pytest -q")
    session.task.armed = True
    session.task.armed_profile = "plan"

    report = await plan_only(loop, session, topic="新主题")

    assert report.stopped, "an armed plan stops a new planning round"
    assert session.task.armed is True, "the approved plan is still approved"
    assert [item.content for item in session.task.items] == ["写代码"]
    assert loop.calls == [], "nothing was run"
