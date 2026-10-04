"""Interactive REPL (M5-2).

The testable core — :func:`handle_input` and the :class:`REPLAction` decision —
stays import-light and offline; the full-screen UI lives in ``textual_app``.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Any

from ai_coding.ui.console import DialogueConsole
from ai_coding.ui.slash import parse_slash, suggest_command

HELP_TEXT = (
    "direct commands:\n"
    "  /help, /h       show this help\n"
    "  /status         show session info\n"
    "  /model [name]   list or switch models\n"
    "  /resume [id]    list or resume saved sessions\n"
    "  /permissions   view or change approval policy\n"
    "  /todo           show current task list\n"
    "  /tasks          list background sub-agent runs\n"
    "  /skills         list available skills\n"
    "  /plan           generate and execute an implementation plan\n"
    "  /review        review current changes\n"
    "  /config         show effective configuration\n"
    "  /compact        compact conversation history\n"
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


async def handle_input(
    loop: Any,
    text: str,
    session: Any,
    sessions: Any,
    console: DialogueConsole,
) -> tuple[REPLAction, Any]:
    """Process a single input line. Plain text runs the loop; slash reacts.

    Returns ``(action, session)`` where ``session`` may be a fresh one when a
    new session was started.  ``sessions`` only needs ``create()``.
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
        answer = await loop.process_input(session, text, on_delta=console.delta)
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
        console.info(
            f"[compact] messages {report.get('messages')} → {report.get('after')} "
            f"(removed {report.get('removed')})"
        )
        if getattr(sessions, "save", None) is not None:
            sessions.save(session)
        return REPLAction.CONTINUE, session
    if cmd.name == "memory":
        console.info(_memory_report(loop))
        return REPLAction.CONTINUE, session
    if cmd.name == "trace":
        console.info(_trace_report(loop))
        return REPLAction.CONTINUE, session
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
            console.info(task.render())
        return REPLAction.CONTINUE, session
    if cmd.name == "tasks":
        return _handle_tasks(loop, cmd.raw, session, console)
    if cmd.name == "skills":
        skills_reg = getattr(loop, "skills", None)
        if skills_reg is None:
            console.info("[skills] not available in this session")
            return REPLAction.CONTINUE, session
        catalog = skills_reg.catalog_text()
        if catalog:
            console.info(f"[skills] available:\n{catalog}")
        else:
            console.info("[skills] no skills found")
        return REPLAction.CONTINUE, session
    if cmd.name == "plan":
        # M1-A: /plan plans AND executes.  The two-command handoff let the
        # plan go stale between /plan and /plan-run (measured: a run that
        # executed 0 turns against a list from a prior turn).
        topic = cmd.raw[len("/plan"):].strip()
        if topic:
            session.begin_plan(topic)
            console.info(f"[plan] new plan for: {topic} (task list reset)")
        else:
            topic = "the current task"
            console.info("[plan] generating implementation plan...")
        from ai_coding.core.plan_runner import PlanRunner, continuations_disabled

        # C3 off: this turn authors the plan, it does not do the work.
        with continuations_disabled(loop):
            await loop.process_input(
                session,
                f"Create a detailed implementation plan for: {topic}. "
                "Break it down into specific, actionable steps. "
                "Write the plan as todo items with todo_write; every step with "
                "a machine-checkable outcome must carry its proof command in "
                "'verify' (a shell command), so the plan can be executed and "
                "verified step by step. "
                "Write the items as pending/open steps only — do not execute "
                "the work and do not mark items completed during planning.",
                on_delta=console.delta,
            )
        await PlanRunner(loop).run(session, console)
        return REPLAction.CONTINUE, session
    if cmd.name == "review":
        console.info("[review] reviewing current changes...")
        await loop.process_input(
            session,
            "Review the current git diff and identify any issues, "
            "bugs, or improvements needed.",
            on_delta=console.delta,
        )
        return REPLAction.CONTINUE, session
    if cmd.name == "config":
        return _handle_config(loop, session, console)
    return REPLAction.CONTINUE, session


def _handle_config(loop: Any, session: Any, console: DialogueConsole) -> tuple[REPLAction, Any]:
    settings = getattr(loop, "settings", None)
    if settings is None:
        console.info("[config] not available")
        return REPLAction.CONTINUE, session
    lines = [
        f"[config] model: {getattr(settings, 'default_model', '?')}",
        f"  max_context_tokens: {getattr(settings, 'max_context_tokens', '?')}",
        f"  max_tool_iterations: {getattr(settings, 'max_tool_iterations', '?')}",
        f"  stop_policy: {getattr(settings, 'stop_policy', '?')}",
        f"  turn_timeout: {getattr(settings, 'turn_timeout_seconds', '?')}s",
    ]
    console.info("\n".join(lines))
    return REPLAction.CONTINUE, session


def _handle_permissions(
    loop: Any, raw: str, session: Any, console: DialogueConsole
) -> tuple[REPLAction, Any]:
    parts = raw.split()
    dispatcher = getattr(loop, "dispatcher", None)
    approval = getattr(dispatcher, "approval", None) if dispatcher else None
    if len(parts) == 1:
        name = type(approval).__name__ if approval else "(none)"
        console.info(f"[permissions] current approval: {name}")
        return REPLAction.CONTINUE, session
    target = parts[1]
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
        console.error(f"[permissions] unknown policy '{target}'. Available: auto, confirm")
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
    loop: Any, raw: str, session: Any, sessions: Any, console: DialogueConsole
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


def _handle_tasks(
    loop: Any, raw: str, session: Any, console: DialogueConsole
) -> tuple[REPLAction, Any]:
    """List background sub-agent runs, or stop one (R1-2)."""
    runner = _runner_of(loop)
    if runner is None:
        console.info("[tasks] no sub-agent runner in this session")
        return REPLAction.CONTINUE, session
    parts = raw.split()
    if len(parts) >= 3 and parts[1] == "stop":
        if runner.cancel(parts[2]):
            console.info(f"[tasks] stopping {parts[2][:12]}")
        else:
            console.info(f"[tasks] no background task '{parts[2]}'")
        return REPLAction.CONTINUE, session
    rows = runner.task_list()
    if not rows:
        console.info("[tasks] no background runs")
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
    return REPLAction.CONTINUE, session


def _handle_model(
    loop: Any, raw: str, session: Any, console: DialogueConsole
) -> tuple[REPLAction, Any]:
    settings = getattr(loop, "settings", None)
    models = getattr(settings, "models", {}) if settings else {}
    default_model = getattr(settings, "default_model", "") if settings else ""
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
        set_model(target)
        console.info(f"[model] switched to {target}")
    else:
        console.error("[model] model switching not supported by this loop")
    return REPLAction.CONTINUE, session


def _trace_report(loop: Any) -> str:
    """Render the last turn's trace summary for ``/trace`` (best-effort)."""
    from ai_coding.ui.trace_render import render_turn, summarize_turn

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
    from ai_coding.ui.trace_render import render_budget

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
    return f"[budget] {render_budget(summary)}"


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
