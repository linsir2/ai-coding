"""TDD (R8c): streamed tool output reaches the console, and only the console.

The viewer is deliberately dumb: one throttled line per call, and the same
``ToolFinished`` preview as before when the call ends (docs/r8-analysis.md 3.3-S1).
"""

from rich.console import Console

from ai_coding.config.models import UIConfig
from ai_coding.core.events import ToolChunk
from ai_coding.ui.console import ConsoleObserver, DialogueConsole


def _recording() -> tuple[ConsoleObserver, Console]:
    console = Console(record=True, width=200)
    return ConsoleObserver(DialogueConsole(console)), console


def test_r8_the_console_shows_a_chunk_line():
    observer, console = _recording()
    observer.on_event(ToolChunk("c1", "bash", "stdout", "step-1\n", 0))
    assert "step-1" in console.export_text()


def test_r8_chunks_are_throttled_per_call():
    clock = [0.0]
    console = Console(record=True, width=200)
    observer = ConsoleObserver(DialogueConsole(console), clock=lambda: clock[0])

    observer.on_event(ToolChunk("c1", "bash", "stdout", "one\n", 0))
    observer.on_event(ToolChunk("c1", "bash", "stdout", "two\n", 1))  # same window
    clock[0] += 0.2
    observer.on_event(ToolChunk("c1", "bash", "stdout", "three\n", 2))
    observer.on_event(ToolChunk("c2", "bash", "stdout", "other-call\n", 0))

    text = console.export_text()
    assert "one" in text
    assert "three" in text
    assert "other-call" in text  # a different call has its own window
    assert "two" not in text


def test_r8_long_chunks_are_cut_to_one_readable_line():
    observer, console = _recording()
    observer.on_event(ToolChunk("c1", "bash", "stdout", "x" * 5000, 0))
    line = console.export_text().strip()
    assert len(line) < 200
    assert line.endswith("…")


def test_r8_streaming_is_on_by_default_and_can_be_turned_off():
    assert UIConfig().tool_chunks is True
    assert UIConfig(tool_chunks=False).tool_chunks is False


def test_r8_turning_the_switch_off_silences_the_console():
    """`ui.tool_chunks: false` must really mean "no streamed lines"."""
    console = Console(record=True, width=200)
    observer = ConsoleObserver(DialogueConsole(console), chunks=False)
    observer.on_event(ToolChunk("c1", "bash", "stdout", "hidden\n", 0))
    assert console.export_text().strip() == ""
