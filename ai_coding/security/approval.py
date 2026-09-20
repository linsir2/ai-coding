"""HITL (human-in-the-loop) approval callback protocol.

The ``needs_approval`` hook in the SDK's FunctionTool asks *us* whether to let a
tool call proceed.  We wire it through ``PermissionGate`` + an ``ApprovalCallback``
so the CLI (or future UI) can prompt the user.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from typing import Any

from ai_coding.security.permission_gate import Decision, PermissionDecision


class ApprovalResult(enum.Enum):
    YES = "yes"
    NO = "no"
    STOP = "stop"


class ApprovalCallback:
    """Abstract protocol for interactive approval prompts."""

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
    """

    def __init__(
        self,
        input_fn: Callable[[str], str] | None = None,
    ) -> None:
        self._input = input_fn or _default_input

    async def approve(
        self,
        tool_name: str,
        params: dict[str, Any],
        reason: str,
    ) -> ApprovalResult:
        prompt = self._format_prompt(tool_name, params, reason)
        try:
            answer = self._input(prompt).strip().lower()
        except (EOFError, OSError):
            return ApprovalResult.NO  # fail-closed

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
        return f"[{tool_name}] {reason}\n  params: {preview}\nAllow? [y/N/s] "


def _default_input(prompt: str) -> str:  # pragma: no cover - trivial wrapper
    return input(prompt)


async def approve_from_gate_decision(
    decision: Decision | PermissionDecision | str,
    tool_name: str,
    params: dict[str, Any],
    approval: ApprovalCallback | None,
) -> bool:
    """Translate a gate decision into a boolean for SDK's ``needs_approval``.

    - ALLOW → True (proceed, no approval needed)
    - DENY → False (block, don't ask)
    - WARN → ask the user via ``approval``; return True only if they say YES

    Accepts ``Decision``, ``PermissionDecision`` enum, or a string like
    ``"ALLOW"`` / ``"WARN"`` / ``"DENY"``.
    """
    value: PermissionDecision
    if isinstance(decision, Decision):
        value = decision.decision
    elif isinstance(decision, PermissionDecision):
        value = decision
    else:
        try:
            value = PermissionDecision(str(decision).lower())
        except ValueError:
            value = PermissionDecision.WARN  # fail-closed on unknown

    if value == PermissionDecision.ALLOW:
        return True
    if value == PermissionDecision.DENY:
        return False
    # WARN: need human approval
    if approval is None:
        return False  # fail-closed without a callback
    reason = decision.reason if isinstance(decision, Decision) else ""
    result = await approval.approve(tool_name, params, reason)
    return result == ApprovalResult.YES


__all__ = [
    "ApprovalCallback",
    "ApprovalResult",
    "CLIApprovalCallback",
    "approve_from_gate_decision",
]
