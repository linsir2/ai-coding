"""M-D1: the OS-level process sandbox -- the write boundary.

The cases split in two by what they can prove:

* U3/U3b/U4/U5/U6/U7 need **no** real mechanism: they use a backend double, so
  they run everywhere and pin the contract that matters most -- fail-closed, the
  stable reason code, ``denied`` rather than ``tool_error``, warn reporting, the
  bypass, and the bind order.
* U1/U1b/U1c/U2/U2b run a **real** subprocess through bubblewrap.  This
  environment cannot run the mechanism (bubblewrap is not installed as
  delivered, and this kernel refuses its namespace construction even when it
  is), so they skip with an explicit reason instead of passing silently -- the
  honest outcome, because a skipped write-boundary test is not a green one.
  CI installs bubblewrap and runs them.

U2b is a **reverse** assertion on purpose: it pins that the read boundary is NOT
in this round's scope.  If someone adds read refusal later, this case goes red
and tells them it is a semantic change that needs its own review.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from ai_coding.core.events import Degraded
from ai_coding.security.process_sandbox import (
    BwrapBackend,
    ProcessSandboxPolicy,
    SandboxGap,
    spawn_argv,
)
from ai_coding.tools.base import ToolContext

#: The mechanism probe, run once.  ``False`` here (no bwrap, no userns) means
#: every real-subprocess case skips with this exact reason.
_BWRAP_AVAILABLE = BwrapBackend().available()
_NEEDS_BWRAP = pytest.mark.skipif(
    not _BWRAP_AVAILABLE,
    reason="bwrap unavailable: the mechanism cannot run here (bubblewrap "
    "missing, or the kernel refuses its namespace construction), so the OS "
    "write boundary cannot be exercised",
)


class _UnavailableBackend:
    """A mechanism that is not there -- the shape ``enforce`` must refuse."""

    name = "fake"

    def available(self) -> bool:
        return False

    def gap(self) -> str:
        return "bwrap.unavailable"

    def wrap(self, argv, roots):  # noqa: ANN001, ANN202 - never reached
        return list(argv)


class _UntouchableBackend:
    """Fails the test if a bypassed call consults the mechanism at all."""

    name = "explode"

    def available(self) -> bool:
        raise AssertionError("bypass must not consult the mechanism")

    def gap(self) -> str:
        raise AssertionError("bypass must not consult the mechanism")

    def wrap(self, argv, roots):  # noqa: ANN001, ANN202 - never reached
        raise AssertionError("bypass must not wrap")


@pytest.fixture
def outside_base():
    """A directory the sandbox still *sees*, for the "outside the workspace" file.

    ``--tmpfs /tmp`` hides everything under ``/tmp`` except the bound roots, so
    an "outside" path cannot sit beside a pytest ``tmp_path``: the write would
    be refused because the path does not exist, and the case would prove
    nothing.  The workspace stays under ``tmp_path`` (the bind comes after the
    tmpfs, which is exactly the ordering this module fixes), while the outside
    file lives under the home directory.
    """
    base = Path(tempfile.mkdtemp(prefix="ai-coding-md1-", dir=str(Path.home())))
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _outside(base: Path, name: str) -> Path:
    """A path outside the workspace, cleaned before the case runs."""
    target = base / name
    target.unlink(missing_ok=True)
    return target


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(argv, cwd=str(cwd), capture_output=True, timeout=30)


# ---- U1: the write boundary itself ----------------------------------------


@_NEEDS_BWRAP
def test_a_process_under_enforce_cannot_write_outside(tmp_path, outside_base):
    backend = BwrapBackend(writable_paths=())
    outside = _outside(outside_base, "outside.txt")
    argv = spawn_argv(
        f"echo x > {outside}", ProcessSandboxPolicy(mode="enforce"), backend, [tmp_path]
    )
    _run(argv, tmp_path)
    assert not outside.exists(), "a process under enforce wrote outside the workspace"


@_NEEDS_BWRAP
@pytest.mark.asyncio
async def test_bash_tool_execute_is_actually_sandboxed(tmp_path, outside_base):
    """Through the tool: a backend-only test cannot catch a caller forgetting."""
    from ai_coding.tools.bash_tool import BashTool

    backend = BwrapBackend(writable_paths=())
    outside = _outside(outside_base, "tool-outside.txt")
    tool = BashTool(
        policy=ProcessSandboxPolicy(mode="enforce"), backend=backend, roots=[tmp_path]
    )
    result = await tool.execute(
        {"command": f"echo x > {outside}"}, ToolContext(workspace=str(tmp_path))
    )
    assert not outside.exists()
    assert result.success is False


@_NEEDS_BWRAP
def test_a_background_job_is_sandboxed(tmp_path, outside_base):
    """Through ``JobRegistry.start``: the one wrap point for a job."""
    from ai_coding.core.jobs import JobRegistry

    backend = BwrapBackend(writable_paths=())
    registry = JobRegistry(
        root=tmp_path / "jobs",
        policy=ProcessSandboxPolicy(mode="enforce"),
        backend=backend,
        roots=[tmp_path],
    )
    outside = _outside(outside_base, "job-outside.txt")
    job = registry.start(f"echo x > {outside}", cwd=str(tmp_path), session_id="s")
    assert job.done.wait(timeout=30), "the job never settled"
    assert not outside.exists(), "a background job wrote outside the workspace"


# ---- U2 / U2b: the boundary's two sides ------------------------------------


@_NEEDS_BWRAP
def test_a_process_under_enforce_can_write_inside(tmp_path):
    """Prove: the boundary is a boundary, not a total lockdown."""
    backend = BwrapBackend(writable_paths=())
    inside = tmp_path / "inside.txt"
    argv = spawn_argv(
        f"echo x > {inside}", ProcessSandboxPolicy(mode="enforce"), backend, [tmp_path]
    )
    outcome = _run(argv, tmp_path)
    assert outcome.returncode == 0
    assert inside.read_text(encoding="utf-8").strip() == "x"


@_NEEDS_BWRAP
def test_enforce_still_reads_outside_the_workspace(tmp_path, outside_base):
    """Reverse assertion: the read boundary is NOT in this round's scope."""
    backend = BwrapBackend(writable_paths=())
    outside = _outside(outside_base, "readable.txt")
    outside.write_text("secret\n", encoding="utf-8")
    argv = spawn_argv(
        f"cat {outside}", ProcessSandboxPolicy(mode="enforce"), backend, [tmp_path]
    )
    outcome = _run(argv, tmp_path)
    assert outcome.returncode == 0
    assert "secret" in outcome.stdout.decode("utf-8", errors="replace")


