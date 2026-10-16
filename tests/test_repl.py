"""TDD (M5-2): REPL core — one-input handling, offline with injected fakes.

The REPL is a thin loop over :func:`handle_input`; here we verify the *decision*
logic (plain text → run the loop, slash → react) without touching the network.
Rich console output is captured via ``record=True``.
"""

from types import SimpleNamespace
from typing import Any

import pytest
from rich.console import Console

from ai_coding.app.commands import REPLAction, handle_input
from ai_coding.app.console import DialogueConsole
from ai_coding.core.turn_policy import TurnPolicy, TurnPolicyConfig

pytestmark = pytest.mark.asyncio


def _sd(sid: str):
    from ai_coding.domain.session import SessionData

    return SessionData(
        session_id=sid, title="", created_time="", last_access_time=""
    )


class _FakeLoop:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.reply = "assistant answer"
        self.busy = False
        # M1: the runner swaps the loop's policy per step, so a stub needs one.
        self.settings = SimpleNamespace(
            plan_step_max_iterations=8, plan_max_step_turns=20, stop_policy="natural"
        )
        self.policy = TurnPolicy(TurnPolicyConfig(max_iterations=100))

    @property
    def turn_running(self) -> bool:
        return self.busy

    def request_posture(self, session, *, mode=None, readonly=None):
        """18 §10.1: the immediate half of AgentLoop.request_posture."""
        if self.busy:
            pending = dict(session.pending_posture or {})
            if mode is not None:
                pending["mode"] = mode
            if readonly is not None:
                pending["readonly"] = bool(readonly)
            session.pending_posture = pending
            return False
        if mode is not None:
            session.mode = mode
        if readonly is not None:
            session.readonly = bool(readonly)
        return True

    async def process_input(self, session, text, on_delta=None, observer=None):
        self.calls.append((text, on_delta, observer))
        return self.reply


class _FakeSessions:
    def __init__(self) -> None:
        self.items = [_sd("s1"), _sd("s2")]

    def create(self, title: str = ""):
        s = _sd(f"new{len(self.items)}")
        self.items.append(s)
        return s

    def load(self, session_id):
        return next((s for s in self.items if s.session_id == session_id), None)

    def list_sessions(self):
        return list(self.items)

    def resolve(self, prefix):
        return [s for s in self.items if s.session_id.startswith(prefix)]


def _console_capture() -> tuple[DialogueConsole, Console]:
    c = Console(record=True, width=80)
    return DialogueConsole(c), c


def _session_text(c: Console) -> str:
    return c.export_text()


async def test_handle_plain_text_runs_loop():
    loop = _FakeLoop()
    sessions = _FakeSessions()
    dc, _ = _console_capture()

    action, session = await handle_input(
        loop, "tell me", sessions.items[0], sessions, dc
    )

    assert action is REPLAction.CONTINUE
    assert len(loop.calls) == 1
    assert loop.calls[0][0] == "tell me"
    assert callable(loop.calls[0][1])  # the rich delta sink was wired


async def test_handle_quit_does_not_run_loop():
    loop = _FakeLoop()
    sessions = _FakeSessions()
    dc, _ = _console_capture()

    action, session = await handle_input(loop, "/quit", sessions.items[0], sessions, dc)

    assert action is REPLAction.QUIT
    assert loop.calls == []


async def test_handle_clear_starts_new_session():
    loop = _FakeLoop()
    sessions = _FakeSessions()
    dc, _ = _console_capture()
    old = sessions.items[0]

    action, new_session = await handle_input(loop, "/clear", old, sessions, dc)

    assert action is REPLAction.NEW_SESSION
    assert new_session is not old
    assert loop.calls == []


async def test_handle_help_prints_and_continues():
    loop = _FakeLoop()
    sessions = _FakeSessions()
    dc, c = _console_capture()

    action, _ = await handle_input(loop, "/help", sessions.items[0], sessions, dc)

    assert action is REPLAction.CONTINUE
    assert loop.calls == []
    assert "quit" in _session_text(c)


