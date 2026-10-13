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
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ai_coding.security.grants import GrantStore


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


#: Tools an "always" may never remember, whatever resource can be extracted.
#: A policy exclusion rather than a technical one: an always on bash would be an
#: always on every command it may ever run.  It lives here, beside the gate,
#: because it is a permission rule -- it used to live in the approval modal,
#: where the button and the recorder each had to remember to consult it.
_NEVER_REMEMBERED = frozenset({"bash"})

#: Tools whose params name a single file, so a grant can say which one.
_PATH_TOOLS = frozenset({"write", "edit"})


def grant_key(tool_name: str, params: Mapping[str, Any]) -> str | None:
    """What an "always" would actually remember, or ``None`` when it cannot be said.

    The key has to be *what the operator just read*.  Keyed on the tool name
    alone it is strictly broader than the modal it came from: the operator reads
    "edit src/foo.py" -- diff included -- and would otherwise be granting edits
    to every file in the workspace.

    ``None`` means the grant must not be offered at all: either the tool is
    excluded by policy, or its params name no single resource (a remote tool's
    arguments are not summarisable).  Remembering "something" in those cases
    would repeat the same defect one level down.

    Paths are normalised lexically (``./a`` -> ``a``) because the key is for
    *matching*, not for enforcing a boundary -- ``Sandbox`` owns that, and it
    resolves properly.  Two spellings this cannot unify cost an extra prompt,
    never a wrong decision.
    """
    name = tool_name.lower()
    if name in _NEVER_REMEMBERED:
        return None
    # M2: ``server__tool`` — the remote tool's own name is what was approved.
    # It used to return None here ("a remote tool's arguments are not
    # summarisable"), which made "Always" silently "this once" for every MCP
    # tool: the operator's answer had nothing to be remembered *as*.
    if "__" in name:
        return name
    path = str(params.get("path") or "").strip()
    if name in _PATH_TOOLS and path:
        return f"{name}:{os.path.normpath(path)}"
    return None


