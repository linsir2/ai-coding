"""M1 (TDD): /plan absorbs /plan-run + per-step short conversations.

Two changes, one milestone:

A. ``/plan <topic>`` plans AND executes in one command.  ``/plan-run`` is
   deleted — the two-command handoff let the plan go stale between commands
   (measured: a run that executed 0 turns against a list from a prior turn).

B. Each plan step runs in a FRESH session seeded with goal + plan + prior
   step summaries, instead of resending the parent's whole history.  The
   parent session only receives the step prompt (replay marker) and a one-line
   summary.  Measured before: 4 turns -> 41k/82k/130k/254k prompt tokens,
   because each turn resent the earlier turns.  After: each step reads only
   the goal and the plan's open items (the seed does still grow with the
   number of open items, so the total over a plan stays quadratic — with a
   constant ~100x smaller).  See ``test_no_step_reads_an_earlier_step_s_output``.

Borrowed mechanisms (see docs): subagent's fresh-SessionData pattern
(core/subagent.py:_run_in), Hermes's structured handoff schema, opencode's
anchored iterative merge, Codex's goal persistence, Hermes's "only pending
items" rule.
"""

from types import SimpleNamespace

import pytest

from ai_coding.core.plan_runner import STEP_HEAD, PlanRunner
from ai_coding.core.turn_policy import TurnPolicy, TurnPolicyConfig
from ai_coding.domain.loop import TurnStatus
from ai_coding.domain.message import ChatMessage
from ai_coding.domain.session import SessionData
from ai_coding.domain.stance import PostureResult
from ai_coding.domain.task import TaskState
from ai_coding.infra.time_utils import parse_iso8601

#: Unique per call so a test can ask "did this step read an earlier step?".
_TOOL_MARK = "tool-output-%d "


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

    async def process_input(self, session, text, on_delta=None, observer=None, **kwargs):
        session.turn_count += 1
        self.calls.append((text, session.session_id))
        self.step_sessions.append(session)
        self.prompt_chars.append(sum(len(m.content or "") for m in session.messages))
        # Simulate the real loop: each turn leaves a tool result behind.  The
        # marker makes it detectable if a later step ever reads an earlier
        # step's output — which is the bug M1 exists to prevent.
        session.messages.append(
            ChatMessage(role="tool", content=_TOOL_MARK % len(self.calls) + "x" * 5000,
                        timestamp="", tool_call_id=f"call-{len(self.calls)}")
        )
        answer = "step done"
        if on_delta is not None:
            on_delta(answer)      # the real loop streams its answer as it comes
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
        return answer

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

    def request_posture(
        self, session, *, agent=None, readonly=None, reason="", observer=None
    ):
        """18 §10.1: the immediate half of AgentLoop.request_posture."""
        self.commit_posture(
            session, agent=agent, readonly=readonly, reason=reason, observer=observer
        )
        return PostureResult.COMMITTED

    def commit_posture(
        self, session, *, agent=None, readonly=None, reason="", observer=None
    ):
        session.agent = agent if agent is not None else session.agent
        session.readonly = bool(readonly) if readonly is not None else session.readonly
        session.pending_posture = None

    async def process_input(self, session, text, on_delta=None, observer=None, **kwargs):
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
async def test_plan_command_plans_and_plan_run_executes():
    """18 §10.1: '/plan <topic>' only plans; '/plan run' executes the plan."""
    from rich.console import Console

    from ai_coding.app.commands import REPLAction, handle_input
    from ai_coding.app.console import DialogueConsole

    class _Sessions:
        def __init__(self):
            self.saved = []

        def save(self, session):
            self.saved.append(session)

        def mark_pending_approval(self, session_id, request) -> None:
            return None

    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    loop = _TwoPhaseLoop()
    sessions = _Sessions()
    rich_console = Console(record=True, width=80)
    console = DialogueConsole(rich_console)

    action, _ = await handle_input(
        loop, "/plan 给 tasks 加 show", session, sessions, console
    )

    assert action is REPLAction.CONTINUE
    assert any("implementation plan" in c for c in loop.calls)      # planned
    assert sum(STEP_HEAD in c for c in loop.calls) == 0             # not executed
    assert session.task.done_count == 0
    assert (session.mode, session.readonly) == ("plan", True)
    assert sessions.saved == [session]
    # 19 §3.5: no command-layer "ready" claim anymore.
    assert "计划就绪" not in rich_console.export_text()

    await handle_input(loop, "/plan run", session, sessions, console)

    assert sum(STEP_HEAD in c for c in loop.calls) == 2             # both steps ran
    assert session.task.done_count == 2
    assert (session.mode, session.readonly) == ("normal", False)


