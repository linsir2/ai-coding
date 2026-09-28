"""Gated integration test (R8c): a real Escape key at a real terminal.

``tests/test_tool_bash.py`` proves the chain (dispatcher → kill_event → killpg)
without a terminal, and ``tests/test_ui_reader.py`` proves the binding fires.  This
one proves the two are the same path: the key is typed into a pty, the running tool
is dropped, the process group dies, and the turn still finishes.

Run it yourself:

    AI_CODING_PTY=1 .venv/bin/python -m pytest tests/test_repl_cancel_pty.py -q
"""

from __future__ import annotations

import json
import os
import pty
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

CHILD = Path(__file__).resolve().parent / "_pty_cancel_child.py"

pytestmark = pytest.mark.skipif(
    os.environ.get("AI_CODING_PTY") != "1",
    reason="a real pty round trip needs a terminal; set AI_CODING_PTY=1",
)


def _drain(master: int, seconds: float) -> bytes:
    """Read whatever the pty has, for a bounded window (never blocks the child)."""
    out = b""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        ready, _, _ = select.select([master], [], [], 0.2)
        if not ready:
            continue
        try:
            chunk = os.read(master, 8192)
        except OSError:  # the child closed its side of the pty
            break
        if not chunk:
            break
        out += chunk
    return out


def _wait_for_file(path: Path, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            text = path.read_text(encoding="utf-8")
            if text:
                return text
        time.sleep(0.1)
    return ""


def test_escape_in_a_real_pty_cancels_the_tool_and_the_turn_continues(tmp_path: Path):
    master, slave = pty.openpty()
    env = dict(os.environ)
    env["TERM"] = env.get("TERM", "xterm-256color")
    proc = subprocess.Popen(
        [sys.executable, str(CHILD), str(tmp_path)],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
        close_fds=True,
    )
    os.close(slave)
    try:
        started = _drain(master, 2.0)
        assert b"READY" in started, f"the child never started a turn: {started!r}"
        # give the bash call a moment to start, then press Escape for real
        time.sleep(1.2)
        os.write(master, b"\x1b")
        payload_text = _wait_for_file(tmp_path / "result.json", timeout=30)
        assert payload_text, f"the turn never finished: {started!r}"
    finally:
        _drain(master, 0.2)
        proc.wait(timeout=30)
        os.close(master)

    payload = json.loads(payload_text)
    transcript = "\n".join(payload["messages"])

    assert "cancelled_by_user" in transcript, transcript
    assert payload["answer"] == "收尾了", payload  # the turn carried on

    pid_file = tmp_path / "cancel-pid"
    assert pid_file.exists(), "the command never started"
    pgid = int(pid_file.read_text(encoding="utf-8").strip())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("the cancelled command is still running")
