"""Plan command routing (18 §10.1, TDD): the two-switch command matrix.

``/plan`` enters read-only plan mode; ``/plan <topic>`` plans through
``plan_only``; ``/plan off`` disarms both switches; ``/plan run`` executes via
the runner; ``/readonly on|off`` owns the enforcement axis alone.  The old
one-shot contract (bare ``/plan`` asks a topic, then plans AND executes) is
gone: a conversational agreement approves nothing, and execution is an
explicit command.
"""

import pytest
from rich.console import Console

from ai_coding.app.commands import REPLAction, handle_input
from ai_coding.app.console import DialogueConsole
from ai_coding.core.turn_policy import TurnPolicy, TurnPolicyConfig
from ai_coding.domain.session import SessionData


class _Loop:
    """A posture-aware stub mirroring AgentLoop.request_posture's two halves."""

    def __init__(self, *, busy: bool = False) -> None:
        self.calls: list[str] = []
        self.policy = TurnPolicy(TurnPolicyConfig(max_auto_continuations=3))
        self.busy = busy

    @property
    def turn_running(self) -> bool:
        return self.busy

    def request_posture(self, session, *, mode=None, readonly=None):
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
        self.calls.append(text)
        return "planned"


class _Sessions:
    def __init__(self) -> None:
        self.saved: list = []

    def save(self, session) -> None:
        self.saved.append(session)

    def mark_pending_approval(self, session_id: str, request: object) -> None:
        return None


def _session() -> SessionData:
    return SessionData(session_id="s", title="", created_time="t", last_access_time="t")


def _console() -> tuple[DialogueConsole, Console]:
    c = Console(record=True, width=80)
    return DialogueConsole(c), c


# ---- the entry horn: /plan / /plan <topic> -----------------------------------


@pytest.mark.asyncio
async def test_bare_plan_enters_read_only_mode_without_a_model_call():
    loop = _Loop()
    sessions = _Sessions()
    session = _session()
    dc, c = _console()

    action, _ = await handle_input(loop, "/plan", session, sessions, dc)

    assert action is REPLAction.CONTINUE
    assert (session.mode, session.readonly) == ("plan", True)
    assert sessions.saved == [session]
    assert loop.calls == []
    assert "已进入计划模式（只读）" in c.export_text()


@pytest.mark.asyncio
async def test_bare_plan_while_already_on_is_a_notice_only():
    loop = _Loop()
    sessions = _Sessions()
    session = _session()
    session.mode, session.readonly = "plan", True
    dc, c = _console()

    await handle_input(loop, "/plan", session, sessions, dc)

    assert sessions.saved == []
    assert loop.calls == []
    assert "已在计划模式" in c.export_text()


@pytest.mark.asyncio
async def test_plan_notices_teach_the_mode_not_slash_commands():
    """内化批次: the notices speak the mode's own verbs (review card,
    shift+tab) -- the /plan family stays an internal interface and is not
    advertised anywhere the user reads."""
    loop = _Loop()
    sessions = _Sessions()
    session = _session()
    dc, c = _console()

    await handle_input(loop, "/plan", session, sessions, dc)
    out = c.export_text()
    assert "已进入计划模式（只读）" in out
    assert "评审卡" in out and "shift+tab" in out
    assert "/plan" not in out
    assert "/readonly" not in out

    dc2, c2 = _console()
    await handle_input(loop, "/plan", session, sessions, dc2)
    out2 = c2.export_text()
    assert "已在计划模式" in out2 and "评审卡" in out2
    assert "/plan" not in out2


@pytest.mark.asyncio
async def test_help_no_longer_lists_the_plan_family():
    """内化批次: /help dropped the four internal lines (plan x3, readonly);
    the frontend catalogue was cleaned in stage 3, the backend help was not."""
    loop = _Loop()
    sessions = _Sessions()
    session = _session()
    dc, c = _console()

    await handle_input(loop, "/help", session, sessions, dc)

    out = c.export_text()
    assert "direct commands" in out
    assert "/plan" not in out
    assert "/readonly" not in out