@pytest.mark.asyncio
async def test_plan_run_is_no_longer_a_command():
    from ai_coding.app.slash import parse_slash

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
    assert "[计划标题]" not in parent_text
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
    # R21 §3: the seed says "title", not "goal" -- ``plan.updated`` also has a
    # ``goal`` key, and "目标" sent steps looking for a running goal.
    assert "[计划标题]" in seed_2
    assert "[计划目标]" not in seed_2
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
async def test_no_step_reads_an_earlier_step_s_output():
    """The M1 claim, pinned structurally instead of by a growth ratio.

    What M1 buys: a step's context is built from the goal and the plan's *open*
    items, never from what earlier steps produced.  The old test compared total
    prompt growth for a 3- and a 6-step plan and needed a magic threshold to
    separate quadratic from linear — it passed at 2.963 against a 3.0 limit and
    failed at 7 steps, so it was really pinning the quadratic it claimed to
    rule out.

    Two channels can leak, and both are asserted with no threshold at all:

    1. the sessions themselves are shared, so an earlier step's tool output is
       in view (the pre-M1 shape);
    2. the seed grows a "已完成步骤" block, so an earlier step's *statement* is
       in view — also pre-M1, and the reason this test was needed: channel 1
       alone stayed green when channel 2 came back.
    """
    contents = [f"step {n}" for n in range(4)]
    session = _session_with(*contents)
    loop = _StubLoop(["complete"] * 4)
    await PlanRunner(loop).run(session)
    assert len(loop.step_sessions) == 4

    for index, step in enumerate(loop.step_sessions):
        blob = "\n".join(m.content or "" for m in step.messages)
        finished = [call for call in range(1, index + 1)
                    if (_TOOL_MARK % call) in blob]
        assert not finished, f"step {index + 1} reads earlier tool output: {finished}"
        # the seed lists open items only, so a finished step's statement is a leak
        stated = [content for content in contents[:index] if content in blob]
        assert not stated, f"step {index + 1} reads finished steps' statements: {stated}"


@pytest.mark.asyncio
async def test_planning_failure_does_not_execute():
    """No todos from the plan turn -> no execution and no 'ready' claim; the
    refusal lives in present_plan's tool result now (19 §3.5)."""
    from rich.console import Console

    from ai_coding.app.commands import handle_input
    from ai_coding.app.console import DialogueConsole

    class _EmptyLoop:
        def __init__(self):
            self.calls: list[str] = []
            self.settings = SimpleNamespace(
                plan_step_max_iterations=8, plan_max_step_turns=20, stop_policy="natural"
            )
            self.policy = TurnPolicy(TurnPolicyConfig(max_iterations=100))

        def request_posture(
            self, session, *, agent=None, readonly=None, reason="", observer=None
        ):
            """18 §10.1: the immediate half of AgentLoop.request_posture."""
            self.commit_posture(
                session, agent=agent, readonly=readonly, reason=reason, observer=observer
            )
            return PostureResult.COMMITTED

        def commit_posture(
            self, session, *, agent=None, readonly=None, reason="", observer=None
        ):
            session.agent = agent if agent is not None else session.agent
            session.readonly = (
                bool(readonly) if readonly is not None else session.readonly
            )
            session.pending_posture = None

        async def process_input(self, session, text, on_delta=None, observer=None, **kwargs):
            self.calls.append(text)
            return "nothing to plan"

    class _Sessions:
        def __init__(self, session):
            self.items = [session]

        def save(self, session):
            return None

        def mark_pending_approval(self, session_id, request) -> None:
            return None

    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    loop = _EmptyLoop()
    c = Console(record=True, width=80)
    await handle_input(loop, "/plan 没事做", session, _Sessions(session), DialogueConsole(c))

    assert len(loop.calls) == 1  # only the planning turn
    # 18 §10.1: execution now lives behind /plan run; an empty plan says so.
    assert "计划就绪" not in c.export_text()


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


