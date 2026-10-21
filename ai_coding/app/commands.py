"""Interactive REPL commands (M5-2, migrated from ``ui/repl.py`` in M6).

The testable core — :func:`handle_input` and the :class:`REPLAction` decision —
stays import-light and offline; the OpenTUI frontend drives it over IPC.
"""

from __future__ import annotations

import time
from enum import Enum
from pathlib import Path
from typing import Any

from ai_coding.app.console import ConsoleSink
from ai_coding.app.slash import parse_slash, suggest_command
from ai_coding.domain.loop import TurnStatus
from ai_coding.skills.catalog import build_catalog

HELP_TEXT = (
    "direct commands:\n"
    "  /help, /h       show this help\n"
    "  /status         show session info\n"
    "  /model [name]   list or switch models\n"
    "  /resume [id]    list or resume saved sessions\n"
    "  /permissions   view or change approval policy\n"
    "  /todo           show current task list\n"
    "  /goal [obj|resume|off] self-driving goal (resume=continue same goal, off=stop)\n"
    "  /tasks          list background sub-agent runs\n"
    "  /skills         list available skills\n"
    "  /review        review current changes\n"
    "  /config         show effective configuration\n"
    "  /compact        compact conversation history\n"
    "  /undo           roll the last turn back (history + files)\n"
    "  /redo           put back the turn /undo removed\n"
    "  /memory         show memory status\n"
    "  /trace          show last turn trace\n"
    "  /budget         show session token totals\n"
    "  /clear          start a fresh session\n"
    "  /quit, /q       exit\n"
    "  ctrl-y          copy last tool output to clipboard"
)


class REPLAction(Enum):
    """What one input line should trigger in the REPL loop."""

    CONTINUE = "continue"
    QUIT = "quit"
    NEW_SESSION = "new_session"
    #: The command rewrote the *same* session's history (/undo, /redo): the
    #: client must re-project it.  Distinct from NEW_SESSION, where the live
    #: session itself was switched.
    RELOAD = "reload"


#: Why an undo or redo moved nothing, in the words the user gets.  r17 reported
#: an empty file list, which reads the same as "nothing had changed" (r18/F8).
_REASON_TEXT = {
    "a turn is running": "回合进行中，等这一轮结束再试",
    "nothing to undo": "没有可撤销的回合",
    "nothing to redo": "没有可恢复的回合",
    "no-snapshot": "本回合没有可用快照（工作区不是 git 仓库，或捕获失败），文件未还原",
}


def _reason_text(reason: str) -> str:
    """The sentence for a refusal; unknown reasons keep their own text."""
    base = reason.split(":", 1)[0]
    return _REASON_TEXT.get(base, reason or "failed")


def _undo_outcome(report: dict[str, Any]) -> str:
    """The file half of an undo, said plainly either way."""
    reason = str(report.get("reason") or "")
    if not reason:
        return f"还原 {len(report.get('files') or [])} 个文件"
    return _reason_text(reason)


