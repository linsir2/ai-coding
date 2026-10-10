"""Launch the TUI: build the world, serve IPC, spawn the bun frontend.

Process model (plan M0):

    ai-coding (this Python process)
      |-- Unix-socket JSON-RPC service          (in-process, python-lsp-jsonrpc)
      |-- bun frontend/src/index.tsx            (child; inherits the tty)
      '-- wait for the child to exit, then close the world

The frontend receives ``AI_CODING_SOCKET`` in its environment and connects
back to the service with ``vscode-jsonrpc``.  Python stays the entry point,
so the existing CLI and install flow are unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from pathlib import Path

from ai_coding.server.handlers import make_handlers
from ai_coding.server.service import IpcService
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
    # The handler table needs the service (event push/approval calls), and the
    # service needs the table — the dict is shared and filled in one step.
    service = IpcService(socket_path, asyncio.get_running_loop())
    service.start()
    service.dispatcher.update(make_handlers(world, service, shutdown=stop_event.set))

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
        service.close()
        await world.aclose()
        if proc.returncode not in (0, None):
            print(f"frontend exited with code {proc.returncode}", file=sys.stderr)