def _real_loop(tmp_path, *, trace=None, engine=None, **overrides):
    """A real AgentLoop on a real SessionService, with a text-only engine."""
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
    settings = {"stop_policy": "natural", "max_tool_iterations": 4,
                "plan_step_max_iterations": 2}
    settings.update(overrides)
    loop = AgentLoop(engine or _TextEngine(), sessions, registry,
                     settings=AIConfig(**settings),
                     workspace=str(tmp_path / "ws"), trace=trace)
    return loop, sessions


# ---- 18 §10.2: posture flips queue while a turn is running -------------------


@pytest.mark.asyncio
async def test_an_immediate_flip_supersedes_the_same_key_already_queued(tmp_path):
    """A stale queued value must not resurrect at the next drain and undo a
    newer immediate command (18 §10.2)."""
    loop, _sessions = _real_loop(tmp_path)
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    session.pending_posture = {"readonly": True}

    applied = loop.request_posture(session, readonly=False)

    # M1b: asking for the state we are already in, while a different flip is
    # queued, withdraws the queued flip — that is CANCELLED, and the effect
    # the test is about (the stale value cannot resurrect) is the same.
    assert applied is PostureResult.CANCELLED
    assert session.readonly is False
    assert session.pending_posture is None
    await loop.aclose()


@pytest.mark.asyncio
async def test_a_leftover_pending_flip_lands_at_the_next_turn(tmp_path):
    """The harvest-window fallback: a flip queued after the close ran still
    lands at the next turn's entry (18 §10.2)."""
    loop, _sessions = _real_loop(tmp_path)
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    session.pending_posture = {"agent": "plan", "readonly": True, "reason": "test"}

    await loop.process_input(session, "go")

    assert session.mode == "plan"
    assert session.pending_posture is None
    await loop.aclose()


@pytest.mark.asyncio
async def test_a_flip_queued_mid_turn_lands_at_close_and_reads_back(tmp_path):
    """The real path (18 §10.2): while a turn holds the lock, request_posture
    queues instead of writing; the turn's own close applies the flip and the
    close's save persists it — verified by loading the file back."""
    import asyncio

    from ai_coding.domain.loop import StepResult

    class _BlockingEngine:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def step(self, messages, *, tools, instructions=None, on_delta=None):
            self.started.set()
            await self.release.wait()
            return StepResult(text="done", finish_reason="stop")

        async def aclose(self):
            return None

    engine = _BlockingEngine()
    loop, sessions = _real_loop(tmp_path, engine=engine)
    session = sessions.create()

    turn = asyncio.create_task(loop.process_input(session, "go"))
    await asyncio.wait_for(engine.started.wait(), timeout=5)

    applied = loop.request_posture(session, agent="plan", readonly=True)
    assert applied is PostureResult.QUEUED
    assert (session.mode, session.readonly) == ("normal", False)
    assert session.pending_posture == {
        "agent": "plan",
        "readonly": True,
        "reason": "",
    }

    engine.release.set()
    await asyncio.wait_for(turn, timeout=5)

    assert (session.mode, session.readonly) == ("plan", True)
    loaded = sessions.load(session.session_id)
    assert (loaded.mode, loaded.readonly) == ("plan", True)
    await loop.aclose()