@pytest.mark.asyncio
async def test_plan_topic_clears_the_receipt_book_behaviourally():
    """The collision bug, end to end: a new plan must not inherit receipts."""
    from ai_coding.domain.plan_delivery import StepDelivery

    session = _session()
    session.step_deliveries = [
        StepDelivery(step_id="1", statement="old one", status="completed")
    ]
    loop = _Loop()
    sessions = _Sessions()
    dc, c = _console()

    action, _ = await handle_input(loop, "/plan 新计划", session, sessions, dc)

    assert action is REPLAction.CONTINUE
    assert session.task.goal == "新计划"
    assert session.step_deliveries == []
    assert (session.mode, session.readonly) == ("plan", True)
    assert sessions.saved == [session]
    # 19 §3.5: readiness is announced by present_plan's review card now — a
    # finished planning turn no longer auto-claims "计划就绪" (measured: a
    # budget-dead half-plan said it anyway).
    assert "计划就绪" not in c.export_text()


@pytest.mark.asyncio
async def test_repl_plan_turn_runs_with_auto_continuation_disabled():
    """Review fix: the planning turn must not inherit C3."""
    loop = _Loop()
    armed = loop.policy
    session = _session()
    dc, _ = _console()

    await handle_input(loop, "/plan 新计划", session, _Sessions(), dc)

    assert all(call.count("implementation plan") for call in loop.calls[:1]) or loop.calls
    assert loop.policy is armed


@pytest.mark.asyncio
async def test_the_planning_turn_sees_c3_disabled(monkeypatch):
    """The planning turn itself must run under max_auto_continuations=0."""
    seen: list[int] = []

    class _RecordingLoop(_Loop):
        async def process_input(self, session, text, on_delta=None, observer=None):
            self.calls.append(text)
            seen.append(self.policy.config.max_auto_continuations)
            return "planned"

    loop = _RecordingLoop()
    dc, _ = _console()

    await handle_input(loop, "/plan 新计划", _session(), _Sessions(), dc)

    assert seen[0] == 0
    assert loop.policy.config.max_auto_continuations == 3


# ---- the exit horns: /plan off / /plan run -----------------------------------


@pytest.mark.asyncio
async def test_plan_off_double_disarms_and_keeps_the_hint_slash_free():
    loop = _Loop()
    sessions = _Sessions()
    session = _session()
    session.mode, session.readonly = "plan", True
    session.task = __import__("ai_coding.domain.task", fromlist=["TaskState"]).TaskState(
        goal="g"
    )
    session.task.add("still open")
    dc, c = _console()

    await handle_input(loop, "/plan off", session, sessions, dc)

    assert (session.mode, session.readonly) == ("normal", False)
    assert sessions.saved == [session]
    out = c.export_text()
    # 内化批次: the open-items hint used to teach "/plan run"; it now points
    # back at the mode itself.
    assert "已退出计划模式" in out and "1 项" in out
    assert "/plan" not in out


@pytest.mark.asyncio
async def test_plan_off_without_a_plan_still_confirms_the_switch():
    loop = _Loop()
    sessions = _Sessions()
    session = _session()
    dc, c = _console()

    await handle_input(loop, "/plan off", session, sessions, dc)

    assert (session.mode, session.readonly) == ("normal", False)
    assert "本就不在计划模式" in c.export_text()


# ---- D1 (20 §批次 4a): /plan off can swap an approved plan --------------------


