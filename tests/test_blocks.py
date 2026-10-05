"""blocks_from_event + TranscriptObserver: pure, offline, no Textual."""

import pytest

from ai_coding.core.events import (
    IterationStarted,
    TaskUpdated,
    ToolChunk,
    ToolFinished,
    ToolStarted,
    ToolSurfaceWarning,
    TurnFinished,
)
from ai_coding.domain.loop import TurnOutcome, TurnStatus
from ai_coding.ui.blocks import (
    Block,
    TranscriptObserver,
    blocks_from_event,
    tool_card_meta,
    tool_title,
)


def test_iteration_started_is_silent():
    # WP1: the event still marks turn boundaries for the trace recorder
    # (trace.py), but the transcript no longer shows per-step "[turn x/y]".
    assert blocks_from_event(IterationStarted(index=1, max_iterations=8)) is None


def test_tool_started_names_the_tool_and_previews_params():
    block = blocks_from_event(
        ToolStarted(call_id="c1", tool_name="bash", params={"command": "ls -la"})
    )
    assert block is not None
    assert block.kind == "tool"
    assert block.tool_name == "bash"
    assert block.call_id == "c1"
    assert block.tool_status == "running"
    assert "ls -la" in block.text


def test_tool_finished_marks_success_and_failure():
    ok = blocks_from_event(
        ToolFinished(call_id="c1", tool_name="bash", success=True, output_preview="done")
    )
    bad = blocks_from_event(
        ToolFinished(call_id="c2", tool_name="bash", success=False, error="boom")
    )
    assert ok is not None and ok.tool_status == "ok"
    assert bad is not None and bad.tool_status == "failed"
    assert ok.call_id == "c1" and ok.tool_name == "bash"


def test_tool_chunk_carries_call_id():
    block = blocks_from_event(ToolChunk("c1", "bash", "stdout", "step-1\n", 0))
    assert block is not None
    assert block.kind == "tool-chunk"
    assert block.call_id == "c1"


def test_tool_target_for_path_tools():
    block = blocks_from_event(
        ToolStarted(call_id="c1", tool_name="read", params={"path": "src/foo.py"})
    )
    assert block is not None
    assert block.tool_target == "src/foo.py"


def test_tool_target_glob_uses_pattern_not_path():
    """``glob_tool.py`` names its argument ``pattern``; reading ``path`` yields ""."""
    block = blocks_from_event(
        ToolStarted(call_id="c1", tool_name="glob", params={"pattern": "**/*.py"})
    )
    assert block is not None
    assert block.tool_target == "**/*.py"


def test_tool_target_write_and_edit_use_path():
    for tool_name in ("write", "edit"):
        block = blocks_from_event(
            ToolStarted(call_id="c1", tool_name=tool_name, params={"path": "src/foo.py"})
        )
        assert block is not None
        assert block.tool_target == "src/foo.py"


def test_tool_target_bash_truncates_long_command():
    block = blocks_from_event(
        ToolStarted(call_id="c1", tool_name="bash", params={"command": "x" * 100})
    )
    assert block is not None
    assert block.tool_target == "x" * 60 + "…"


def test_tool_target_unknown_tool_is_empty():
    block = blocks_from_event(
        ToolStarted(call_id="c1", tool_name="mcp_foo", params={"a": 1})
    )
    assert block is not None
    assert block.tool_target == ""


@pytest.mark.parametrize(
    "tool_name,kind,verb",
    [
        ("bash", "Shell", "运行命令"),
        ("read", "Read", "读取文件"),
        ("edit", "Edit", "编辑文件"),
        ("write", "Write", "写入文件"),
        ("glob", "Glob", "查找文件"),
        ("todo_write", "Todo", "更新计划"),
        ("subAgent", "Subagent", "委派子任务"),
        ("skill", "Skill", "读取技能"),
        ("memory_search", "Memory", "搜索记忆"),
        ("memory_write", "Memory", "记录记忆"),
        ("checkpoint_write", "Checkpoint", "记录检查点"),
        ("ask_user", "Ask", "询问用户"),
        ("mcp__get_sum", "mcp__get_sum", "运行工具"),
        ("future_tool", "future_tool", "运行工具"),
    ],
)
def test_tool_card_meta_table(tool_name, kind, verb):
    assert tool_card_meta(tool_name) == (kind, verb)


def test_started_block_carries_verb_and_kind():
    block = blocks_from_event(
        ToolStarted(call_id="c1", tool_name="bash", params={"command": "ls"})
    )
    assert block is not None
    assert block.kind_label == "Shell"
    assert block.verb == "运行命令"


def test_tool_title_states():
    running = tool_title(mark="▸", verb="运行命令", target="ls", running=True)
    assert running == "▸ 正在运行命令 · ls"
    done = tool_title(mark="✓", verb="运行命令", target="ls")
    assert done == "✓ 已运行命令 · ls"
    failed = tool_title(mark="✗", verb="读取文件", target="a.py")
    assert failed == "✗ 已读取文件 · a.py"
    bare = tool_title(mark="✓", verb="运行工具", target="")
    assert bare == "✓ 已运行工具"


def test_completed_turn_is_silent():
    assert blocks_from_event(TurnFinished(outcome=TurnOutcome(status=TurnStatus.COMPLETED))) is None


