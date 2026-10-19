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
from typing import Any, Protocol

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

    async def ask(
        self,
        question: str,
        options: Sequence[str] = (),
        *,
        detail: str = "",
        intent: str = "",
        data: dict[str, Any] | None = None,
    ) -> UserAnswer: ...


def _format_prompt(question: str, options: Sequence[str], detail: str = "") -> str:
    lines = [question]
    if detail:
        lines.append(detail)
    if options:
        lines.append("（可选：" + " / ".join(str(option) for option in options) + "）")
    lines.append("> ")
    return "\n".join(lines)


class CLIUserPromptCallback:
    """Read one line from stdin — inside a thread, so the turn clock still works."""

    def __init__(self, input_fn: Callable[[str], str] | None = None) -> None:
        self._read: Callable[[str], str] = input_fn or (lambda prompt: input(prompt))
        # Five parameters since R04: question and options apart (the TUI
        # renders options as a list), the plan-review ``detail`` (markdown
        # body), its ``intent`` (which card to render) and its structured
        # ``data`` (the item rows the card shows).  Same shape the approval
        # channel already uses (``approval.py`` passes tool/params/reason).
        self._provider: Callable[..., Any] | None = None

    def set_provider(self, provider: Callable[..., Any]) -> None:
        """Use an async provider instead of stdin (TUI mode)."""
        self._provider = provider

    async def ask(
        self,
        question: str,
        options: Sequence[str] = (),
        *,
        detail: str = "",
        intent: str = "",
        data: dict[str, Any] | None = None,
    ) -> UserAnswer:
        if self._provider is not None:
            try:
                raw = await self._provider(
                    question, list(options), detail, intent, dict(data or {})
                )
                text = str(raw or "").strip()
                if not text:
                    return UserAnswer(answered=False, reason="empty")
                return UserAnswer(answered=True, text=text)
            except TypeError:
                # E (audit fix): a provider signature mismatch is a bug, not an
                # absent user.  Swallowing it here is what kept the plan review
                # dark on real machines -- the review "just never answered".
                raise
            except Exception:  # noqa: BLE001 — the channel must never break a turn
                return UserAnswer(answered=False, reason="no_user")
        prompt = _format_prompt(question, options, detail)
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

    async def ask(
        self,
        question: str,
        options: Sequence[str] = (),
        *,
        detail: str = "",
        intent: str = "",
        data: dict[str, Any] | None = None,
    ) -> UserAnswer:
        return UserAnswer(answered=False, reason="no_user")
