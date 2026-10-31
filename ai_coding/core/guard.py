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

from ai_coding.domain.message import ChatMessage, ToolCall, ToolResult
from ai_coding.tools.base import ToolContext

__all__ = [
    "BashPolicyGuard",
    "DuplicateCallGuard",
    "FailedRepeatGuard",
    "Guard",
    "GuardChain",
    "GuardDecision",
    "GuardVerdict",
    "PathEscapeGuard",
    "SecretGuard",
    "TurnView",
    "build_guards",
    "call_fingerprint",
]


def build_guards(app_config: Any) -> GuardChain:
    """Build the guard chain; ``FailedRepeatGuard`` is always its first member.

    Lives here rather than in ``cli.py`` because three call sites need it -- the
    REPL loop, the TUI loop, and every sub-agent -- and a policy that only one of
    them can assemble is a policy the other two silently lack.  The failed-repeat
    guard is a permanent member (13 §2.2): its counting is free while dormant,
    and the refusal only engages through ``TurnView.strict_repeats``, which the
    strategy/fatigue ladder sets while an L2 window is open.  Every other switch
    stays config-driven.

    Returns a *fresh* chain per call: ``DuplicateCallGuard`` counts calls within a
    turn, and a sub-agent sharing the parent's counter would be refused for calls
    it never made.
    """
    guards: list[Guard] = [FailedRepeatGuard()]
    guard_cfg = getattr(app_config, "guard", None)
    if guard_cfg is not None:
        threshold = int(getattr(guard_cfg, "duplicate_threshold", 2) or 0)
        if threshold > 0:
            guards.append(DuplicateCallGuard(threshold=threshold))
        deny = list(getattr(guard_cfg, "bash_deny", []) or [])
        confirm = list(getattr(guard_cfg, "bash_confirm", []) or [])
        if deny or confirm:
            guards.append(BashPolicyGuard(deny=deny, confirm=confirm))
        if bool(getattr(guard_cfg, "strict_paths", False)):
            guards.append(PathEscapeGuard(strict=True))
        if bool(getattr(guard_cfg, "redact_history", False)):
            guards.append(SecretGuard(redact_history=True))
    return GuardChain(guards)


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
    #: 13 §2.2: True while a strategy/fatigue L2 window is open.  The
    #: ``FailedRepeatGuard`` stays dormant while this is False.
    strict_repeats: bool = False


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


#: What the model should do when it repeats a call that already succeeded.
#:
#: One sentence cannot fit every tool: "reuse the earlier result" is exactly
#: right for a read, meaningless for an ask (the result is a human's answer,
#: not a value to reuse) and for a write (the side effect is already done).
#: The refusal is a teaching moment, so it names the right next move -- a
#: generic wording sent the model back to reword its arguments and retry
#: (measured in trace ec89d5ee, before the clear action was removed).
_DUPLICATE_GUIDANCE: dict[str, str] = {
    "ask_user": "the answer is already in the transcript — act on it, or ask something different",
    "write": "the file already holds that write — no second call is needed",
    "edit": "the file already reflects that edit — no second call is needed",
    "todo_write": "the list already reflects that call — read the latest task-state rendering",
}

_DUPLICATE_GUIDANCE_DEFAULT = "reuse the earlier result"

#: O-1: tools whose output *is* the result and whose re-execution has no side
#: effect.  Only these may run again when their earlier output left the context
#: -- a trimmed ``bash`` output is not evidence that its side effect did not
#: happen, so the guard keeps refusing repeats there.
_REREADABLE = frozenset({"read"})

#: O-1: hard ceiling on how many times one fingerprint may succeed in a turn
#: once its output is no longer visible, as a multiple of the threshold.  The
#: "reuse is possible" rule alone cannot bound this: L3 replaces a large read
#: with a marker immediately, so "nothing to reuse" would be true every time.
_UNVISIBLE_REREAD_FACTOR = 2

#: The heads that mean "this tool message no longer carries its body": the L2
#: envelope and its legacy bare form, then L3's persisted marker.  An L4 summary
#: removes the message entirely, so it never reaches this check.
_GONE_HEADS = ("[Previous: used", "<persisted-output")


#: Calls whose execution may change a file the model has already read.  A read
#: after one of these may be a *necessary* refresh, not a duplicate.
_MUTATING_CALLS = frozenset({"bash", "write", "edit"})


