"""B1: the harness runs the project's own static checks when a turn stalls.

The ladder (``core/interventions.py``) decides *when* to ask; this module answers
with command output, so the model gets the finding instead of another keyword.

Every case here pins one property that keeps the feature from becoming a new
failure mode: it never raises, it never hangs, it never floods the prompt, and it
never turns a string from a repository's config into a shell.

The checks used in these tests are fake ``ruff`` binaries dropped into the
workspace's own ``.venv/bin`` -- that is both how a real project installs its
linter and how the allowlist decides what may run.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from ai_coding.core.static_check import run_static_checks

pytestmark = pytest.mark.asyncio


def _fake_check(workspace: Path, body: str, name: str = "ruff") -> Path:
    """A linter the workspace installs itself, exactly where a real one lives."""
    bin_dir = workspace / ".venv" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / name
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(0o755)
    return script


async def test_a_command_reports_its_output_and_exit_code(tmp_path: Path):
    _fake_check(tmp_path, "echo lint-was-here")
    block = await run_static_checks(str(tmp_path), ["ruff check ."], timeout=10.0)
    assert "$ ruff check ." in block
    assert "exit 0" in block
    assert "lint-was-here" in block


async def test_no_commands_means_nothing_to_say(tmp_path: Path):
    assert await run_static_checks(str(tmp_path), []) == ""


async def test_a_missing_check_is_reported_not_raised(tmp_path: Path):
    """A check that is not installed: the nudge still goes out and says so."""
    block = await run_static_checks(str(tmp_path), ["rubocop ."], timeout=10.0)
    assert block != ""
    assert "could not run" in block


async def test_a_hanging_check_is_bounded(tmp_path: Path):
    _fake_check(tmp_path, "sleep 30")
    started = time.monotonic()
    block = await run_static_checks(str(tmp_path), ["ruff check ."], timeout=0.2)
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, "the check must be bounded, not wait for the command"
    assert "timeout" in block


async def test_the_block_is_capped(tmp_path: Path):
    _fake_check(tmp_path, "printf 'x%.0s' $(seq 1 5000)")
    block = await run_static_checks(
        str(tmp_path), ["ruff check ."], timeout=10.0, max_chars=200
    )
    assert len(block) <= 200, "a 5k-character check must not flood the prompt"


async def test_the_workspace_venv_is_on_the_path(tmp_path: Path):
    _fake_check(tmp_path, "echo venv-was-on-path")
    block = await run_static_checks(str(tmp_path), ["ruff check ."], timeout=10.0)
    assert "venv-was-on-path" in block


async def test_an_empty_workspace_says_nothing(tmp_path: Path):
    assert await run_static_checks("", ["ruff check ."], timeout=10.0) == ""


async def test_a_shell_metacharacter_is_not_an_operator(tmp_path: Path):
    """P0: a repo's *tracked* config.example.yaml can set `commands`.

    The command is shlex-split and run without a shell, so `;` and `&&` are
    ordinary arguments -- they cannot chain a second command.
    """
    _fake_check(tmp_path, "echo safe")

    marker = tmp_path / "PWNED"
    await run_static_checks(
        str(tmp_path), [f"ruff check . ; touch {marker}"], timeout=10.0
    )
    assert not marker.exists()

    marker2 = tmp_path / "PWNED2"
    await run_static_checks(
        str(tmp_path), [f"ruff check . && touch {marker2}"], timeout=10.0
    )
    assert not marker2.exists()


async def test_an_unknown_program_is_refused_not_executed(tmp_path: Path):
    """A repo must not be able to name an arbitrary binary in that field."""
    marker = tmp_path / "PWNED3"
    bin_dir = tmp_path / ".venv" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    evil = bin_dir / "touch"
    evil.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    evil.chmod(0o755)

    block = await run_static_checks(str(tmp_path), ["touch anything"], timeout=10.0)

    assert not marker.exists()
    assert "skipped" in block
