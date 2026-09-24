"""The question channel — the model asking a human, instead of a gate asking (R6c).

``approval.py`` answers "may this call proceed?"; it is *passive* (the harness
decides when to ask) and fail-closed.  This module is the other direction: the
model decides that it needs an answer ("which of these two files did you mean?"),
which is what GA calls a first-class ``ask_user`` tool and what we were missing
(docs/r6-analysis.md 2.3, H7).

Two properties matter more than the shape:

* **a missing human is an answer, not a hang.**  ``UnavailableUserPromptCallback``
  is what the non-interactive ``ask`` entry point gets, and it says so immediately,
  so the model can decide for itself and write down its assumption;
* **the wait is interruptible.**  ``CLIUserPromptCallback`` reads the line inside
  ``asyncio.to_thread`` so the event loop keeps turning and the dispatcher's
  per-call budget (``BaseTool.max_duration_seconds``) can actually fire.  The
  existing ``CLIApprovalCallback`` does *not* do this — a stuck approval prompt
  hangs the turn — which is why this is a separate module rather than a copy of it
  (D-R6-9 keeps the old path out of scope).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

__all__ = [
    "CLIUserPromptCallback",
    "UnavailableUserPromptCallback",
    "UserAnswer",
    "UserPromptCallback",
]


@dataclass(frozen=True)
class UserAnswer:
    """What came back from a question: an answer, or a reason there is none."""

    answered: bool
    text: str = ""
    reason: str = ""  # "" | "no_user" | "eof" | "empty"


class UserPromptCallback(Protocol):
    """Anything that can put a question in front of a human."""

    async def ask(self, question: str, options: Sequence[str] = ()) -> UserAnswer: ...


def _format_prompt(question: str, options: Sequence[str]) -> str:
    lines = [question]
    if options:
        lines.append("（可选：" + " / ".join(str(option) for option in options) + "）")
    lines.append("> ")
    return "\n".join(lines)


class CLIUserPromptCallback:
    """Read one line from stdin — inside a thread, so the turn clock still works.

    R7c: the reader is swappable.  Inside the REPL the terminal already belongs to
    a ``prompt_toolkit`` session, and a second reader on the same tty competes with
    it (measured: a scripted pipe degenerates into ``no_answer``).  ``run_repl``
    therefore hands its own session's ``prompt`` in through :meth:`set_reader`, so
    there is exactly one reader per terminal (docs/r7-analysis.md 3.3).
    """

    def __init__(self, input_fn: Callable[[str], str] | None = None) -> None:
        self._read: Callable[[str], str] = input_fn or (lambda prompt: input(prompt))

    def set_reader(self, read: Callable[[str], str]) -> None:
        """Use ``read`` for the next question (the REPL's ``PromptSession.prompt``)."""
        self._read = read

    async def ask(self, question: str, options: Sequence[str] = ()) -> UserAnswer:
        prompt = _format_prompt(question, options)
        try:
            raw = await asyncio.to_thread(self._read, prompt)
        except EOFError:
            return UserAnswer(answered=False, reason="eof")
        except OSError:
            return UserAnswer(answered=False, reason="eof")
        text = str(raw or "").strip()
        if not text:
            return UserAnswer(answered=False, reason="empty")
        return UserAnswer(answered=True, text=text)


class UnavailableUserPromptCallback:
    """Nobody is there (``ask``, a sub-agent, CI): say so at once."""

    async def ask(self, question: str, options: Sequence[str] = ()) -> UserAnswer:
        return UserAnswer(answered=False, reason="no_user")