@pytest.mark.asyncio
async def test_plan_off_gives_up_an_approved_plan():
    """The only way out of an approved plan the run never took.

    Both refusals point here -- "已有一份批准的计划尚未执行；先执行它
    （/plan run），或用 /plan off 放弃后再重新规划" -- and until now the
    command only flipped the posture, so the user did what the error said
    and hit the same wall twice over.

    Giving it up means withdrawing the approval: the arm goes, and the
    profile goes with it (a profile with nothing armed owns nothing, and
    the boot resume reads the pair).
    """
    from ai_coding.core.events import RecordingObserver, TaskUpdated
    from ai_coding.domain.task import TaskState

    loop = _Loop()
    sessions = _Sessions()
    session = _session()
    session.mode, session.readonly = "plan", True
    session.task = TaskState(goal="g")
    session.task.add("第一步")
    session.task.armed = True
    session.task.armed_profile = "plan"
    dc, c = _console()
    observer = RecordingObserver()

    await handle_input(loop, "/plan off", session, sessions, dc, observer=observer)

    assert session.task.armed is False
    assert session.task.armed_profile == ""
    out = c.export_text()
    assert "已放弃" in out
    # The old hint said "you can continue" -- which contradicts giving up.
    assert "未执行" not in out
    events = observer.of_type(TaskUpdated)
    assert len(events) == 1 and events[0].task.armed is False, (
        "the front end must see the withdrawal, or it keeps a card for a "
        "plan the user gave up"
    )


@pytest.mark.asyncio
async def test_plan_off_without_an_armed_plan_says_nothing_about_giving_up():
    """R3: the withdrawal must not misfire.  A task that is not an approved
    plan gets the words it always got."""
    from ai_coding.domain.task import TaskState

    loop = _Loop()
    sessions = _Sessions()
    session = _session()
    session.task = TaskState(goal="g")
    session.task.add("still open")
    dc, c = _console()

    await handle_input(loop, "/plan off", session, sessions, dc)

    out = c.export_text()
    assert "已放弃" not in out
    assert "还有 1 项未执行" in out


@pytest.mark.asyncio
async def test_plan_off_does_not_claim_a_goal_was_given_up():
    """R4: a goal is another line -- its exit is "/goal off".  The plan
    command must not say it gave up a plan it never touched."""
    from ai_coding.domain.task import TaskState

    loop = _Loop()
    sessions = _Sessions()
    session = _session()
    session.task = TaskState(goal="obj")
    session.task.arm_goal("obj")
    dc, c = _console()

    await handle_input(loop, "/plan off", session, sessions, dc)

    assert session.task.armed is True
    assert session.task.armed_profile == "goal"
    assert "已放弃" not in c.export_text()


@pytest.mark.asyncio
async def test_a_plan_given_up_can_be_replaced_by_a_new_one():
    """The dead end, closed.

    "已有一份批准的计划尚未执行；先执行它（/plan run），或用 /plan off
    放弃后再重新规划" -- and the second half of that sentence was a lie:
    "/plan off" left the plan approved, so the new planning round was
    refused with the same words.  Giving the plan up has to make the next
    round possible.
    """
    from ai_coding.domain.task import TaskState

    loop = _Loop()
    sessions = _Sessions()
    session = _session()
    session.task = TaskState(goal="g")
    session.task.add("第一步")
    session.task.armed = True
    session.task.armed_profile = "plan"
    dc, _c = _console()

    await handle_input(loop, "/plan off", session, sessions, dc)
    dc2, c2 = _console()
    await handle_input(loop, "/plan 新主题", session, sessions, dc2)

    assert loop.calls, "the new planning round was refused"
    assert "尚未执行" not in c2.export_text()



