"""The child half of ``tests/test_repl_ask_user_pty.py`` — a real REPL in a pty.

It is not a test module (no ``test_`` prefix): it is a *driver* the test spawns, so
the REPL runs in its own process with a terminal attached, which is the only way to
see whether a line typed by a human reaches the ``ask_user`` tool.

It prints one line at the end: ``RESULT ["..."]`` — the contents of every tool
message in the session, JSON-encoded.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai_coding.ai.engine import ChatEngine  # noqa: E402
from ai_coding.config.models import AIConfig  # noqa: E402
from ai_coding.core.agent_loop import AgentLoop  # noqa: E402
from ai_coding.domain.loop import StepResult  # noqa: E402
from ai_coding.domain.message import ToolCallRef  # noqa: E402
from ai_coding.domain.session import SessionData  # noqa: E402
from ai_coding.security.approval import AutoApproveCallback  # noqa: E402
from ai_coding.security.user_prompt import CLIUserPromptCallback  # noqa: E402
from ai_coding.tools.ask_user_tool import AskUserTool  # noqa: E402
from ai_coding.tools.registry import ToolRegistry  # noqa: E402
from ai_coding.ui.console import DialogueConsole  # noqa: E402
from ai_coding.ui.repl import run_repl  # noqa: E402


class _Engine(ChatEngine):
    """Ask the human once, then answer."""

    def __init__(self) -> None:
        self.steps = 0

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.steps += 1
        if self.steps == 1:
            return StepResult(
                text=None,
                tool_calls=[
                    ToolCallRef(
                        id="c1",
                        name="ask_user",
                        arguments=json.dumps({"question": "改 A 还是 B？"}),
                    )
                ],
                finish_reason="tool_calls",
            )
        return StepResult(text="好的，按你说的做。", tool_calls=[], finish_reason="stop")


class _Sessions:
    """One session, kept so the driver can print it afterwards."""

    def __init__(self) -> None:
        self.session = SessionData(
            session_id="pty", title="", created_time="t", last_access_time="t"
        )

    def create(self) -> SessionData:
        return self.session

    def save(self, session: SessionData) -> None:
        return None


async def main() -> None:
    sessions = _Sessions()
    # The real channel, exactly as `cli.repl` builds it: no input_fn, so until the
    # REPL swaps the reader in, this is the builtin input().
    user_prompt = CLIUserPromptCallback()
    registry = ToolRegistry()
    registry.register(AskUserTool(user_prompt, timeout_seconds=30))
    loop = AgentLoop(
        _Engine(),
        sessions,
        registry,
        approval=AutoApproveCallback(),
        settings=AIConfig(max_tool_iterations=3, stop_policy="natural"),
    )
    try:
        await run_repl(loop, sessions, DialogueConsole(), user_prompt=user_prompt)
    finally:
        await loop.aclose()
    tools = [
        message.content
        for message in sessions.session.messages
        if message.role == "tool"
    ]
    print("RESULT " + json.dumps(tools, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