async def handle_input(
    loop: Any,
    text: str,
    session: Any,
    sessions: Any,
    console: ConsoleSink,
    observer: Any = None,
) -> tuple[REPLAction, Any]:
    """Process a single input line. Plain text runs the loop; slash reacts.

    Returns ``(action, session)`` where ``session`` may be a fresh one when a
    new session was started.  ``sessions`` only needs ``create()``.

    ``observer`` is threaded into every turn this dispatches runs (the plain
    path, ``/review``, the plan workflow's step turns): a front-end that sees
    only deltas cannot render tool cards or turn ends.  ``None`` keeps the
    REPL's console-only behaviour.
    """
    cmd = parse_slash(text)
    if cmd is None:
        stripped = text.strip()
        if stripped.startswith("/"):
            token = stripped.split(None, 1)[0].lower()
            suggestion = suggest_command(token)
            if suggestion:
                console.error(
                    f"unknown command '{token}' — did you mean '/{suggestion}'?"
                )
                return REPLAction.CONTINUE, session
        answer = await run_human_turn(
            loop, session, text, console=console,
            on_delta=console.delta, observer=observer,
        )
        if answer and not answer.endswith("\n"):
            console.rule("")
        return REPLAction.CONTINUE, session

    if cmd.name == "quit":
        return REPLAction.QUIT, session
    if cmd.name == "clear":
        new_session = sessions.create()
        console.info("[clear] started a new session")
        return REPLAction.NEW_SESSION, new_session
    if cmd.name == "help":
        console.info(HELP_TEXT)
        return REPLAction.CONTINUE, session
    if cmd.name == "status":
        n = len(getattr(session, "messages", []))
        sid = getattr(session, "session_id", "?")
        extra = ""
        status = getattr(loop, "memory_status", None)
        if callable(status):
            try:
                stats = status()
                extra = f" memory={stats.get('total', 0)}/{stats.get('by_kind', {})}"
            except Exception:  # noqa: BLE001 — a UI helper must not break the REPL
                extra = ""
        console.info(f"[status] session={sid} messages={n}{extra}")
        return REPLAction.CONTINUE, session
    if cmd.name == "compact":
        compact = getattr(loop, "compact_session", None)
        if compact is None:
            console.info("[compact] not supported by this loop")
            return REPLAction.CONTINUE, session
        try:
            report = await compact(session)
        except Exception as exc:  # noqa: BLE001 — never break the REPL
            console.error(f"[compact] failed: {exc}")
            return REPLAction.CONTINUE, session
        if report.get("removed"):
            # Success already has its row (── 压缩 ── + counts + usage):
            # a second line saying the same thing reads as a second
            # compaction.  Silence here; failure/no-change still reports.
            if getattr(sessions, "save", None) is not None:
                sessions.save(session)
            return REPLAction.CONTINUE, session
        console.info(
            f"[compact] messages {report.get('messages')} → {report.get('after')} "
            f"(removed {report.get('removed')})"
        )
        if getattr(sessions, "save", None) is not None:
            sessions.save(session)
        return REPLAction.CONTINUE, session
    if cmd.name == "undo":
        undo = getattr(loop, "undo_last_turn", None)
        if undo is None:
            console.info("[undo] not supported by this loop")
            return REPLAction.CONTINUE, session
        report = await undo(session)
        if not report.get("ok"):
            console.info("[undo] " + _reason_text(report.get("reason", "")))
            return REPLAction.CONTINUE, session
        console.info(
            f"[undo] {report['messages_before']} → {report['messages_after']} 条，"
            + _undo_outcome(report)
        )
        return REPLAction.RELOAD, session
    if cmd.name == "redo":
        redo = getattr(loop, "redo_last_turn", None)
        if redo is None:
            console.info("[redo] not supported by this loop")
            return REPLAction.CONTINUE, session
        report = await redo(session)
        if report.get("ok"):
            console.info(f"[redo] 已恢复上一回合，还原 {len(report.get('files') or [])} 个文件")
            return REPLAction.RELOAD, session
        console.info("[redo] " + _reason_text(report.get("reason", "")))
        return REPLAction.CONTINUE, session
    if cmd.name == "memory":
        console.info(_memory_report(loop))
        return REPLAction.CONTINUE, session
    if cmd.name == "trace":
        console.info(_trace_report(loop))
        return REPLAction.CONTINUE, session
    if cmd.name == "stats":
        console.info(_stats_report(loop))
        return REPLAction.CONTINUE, session
    if cmd.name == "export":
        return _handle_export(loop, cmd.raw, session, console)
    if cmd.name == "budget":
        console.info(_budget_report(loop, session))
        return REPLAction.CONTINUE, session
    if cmd.name == "model":
        return _handle_model(loop, cmd.raw, session, console)
    if cmd.name == "resume":
        return _handle_resume(loop, cmd.raw, session, sessions, console)
    if cmd.name == "permissions":
        return _handle_permissions(loop, cmd.raw, session, console)
    if cmd.name == "todo":
        task = getattr(session, "task", None)
        if task is None:
            console.info("[todo] no task list for this session")
        else:
            if observer is not None:
                from ai_coding.core.events import TaskUpdated

                try:
                    observer.on_event(TaskUpdated(task=task))
                except Exception:  # noqa: BLE001 — observation only
                    pass
            console.info(task.render_display())
        return REPLAction.CONTINUE, session
    if cmd.name == "tasks":
        return _handle_tasks(loop, cmd.raw, session, console)
    if cmd.name == "skills":
        skills_reg = getattr(loop, "skills", None)
        if skills_reg is None:
            console.info("[skills] not available in this session")
            return REPLAction.CONTINUE, session
        catalog = build_catalog(skills_reg.all())
        if catalog:
            console.info(f"[skills] available:\n{catalog}")
        else:
            console.info("[skills] no skills found")
        return REPLAction.CONTINUE, session
    if cmd.name == "plan":
        return await _handle_plan(loop, session, sessions, cmd.raw, console, observer)
    if cmd.name == "goal":
        return await _handle_goal(loop, session, sessions, cmd.raw, console, observer)
    if cmd.name == "readonly":
        return _handle_readonly(loop, session, sessions, cmd.raw, console)
    if cmd.name == "review":
        console.info("[review] reviewing current changes...")
        await loop.process_input(
            session,
            "Review the current git diff and identify any issues, "
            "bugs, or improvements needed.",
            on_delta=console.delta,
            observer=observer,
        )
        return REPLAction.CONTINUE, session
    if cmd.name == "config":
        return _handle_config(loop, session, console)
    return REPLAction.CONTINUE, session


def _handle_config(loop: Any, session: Any, console: ConsoleSink) -> tuple[REPLAction, Any]:
    settings = getattr(loop, "settings", None)
    if settings is None:
        console.info("[config] not available")
        return REPLAction.CONTINUE, session
    lines = [
        f"[config] model: {getattr(loop, 'default_model', '') or '?'}",
        f"  max_context_tokens: {getattr(settings, 'max_context_tokens', '?')}",
        f"  max_tool_iterations: {getattr(settings, 'max_tool_iterations', '?')}",
        f"  stop_policy: {getattr(settings, 'stop_policy', '?')}",
        f"  turn_timeout: {getattr(settings, 'turn_timeout_seconds', '?')}s",
    ]
    console.info("\n".join(lines))
    return REPLAction.CONTINUE, session


#: 18 §10.2: every posture receipt written during a running turn ends with this.
_QUEUED = "（将在当前回合结束后生效）"


def _save_session(sessions: Any, session: Any) -> None:
    """Persist a posture flip; a ``sessions`` without ``save`` (stubs) is fine."""
    save = getattr(sessions, "save", None)
    if save is not None:
        save(session)


