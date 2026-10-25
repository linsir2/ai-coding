"""Run the project's own static checks when a turn stalls in look-only mode (B1).

``core/interventions.py`` decides *when* to ask; this module answers with actual
command output, so the model gets the finding instead of another keyword to guess.

Two properties shape everything here:

**Nothing raises.** A turn must not fail because a linter is missing, so every
command is independent and a broken one is reported, not propagated.

**Nothing runs unless the operator opted in.** ``commands`` can be set by a
project's *tracked* ``config.example.yaml`` (``ConfigLoader.DEFAULT_NAMES`` walks
up from the workspace and a repo ships that file), so config alone cannot be the
permission -- a repository would be granting itself the right to run code.  The
gate is ``AI_CODING_STATIC_CHECK=1`` in the environment: a repository cannot set
that for the agent process, and an operator who sets it is making the same trust
decision as running ``make`` in that repo.  Off by default; the ``[search-gate]``
nudge still names the commands, so the model can run them through ``bash`` and its
permission gate instead.

**And what runs is an argv, not a shell.** Even when enabled, the command is
``shlex``-split and executed with ``shell=False`` and a first word from
``ALLOWED_PROGRAMS``; ``;``, ``|`` and ``&&`` are then arguments.  The programs in
the list are the ones that analyse code without running the project's own build
(for example ``cargo``/``go``/``eslint`` are deliberately absent: they execute
``build.rs``, ``go run`` and config modules by design).

Residual risk, stated because it is real: with the opt-in given, a repository's
own ``.venv/bin/ruff`` is what runs -- that is the whole point of using the
project's installed linter, and it is why the opt-in is a trust decision rather
than a toggle.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Sequence
from pathlib import Path

from ai_coding.infra.subprocess_runner import run_process_async

__all__ = ["ALLOWED_PROGRAMS", "ENABLE_ENV", "enabled", "run_static_checks"]

#: The environment variable that turns the feature on.  A repository cannot set it
#: for the agent process, which is what makes it a permission rather than a config.
ENABLE_ENV = "AI_CODING_STATIC_CHECK"

#: Programs that analyse code *without* running the project's own build.  Matched
#: on the basename of the first word, so an absolute ``.venv/bin/ruff`` is allowed
#: and ``curl`` is not.  ``cargo``/``go``/``eslint``/``pylint``/``rubocop``/
#: ``phpstan`` are deliberately absent: each executes repository code by design
#: (``build.rs``, ``go run``, config modules, ``--init-hook``, ``require:``,
#: ``bootstrapFiles``), which is not what this feature is for.
ALLOWED_PROGRAMS = frozenset(
    {
        "ruff",
        "mypy",
        "pyright",
        "flake8",
        "black",
        "isort",
        "tsc",
        "gofmt",
        "staticcheck",
        "shellcheck",
    }
)


def enabled() -> bool:
    """Is the harness allowed to run the checks itself?"""
    return os.environ.get(ENABLE_ENV) == "1"


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

    ``""`` means "nothing to say" -- not opted in, no workspace, no commands, or
    nothing could be run at all.  The caller then keeps the nudge's own text, which
    still tells the model what to run by hand.
    """
    if not enabled():
        return ""
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
