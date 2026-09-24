"""R7c: a scripted session's answer reaches the tool instead of vanishing.

This is the case that actually reproduced the R6c gap, and finding it corrected
this round's own reasoning (docs/r7-analysis.md 1.4): a *human at a real
terminal* always worked — ``input()`` reads the tty fine.  What broke was a
script feeding the REPL several lines at once, because prompt_toolkit's reader
grabs stdin first and the tool's own reader then sees EOF:

    without the shared reader (the R6c code path)
        {"answered": false, "reason": "eof"}   → "没有人给出回答…"
    with it (R7c)
        {"answered": true, "answer": "改 A"}

Both halves are measured with the same payload, in
``docs/r7-analysis.md`` §1.4.  The assertion is on the **tool result**, so a
passing echo cannot fake it.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

CHILD = Path(__file__).resolve().parent / "_pty_ask_user_child.py"

# Everything a scripted caller would send, in one write: the request, the answer to
# the question the model has not asked yet, and the quit.
SCRIPT = "改一下这个仓库\n改 A\n/quit\n"


def test_a_scripted_answer_reaches_the_tool_result():
    completed = subprocess.run(
        [sys.executable, str(CHILD)],
        input=SCRIPT.encode(),
        capture_output=True,
        timeout=60,
    )
    text = completed.stdout.decode(errors="replace")
    results = re.findall(r"RESULT (\[.*?\])", text)
    assert results, text[-1500:]
    messages = json.loads(results[-1])
    assert messages, "the turn produced no tool message"
    assert any('"answered": true' in message for message in messages), messages
    assert any("改 A" in message for message in messages), messages