def _goal_budget_warning(loop: Any, task: Any) -> str:
    """One honest line when the episode's budgets are already spent (G1/G2).

    Resume still runs -- the user asked, and one bounded round is theirs to
    spend -- but the run will stop again almost immediately, and saying so
    before the round is cheaper than the report after it.
    """
    settings = getattr(loop, "settings", None)

    def _int(name: str, default: int) -> int:
        try:
            return int(getattr(settings, name, default) or default)
        except (TypeError, ValueError):
            return default

    spent: list[str] = []
    rounds, tokens = int(getattr(task, "rounds_run", 0) or 0), int(
        getattr(task, "tokens_used", 0) or 0
    )
    cap = _int("goal_max_rounds", 20)
    budget = _int("goal_token_budget", 0)
    if 0 < cap <= rounds:
        spent.append(f"轮数 {rounds}/{cap}")
    if budget > 0 and tokens >= budget:
        spent.append(f"token {tokens:,}/{budget:,}")
    if not spent:
        return ""
    return (
        f"[goal] 注意：{'、'.join(spent)} 已用尽——本次继续会立刻/一轮后停下；"
        "调大配置后再来。"
    )


async def _handle_goal(
    loop: Any, session: Any, sessions: Any, raw: str,
    console: ConsoleSink, observer: Any,
) -> tuple[REPLAction, Any]:
    """``/goal [objective|resume|off]`` — the human entry to a self-driving goal (D-4/G2).

    ``/goal <objective>`` is the human spelling of ``start_goal``: it arms the
    same episode (one place: ``TaskState.arm_goal``) and drives the same
    ``GoalRunner`` — no planning gate, so it works even when the model never
    proposed one.  ``resume`` continues the same episode (counters kept; a
    completed goal is terminal and refused); ``off`` disarms (the same seam
    as esc); ``pause/clear/status`` are reserved redirects so a control word
    can never silently become an objective.  A bare ``/goal`` reports status.
    The run blocks here like ``/plan run`` does, so a command turn is one
    envelope with a single end.
    """
    from ai_coding.core.goal_runner import GoalRunner
    from ai_coding.domain.task import TaskState

    task = getattr(session, "task", None)
    rest = raw[len("/goal"):].strip()

    if rest.lower() == "off":
        if task is not None and task.disarm():
            _save_session(sessions, session)
            # G2: the off path emits too -- an idle stop must clear the
            # sidebar's goal line without waiting for another run's arm.
            if observer is not None:
                from ai_coding.core.events import TaskUpdated

                try:
                    observer.on_event(TaskUpdated(task=task))
                except Exception:  # noqa: BLE001 — observation only
                    pass
            console.info(
                "[goal] 已停止；目标与账目留在会话里——继续：/goal resume；"
                "换目标：/goal <objective>。"
            )
        else:
            console.info("[goal] 当前没有在跑的任务。")
        return REPLAction.CONTINUE, session

    if rest.lower() == "resume":
        # G2: continue the SAME episode -- counters (rounds/account/resumes)
        # are not touched; that is the whole difference from /goal <objective>.
        if task is None or not getattr(task, "goal", ""):
            console.info("[goal] 没有可续跑的目标（未立案）。立案：/goal <objective>。")
            return REPLAction.CONTINUE, session
        if getattr(task, "armed", False):
            console.info(
                f"[goal] 已在跑（profile={task.armed_profile or '?'}）；"
                "停止用 esc / /goal off。"
            )
            return REPLAction.CONTINUE, session
        if session.mode == "plan" or getattr(session, "readonly", False):
            console.info("[goal] 计划模式（只读）中不能续跑——先 /plan off 再试。")
            return REPLAction.CONTINUE, session
        if str(getattr(task, "goal_phase", "")) == "complete":
            console.info(
                "[goal] 目标已申报完成（终态，不可续跑）；重新立案用 /goal <objective>。"
            )
            return REPLAction.CONTINUE, session
        # A blocked claim resumes as a fresh attempt; leaving it in place
        # would stop the first round the moment it ends (should_stop reads
        # the stale phase).
        task.goal_phase = ""
        warning = _goal_budget_warning(loop, task)
        if warning:
            console.info(warning)
        console.info("[goal] 继续本目标（不重置轮数与账目）。")
        await GoalRunner(loop).run(
            session, console, on_delta=console.delta, observer=observer
        )
        return REPLAction.CONTINUE, session

    if rest.lower() in ("pause", "clear", "status"):
        # Reserved so a control word can never silently become an objective
        # (the parse below arms a goal from any other non-empty rest).
        hint = {
            "pause": "停止用 /goal off（之后可 /goal resume 继续）。",
            "clear": "没有 clear 子命令——重新立案（/goal <objective>）会换成新目标。",
            "status": "直接输入 /goal 查看状态。",
        }[rest.lower()]
        console.info(f"[goal] {hint}")
        return REPLAction.CONTINUE, session

    if not rest:
        if task is None or not (task.goal or task.armed):
            console.info("[goal] 未立案。用法：/goal <objective> 立案；/goal off 停止。")
        else:
            console.info(
                f"[goal] goal={task.goal!r} · armed={task.armed} · "
                f"profile={task.armed_profile or '-'} · rounds={task.rounds_run} · "
                f"tokens={task.tokens_used} · phase={task.goal_phase or '-'}"
            )
        return REPLAction.CONTINUE, session

    if session.mode == "plan" or getattr(session, "readonly", False):
        console.info("[goal] 计划模式（只读）中不能立案——先 /plan off 再试。")
        return REPLAction.CONTINUE, session
    if task is not None and getattr(task, "armed", False):
        console.info(
            f"[goal] 已有在跑的任务（profile={task.armed_profile or '?'}）；"
            "先停止（esc / /goal off）。"
        )
        return REPLAction.CONTINUE, session
    if task is None:
        session.task = TaskState()
        task = session.task
    task.arm_goal(rest)
    _save_session(sessions, session)
    console.info(f"[goal] 已立案：{rest}（每轮自动续跑；停止：esc / /goal off）。")
    await GoalRunner(loop).run(
        session, console, on_delta=console.delta, observer=observer
    )
    return REPLAction.CONTINUE, session


