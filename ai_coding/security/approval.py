"""HITL (human-in-the-loop) approval callback protocol.

``ToolDispatcher`` asks the callback whenever ``PermissionGate`` returns WARN, and
maps the answer to an ``ApprovalVerdict``.  Both front ends implement the same
three-way (four with ALWAYS) answer, so the vocabulary lives here rather than in
either of them.
"""

from __future__ import annotations

import asyncio
import enum
from collections.abc import Callable
from typing import Any


class ApprovalResult(enum.Enum):
    YES = "yes"
    #: Allow this call *and* remember exactly this resource.  A separate member
    #: rather than a flag on YES: the front ends answer with one letter, and
    #: flattening "a" into "y" is what used to make the grant a UI-private
    #: affair that the REPL could never reach (G4b).
    ALWAYS = "always"
    NO = "no"
    STOP = "stop"


class ApprovalCallback:
    """Abstract protocol for interactive approval prompts."""

    #: Whether an ``APPROVED`` answer came from a person.  Automatic callbacks
    #: are allowed to relax ordinary WARN tiers for non-interactive runs, but
    #: must never be mistaken for human consent to read a credential.
    is_human: bool = False

    async def approve(
        self,
        tool_name: str,
        params: dict[str, Any],
        reason: str,
    ) -> ApprovalResult:
        raise NotImplementedError


class CLIApprovalCallback(ApprovalCallback):
    """CLI-based approval: prompt on stdin, parse y/n/s.

    ``input_fn`` is injectable for testing (replaces ``builtins.input``).

    R7c: the read happens in a worker thread.  A synchronous ``input()`` blocks the
    event loop *thread*, which means the turn clock cannot fire while a prompt is
    on screen — measured: a 1.0s turn took 5.00s, and the answer that finally
    arrived was thrown away (docs/r7-analysis.md 1.3).
    """

    is_human = True

    def __init__(
        self,
        input_fn: Callable[[str], str] | None = None,
    ) -> None:
        self._input = input_fn or _default_input
        self._provider: Callable[..., Any] | None = None

    def set_provider(self, provider: Callable[..., Any]) -> None:
        """Use an async provider instead of stdin (TUI mode).

        The provider is called as ``provider(tool_name, params, reason)`` — the
        *structured* arguments, not the formatted prompt.  The prompt truncates
        ``params`` to 120 characters, and that truncation used to be the only
        thing a TUI modal ever saw, so a long ``bash`` command could be approved
        without being displayed.  A UI that wants the prompt can rebuild it with
        ``_format_prompt``; a UI that wants the data now gets it.
        """
        self._provider = provider

    async def approve(
        self,
        tool_name: str,
        params: dict[str, Any],
        reason: str,
    ) -> ApprovalResult:
        if self._provider is not None:
            try:
                answer = str(await self._provider(tool_name, params, reason)).strip().lower()
            except Exception:  # noqa: BLE001 — fail-closed
                return ApprovalResult.NO
        else:
            prompt = self._format_prompt(tool_name, params, reason)
            try:
                answer = (await asyncio.to_thread(self._input, prompt)).strip().lower()
            except (EOFError, OSError):
                return ApprovalResult.NO  # fail-closed

        if answer in {"a", "always"}:
            # Not flattened into YES: the caller has to know to record it.  This
            # is the only parser for either front end -- the TUI hands its modal
            # answer back through the same ``_provider`` hook.
            return ApprovalResult.ALWAYS
        if answer in {"y", "yes"}:
            return ApprovalResult.YES
        if answer in {"s", "stop"}:
            return ApprovalResult.STOP
        # default: no (fail-closed)
        return ApprovalResult.NO

    @staticmethod
    def _format_prompt(tool_name: str, params: dict[str, Any], reason: str) -> str:
        preview = str(params)
        if len(preview) > 120:
            preview = preview[:117] + "..."
        return f"[{tool_name}] {reason}\n  params: {preview}\nAllow? [y/N/a/s] "


class AutoApproveCallback(ApprovalCallback):
    """Non-interactive approval: always permits WARN tiers.

    Use for non-interactive entry points (``ask``) and for sub-agent tool
    stacks where no human is present to confirm.  It answers ``YES`` and never
    ``ALWAYS``: a grant is a human decision, and nothing here made one.  Hard-deny
    (``DENY``) tiers are still blocked by the dispatcher before it ever asks, so
    this relaxes the interactive tier only, never the deny tier.
    """

    is_human = False

    async def approve(
        self,
        tool_name: str,
        params: dict[str, Any],
        reason: str,
    ) -> ApprovalResult:
        return ApprovalResult.YES


def _default_input(prompt: str) -> str:  # pragma: no cover - trivial wrapper
    return input(prompt)


__all__ = [
    "ApprovalCallback",
    "ApprovalResult",
    "CLIApprovalCallback",
    "AutoApproveCallback",
]