@pytest.mark.asyncio
async def test_a_command_flip_queued_mid_turn_lands_at_close_and_reads_back(tmp_path):
    """The full chain of 18 §10.2: handle_input → request_posture (queued,
    receipt says so) → the turn's close applies it → the file reads back
    flipped.  Mechanism and command layers, wired for real."""
    import asyncio

    from rich.console import Console

    from ai_coding.app.commands import handle_input
    from ai_coding.app.console import DialogueConsole
    from ai_coding.domain.loop import StepResult

    class _BlockingEngine:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def step(self, messages, *, tools, instructions=None, on_delta=None):
            self.started.set()
            await self.release.wait()
            return StepResult(text="done", finish_reason="stop")

        async def aclose(self):
            return None

    engine = _BlockingEngine()
    loop, sessions = _real_loop(tmp_path, engine=engine)
    session = sessions.create()
    rc = Console(record=True, width=80)
    console = DialogueConsole(rc)

    turn = asyncio.create_task(loop.process_input(session, "go"))
    await asyncio.wait_for(engine.started.wait(), timeout=5)
    try:
        await handle_input(loop, "/plan", session, sessions, console)

        assert session.pending_posture == {
            "agent": "plan",
            "readonly": True,
            "reason": "/plan",
        }
        assert (session.mode, session.readonly) == ("normal", False)
        assert "将在当前回合结束后生效" in rc.export_text()
    finally:
        engine.release.set()
        await asyncio.wait_for(turn, timeout=5)

    assert (session.mode, session.readonly) == ("plan", True)
    loaded = sessions.load(session.session_id)
    assert (loaded.mode, loaded.readonly) == ("plan", True)
    await loop.aclose()


@pytest.mark.asyncio
async def test_step_sessions_are_ephemeral(tmp_path):
    """H1 audit fix: step sessions must not pollute the sessions directory.

    They run on the main loop, whose TurnCloser persists every turn — so a
    plan run used to leave one "plan step N" file per executed step, and the
    /resume picker listed them all.  Ephemeral sessions are skipped at the
    single save choke point.
    """
    loop, sessions = _real_loop(tmp_path)
    session = sessions.create(title="main")
    task = TaskState(goal="g")
    task.add("one")
    session.task = task

    await loop.process_input(session, "go")   # a normal turn persists the parent
    await PlanRunner(loop).run(session)

    files = [p.name for p in (tmp_path / "sessions").glob("*.json")]
    assert [name for name in files if not name.startswith("plan-")]
    assert [name for name in files if name.startswith("plan-")] == []


@pytest.mark.asyncio
async def test_run_persists_the_parent_session(tmp_path):
    """Review fix: the run's own results must reach disk before the user quits.

    Steps run on ephemeral sessions (H1), so no turn inside the run saves the
    parent any more.  Measured before this fix: right after a run the on-disk
    parent still had 0 receipts and every item pending — the run's work only
    reached disk if the user happened to send another message.
    """
    import json

    loop, sessions = _real_loop(tmp_path)
    session = sessions.create(title="main")
    task = TaskState(goal="g")
    task.add("one")
    task.add("two")
    session.task = task

    await loop.process_input(session, "go")   # a normal turn persists the parent
    before = len(session.messages)
    report = await PlanRunner(loop).run(session)
    assert report.deliveries, "the run settled nothing — nothing to persist"

    saved = json.loads(
        (tmp_path / "sessions" / f"{session.session_id}.json").read_text()
    )
    assert [d["stepId"] for d in saved["stepDeliveries"]] == [
        d.step_id for d in report.deliveries
    ]
    # and the per-step bubbles the runner appended to the parent: one prompt
    # (the replay marker) plus one summary line per settled step.
    assert len(session.messages) - before == 2 * len(report.deliveries)
    assert len(saved["messages"]) == len(session.messages)


