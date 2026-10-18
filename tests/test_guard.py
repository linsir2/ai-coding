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
from ai_coding.domain.message import ToolCall, ToolResult
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
    """A call that *succeeded* twice is refused the third time.

    The counting moved from ``check`` to ``after`` (hotel D1-a): a failure is
    not a result worth reusing, so only successes enter the budget.  The
    protection this guard was written for is unchanged -- see
    ``test_duplicate_guard_does_not_refuse_a_retry_that_failed`` for the other
    half of the new contract.
    """
    guard = DuplicateCallGuard(threshold=2)
    view = TurnView()
    call = _call("read", path="a.txt")
    ctx = ToolContext()

    assert guard.check(call, ctx, view).verdict is GuardVerdict.ALLOW
    guard.after(call, ToolResult(success=True, output="ok"), ctx, view)
    assert guard.check(call, ctx, view).verdict is GuardVerdict.ALLOW  # one retry is fine
    guard.after(call, ToolResult(success=True, output="ok"), ctx, view)
    third = guard.check(call, ctx, view)
    assert third.verdict is GuardVerdict.DENY
    assert third.guard == "duplicate_call"
    # the refusal points at the earlier result so the model can reuse it
    assert "c1" in third.reason


def test_duplicate_guard_resets_between_turns():
    guard = DuplicateCallGuard(threshold=1)
    view = TurnView()
    ctx = ToolContext()
    call = _call("read", path="a.txt")
    guard.check(call, ctx, view)
    guard.after(call, ToolResult(success=True, output="ok"), ctx, view)
    assert guard.check(call, ctx, view).verdict is GuardVerdict.DENY

    guard.reset()

    assert guard.check(call, ctx, view).verdict is GuardVerdict.ALLOW


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


# ---- hotel D1-a: a failure is not a result worth reusing


def _fail() -> ToolResult:
    return ToolResult(success=False, output="boom", error="tool_error")


def _ok() -> ToolResult:
    return ToolResult(success=True, output="fine")


def test_duplicate_guard_does_not_refuse_a_retry_that_failed():
    """Measured in 7e48aeef turn 1: the model asked for the same unreadable
    path three times -- a correct response to a wrong path -- and the third was
    refused with "reuse the earlier result", naming a call that had produced an
    error, not a result.  There is nothing to reuse, so the refusal only taught
    the model to stop asking.  A failure must not count toward the duplicate
    budget; the retry is the fix.
    """
    guard = DuplicateCallGuard(threshold=2)
    view, ctx = TurnView(), ToolContext()
    call = _call("read", path="/outside/requirements.txt")
    for _ in range(4):
        decision = guard.check(call, ctx, view)
        assert decision.verdict is GuardVerdict.ALLOW
        guard.after(call, _fail(), ctx, view)


def test_duplicate_guard_still_refuses_a_third_successful_repeat():
    """The protection itself is unchanged: a call that worked twice has a result
    worth reusing, and that is exactly when the refusal is the useful message."""
    guard = DuplicateCallGuard(threshold=2)
    view, ctx = TurnView(), ToolContext()
    call = _call("read", path="a.txt")
    for _ in range(2):
        assert guard.check(call, ctx, view).verdict is GuardVerdict.ALLOW
        guard.after(call, _ok(), ctx, view)
    third = guard.check(call, ctx, view)
    assert third.verdict is GuardVerdict.DENY
    assert "reuse the earlier result" in third.reason


def test_duplicate_guard_names_no_call_when_every_attempt_failed():
    """The refusal points at the earlier call so the model can reuse it.  With
    only failures on record there is no such call, and naming one would send the
    model back to an error."""
    guard = DuplicateCallGuard(threshold=1)
    view, ctx = TurnView(), ToolContext()
    call = _call("read", path="a.txt")
    guard.check(call, ctx, view)
    guard.after(call, _fail(), ctx, view)
    assert guard.check(call, ctx, view).verdict is GuardVerdict.ALLOW
    # force the count up so a refusal is possible at all, and it names nothing
    guard.check(call, ctx, view)
    guard.after(call, _ok(), ctx, view)
    guard.check(call, ctx, view)
    guard.after(call, _ok(), ctx, view)
    refused = guard.check(call, ctx, view)
    assert refused.verdict is GuardVerdict.DENY
    assert "(call " in refused.reason  # the successes gave it something to name


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


# ---- Q4: the refusal names the right next move, per tool --------------------
#
# "reuse the earlier result" is exactly right for a read and meaningless for an
# ask (the result is the human's answer, not a value to reuse) or a write (the
# side effect is already done).  Measured in trace ec89d5ee: the model read a
# refusal that did not match its call and retried with reworded arguments.


def _succeed_once(guard, call, ctx, view):
    assert guard.check(call, ctx, view).verdict is GuardVerdict.ALLOW
    guard.after(call, ToolResult(success=True, output="ok"), ctx, view)


def test_a_repeated_read_still_says_reuse_the_result():
    guard = DuplicateCallGuard(threshold=1)
    view, ctx = TurnView(), ToolContext()
    call = _call("read", path="a.txt")
    _succeed_once(guard, call, ctx, view)
    assert "reuse the earlier result" in guard.check(call, ctx, view).reason


def test_a_repeated_ask_user_says_the_question_was_already_answered():
    guard = DuplicateCallGuard(threshold=1)
    view, ctx = TurnView(), ToolContext()
    call = _call("ask_user", question="怎么办？")
    _succeed_once(guard, call, ctx, view)
    reason = guard.check(call, ctx, view).reason
    assert "reuse the earlier result" not in reason
    assert "answer" in reason


def test_a_repeated_write_names_the_write_not_a_result():
    guard = DuplicateCallGuard(threshold=1)
    view, ctx = TurnView(), ToolContext()
    call = _call("write", path="a.py", content="x")
    _succeed_once(guard, call, ctx, view)
    reason = guard.check(call, ctx, view).reason
    assert "reuse the earlier result" not in reason
    assert "file" in reason


def test_a_repeated_edit_says_the_file_already_reflects_it():
    guard = DuplicateCallGuard(threshold=1)
    view, ctx = TurnView(), ToolContext()
    call = _call("edit", path="a.py")
    _succeed_once(guard, call, ctx, view)
    reason = guard.check(call, ctx, view).reason
    assert "reuse the earlier result" not in reason
    assert "file" in reason
