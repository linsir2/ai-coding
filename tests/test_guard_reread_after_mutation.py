"""A file changed after a read: the read must be allowed once, not refused.

Measured live 2026-10-31 (session c77c0a68, item 2): ``write`` refused with
"file has been modified externally; re-read it first", the model tried to
re-read the same file, and the duplicate-call guard refused it because the
*old* result was still visible.  The two protections deadlocked the item for 75
iterations (15 ``read`` refusals, 5 ``bash`` refusals).  A mutation after the
read (even one that failed after reaching the tool) is the refresh signal.
Calls refused by a guard or an unanswered approval never ran, so they are not
mutations and must not reset the duplicate budget.
"""

from ai_coding.core.guard import DuplicateCallGuard, TurnView
from ai_coding.domain.message import ChatMessage, ToolCall, ToolCallRef, ToolResult
from ai_coding.tools.base import ToolContext


def _read(call_id: str) -> ToolCall:
    return ToolCall(id=call_id, name="read", params={"path": "a.py"})


def _bash(call_id: str) -> ToolCall:
    return ToolCall(id=call_id, name="bash", params={"command": "pytest -q"})


def _ctx(messages: list[ChatMessage]) -> ToolContext:
    return ToolContext(messages=tuple(messages))


def _assistant_call(call_id: str, name: str) -> ChatMessage:
    return ChatMessage(
        role="assistant",
        content="",
        timestamp="",
        tool_calls=[ToolCallRef(id=call_id, name=name, arguments="{}")],
    )


def _tool_result(
    call_id: str, body: str = "body of a.py", *, tool_name: str = "read"
) -> ChatMessage:
    return ChatMessage(
        role="tool", content=body, timestamp="", tool_call_id=call_id, tool_name=tool_name
    )


def _success(guard: DuplicateCallGuard, call: ToolCall) -> None:
    guard.after(call, ToolResult(success=True, output="ok"), _ctx([]), TurnView())


def test_a_reread_after_a_mutation_is_allowed_once():
    guard = DuplicateCallGuard(threshold=1)
    _success(guard, _read("r1"))

    history = [
        _assistant_call("r1", "read"),
        _tool_result("r1"),
        _assistant_call("b1", "bash"),
    ]
    decision = guard.check(_read("r2"), _ctx(history), TurnView())

    assert decision.verdict.value == "allow", "the file may have changed; re-read"
    _success(guard, _read("r2"))

    # The refresh landed after the mutation, so another repeat is a duplicate.
    history.append(_assistant_call("r2", "read"))
    history.append(_tool_result("r2"))
    after = guard.check(_read("r3"), _ctx(history), TurnView())
    assert after.verdict.value == "deny"


def test_a_bash_command_may_repeat_after_a_mutation():
    """Re-running the suite after an edit is the repair loop; without a change
    the repeat is still the duplicate the guard exists to stop."""
    guard = DuplicateCallGuard(threshold=1)
    _success(guard, _bash("b1"))

    history = [
        _assistant_call("b1", "bash"),
        _tool_result("b1"),
        _assistant_call("w1", "write"),
    ]
    decision = guard.check(_bash("b2"), _ctx(history), TurnView())

    assert decision.verdict.value == "allow", "the workspace changed; re-run"

    no_mutation = [_assistant_call("b1", "bash"), _tool_result("b1")]
    repeat = guard.check(_bash("b3"), _ctx(no_mutation), TurnView())
    assert repeat.verdict.value == "deny"


def test_a_reread_without_a_mutation_is_still_a_duplicate():
    guard = DuplicateCallGuard(threshold=1)
    _success(guard, _read("r1"))

    history = [_assistant_call("r1", "read"), _tool_result("r1")]
    decision = guard.check(_read("r2"), _ctx(history), TurnView())

    assert decision.verdict.value == "deny"


def test_a_mutation_before_the_read_does_not_excuse_it():
    guard = DuplicateCallGuard(threshold=1)
    _success(guard, _read("r1"))

    history = [
        _assistant_call("b1", "bash"),
        _assistant_call("r1", "read"),
        _tool_result("r1"),
    ]
    decision = guard.check(_read("r2"), _ctx(history), TurnView())

    assert decision.verdict.value == "deny"


def test_a_repeated_todo_update_is_allowed_but_add_is_not():
    """``list`` is a read and ``update`` is a state setter; ``add`` is not
    idempotent, so a repeat would append a second item."""
    guard = DuplicateCallGuard(threshold=1)
    update = ToolCall(
        id="u1", name="todo_write", params={"action": "update", "id": "1", "status": "in_progress"}
    )
    _success(guard, update)
    repeated = guard.check(update, _ctx([]), TurnView())
    assert repeated.verdict.value == "allow"

    add = ToolCall(id="a1", name="todo_write", params={"action": "add", "content": "x"})
    _success(guard, add)
    repeated_add = guard.check(add, _ctx([]), TurnView())
    assert repeated_add.verdict.value == "deny"


def test_a_repeated_bash_is_not_excused_by_its_own_current_call():
    """The current assistant message is already in ``ctx.messages``.

    Counting that message's own ``bash`` call as a mutation-after-read made
    every repeated bash look like the repair loop, so the duplicate guard never
    fired in the real trace (14 identical ``find`` calls).
    """
    guard = DuplicateCallGuard(threshold=1)
    _success(guard, _bash("b1"))

    history = [
        _assistant_call("b1", "bash"),
        _tool_result("b1"),
        _assistant_call("b2", "bash"),  # the call being checked
    ]
    decision = guard.check(_bash("b2"), _ctx(history), TurnView())

    assert decision.verdict.value == "deny"


def test_a_guard_denied_bash_does_not_excuse_its_own_retry():
    """A guard refusal never ran the command, so it cannot count as a mutation.

    Measured in session fe53ff6f turn 6: the third identical ``tail`` was
    refused, then the fourth was allowed because the *refused* third call sat
    between the earlier result and the retry.  The guard reset itself and the
    model looped approve/deny/approve.
    """
    guard = DuplicateCallGuard(threshold=1)
    _success(guard, _bash("b1"))

    history = [
        _assistant_call("b1", "bash"),
        _tool_result("b1", "same output", tool_name="bash"),
        _assistant_call("b2", "bash"),
        _tool_result(
            "b2",
            '{"decision": "deny", "guard": "duplicate_call", "tool": "bash"}',
            tool_name="bash",
        ),
    ]
    decision = guard.check(_bash("b3"), _ctx(history), TurnView())

    assert decision.verdict.value == "deny"


def test_an_unanswered_approval_does_not_excuse_the_next_retry():
    """``approval_timeout``/``not_approved`` also never reached the workspace."""
    guard = DuplicateCallGuard(threshold=1)
    _success(guard, _bash("b1"))

    history = [
        _assistant_call("b1", "bash"),
        _tool_result("b1", "same output", tool_name="bash"),
        _assistant_call("b2", "bash"),
        _tool_result(
            "b2",
            '{"decision": "warn", "approved": false, "reason": "approval_timeout"}',
            tool_name="bash",
        ),
    ]
    decision = guard.check(_bash("b3"), _ctx(history), TurnView())

    assert decision.verdict.value == "deny"