class PermissionGate:
    """Determines whether a tool invocation is allowed, warned, or denied."""

    def __init__(
        self,
        *,
        trusted_servers: Iterable[str] = (),
        grants: Iterable[str] = (),
        store: Any | None = None,
        denied_tools: Iterable[str] = (),
        allowed_tools: Iterable[str] = (),
        skill_permissions: Mapping[str, str] | None = None,
        skill_resolver: Callable[[str], str | None] | None = None,
    ) -> None:
        #: Calls the operator answered "Always" for, as ``grant_key`` strings.
        #: Seeded from the store at startup and extended by ``grant``.
        self._grants = {str(key) for key in grants}
        #: Where a new grant is written down, or ``None`` for this process only.
        #: Injected rather than built here so the gate stays constructible
        #: without a filesystem -- every test in the suite relies on that.
        self._store = store
        # R7d: MCP servers the user declared as trusted.  A remote tool is named
        # ``server__tool``, so the server is the only thing that can be declared
        # here — the tool names come from the server, not from our config.
        self._trusted_servers = {str(name) for name in trusted_servers if name}
        # M2: per-tool rules, declared per server (``mcp.servers[].permissions``).
        # Full ``server__tool`` names, so a rule reads the same way the catalog
        # and the gate do.
        self._denied_tools = {str(name) for name in denied_tools if name}
        self._allowed_tools = {str(name) for name in allowed_tools if name}
        # E-5: the model's skill tool is judged per skill id (alias-normalized
        # once, here, so check() is a dict lookup).  ``skill_resolver`` maps a
        # spelling to the canonical id; without one, spellings must match the
        # config literally.
        self._skill_resolver = skill_resolver
        self._skill_permissions = {
            (skill_resolver(key) or key) if skill_resolver is not None else key: str(value)
            for key, value in (skill_permissions or {}).items()
        }
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
            # R8a: writing one line into the turn's own book touches neither the
            # repo nor the durable memory (docs/r8-analysis.md 3.1, step 5).
            "checkpoint_write",
            # M2: the MCP dispatcher is a vessel, not an action — what it carries
            # is authorized as ``server__tool`` (BaseTool.permission_name), which
            # is what the gate actually judges.  The only calls that reach it
            # under its own name are ones whose pair is not callable (not
            # connected, unknown, whitelisted out), and those must come back as a
            # *result* naming the real tools, not as an approval prompt for a
            # call that cannot run (seen in the M3 end-to-end).
            "mcp_call",
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

        # 1.5. M2: a tool the config denies outright.  Before the grants, for the
        # same reason the bash list is first: a standing "always" may never
        # reopen what the operator's own config refuses.
        if tool_name in self._denied_tools:
            return Decision(
                PermissionDecision.DENY,
                f"tool '{tool_name}' is denied by configuration",
            )

        # 2. A resource the operator already approved.  After the deny rules, so
        # a standing grant can never reopen something that is refused outright:
        # doing this first would make every future path-based deny bypassable by
        # an "always" the user gave before it existed.
        key = grant_key(name, params)
        if key is not None and key in self._grants:
            return Decision(PermissionDecision.ALLOW, "")

        # 2.5. E-5: a per-skill policy for the model's skill tool.  Only the
        # model-initiated read is judged: a human's @mention is its own
        # authorization, and ``list`` names skills but reads none.
        if name == "skill" and str(params.get("action", "")).lower() == "read":
            target = str(params.get("name", "")).strip()
            canonical = self._skill_resolver(target) if self._skill_resolver is not None else None
            policy = (
                self._skill_permissions.get(canonical or "")
                or self._skill_permissions.get(target)
                or "allow"
            )
            if policy == "deny":
                return Decision(
                    PermissionDecision.DENY,
                    f"skill '{target}' is denied by configuration",
                )
            if policy == "ask":
                return Decision(
                    PermissionDecision.WARN,
                    f"skill '{target}' requires confirmation",
                )

        # 3. Known allow-list
        if name in self._allow_tools:
            return Decision(PermissionDecision.ALLOW, "")

        # 3.5. M2: a tool the config explicitly allows — no prompt, like the
        # trusted-server rule but for one tool instead of the whole server.
        if tool_name in self._allowed_tools:
            return Decision(PermissionDecision.ALLOW, "")

        # 4. Known warn-list
        if name in self._warn_tools:
            return Decision(
                PermissionDecision.WARN,
                f"tool '{tool_name}' requires confirmation",
            )

        # 5. A tool from a server the user declared trusted (R7d).  Checked before
        # the fail-closed rule, and matched on the *server* segment only, so a
        # local tool cannot be trusted by naming itself "bash__something".
        server, sep, _remote = tool_name.partition("__")
        if sep and server in self._trusted_servers:
            return Decision(PermissionDecision.ALLOW, "")

        # 6. Unknown tools → WARN (fail-closed / conservative)
        return Decision(
            PermissionDecision.WARN,
            f"unknown tool '{tool_name}' requires confirmation",
        )

    def trust_server(self, name: str) -> None:
        """Trust a server's tools from now on (M5, the wizard's last step).

        The gate is built once from the config at startup, so a server added at
        runtime has to be trusted on the *live* object — otherwise the answer
        the operator just gave would be ignored until the next start, which is
        the same set of rules from_config installs.
        """
        if name:
            self._trusted_servers.add(str(name))

    def grant(self, tool_name: str, params: Mapping[str, Any]) -> str | None:
        """Remember exactly this call; return the key, or ``None`` if unnameable.

        Called by the dispatcher when the human answered ``ALWAYS``.  Writing
        through to the store is best effort -- the operator did say always, so
        the grant holds for this process even if it cannot be written down.
        """
        key = grant_key(tool_name, params)
        if key is None:
            return None
        if key not in self._grants:
            self._grants.add(key)
            store = self._store
            if store is not None:
                store.add(key)
        return key

    @property
    def grants(self) -> tuple[str, ...]:
        """What is currently remembered, sorted for display."""
        return tuple(sorted(self._grants))

    def revoke_all(self) -> None:
        """Forget everything, on disk as well as in memory."""
        self._grants.clear()
        if self._store is not None:
            self._store.clear()

    @classmethod
    def from_config(
        cls,
        app_cfg: Any,
        *,
        workspace: str | None = None,
        skill_resolver: Callable[[str], str | None] | None = None,
    ) -> PermissionGate:
        """The one place the composition root's policy is read (R7d).

        Nothing about the *tool list* is needed: a server is trusted by name, so
        this works before (and independently of) the MCP connection.

        ``workspace`` turns on persistence (G4b-2): the grants the operator has
        given in this directory are loaded, and new ones are written beside them.
        Without it the gate is session-only -- which is what a sub-agent builds,
        and it never asks anyway.

        ``skill_resolver`` (E-5) turns a skill spelling into its canonical id so
        ``ai.skill_permissions`` keys and call arguments meet in one namespace.
        """
        mcp = getattr(app_cfg, "mcp", None)
        servers = getattr(mcp, "servers", None) or []
        store = GrantStore(workspace) if workspace else None
        denied: list[str] = []
        allowed: list[str] = []
        for server in servers:
            name = str(getattr(server, "name", ""))
            rules = getattr(server, "permissions", None)
            for remote in getattr(rules, "deny", None) or []:
                denied.append(f"{name}__{remote}")
            for remote in getattr(rules, "allow", None) or []:
                allowed.append(f"{name}__{remote}")
        return cls(
            trusted_servers=[
                str(getattr(server, "name", ""))
                for server in servers
                if getattr(server, "auto_approve", False)
            ],
            grants=store.load() if store is not None else (),
            store=store,
            denied_tools=denied,
            allowed_tools=allowed,
            skill_permissions=getattr(getattr(app_cfg, "ai", None), "skill_permissions", None)
            or {},
            skill_resolver=skill_resolver,
        )


__all__ = ["PermissionGate", "PermissionDecision", "Decision", "grant_key"]
