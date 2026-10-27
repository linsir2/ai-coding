"""M-G3: the test layers are declared, marked, and actually selected.

The taxonomy lives in ``pyproject.toml``; the marks live on the files.  A marker
that is declared but never applied is a hole, and an ``-m`` in ``addopts`` is
worse: it is prepended to *every* invocation, so ``pytest tests/test_mcp_stdio.py``
would collect zero tests and exit 5 (measured).

The configuration is read through ``pytestconfig.getini`` rather than by parsing
``pyproject.toml``: ``tomllib`` is 3.11+, and this suite runs on 3.10 too.

The layer membership is asserted from pytest's own collection, not from source
text: ``test_r17_dataflow.py``'s docstring mentions "integration" and
``test_mcp_oauth.py`` mentions "live", so a substring match would be fooled.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PYPROJECT = REPO / "pyproject.toml"

GATED_FILES = (
    "tests/test_mcp_stdio.py",
    "tests/test_mcp_end_to_end.py",
    "tests/test_mcp_progress.py",
)

#: A subprocess that hangs (a module that blocks on import) must not eat the
#: job's whole 15-minute budget.
_SUBPROCESS_TIMEOUT = 120


def _pytest(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", *args],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT,
    )


def _collected_files(result: subprocess.CompletedProcess[str]) -> set[str]:
    """The files pytest collected, from either output shape.

    ``--collect-only -q`` prints ``tests/x.py: 3`` (a count per file); with
    ``-v`` it prints ``tests/x.py::test_name``.  Both are accepted so the helper
    does not depend on a verbosity flag.  Warnings go to stderr, and the
    ``rpartition`` guard rejects anything that is not ``<path>.py: <int>``.
    """
    files: set[str] = set()
    for raw in result.stdout.splitlines():
        line = raw.strip()
        if "::" in line:
            files.add(line.split("::")[0])
            continue
        name, sep, count = line.rpartition(": ")
        if sep and count.isdigit() and name.endswith(".py"):
            files.add(name)
    return files


# ---- the declaration --------------------------------------------------------


def test_the_layer_markers_are_declared(pytestconfig):
    declared = " ".join(pytestconfig.getini("markers"))
    assert "integration:" in declared
    assert "live:" in declared


def test_strict_markers_is_on(pytestconfig):
    """An undeclared marker must be a collection error, not a warning."""
    assert "--strict-markers" in pytestconfig.getini("addopts")


def test_addopts_does_not_select_a_layer(pytestconfig):
    """``-m`` in addopts would break every single-file invocation.

    Measured: with ``-m 'not live'`` in addopts, ``pytest tests/test_mcp_stdio.py``
    collects zero tests and exits 5 -- which would break the documented
    single-file commands and the live CI step.  CI selects the layer explicitly.
    """
    addopts = list(pytestconfig.getini("addopts"))
    assert not any(arg == "-m" or arg.startswith("-m=") for arg in addopts), (
        f"-m in addopts is prepended to every invocation: {addopts}"
    )


# ---- the membership ---------------------------------------------------------


def test_the_live_layer_is_exactly_the_gated_files():
    result = _pytest("--collect-only", "-q", "-m", "live")

    assert result.returncode == 0, result.stderr
    assert _collected_files(result) == {*GATED_FILES, "tests/test_live_gate.py"}


def test_the_integration_layer_is_exactly_the_suite():
    result = _pytest("--collect-only", "-q", "-m", "integration")

    assert result.returncode == 0, result.stderr
    expected = {
        f"tests/integration/{path.name}"
        for path in (REPO / "tests" / "integration").glob("test_*.py")
    }
    assert _collected_files(result) == expected


def test_the_three_layers_partition_the_suite():
    """Unit + integration + live must cover every collected test exactly once.

    ``-m`` expressions are independent; a file marked with both would be counted
    twice and a file marked with neither would fall out of every layer.
    """
    unit = _pytest("--collect-only", "-q", "-m", "not integration and not live")
    integration = _pytest("--collect-only", "-q", "-m", "integration")
    live = _pytest("--collect-only", "-q", "-m", "live")
    everything = _pytest("--collect-only", "-q")
    for result in (unit, integration, live, everything):
        assert result.returncode == 0, result.stderr

    def _count(result: subprocess.CompletedProcess[str]) -> int:
        total = 0
        for raw in result.stdout.splitlines():
            name, sep, count = raw.strip().rpartition(": ")
            if sep and count.isdigit() and name.endswith(".py"):
                total += int(count)
        return total

    assert _count(unit) + _count(integration) + _count(live) == _count(everything)


def test_a_gated_file_keeps_its_asyncio_and_skipif_marks():
    """Appending ``live`` must not overwrite the module's existing mark list.

    Assigning ``pytestmark = pytest.mark.live`` would drop ``asyncio`` (the async
    cases stop being treated as coroutine tests) and ``skipif`` (a local run
    without the gate would really spawn an MCP server).  Selecting on all three
    marks at once is the behavioural way to see that none was lost.
    """
    for name in GATED_FILES:
        result = _pytest(
            "--collect-only", "-q", name, "-m", "live and asyncio and skipif"
        )
        assert result.returncode == 0, result.stderr
        assert _collected_files(result) == {name}, f"{name} lost a module-level mark"


# ---- the canary -------------------------------------------------------------


def test_the_live_gate_canary_fails_without_the_variable():
    """`-m live` without the gate must be a red build, not a silent all-skip.

    This is the CI mistake the canary exists to catch: every live test is
    ``skipif``-gated, so without the variable the whole layer skips and exits 0.
    The message is asserted too -- "some failure happened" is not the same claim.
    """
    env = {k: v for k, v in os.environ.items() if k != "AI_CODING_MCP_STDIO"}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-m", "live", "tests/test_live_gate.py"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        env=env,
        timeout=_SUBPROCESS_TIMEOUT,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "is not set, so every other live test just skipped" in (
        result.stdout + result.stderr
    )


# ---- strict-markers actually bites -----------------------------------------


def test_an_undeclared_marker_is_a_collection_error(tmp_path):
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        textwrap.dedent(
            """
            import pytest

            pytestmark = pytest.mark.totally_undeclared_marker

            def test_x():
                assert True
            """
        ),
        encoding="utf-8",
    )

    # `-c` on purpose: without it the rootdir lands on the temp directory and the
    # repo's `--strict-markers` never loads.
    result = _pytest("--collect-only", "-q", "-c", str(PYPROJECT), str(probe))

    assert result.returncode != 0
    assert "not found in `markers` configuration" in (result.stdout + result.stderr)