async def _handle_plan(
    loop: Any, session: Any, sessions: Any, raw: str,
    console: ConsoleSink, observer: Any,
) -> tuple[REPLAction, Any]:
    """``/plan [topic|off|run]`` — the command matrix of 18 §10.1.

    Bare enters (mode=plan + readonly), a topic plans through ``plan_only``,
    ``off`` double-disarms, ``run`` double-disarms when needed and executes
    through ``PlanRunner`` (step sessions are fresh SessionData — normal and
    writable by construction).  A flip requested mid-turn is queued by
    ``loop.request_posture`` and the receipt says so (18 §10.2); a mid-turn
    topic never starts a planning turn.
    """
    from ai_coding.core.plan_runner import PlanRunner, plan_only

    rest = raw[len("/plan"):].strip()
    sub = rest.lower()

    if sub == "off":
        was_plan, was_readonly = session.mode == "plan", bool(session.readonly)
        if not loop.request_posture(session, mode="normal", readonly=False):
            console.info(f"[plan] 退出计划模式已排队{_QUEUED}。")
            return REPLAction.CONTINUE, session
        # D1 (20 §批次 4a): this is the user's explicit "give this plan up".
        #
        # An approved plan the run never took is a dead end without it: both
        # refusals point here ("已有一份批准的计划尚未执行；先执行它（/plan
        # run），或用 /plan off 放弃后再重新规划"), and until now the command
        # only flipped the posture -- so the user did what the error said and
        # hit the same wall again.
        #
        # Only the plan profile goes: a goal is another line, with its own
        # exit ("/goal off"), and the two must not step on each other.  The
        # arm that is running is not touched either -- a command that lands
        # while a turn is busy is queued (the posture flip above), and by the
        # time it lands the runner has already disarmed on its way out.
        task = getattr(session, "task", None)
        gave_up = False
        if (
            task is not None
            and getattr(task, "armed", False)
            and getattr(task, "armed_profile", "") == "plan"
        ):
            gave_up = bool(task.disarm())
        _save_session(sessions, session)
        if gave_up and task is not None:
            # R06: the wire, too -- otherwise the front end keeps a card for
            # a plan the user just gave up.
            if observer is not None:
                from ai_coding.core.events import TaskUpdated

                try:
                    observer.on_event(TaskUpdated(task=task))
                except Exception:  # noqa: BLE001 — observation only
                    pass
        if was_plan:
            msg = "[plan] 已退出计划模式（只读已关闭）。"
        elif was_readonly:
            msg = "[plan] 只读已关闭（本就不在计划模式）。"
        else:
            msg = "[plan] 本就不在计划模式；只读开关也确认关闭。"
        if gave_up:
            # The old hint said "you can continue" -- which contradicts
            # giving the plan up: a new plan starts a fresh book.
            msg += " 已放弃这份批准的计划，可以重新规划。"
        else:
            open_n = len(session.task.open_items()) if getattr(session, "task", None) else 0
            if open_n:
                msg += f" 还有 {open_n} 项未执行（重新进入 Plan 模式可以继续）。"
        console.info(msg)
        return REPLAction.CONTINUE, session

    # R03: a task another profile owns keeps the whole /plan family out --
    # /plan <topic> would clear the goal via begin_plan, /plan run would
    # rewrite armed_profile to "plan" and execute; bare entry would just
    # paint a read-only canvas whose submissions get refused anyway.  off
    # above stays available as the safety valve.
    armed_task = getattr(session, "task", None)
    if (
        armed_task is not None
        and getattr(armed_task, "armed", False)
        and getattr(armed_task, "armed_profile", "") != "plan"
    ):
        console.info(
            f"[plan] 已有任务在跑（profile={armed_task.armed_profile or '?'}）；"
            "先 /goal off 或等它结束，再进计划模式。"
        )
        return REPLAction.CONTINUE, session

    if sub == "run":
        needs_exit = session.mode == "plan" or session.readonly
        if needs_exit:
            if loop.request_posture(session, mode="normal", readonly=False):
                _save_session(sessions, session)
                console.info("[plan] 已退出计划模式（只读已关闭）；开始执行计划。")
            else:
                # stage 4 r2: the dispatch often lands inside the closing
                # turn's lock, so the flip queues onto the MAIN session --
                # but the run drives fresh step sessions, and the queue would
                # sit until the next user turn (measured: mode stayed plan on
                # disk, c41e461's tmux run).  The finally below lands it; the
                # receipt says what will happen rather than the old
                # "当前回合结束后生效" (that turn was already over).
                console.info("[plan] 开始执行计划；执行完成后退出计划模式（只读）。")
        try:
            report = await PlanRunner(loop).run(
                session, console, on_delta=console.delta, observer=observer
            )
            # W2/R02: the run branch used to drop the report, so a plan the
            # runner refused (a step naming a path the file tools cannot
            # reach) produced no message at all -- the command returned as if
            # it had run.  The runner's own render only prints when it got as
            # far as the driver, so the early refusals need this line.
            if report.stopped:
                console.error(f"[plan] {report.stopped}")
        finally:
            # stage 4 r2, boundary semantics (dsh's onBoundary): the run's
            # end IS the boundary, so the flip lands here directly instead of
            # racing request_posture's queue.  The queue measured broken for
            # this caller: when the dispatch arrives inside the closing
            # turn's lock the flip parks on the MAIN session, the run only
            # drives fresh step sessions, and if no later user turn ever
            # comes the queue never lands (c41e461: mode stayed plan on
            # disk; the probe run confirmed the parked queue only lands at
            # the NEXT turn's entry).  Writing through is safe by intent --
            # approval promised the exit, and a concurrently running turn
            # (a rare user message in the same instant) reads postures only
            # at assembly/dispatch time anyway.
            if needs_exit:
                session.pending_posture = None
                session.mode, session.readonly = "normal", False
                _save_session(sessions, session)
        return REPLAction.CONTINUE, session

    already = session.mode == "plan" and session.readonly
    if not already:
        if not loop.request_posture(session, mode="plan", readonly=True):
            if rest:
                console.info(
                    f"[plan] 进入计划模式（只读）已排队{_QUEUED}；"
                    "回合结束后直接描述计划主题即可。"
                )
            else:
                console.info(f"[plan] 进入计划模式（只读）已排队{_QUEUED}。")
            return REPLAction.CONTINUE, session
        _save_session(sessions, session)
        console.info(
            "[plan] 已进入计划模式（只读）。继续对话写计划；"
            "模型完成规划后会弹出评审卡，批准即自动执行；shift+tab 返回 Build。"
        )
    if not rest:
        if already:
            console.info(
                "[plan] 已在计划模式（只读）。继续对话写计划；评审卡批准后自动执行。"
            )
        return REPLAction.CONTINUE, session
    if already and loop.turn_running:
        console.info("[plan] 已在计划模式；回合结束后直接描述主题即可。")
        return REPLAction.CONTINUE, session

    report = await plan_only(
        loop, session, topic=rest, on_delta=console.delta, observer=observer
    )
    if report.stopped:
        console.error(f"[plan] {report.stopped}")
        return REPLAction.CONTINUE, session
    # 19 §3.5: readiness belongs to present_plan's review card now.  A planning
    # turn that ran out of iterations is NOT a ready plan (measured: a dead
    # round printed "计划就绪：1 项" over a half-written list).
    outcome = getattr(loop, "last_outcome", None)
    if getattr(outcome, "status", None) is TurnStatus.MAX_ITERATIONS:
        console.info(
            "[plan] 规划未完成（预算耗尽）：可继续对话补充，或重新描述需求后再次规划。"
        )
    return REPLAction.CONTINUE, session


