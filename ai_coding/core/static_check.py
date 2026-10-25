"""Run the project's own static checks when a turn stalls in look-only mode (B1).

``core/interventions.py`` decides *when* to ask; this module answers with actual
command output, so the model gets the finding instead of another keyword to guess.

Two properties shape everything here:

**Nothing raises.** A turn must not fail because a linter is missing, so every
command is independent and a broken one is reported, not propagated.

**Nothing the repository can say becomes a shell.** ``commands`` may come from a
project's tracked ``config.example.yaml`` -- ``ConfigLoader.DEFAULT_NAMES`` walks
up from the workspace and a repo ships that file -- so a string from there must
never reach a shell.  The command is ``shlex``-split and run with
``shell=False``, and its first word must be one of the known analysis tools:
``;``, ``|`` and ``&&`` are then literal arguments, not operators, and an unknown
program is refused instead of executed.  This is a smaller, structural version of
what ``core/acceptance.py`` does with its proof commands, and it exists because
this is the one place the harness runs something by itself.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Sequence
from pathlib import Path

from ai_coding.infra.subprocess_runner import run_process_async

__all__ = ["ALLOWED_PROGRAMS", "run_static_checks"]

#: The analysis tools this feature exists for.  Matched on the *basename* of the
#: first word, so an absolute ``.venv/bin/ruff`` is allowed and ``curl`` is not.
ALLOWED_PROGRAMS = frozenset(
    {
        "ruff",
        "mypy",
        "pyright",
        "pylint",
        "flake8",
        "black",
        "isort",
        "eslint",
        "tsc",
        "gofmt",
        "go",
        "staticcheck",
        "cargo",
        "rubocop",
        "phpstan",
        "shellcheck",
    }
)


def _search_path(workspace: str) -> dict[str, str]:
    """The environment, with the workspace's own venv first on ``PATH``.

    The checks are usually installed in ``.venv/bin`` and are not on the shell's
    ``PATH``; asking the operator to hard-code ``.venv/bin/ruff`` in their config
    would make that config machine-specific.
    """
    env = dict(os.environ)
    venv_bin = Path(workspace) / ".venv" / "bin"
    if venv_bin.is_dir():
        env["PATH"] = f"{venv_bin}{os.pathsep}{env.get('PATH', '')}"
    return env


def _argv(command: str) -> list[str] | None:
    """``command`` as argv, or ``None`` when it is not an allowed analysis tool."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if not argv:
        return None
    if Path(argv[0]).name not in ALLOWED_PROGRAMS:
        return None
    return argv


async def run_static_checks(
    workspace: str,
    commands: Sequence[str],
    *,
    timeout: float = 20.0,
    max_chars: int = 2000,
) -> str:
    """Run ``commands`` in ``workspace``; return a compact block, or ``""``.

    ``""`` means "nothing to say" -- no workspace, no commands, or nothing could be
    run at all.  The caller then keeps the nudge's own text, which still tells the
    model what to run by hand.
    """
    if not workspace or not commands or max_chars <= 0:
        return ""
    env = _search_path(workspace)
    lines = [f"[static-check] harness 自动执行了 {len(commands)} 条检查（工作区: {workspace}）"]
    spent = len(lines[0])
    spoke = 0
    for command in commands:
        argv = _argv(command)
        if argv is None:
            # A refusal is worth telling: it is the operator's own configured
            # command, and silently dropping it would look like "no findings".
            header = f"$ {command}  → skipped: 不在允许的静态检查工具里"
            lines.append(header[: max(0, max_chars - spent)])
            spent += len(header)
            spoke += 1
            continue
        body = ""
        status = "no exit code"
        try:
            outcome = await run_process_async(
                argv,
                shell=False,
                cwd=workspace,
                env=env,
                timeout=timeout,
                max_output_chars=max(0, max_chars - spent),
            )
        except Exception as exc:  # noqa: BLE001 -- a check must never fail the turn
            status = f"could not run: {type(exc).__name__}: {exc}"
        else:
            if outcome.returncode is not None:
                status = f"exit {outcome.returncode}"
            if outcome.timed_out:
                status = f"timeout after {timeout:g}s"
            elif outcome.error:
                status = f"could not run: {outcome.error}"
            body = "\n".join(
                part
                for part in (
                    (outcome.stdout or "").strip(),
                    (outcome.stderr or "").strip(),
                )
                if part
            )
        header = f"$ {command}  → {status}"
        lines.append(header[: max(0, max_chars - spent)])
        spent += len(header)
        spoke += 1
        if body and spent < max_chars:
            clipped = body[: max(0, max_chars - spent)]
            lines.append(clipped)
            spent += len(clipped)
        if spent >= max_chars:
            break
    if not spoke:
        return ""
    return "\n".join(lines)[:max_chars]