@pytest.mark.asyncio
async def test_plan_run_executes_the_open_plan():
    """The explicit approval: /plan run disarms and executes, /plan plans only."""
    from types import SimpleNamespace

    from ai_coding.core.plan_runner import STEP_HEAD
    from ai_coding.domain.task import TaskState

    class _PlanLoop(_Loop):
        def __init__(self) -> None:
            super().__init__()
            self.settings = SimpleNamespace(
                plan_step_max_iterations=8,
                plan_max_step_turns=20,
                stop_policy="natural",
            )
            self.policy = TurnPolicy(TurnPolicyConfig(max_iterations=100))

        async def process_input(self, session, text, on_delta=None, observer=None):
            self.calls.append(text)
            if "implementation plan" in text:
                session.task = TaskState(goal="g")
                session.task.add("fix bug")
                return "plan written"
            for item in session.task.open_items():
                if not item.is_terminal():
                    item.status = "completed"
                    break
            return "step done"

    session = _session()
    sessions = _Sessions()
    loop = _PlanLoop()
    dc, c = _console()

    await handle_input(loop, "/plan 修 bug", session, sessions, dc)

    assert sum(STEP_HEAD in call for call in loop.calls) == 0
    assert session.task.done_count == 0
    assert (session.mode, session.readonly) == ("plan", True)

    await handle_input(loop, "/plan run", session, sessions, dc)

    assert sum(STEP_HEAD in call for call in loop.calls) == 1
    assert session.task.done_count == 1
    assert (session.mode, session.readonly) == ("normal", False)
    assert "已完成" in c.export_text()


@pytest.mark.asyncio
async def test_plan_run_without_a_plan_runs_and_reports_empty():
    """No plan, no model turn: the runner says "无任务项" instead."""
    class _RunnerLessLoop(_Loop):
        async def process_input(self, session, text, on_delta=None, observer=None):
            self.calls.append(text)
            return "step done"

    loop = _RunnerLessLoop()
    session = _session()
    dc, c = _console()

    await handle_input(loop, "/plan run", session, _Sessions(), dc)

    assert loop.calls == []
    out = c.export_text()
    assert "无任务项" in out or "完成 0 步" in out


@pytest.mark.asyncio
async def test_run_surfaces_a_stopped_plan():
    """W2/R02: the run branch used to drop the runner's report on the floor,
    so a plan the runner refused produced no message at all -- the user saw
    the command return as if it had run."""
    from types import SimpleNamespace

    from ai_coding.domain.task import TaskState

    class _RefusedLoop(_Loop):
        def __init__(self) -> None:
            super().__init__()
            self.settings = SimpleNamespace(
                plan_step_max_iterations=8,
                plan_max_step_turns=20,
                stop_policy="natural",
            )
            self.policy = TurnPolicy(TurnPolicyConfig(max_iterations=100))
            self.workspace = "/tmp/ws"

        async def process_input(self, session, text, on_delta=None, observer=None):
            self.calls.append(text)
            return "step done"

    session = _session()
    session.task = TaskState(goal="g")
    session.task.add("写代码到 /data/x/app.py")
    loop = _RefusedLoop()
    dc, c = _console()

    await handle_input(loop, "/plan run", session, _Sessions(), dc)

    out = c.export_text()
    assert "工作区外" in out, f"the refusal was not surfaced: {out!r}"
    assert "/data/x/app.py" in out
    assert loop.calls == [], "the refused plan still ran"


# ---- the enforcement axis alone: /readonly -----------------------------------


@pytest.mark.asyncio
async def test_readonly_command_toggles_the_switch():
    loop = _Loop()
    sessions = _Sessions()
    session = _session()
    dc, c = _console()

    await handle_input(loop, "/readonly on", session, sessions, dc)
    assert session.readonly is True
    assert sessions.saved == [session]
    assert "只读已开启" in c.export_text()

    await handle_input(loop, "/readonly off", session, sessions, dc)
    assert session.readonly is False
    assert len(sessions.saved) == 2
    assert "只读已关闭" in c.export_text()


@pytest.mark.asyncio
async def test_readonly_bare_shows_usage_without_saving():
    loop = _Loop()
    sessions = _Sessions()
    session = _session()
    dc, c = _console()

    await handle_input(loop, "/readonly", session, sessions, dc)

    assert sessions.saved == []
    out = c.export_text()
    assert "用法" in out and "off" in out


