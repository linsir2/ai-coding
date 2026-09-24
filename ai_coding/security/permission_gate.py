"""Permission gate — decides ALLOW/WARN/DENY for each tool invocation.

Rules mirror the original Java ``PermissionGate``:
  - read / glob          → ALLOW
  - write / edit         → WARN
  - bash                 → WARN (hard-deny list → DENY)
  - todo_write / skill / subAgent / memory_search / ask_user → ALLOW
  - unknown tools        → WARN (fail-closed, conservative)
"""

from __future__ import annotations

import enum
from collections.abc import Iterable
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

    def __init__(
        self,
        *,
        trusted_servers: Iterable[str] = (),
    ) -> None:
        # R7d: MCP servers the user declared as trusted.  A remote tool is named
        # ``server__tool``, so the server is the only thing that can be declared
        # here — the tool names come from the server, not from our config.
        self._trusted_servers = {str(name) for name in trusted_servers if name}
        self._allow_tools = {
            "read",
            "glob",
            "todo_write",
            "skill",
            "subagent",
            "memory_search",  # read-only recall (R3b)
            # R7e-1: durable, but not a side effect on the repo, and the harness
            # harvests notes without asking anyway (docs/r7e-analysis.md Q1).
            "memory_write",
            # R6c: asking a question changes nothing by itself — the answer only
            # becomes an action through further tool calls, which are gated on their
            # own.  Leaving it WARN meant a REPL asked "Allow ask_user?" *before*
            # the question: two prompts for one question, and any answer that was
            # not "y" turned the question into "the user declined" (measured).
            "ask_user",
        }
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

        # 4. A tool from a server the user declared trusted (R7d).  Checked before
        # the fail-closed rule, and matched on the *server* segment only, so a
        # local tool cannot be trusted by naming itself "bash__something".
        server, sep, _remote = tool_name.partition("__")
        if sep and server in self._trusted_servers:
            return Decision(PermissionDecision.ALLOW, "")

        # 5. Unknown tools → WARN (fail-closed / conservative)
        return Decision(
            PermissionDecision.WARN,
            f"unknown tool '{tool_name}' requires confirmation",
        )

    @classmethod
    def from_config(cls, app_cfg: Any) -> PermissionGate:
        """The one place the composition root's policy is read (R7d).

        Nothing about the *tool list* is needed: a server is trusted by name, so
        this works before (and independently of) the MCP connection.
        """
        mcp = getattr(app_cfg, "mcp", None)
        servers = getattr(mcp, "servers", None) or []
        return cls(
            trusted_servers=[
                str(getattr(server, "name", ""))
                for server in servers
                if getattr(server, "auto_approve", False)
            ]
        )


__all__ = ["PermissionGate", "PermissionDecision", "Decision"]
