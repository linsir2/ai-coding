"""Approval and ask_user E2E in a real pty (replaces the deleted run_repl tests).

The child is a real Textual app with a real terminal attached; the test plays a
human: submit a line (which starts the scripted turn), press "y" on the approval
modal, then type the ask answer.  ("y", not Enter: the modal focuses Deny so a
reflexive Enter fails closed — see ``ApprovalScreen``.)
A pty has no synchronisation point other than the clock, so the schedule below
is wall-clock; if the first run is flaky, print ``result["output"]`` to see the
screen and adjust the delays — never skip or delete the test.
"""

from __future__ import annotations

import os
import pty
import select
import subprocess
import sys
import time

CHILD = os.path.join(os.path.dirname(__file__), "_pty_textual_child.py")


def _drive(script: list[tuple[float, str]], timeout: float = 25.0) -> dict:
    """Run the child in a pty; send each ``(at_seconds, keys)`` when its time comes."""
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        [sys.executable, CHILD],
        stdin=slave, stdout=slave, stderr=slave, close_fds=True,
        env={**os.environ, "TERM": "xterm-256color"},
    )
    os.close(slave)
    output = b""
    started = time.time()
    step = 0
    try:
        while time.time() - started < timeout:
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    output += os.read(master, 65536)
                except OSError:
                    break
            if step < len(script) and time.time() - started >= script[step][0]:
                os.write(master, script[step][1].encode())
                step += 1
            if proc.poll() is not None:
                break
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        try:
            os.close(master)
        except OSError:
            pass
    return {"output": output.decode(errors="replace"), "code": proc.wait()}


def test_approval_and_ask_user_reach_the_tools():
    result = _drive([
        (1.5, "go\r"),    # submit a line → the scripted turn starts
        (3.5, "y"),      # approval modal: y allows (Enter alone would now deny)
        (6.0, "a.py\r"),  # ask modal: type an answer and submit (extra slack:
        (8.0, "\r"),      # the approval box grew a hint row, so leave room)
    ])
    assert result["code"] == 0, result["output"]
    assert '"approved": true' in result["output"], result["output"]
    assert '"answer": "a.py"' in result["output"], result["output"]
