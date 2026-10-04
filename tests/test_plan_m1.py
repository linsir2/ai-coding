"""M1 (TDD): /plan absorbs /plan-run + per-step short conversations.

Two changes, one milestone:

A. ``/plan <topic>`` plans AND executes in one command.  ``/plan-run`` is
   deleted — the two-command handoff let the plan go stale between commands
   (measured: a run that executed 0 turns against a list from a prior turn).

B. Each plan step runs in a FRESH session seeded with goal + plan + prior
   step summaries, instead of resending the parent's whole history.  The
   parent session only receives the step prompt (replay marker) and a one-line
   summary.  Measured before: 4 turns -> 41k/82k/130k/254k prompt tokens
   (quadratic).  After: constant per step (linear).

Borrowed mechanisms (see docs): subagent's fresh-SessionData pattern
(core/subagent.py:_run_in), Hermes's structured handoff schema, opencode's
anchored iterative merge, Codex's goal persistence, Hermes's "only pending
items" rule.
"""

from types import SimpleNamespace

import pytest

from ai_coding.core.plan_runner import STEP_HEAD, PlanRunner
from ai_coding.core.turn_policy import TurnPolicy, TurnPolicyConfig
from ai_coding.domain.message import ChatMessage
from ai_coding.domain.session import SessionData
from ai_coding.domain.task import TaskState


def _session_with(*contents, goal="plan", verifies=()):
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    task = TaskState(goal=goal)
    for index, content in enumerate(contents):
        item = task.add(content)
        if index < len(verifies) and verifies[index]:
            item.verify = verifies[index]
    session.task = task
    return session


class _StubLoop:
    """Stands in for AgentLoop; records what each turn was asked to send.

    ``prompt_chars`` accumulates the size of the session's message list at
    each ``process_input`` call — the proxy for what the engine would be
    billed for.  A step that resends the parent's whole history shows up as
    a growing series; a step with a fresh context shows up as a flat one.
    """

    def __init__(self, script=(), step_budget=8, run_budget=0, usage=None):
        self.settings = SimpleNamespace(
            plan_step_max_iterations=step_budget,
            plan_max_step_turns=run_budget,
            stop_policy="todo_aware",
        )
        self.policy = TurnPolicy(TurnPolicyConfig(max_iterations=100))
        self._script = list(script)
        self._usage = usage
        self.calls = []          # (text, session_id)
        self.prompt_chars = []   # chars of session.messages at each call
        self.step_sessions = []  # the session object each call ran on

    async def process_input(self, session, text, on_delta=None, observer=None):
        session.turn_count += 1
        self.calls.append((text, session.session_id))
        self.step_sessions.append(session)
        self.prompt_chars.append(sum(len(m.content or "") for m in session.messages))
        # simulate the real loop: each turn leaves a tool result behind
        session.messages.append(
            ChatMessage(role="tool", content="x" * 5000, timestamp="",
                        tool_call_id=f"call-{len(self.calls)}")
        )
        action = self._script.pop(0) if self._script else "complete"
        if action in ("complete", "complete-all"):
            targets = session.task.open_items()
            if action == "complete":
                targets = targets[:1]
            for item in targets:
                if not item.is_terminal():
                    item.status = "completed"
        elif action == "verify-pass":
            for item in session.acceptance:
                item.passed = True
                item.output = "ok"
        elif action == "verify-fail":
            for item in session.acceptance:
                item.passed = False
                item.output = "assert 245 == 200"
        elif action == "ignore":
            pass
        return "step done"

    @property
    def last_outcome(self):
        if self._usage is None:
            return None
        return SimpleNamespace(usage=dict(self._usage))


# ---- A: /plan absorbs /plan-run ---------------------------------------------


class _TwoPhaseLoop:
    """First call plans (writes todos), later calls execute."""

    def __init__(self):
        self.calls: list[str] = []
        self.settings = SimpleNamespace(
            plan_step_max_iterations=8, plan_max_step_turns=20, stop_policy="natural"
        )
        self.policy = TurnPolicy(TurnPolicyConfig(max_iterations=100))

    async def process_input(self, session, text, on_delta=None, observer=None):
        self.calls.append(text)
        if "implementation plan" in text:
            session.task = TaskState(goal="test plan")
            session.task.add("step one")
            session.task.add("step two")
            return "plan written"
        for item in session.task.open_items():
            if not item.is_terminal():
                item.status = "completed"
                break
        return "step done"


