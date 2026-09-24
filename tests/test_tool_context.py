"""TDD (R1a-1): ToolContext + the parallel_safe contract."""

from ai_coding.domain.task import TaskState
from ai_coding.tools.base import BaseTool, ToolContext, resolve_ctx
from ai_coding.tools.bash_tool import BashTool
from ai_coding.tools.edit_tool import EditTool
from ai_coding.tools.glob_tool import GlobTool
from ai_coding.tools.read_tool import ReadTool
from ai_coding.tools.todo_write_tool import TodoWriteTool
from ai_coding.tools.write_tool import WriteTool


def test_context_carries_task_and_session_id():
    task = TaskState(goal="g")
    ctx = ToolContext(task=task, workspace="/tmp/ws", session_id="sid")
    assert ctx.task is task
    assert ctx.workspace == "/tmp/ws"
    assert ctx.session_id == "sid"
    assert ctx.messages == ()
    assert ctx.instructions is None


def test_context_emit_default_is_noop():
    ctx = ToolContext()
    assert ctx.emit("anything") is None


def test_resolve_ctx_returns_default_when_none():
    ctx = resolve_ctx(None)
    assert isinstance(ctx, ToolContext)
    assert ctx.task.is_complete() is True
    given = ToolContext(session_id="x")
    assert resolve_ctx(given) is given


def test_base_tool_default_parallel_safe_false():
    assert BaseTool.parallel_safe is False
    assert BashTool.parallel_safe is False
    assert WriteTool.parallel_safe is False
    assert EditTool.parallel_safe is False


def test_parallel_safe_overrides_per_tool(tmp_path):
    from ai_coding.security.file_state_tracker import FileStateTracker
    from ai_coding.security.sandbox import Sandbox

    sandbox = Sandbox(str(tmp_path))
    assert ReadTool(sandbox, FileStateTracker()).parallel_safe is True
    assert GlobTool(sandbox).parallel_safe is True
    assert TodoWriteTool().parallel_safe is True


# ---- R7e-1: the turn number travels with the context ------------------------


def test_tool_context_carries_the_turn_number():
    from ai_coding.core.request_builder import tool_context
    from ai_coding.domain.task import TaskState

    class _Session:
        session_id = "s1"

    ctx = tool_context(
        task=TaskState(goal="g"),
        workspace=".",
        session=_Session(),  # type: ignore[arg-type]
        history=[],
        instructions=None,
        emit=lambda _event: None,
        turn=7,
    )
    assert ctx.turn == 7
    assert ctx.session_id == "s1"
