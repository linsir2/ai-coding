"""Launch the TUI: build the world, serve IPC, spawn the bun frontend.

Process model (plan M0):

    ai-coding (this Python process)
      |-- asyncio Unix-socket JSON-RPC server  (in-process)
      |-- bun frontend/src/index.tsx           (child; inherits the tty)
      '-- wait for the child to exit, then close the world

The frontend receives ``AI_CODING_SOCKET`` in its environment and connects
back to the server.  Python stays the entry point, so the existing CLI and
install flow are unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from ai_coding.server.handlers import make_handlers
from ai_coding.server.server import IpcServer
from ai_coding.server.world import build_world

_log = logging.getLogger(__name__)

__all__ = ["run_tui"]

#: Where the frontend lives relative to the repo root (ai_coding/server/..).
_DEFAULT_FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


def _frontend_dir() -> Path:
    override = os.environ.get("AI_CODING_FRONTEND", "")
    return Path(override).expanduser() if override else _DEFAULT_FRONTEND


async def run_tui(path: str | None, workspace: str, skills: str | None) -> None:
    """Serve the TUI until the frontend exits.  Raises on startup failure."""
    frontend = _frontend_dir()
    entry = frontend / "src" / "index.tsx"
    if not entry.is_file():
        raise FileNotFoundError(
            f"frontend not found at {entry} (set AI_CODING_FRONTEND to override)"
        )
    bun = shutil.which("bun")
    if bun is None:
        raise RuntimeError(
            "bun is required for the TUI frontend but was not found on PATH "
            "(install: https://bun.sh)"
        )

    world = await build_world(path, workspace, skills)
    socket_path = Path(f"/tmp/ai-coding-{os.getpid()}.sock")
    stop_event = asyncio.Event()
    # The handler table needs the server (event push/approval calls), and the
    # server needs the table — the dict is shared and filled in one step.
    handlers: dict[str, Any] = {}
    server = IpcServer(socket_path, handlers)
    handlers.update(make_handlers(world, server, shutdown=stop_event.set))

    await server.start()
    env = {**os.environ, "AI_CODING_SOCKET": str(socket_path)}
    proc = await asyncio.create_subprocess_exec(
        bun, str(entry),
        cwd=str(frontend),
        env=env,
        # The tty stays with the child; IPC rides the socket instead.
        stdin=None,
        stdout=None,
        stderr=None,
    )
    try:
        await proc.wait()
    finally:
        await server.stop()
        await world.aclose()
        if proc.returncode not in (0, None):
            print(f"frontend exited with code {proc.returncode}", file=sys.stderr)