def test_timed_out_turn_says_so():
    block = blocks_from_event(
        TurnFinished(outcome=TurnOutcome(status=TurnStatus.TIMEOUT, reason="deadline"))
    )
    assert block is not None
    assert "timed out" in block.text


def test_length_cut_turn_says_truncated():
    # W1: a model cut by max_tokens used to read as "done".  The harness can
    # only detect the length cutoff (a bare stop is indistinguishable), so a
    # set flag must surface it with a way back.
    block = blocks_from_event(
        TurnFinished(
            outcome=TurnOutcome(
                status=TurnStatus.COMPLETED,
                text="half a sentence",
                iterations=2,
                reason="model finished",
                truncated_by_length=True,
            )
        )
    )
    assert block is not None
    assert block.kind == "turn-end"
    assert "截断" in block.text
    assert "继续" in block.text


def test_length_cut_wins_over_budget_notice():
    # Both can be true (last step cut, then the budget fired); the cut
    # explains the visible symptom, so it takes precedence.
    block = blocks_from_event(
        TurnFinished(
            outcome=TurnOutcome(
                status=TurnStatus.MAX_ITERATIONS,
                iterations=5,
                reason="iteration budget exhausted",
                truncated_by_length=True,
            )
        )
    )
    assert block is not None
    assert "截断" in block.text


def test_max_iterations_turn_says_continue():
    # WP2: a budget cut used to end the transcript in silence (only TIMEOUT
    # rendered); the user could not tell "done" from "cut off".
    block = blocks_from_event(
        TurnFinished(
            outcome=TurnOutcome(
                status=TurnStatus.MAX_ITERATIONS,
                iterations=5,
                reason="iteration budget exhausted",
            )
        )
    )
    assert block is not None
    assert block.kind == "turn-end"
    assert "5" in block.text
    assert "继续" in block.text


def test_tool_surface_warning_is_shown():
    """R7e-3c parity: the old console rendered this advisory; the TUI keeps it visible."""
    block = blocks_from_event(
        ToolSurfaceWarning(tools_chars=13_301, threshold=12_000, origins=(("mcp:big", 9000),))
    )
    assert block is not None
    assert "13301" in block.text
    assert "mcp:big" in block.text


def test_task_update_is_shown():
    class _Task:
        done_count = 0
        total = 1

        def render(self, *, max_items: int = 5) -> str:
            return "[ ] 1. 分析代码结构"

        def render_display(self, *, max_items: int = 5) -> str:
            return self.render(max_items=max_items)

    block = blocks_from_event(TaskUpdated(task=_Task()))
    assert block is not None
    assert "分析代码结构" in block.text


def test_task_update_carries_a_one_line_title():
    """TaskUpdated 刷屏的根因：每次全量 render() 挂 Transcript 顶层。

    改后 Block 自带单行标题（折叠用），正文是去标签版 —— 模型那份不动。
    """
    from ai_coding.domain.task import TaskState

    task = TaskState(goal="g")
    task.add("first")
    task.add("second")
    block = blocks_from_event(TaskUpdated(task=task))
    assert block is not None
    assert block.title == "计划 0/2"
    assert "<task-state>" not in block.text
    assert "first" in block.text and "second" in block.text


def test_r8_chunks_reach_the_sink():
    seen: list[Block] = []
    observer = TranscriptObserver(seen.append)
    observer.on_event(ToolChunk("c1", "bash", "stdout", "step-1\n", 0))
    assert any("step-1" in b.text for b in seen)


def test_r8_chunks_are_throttled_per_call():
    seen: list[Block] = []
    clock = [0.0]
    observer = TranscriptObserver(seen.append, clock=lambda: clock[0])
    observer.on_event(ToolChunk("c1", "bash", "stdout", "one\n", 0))
    observer.on_event(ToolChunk("c1", "bash", "stdout", "two\n", 1))  # same window
    clock[0] += 0.2
    observer.on_event(ToolChunk("c1", "bash", "stdout", "three\n", 2))
    observer.on_event(ToolChunk("c2", "bash", "stdout", "other-call\n", 0))
    text = "\n".join(b.text for b in seen)
    assert "one" in text and "three" in text and "other-call" in text
    assert "two" not in text


def test_chunks_can_be_disabled():
    seen: list[Block] = []
    observer = TranscriptObserver(seen.append, chunks=False)
    observer.on_event(ToolChunk("c1", "bash", "stdout", "hidden\n", 0))
    assert seen == []


def test_aborted_turn_renders_interrupted_marker():
    # Esc abort used to end the transcript in silence (only TIMEOUT/MAX
    # rendered); the user could not tell "done" from "interrupted".
    block = blocks_from_event(
        TurnFinished(outcome=TurnOutcome(status=TurnStatus.ABORTED))
    )
    assert block is not None
    assert block.kind == "turn-end"
    assert "已中断" in block.text


def test_abort_wins_over_length_cut():
    # A step can be length-truncated and then the user hits Esc.  The user's
    # action is the authoritative ending; "截断：输入继续可接上" would be a lie.
    block = blocks_from_event(
        TurnFinished(
            outcome=TurnOutcome(
                status=TurnStatus.ABORTED,
                reason="cancelled",
                truncated_by_length=True,
            )
        )
    )
    assert block is not None
    assert "已中断" in block.text
    assert "截断" not in block.text