@pytest.mark.asyncio
async def test_plan_command_plans_and_executes_in_one_command():
    """'/plan <topic>' must not stop at the plan — it executes it (M1-A)."""
    from rich.console import Console

    from ai_coding.ui.console import DialogueConsole
    from ai_coding.ui.repl import REPLAction, handle_input

    class _Sessions:
        def __init__(self, session):
            self.items = [session]

        def save(self, session):
            return None

    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    loop = _TwoPhaseLoop()
    action, _ = await handle_input(
        loop, "/plan 给 tasks 加 show", session, _Sessions(session),
        DialogueConsole(Console(record=True, width=80)),
    )

    assert action is REPLAction.CONTINUE
    assert any("implementation plan" in c for c in loop.calls)      # phase 1
    assert sum(STEP_HEAD in c for c in loop.calls) == 2            # phase 2
    assert session.task.done_count == 2


@pytest.mark.asyncio
async def test_plan_run_is_no_longer_a_command():
    from ai_coding.ui.slash import parse_slash

    assert parse_slash("/plan-run") is None
    assert parse_slash("/plan-run 继续") is None


# ---- B: per-step short conversations ----------------------------------------


@pytest.mark.asyncio
async def test_each_step_runs_in_a_fresh_session():
    """The step must not inherit the parent's history (M1-B)."""
    session = _session_with("one", "two", "three")
    loop = _StubLoop(["complete", "complete", "complete"])
    await PlanRunner(loop).run(session)

    parent_id = session.session_id
    assert len(loop.step_sessions) == 3
    assert all(s.session_id != parent_id for s in loop.step_sessions)
    assert len({s.session_id for s in loop.step_sessions}) == 3


@pytest.mark.asyncio
async def test_parent_messages_exclude_step_transcripts():
    """Only the step prompt and a summary reach the parent — not the seed."""
    session = _session_with("one", "two")
    loop = _StubLoop(["complete", "complete"])
    await PlanRunner(loop).run(session)

    parent_text = "\n".join(m.content or "" for m in session.messages)
    assert parent_text.count(STEP_HEAD) == 2       # both prompts are there
    assert "one" in parent_text and "two" in parent_text
    # the seed (goal + plan) stays inside the step's own short conversation
    assert "[计划目标]" not in parent_text
    # linear: 2 steps -> 2 prompts + 2 summaries, nothing else
    assert len(session.messages) == 4


@pytest.mark.asyncio
async def test_step_seed_carries_goal_and_plan_state():
    """The seed is the current plan state, not a growing transcript."""
    session = _session_with("one", "two", "three")
    loop = _StubLoop(["complete", "complete", "complete"])
    await PlanRunner(loop).run(session)

    # step 2's seed knows the goal and the plan; step 1 is gone from the open
    # list because the task's own statuses already say it is done.
    seed_2 = loop.step_sessions[1].messages[0].content
    assert "plan" in seed_2
    assert "two" in seed_2 and "three" in seed_2
    assert "one" not in seed_2
    # constant size: step 3's seed is not larger than step 2's
    assert len(loop.step_sessions[2].messages[0].content) <= len(seed_2) + 50


@pytest.mark.asyncio
async def test_seed_lists_only_open_items():
    """Completed items are dropped from the seed (Hermes rule: they make the
    model redo finished work)."""
    session = _session_with("one", "two", "three")
    loop = _StubLoop(["complete", "complete", "complete"])
    await PlanRunner(loop).run(session)

    seed_3 = loop.step_sessions[2].messages[0].content
    assert "three" in seed_3            # still open
    assert "[completed]" not in seed_3  # done items are not re-listed


@pytest.mark.asyncio
async def test_step_policy_closes_the_todo_gate():
    """A step must not be nudged about the plan's other open items."""
    session = _session_with("one", "two")
    loop = _StubLoop(["complete", "complete"])
    await PlanRunner(loop).run(session)

    for call_text, _ in loop.calls:
        assert "[task-gate]" not in call_text


@pytest.mark.asyncio
async def test_receipts_still_land_on_the_parent():
    session = _session_with("one", "two")
    loop = _StubLoop(["complete", "complete"])
    report = await PlanRunner(loop).run(session)

    assert len(session.step_deliveries) == 2
    assert all(d.attested is True for d in report.deliveries)
    assert report.completed == ["1", "2"]


@pytest.mark.asyncio
async def test_step_prompt_is_in_the_parent_for_replay():
    """The replay matches step headers by prompt marker on user bubbles."""
    session = _session_with("one", "two")
    await PlanRunner(_StubLoop(["complete", "complete"])).run(session)

    user_bubbles = [m.content for m in session.messages if m.role == "user"]
    assert sum(STEP_HEAD in c for c in user_bubbles) == 2


