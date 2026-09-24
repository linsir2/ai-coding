"""TDD (R4b): the post-execution guard that keeps secrets out of the history.

Traces are redacted unconditionally, but whether the *model's* view is redacted is
a policy choice: it changes what the model sees, so it is off by default and only
the operator can turn it on (docs/r4-analysis.md 5.2, D-R4-8).
"""

from dataclasses import replace  # noqa: F401  (kept for clarity of the contract)

from ai_coding.core.guard import GuardChain, SecretGuard, TurnView
from ai_coding.domain.message import ToolCall, ToolResult
from ai_coding.tools.base import ToolContext

SECRET = "sk-abcdefghijklmnopqrstuvwxyz012345"


def _call() -> ToolCall:
    return ToolCall(id="c1", name="bash", params={"command": "env"})


def test_check_never_blocks():
    guard = SecretGuard()
    assert guard.check(_call(), ToolContext(), TurnView()).verdict.value == "allow"


def test_history_is_left_alone_by_default():
    chain = GuardChain([SecretGuard()])
    result = ToolResult(success=True, output=f"export KEY={SECRET}")

    out = chain.after(_call(), result, ToolContext(), TurnView())

    assert SECRET in out.output


def test_history_is_redacted_when_asked():
    chain = GuardChain([SecretGuard(redact_history=True)])
    result = ToolResult(success=True, output=f"export KEY={SECRET}")

    out = chain.after(_call(), result, ToolContext(), TurnView())

    assert SECRET not in out.output
    assert "redacted" in out.output


def test_redaction_is_not_applied_to_other_guards_results():
    """A chain with no post-guard passes the result through untouched."""
    chain = GuardChain([])
    result = ToolResult(success=True, output=f"KEY={SECRET}")
    assert chain.after(_call(), result, ToolContext(), TurnView()) is result


def test_a_failing_post_guard_cannot_break_the_chain():
    class _Boom:
        name = "boom"

        def after(self, call, result, ctx, view):  # type: ignore[no-untyped-def]
            raise RuntimeError("post guard exploded")

    chain = GuardChain([_Boom(), SecretGuard(redact_history=True)])
    result = ToolResult(success=True, output=f"KEY={SECRET}")

    out = chain.after(_call(), result, ToolContext(), TurnView())

    assert SECRET not in out.output