@pytest.mark.asyncio
async def test_step_turns_trace_under_the_parent_session(tmp_path):
    """An ephemeral step must not take the trace's session id with it.

    Steps run on throwaway sessions, and the trace follows whatever session
    the current turn runs on (``_bind_trace``).  So a step used to repoint
    the recorder at ``plan-<hex>``: MCP events fired during a step wrote
    ``traces/plan-<hex>/…``, the step's token spend landed in a summary the
    parent never shows, and the pointer stayed on the dead id after the run
    — the next event of any kind opened a fresh directory for it.

    Nothing writes today only because the Textual app hands the recorder no
    loop events (``AgentLoop(observer=None, trace=recorder)``) and only MCP
    events reach it; ``cli._compose_observer``, the code that would close
    that gap, has no callers.  So the test feeds the recorder an event by
    hand — the directory is created by ``session_id`` alone.
    """
    from ai_coding.core.events import IterationStarted
    from ai_coding.core.trace import TraceRecorder, TraceStore

    store = TraceStore(tmp_path / "traces", workspace=tmp_path)
    recorder = TraceRecorder(store, session_id="")
    loop, sessions = _real_loop(tmp_path, trace=recorder)
    session = sessions.create(title="main")
    task = TaskState(goal="g")
    task.add("one")
    task.add("two")
    session.task = task

    await loop.process_input(session, "go")
    recorder.session_id = session.session_id      # what a normal turn binds
    await PlanRunner(loop).run(session)

    assert recorder.session_id == session.session_id
    recorder.on_event(IterationStarted(index=0, max_iterations=2))
    written = sorted(p.name for p in (tmp_path / "traces").rglob("turn-*.jsonl"))
    assert written, "the recorder wrote nothing — the event never landed"
    assert not list((tmp_path / "traces").glob("plan-*"))


@pytest.mark.asyncio
async def test_a_replaced_list_does_not_inherit_the_previous_plan_s_receipts():
    """Step ids restart at 1 for every list, so old receipts must be dropped.

    ``/plan <topic>`` resets goal, items and receipts together
    (``SessionData.begin_plan``).  The path nobody covered is the list being
    replaced mid-turn — ``present_plan`` approval replaces it now, and
    sessions written before ``todo_write(clear)`` was removed could do the
    same — which restarts ids at 1 while the previous plan's receipts stay in
    the session.  Then step id "1" is ambiguous: the runner's ``receipted`` set
    matched on the id alone, so the new plan's step 1 was treated as already
    accounted for and its out-of-band completion was never receipted.

    Measured before this fix: receipts for ids 1 and 2 from plan A coexisted
    with plan B's, and B's completed-out-of-band step produced nothing.
    """
    from ai_coding.domain.plan_delivery import StepDelivery

    session = _session_with("A 的第一步", "A 的第二步")
    session.step_deliveries = [
        StepDelivery(step_id="1", statement="A 的第一步", status="completed",
                     ac=[], summary="A1"),
        StepDelivery(step_id="2", statement="A 的第二步", status="completed",
                     ac=[], summary="A2"),
    ]
    # the list was replaced and a different plan written; ids restart (the
    # session was loaded from disk, or written before todo_write(clear) was
    # removed -- either way the data shape is a fresh list)
    session.task = TaskState(goal="B 的目标")
    out_of_band = session.task.add("B 的第一步")
    session.task.add("B 的第二步")
    out_of_band.status = "completed"   # marked done outside any step turn

    report = await PlanRunner(_StubLoop(["complete"])).run(session)

    # B's out-of-band completion gets its own receipt…
    statements = [(entry.step_id, entry.statement) for entry in report.deliveries]
    assert ("1", "B 的第一步") in statements, f"no receipt for the new plan: {statements}"
    # …and A's receipts are gone from the book, so no id names two plans
    assert [(e.step_id, e.statement) for e in session.step_deliveries] == statements
    assert len({e.step_id for e in session.step_deliveries}) == len(
        session.step_deliveries
    )


@pytest.mark.asyncio
async def test_parent_step_bubbles_carry_real_timestamps():
    """The parent's per-step bubbles are persisted messages, so they get stamps.

    The runner wrote them with timestamp="", which is not a formatting detail:
    a non-ISO-8601 stamp shows an empty clock wherever it is replayed, and the
    OTLP exporter's ``_nano()`` returned 0 for them.  Nothing sorts or trims by
    timestamp (checked across core and domain), so this is display and export
    only — but a persisted message with no time is a lie about when the words
    were said.

    Asserted by parsing the stamp as ISO-8601 — the format every consumer
    (replay, export, OTLP) assumes — rather than against a literal string.
    """
    session = _session_with("one")
    await PlanRunner(_StubLoop(["complete"])).run(session)

    bubbles = session.messages[-2:]
    assert [m.role for m in bubbles] == ["user", "assistant"]
    for message in bubbles:
        stamp = message.timestamp
        assert stamp, "a persisted message with no timestamp"
        # ``parse_iso8601``, not ``datetime.fromisoformat``: the ``Z`` designator is
        # only understood from 3.11, and 3.10 is this project's declared floor, so the
        # bare call failed on 3.10 only (docs/plan-ci-fix.md section 3).
        assert parse_iso8601(stamp) is not None, "not a parseable ISO-8601 stamp"


