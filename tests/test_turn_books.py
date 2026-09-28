"""R7a: a turn's books — the nine counters that used to be nine loose locals.

`process_input` carried iterations / nudges / attempted_calls / successful_calls /
failed_streak / strategy_nudges / failures / last_text / summaries inline across
228 lines, and R6's additions are exactly why the method grew back after R5 split
it.  These tests pin the accounting rules *before* the loop starts using the
object, so the move is a move and not a rewrite (docs/r7-analysis.md 3.1).
"""

from ai_coding.core.dispatch import DispatchOutcome
from ai_coding.core.turn_books import TurnBooks, TurnFrame
from ai_coding.core.turn_policy import TurnSnapshot
from ai_coding.domain.message import ToolCall, ToolResult
from ai_coding.domain.task import TaskState, TodoItem
from ai_coding.infra.deadline import Deadline


def _outcome(
    name: str = "bash",
    *,
    success: bool = True,
    error: str | None = None,
    output: str = "",
    halted: bool = False,
) -> DispatchOutcome:
    return DispatchOutcome(
        call=ToolCall(id=f"c-{name}", name=name),
        result=ToolResult(success=success, output=output, error=error),
        denied=not success,
        halted=halted,
    )


# ---------------------------------------------------------------- defaults


def test_books_start_empty():
    books = TurnBooks()
    assert books.iterations == 0
    assert books.nudges == 0
    assert books.attempted_calls == 0
    assert books.successful_calls == 0
    assert books.failed_streak == 0
    assert books.strategy_nudges == 0
    assert books.failures == []
    assert books.last_text is None
    assert books.summaries == []


# ------------------------------------------------------------------ notes


def test_note_iteration_records_the_index():
    books = TurnBooks()
    books.note_iteration(3)
    assert books.iterations == 3


def test_note_nudge_and_strategy_nudge_are_separate_counters():
    books = TurnBooks()
    books.note_nudge()
    books.note_strategy_nudge()
    books.note_nudge()
    assert books.nudges == 2
    assert books.strategy_nudges == 1


def test_note_step_keeps_the_last_text():
    books = TurnBooks()
    books.note_step("第一句")
    books.note_step(None)
    assert books.last_text is None  # a silent step is still the latest word
    books.note_step("第二句")
    assert books.last_text == "第二句"


# --------------------------------------------------------------- outcomes


def test_note_outcomes_counts_attempts_and_successes():
    books = TurnBooks()
    books.note_outcomes(
        [_outcome("bash"), _outcome("read"), _outcome("edit", success=False, error="boom")]
    )
    assert books.attempted_calls == 3
    assert books.successful_calls == 2


def test_note_outcomes_resets_streak_when_anything_succeeded():
    books = TurnBooks(failed_streak=2)
    books.note_outcomes([_outcome("bash", success=False, error="boom"), _outcome("read")])
    assert books.failed_streak == 0


def test_note_outcomes_raises_streak_when_everything_failed():
    books = TurnBooks()
    books.note_outcomes([_outcome("bash", success=False, error="boom")])
    assert books.failed_streak == 1
    books.note_outcomes([_outcome("bash", success=False, error="boom")])
    assert books.failed_streak == 2


def test_note_outcomes_ignores_an_empty_batch():
    """A plan-only step is not a failure streak (the R6b rule, unchanged)."""
    books = TurnBooks(failed_streak=1)
    books.note_outcomes([])
    assert books.failed_streak == 1
    assert books.attempted_calls == 0


def test_note_outcomes_records_failures_with_a_160_char_excerpt():
    books = TurnBooks()
    books.note_outcomes([_outcome("edit", success=False, error=None, output="x" * 400)])
    assert books.failures == [("edit", "failed", "x" * 160)]


def test_note_outcomes_keeps_successful_calls_out_of_the_failure_list():
    books = TurnBooks()
    books.note_outcomes([_outcome("read"), _outcome("bash", success=False, error="denied")])
    assert books.failures == [("bash", "denied", "")]


# -------------------------------------------------------------- snapshots


def test_snapshot_carries_every_counter():
    books = TurnBooks(
        iterations=2,
        nudges=1,
        attempted_calls=3,
        successful_calls=0,
        failed_streak=2,
        strategy_nudges=1,
    )
    assert books.snapshot(iteration=3, tokens_spent=1000) == TurnSnapshot(
        iteration=3,
        nudges=1,
        tokens_spent=1000,
        failed_streak=2,
        attempted_calls=3,
        successful_calls=0,
        strategy_nudges=1,
    )