# ---- U3 / U3b: fail-closed -----------------------------------------------


@pytest.mark.asyncio
async def test_enforce_refuses_when_the_mechanism_is_missing(tmp_path):
    """No mechanism + enforce = a denial, not a tool error and not a run."""
    from ai_coding.tools.bash_tool import BashTool

    tool = BashTool(
        policy=ProcessSandboxPolicy(mode="enforce"), backend=_UnavailableBackend()
    )
    result = await tool.execute(
        {"command": "echo must-not-run"}, ToolContext(workspace=str(tmp_path))
    )
    assert result.success is False
    assert result.error == "denied"
    assert result.data is not None
    assert result.data["reason"] == "bwrap.unavailable"


def test_enforce_refuses_when_the_backend_is_missing(tmp_path):
    """``enforce`` with ``backend=None`` is a gap, not a silent pass."""
    with pytest.raises(SandboxGap) as exc:
        spawn_argv(
            "echo x", ProcessSandboxPolicy(mode="enforce"), None, [tmp_path]
        )
    assert exc.value.reason == "sandbox.backend.missing"


@pytest.mark.asyncio
async def test_warn_degrades_and_reports(tmp_path):
    """``warn`` keeps going and says so -- a Degraded, never a green lie."""
    from ai_coding.tools.bash_tool import BashTool

    seen: list[object] = []
    tool = BashTool(
        policy=ProcessSandboxPolicy(mode="warn"), backend=_UnavailableBackend()
    )
    result = await tool.execute(
        {"command": "echo still-runs"},
        ToolContext(workspace=str(tmp_path), emit=seen.append, call_id="c1"),
    )
    assert result.success is True
    assert "still-runs" in result.output
    degraded = [event for event in seen if isinstance(event, Degraded)]
    assert degraded, seen
    assert degraded[0].component == "process_sandbox"
    assert degraded[0].reason == "bwrap.unavailable"


def test_the_gap_has_a_stable_reason_code(tmp_path):
    gap = SandboxGap("bwrap.unavailable")
    assert gap.reason == "bwrap.unavailable"
    assert str(gap) == "bwrap.unavailable"


@pytest.mark.asyncio
async def test_the_gap_reaches_the_model_as_denied(tmp_path):
    from ai_coding.tools.bash_tool import BashTool

    tool = BashTool(
        policy=ProcessSandboxPolicy(mode="enforce"), backend=_UnavailableBackend()
    )
    result = await tool.execute({"command": "echo x"}, ToolContext(workspace=str(tmp_path)))
    assert result.error == "denied"
    assert result.data is not None
    assert result.data["reason"] == "bwrap.unavailable"


def test_bypass_skips_the_mechanism(tmp_path):
    argv = spawn_argv(
        "echo x",
        ProcessSandboxPolicy(mode="enforce", bypass=True),
        _UntouchableBackend(),
        [tmp_path],
    )
    assert argv == ["/bin/sh", "-c", "echo x"]


def test_wrap_puts_the_writable_bind_after_the_readonly_root(tmp_path):
    """Order matters: a bind before ``--ro-bind / /`` is covered by it."""
    backend = BwrapBackend(writable_paths=())
    argv = backend.wrap(["/bin/true"], [tmp_path])
    readonly = argv.index("--ro-bind")
    writable = argv.index("--bind")
    assert writable > readonly, argv
    assert argv[writable + 1] == str(tmp_path)
    assert argv[writable + 2] == str(tmp_path)
    assert argv[-1] == "/bin/true"


def test_wrap_mounts_tmpfs_before_the_roots(tmp_path):
    """A root under ``/tmp`` must survive the ``--tmpfs /tmp`` mount.

    ``docs/plan-m-d.md`` §A.2 lists ``--tmpfs /tmp`` after the binds; measured
    here, that order hides a workspace under ``/tmp`` entirely (pytest's
    ``tmp_path`` is under ``/tmp``), so U2 would fail in CI.  The tmpfs goes
    first, and the bind overrides it.
    """
    backend = BwrapBackend(writable_paths=())
    argv = backend.wrap(["/bin/true"], [tmp_path])
    assert argv.index("--tmpfs") < argv.index("--bind"), argv


def test_wrap_keeps_the_isolation_switches(tmp_path):
    backend = BwrapBackend(writable_paths=())
    argv = backend.wrap(["/bin/true"], [tmp_path])
    for flag in ("--unshare-pid", "--die-with-parent", "--new-session"):
        assert flag in argv, argv
