"""Plan command routing (18 §10.1, TDD): the two-switch command matrix.

``/plan`` enters read-only plan mode; ``/plan <topic>`` runs one ordinary
planning turn whose answer is a ``<proposed_plan>`` block; ``/plan off``
disarms both switches; ``/plan run`` executes via ``CodexPlanRunner``;
``/readonly on|off`` owns the enforcement axis alone.  The old one-shot
contract (bare ``/plan`` asks a topic, then plans AND executes) is gone: a
conversational agreement approves nothing, and execution is an explicit
command.
"""

import pytest
from rich.console import Console

from ai_coding.app.commands import REPLAction, handle_input
from ai_coding.app.console import DialogueConsole
from ai_coding.core.turn_policy import TurnPolicy, TurnPolicyConfig
from ai_coding.domain.session import SessionData
from ai_coding.domain.stance import PostureResult


class _Loop:
    """A posture-aware stub mirroring AgentLoop.request_posture's two halves."""

    def __init__(self, *, busy: bool = False) -> None:
        self.calls: list[str] = []
        self.policy = TurnPolicy(TurnPolicyConfig(max_auto_continuations=3))
        self.busy = busy

    @property
    def turn_running(self) -> bool:
        return self.busy

    def request_posture(
        self, session, *, agent=None, readonly=None, reason="", observer=None
    ):
        target = (
            agent if agent is not None else session.agent,
            bool(readonly) if readonly is not None else session.readonly,
        )
        if target == (session.agent, session.readonly):
            if session.pending_posture:
                session.pending_posture = None
                return PostureResult.CANCELLED
            return PostureResult.NOOP
        if self.busy:
            session.pending_posture = {
                "agent": target[0],
                "readonly": target[1],
                "reason": reason,
            }
            return PostureResult.QUEUED
        self.commit_posture(
            session, agent=target[0], readonly=target[1], reason=reason, observer=observer
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
    session.agent, session.readonly = "plan", True
    dc, c = _console()

    await handle_input(loop, "/plan", session, sessions, dc)

    assert sessions.saved == []
    assert loop.calls == []
    assert "已在计划模式" in c.export_text()


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
async def test_plan_topic_starts_a_fresh_book_and_saves():
    """A new plan starts its own book (goal, items, posture) and is persisted."""
    session = _session()
    loop = _Loop()
    sessions = _Sessions()
    dc, c = _console()

    action, _ = await handle_input(loop, "/plan 新计划", session, sessions, dc)

    assert action is REPLAction.CONTINUE
    assert session.task.goal == "新计划"
    assert (session.mode, session.readonly) == ("plan", True)
    assert sessions.saved == [session]
    # 19 §3.5: readiness is announced by present_plan's review card now — a
    # finished planning turn no longer auto-claims "计划就绪" (measured: a
    # budget-dead half-plan said it anyway).
    assert "计划就绪" not in c.export_text()


@pytest.mark.asyncio
async def test_a_plan_topic_runs_one_planning_turn_with_the_humans_words():
    """C-2: the planning round is the human's own words, through the one front
    door -- no synthetic instruction and no automatic second turn."""
    loop = _Loop()
    session = _session()
    dc, _ = _console()

    await handle_input(loop, "/plan 新计划", session, _Sessions(), dc)

    assert loop.calls == ["新计划"]
    assert (session.mode, session.readonly) == ("plan", True)


@pytest.mark.asyncio
async def test_plan_off_double_disarms_and_keeps_the_hint_slash_free():
    loop = _Loop()
    sessions = _Sessions()
    session = _session()
    session.agent, session.readonly = "plan", True
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
    session.agent, session.readonly = "plan", True
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
async def test_readonly_off_is_refused_in_plan_mode():
    """P1a: plan fixes the enforcement axis.  ``/readonly off`` would leave
    ``mode=plan`` writable, so it is refused without touching any field."""
    loop = _Loop()
    sessions = _Sessions()
    session = _session()
    session.agent, session.readonly = "plan", True
    dc, c = _console()

    action, _ = await handle_input(loop, "/readonly off", session, sessions, dc)

    assert action is REPLAction.CONTINUE
    assert (session.mode, session.readonly) == ("plan", True)
    assert sessions.saved == []
    assert "计划模式下已是只读" in c.export_text()


@pytest.mark.asyncio
async def test_readonly_off_does_not_withdraw_a_queued_plan():
    """The queued plan is the other way plan pins the axis.  Routing the
    refusal through ``request_posture`` would answer CANCELLED and silently
    drop the ``/plan`` the user asked for."""
    loop = _Loop(busy=True)
    sessions = _Sessions()
    session = _session()
    dc, c = _console()

    await handle_input(loop, "/plan", session, sessions, dc)
    queued = dict(session.pending_posture)

    await handle_input(loop, "/readonly off", session, sessions, dc)

    assert session.pending_posture == queued
    assert (session.mode, session.readonly) == ("normal", False)
    out = c.export_text()
    assert "进入计划模式已排队" in out
    assert "已撤销排队中的切换" not in out


@pytest.mark.asyncio
async def test_readonly_off_still_works_in_build_mode():
    loop = _Loop()
    sessions = _Sessions()
    session = _session()
    session.readonly = True
    dc, c = _console()

    await handle_input(loop, "/readonly off", session, sessions, dc)

    assert session.readonly is False
    assert sessions.saved == [session]
    assert "只读已关闭" in c.export_text()


@pytest.mark.asyncio
async def test_a_pending_entry_without_an_agent_is_not_a_queued_plan():
    """Only an explicit plan target pins the axis: a pending flip on the
    readonly axis alone must not be refused as if plan were queued."""
    loop = _Loop(busy=True)
    sessions = _Sessions()
    session = _session()
    session.pending_posture = {
        "agent": None,
        "readonly": True,
        "reason": "/readonly on",
    }
    dc, c = _console()

    await handle_input(loop, "/readonly off", session, sessions, dc)

    out = c.export_text()
    assert "计划模式下已是只读" not in out
    assert session.pending_posture is None


@pytest.mark.asyncio
async def test_plan_off_restores_the_writable_overlay_after_a_refusal():
    """The refusal points at ``/plan off``; that has to actually work."""
    loop = _Loop()
    sessions = _Sessions()
    session = _session()
    session.agent, session.readonly = "plan", True
    dc, _c = _console()

    await handle_input(loop, "/readonly off", session, sessions, dc)
    await handle_input(loop, "/plan off", session, sessions, dc)

    assert (session.mode, session.readonly) == ("normal", False)


@pytest.mark.asyncio
async def test_posture_commands_queue_while_a_turn_is_running():
    """18 §10.2: mid-turn posture flips queue with an explicit receipt."""
    loop = _Loop(busy=True)
    sessions = _Sessions()
    session = _session()
    dc, c = _console()

    await handle_input(loop, "/plan", session, sessions, dc)

    assert (session.mode, session.readonly) == ("normal", False)
    assert session.pending_posture == {
        "agent": "plan",
        "readonly": True,
        "reason": "/plan",
    }
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
    assert session.pending_posture == {
        "agent": "plan",
        "readonly": True,
        "reason": "/plan",
    }
    out = c.export_text()
    assert "直接描述计划主题" in out and "将在当前回合结束后生效" in out


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