@pytest.mark.asyncio
async def test_posture_commands_queue_while_a_turn_is_running():
    """18 §10.2: mid-turn posture flips queue with an explicit receipt."""
    loop = _Loop(busy=True)
    sessions = _Sessions()
    session = _session()
    dc, c = _console()

    await handle_input(loop, "/plan", session, sessions, dc)

    assert (session.mode, session.readonly) == ("normal", False)
    assert session.pending_posture == {"mode": "plan", "readonly": True}
    assert sessions.saved == []
    assert "将在当前回合结束后生效" in c.export_text()


@pytest.mark.asyncio
async def test_a_mid_turn_plan_topic_queues_the_posture_only():
    """The topic half needs an idle loop; mid-turn the receipt says so."""
    loop = _Loop(busy=True)
    sessions = _Sessions()
    session = _session()
    dc, c = _console()

    await handle_input(loop, "/plan 给缓存加 TTL", session, sessions, dc)

    assert loop.calls == []  # no planning turn was started
    assert session.pending_posture == {"mode": "plan", "readonly": True}
    out = c.export_text()
    assert "直接描述计划主题" in out and "将在当前回合结束后生效" in out


# ---- 19 §3.4-1: the planning round gets its own budget ------------------------


@pytest.mark.asyncio
async def test_the_planning_turn_runs_with_the_planning_budget():
    """The planning round used to share max_tool_iterations (measured: dead at
    15 while still reading files).  It now swaps in its own budget, restored
    afterwards like the C3 swap (19 §3.4-1)."""
    seen: list[tuple[int, int]] = []

    class _RecordingLoop(_Loop):
        async def process_input(self, session, text, on_delta=None, observer=None):
            self.calls.append(text)
            seen.append(
                (
                    self.policy.config.max_iterations,
                    self.policy.config.max_auto_continuations,
                )
            )
            return "planned"

    loop = _RecordingLoop()
    original = loop.policy.config.max_iterations
    dc, _ = _console()

    await handle_input(loop, "/plan 新计划", _session(), _Sessions(), dc)

    assert seen == [(30, 0)]
    assert loop.policy.config.max_iterations == original  # swap restored


def test_the_plan_prompt_starts_with_a_skeleton_and_bans_whole_file_reads():
    """19 §3.4-1: the budget is headroom; the guidance is the fix."""
    from ai_coding.core.plan_runner import PLAN_PROMPT

    lowered = PLAN_PROMPT.lower()
    assert "skeleton" in lowered
    assert "grep/glob" in lowered


@pytest.mark.asyncio
async def test_a_budget_dead_planning_round_says_so():
    """19 §3.5: a planning turn cut by the iteration budget gets an honest
    notice — it used to print "计划就绪：N 项" over a half-written plan."""
    from types import SimpleNamespace

    from ai_coding.domain.loop import TurnStatus

    class _DeadLoop(_Loop):
        @property
        def last_outcome(self):
            return SimpleNamespace(status=TurnStatus.MAX_ITERATIONS, usage=None)

    dc, c = _console()

    await handle_input(_DeadLoop(), "/plan 新计划", _session(), _Sessions(), dc)

    out = c.export_text()
    assert "规划未完成" in out
    assert "计划就绪" not in out


def test_the_plan_prompt_submits_through_present_plan():
    """19 §3.2: the model must be told to hand the plan over — the review card
    is the only readiness signal, and dsh's guidance says the same thing
    (exit_plan_mode as the final call)."""
    from ai_coding.core.plan_runner import PLAN_PROMPT

    assert "present_plan" in PLAN_PROMPT


def test_the_plan_prompt_sends_the_steps_through_present_plan():
    """P1 (trace ec89d5ee): the prompt taught a todo_write workflow whose
    measured cost was 8 iterations for 8 steps plus an add/clear loop.  The
    steps ride present_plan's own call now, with verify on each item."""
    from ai_coding.core.plan_runner import PLAN_PROMPT

    lowered = PLAN_PROMPT.lower()
    assert "present_plan" in lowered
    assert "items" in lowered
    assert "verify" in lowered
    assert "todo items with todo_write" not in lowered  # the old teaching is gone
    assert "do not track the planning with todo_write" in lowered