@pytest.mark.asyncio
async def test_step_contracts_never_touch_the_parent_acceptance_list():
    """A step's verify contracts belong to the step session, and only there.

    The runner used to snapshot the parent's acceptance list and restore it in
    a finally — dead work since the contracts moved onto the step session
    (M1-B): nothing writes the parent's list during a step any more, so the
    save/restore could only ever restore what it had copied.  The behaviour
    worth keeping is the one the dead code implied: the parent's own
    acceptance list is not the step's scratch space.
    """
    from ai_coding.domain.acceptance import AcceptanceItem

    session = _session_with("one", verifies=["pytest -q"])
    session.acceptance = [AcceptanceItem(id="parent-1", statement="mine",
                                         verify="true")]

    await PlanRunner(_StubLoop(["verify-pass", "complete"])).run(session)

    assert [item.id for item in session.acceptance] == ["parent-1"]
    assert session.acceptance[0].passed is None
    # and the step's verdicts landed on its receipt, where replay can see them
    assert session.step_deliveries[0].ac == [
        {"id": "plan-1", "passed": True}
    ]


class _StepViewConsole:
    """A console that can open a per-step view (M3 seam)."""

    def __init__(self):
        self.messages = []
        self.opened = []      # (title, sink)
        self.ended = []       # (title, summary)

    def info(self, message):
        self.messages.append(message)

    def error(self, message):
        self.messages.append(message)

    def step(self, title):
        sink = _StepSink(title, self)
        self.opened.append((title, sink))
        return sink


class _StepSink:
    def __init__(self, title, console):
        self.title, self.console, self.deltas = title, console, []

    def delta(self, text):
        self.deltas.append(text)

    def end(self, summary=None):
        self.console.ended.append((self.title, summary))


@pytest.mark.asyncio
async def test_each_step_streams_into_its_own_view():
    """M3: the step's live output must reach the user, per step.

    The runner already threaded ``on_delta`` into every step turn (it was
    written for the planning turn and never wired up), but neither UI passed
    one for the run, so a plan executed in front of the user as a sequence of
    "[plan] 步骤 k/N" lines and nothing else — the model wrote a paragraph,
    ran three commands, and none of it was visible.  Steps run on throwaway
    sessions whose transcript is discarded on purpose, so this is the only
    place the work can be shown.

    The runner's job is to open a view per step, route that step's deltas into
    it, and close it with the settled summary — so the live title says what
    replay will say.
    """
    session = _session_with("改 loader", "写测试")
    seen_outer = []
    console = _StepViewConsole()

    async def outer_delta(text):        # the caller-level sink, must stay unused
        seen_outer.append(text)

    loop = _StubLoop(["complete", "complete"])
    await PlanRunner(loop).run(session, console, on_delta=outer_delta)

    assert [title for title, _ in console.opened] == [
        "[plan] 步骤 1/2：改 loader", "[plan] 步骤 2/2：写测试",
    ]
    for _title, sink in console.opened:
        assert sink.deltas, "the step's own deltas never arrived"
    assert seen_outer == [], "step output leaked into the caller's stream"
    # closed with the receipt summary, which is what /resume will show
    assert [summary for _title, summary in console.ended] == [
        entry.summary for entry in session.step_deliveries
    ]


@pytest.mark.asyncio
async def test_a_console_without_step_support_still_runs():
    """The seam is optional: a console that cannot open a view is not an error."""
    session = _session_with("one")

    report = await PlanRunner(_StubLoop(["complete"])).run(session, None)

    assert [entry.status for entry in report.deliveries] == ["completed"]


