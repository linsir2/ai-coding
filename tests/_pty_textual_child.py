"""Drive the Textual app in a real pty: approve once, answer one ask_user.

Prints one JSON line at the end: {"approved": bool, "answer": str}.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai_coding.ui.textual_app import AICodingApp  # noqa: E402


class _ScriptedLoop:
    """One turn, no model: an approval, then an ask_user, then exit."""

    def __init__(self) -> None:
        self.workspace = "/tmp/ws"
        self.results: dict[str, object] = {"approved": False, "answer": ""}
        self._app: AICodingApp | None = None

    async def process_input(self, session, text, *, on_delta=None, observer=None):
        from ai_coding.security.approval import CLIApprovalCallback

        callback = CLIApprovalCallback()
        app = self._app
        callback.set_provider(app.approval_provider)
        result = await callback.approve("bash", {"command": "ls"}, "read-only")
        self.results["approved"] = result.value == "yes"

        from ai_coding.security.user_prompt import CLIUserPromptCallback

        ask = CLIUserPromptCallback()
        ask.set_provider(app.ask_provider)
        answer = await ask.ask("Which file?")
        self.results["answer"] = answer.text
        app.exit()
        return "done"

    async def aclose(self):
        return None


async def main() -> None:
    loop = _ScriptedLoop()
    app = AICodingApp(loop=loop, session=object(), sessions=object(), workspace="/tmp/ws")
    loop._app = app
    await app.run_async()
    print(json.dumps(loop.results, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