def test_snapshot_defaults_open_items_and_final_text():
    snapshot = TurnBooks().snapshot(iteration=1, tokens_spent=0)
    assert snapshot.open_items == ()
    assert snapshot.final_text is None


def test_snapshot_is_the_same_decision_the_policy_makes_by_hand():
    """The snapshot is built here, decided there — and it must decide identically."""
    from ai_coding.core.turn_policy import TurnAction, TurnPolicy

    policy = TurnPolicy()
    books = TurnBooks(iterations=3, attempted_calls=4, successful_calls=0)
    built = books.snapshot(iteration=4, tokens_spent=0)
    by_hand = TurnSnapshot(
        iteration=4,
        nudges=0,
        open_items=(),
        final_text=None,
        tokens_spent=0,
        failed_streak=0,
        attempted_calls=4,
        successful_calls=0,
        strategy_nudges=0,
    )
    assert policy.decide_next_iteration(built) == policy.decide_next_iteration(by_hand)
    assert policy.decide_next_iteration(built).action is TurnAction.FINISH  # the stall ladder


def test_snapshot_passes_open_items_and_final_text_through():
    item = TodoItem(id=1, content="还差一步")
    snapshot = TurnBooks().snapshot(
        iteration=1, tokens_spent=0, open_items=(item,), final_text="做完了"
    )
    assert snapshot.open_items == (item,)
    assert snapshot.final_text == "做完了"


# ------------------------------------------------------------------- frames


def test_a_frame_defaults_to_no_hook_context_and_no_deadline():
    frame = TurnFrame(turn_number=1, task=TaskState(goal="做点事"))
    assert frame.hook_context == ""
    assert frame.deadline is None


def test_a_frame_carries_the_clock_the_prologue_fixed():
    deadline = Deadline.after(30)
    frame = TurnFrame(
        turn_number=2,
        task=TaskState(goal="做点事"),
        hook_context="<hook-context>x",
        deadline=deadline,
    )
    assert frame.turn_number == 2
    assert frame.hook_context == "<hook-context>x"
    assert frame.deadline is deadline
    assert frame.deadline.expired is False


# ---- R7e-2: steering counts, and it restarts the streak ----------------------


def test_note_steering_counts_and_resets_the_failure_streak():
    books = TurnBooks(failed_streak=2, attempted_calls=3, successful_calls=0)
    books.note_steering(2)
    assert books.steering_messages == 2
    assert books.failed_streak == 0  # Q5: 新指令 = 重新开始
    assert books.attempted_calls == 3  # 但工具确实没成功过，计数不动
    assert books.successful_calls == 0


def test_note_steering_defaults_to_one():
    books = TurnBooks()
    books.note_steering()
    assert books.steering_messages == 1


# ------------------------------------------------- R8a: the checkpoint line


def test_r8_checkpoint_collapses_to_one_line_and_caps():
    books = TurnBooks()
    books.note_checkpoint("  第一行\n第二行  ", limit=10)
    assert books.checkpoint == "第一行 第二行"
    books.note_checkpoint("字" * 50, limit=5)
    assert books.checkpoint == "字字字字…"


def test_r8_last_checkpoint_of_a_batch_wins():
    books = TurnBooks()
    books.note_checkpoint("先写的", limit=120)
    books.note_checkpoint("后写的", limit=120)
    assert books.checkpoint == "后写的"


def test_r8_a_blank_checkpoint_never_erases_a_real_one():
    books = TurnBooks()
    books.note_checkpoint("有价值的", limit=120)
    books.note_checkpoint("   \n  ", limit=120)
    assert books.checkpoint == "有价值的"


def test_r8_a_fresh_turn_has_no_checkpoint():
    assert TurnBooks().checkpoint is None


# --------------------------------------- R8a D-14: what counts as evidence


def test_r8_a_checkpoint_write_is_not_world_evidence():
    books = TurnBooks()
    books.note_outcomes([_outcome("checkpoint_write", success=True)])
    assert books.successful_calls == 1  # the stall ladder still sees progress
    assert books.evidence_calls == 0  # but it is not evidence about the world
    assert books.failed_streak == 0


def test_r8_a_real_tool_call_is_evidence():
    books = TurnBooks()
    books.note_outcomes([_outcome("bash", success=True)])
    assert books.evidence_calls == 1


def test_r8_a_failed_call_is_never_evidence():
    books = TurnBooks()
    books.note_outcomes([_outcome("bash", success=False, error="nonzero_exit")])
    assert books.evidence_calls == 0
