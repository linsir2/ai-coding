"""C-6: the behaviour gate for Codex plan parity.

These assertions are the machine-readable half of the "similarity >= 90%"
claim: the plan tool is Codex's ``update_plan``, an approved plan is exactly
one ``"Implement the plan."`` turn, the planning turn is the human's own words,
and neither the task gate nor the review guard may speak on these paths.

No network, no real model: a fake loop records the calls and a fake prompt
answers the one question.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ai_coding.app.commands import handle_input
from ai_coding.core.plan_codex import CodexPlanRunner
from ai_coding.core.plan_report import PlanReport
from ai_coding.core.turn_policy import (
    TurnAction,
    TurnPolicy,
    TurnPolicyConfig,
    TurnSnapshot,
)
from ai_coding.domain.session import SessionData
from ai_coding.domain.stance import PostureResult
from ai_coding.domain.task import TaskState
from ai_coding.security.user_prompt import UserAnswer
from ai_coding.tools.base import ToolContext
from ai_coding.tools.update_plan_tool import UpdatePlanTool

PROPOSED_PLAN_TEXT = (
    "Here is the plan.\n"
    "<proposed_plan>\n"
    "## Plan\n"
    "1. add a health endpoint\n"
    "</proposed_plan>"
)


# --------------------------------------------------------------- 1. update_plan


def test_update_plan_schema_is_codex_shape() -> None:
    schema = UpdatePlanTool().params_json_schema
    assert set(schema["properties"]) == {"explanation", "plan"}
    assert schema["required"] == ["plan"]
    step = schema["properties"]["plan"]["items"]
    assert step["properties"]["status"]["enum"] == [
        "pending",
        "in_progress",
        "completed",
    ]


@pytest.mark.asyncio
async def test_update_plan_result_is_the_fixed_text() -> None:
    task = TaskState()
    result = await UpdatePlanTool().execute(
        {"plan": [{"step": "write the endpoint", "status": "in_progress"}]},
        ToolContext(task=task),
    )
    assert result.success is True
    assert result.output == "Plan updated"
    assert task.items[0].content == "write the endpoint"


@pytest.mark.asyncio
async def test_update_plan_refuses_in_plan_mode() -> None:
    task = TaskState()
    result = await UpdatePlanTool().execute(
        {"plan": [{"step": "write the plan", "status": "pending"}]},
        ToolContext(task=task, mode="plan"),
    )
    assert result.success is False
    assert result.error == "plan_mode"
    assert task.items == []


# --------------------------------------------------------------- the fake loop


class _Console:
    def info(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        pass

    def rule(self, title: str = "") -> None:
        pass

    def delta(self, token: str) -> None:
        pass


class _FakePrompt:
    def __init__(self, answer: UserAnswer) -> None:
        self.answer = answer
        self.calls: list[dict[str, Any]] = []

    async def ask(
        self,
        question: str,
        options: Any = (),
        *,
        detail: str = "",
        intent: str = "",
        data: Any = None,
    ) -> UserAnswer:
        self.calls.append(
            {
                "question": question,
                "options": list(options),
                "detail": detail,
                "intent": intent,
                "data": dict(data or {}),
            }
        )
        return self.answer


class _FakeLoop:
    """Records posture writes and turns; behaves like an idle, codex loop."""

    def __init__(self) -> None:
        self.settings = SimpleNamespace(plan_verify_items=False)
        self.events: list[tuple[str, Any]] = []
        self.turn_running = False
        self.last_outcome = None
        self.workspace = "."
        self.plan_reply = "no plan here"
        self.reply = "done"

    async def process_input(
        self,
        session: SessionData,
        text: str,
        on_delta: Any = None,
        observer: Any = None,
        images: Any = (),
        *,
        synthetic: bool = False,
    ) -> str:
        self.events.append(("process_input", text, synthetic))
        if session.mode == "plan" and not session.plan_implementing:
            return self.plan_reply
        return self.reply

    def commit_posture(
        self,
        session: SessionData,
        *,
        agent: str | None = None,
        readonly: bool | None = None,
        reason: str = "",
        observer: Any = None,
    ) -> None:
        self.events.append(("commit_posture", (agent, readonly)))
        if agent is not None:
            session.agent = agent
        if readonly is not None:
            session.readonly = readonly

    def request_posture(
        self,
        session: SessionData,
        *,
        agent: str | None = None,
        readonly: bool | None = None,
        reason: str = "",
        observer: Any = None,
    ) -> PostureResult:
        self.commit_posture(
            session, agent=agent, readonly=readonly, reason=reason, observer=observer
        )
        return PostureResult.COMMITTED


def _session(agent: str = "build", readonly: bool = False) -> SessionData:
    session = SessionData(
        session_id="s1", title="", created_time="", last_access_time=""
    )
    session.agent = agent
    session.readonly = readonly
    return session


def _sessions() -> Any:
    return SimpleNamespace(save=lambda session: None)


# ------------------------------------------------------------- 2. /plan run


@pytest.mark.asyncio
async def test_plan_run_commits_build_then_sends_one_literal_turn() -> None:
    loop = _FakeLoop()
    session = _session(agent="plan", readonly=True)
    session.task = TaskState(goal="plan")

    await handle_input(loop, "/plan run", session, _sessions(), _Console())

    assert loop.events[0] == ("commit_posture", ("build", False))
    assert loop.events[1] == ("process_input", "Implement the plan.", True)
    turns = [event for event in loop.events if event[0] == "process_input"]
    assert turns == [("process_input", "Implement the plan.", True)]
    assert (session.mode, session.readonly) == ("normal", False)


@pytest.mark.asyncio
async def test_codex_runner_returns_a_report_without_an_early_stop() -> None:
    loop = _FakeLoop()
    session = _session()
    session.task = TaskState(goal="plan")
    session.task.add("one")
    runner = CodexPlanRunner(loop)

    report = await runner.run(session)

    assert isinstance(report, PlanReport)
    # ``PlanReport.stopped`` is None when the run reached the end (C-3/R6).
    assert report.stopped is None
    assert loop.events == [("process_input", "Implement the plan.", True)]
    assert session.plan_implementing is False


# ------------------------------------------------- 3. command layer T1


@pytest.mark.asyncio
async def test_plan_topic_enters_plan_mode_then_offers_and_implements() -> None:
    loop = _FakeLoop()
    loop.plan_reply = PROPOSED_PLAN_TEXT
    session = _session()
    sessions = _sessions()
    prompt = _FakePrompt(UserAnswer(answered=True, text="Yes, implement this plan"))

    began: list[str] = []
    real_begin_plan = session.begin_plan

    def begin_plan(goal: str) -> TaskState:
        began.append(goal)
        return real_begin_plan(goal)

    session.begin_plan = begin_plan  # type: ignore[method-assign]

    await handle_input(
        loop, "/plan 加健康检查", session, sessions, _Console(), user_prompt=prompt
    )

    assert began == ["加健康检查"]
    assert session.task is not None
    assert session.task.goal == "加健康检查"
    assert ("commit_posture", ("plan", True)) in loop.events
    planning = [
        event for event in loop.events if event[0] == "process_input"
    ]
    assert planning[0] == ("process_input", "加健康检查", False)
    assert prompt.calls[0]["intent"] == "plan_implement"
    assert prompt.calls[0]["detail"].startswith("## Plan")
    assert ("commit_posture", ("build", False)) in loop.events
    implemented = [event for event in planning if event[1] == "Implement the plan."]
    assert implemented == [("process_input", "Implement the plan.", True)]


@pytest.mark.asyncio
async def test_a_plan_turn_without_a_block_never_asks() -> None:
    loop = _FakeLoop()
    loop.plan_reply = "still researching"
    session = _session(agent="plan", readonly=True)
    prompt = _FakePrompt(UserAnswer(answered=True, text="Yes, implement this plan"))

    await handle_input(loop, "look deeper", session, _sessions(), _Console(), user_prompt=prompt)

    assert prompt.calls == []
    assert session.mode == "plan"


# -------------------------------------------------------------- 4. stop gates


def test_a_plain_text_stop_on_an_implementing_turn_finishes() -> None:
    policy = TurnPolicy(TurnPolicyConfig(stop_policy="todo_aware"))
    task = TaskState(goal="plan")
    task.add("still pending")

    decision = policy.decide_after_step(
        TurnSnapshot(
            iteration=1,
            plan_implementing=True,
            open_items=tuple(task.items),
        )
    )

    assert decision.action is TurnAction.FINISH
    assert decision.inject is None


def test_a_plan_turn_finishes_without_nudging() -> None:
    policy = TurnPolicy(TurnPolicyConfig())

    decision = policy.decide_after_step(
        TurnSnapshot(iteration=1, mode="plan")
    )
    assert decision.action is TurnAction.FINISH
    assert decision.reason == "model finished"


@pytest.mark.asyncio
async def test_a_yes_keeps_the_approved_plan_as_recent_context() -> None:
    loop = _FakeLoop()
    loop.plan_reply = PROPOSED_PLAN_TEXT
    session = _session()
    prompt = _FakePrompt(UserAnswer(answered=True, text="Yes, implement this plan"))

    await handle_input(
        loop, "/plan 加健康检查", session, _sessions(), _Console(), user_prompt=prompt
    )

    assert session.approved_plan.startswith("## Plan")
    assert "加健康检查" in session.task.goal
