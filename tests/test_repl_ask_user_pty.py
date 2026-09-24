"""R7c: a line typed at a real terminal reaches the ``ask_user`` tool.

This is the one path R6c never verified (docs/r6-analysis.md 10.8): under a pipe,
prompt_toolkit grabs stdin first and the tool degrades to ``no_answer``; whether a
real keystroke could get through was unknown.

The mechanism was proved feasible before any code changed (docs/r7-analysis.md
1.4: a ``PromptSession`` prompted from a worker thread, while the main thread runs
the event loop, reads the line).  This test proves the wiring: a real REPL in a
real pty, and the assertion is on the *tool result* — not on the echo, which would
pass even if nothing reached the model.

Gated behind ``AI_CODING_PTY=1`` because it spawns a child process and depends on
terminal behaviour, which is exactly the sort of thing that should not make CI
flaky by default:

    AI_CODING_PTY=1 .venv/bin/python -m pytest tests/test_repl_ask_user_pty.py -q
"""

from __future__ import annotations

import json
import os
import pty
import re
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

CHILD = Path(__file__).resolve().parent / "_pty_ask_user_child.py"
QUESTION = "改 A 还是 B？"

pytestmark = pytest.mark.skipif(
    os.environ.get("AI_CODING_PTY") != "1",
    reason="a real pty round trip needs a terminal; set AI_CODING_PTY=1 (docs/r7-analysis.md 3.3)",
)


def _read_until(
    master: int, needle: str, deadline: float, buffer: bytes = b"", occurrences: int = 1
) -> bytes:
    """Read the pty until ``needle`` has shown up ``occurrences`` times."""
    while time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], 0.5)
        if not ready:
            if buffer.count(needle.encode()) >= occurrences:
                break
            continue
        try:
            chunk = os.read(master, 4096)
        except OSError:
            break
        if not chunk:
            break
        buffer += chunk
        if buffer.count(needle.encode()) >= occurrences:
            break
    return buffer


def test_a_typed_line_reaches_the_tool_result():
    master, slave = pty.openpty()
    child = subprocess.Popen(
        [sys.executable, str(CHILD)],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
    )
    os.close(slave)
    buffer = b""
    try:
        # 1. the REPL is up (its own prompt is on screen) — ask it for something
        buffer = _read_until(master, "you>", time.monotonic() + 30, buffer)
        assert b"you>" in buffer, buffer.decode(errors="replace")[-800:]
        os.write(master, "改一下这个仓库\n".encode())

        # 2. the model asks its question, through the tool's own prompt
        buffer = _read_until(master, QUESTION, time.monotonic() + 30, buffer)
        assert QUESTION.encode() in buffer, buffer.decode(errors="replace")[-800:]

        # 3. a human answers
        os.write(master, "改 A\n".encode())

        # 4. the turn finishes and the REPL prompts again (the second "you>")
        buffer = _read_until(
            master, "you>", time.monotonic() + 30, buffer, occurrences=2
        )

        # 5. leave, and read the driver's report of the session
        os.write(master, b"/quit\n")
        buffer = _read_until(master, "RESULT ", time.monotonic() + 30, buffer)
        child.wait(timeout=30)
    finally:
        if child.poll() is None:
            child.kill()
        os.close(master)

    text = buffer.decode(errors="replace").replace("\r\n", "\n")
    results = re.findall(r"RESULT (\[.*?\])", text)
    assert results, text[-1500:]
    messages = json.loads(results[-1])
    assert messages, "the turn produced no tool message"
    assert any("改 A" in message for message in messages), messages
