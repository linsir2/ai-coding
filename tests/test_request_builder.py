"""TDD (R5): assembling one request — the volatile tail and the tool context.

The tail is where the cache discipline lives: it is the **only** region that
changes between iterations, so it gets a name, an order and a test instead of
being 30 inline lines inside the loop (docs/r5-analysis.md 3.4).
"""

from ai_coding.core.request_builder import (
    TailBlock,
    build_tail,
    content_of,
    tool_context,
    volatile_block,
)
from ai_coding.domain.message import ChatMessage
from ai_coding.domain.task import TaskState


class _Session:
    def __init__(self, session_id: str = "s1") -> None:
        self.session_id = session_id
        # 18 §10.1: the double follows the widened interface (tool_context
        # reads the read-only switch off the session).
        self.readonly = False


def _block(name: str, content: str) -> TailBlock:
    return TailBlock(name=name, message=ChatMessage(role="user", content=content, timestamp=""))


def test_blocks_keep_their_declared_order():
    memory = _block("memory", "MEM")
    task = _block("task", "TASK")
    hook = _block("hook", "HOOK")

    messages, sizes = build_tail([memory, task, hook])

    assert [m.content for m in messages] == ["MEM", "TASK", "HOOK"]
    assert sizes == (("memory", 3), ("task", 4), ("hook", 4))


def test_empty_and_missing_blocks_are_skipped():
    messages, sizes = build_tail([None, _block("memory", "MEM"), None])
    assert [m.content for m in messages] == ["MEM"]
    assert sizes == (("memory", 3),)


def test_an_empty_tail_is_legal():
    assert build_tail([]) == ([], ())
    assert build_tail([None]) == ([], ())


def test_the_size_table_matches_the_actual_text():
    block = _block("task", "x" * 123)
    _, sizes = build_tail([block])
    assert sizes[0][1] == 123


def test_tail_block_chars_tolerates_empty_content():
    empty = TailBlock(
        name="memory", message=ChatMessage(role="user", content=None, timestamp="")
    )
    assert empty.chars == 0


# ---- R7a: the two helpers the loop used to keep privately -------------------


def test_volatile_block_returns_none_when_there_is_nothing_to_say():
    assert volatile_block("memory", "") is None
    assert volatile_block("memory", None) is None


def test_volatile_block_wraps_text_as_a_user_message():
    block = volatile_block("digest", "<working-memory>x</working-memory>")
    assert block is not None
    assert block.name == "digest"
    assert block.message.role == "user"
    assert block.message.content == "<working-memory>x</working-memory>"
    assert block.message.timestamp == ""  # never persisted, so no clock


def test_content_of_tolerates_a_missing_message():
    assert content_of(None) is None
    message = ChatMessage(role="user", content="hi", timestamp="")
    assert content_of(message) == "hi"


def test_tool_context_carries_everything_a_tool_needs():
    emitted: list[object] = []
    task = TaskState(goal="do the thing")
    history = [ChatMessage(role="user", content="hi", timestamp="")]

    ctx = tool_context(
        task=task,
        workspace="/tmp/ws",
        session=_Session("sess-9"),
        history=history,
        instructions="SYS",
        emit=emitted.append,
    )

    assert ctx.task is task
    assert ctx.workspace == "/tmp/ws"
    assert ctx.session_id == "sess-9"
    assert ctx.messages == tuple(history)
    assert ctx.instructions == "SYS"
    ctx.emit("event")
    assert emitted == ["event"]


# ---- G8-1: the pressure block is just another tail block --------------------


def test_a_budget_block_joins_the_tail_in_declared_order():
    """The tail is the only region allowed to change between iterations, which
    is exactly what a counter describing *this* iteration needs."""
    from ai_coding.core.events import preview
    from ai_coding.core.request_builder import build_tail, volatile_block

    messages, sizes = build_tail(
        [
            volatile_block("memory", None),
            volatile_block("budget", "[budget] 快用完了"),
            volatile_block("digest", "d"),
        ]
    )

    assert sizes == (("budget", len("[budget] 快用完了")), ("digest", 1))
    assert preview(messages[0].content) == "[budget] 快用完了"
