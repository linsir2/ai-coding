"""Permission gate — decides ALLOW/WARN/DENY for each tool invocation.

Rules mirror the original Java ``PermissionGate``:
  - read / glob          → ALLOW
  - write / edit         → WARN
  - bash                 → WARN (hard-deny list → DENY)
  - todo_write / skill / subAgent → ALLOW
  - unknown tools        → WARN (fail-closed, conservative)
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any


class PermissionDecision(enum.Enum):
    ALLOW = "allow"
    WARN = "warn"
    DENY = "deny"


@dataclass(frozen=True)
class _DecisionWithReason:
    """A decision plus a human-readable reason (shown to the user on WARN/DENY)."""

    decision: PermissionDecision
    reason: str


# Re-export the enum so ``PermissionGate.check()`` returns a ``PermissionDecision``
# but we internally carry reasons.  The public API uses ``PermissionDecision``
# for simple equality checks; ``reason`` is available as an attribute when needed.
#
# We attach ``reason`` to each enum member via a wrapper approach: return a custom
# subclass instance?  Simpler: return the enum but also set an attribute.
# Enums can't carry extra instance state directly, so we use a dataclass that
# compares equal to the enum by value.


class Decision:
    """A permission decision with an attached reason.

    Equality with ``PermissionDecision`` enum members works by value, so callers
    can write ``decision == PermissionDecision.ALLOW`` and also access
    ``decision.reason``.
    """

    def __init__(self, decision: PermissionDecision, reason: str = "") -> None:
        self.decision = decision
        self.reason = reason

    def __eq__(self, other: object) -> bool:
        if isinstance(other, PermissionDecision):
            return self.decision == other
        if isinstance(other, Decision):
            return self.decision == other.decision and self.reason == other.reason
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.decision)

    def __repr__(self) -> str:
        return f"Decision({self.decision.value!r}, reason={self.reason!r})"


# Hard deny patterns for bash commands
_BASH_HARD_DENY_PATTERNS: list[tuple[str, str]] = [
    ("rm -rf /", "recursive root deletion"),
    ("sudo ", "sudo escalation"),
    ("shutdown", "system shutdown"),
    ("reboot", "system reboot"),
    ("mkfs", "filesystem format"),
    ("dd if=", "disk write"),
    ("> /dev/sd", "raw disk write"),
    ("git push --force", "force git push"),
    ("git push -f ", "force git push (short)"),
    ("chmod 777", "world-writable permissions"),
]


class PermissionGate:
    """Determines whether a tool invocation is allowed, warned, or denied."""

    def __init__(self) -> None:
        self._allow_tools = {"read", "glob", "todo_write", "skill", "subagent"}
        self._warn_tools = {"write", "edit", "bash"}

    def check(self, tool_name: str, params: dict[str, Any]) -> Decision:
        """Return a ``Decision`` for the given tool invocation."""
        name = tool_name.lower()

        # 1. Bash hard-deny patterns (checked first, most specific)
        if name == "bash":
            command = str(params.get("command", ""))
            for pattern, reason in _BASH_HARD_DENY_PATTERNS:
                if pattern in command:
                    return Decision(
                        PermissionDecision.DENY,
                        f"bash command blocked: {reason} ('{pattern}')",
                    )
            return Decision(
                PermissionDecision.WARN,
                "bash command requires confirmation",
            )

        # 2. Known allow-list
        if name in self._allow_tools:
            return Decision(PermissionDecision.ALLOW, "")

        # 3. Known warn-list
        if name in self._warn_tools:
            return Decision(
                PermissionDecision.WARN,
                f"tool '{tool_name}' requires confirmation",
            )

        # 4. Unknown tools → WARN (fail-closed / conservative)
        return Decision(
            PermissionDecision.WARN,
            f"unknown tool '{tool_name}' requires confirmation",
        )


__all__ = ["PermissionGate", "PermissionDecision", "Decision"]