@pytest.mark.asyncio
async def test_step_session_shares_the_plan_task():
    """A step marking its item done must move the plan, not a copy."""
    session = _session_with("one", "two")
    loop = _StubLoop(["complete", "complete"])
    await PlanRunner(loop).run(session)

    assert [i.status for i in session.task.items] == ["completed", "completed"]


@pytest.mark.asyncio
async def test_prompt_growth_is_linear_not_quadratic():
    """The token bug, measured: 6 steps must not cost 4x 3 steps."""
    small = _session_with("a", "b", "c")
    small_loop = _StubLoop(["complete"] * 3)
    await PlanRunner(small_loop).run(small)

    large = _session_with(*[f"s{n}" for n in range(6)])
    large_loop = _StubLoop(["complete"] * 6)
    await PlanRunner(large_loop).run(large)

    small_total = sum(small_loop.prompt_chars)
    large_total = sum(large_loop.prompt_chars)
    ratio = large_total / max(small_total, 1)
    # quadratic would be ~4.0; linear is ~2.0.  Allow slack for the seed.
    assert ratio < 3.0, f"prompt growth ratio {ratio:.2f} is not linear"


@pytest.mark.asyncio
async def test_planning_failure_does_not_execute():
    """No todos from the plan turn -> no execution, a clear message."""
    from rich.console import Console

    from ai_coding.ui.console import DialogueConsole
    from ai_coding.ui.repl import handle_input

    class _EmptyLoop:
        def __init__(self):
            self.calls: list[str] = []
            self.settings = SimpleNamespace(
                plan_step_max_iterations=8, plan_max_step_turns=20, stop_policy="natural"
            )
            self.policy = TurnPolicy(TurnPolicyConfig(max_iterations=100))

        async def process_input(self, session, text, on_delta=None, observer=None):
            self.calls.append(text)
            return "nothing to plan"

    class _Sessions:
        def __init__(self, session):
            self.items = [session]

        def save(self, session):
            return None

    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    loop = _EmptyLoop()
    c = Console(record=True, width=80)
    await handle_input(loop, "/plan 没事做", session, _Sessions(session), DialogueConsole(c))

    assert len(loop.calls) == 1  # only the planning turn
    assert "完成 0 步" in c.export_text()


@pytest.mark.asyncio
async def test_seed_chars_recorded_on_the_receipt():
    session = _session_with("one")
    loop = _StubLoop(["complete"])
    report = await PlanRunner(loop).run(session)

    assert report.deliveries[0].seed_chars > 0
    assert report.deliveries[0].seed_chars == len(
        loop.step_sessions[0].messages[0].content
    )


@pytest.mark.asyncio
async def test_usage_still_summed_across_steps():
    usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
    session = _session_with("one", "two")
    loop = _StubLoop(["complete", "complete"], usage=usage)
    report = await PlanRunner(loop).run(session)

    assert report.turns_used == 2
    assert report.tokens_used == {
        "prompt_tokens": 200, "completion_tokens": 40, "total_tokens": 240,
    }


@pytest.mark.asyncio
async def test_step_sessions_are_ephemeral(tmp_path):
    """H1 audit fix: step sessions must not pollute the sessions directory.

    They run on the main loop, whose TurnCloser persists every turn — so a
    plan run used to leave one "plan step N" file per executed step, and the
    /resume picker listed them all.  Ephemeral sessions are skipped at the
    single save choke point.
    """
    from ai_coding.config.models import AIConfig
    from ai_coding.core.agent_loop import AgentLoop
    from ai_coding.domain.loop import StepResult
    from ai_coding.service.session_service import SessionService
    from ai_coding.tools.registry import ToolRegistry
    from ai_coding.tools.todo_write_tool import TodoWriteTool

    class _TextEngine:
        async def step(self, messages, *, tools, instructions=None, on_delta=None):
            return StepResult(text="done", finish_reason="stop")

        async def aclose(self):
            return None

    sessions = SessionService(root=tmp_path / "sessions")
    registry = ToolRegistry()
    registry.register(TodoWriteTool())
    loop = AgentLoop(
        _TextEngine(), sessions, registry,
        settings=AIConfig(stop_policy="natural", max_tool_iterations=4,
                          plan_step_max_iterations=2),
        workspace=str(tmp_path / "ws"),
    )
    session = sessions.create(title="main")
    task = TaskState(goal="g")
    task.add("one")
    session.task = task

    await loop.process_input(session, "go")   # a normal turn persists the parent
    await PlanRunner(loop).run(session)

    files = [p.name for p in (tmp_path / "sessions").glob("*.json")]
    assert [name for name in files if not name.startswith("plan-")]
    assert [name for name in files if name.startswith("plan-")] == []
