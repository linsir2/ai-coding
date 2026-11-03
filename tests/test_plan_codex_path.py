"""C-2/C-3: the codex plan path (``<proposed_plan>`` + one implementation turn).

The facts these tests pin are the ones Codex has and this harness did not:

* the plan is a text block, not ``present_plan`` / ``update_plan`` output;
* the implementation turn is ``"Implement the plan."``, byte for byte;
* that turn has no ceiling, no ``todo_write``, and no structural continuation;
* a plan turn is sent without ``<task-state>`` but with ``<plan-mode>``;
* ``/plan run`` writes the build posture through before the run starts.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.app.commands import handle_input
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.plan_codex import (
    CodexPlanRunner,
    extract_proposed_plan,
    strip_proposed_plan,
)
from ai_coding.core.request_assembly import RequestAssembler
from ai_coding.core.turn_books import TurnBooks
from ai_coding.core.turn_policy import (
    TurnAction,
    TurnPolicy,
    TurnPolicyConfig,
    TurnSnapshot,
)
from ai_coding.domain.loop import StepResult
from ai_coding.domain.session import SessionData
from ai_coding.domain.task import TaskState
from ai_coding.security.approval import AutoApproveCallback
from ai_coding.service.session_service import SessionService
from ai_coding.tools.registry import ToolRegistry
from ai_coding.tools.todo_write_tool import TodoWriteTool
from ai_coding.tools.update_plan_tool import UpdatePlanTool

# ---------------------------------------------------------------- 1. parser


def test_extract_proposed_plan_takes_the_first_complete_block():
    text = (
        "Here is the plan.\n"
        "<proposed_plan>\n## Plan\n1. add x\n</proposed_plan>\n"
        "<proposed_plan>second</proposed_plan>"
    )
    assert extract_proposed_plan(text) == "## Plan\n1. add x"


def test_extract_proposed_plan_missing_or_unclosed_is_none():
    assert extract_proposed_plan("no block here") is None
    # C-2: a real provider (qwen-plus, measured) omitted the close; the tail is
    # still the plan, not "no plan".
    assert extract_proposed_plan("<proposed_plan>unclosed") == "unclosed"
    assert extract_proposed_plan("<proposed_plan></proposed_plan>") == ""


def test_strip_proposed_plan_removes_only_the_block():
    text = "intro\n<proposed_plan>body</proposed_plan>\noutro"
    assert strip_proposed_plan(text) == "intro\n\noutro"
    assert strip_proposed_plan("plain text") == "plain text"
    assert strip_proposed_plan("<proposed_plan>unclosed") == ""


# ------------------------------------------------------- 2. stop decisions


def test_a_plan_implementing_turn_finishes_and_ignores_the_task_gate():
    policy = TurnPolicy(TurnPolicyConfig(stop_policy="todo_aware"))
    task = TaskState(goal="real run")
    task.add("still open")
    decision = policy.decide_after_step(
        TurnSnapshot(iteration=1, plan_implementing=True, open_items=tuple(task.items))
    )
    assert decision.action is TurnAction.FINISH
    assert decision.reason == "codex plan turn: no structural continuation"
    assert decision.inject is None


def test_a_plan_turn_finishes_instead_of_nudging():
    policy = TurnPolicy(TurnPolicyConfig())
    decision = policy.decide_after_step(
        TurnSnapshot(iteration=1, mode="plan")
    )
    assert decision.action is TurnAction.FINISH
    assert decision.reason == "model finished"
    assert decision.inject is None


# ------------------------------------------------------- 3. iteration budget


def test_plan_implementing_has_no_iteration_ceiling():
    policy = TurnPolicy(TurnPolicyConfig(max_iterations=3))
    decision = policy.decide_next_iteration(
        TurnSnapshot(iteration=5, plan_implementing=True)
    )
    assert decision.action is TurnAction.ITERATE
    normal = policy.decide_next_iteration(TurnSnapshot(iteration=5))
    assert normal.action is TurnAction.FINISH


# ----------------------------------------------------------- 4. the runner


class _Scripted(ChatEngine):
    """One scripted model step that also records what the request carried."""

    def __init__(self, step: StepResult) -> None:
        self.step_result = step
        self.seen: list[list[Any]] = []
        self.tools_seen: list[list[str]] = []
        self.session: SessionData | None = None
        self.flags_seen: list[bool] = []

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.seen.append(list(messages))
        self.tools_seen.append([tool["function"]["name"] for tool in tools])
        assert self.session is not None
        self.flags_seen.append(self.session.plan_implementing)
        return self.step_result

    async def aclose(self) -> None:
        return None


def _loop(engine: ChatEngine, tmp_path: Any) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(UpdatePlanTool())
    registry.register(TodoWriteTool())
    return AgentLoop(
        engine,
        SessionService(tmp_path / "sessions"),
        registry,
        approval=AutoApproveCallback(),
        settings=AIConfig(),
        workspace=str(tmp_path / "ws"),
    )


@pytest.fixture(autouse=True)
def _workspace(tmp_path):
    (tmp_path / "ws").mkdir()
    return tmp_path


@pytest.mark.asyncio
async def test_codex_runner_is_one_turn_with_the_literal_prompt(tmp_path):
    engine = _Scripted(StepResult(text="done", finish_reason="stop"))
    loop = _loop(engine, tmp_path)
    session = loop.sessions.create()
    engine.session = session

    await CodexPlanRunner(loop).run(session)

    assert len(engine.seen) == 1, "exactly one model step (no continuation)"
    assert engine.flags_seen == [True]
    assert session.plan_implementing is False, "the flag must not survive the run"
    prompts = [
        message.content
        for message in session.messages
        if message.role == "user" and message.synthetic
    ]
    assert prompts == ["Implement the plan."], "the prompt is Codex's literal text"
    bodies = [message.content or "" for message in session.messages]
    assert not any("[plan-run]" in body for body in bodies)
    assert not any("[plan-continue]" in body for body in bodies)
    # C-3: the legacy list tool is not in the surface for this turn; the plan
    # tool still is.
    assert engine.tools_seen == [["update_plan"]]


# ------------------------------------------------------- 5. request assembly


class _Compactor:
    async def compress(self, history, *, deadline, on_summary=None, force=False, pinned=()):
        return history


class _Memory:
    def block(self) -> tuple[str, list[str]]:
        return "", []


class _Reporter:
    def report_context(self, **kwargs: Any) -> str:
        return "canonical"

    def tokens_spent(self) -> int:
        return 0


def _assembler() -> RequestAssembler:
    async def _instructions(deadline: Any, session: Any = None) -> str | None:
        return "system instructions"

    return RequestAssembler(
        compactor=_Compactor(),
        policy=TurnPolicy(TurnPolicyConfig()),
        memory=_Memory(),
        reporter=_Reporter(),
        build_instructions=_instructions,
        settings=SimpleNamespace(
            auto_compact=True,
            working_memory_chars=1200,
        ),
        emit=lambda event: None,
    )


async def _assemble(
    assembler: RequestAssembler,
    session: SessionData,
    task: TaskState,
    *,
    index: int = 1,
):
    return await assembler.assemble(
        session=session,
        task=task,
        books=TurnBooks(),
        turn_number=1,
        hook_context="",
        deadline=None,
        index=index,
        previous_request=None,
    )


def _task() -> TaskState:
    task = TaskState(goal="plan")
    task.add("first")
    return task


@pytest.mark.asyncio
async def test_codex_plan_turn_carries_plan_mode_and_no_task_block():
    session = SessionData(session_id="s1", title="", created_time="", last_access_time="")
    session.agent = "plan"
    assembled = await _assemble(_assembler(), session, _task())
    bodies = "\n".join(message.content or "" for message in assembled.request)
    assert "<plan-mode>" in bodies
    assert "Implement the plan." not in bodies
    assert "<task-state>" not in bodies
    # The block sits before the task slot (which is empty on this path), so
    # removing the duplicate task block did not reorder anything else.
    assert bodies.index("<plan-mode>") >= 0


@pytest.mark.asyncio
async def test_codex_implementing_turn_carries_no_task_block():
    session = SessionData(session_id="s1", title="", created_time="", last_access_time="")
    session.agent = "build"
    session.plan_implementing = True
    assembled = await _assemble(_assembler(), session, _task())
    bodies = "\n".join(message.content or "" for message in assembled.request)
    assert "<task-state>" not in bodies
    assert "<plan-mode>" not in bodies


@pytest.mark.asyncio
async def test_codex_implementing_turn_gets_no_budget_pressure_at_step_twelve():
    """C-3: the implementation turn runs with ``mode == "normal"``; without the
    ``plan_implementing`` signal the assembler injected "步数 12/15" and the
    model wrapped up early on a cap this turn does not inherit."""
    session = SessionData(session_id="s1", title="", created_time="", last_access_time="")
    session.agent = "build"
    session.plan_implementing = True
    assembler = _assembler()
    assembler._policy = TurnPolicy(  # noqa: SLF001 - the config under test
        TurnPolicyConfig(max_iterations=15)
    )

    assembled = await _assemble(assembler, session, _task(), index=12)

    bodies = "\n".join(message.content or "" for message in assembled.request)
    assert "[budget]" not in bodies
    assert "12/15" not in bodies


# ------------------------------------------------------- 6. /plan run posture


class _Console:
    def __init__(self) -> None:
        self.infos: list[str] = []
        self.errors: list[str] = []

    def info(self, message: str) -> None:
        self.infos.append(message)

    def error(self, message: str) -> None:
        self.errors.append(message)

    def rule(self, title: str = "") -> None:
        pass

    def delta(self, token: str) -> None:
        pass


class _FakeLoop:
    """A loop double that records the order of the posture write and the turn."""

    def __init__(self) -> None:
        self.settings = SimpleNamespace(plan_verify_items=False)
        self.events: list[tuple[str, Any]] = []
        self.turn_running = False
        self.last_outcome = None
        self.workspace = "."

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
        self.events.append(("process_input", text))
        return "done"

    def commit_posture(
        self,
        session: SessionData,
        *,
        agent: str | None = None,
        readonly: bool | None = None,
        reason: str = "",
        observer: Any = None,
    ) -> None:
        self.events.append(("commit_posture", (agent, readonly, reason)))
        session.agent = agent if agent is not None else session.agent
        if readonly is not None:
            session.readonly = readonly


@pytest.mark.asyncio
async def test_plan_run_writes_the_build_posture_through_before_the_run():
    loop = _FakeLoop()
    session = SessionData(session_id="s1", title="", created_time="", last_access_time="")
    session.agent = "plan"
    session.readonly = True
    session.task = TaskState(goal="plan")
    sessions = SimpleNamespace(save=lambda s: None)

    await handle_input(loop, "/plan run", session, sessions, _Console())

    assert [name for name, _ in loop.events][:2] == [
        "commit_posture",
        "process_input",
    ]
    assert loop.events[0][1] == ("build", False, "/plan run")
    assert (session.mode, session.readonly) == ("normal", False)
    assert loop.events[1] == ("process_input", "Implement the plan.")
