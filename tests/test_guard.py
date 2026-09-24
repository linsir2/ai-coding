"""TDD (R4b): the guard chain and its built-in guards.

A guard is a *stateful* check: the permission gate asks "is this dangerous?",
a guard asks "should this still happen now, given what this turn has already
done?" (docs/r4-analysis.md 5.1/5.2).
"""


from ai_coding.core.guard import (
    BashPolicyGuard,
    DuplicateCallGuard,
    GuardChain,
    GuardDecision,
    GuardVerdict,
    PathEscapeGuard,
    TurnView,
    call_fingerprint,
)
from ai_coding.domain.message import ToolCall
from ai_coding.tools.base import ToolContext


def _call(name: str = "read", **params) -> ToolCall:
    return ToolCall(id="c1", name=name, params=dict(params))


class _Deny:
    name = "always-deny"

    def check(self, call, ctx, view):  # type: ignore[no-untyped-def]
        return GuardDecision(GuardVerdict.DENY, reason="no", guard=self.name)


class _Rewrite:
    name = "rewrite"

    def check(self, call, ctx, view):  # type: ignore[no-untyped-def]
        return GuardDecision(
            GuardVerdict.REWRITE, params={"path": "other"}, guard=self.name
        )


class _Boom:
    name = "boom"

    def check(self, call, ctx, view):  # type: ignore[no-untyped-def]
        raise RuntimeError("guard exploded")


def test_an_empty_chain_allows():
    decision = GuardChain([]).check(_call(), ToolContext(), TurnView())
    assert decision.verdict is GuardVerdict.ALLOW


def test_the_first_non_allow_verdict_wins():
    chain = GuardChain([_Rewrite(), _Deny()])
    decision = chain.check(_call(), ToolContext(), TurnView())
    assert decision.verdict is GuardVerdict.REWRITE
    assert decision.params == {"path": "other"}


def test_a_raising_guard_fails_closed():
    decision = GuardChain([_Boom()]).check(_call(), ToolContext(), TurnView())
    assert decision.verdict is GuardVerdict.DENY
    assert decision.guard == "boom"
    assert "exploded" in decision.reason


def test_fingerprint_ignores_parameter_order_but_not_values():
    a = call_fingerprint(_call("read", path="a", limit=1))
    b = call_fingerprint(_call("read", limit=1, path="a"))
    c = call_fingerprint(_call("read", path="b", limit=1))
    assert a == b
    assert a != c


# ---- DuplicateCallGuard ----------------------------------------------------


def test_duplicate_guard_allows_a_retry_then_refuses():
    guard = DuplicateCallGuard(threshold=2)
    view = TurnView()
    call = _call("read", path="a.txt")
    ctx = ToolContext()

    assert guard.check(call, ctx, view).verdict is GuardVerdict.ALLOW
    assert guard.check(call, ctx, view).verdict is GuardVerdict.ALLOW  # one retry is fine
    third = guard.check(call, ctx, view)
    assert third.verdict is GuardVerdict.DENY
    assert third.guard == "duplicate_call"
    # the refusal points at the earlier result so the model can reuse it
    assert "c1" in third.reason


def test_duplicate_guard_resets_between_turns():
    guard = DuplicateCallGuard(threshold=1)
    ctx = ToolContext()
    call = _call("read", path="a.txt")
    guard.check(call, ctx, TurnView())
    assert guard.check(call, ctx, TurnView()).verdict is GuardVerdict.DENY

    guard.reset()

    assert guard.check(call, ctx, TurnView()).verdict is GuardVerdict.ALLOW


def test_duplicate_guard_does_not_touch_different_parameters():
    guard = DuplicateCallGuard(threshold=1)
    ctx = ToolContext()
    guard.check(_call("read", path="a.txt"), ctx, TurnView())

    assert (
        guard.check(_call("read", path="b.txt"), ctx, TurnView()).verdict
        is GuardVerdict.ALLOW
    )


def test_duplicate_guard_is_off_at_threshold_zero():
    guard = DuplicateCallGuard(threshold=0)
    ctx = ToolContext()
    call = _call("bash", command="ls")
    for _ in range(5):
        assert guard.check(call, ctx, TurnView()).verdict is GuardVerdict.ALLOW


# ---- BashPolicyGuard -------------------------------------------------------


def test_bash_policy_guard_adds_user_deny_patterns():
    guard = BashPolicyGuard(deny=["curl "])
    assert (
        guard.check(_call("bash", command="curl example.com"), ToolContext(), TurnView()).verdict
        is GuardVerdict.DENY
    )
    assert (
        guard.check(_call("bash", command="ls -la"), ToolContext(), TurnView()).verdict
        is GuardVerdict.ALLOW
    )


def test_bash_policy_guard_leaves_other_tools_alone():
    guard = BashPolicyGuard(deny=["curl "])
    assert (
        guard.check(_call("read", path="curl notes"), ToolContext(), TurnView()).verdict
        is GuardVerdict.ALLOW
    )


def test_bash_policy_guard_with_empty_tables_allows_everything():
    guard = BashPolicyGuard()
    assert (
        guard.check(_call("bash", command="rm -rf /"), ToolContext(), TurnView()).verdict
        is GuardVerdict.ALLOW
    )


# ---- PathEscapeGuard -------------------------------------------------------


def test_path_escape_guard_only_observes_by_default():
    guard = PathEscapeGuard(strict=False)
    decision = guard.check(
        _call("bash", command="cat /etc/passwd"), ToolContext(workspace="/tmp/ws"), TurnView()
    )
    assert decision.verdict is GuardVerdict.ALLOW
    assert decision.reason  # but it said something


def test_path_escape_guard_denies_when_strict():
    guard = PathEscapeGuard(strict=True)
    decision = guard.check(
        _call("bash", command="cd / && ls"), ToolContext(workspace="/tmp/ws"), TurnView()
    )
    assert decision.verdict is GuardVerdict.DENY
    assert decision.guard == "path_escape"


def test_path_escape_guard_ignores_relative_commands():
    guard = PathEscapeGuard(strict=True)
    assert (
        guard.check(
            _call("bash", command="ls -la ./src"),
            ToolContext(workspace="/tmp/ws"),
            TurnView(),
        ).verdict
        is GuardVerdict.ALLOW
    )