def _mutated_after_read(
    history: Sequence[ChatMessage], read_ids: Sequence[str]
) -> bool:
    """Did a mutating call happen after the last result of ``read_ids``?

    Pure over the assembled history: no IO, no session.  A failed mutation still
    counts -- the file may have changed even when the tool refused (the stale
    write that started the deadlock did).
    """
    read_index = -1
    ids = set(read_ids)
    for index, message in enumerate(history):
        if message.role == "tool" and (message.tool_call_id or "") in ids:
            read_index = index
    if read_index < 0:
        return False
    for message in history[read_index + 1 :]:
        for call in getattr(message, "tool_calls", ()) or ():
            if str(getattr(call, "name", "")).lower() in _MUTATING_CALLS:
                return True
    return False


def _visible_tool_ids(history: Sequence[ChatMessage]) -> frozenset[str]:
    """Call ids whose tool result is still fully in the request's history (O-1).

    ``ToolContext.messages`` is by construction the assembled history -- what
    the model can still see (``tools/base.py``).  Reading it keeps this guard
    pure: no IO, no session, no clock.
    """
    visible: set[str] = set()
    for message in history:
        if message.role != "tool" or not message.tool_call_id:
            continue
        body = message.content or ""
        if any(body.startswith(head) for head in _GONE_HEADS):
            continue
        visible.add(message.tool_call_id)
    return frozenset(visible)


class DuplicateCallGuard:
    """Refuse a call that repeats one this turn has already made too often.

    The classic ReAct failure is the model asking for the same ``read``/``bash``
    over and over; the refusal names the earlier call so it can reuse that result
    instead of re-deriving it (docs/r4-analysis.md 5.2).

    **What counts is the success, not the attempt.**  A failed call produced no
    result to reuse, so telling the model to reuse it teaches it to stop asking
    -- exactly backwards when the failure was its own wrong argument.  Measured
    in 7e48aeef turn 1: three attempts at one unreadable path, the third refused
    with "reuse the earlier result" naming a call that had only ever errored.
    So ``check`` no longer counts; ``after`` does, and only on success.  The
    protection is unchanged for what it was written for: a call that *worked*
    twice has a result worth reusing, and those calls are counted.

    The two are the same shape as :meth:`FailedRepeatGuard.after` on purpose --
    one guard counts successes, the other failures, and between them a call's
    outcome decides which budget it lands in.
    """

    name = "duplicate_call"

    def __init__(self, threshold: int = 2) -> None:
        self.threshold = max(0, int(threshold))
        self.reset()

    def reset(self) -> None:
        #: Fingerprint -> the ids of its successful calls, oldest first.  The id
        #: (not just the count) is kept so ``check`` can ask which results the
        #: model still has in front of it (O-1).
        self._successes: dict[str, list[str]] = {}

    def check(self, call: ToolCall, ctx: ToolContext, view: TurnView) -> GuardDecision:
        del view
        if self.threshold <= 0:
            return GuardDecision(GuardVerdict.ALLOW)
        # ``list`` is a read and ``update`` is a state setter: repeating the
        # same one changes nothing, and refusing it taught the model to retry
        # (trace 2026-10-18: eight identical refusals in one turn).  ``add`` is
        # not idempotent -- a repeat appends a second item -- so it stays here.
        if call.name == "todo_write" and str(call.params.get("action", "")).lower() in (
            "list",
            "update",
        ):
            return GuardDecision(GuardVerdict.ALLOW)
        fingerprint = call_fingerprint(call)
        succeeded = self._successes.get(fingerprint, [])
        if call.name.lower() in _REREADABLE:
            return self._check_reread(
                call, succeeded, _visible_tool_ids(ctx.messages), ctx.messages
            )
        if len(succeeded) >= self.threshold:
            return self._refusal(call, succeeded[0], len(succeeded))
        return GuardDecision(GuardVerdict.ALLOW)

    def _check_reread(
        self,
        call: ToolCall,
        succeeded: Sequence[str],
        visible: frozenset[str],
        history: Sequence[ChatMessage],
    ) -> GuardDecision:
        """A read is only "duplicate" while its earlier output is still there.

        O-1 (s3 turn-1): after L3 persisted every 40 KB block and L4 summarised
        the batch away, re-reading the same range was the only way to recover
        the content -- and the refusal pointed at a result that no longer
        existed.  The total is still capped so one range cannot be re-read
        forever.

        The second exception is a file that changed after the read.  Measured
        live 2026-10-31: ``write`` refused with "file has been modified
        externally; re-read it first", the model tried to re-read, and this
        guard refused it as a duplicate because the *old* result was still
        visible.  The two rules deadlocked one item for 75 iterations.  A
        ``bash``/``write``/``edit`` call after the read means the earlier body
        may no longer describe the file, so the re-read is allowed exactly once
        (the next read's result is newer than the mutation, so a further repeat
        is refused again).
        """
        if _mutated_after_read(history, succeeded):
            return GuardDecision(GuardVerdict.ALLOW)
        shown = [call_id for call_id in succeeded if call_id in visible]
        if len(shown) >= self.threshold:
            return self._refusal(call, shown[0], len(shown))
        if len(succeeded) >= self.threshold * _UNVISIBLE_REREAD_FACTOR:
            return self._refusal(
                call, succeeded[0], len(succeeded), reusable=False
            )
        return GuardDecision(GuardVerdict.ALLOW)

    def _refusal(
        self, call: ToolCall, earlier: str, previous: int, *, reusable: bool = True
    ) -> GuardDecision:
        if reusable:
            guidance = _DUPLICATE_GUIDANCE.get(
                call.name.lower(), _DUPLICATE_GUIDANCE_DEFAULT
            )
            reason = (
                f"'{call.name}' already succeeded {previous} time(s) this turn "
                f"with the same arguments; {guidance}"
            )
        else:
            reason = (
                f"'{call.name}' already succeeded {previous} time(s) this turn "
                "with the same arguments and its earlier output is no longer "
                "fully in the context; read a different range, or use grep for "
                "a search or count"
            )
        if earlier:
            reason += f" (call {earlier})"
        return GuardDecision(GuardVerdict.DENY, reason=reason, guard=self.name)

    def after(
        self, call: ToolCall, result: ToolResult, ctx: ToolContext, view: TurnView
    ) -> ToolResult:
        """Count a call that worked; forget one that didn't.

        A failure deletes the fingerprint instead of adding to it, so a model
        that fixes its arguments and retries pays nothing for the attempt that
        taught it the fix.
        """
        del ctx, view
        if self.threshold <= 0:
            return result
        fingerprint = call_fingerprint(call)
        if result.success:
            self._successes.setdefault(fingerprint, []).append(call.id)
        else:
            self._successes.pop(fingerprint, None)
        return result


