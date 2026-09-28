"""The child half of the R8c pty case: a real reader, a real stuck command.

The parent sends a real Escape key into the pty; the assertion is on the
*transcript* and on the process group, not on the echo (docs/r8-analysis.md 3.3-S2).

Usage: python tests/_pty_cancel_child.py <workdir>
Prints ``READY`` before the turn starts, then ``RESULT {json}`` at the end.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai_coding.ai.engine import ChatEngine  # noqa: E402
from ai_coding.config.models import AIConfig  # noqa: E402
from ai_coding.core.agent_loop import AgentLoop  # noqa: E402
from ai_coding.domain.loop import StepResult  # noqa: E402
from ai_coding.domain.message import ToolCallRef  # noqa: E402
from ai_coding.domain.session import SessionData  # noqa: E402
from ai_coding.security.approval import AutoApproveCallback  # noqa: E402
from ai_coding.tools.bash_tool import BashTool  # noqa: E402
from ai_coding.tools.registry import ToolRegistry  # noqa: E402
from ai_coding.ui.reader import InteractiveReader  # noqa: E402

WORKDIR = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
PID_FILE = WORKDIR / "cancel-pid"
RESULT_FILE = WORKDIR / "result.json"
COMMAND = f"echo $$ > {PID_FILE}; sleep 30"


class _Engine(ChatEngine):
    """One stuck command, then a normal answer (the turn must continue)."""

    def __init__(self) -> None:
        self.steps = 0

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.steps += 1
        if self.steps == 1:
            return StepResult(
                text=None,
                tool_calls=[
                    ToolCallRef(
                        id="c1", name="bash", arguments=json.dumps({"command": COMMAND})
                    )
                ],
                finish_reason="tool_calls",
            )
        return StepResult(text="收尾了", finish_reason="stop")


class _Sessions:
    def __init__(self) -> None:
        self.session = SessionData(
            session_id="r8c", title="", created_time="t", last_access_time="t"
        )

    def create(self) -> SessionData:
        return self.session

    def save(self, session: SessionData) -> None:
        return None


async def main() -> None:
    reader = InteractiveReader.for_terminal(enabled=False)
    reader.start()
    registry = ToolRegistry()
    registry.register(BashTool(timeout_seconds=30))
    sessions = _Sessions()
    loop = AgentLoop(
        _Engine(),
        sessions,
        registry,
        approval=AutoApproveCallback(),
        settings=AIConfig(
            max_tool_iterations=4, stop_policy="natural", turn_timeout_seconds=0
        ),
        cancel=reader.cancel_requested,
        workspace=str(WORKDIR),
    )
    print("READY", flush=True)
    payload: dict[str, Any]
    try:
        answer = await loop.process_input(sessions.session, "跑一条长命令")
        payload = {
            "answer": answer,
            "messages": [
                f"{message.role}:{message.content or ''}"
                for message in sessions.session.messages
            ],
        }
    finally:
        await reader.aclose()
        await loop.aclose()
    # The assertion reads this file: a pty is a terminal, and what a *terminal*
    # does with what we print is not what this test is about (measured: after the
    # last slave fd closes, the parent's read fails with EIO and the queued bytes
    # are gone).  The Escape key still travels through the real pty.
    RESULT_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print("RESULT " + json.dumps(payload, ensure_ascii=False), flush=True)
    time.sleep(0.5)


if __name__ == "__main__":
    asyncio.run(main())
