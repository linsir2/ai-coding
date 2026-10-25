"""B1: the harness runs the project's own static checks when a turn stalls.

The ladder (``core/interventions.py``) decides *when* to ask; this module answers
with command output, so the model gets the finding instead of another keyword.

Every case here pins one property that keeps the feature from becoming a new
failure mode: it never raises, it never hangs, and it never floods the prompt.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from ai_coding.core.static_check import run_static_checks

pytestmark = pytest.mark.asyncio


async def test_a_command_reports_its_output_and_exit_code(tmp_path: Path):
    block = await run_static_checks(str(tmp_path), ["echo hi"], timeout=10.0)
    assert "$ echo hi" in block
    assert "exit 0" in block
    assert "hi" in block


async def test_no_commands_means_nothing_to_say(tmp_path: Path):
    assert await run_static_checks(str(tmp_path), []) == ""


async def test_a_missing_command_is_reported_not_raised(tmp_path: Path):
    block = await run_static_checks(
        str(tmp_path), ["definitely-not-a-real-command-xyz"], timeout=10.0
    )
    assert block != ""                       # the failure is still worth telling
    assert "exit" in block


async def test_a_hanging_command_is_bounded(tmp_path: Path):
    started = time.monotonic()
    block = await run_static_checks(str(tmp_path), ["sleep 30"], timeout=0.2)
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, "the check must be bounded, not wait for the command"
    assert block != ""


async def test_the_block_is_capped(tmp_path: Path):
    block = await run_static_checks(
        str(tmp_path),
        ["python3 -c \"print('x' * 5000)\""],
        timeout=10.0,
        max_chars=200,
    )
    assert len(block) < 600, "a 5k-line check must not flood the prompt"


async def test_the_workspace_venv_is_on_the_path(tmp_path: Path):
    """The checks live in ``.venv/bin`` and are usually not on the shell's PATH."""
    bin_dir = tmp_path / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    script = bin_dir / "fakecheck"
    script.write_text("#!/bin/sh\necho venv-was-on-path\n", encoding="utf-8")
    script.chmod(0o755)

    block = await run_static_checks(str(tmp_path), ["fakecheck"], timeout=10.0)

    assert "venv-was-on-path" in block


async def test_an_empty_workspace_says_nothing(tmp_path: Path):
    assert await run_static_checks("", ["echo hi"], timeout=10.0) == ""
