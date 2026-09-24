"""Runtime guards — the stateful half of tool policy (R4b).

``PermissionGate`` answers "is this call dangerous?" from the call alone, and that
is all it can answer: it is a static table.  A guard answers a question that needs
memory — *"should this still happen, given what this turn has already done?"* — so
it can refuse a repeated call, add a user-configured red line, or flag a command
that reaches outside the workspace (docs/r4-analysis.md 5).

Two rules keep guards honest:

* **fail closed** — a guard that raises is a refusal, never a silent pass (P2);
* **pure and offline** — guards are synchronous, do no IO and see only the
  context and a read-only :class:`TurnView`, so they stay unit-testable and can
  never become a hidden second dispatcher.
"""

from __future__ import annotations

import enum
import hashlib
import json
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

from ai_coding.domain.message import ToolCall, ToolResult
from ai_coding.tools.base import ToolContext

__all__ = [
    "BashPolicyGuard",
    "DuplicateCallGuard",
    "Guard",
    "GuardChain",
    "GuardDecision",
    "GuardVerdict",
    "PathEscapeGuard",
    "SecretGuard",
    "TurnView",
    "call_fingerprint",
]


class GuardVerdict(enum.Enum):
    """What a guard decided about one call."""

    ALLOW = "allow"
    DENY = "deny"
    REWRITE = "rewrite"


@dataclass(frozen=True)
class GuardDecision:
    """One guard's verdict, with the reason and (for rewrites) the new params."""

    verdict: GuardVerdict
    reason: str = ""
    params: dict[str, Any] | None = None
    guard: str = ""


@dataclass(frozen=True)
class TurnView:
    """The read-only turn state a guard may consult (never global state)."""

    iteration: int = 0
    tokens_spent: int = 0
    task_open: int = 0


