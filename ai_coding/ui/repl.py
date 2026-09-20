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
        console.info(f"[status] session={sid} messages={n}")
        return REPLAction.CONTINUE, session
    return REPLAction.CONTINUE, session


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