async def test_handle_status_prints_session_info():
    loop = _FakeLoop()
    sessions = _FakeSessions()
    dc, c = _console_capture()
    session = sessions.items[0]
    from ai_coding.domain.message import ChatMessage

    session.add_message(ChatMessage(role="user", content="hi", timestamp=""))

    action, _ = await handle_input(loop, "/status", session, sessions, dc)

    assert action is REPLAction.CONTINUE
    assert loop.calls == []
    out = _session_text(c)
    assert "1" in out  # one message reported by /status


def _model_table(loop: Any) -> None:
    """Put the model catalogue on the loop, where it actually lives.

    It used to be installed on ``loop.settings``, which is an ``AIConfig`` --
    a type that has never carried a model table, so the shipped ``/model`` could
    only ever report "unknown model" while these tests passed.
    """
    loop.models = {"gpt-4": "GPT-4", "claude": "Claude"}
    loop.default_model = "gpt-4"


async def test_handle_model_lists_models():
    loop = _FakeLoop()
    _model_table(loop)
    sessions = _FakeSessions()
    dc, c = _console_capture()
    action, _ = await handle_input(loop, "/model", sessions.items[0], sessions, dc)
    assert action is REPLAction.CONTINUE
    out = _session_text(c)
    assert "gpt-4" in out and "claude" in out


async def test_handle_model_switches():
    loop = _FakeLoop()
    _model_table(loop)
    loop.set_model = lambda name: None
    sessions = _FakeSessions()
    dc, c = _console_capture()
    action, _ = await handle_input(loop, "/model claude", sessions.items[0], sessions, dc)
    assert action is REPLAction.CONTINUE
    out = _session_text(c)
    assert "claude" in out


async def test_handle_resume_lists_sessions():
    loop = _FakeLoop()
    sessions = _FakeSessions()
    dc, c = _console_capture()
    action, _ = await handle_input(loop, "/resume", sessions.items[0], sessions, dc)
    assert action is REPLAction.CONTINUE
    out = _session_text(c)
    assert "s1" in out or "s2" in out


async def test_handle_resume_loads_session():
    loop = _FakeLoop()
    sessions = _FakeSessions()
    target = sessions.items[0]
    dc, c = _console_capture()
    action, session = await handle_input(loop, "/resume s1", sessions.items[1], sessions, dc)
    assert action is REPLAction.NEW_SESSION
    assert session is target


async def test_handle_resume_shows_real_message_counts():
    """The list used to say msgs=0 for everything, because it counted the
    bodies it had just stripped."""
    loop = _FakeLoop()
    sessions = _FakeSessions()
    target = sessions.items[0]
    from ai_coding.domain.message import ChatMessage

    for i in range(3):
        target.add_message(ChatMessage(role="user", content=f"m{i}", timestamp=""))
    target.message_count = 3
    sessions.items[1].message_count = 0
    dc, c = _console_capture()

    await handle_input(loop, "/resume", sessions.items[1], sessions, dc)

    out = _session_text(c)
    assert "msgs=3" in out
    assert "msgs=0" in out


async def test_handle_resume_accepts_the_prefix_it_printed():
    """12 hex chars is what the list shows; that is what a user can type."""
    loop = _FakeLoop()
    sessions = _FakeSessions()
    target = _sd("abcdef0123456789fedcba9876543210")
    sessions.items.append(target)
    dc, c = _console_capture()

    action, session = await handle_input(
        loop, "/resume abcdef012345", sessions.items[1], sessions, dc
    )

    assert action is REPLAction.NEW_SESSION
    assert session is target
    assert loop.calls == []  # a resume must not also become a model turn


async def test_handle_resume_refuses_an_ambiguous_prefix():
    """Two matches must not resolve to whichever came first."""
    loop = _FakeLoop()
    sessions = _FakeSessions()
    sessions.items.append(_sd("aaaaaaaa1111"))
    sessions.items.append(_sd("aaaaaaaa2222"))
    dc, c = _console_capture()

    action, session = await handle_input(
        loop, "/resume aaaaaaaa", sessions.items[0], sessions, dc
    )

    assert action is REPLAction.CONTINUE
    assert session is sessions.items[0]
    assert loop.calls == []
    out = _session_text(c)
    assert "aaaaaaaa1111" in out and "aaaaaaaa2222" in out