async def run_human_turn(
    loop: Any,
    session: Any,
    text: str,
    *,
    console: ConsoleSink,
    on_delta: Any = None,
    observer: Any = None,
    images: tuple[Any, ...] = (),
) -> str:
    """Run one human utterance as the turn it deserves.

    In plan mode a human utterance is a *planning round* (19 §4): the words
    are the topic, the model is asked for a plan, and the plan is what gets
    reviewed.  Everywhere else it is an ordinary turn.

    One function for both front doors -- the TUI's prompt and the REPL --
    because the two drifting apart is what made plan mode a read-only flag
    instead of a planning mode: the TUI sent the words as an ordinary
    prompt, so the model answered instead of planning, and the guard could
    only ask for a plan after the work was already done.
    """
    if getattr(session, "mode", "normal") != "plan":
        # The images are offered only when there is something to offer, so
        # loop doubles that predate r17/T5 stay callable for text turns.
        answer: str = await loop.process_input(
            session, text, on_delta=on_delta, observer=observer,
            **({"images": images} if images else {}),
        )
        return answer
    from ai_coding.core.plan_runner import plan_only

    report = await plan_only(
        loop, session, topic=text,
        on_delta=on_delta, observer=observer, images=images,
    )
    if report.stopped:
        console.error(f"[plan] {report.stopped}")
    return ""


def _handle_readonly(
    loop: Any, session: Any, sessions: Any, raw: str, console: ConsoleSink
) -> tuple[REPLAction, Any]:
    """``/readonly on|off`` — the enforcement axis alone (18 §10.1)."""
    arg = raw[len("/readonly"):].strip().lower()
    if arg not in ("on", "off"):
        state = "开" if session.readonly else "关"
        line = f"[readonly] 用法：/readonly on|off（当前：{state}）"
        (console.error if arg else console.info)(line)
        return REPLAction.CONTINUE, session
    value = arg == "on"
    if not loop.request_posture(session, readonly=value):
        console.info(f"[readonly] {'开启' if value else '关闭'}已排队{_QUEUED}。")
        return REPLAction.CONTINUE, session
    _save_session(sessions, session)
    if value:
        console.info("[readonly] 只读已开启：写/执行类工具将被拒绝；关闭：/readonly off。")
    else:
        console.info("[readonly] 只读已关闭。")
    return REPLAction.CONTINUE, session


