"""The child half of the R7e pty cases — a real REPL driven by a real reader.

Two modes, because they are the two things the reader has to arbitrate:

  steering : the model is slow (one tool call that takes a while), so a line typed
             during the turn must land in the transcript as [steering]
  question : the model asks, and the answer must come back through the same prompt
             line the reader already owns

It prints one line at the end: ``RESULT ["..."]`` — every message of the session,
JSON-encoded, so the assertion is on the *transcript*, not on the echo.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai_coding.ai.engine import ChatEngine  # noqa: E402
from ai_coding.config.models import AIConfig  # noqa: E402
from ai_coding.core.agent_loop import AgentLoop  # noqa: E402
from ai_coding.core.steering import SteeringQueue  # noqa: E402
from ai_coding.domain.loop import StepResult  # noqa: E402
from ai_coding.domain.message import ToolCallRef, ToolResult  # noqa: E402
from ai_coding.domain.session import SessionData  # noqa: E402
from ai_coding.security.approval import AutoApproveCallback  # noqa: E402
from ai_coding.security.user_prompt import CLIUserPromptCallback  # noqa: E402
from ai_coding.tools.ask_user_tool import AskUserTool  # noqa: E402
from ai_coding.tools.base import BaseTool, ToolContext  # noqa: E402
from ai_coding.tools.registry import ToolRegistry  # noqa: E402
from ai_coding.ui.console import DialogueConsole  # noqa: E402
from ai_coding.ui.reader import InteractiveReader  # noqa: E402
from ai_coding.ui.repl import run_repl  # noqa: E402

MODE = sys.argv[1] if len(sys.argv) > 1 else "steering"


class _SlowTool(BaseTool):
    """A tool that takes long enough for a human to type while it runs."""

    parallel_safe = True

    @property
    def name(self) -> str:
        return "slow"

    @property
    def description(self) -> str:
        return "slow"

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, params: dict[str, Any], ctx: ToolContext | None = None) -> ToolResult:
        await asyncio.sleep(1.5)
        return ToolResult(success=True, output="slow done")


class _Engine(ChatEngine):
    """One tool call, then a question, then the answer."""

    def __init__(self) -> None:
        self.steps = 0

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.steps += 1
        if MODE == "steering":
            if self.steps == 1:
                return StepResult(
                    text=None,
                    tool_calls=[ToolCallRef(id="c1", name="slow", arguments="{}")],
                    finish_reason="tool_calls",
                )
            return StepResult(text="照你说的改了。", tool_calls=[], finish_reason="stop")
        if self.steps == 1:
            return StepResult(
                text=None,
                tool_calls=[
                    ToolCallRef(
                        id="c2",
                        name="ask_user",
                        arguments=json.dumps({"question": "改 A 还是 B？"}),
                    )
                ],
                finish_reason="tool_calls",
            )
        return StepResult(text="好的。", tool_calls=[], finish_reason="stop")


class _Sessions:
    def __init__(self) -> None:
        self.session = SessionData(
            session_id="r7e", title="", created_time="t", last_access_time="t"
        )

    def create(self) -> SessionData:
        return self.session

    def save(self, session: SessionData) -> None:
        return None


async def main() -> None:
    sessions = _Sessions()
    queue = SteeringQueue()
    reader = InteractiveReader.for_terminal(steering=queue, enabled=True)
    channel = CLIUserPromptCallback()
    registry = ToolRegistry()
    registry.register(_SlowTool())
    if MODE == "question":
        registry.register(AskUserTool(channel, timeout_seconds=30))
    loop = AgentLoop(
        _Engine(),
        sessions,
        registry,
        approval=AutoApproveCallback(),
        settings=AIConfig(max_tool_iterations=4, stop_policy="natural"),
        steering=queue,
    )
    try:
        await run_repl(loop, sessions, DialogueConsole(), user_prompt=channel, reader=reader)
    finally:
        await loop.aclose()
    messages = [f"{m.role}:{m.content or ''}" for m in sessions.session.messages]
    print("RESULT " + json.dumps(messages, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