async def test_handle_permissions_shows_current():
    approval = type("A", (), {"__repr__": lambda s: "CLIApprovalCallback"})()
    loop = _FakeLoop()
    loop.dispatcher = type("D", (), {"approval": approval})()
    sessions = _FakeSessions()
    dc, c = _console_capture()
    action, _ = await handle_input(loop, "/permissions", sessions.items[0], sessions, dc)
    assert action is REPLAction.CONTINUE
    out = _session_text(c)
    assert "CLIApprovalCallback" in out or "approval" in out.lower()


async def test_handle_todo_renders_task_state():
    from ai_coding.domain.task import TaskState, TodoItem
    loop = _FakeLoop()
    sessions = _FakeSessions()
    session = sessions.items[0]
    task = TaskState(goal="Build feature X")
    task.add(TodoItem(id="1", content="Write tests", status="pending"))
    task.add(TodoItem(id="2", content="Implement feature", status="in_progress"))
    session.task = task
    dc, c = _console_capture()
    action, _ = await handle_input(loop, "/todo", session, sessions, dc)
    assert action is REPLAction.CONTINUE
    out = _session_text(c)
    assert "Build feature X" in out
    assert "Write tests" in out


async def test_handle_todo_no_task():
    loop = _FakeLoop()
    sessions = _FakeSessions()
    dc, c = _console_capture()
    action, _ = await handle_input(loop, "/todo", sessions.items[0], sessions, dc)
    assert action is REPLAction.CONTINUE
    out = _session_text(c)
    assert "no task" in out.lower() or "empty" in out.lower() or "0" in out


async def test_handle_skills_lists_catalog(tmp_path):
    from ai_coding.skills.registry import Skill

    loop = _FakeLoop()
    skill = Skill(
        id="test-skill",
        body="b",
        path=tmp_path / "SKILL.md",
        description="A test skill",
    )
    loop.skills = type("SR", (), {"all": lambda self: (skill,)})()
    sessions = _FakeSessions()
    dc, c = _console_capture()
    action, _ = await handle_input(loop, "/skills", sessions.items[0], sessions, dc)
    assert action is REPLAction.CONTINUE
    out = _session_text(c)
    assert "test-skill" in out


async def test_bare_plan_enters_mode_without_sending_a_prompt():
    """18 §10.1: bare /plan flips the two switches — no topic ask, no turn.

    The old contract asked for a topic (M4-a) and then planned and executed
    it; the mode redesign replaced that with a posture flip you can follow
    with ordinary conversation.  (The ask path itself is gone from the
    console surface — 18-P5 deleted ``DialogueConsole.ask``.)
    """
    loop = _FakeLoop()
    sessions = _FakeSessions()
    dc, c = _console_capture()

    action, _ = await handle_input(loop, "/plan", sessions.items[0], sessions, dc)

    assert action is REPLAction.CONTINUE
    assert loop.calls == []
    assert (sessions.items[0].mode, sessions.items[0].readonly) == ("plan", True)
    assert "已进入计划模式" in _session_text(c)


async def test_handle_review_sends_prompt():
    loop = _FakeLoop()
    sessions = _FakeSessions()
    dc, _ = _console_capture()
    action, _ = await handle_input(loop, "/review", sessions.items[0], sessions, dc)
    assert action is REPLAction.CONTINUE
    assert len(loop.calls) == 1
    assert "review" in loop.calls[0][0].lower() or "审查" in loop.calls[0][0].lower()


async def test_handle_config_shows_settings():
    loop = _FakeLoop()
    loop.default_model = "gpt-4"
    loop.settings = type("S", (), {
        "max_context_tokens": 48000,
        "max_tool_iterations": 10,
        "stop_policy": "todo_aware",
    })()
    sessions = _FakeSessions()
    dc, c = _console_capture()
    action, _ = await handle_input(loop, "/config", sessions.items[0], sessions, dc)
    assert action is REPLAction.CONTINUE
    out = _session_text(c)
    assert "gpt-4" in out
    assert "48000" in out