def _handle_permissions(
    loop: Any, raw: str, session: Any, console: ConsoleSink
) -> tuple[REPLAction, Any]:
    parts = raw.split()
    dispatcher = getattr(loop, "dispatcher", None)
    approval = getattr(dispatcher, "approval", None) if dispatcher else None
    gate = getattr(dispatcher, "gate", None)
    if len(parts) == 1:
        name = type(approval).__name__ if approval else "(none)"
        lines = [f"[permissions] current approval: {name}"]
        # G4b: a standing grant outlives the process, so it has to be visible --
        # an authorisation the operator cannot inspect is one they cannot
        # consent to.  The path is shown so it can be deleted by hand too.
        grants = list(getattr(gate, "grants", ()) or ())
        lines.append(f"[permissions] remembered ({len(grants)}):")
        if grants:
            lines.extend(f"  {key}" for key in grants)
        else:
            lines.append("  (none)")
        store = getattr(gate, "_store", None)
        if store is not None:
            lines.append(f"[permissions] stored at: {store.path}")
        lines.append("[permissions] /permissions clear removes them")
        console.info("\n".join(lines))
        return REPLAction.CONTINUE, session
    target = parts[1]
    if target == "clear":
        if gate is not None:
            gate.revoke_all()
        console.info("[permissions] cleared remembered grants for this workspace")
        return REPLAction.CONTINUE, session
    from ai_coding.security.approval import AutoApproveCallback, CLIApprovalCallback
    if target == "auto":
        if dispatcher:
            dispatcher.approval = AutoApproveCallback()
        console.info("[permissions] switched to auto-approve")
    elif target == "confirm":
        if dispatcher:
            dispatcher.approval = CLIApprovalCallback()
        console.info("[permissions] switched to confirm-all")
    else:
        console.error(f"[permissions] unknown policy '{target}'. Available: auto, confirm, clear")
    return REPLAction.CONTINUE, session


def _message_count(session: Any) -> int:
    """How many messages a session holds.

    A listed session has had its bodies dropped but keeps ``message_count``;
    a loaded one reports -1 there and still has the bodies, so counting those is
    both correct and free.  Anything else (a test double) falls back to the same
    rule rather than to a hard zero.
    """
    count = getattr(session, "message_count", -1)
    if isinstance(count, int) and count >= 0:
        return count
    return len(getattr(session, "messages", []))


def _handle_resume(
    loop: Any, raw: str, session: Any, sessions: Any, console: ConsoleSink
) -> tuple[REPLAction, Any]:
    parts = raw.split()
    if len(parts) == 1:
        items = sessions.list_sessions()
        if not items:
            console.info("[resume] no saved sessions")
            return REPLAction.CONTINUE, session
        lines = ["[resume] saved sessions:"]
        for s in items:
            sid = getattr(s, "session_id", "?")
            title = getattr(s, "title", "")
            n = _message_count(s)
            lines.append(f"  {sid[:12]}  msgs={n}  {title}")
        console.info("\n".join(lines))
        return REPLAction.CONTINUE, session
    target_id = parts[1]
    # The list prints 12 hex chars, so a resume has to accept that back.  An
    # ambiguous prefix is refused with the candidates rather than resolved to
    # whichever happened to be read first.
    matches = sessions.resolve(target_id)
    if len(matches) == 0:
        console.error(
            f"[resume] session '{target_id}' not found — use the id shown by /resume"
        )
        return REPLAction.CONTINUE, session
    if len(matches) > 1:
        listed = ", ".join(sorted(s.session_id[:12] for s in matches))
        console.error(f"[resume] '{target_id}' matches {len(matches)} sessions: {listed}")
        return REPLAction.CONTINUE, session
    # ``resolve`` answers from the stripped list view (message bodies dropped
    # for the list), so a resume must follow it with a full ``load`` — handing
    # the stripped row back would open a session with zero messages, and the
    # next save would wipe the conversation it just opened.
    loaded = sessions.load(matches[0].session_id)
    if loaded is None:
        loaded = matches[0]
    console.info(f"[resume] loaded session {loaded.session_id[:12]}")
    return REPLAction.NEW_SESSION, loaded


def _runner_of(loop: Any) -> Any | None:
    """The sub-agent runner behind this loop's tool stack, if any."""
    dispatcher = getattr(loop, "dispatcher", None)
    registry = getattr(dispatcher, "registry", None)
    tool = registry.get("subAgent") if registry is not None else None
    return getattr(tool, "runner", None)


def _print_jobs(loop: Any, console: ConsoleSink) -> None:
    """The workspace's background shell jobs, in the same table shape.

    Same rows ``job_list`` gives the model, so what the operator sees and what
    the model reads cannot drift into two vocabularies.
    """
    registry = getattr(loop, "jobs", None)
    if registry is None:
        return
    rows = registry.snapshot()
    if not rows:
        console.info("[tasks] no background shell jobs")
        return
    lines = ["[tasks] background shell jobs:"]
    for row in rows:
        origin = "inherited" if row["adopted"] else "this session"
        detail = f" · {row['detail']}" if row["detail"] else ""
        lines.append(
            f"  {row['id']}  {row['status']:<9} pid {row['pid']}  [{origin}]{detail}"
            f"\n      {row['command']}"
        )
    console.info("\n".join(lines))