class FailedRepeatGuard:
    """Refuse repeating a call that already failed too often this turn.

    Dormant until the strategy/fatigue ladder arms it through
    ``TurnView.strict_repeats``: the L2 response is a *structural* refusal, not
    another nudge (13 §2.2).  Counting runs even while dormant -- the failures
    that justify an L2 refusal happened before it armed -- and any success
    deletes the fingerprint's count, so a call that has worked once (the
    "fix, then rerun the same command" flow) is never blocked by its earlier
    failures.
    """

    name = "failed_repeat"

    def __init__(self, threshold: int = 2) -> None:
        self.threshold = max(1, int(threshold))
        self.reset()

    def reset(self) -> None:
        self._failures: Counter[str] = Counter()

    def check(self, call: ToolCall, ctx: ToolContext, view: TurnView) -> GuardDecision:
        del ctx
        if not view.strict_repeats:
            return GuardDecision(GuardVerdict.ALLOW)
        fingerprint = call_fingerprint(call)
        failures = self._failures[fingerprint]
        if failures >= self.threshold:
            return GuardDecision(
                GuardVerdict.DENY,
                reason=(
                    f"'{call.name}' already failed {failures} time(s) this turn with "
                    "the same arguments; read the error and change approach, or ask the user"
                ),
                guard=self.name,
            )
        return GuardDecision(GuardVerdict.ALLOW)

    def after(
        self, call: ToolCall, result: ToolResult, ctx: ToolContext, view: TurnView
    ) -> ToolResult:
        del ctx, view
        fingerprint = call_fingerprint(call)
        if result.success:
            self._failures.pop(fingerprint, None)
        else:
            self._failures[fingerprint] += 1
        return result


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
        if call.name not in ("bash", "shell_read"):
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
        if call.name not in ("bash", "shell_read"):
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
