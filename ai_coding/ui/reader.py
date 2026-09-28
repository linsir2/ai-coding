"""One reader for one terminal (R7e-2).

R7c established the rule: a second reader on the same tty fights the first.  R7e needs
the opposite property from the same terminal - a reader that stays alive *while a
turn is running*, so a line typed mid-turn can be delivered.  Measured before it was
built (docs/r7e-analysis.md 1.1): that has to be an async task (``prompt_async``),
because a thread reader delivers the line and then refuses to be stopped.

So this class owns the session and arbitrates each line:

* a pending question takes it (the model asked; this is the R7c channel, now going
  through the same reader);
* a line typed while a turn is running becomes steering;
* a command (``/…``) is held for the REPL;
* everything else is the REPL's next input.

The prompt text is a *callable* so it can change mid-turn (``invalidate()`` forces
the redraw); both were verified in a real pty before this file existed.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from ai_coding.core.steering import SteeringQueue
from ai_coding.security.user_prompt import UserAnswer

__all__ = ["InteractiveReader"]

_INPUT_PROMPT = "you> "
_STEER_PROMPT = "steer> "
_QUESTION_PROMPT = "问题> "


class InteractiveReader:
    """The single owner of the terminal's input, and the arbiter of each line."""

    def __init__(
        self,
        *,
        prompt: Callable[[Any], Awaitable[str]] | None = None,
        invalidate: Callable[[], None] | None = None,
        steering: SteeringQueue | None = None,
        enabled: bool = True,
        cancel_key: str = "escape",
        cancel_requested: asyncio.Event | None = None,
    ) -> None:
        self._prompt = prompt
        self._invalidate = invalidate or (lambda: None)
        self._steering = steering
        self._enabled = bool(enabled)
        #: R8c: the loop watches this; the Esc key sets it (D-6).
        self.cancel_requested = cancel_requested or asyncio.Event()
        self._cancel_key = cancel_key
        self._inputs: deque[str] = deque()
        self._waiting: asyncio.Future[str] | None = None
        self._question: str | None = None
        self._answer: asyncio.Future[str] | None = None
        self._busy = False
        self._closed = False
        self._task: asyncio.Task[None] | None = None

    # ---- construction ------------------------------------------------------

    @classmethod
    def for_terminal(
        cls,
        *,
        steering: SteeringQueue | None = None,
        enabled: bool = True,
        cancel_key: str = "escape",
    ) -> InteractiveReader:
        """Build the real thing (imports prompt_toolkit lazily)."""
        from prompt_toolkit import PromptSession
        from prompt_toolkit.patch_stdout import patch_stdout

        reader = cls(steering=steering, enabled=enabled, cancel_key=cancel_key)
        # The key bindings have to exist before the session does, and they write
        # into the reader - so the reader comes first and the session gets them.
        session: Any = PromptSession(key_bindings=reader.key_bindings())

        async def prompt(message: Any) -> str:
            with patch_stdout():
                return str(await session.prompt_async(message))

        reader.configure_prompt(prompt, session.app.invalidate)
        return reader

    def configure_prompt(
        self,
        prompt: Callable[[Any], Awaitable[str]],
        invalidate: Callable[[], None] | None = None,
    ) -> None:
        """Attach the terminal channel (``for_terminal`` builds them in this order)."""
        self._prompt = prompt
        self._invalidate = invalidate or (lambda: None)

    # ---- what the Esc key does (R8c) ---------------------------------------

    def request_cancel(self) -> None:
        """Ask the running turn to drop this batch (the Esc key calls this)."""
        self.cancel_requested.set()

    def key_bindings(self) -> Any:
        """The terminal's extra bindings: Esc = "stop waiting for this tool"."""
        from prompt_toolkit.key_binding import KeyBindings

        bindings = KeyBindings()

        @bindings.add(self._cancel_key)
        def _cancel(event: Any) -> None:  # noqa: ARG001 - the key needs its event
            self.request_cancel()

        return bindings

    # ---- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Start the one read loop (idempotent)."""
        if self._task is None and self._prompt is not None:
            self._task = asyncio.create_task(self._read_loop(), name="ai-coding-reader")

    async def aclose(self) -> None:
        """Stop reading; the terminal is left as we found it (verified in a pty)."""
        task, self._task = self._task, None
        self._closed = True
        if task is not None and not task.done():
            task.cancel()

    # ---- what the REPL asks for -------------------------------------------

    def set_busy(self, busy: bool) -> None:
        """A turn is (not) running: it decides where an unclaimed line goes."""
        self._busy = bool(busy)
        self._invalidate()

    async def input(self) -> str:
        """The next line meant for the REPL; raises ``EOFError`` when input ends."""
        if self._inputs:
            return self._inputs.popleft()
        if self._closed:
            raise EOFError
        self._waiting = asyncio.get_running_loop().create_future()
        try:
            return await self._waiting
        finally:
            self._waiting = None

    # ---- what the model asks for (R7c's channel, through this reader) ------

    async def ask(self, question: str, options: Sequence[str] = ()) -> UserAnswer:
        """Put ``question`` on the prompt line and take the next line as its answer."""
        if self._closed:
            return UserAnswer(answered=False, reason="eof")
        self._question = _render_question(question, options)
        self._answer = asyncio.get_running_loop().create_future()
        self._invalidate()
        try:
            text = (await self._answer).strip()
        finally:
            self._question = None
            self._answer = None
            self._invalidate()
        if not text:
            return UserAnswer(answered=False, reason="empty")
        return UserAnswer(answered=True, text=text)

    # ---- the one loop ------------------------------------------------------

    async def _read_loop(self) -> None:
        assert self._prompt is not None
        while not self._closed:
            try:
                line = await self._prompt(self._message)
            except (EOFError, OSError):
                self._close_stream()
                return
            self._dispatch(line)

    def _message(self) -> str:
        """The prompt text; a callable, so it can change mid-turn."""
        if self._question:
            return self._question
        if self._busy and self._enabled:
            return _STEER_PROMPT
        return _INPUT_PROMPT

    def _dispatch(self, line: str) -> None:
        """Give one line to exactly one consumer: question, steering, or the REPL."""
        body = str(line or "")
        if self._answer is not None and not self._answer.done():
            self._answer.set_result(body)
            return
        if self._busy and self._enabled and self._steering is not None:
            if self._steering.put(body):
                return
        self._inputs.append(body)
        if self._waiting is not None and not self._waiting.done():
            self._waiting.set_result(self._inputs.popleft())

    def _close_stream(self) -> None:
        self._closed = True
        if self._answer is not None and not self._answer.done():
            self._answer.set_exception(EOFError())
        if self._waiting is not None and not self._waiting.done():
            self._waiting.set_exception(EOFError())


def _render_question(question: str, options: Sequence[str]) -> str:
    if not options:
        return f"{_QUESTION_PROMPT}{question} "
    rendered = " / ".join(str(option) for option in options)
    return f"{_QUESTION_PROMPT}{question}（{rendered}） "