def _handle_tasks(
    loop: Any, raw: str, session: Any, console: ConsoleSink
) -> tuple[REPLAction, Any]:
    """List background work, or stop one piece of it (R1-2, D-1b).

    Two kinds share the command because they share the question: sub-agent runs
    and background shell jobs.  A job id is tried first -- ``job_`` cannot be
    confused with a run id, and the panel stops jobs through this same path so
    there is one way to stop a thing rather than two.
    """
    parts = raw.split()
    if len(parts) >= 3 and parts[1] == "stop":
        jobs = getattr(loop, "jobs", None)
        if jobs is not None and parts[2].startswith("job_"):
            try:
                job = jobs.kill(parts[2], reason="stopped from /tasks")
            except KeyError:
                console.info(f"[tasks] no background job '{parts[2]}'")
                return REPLAction.CONTINUE, session
            except Exception as exc:  # noqa: BLE001 - a refusal must say so
                console.error(f"[tasks] {exc}")
                return REPLAction.CONTINUE, session
            console.info(
                f"[tasks] job {job.id} is {job.status}"
                f"{f' ({job.detail})' if job.detail else ''}"
            )
            return REPLAction.CONTINUE, session
    runner = _runner_of(loop)
    if runner is None:
        _print_jobs(loop, console)
        console.info("[tasks] no sub-agent runner in this session")
        return REPLAction.CONTINUE, session
    if len(parts) >= 3 and parts[1] == "stop":
        given = parts[2]
        # The listing prints run_id[:12]; accept what was actually copyable.
        resolved = runner.resolve_run_id(given)
        if resolved is None:
            matches = [row for row in runner.task_list() if row.run_id.startswith(given)]
            if len(matches) > 1:
                listed = ", ".join(row.run_id[:12] for row in matches)
                console.error(f"[tasks] '{given}' is ambiguous: {listed}")
            else:
                console.info(f"[tasks] no background task '{given}'")
            return REPLAction.CONTINUE, session
        if runner.cancel(resolved):
            console.info(f"[tasks] stopping {resolved[:12]}")
        else:
            console.info(f"[tasks] '{given}' is not running")
        return REPLAction.CONTINUE, session
    rows = runner.task_list()
    if not rows:
        console.info("[tasks] no background runs")
        _print_jobs(loop, console)
        return REPLAction.CONTINUE, session
    now = time.monotonic()
    lines = ["[tasks] background sub-agents:"]
    for row in rows:
        elapsed = (row.ended if row.ended is not None else now) - row.started
        tokens = ""
        if row.usage:
            total = int(row.usage.get("total_tokens", 0) or 0)
            if not total:
                total = int(row.usage.get("prompt_tokens", 0) or 0) + int(
                    row.usage.get("completion_tokens", 0) or 0
                )
            tokens = f" · {total} tok"
        lines.append(
            f"  {row.run_id[:12]} {row.name} {row.status} {elapsed:.0f}s{tokens}"
        )
    console.info("\n".join(lines))
    _print_jobs(loop, console)
    return REPLAction.CONTINUE, session


def _handle_model(
    loop: Any, raw: str, session: Any, console: ConsoleSink
) -> tuple[REPLAction, Any]:
    # Read off the loop, not off ``settings``: the model table is an ``AppConfig``
    # field and ``loop.settings`` is an ``AIConfig``, which has never carried one.
    models = getattr(loop, "models", None) or {}
    default_model = getattr(loop, "default_model", "") or ""
    parts = raw.split()
    if len(parts) == 1:
        lines = [f"[model] current: {default_model or '(none)'}"]
        for name in models:
            marker = " *" if name == default_model else ""
            lines.append(f"  {name}{marker}")
        console.info("\n".join(lines))
        return REPLAction.CONTINUE, session
    target = parts[1]
    if target not in models:
        console.error(f"[model] unknown model '{target}'. Available: {', '.join(models)}")
        return REPLAction.CONTINUE, session
    set_model = getattr(loop, "set_model", None)
    if callable(set_model):
        try:
            set_model(target)
        except ValueError as exc:
            # A model can be configured and still not be servable: with a
            # fallback chain the engine only knows the models on that chain, and
            # switching to a third one would mean rebuilding it (G7).  The
            # command has to say so -- it used to raise out of the submit path,
            # where nothing catches it.
            console.error(f"[model] cannot switch to '{target}': {exc}")
            return REPLAction.CONTINUE, session
        console.info(f"[model] switched to {target}")
    else:
        console.error("[model] model switching not supported by this loop")
    return REPLAction.CONTINUE, session


def _stats_report(loop: Any) -> str:
    """Cross-session rollup (R9b's numbers) for ``/stats``.

    Read-only: every traced session is folded into one report, newest first.
    An empty store says so — a hollow table would read as a bug.
    """
    from ai_coding.core.aggregate import build_report

    recorder = getattr(loop, "trace", None)
    store = getattr(recorder, "store", None)
    if store is None:
        return "[stats] tracing is off for this session"
    report = build_report(store)
    if not report.rows:
        return "[stats] no traced sessions under this workspace yet"
    lines = ["[stats] sessions:"]
    for row in report.rows:
        lines.append(
            f"  {row.session[:12]}  turns={row.turns} steps={row.steps} "
            f"tok={row.prompt_tokens + row.completion_tokens} "
            f"cache={row.cache_ratio * 100:.0f}% age={row.age_days:.1f}d"
        )
    totals = report.totals
    lines.append(
        f"  (all)  sessions={len(report.rows)} turns={totals.turns} "
        f"steps={totals.steps} tok={totals.prompt_tokens + totals.completion_tokens} "
        f"cache={report.cache_ratio * 100:.0f}%"
    )
    return "\n".join(lines)