def call_fingerprint(call: ToolCall) -> str:
    """A stable identity for ``(tool, params)`` — order-insensitive, value-sensitive."""
    try:
        payload = json.dumps(call.params, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        payload = repr(sorted(dict(call.params).items()))
    digest = hashlib.sha1(f"{call.name}:{payload}".encode()).hexdigest()
    return digest[:12]


class Guard(Protocol):
    """A stateless-or-resettable policy check for one call."""

    name: str

    def check(
        self, call: ToolCall, ctx: ToolContext, view: TurnView
    ) -> GuardDecision: ...


class GuardChain:
    """Run guards in order; the first non-ALLOW verdict decides."""

    def __init__(self, guards: Sequence[Guard]) -> None:
        self._guards: list[Guard] = list(guards)

    def __len__(self) -> int:
        return len(self._guards)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(getattr(guard, "name", type(guard).__name__) for guard in self._guards)

    def reset(self) -> None:
        """Clear per-turn state (called at the start of every turn)."""
        for guard in self._guards:
            reset = getattr(guard, "reset", None)
            if callable(reset):
                reset()

    def check(
        self, call: ToolCall, ctx: ToolContext, view: TurnView
    ) -> GuardDecision:
        for guard in self._guards:
            name = getattr(guard, "name", type(guard).__name__)
            try:
                decision = guard.check(call, ctx, view)
            except Exception as exc:  # noqa: BLE001 — fail closed (P2)
                return GuardDecision(
                    GuardVerdict.DENY,
                    reason=f"guard '{name}' failed: {exc}",
                    guard=name,
                )
            if decision.verdict is not GuardVerdict.ALLOW:
                return decision
        return GuardDecision(GuardVerdict.ALLOW)

    def after(
        self, call: ToolCall, result: ToolResult, ctx: ToolContext, view: TurnView
    ) -> ToolResult:
        """Post-execution pass: every guard may transform the result in turn."""
        current = result
        for guard in self._guards:
            hook = getattr(guard, "after", None)
            if not callable(hook):
                continue
            try:
                current = hook(call, current, ctx, view)
            except Exception:  # noqa: BLE001 — a failing post-guard must not leak
                continue
        return current


class DuplicateCallGuard:
    """Refuse a call that repeats one this turn has already made too often.

    The classic ReAct failure is the model asking for the same ``read``/``bash``
    over and over; the refusal names the earlier call so it can reuse that result
    instead of re-deriving it (docs/r4-analysis.md 5.2).
    """

    name = "duplicate_call"

    def __init__(self, threshold: int = 2) -> None:
        self.threshold = max(0, int(threshold))
        self.reset()

    def reset(self) -> None:
        self._counts: Counter[str] = Counter()
        self._first_call: dict[str, str] = {}

    def check(self, call: ToolCall, ctx: ToolContext, view: TurnView) -> GuardDecision:
        del ctx, view
        if self.threshold <= 0:
            return GuardDecision(GuardVerdict.ALLOW)
        fingerprint = call_fingerprint(call)
        previous = self._counts[fingerprint]
        if previous >= self.threshold:
            earlier = self._first_call.get(fingerprint, "")
            return GuardDecision(
                GuardVerdict.DENY,
                reason=(
                    f"'{call.name}' was already called {previous} time(s) this turn "
                    f"with the same arguments; reuse the earlier result"
                    + (f" (call {earlier})" if earlier else "")
                ),
                guard=self.name,
            )
        self._counts[fingerprint] += 1
        self._first_call.setdefault(fingerprint, call.id)
        return GuardDecision(GuardVerdict.ALLOW)


class BashPolicyGuard:
    """User-configured red lines and confirmation points on top of the gate's."""

    name = "bash_policy"

    def __init__(
        self,
        deny: Sequence[str] | None = None,
        confirm: Sequence[str] | None = None,
    ) -> None:
        self.deny = tuple(deny or ())
        self.confirm = tuple(confirm or ())

    def check(self, call: ToolCall, ctx: ToolContext, view: TurnView) -> GuardDecision:
        del ctx, view
        if call.name != "bash":
            return GuardDecision(GuardVerdict.ALLOW)
        command = str(call.params.get("command", ""))
        for pattern in self.deny:
            if pattern and pattern in command:
                return GuardDecision(
                    GuardVerdict.DENY,
                    reason=f"bash command matched a configured red line ('{pattern}')",
                    guard=self.name,
                )
        return GuardDecision(GuardVerdict.ALLOW)


_TOKEN_SPLIT = re.compile(r"[\s;|&]+")


class SecretGuard:
    """Keep high-confidence secrets out of what the model reads (post-pass).

    Always active for traces (that is the recorder's business, see
    ``core.trace.Redactor``); rewriting the *history* changes what the model sees,
    so it is opt-in (docs/r4-analysis.md 5.2, D-R4-8).
    """

    name = "secret"

    def __init__(self, redact_history: bool = False, redactor: Any | None = None) -> None:
        from ai_coding.core.trace import Redactor

        self.redact_history = redact_history
        self._redactor = redactor if redactor is not None else Redactor()

    def check(
        self, call: ToolCall, ctx: ToolContext, view: TurnView
    ) -> GuardDecision:
        del call, ctx, view
        return GuardDecision(GuardVerdict.ALLOW)

    def after(
        self,
        call: ToolCall,
        result: ToolResult,
        ctx: ToolContext,
        view: TurnView,
    ) -> ToolResult:
        del call, ctx, view
        if not self.redact_history:
            return result
        return replace(result, output=self._redactor.redact(result.output))


def _escaped_paths(command: str, workspace: str) -> list[str]:
    """Absolute path tokens that do not live under ``workspace``.

    Token-based on purpose: a regex that merely looks for ``/`` misfires on
    ``./src``, while the escape that actually matters is a *token* rooted at ``/``
    (``cat /etc/passwd``, ``cd / && …``).
    """
    root = (workspace or "").rstrip("/")
    found: list[str] = []
    for raw in _TOKEN_SPLIT.split(command):
        token = raw.strip("'\"")
        if not token.startswith("/"):
            continue
        if root and (token == root or token.startswith(root + "/")):
            continue
        found.append(token)
    return found


class PathEscapeGuard:
    """Flag (or refuse) bash commands that reach outside the workspace.

    ``Sandbox`` already confines the file tools; a shell is free to ``cd /``, so
    this only *observes* by default and refuses when the operator opts in
    (docs/r4-analysis.md 5.2).
    """

    name = "path_escape"

    def __init__(self, strict: bool = False) -> None:
        self.strict = strict

    def check(self, call: ToolCall, ctx: ToolContext, view: TurnView) -> GuardDecision:
        del view
        if call.name != "bash":
            return GuardDecision(GuardVerdict.ALLOW)
        command = str(call.params.get("command", ""))
        escaped = _escaped_paths(command, str(ctx.workspace or ""))
        if not escaped:
            return GuardDecision(GuardVerdict.ALLOW)
        reason = f"bash command references paths outside the workspace: {', '.join(escaped[:3])}"
        if self.strict:
            return GuardDecision(GuardVerdict.DENY, reason=reason, guard=self.name)
        # observe-only: allow, but say so (the trace records it)
        return GuardDecision(GuardVerdict.ALLOW, reason=reason, guard=self.name)
