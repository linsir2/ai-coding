"""TDD (13 §2.2): the stuck-aware refusal, dormant unless armed.

Dormant/armed/counting semantics are the whole contract: the guard must be
invisible on an ordinary turn and decisive inside an L2 window.
"""

from ai_coding.core.guard import FailedRepeatGuard, GuardVerdict, TurnView
from ai_coding.domain.message import ToolCall, ToolResult


def _call(**params) -> ToolCall:
    return ToolCall(id="c1", name="bash", params=params or {"command": "pytest"})


def _fail() -> ToolResult:
    return ToolResult(success=False, output="boom", error="failed")


def _ok() -> ToolResult:
    return ToolResult(success=True, output="fine")


def test_dormant_without_arming():
    g = FailedRepeatGuard(threshold=2)
    for _ in range(3):
        g.after(_call(), _fail(), None, TurnView())
    assert g.check(_call(), None, TurnView()).verdict is GuardVerdict.ALLOW


def test_two_failures_deny_the_third_same_call_when_armed():
    g = FailedRepeatGuard(threshold=2)
    armed = TurnView(strict_repeats=True)
    g.after(_call(), _fail(), None, armed)
    g.after(_call(), _fail(), None, armed)
    d = g.check(_call(), None, armed)
    assert d.verdict is GuardVerdict.DENY
    assert "change approach" in d.reason


def test_a_success_deletes_the_fingerprint():
    g = FailedRepeatGuard(threshold=2)
    armed = TurnView(strict_repeats=True)
    g.after(_call(), _fail(), None, armed)
    g.after(_call(), _fail(), None, armed)
    g.after(_call(), _ok(), None, armed)          # 一次成功 → 计数删除
    assert g.check(_call(), None, armed).verdict is GuardVerdict.ALLOW


def test_different_arguments_do_not_share_a_count():
    g = FailedRepeatGuard(threshold=2)
    armed = TurnView(strict_repeats=True)
    g.after(_call(command="a"), _fail(), None, armed)
    g.after(_call(command="b"), _fail(), None, armed)
    assert g.check(_call(command="a"), None, armed).verdict is GuardVerdict.ALLOW


def test_reset_clears_counts_per_turn():
    g = FailedRepeatGuard(threshold=2)
    armed = TurnView(strict_repeats=True)
    g.after(_call(), _fail(), None, armed)
    g.after(_call(), _fail(), None, armed)
    g.reset()
    assert g.check(_call(), None, armed).verdict is GuardVerdict.ALLOW
