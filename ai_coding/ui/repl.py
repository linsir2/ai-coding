"""Interactive REPL (M5-2).

The heavy terminal layer (``prompt_toolkit``) is imported lazily inside
:func:`run_repl` so the testable core — :func:`handle_input` and the
:class:`REPLAction` decision — stays import-light and offline.
"""

from __future__ import annotations

import asyncio
from enum import Enum
from typing import Any

from ai_coding.ui.console import DialogueConsole
from ai_coding.ui.slash import parse_slash

HELP_TEXT = (
    "direct commands:\n"
    "  /help, /h    show this help\n"
    "  /status      show session info\n"
    "  /trace       show what the last turn cost and what degraded (R4a)\n"
    "  /budget      show this session's recorded token totals (R4a)\n"
    "  /clear       start a fresh session\n"
    "  /quit, /q    exit the REPL"
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
    """Render the session ledger for ``/budget`` (best-effort)."""
    from ai_coding.ui.trace_render import render_budget

    recorder = getattr(loop, "trace", None)
    store = getattr(recorder, "store", None)
    session_id = getattr(session, "session_id", "")
    if store is None or not session_id:
        return "[budget] tracing is off for this session"
    return f"[budget] {render_budget(store.read_summary(session_id))}"


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
    return (
        f"[memory] total={stats.get('total', 0)} active={stats.get('active', 0)} "
        f"pinned={stats.get('pinned', 0)} kinds: {kinds or '-'} "
        f"evidence: {evidence or '-'} "
        f"enabled={stats.get('enabled')}"
    )


async def run_repl(
    loop: Any,
    sessions: Any,
    console: DialogueConsole,
) -> None:
    """Interactive loop. Lazily imports prompt_toolkit."""
    from prompt_toolkit import PromptSession

    session = sessions.create()
    prompt: Any = PromptSession()
    console.rule("ai-coding REPL — /help for commands")
    while True:
        try:
            text = await asyncio.to_thread(prompt.prompt, "you> ")
        except (KeyboardInterrupt, EOFError):
            break
        stripped = text.strip()
        if not stripped:
            continue
        action, session = await handle_input(loop, stripped, session, sessions, console)
        if action is REPLAction.QUIT:
            break
    console.info("bye")


__all__ = ["REPLAction", "handle_input", "run_repl", "HELP_TEXT"]
