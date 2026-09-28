"""R7e-2: one reader for one terminal, and who gets each line.

The reader is driven by an injectable ``prompt`` coroutine, so every case here is
deterministic and needs no terminal - the pty/pipe cases live in the REPL tests.
"""

from __future__ import annotations

import asyncio
from collections import deque

import pytest

from ai_coding.core.steering import SteeringQueue
from ai_coding.ui.reader import InteractiveReader

pytestmark = pytest.mark.asyncio


class _Scripted:
    """A stand-in for ``PromptSession.prompt_async``.

    ``gate`` lets a test hold the read loop at a known point, which is what makes
    "a question was already pending" deterministic instead of a race.
    """

    def __init__(self, lines: list[str], gate: asyncio.Event | None = None) -> None:
        self.lines = deque(lines)
        self.prompts: list[str] = []
        self.gate = gate

    async def __call__(self, message):
        self.prompts.append(message() if callable(message) else str(message))
        if self.gate is not None:
            await self.gate.wait()
        if not self.lines:
            raise EOFError
        return self.lines.popleft()


def _reader(lines: list[str], *, steering: SteeringQueue | None = None, gate=None):
    scripted = _Scripted(lines, gate)
    reader = InteractiveReader(
        prompt=scripted, invalidate=lambda: None, steering=steering
    )
    return reader, scripted


async def test_a_line_goes_to_the_repl_when_nothing_is_running():
    reader, _scripted = _reader(["你好"])
    reader.start()
    try:
        assert await asyncio.wait_for(reader.input(), timeout=2) == "你好"
    finally:
        await reader.aclose()


async def test_a_line_becomes_steering_while_the_turn_is_running():
    queue = SteeringQueue()
    reader, _scripted = _reader(["改用 uv"], steering=queue)
    reader.set_busy(True)  # busy *before* the read loop starts: no race
    reader.start()
    try:
        for _ in range(200):
            if queue.pending:
                break
            await asyncio.sleep(0.01)
        assert queue.drain() == ["改用 uv"]
    finally:
        await reader.aclose()


async def test_a_slash_line_is_held_for_the_repl_even_while_busy():
    queue = SteeringQueue()
    reader, _scripted = _reader(["/status"], steering=queue)
    reader.set_busy(True)
    reader.start()
    try:
        assert await asyncio.wait_for(reader.input(), timeout=2) == "/status"
        assert queue.pending == 0
    finally:
        await reader.aclose()


async def test_a_pending_question_takes_the_next_line():
    gate = asyncio.Event()
    reader, scripted = _reader(["改 A"], gate=gate)
    reader.start()
    try:
        asking = asyncio.create_task(reader.ask("改 A 还是 B？", ["A", "B"]))
        await asyncio.sleep(0)  # let ask() register the question
        gate.set()
        answer = await asyncio.wait_for(asking, timeout=2)
        assert answer.answered is True
        assert answer.text == "改 A"
        assert any("改 A 还是 B？" in text for text in scripted.prompts)
    finally:
        await reader.aclose()


async def test_eof_ends_the_input_stream():
    reader, _scripted = _reader([])
    reader.start()
    try:
        with pytest.raises(EOFError):
            await asyncio.wait_for(reader.input(), timeout=2)
    finally:
        await reader.aclose()


async def test_the_prompt_text_tracks_the_state():
    gate = asyncio.Event()
    reader, scripted = _reader(["x", "y"], gate=gate)
    assert reader._message() == "you> "
    reader.set_busy(True)
    assert reader._message() == "steer> "
    reader.set_busy(False)
    assert reader._message() == "you> "
    await reader.aclose()
    assert scripted is not None


# ---- R8c: Esc asks the turn to drop what it is waiting for ------------------


@pytest.mark.asyncio
async def test_r8_request_cancel_sets_the_event():
    reader = InteractiveReader()
    assert not reader.cancel_requested.is_set()
    reader.request_cancel()
    assert reader.cancel_requested.is_set()


@pytest.mark.asyncio
async def test_r8_the_escape_binding_is_registered_and_silent_until_pressed():
    """The binding exists, and nothing cancels until a key actually arrives."""
    from prompt_toolkit.keys import Keys

    reader = InteractiveReader()
    bindings = reader.key_bindings()
    assert bindings.get_bindings_for_keys((Keys.Escape,))
    assert reader.cancel_requested.is_set() is False


@pytest.mark.asyncio
async def test_r8_escape_reaches_the_binding_inside_a_running_prompt():
    """The spike showed the key can be delivered without a real terminal."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.key_binding.key_processor import KeyPress
    from prompt_toolkit.keys import Keys

    reader = InteractiveReader()
    session = PromptSession(key_bindings=reader.key_bindings())
    task = asyncio.create_task(session.prompt_async("you> "))
    await asyncio.sleep(0.2)
    session.app.key_processor.feed(KeyPress(Keys.Escape))
    session.app.key_processor.process_keys()
    assert reader.cancel_requested.is_set() is True
    task.cancel()
    with pytest.raises((asyncio.CancelledError, EOFError)):
        await task
