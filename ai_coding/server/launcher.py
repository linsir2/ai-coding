"""Launch the TUI: build the world, serve IPC, spawn the bun frontend.

Process model (plan M0):

    ai-coding (this Python process)
      |-- Unix-socket JSON-RPC service          (in-process, python-lsp-jsonrpc)
      |-- bun src/index.tsx                      (child; inherits the tty)
      '-- wait for the child to exit, then close the world

The frontend receives ``AI_CODING_SOCKET`` in its environment and connects
back to the service with ``vscode-jsonrpc``.  Python stays the entry point,
so the existing CLI and install flow are unchanged.

The frontend ships *inside* the package (``ai_coding/frontend``), so the source
tree and an installed wheel resolve the same way (H3).  A wheel has no
``node_modules`` and ``site-packages`` is read-only, so the first launch copies
the sources to ``data_home()/frontend/<version>`` and runs ``bun install``
there; an existing checkout is used in place.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ai_coding import __version__
from ai_coding.paths import data_home
from ai_coding.server.handlers import make_handlers
from ai_coding.server.service import IpcService
from ai_coding.server.world import build_world

_log = logging.getLogger(__name__)

__all__ = ["run_tui"]

#: The frontend as packaged (``ai_coding/frontend``), which is the same path in
#: a source checkout and in an installed wheel.
_PACKAGED_FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


def _frontend_dir() -> Path | None:
    """The ``AI_CODING_FRONTEND`` override, or ``None`` when unset.

    An override is a development checkout: it is used in place (no copy) so
    edits are picked up on the next launch.
    """
    override = os.environ.get("AI_CODING_FRONTEND", "")
    return Path(override).expanduser() if override else None


def _require_entry(directory: Path) -> Path:
    """The frontend entry point, or ``FileNotFoundError`` naming the override."""
    entry = directory / "src" / "index.tsx"
    if not entry.is_file():
        raise FileNotFoundError(
            f"frontend entry not found at {entry}; set AI_CODING_FRONTEND to a "
            "frontend checkout to override the packaged copy, or reinstall ai-coding"
        )
    return entry


def _runtime_dir(frontend: Path, *, in_place: bool = False) -> Path:
    """Where bun runs: ``frontend`` itself, or a per-version copy under data_home.

    Copying is skipped when the directory already has ``node_modules`` (an
    installed checkout) and when the versioned copy is already complete, so
    the 100MB+ tree moves at most once per version.
    """
    if in_place:
        return frontend
    if (frontend / "node_modules").is_dir():
        return frontend
    target = data_home() / "frontend" / __version__
    if not (target / "src" / "index.tsx").is_file():
        target.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            frontend,
            target,
            ignore=shutil.ignore_patterns("node_modules", ".bun"),
            dirs_exist_ok=True,
        )
    return target


def _ensure_deps(
    directory: Path, bun: str, *, run: Callable[..., Any] = subprocess.run
) -> None:
    """Install the frontend's dependencies once, with a bounded wait."""
    if (directory / "node_modules").is_dir():
        return
    result = run(
        [bun, "install", "--frozen-lockfile"],
        cwd=str(directory),
        timeout=600,
    )
    if result.returncode != 0 or not (directory / "node_modules").is_dir():
        shutil.rmtree(directory / "node_modules", ignore_errors=True)
        raise ValueError(
            f"bun install failed in {directory} (exit {result.returncode}); "
            "check that bun is installed and the network is reachable"
        )


async def run_tui(path: str | None, workspace: str, skills: str | None) -> None:
    """Serve the TUI until the frontend exits.  Raises on startup failure."""
    override = _frontend_dir()
    frontend = override or _PACKAGED_FRONTEND
    runtime = _runtime_dir(frontend, in_place=override is not None)
    entry = _require_entry(runtime)
    bun = shutil.which("bun")
    if bun is None:
        raise ValueError(
            "bun is required for the TUI frontend but was not found on PATH "
            "(install: https://bun.sh)"
        )
    await asyncio.to_thread(_ensure_deps, runtime, bun)

    world = await build_world(path, workspace, skills)
    socket_path = Path(f"/tmp/ai-coding-{os.getpid()}.sock")
    stop_event = asyncio.Event()
    # The handler table needs the service (event push/approval calls), and the
    # service needs the table — the dict is shared and filled in one step.
    service = IpcService(socket_path, asyncio.get_running_loop())
    service.start()
    service.dispatcher.update(make_handlers(world, service, shutdown=stop_event.set))

    env = {**os.environ, "AI_CODING_SOCKET": str(socket_path)}
    # The argv is relative to ``cwd``: bun resolves ``node_modules`` by walking
    # up from the *entry file's* directory, so an absolute path into the package
    # would look for the dependencies in the wrong tree.
    proc = await asyncio.create_subprocess_exec(
        bun, str(entry.relative_to(runtime)),
        cwd=str(runtime),
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