def _export_dir(loop: Any) -> Any:
    """Where exports land: inside the workspace, beside the traces (J4)."""
    workspace = str(getattr(loop, "workspace", "") or ".")
    return Path(workspace).expanduser().resolve() / ".ai-coding" / "exports"


def _handle_export(
    loop: Any, raw: str, session: Any, console: ConsoleSink
) -> tuple[REPLAction, Any]:
    """Write the traced sessions out as one HTML page or an OTLP/JSON file.

    ``/export html [session-id]`` renders the whole workspace index (or one
    session's page); ``/export otlp`` writes the OTLP/JSON structure.  The
    renderers are pure (J5: nothing here mutates a session), and the target is
    always inside the workspace (J4).
    """
    import json

    from ai_coding.core.aggregate import build_report
    from ai_coding.core.export_html import read_session_view, render_index_html, render_session_html
    from ai_coding.core.export_otlp import rows_to_otlp

    parts = raw.split()
    target = parts[1] if len(parts) >= 2 else ""
    if target not in {"html", "otlp"}:
        console.error(
            f"[export] unknown format '{target or '(none)'}'. Available: html, otlp"
        )
        return REPLAction.CONTINUE, session

    recorder = getattr(loop, "trace", None)
    store = getattr(recorder, "store", None)
    if store is None:
        console.info("[export] tracing is off for this session")
        return REPLAction.CONTINUE, session
    only = parts[2] if len(parts) >= 3 else ""

    try:
        if target == "html":
            if only:
                view = read_session_view(store, only)
                payload = render_session_html(view)
            else:
                report = build_report(store)
                if not report.rows:
                    console.info("[export] nothing traced yet under this workspace")
                    return REPLAction.CONTINUE, session
                views = [read_session_view(store, row.session) for row in report.rows]
                payload = render_index_html(report, views)
            suffix = "html"
        else:
            report = build_report(store)
            rows = [row.session for row in report.rows] if not only else [only]
            views = [read_session_view(store, sid) for sid in rows]
            if not views:
                console.info("[export] nothing traced yet under this workspace")
                return REPLAction.CONTINUE, session
            payload = json.dumps(rows_to_otlp(views), indent=2, ensure_ascii=False)
            suffix = "json"
    except Exception as exc:  # noqa: BLE001 — a failed export must not break the REPL
        console.error(f"[export] failed: {exc}")
        return REPLAction.CONTINUE, session

    directory = _export_dir(loop)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = directory / f"traces-{stamp}.{suffix}"
    path.write_text(payload, encoding="utf-8")
    console.info(f"[export] wrote {path}")
    return REPLAction.CONTINUE, session


def _trace_report(loop: Any) -> str:
    """Render the last turn's trace summary for ``/trace`` (best-effort)."""
    from ai_coding.app.trace_text import render_turn, summarize_turn

    recorder = getattr(loop, "trace", None)
    path = getattr(recorder, "current_path", None)
    if path is None:
        return "[trace] tracing is off for this session"
    return f"[trace] {render_turn(summarize_turn(path))}"


def _budget_report(loop: Any, session: Any) -> str:
    """Render the session ledger for ``/budget`` (best-effort).

    Four states, because "no recorded usage" used to cover two of them and read
    as a billing bug: tracing off, no turn run yet, a turn ran but the model
    reported no tokens, and real usage.
    """
    from ai_coding.app.trace_text import render_budget

    recorder = getattr(loop, "trace", None)
    store = getattr(recorder, "store", None)
    session_id = getattr(session, "session_id", "")
    if store is None or not session_id:
        return "[budget] tracing is off for this session"
    if not store.summary_path(session_id).is_file():
        return "[budget] 本会话还没跑过回合"
    summary = store.read_summary(session_id)
    prompt = int(summary.get("prompt_tokens", 0) or 0)
    completion = int(summary.get("completion_tokens", 0) or 0)
    if prompt == 0 and completion == 0:
        return "[budget] 本会话尚无 token 用量（模型可能未上报 usage）"
    # The recorder is handed the model's prices at startup (cli.py) and nothing
    # read them until now; without a price the line stays token-only.
    rendered = render_budget(
        summary,
        price=getattr(recorder, "pricing", None),
        prices=getattr(recorder, "pricings", None),
    )
    return f"[budget] {rendered}"


def _memory_report(loop: Any) -> str:
    """Render the memory status for ``/memory`` (best-effort)."""
    status = getattr(loop, "memory_status", None)
    if not callable(status):
        return "[memory] not available in this session"
    try:
        stats = status()
    except Exception as exc:  # noqa: BLE001
        return f"[memory] unavailable: {exc}"
    kinds = ", ".join(f"{kind}={count}" for kind, count in sorted(stats.get("by_kind", {}).items()))
    evidence = ", ".join(
        f"{name}={count}" for name, count in sorted(stats.get("by_evidence", {}).items())
    )
    # R11c: only when the library actually has scenes, so an unscoped workspace
    # shows exactly what it showed before.
    scenes = ", ".join(
        f"{name}={count}" for name, count in sorted(stats.get("by_scene", {}).items())
    )
    return (
        f"[memory] total={stats.get('total', 0)} active={stats.get('active', 0)} "
        f"pinned={stats.get('pinned', 0)} kinds: {kinds or '-'} "
        f"evidence: {evidence or '-'} "
        + (f"scenes: {scenes} " if scenes else "")
        + f"enabled={stats.get('enabled')}"
    )


__all__ = ["REPLAction", "handle_input", "HELP_TEXT"]