async def test_help_includes_new_commands():
    loop = _FakeLoop()
    sessions = _FakeSessions()
    dc, c = _console_capture()
    action, _ = await handle_input(loop, "/help", sessions.items[0], sessions, dc)
    assert action is REPLAction.CONTINUE
    out = _session_text(c)
    expected = (
        "/model", "/resume", "/permissions", "/todo",
        "/skills", "/plan", "/review", "/config",
    )
    for cmd in expected:
        assert cmd in out


class _FakeTrace:
    def __init__(self, store):
        self.store = store
        self.current_path = None


class _BudgetLoop:
    def __init__(self, store):
        self.trace = _FakeTrace(store)


class _NoTraceLoop:
    pass


def _session(sid):
    return type("S", (), {"session_id": sid})()


def test_budget_says_when_no_turn_has_run(tmp_path):
    from ai_coding.app.commands import _budget_report
    from ai_coding.core.trace import TraceStore

    loop = _BudgetLoop(TraceStore(tmp_path))
    out = _budget_report(loop, _session("never-ran"))
    assert "还没跑过回合" in out


def test_budget_says_when_zero_tokens(tmp_path):
    from ai_coding.app.commands import _budget_report
    from ai_coding.core.trace import TraceStore

    store = TraceStore(tmp_path)
    store.update_summary(
        "zero",
        {"turns": 2, "steps": 5, "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0},
    )
    out = _budget_report(_BudgetLoop(store), _session("zero"))
    assert "尚无 token 用量" in out


def test_budget_renders_real_usage(tmp_path):
    from ai_coding.app.commands import _budget_report
    from ai_coding.core.trace import TraceStore

    store = TraceStore(tmp_path)
    store.update_summary(
        "good",
        {
            "turns": 3,
            "steps": 9,
            "prompt_tokens": 30585,
            "completion_tokens": 798,
            "cached_tokens": 12288,
        },
    )
    out = _budget_report(_BudgetLoop(store), _session("good"))
    assert "3 turns" in out and "31,383 tok" in out


def test_budget_says_when_tracing_is_off(tmp_path):
    from ai_coding.app.commands import _budget_report

    out = _budget_report(_NoTraceLoop(), _session("x"))
    assert "tracing is off" in out


async def test_unknown_close_command_gets_a_suggestion():
    """A typo must not become a model turn."""
    loop = _FakeLoop()
    sessions = _FakeSessions()
    dc, c = _console_capture()

    action, _ = await handle_input(loop, "/resum", sessions.items[0], sessions, dc)

    assert action is REPLAction.CONTINUE
    assert loop.calls == []  # not sent to the model
    out = _session_text(c)
    assert "did you mean" in out and "/resume" in out


async def test_handle_resume_returns_the_full_session_not_the_stripped_row():
    """``resolve`` answers from the stripped list view (no bodies); resuming
    must then do a full ``load``, or the new session has zero messages and the
    next save wipes the conversation it just opened."""
    from ai_coding.domain.message import ChatMessage

    loop = _FakeLoop()
    full = _sd("abcdef0123456789fedcba9876543210")
    full.add_message(ChatMessage(role="user", content="remember me", timestamp="t"))
    stripped = _sd(full.session_id)  # what list_sessions/resolve hand out

    class _StrippedSessions(_FakeSessions):
        def __init__(self) -> None:
            self.items = [full]

        def resolve(self, prefix):
            return [stripped] if stripped.session_id.startswith(prefix) else []

        def load(self, session_id):
            return full if session_id == full.session_id else None

    dc, _ = _console_capture()
    action, session = await handle_input(
        loop, "/resume abcdef012345", full, _StrippedSessions(), dc
    )

    assert action is REPLAction.NEW_SESSION
    assert session is full
    assert [m.content for m in session.messages] == ["remember me"]


async def test_a_path_is_still_sent_to_the_model():
    """/path/to/file is not a command; it is text the model may need."""
    loop = _FakeLoop()
    sessions = _FakeSessions()
    dc, _ = _console_capture()

    action, _ = await handle_input(loop, "/path/to/file", sessions.items[0], sessions, dc)

    assert action is REPLAction.CONTINUE
    assert loop.calls[0][0] == "/path/to/file"


async def test_a_distant_unknown_command_is_still_sent_to_the_model():
    """/zzz is not close to anything, so it stays text."""
    loop = _FakeLoop()
    sessions = _FakeSessions()
    dc, _ = _console_capture()

    action, _ = await handle_input(loop, "/zzz", sessions.items[0], sessions, dc)

    assert action is REPLAction.CONTINUE
    assert loop.calls[0][0] == "/zzz"


async def test_unknown_command_at_distance_two_gets_a_suggestion():
    """``/stat`` is two edits from ``/status`` — still a typo, not a path."""
    loop = _FakeLoop()
    sessions = _FakeSessions()
    dc, c = _console_capture()

    action, _ = await handle_input(loop, "/stat", sessions.items[0], sessions, dc)

    assert action is REPLAction.CONTINUE
    assert loop.calls == []
    out = _session_text(c)
    # /stats joined the vocabulary in A3.2, so it is now the nearest match for
    # "/stat" — the suggestion follows the table, not the other way round.
    assert "did you mean" in out and "/stats" in out


async def test_absorbed_command_is_answered_instead_of_becoming_a_model_turn():
    """The end-to-end half: a tombstone suggestion must stop the turn.

    suggest_command returning "plan run" is only half the fix — if the caller
    then forwards the token anyway, the user still gets a model turn, which is
    the behaviour this replaces.
    """
    loop = _FakeLoop()
    sessions = _FakeSessions()
    dc, c = _console_capture()

    action, _ = await handle_input(loop, "/plan-run", sessions.items[0], sessions, dc)

    assert action is REPLAction.CONTINUE
    assert loop.calls == []
    out = _session_text(c)
    assert "did you mean" in out and "/plan run" in out


async def test_plan_run_without_open_items_says_so():
    """No todos, no execution: the runner says so instead of calling the model."""
    from ai_coding.domain.session import SessionData

    loop = _FakeLoop()
    sessions = _FakeSessions()
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    dc, c = _console_capture()

    action, _ = await handle_input(loop, "/plan run", session, sessions, dc)

    assert action is REPLAction.CONTINUE
    assert loop.calls == []
    out = _session_text(c)
    assert "无任务项" in out or "完成 0 步" in out


async def test_plan_then_plan_run_executes_open_todos():
    """18 §10.1: /plan <topic> plans only; /plan run executes each open item."""
    from types import SimpleNamespace

    from ai_coding.core.turn_policy import TurnPolicy, TurnPolicyConfig
    from ai_coding.domain.session import SessionData
    from ai_coding.domain.task import TaskState

    class _PlanLoop(_FakeLoop):
        def __init__(self):
            super().__init__()
            self.settings = SimpleNamespace(
                plan_step_max_iterations=8, plan_max_step_turns=20, stop_policy="natural"
            )
            self.policy = TurnPolicy(TurnPolicyConfig(max_iterations=100))

        async def process_input(self, session, text, on_delta=None, observer=None):
            session.turn_count += 1
            self.calls.append((text, on_delta))
            if "implementation plan" in text:
                session.task = TaskState(goal="g")
                session.task.add("fix bug")
                return "plan written"
            for item in session.task.open_items():
                if not item.is_terminal():
                    item.status = "completed"
                    break
            return "step done"

    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    sessions = _FakeSessions()
    dc, c = _console_capture()
    loop = _PlanLoop()

    await handle_input(loop, "/plan 修 bug", session, sessions, dc)

    assert sum("只做这一步" in call[0] for call in loop.calls) == 0
    assert session.task.done_count == 0

    action, _ = await handle_input(loop, "/plan run", session, sessions, dc)

    assert action is REPLAction.CONTINUE
    out = _session_text(c)
    # H3 audit fix: the deleted /plan-run must not appear anywhere, and the
    # runner reports under [plan].  The old assertion "plan-run in out" could
    # not catch this: "[plan]" is a substring of "[plan-run]".
    assert "[plan-run]" not in out
    assert "[plan]" in out and "已完成" in out
    assert session.task.items[0].status == "completed"


async def test_handle_tasks_without_a_runner_degrades():
    """Loops without a sub-agent stack get words, not a traceback."""
    loop = _FakeLoop()
    sessions = _FakeSessions()
    dc, c = _console_capture()

    action, _ = await handle_input(loop, "/tasks", sessions.items[0], sessions, dc)

    assert action is REPLAction.CONTINUE
    assert "no sub-agent runner" in _session_text(c)


async def test_handle_tasks_lists_background_rows_and_stops_unknown():
    """The table shows status/elapsed/tokens; stopping a missing id says so."""
    from types import SimpleNamespace

    from ai_coding.domain.subagent import BackgroundTask
    from ai_coding.tools.registry import ToolRegistry
    from ai_coding.tools.subagent_tool import SubAgentTool

    class _TableRunner:
        def task_list(self):
            return [BackgroundTask(run_id="abc123", name="probe", task="t",
                                   status="running", started=10.0, ended=None,
                                   usage={"prompt_tokens": 100,
                                          "completion_tokens": 20})]

        def resolve_run_id(self, prefix):
            return prefix if prefix.startswith("abc123") else None

        def cancel(self, run_id):
            self.cancelled = run_id
            return True

    registry = ToolRegistry()
    registry.register(SubAgentTool(_TableRunner()))
    loop = SimpleNamespace(dispatcher=SimpleNamespace(registry=registry))
    sessions = _FakeSessions()
    dc, c = _console_capture()

    action, _ = await handle_input(loop, "/tasks", sessions.items[0], sessions, dc)
    out = _session_text(c)
    assert action is REPLAction.CONTINUE
    assert "abc123" in out and "running" in out and "120" in out


@pytest.mark.asyncio
async def test_handle_tasks_stop_accepts_the_printed_prefix():
    """``/tasks`` prints 12 chars; stopping by exactly those must work (the
    operator has nothing else to copy)."""
    from ai_coding.core.subagent import BackgroundTask
    from ai_coding.tools.registry import ToolRegistry
    from ai_coding.tools.subagent_tool import SubAgentTool

    class _TableRunner:
        cancelled = None

        def task_list(self):
            return [BackgroundTask(run_id="abc123def4567890", name="probe", task="t",
                                   status="running", started=10.0, ended=None)]

        def resolve_run_id(self, prefix):
            return prefix if prefix.startswith("abc123") else None

        def cancel(self, run_id):
            self.cancelled = run_id
            return True

    runner = _TableRunner()
    registry = ToolRegistry()
    registry.register(SubAgentTool(runner))
    loop = SimpleNamespace(dispatcher=SimpleNamespace(registry=registry))
    sessions = _FakeSessions()
    dc, c = _console_capture()

    action, _ = await handle_input(
        loop, "/tasks stop abc123", sessions.items[0], sessions, dc
    )
    assert action is REPLAction.CONTINUE
    assert runner.cancelled == "abc123"  # the prefix itself resolves to the row
    assert "stopping abc123" in _session_text(c)

    action, _ = await handle_input(loop, "/tasks stop nope", sessions.items[0],
                                   sessions, dc)
    assert action is REPLAction.CONTINUE
    assert "no background task" in _session_text(c)


async def test_handle_plan_with_topic_uses_it_and_resets_task():
    """'/plan <topic>' plans that topic on a fresh list (R1-5b).

    The topic used to be silently dropped, and the new plan landed in the
    session's stale task (whose goal was the session's first question).
    """
    from ai_coding.domain.task import TaskState

    loop = _FakeLoop()
    sessions = _FakeSessions()
    session = sessions.items[0]
    session.task = TaskState(goal="old goal")
    session.task.add("stale item")
    dc, _ = _console_capture()

    action, _ = await handle_input(loop, "/plan 给记忆层加缓存", session, sessions, dc)

    assert action is REPLAction.CONTINUE
    assert "给记忆层加缓存" in loop.calls[0][0]
    assert session.task.goal == "给记忆层加缓存"
    assert session.task.items == []
    # Planning writes the list; it must not execute it.
    assert "pending" in loop.calls[0][0].lower() or "do not execute" in loop.calls[0][0].lower()
    # 18 §10.1: the planning path also raises the two switches.
    assert (session.mode, session.readonly) == ("plan", True)


def test_budget_shows_money_when_the_recorder_knows_the_price(tmp_path):
    """`/budget` finally reads the price the recorder has been handed.

    cli.py has been setting ``recorder.pricing`` since R4a with nothing reading
    it, so cached tokens were not merely mispriced — no money was shown at all.
    With a price configured the estimate splits fresh and cached input; without
    one the line stays token-only, which is R4a's rule for unknown prices.
    """
    from ai_coding.app.commands import _budget_report
    from ai_coding.core.budget import Pricing
    from ai_coding.core.trace import TraceStore

    store = TraceStore(tmp_path)
    store.update_summary("priced", {
        "turns": 3, "steps": 9, "prompt_tokens": 30_000,
        "completion_tokens": 3_000, "cached_tokens": 18_000,
    })

    class _PricedLoop:
        def __init__(self, store):
            self.trace = _FakeTrace(store)

    loop = _PricedLoop(store)
    loop.trace.pricing = Pricing(input_per_1k=0.002, output_per_1k=0.008,
                                 cached_per_1k=0.0002)
    priced = _budget_report(loop, _session("priced"))
    assert "cache 60%" in priced
    # 12k fresh * 0.002 + 18k cached * 0.0002 + 3k out * 0.008 = 0.0516
    assert "≈ ¥0.0516" in priced

    loop.trace.pricing = None
    plain = _budget_report(loop, _session("priced"))
    assert "≈" not in plain
    assert "cache 60%" in plain


async def test_handle_model_reports_a_model_the_engine_cannot_serve():
    """A model can be configured and still not be servable (G7).

    With a fallback chain the engine only knows the models on that chain, so
    switching to a third one is refused -- and the refusal used to be a
    ``ValueError`` raised out of the submit path, where nothing catches it.
    The command has to say what happened.
    """
    loop = _FakeLoop()
    _model_table(loop)

    def refuse(name: str) -> None:
        raise ValueError(f"model '{name}' is not in this fallback chain")

    loop.set_model = refuse
    sessions = _FakeSessions()
    dc, c = _console_capture()

    action, _ = await handle_input(loop, "/model claude", sessions.items[0], sessions, dc)

    assert action is REPLAction.CONTINUE
    out = _session_text(c)
    # ``console.error`` here is a real Rich console, so the message is what
    # proves it was reported rather than raised.
    assert "cannot switch to 'claude'" in out
    assert "not in this fallback chain" in out


async def test_handle_input_threads_the_observer_to_the_loop():
    """A TUI driving ``handle_input`` must see turn events, not only deltas
    (the IPC bridge is the observer; tool cards and turn ends depend on it)."""
    loop = _FakeLoop()
    sessions = _FakeSessions()
    dc, _ = _console_capture()

    class _Marker:
        pass

    marker = _Marker()
    await handle_input(loop, "tell me", sessions.items[0], sessions, dc, observer=marker)
    assert loop.calls[-1][2] is marker

    loop.calls.clear()
    await handle_input(loop, "/review", sessions.items[0], sessions, dc, observer=marker)
    assert loop.calls[-1][2] is marker


async def test_handle_input_threads_the_observer_into_the_planning_half(monkeypatch):
    """18 §10.1: /plan <topic> threads topic/observer/on_delta into plan_only."""
    from ai_coding.core import plan_runner as plan_runner_mod

    seen: dict = {}

    async def fake_plan_only(loop, session, *, topic, on_delta=None, observer=None):
        seen.update(topic=topic, observer=observer, on_delta=on_delta)
        return plan_runner_mod.PlanReport()

    monkeypatch.setattr(plan_runner_mod, "plan_only", fake_plan_only)
    loop = _FakeLoop()
    sessions = _FakeSessions()
    dc, _ = _console_capture()

    class _Marker:
        pass

    marker = _Marker()
    await handle_input(
        loop, "/plan 加缓存", sessions.items[0], sessions, dc, observer=marker
    )

    assert seen["topic"] == "加缓存"
    assert seen["observer"] is marker


async def test_stats_reports_across_sessions(tmp_path):
    """``/stats`` folds every traced session into one report.

    Read-only by construction (J5), and an empty store says so instead of
    rendering a hollow table.
    """
    from ai_coding.core.trace import TraceStore

    store = TraceStore(tmp_path / "traces")
    store.update_summary(
        "s-one",
        {"turns": 2, "prompt_tokens": 120, "completion_tokens": 30},
    )
    store.update_summary(
        "s-two",
        {"turns": 1, "prompt_tokens": 10, "completion_tokens": 5},
    )

    class _StatsLoop:
        def __init__(self):
            self.trace = _FakeTrace(store)

    sessions = _FakeSessions()
    dc, c = _console_capture()

    action, _ = await handle_input(_StatsLoop(), "/stats", _sd("s"), sessions, dc)
    out = _session_text(c)
    assert action is REPLAction.CONTINUE
    assert "[stats]" in out
    assert "s-one" in out and "s-two" in out
    assert "165" in out  # the totals row sums both sessions


async def test_stats_says_when_nothing_is_traced(tmp_path):
    from ai_coding.core.trace import TraceStore

    class _StatsLoop:
        def __init__(self):
            self.trace = _FakeTrace(TraceStore(tmp_path / "empty"))

    sessions = _FakeSessions()
    dc, c = _console_capture()
    await handle_input(_StatsLoop(), "/stats", _sd("s"), sessions, dc)
    assert "no traced sessions" in _session_text(c)


async def test_export_writes_html_into_the_workspace(tmp_path):
    """J4: the export lands inside the workspace, and the path is reported."""
    from ai_coding.core.trace import TraceStore

    store = TraceStore(tmp_path / ".ai-coding" / "traces")
    store.update_summary("s-one", {"turns": 1, "prompt_tokens": 7, "completion_tokens": 3})

    class _ExportLoop:
        workspace = str(tmp_path)

        def __init__(self):
            self.trace = _FakeTrace(store)

    sessions = _FakeSessions()
    dc, c = _console_capture()

    action, _ = await handle_input(_ExportLoop(), "/export html", _sd("s"), sessions, dc)
    out = _session_text(c)
    assert action is REPLAction.CONTINUE
    assert "[export]" in out
    written = list((tmp_path / ".ai-coding" / "exports").glob("*.html"))
    assert written, f"no export written; said: {out}"
    assert "<!doctype html>" in written[0].read_text(encoding="utf-8")


async def test_export_otlp_writes_json(tmp_path):
    from ai_coding.core.trace import TraceStore

    store = TraceStore(tmp_path / ".ai-coding" / "traces")
    store.update_summary("s-one", {"turns": 1, "prompt_tokens": 7, "completion_tokens": 3})

    class _ExportLoop:
        workspace = str(tmp_path)

        def __init__(self):
            self.trace = _FakeTrace(store)

    sessions = _FakeSessions()
    dc, c = _console_capture()
    await handle_input(_ExportLoop(), "/export otlp", _sd("s"), sessions, dc)
    written = list((tmp_path / ".ai-coding" / "exports").glob("*.json"))
    assert written, f"no export written; said: {_session_text(c)}"
    assert "resourceSpans" in written[0].read_text(encoding="utf-8")


async def test_export_needs_a_known_format(tmp_path):
    from ai_coding.core.trace import TraceStore

    class _ExportLoop:
        workspace = str(tmp_path)

        def __init__(self):
            self.trace = _FakeTrace(TraceStore(tmp_path / "t"))

    sessions = _FakeSessions()
    dc, c = _console_capture()
    await handle_input(_ExportLoop(), "/export pdf", _sd("s"), sessions, dc)
    said = _session_text(c)  # one read: the recorder drains on export
    assert "html" in said and "otlp" in said

