"""blocks_from_event + TranscriptObserver: pure, offline, no Textual."""

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
from ai_coding.ui.blocks import Block, TranscriptObserver, blocks_from_event


def test_iteration_marker():
    block = blocks_from_event(IterationStarted(index=1, max_iterations=8))
    assert block is not None
    assert "1/8" in block.text
    assert block.kind == "turn"


def test_tool_started_names_the_tool_and_previews_params():
    block = blocks_from_event(
        ToolStarted(call_id="c1", tool_name="bash", params={"command": "ls -la"})
    )
    assert block is not None
    assert "bash" in block.text
    assert "ls -la" in block.text


def test_tool_finished_marks_success_and_failure():
    ok = blocks_from_event(
        ToolFinished(call_id="c1", tool_name="bash", success=True, output_preview="done")
    )
    bad = blocks_from_event(
        ToolFinished(call_id="c2", tool_name="bash", success=False, error="boom")
    )
    assert ok is not None and ok.text.startswith("✓")
    assert bad is not None and bad.text.startswith("✗")


def test_completed_turn_is_silent():
    assert blocks_from_event(TurnFinished(outcome=TurnOutcome(status=TurnStatus.COMPLETED))) is None


def test_timed_out_turn_says_so():
    block = blocks_from_event(
        TurnFinished(outcome=TurnOutcome(status=TurnStatus.TIMEOUT, reason="deadline"))
    )
    assert block is not None
    assert "timed out" in block.text


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
        def render(self, *, max_items: int = 5) -> str:
            return "[ ] 1. 分析代码结构"

    block = blocks_from_event(TaskUpdated(task=_Task()))
    assert block is not None
    assert "分析代码结构" in block.text


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
