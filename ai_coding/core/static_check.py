"""Run the project's own static checks when a turn stalls in look-only mode (B1).

``core/interventions.py`` decides *when* to ask; this module answers with actual
command output, so the model gets the finding instead of another keyword to guess.

Small on purpose: one bounded command per configured check, a size cap, and no
new failure modes -- a check that cannot run is reported as such, never raised.

The commands are **declared, never inferred**: measured on this repository,
``mypy .`` reports 271 errors (it does not exclude ``scripts/``) while
``mypy ai_coding`` -- what CI runs -- reports none.  No heuristic tells those
apart, so ``AIConfig.static_check`` carries the list.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from ai_coding.infra.subprocess_runner import run_process_async

__all__ = ["run_static_checks"]


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


async def run_static_checks(
    workspace: str,
    commands: Sequence[str],
    *,
    timeout: float = 20.0,
    max_chars: int = 2000,
) -> str:
    """Run ``commands`` in ``workspace``; return a compact block, or ``""``.

    ``""`` means "nothing to say" -- no workspace, no commands configured, or the
    block is empty.  The caller then keeps the nudge's own text, which still tells
    the model what to run by hand.

    Each command is independent: one that cannot start or overruns is reported in
    the block and the next one still runs.  Nothing here raises -- a turn must not
    fail because a linter is missing.
    """
    if not workspace or not commands or max_chars <= 0:
        return ""
    env = _search_path(workspace)
    lines = [f"[static-check] harness 自动执行了 {len(commands)} 条检查（工作区: {workspace}）"]
    spent = len(lines[0])
    ran = 0
    for command in commands:
        if spent >= max_chars:
            lines.append(f"… 已达到 {max_chars} 字符上限，剩余检查未执行")
            break
        remaining = max_chars - spent
        try:
            outcome = await run_process_async(
                command,
                shell=True,
                cwd=workspace,
                env=env,
                timeout=timeout,
                max_output_chars=remaining,
            )
        except Exception as exc:  # noqa: BLE001 -- a check must never fail the turn
            body = f"could not run: {type(exc).__name__}: {exc}"
        else:
            status = (
                f"exit {outcome.returncode}"
                if outcome.returncode is not None
                else "no exit code"
            )
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
            lines.append(header)
            spent += len(header)
            ran += 1
        if body:
            clipped = body[: max(0, max_chars - spent)]
            lines.append(clipped)
            spent += len(clipped)
    if not ran:
        return ""
    return "\n".join(lines)[:max_chars]