@pytest.mark.asyncio
async def test_plan_run_exits_the_posture_even_when_the_flip_had_to_queue():
    """竞态实锤（c41e461 的 tmux 运行）：/plan run 派发落在回合锁内 → 退出
    被排队到主会话，而 run 只跑步会话（fresh，自带 normal）→ 队列没人消费，
    主会话残留 plan/readonly=True（磁盘同残，重启复活）。收尾兜底把翻转
    落在 run 之后：即时/排队两态归一，且入盘。"""

    class _BusyThenFreeLoop(_Loop):
        async def process_input(self, session, text, on_delta=None, observer=None):
            self.calls.append(text)
            self.busy = False  # the lock is long gone by the time the run ends
            return "step done"

    from ai_coding.domain.task import TaskState

    loop = _BusyThenFreeLoop(busy=True)
    sessions = _Sessions()
    session = _session()
    session.mode, session.readonly = "plan", True
    session.task = TaskState(goal="g")
    session.task.add("still open")  # a real step: the stub's process_input runs
    dc, c = _console()

    await handle_input(loop, "/plan run", session, sessions, dc)

    assert (session.mode, session.readonly) == ("normal", False)
    assert sessions.saved and sessions.saved[-1] is session
    out = c.export_text()
    assert "执行完成后退出计划模式" in out


def test_the_plan_prompt_says_the_mutating_tools_are_unavailable():
    """只读探索批次: plan mode hides bash/write/edit (the same mutates_world
    predicate the gate refuses with); the prompt says so, because a model that
    never sees the tool will still call it from memory."""
    from ai_coding.core.plan_runner import PLAN_PROMPT

    lowered = PLAN_PROMPT.lower()
    assert "unavailable" in lowered
    assert "read/grep/glob" in lowered


# ---- R03: an armed non-plan task keeps the whole /plan family out ------------


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["/plan", "/plan 新主题", "/plan run"])
async def test_plan_refuses_while_a_non_plan_task_is_armed(raw):
    """R03: a goal (any non-plan profile) owns the task table — bare entry, a
    topic run and /plan run must all stay out of it, or they hijack the goal
    (begin_plan clears it; the runner would rewrite armed_profile to plan)."""
    from types import SimpleNamespace

    from ai_coding.domain.task import TaskState

    class _StubLoop(_Loop):
        def __init__(self) -> None:
            super().__init__()
            self.settings = SimpleNamespace(
                plan_step_max_iterations=8,
                plan_max_step_turns=20,
                stop_policy="natural",
            )
            self.policy = TurnPolicy(TurnPolicyConfig(max_iterations=100))

    loop = _StubLoop()
    sessions = _Sessions()
    session = _session()
    session.task = TaskState(goal="obj")
    session.task.arm_goal("obj")
    session.task.add("goal step")
    dc, c = _console()

    action, _ = await handle_input(loop, raw, session, sessions, dc)

    assert action is REPLAction.CONTINUE
    assert loop.calls == []  # no planning turn, no step run
    assert (session.mode, session.readonly) == ("normal", False)
    assert sessions.saved == []
    assert session.task.armed is True and session.task.armed_profile == "goal"
    assert session.task.done_count == 0
    assert "已有任务在跑" in c.export_text()


@pytest.mark.asyncio
async def test_plan_off_stays_available_while_a_goal_is_armed():
    """/plan off is the safety valve the refusal must not swallow: it lands
    without touching the running task."""
    from ai_coding.domain.task import TaskState

    loop = _Loop()
    sessions = _Sessions()
    session = _session()
    session.task = TaskState(goal="obj")
    session.task.arm_goal("obj")
    dc, c = _console()

    await handle_input(loop, "/plan off", session, sessions, dc)

    assert sessions.saved == [session]
    assert session.task.armed is True and session.task.armed_profile == "goal"
    assert "本就不在计划模式" in c.export_text()