# ---- 19 §3.4: executor resilience (budget continuation / stall skip / retry note) --


class _StatusLoop(_StubLoop):
    """_StubLoop plus the per-turn outcome status (the budget-death trigger).

    ``statuses`` is popped once per ``process_input`` call; ``None`` means a
    normal ending.  The real loop reports ``TurnStatus`` on ``last_outcome``;
    this mirrors that surface without a real turn.
    """

    def __init__(self, script, statuses):
        super().__init__(script)
        self._statuses = list(statuses)
        self._last_status = None

    async def process_input(self, session, text, on_delta=None, observer=None, **kwargs):
        self._last_status = self._statuses.pop(0) if self._statuses else None
        return await super().process_input(
            session, text, on_delta=on_delta, observer=observer
        )

    @property
    def last_outcome(self):
        return SimpleNamespace(usage=None, status=self._last_status)


@pytest.mark.asyncio
async def test_a_budget_dead_step_is_not_continued_by_the_runner():
    """19 §3.4-2, re-homed: the once-only continuation lives *inside* the step
    turn (the loop's C3, scoped to this item by ``step_item_id``, covered in
    ``test_auto_continue.py``).  The runner must not add a second mechanism:
    two of them meant 2 attempts x 2 rounds = 400 iterations for one step.
    """
    session = _session_with("read the big file")
    loop = _StatusLoop(["ignore"], [TurnStatus.MAX_ITERATIONS])

    report = await PlanRunner(loop).run(session)

    assert len(loop.calls) == 1, "the runner adds no continuation of its own"
    assert report.deliveries[0].attempts == 1
    assert report.turns_used == 1
    assert report.stalled == ["1"]


@pytest.mark.asyncio
async def test_a_budget_dead_step_that_never_finishes_settles_as_stalled():
    """Still open after its one turn: a retryable next run, no third turn."""
    session = _session_with("read the endless file")
    loop = _StatusLoop(["ignore"], [TurnStatus.MAX_ITERATIONS])

    report = await PlanRunner(loop).run(session)

    assert len(loop.calls) == 1
    assert report.stalled == ["1"]
    assert report.deliveries[0].attempts == 1


@pytest.mark.asyncio
async def test_a_verify_failure_still_takes_the_repair_path():
    """The repair branch wins: a budget-dead turn that also failed its proof
    gets the failure-targeted prompt, never both extra turns (19 §3.4-2)."""
    session = _session_with("fix it", verifies=["pytest tests/ -q"])
    loop = _StatusLoop(
        ["verify-fail", "verify-pass"],
        [TurnStatus.MAX_ITERATIONS, TurnStatus.COMPLETED],
    )

    report = await PlanRunner(loop).run(session)

    assert report.completed == ["1"]
    assert len(loop.calls) == 2
    assert "只修复失败点" in loop.calls[1][0]   # the repair prompt, not the continue one
    assert "从停下的地方继续" not in loop.calls[1][0]


@pytest.mark.asyncio
async def test_a_stalled_step_does_not_stop_the_run():
    """19 §3.4-3 (Q5=A): record the stall, keep going; the report lists it."""
    session = _session_with("write docs", "next step")
    loop = _StubLoop(["ignore", "complete-all"])

    report = await PlanRunner(loop).run(session)

    assert len(loop.calls) == 2                 # the next step ran anyway
    assert report.stalled == ["1"]
    assert report.completed == ["2"]
    assert report.stopped is None


@pytest.mark.asyncio
async def test_a_retry_seed_carries_the_previous_attempt():
    """19 §3.4-4: the second run's seed says what the first attempt reached."""
    session = _session_with("one")
    loop = _StubLoop(["ignore", "complete"])

    first = await PlanRunner(loop).run(session)
    assert first.stalled == ["1"]

    await PlanRunner(loop).run(session)

    seed = loop.step_sessions[-1].messages[0].content or ""
    assert "[本步历史] 此前已尝试 1 次" in seed
    assert "不要从头再来" in seed
