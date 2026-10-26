"""M-D1/D2: the OS-level process sandbox -- write boundary, read boundary, AF_UNIX.

The cases split in two by what they can prove:

* U3/U3b/U4/U5/U6/U7 and the argv/order cases need **no** real mechanism: they
  use a backend double or inspect the argv, so they run everywhere and pin the
  contract that matters most -- fail-closed, the stable reason code, ``denied``
  rather than ``tool_error``, warn reporting, the bypass, and the bind order.
* U1/U1b/U1c/U2/U2b/V* run a **real** subprocess through bubblewrap.  They skip
  with an explicit reason when the mechanism cannot run (bubblewrap missing, or
  no unprivileged user namespace / ``CAP_SYS_ADMIN``) -- the honest outcome,
  because a skipped boundary test is not a green one.  CI installs bubblewrap
  and runs them.

U2b is a **reverse** assertion on purpose: M-D2 put the read boundary in scope,
so the case that used to pin its absence now pins its presence.  That is a
review-driven semantic change, recorded in ``docs/plan-m-d2.md`` §4.1.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from ai_coding.config.models import ProcessSandboxConfig
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
    "missing, or no unprivileged user namespace / CAP_SYS_ADMIN), so the OS "
    "write/read boundary cannot be exercised",
)


class _UnavailableBackend:
    """A mechanism that is not there -- the shape ``enforce`` must refuse."""

    name = "fake"
    tier = "full"

    def available(self) -> bool:
        return False

    def gap(self) -> str:
        return "bwrap.unavailable"

    def wrap(self, argv, roots):  # noqa: ANN001, ANN202 - never reached
        return list(argv)


class _UntouchableBackend:
    """Fails the test if a bypassed call consults the mechanism at all."""

    name = "explode"
    tier = "full"

    def available(self) -> bool:
        raise AssertionError("bypass must not consult the mechanism")

    def gap(self) -> str:
        raise AssertionError("bypass must not consult the mechanism")

    def wrap(self, argv, roots):  # noqa: ANN001, ANN202 - never reached
        raise AssertionError("bypass must not wrap")


class _NoWrapBackend:
    """Reports "available" and then declines to wrap -- the silent-pass gap."""

    name = "noop"
    tier = "full"

    def available(self) -> bool:
        return True

    def gap(self) -> str:
        return "sandbox.wrap.no-op"

    def wrap(self, argv, roots):  # noqa: ANN001, ANN202
        return None


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


@pytest.fixture
def fake_home(monkeypatch):
    """A throwaway HOME with its own XDG roots, for the read-boundary cases.

    The default denylist is expanded through ``config_home()`` / ``data_home()``
    (``XDG_CONFIG_HOME`` / ``XDG_DATA_HOME``), so a masked ``~/.config/ai-coding``
    only lands on the real key when those are honoured.  The directory lives
    under the *real* home: ``--tmpfs /tmp`` would hide a ``tmp_path``-based one.
    """
    base = Path(tempfile.mkdtemp(prefix="ai-coding-rd-", dir=str(Path.home())))
    monkeypatch.setenv("HOME", str(base))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(base / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(base / "data"))
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


def _spawn(
    command: str, backend: BwrapBackend, roots: list[Path], cwd: Path
) -> subprocess.CompletedProcess[bytes]:
    """Run one command through the single wrap point, with the seccomp fd passed."""
    argv = spawn_argv(command, ProcessSandboxPolicy(mode="enforce"), backend, roots)
    return subprocess.run(
        argv,
        cwd=str(cwd),
        capture_output=True,
        timeout=30,
        pass_fds=getattr(backend, "seccomp_fds", ()),
    )


def _default_read_backend() -> BwrapBackend:
    """A backend carrying the built-in read denylist (expanded for this env)."""
    cfg = ProcessSandboxConfig()
    return BwrapBackend(
        writable_paths=(),
        read_deny_paths=[Path(path).expanduser() for path in cfg.read_deny_paths],
    )


def _host_init_comm() -> str:
    try:
        return Path("/proc/1/comm").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


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


@_NEEDS_BWRAP
def test_a_sandboxed_background_job_can_be_killed(tmp_path):
    """P3: a job under ``enforce`` is stoppable, and its inner child dies too.

    ``Job.pid`` reports the bwrap process and ``kill`` signals the process
    group, so the check has to prove the *inner* command went with it -- an
    orphaned child would keep writing after the panel said "stopped".  The job
    writes a heartbeat into the worktree, and a stopped job must stop writing.
    """
    from ai_coding.core.jobs import JobRegistry

    backend = BwrapBackend(writable_paths=())
    registry = JobRegistry(
        root=tmp_path / "jobs",
        policy=ProcessSandboxPolicy(mode="enforce"),
        backend=backend,
        roots=[tmp_path],
    )
    heartbeat = tmp_path / "heartbeat.txt"
    job = registry.start(
        f"i=0; while [ $i -lt 100 ]; do echo $i >> {heartbeat}; "
        f"i=$((i+1)); sleep 0.2; done",
        cwd=str(tmp_path),
        session_id="s",
    )
    assert job.status == "running"

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if heartbeat.exists() and len(heartbeat.read_text(encoding="utf-8").splitlines()) >= 2:
            break
        time.sleep(0.05)

    killed = registry.kill(job.id, reason="test over")
    assert killed.status == "killed"
    assert job.done.wait(timeout=10), "the killed job never settled"

    count = len(heartbeat.read_text(encoding="utf-8").splitlines())
    time.sleep(1.0)
    after = len(heartbeat.read_text(encoding="utf-8").splitlines())
    assert after == count, (
        f"the sandboxed job's inner child kept running after the kill "
        f"({count} -> {after} lines)"
    )


def _subagent_cfg(ws: str):
    """The duck-typed app config ``build_subagent_runner`` needs."""
    from types import SimpleNamespace

    return SimpleNamespace(
        tools=SimpleNamespace(
            bash=SimpleNamespace(enabled=True, timeout_seconds=60),
            read=SimpleNamespace(enabled=True, max_file_size=1 << 20),
            write=SimpleNamespace(enabled=True),
            edit=SimpleNamespace(enabled=True),
            glob=SimpleNamespace(enabled=True),
            grep=SimpleNamespace(enabled=True),
            extra_workspace_roots=[],
            ask_user=SimpleNamespace(enabled=False),
        ),
        ai=SimpleNamespace(websearch_api_key="", websearch_max_results=5),
        mcp=SimpleNamespace(servers=[]),
        security=SimpleNamespace(grant_ttl_seconds=86400.0),
    )


def test_the_subagent_stack_inherits_the_process_sandbox(tmp_path):
    """P1-1: the child's ``BashTool`` carries the parent's policy/backend/roots.

    The wiring is what the P1-1 finding is about: ``build_subagent_runner`` used
    to build the child stack without them, so a delegated write through
    ``subAgent`` was unsandboxed even though the parent's own bash was held.  A
    real-subprocess case would skip where bwrap is absent, so this pins the
    wiring itself, everywhere.
    """
    from ai_coding.tools.builder import build_subagent_runner

    policy = ProcessSandboxPolicy(mode="enforce")
    backend = _UnavailableBackend()
    worktree = tmp_path / "child-worktree"
    worktree.mkdir()
    runner = build_subagent_runner(
        _subagent_cfg(str(tmp_path)),
        str(tmp_path),
        None,
        sandbox_policy=policy,
        sandbox_backend=backend,
    )

    tool = runner._build_tools(str(worktree)).get("bash")
    assert tool is not None
    assert tool._policy is policy
    assert tool._backend is backend
    # The child's writable face is its worktree, not the parent's workspace.
    assert tool._roots == (worktree.resolve(),)


@_NEEDS_BWRAP
@pytest.mark.asyncio
async def test_bash_through_the_subagent_stack_cannot_write_outside(
    tmp_path, outside_base
):
    """Through a delegated sub-agent's stack: the write boundary still holds."""
    from ai_coding.tools.builder import build_subagent_runner

    worktree = tmp_path / "child-worktree"
    worktree.mkdir()
    runner = build_subagent_runner(
        _subagent_cfg(str(tmp_path)),
        str(tmp_path),
        None,
        sandbox_policy=ProcessSandboxPolicy(mode="enforce"),
        sandbox_backend=BwrapBackend(writable_paths=()),
    )
    tool = runner._build_tools(str(worktree)).get("bash")
    assert tool is not None
    outside = _outside(outside_base, "subagent-outside.txt")
    result = await tool.execute(
        {"command": f"echo x > {outside}"}, ToolContext(workspace=str(worktree))
    )
    assert not outside.exists(), "a delegated sub-agent wrote outside its worktree"
    assert result.success is False


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
def test_enforce_cannot_read_a_denied_path_outside_the_workspace(tmp_path, outside_base):
    """M-D2 §B: the read boundary is in scope (U2b reversed).

    This case used to pin that the read boundary was *not* in scope; M-D2 made
    it the point, so it now pins the opposite.  A review-driven semantic change,
    recorded in ``docs/plan-m-d2.md`` §4.1 and here.
    """
    backend = BwrapBackend(writable_paths=(), read_deny_paths=[outside_base])
    secret = _outside(outside_base, "readable.txt")
    secret.write_text("secret\n", encoding="utf-8")
    argv = spawn_argv(
        f"cat {secret}", ProcessSandboxPolicy(mode="enforce"), backend, [tmp_path]
    )
    outcome = _run(argv, tmp_path)
    assert outcome.returncode != 0
    assert "secret" not in outcome.stdout.decode("utf-8", errors="replace")


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


def test_enforce_refuses_when_wrap_returns_nothing(tmp_path):
    """P2-2: a mechanism that answers "available" and then declines to wrap.

    ``wrap()`` returning ``None`` used to fall through to ``or argv`` and the
    command ran with no isolation at all -- the exact silent pass ``enforce``
    exists to prevent.
    """
    with pytest.raises(SandboxGap) as exc:
        spawn_argv(
            "echo x", ProcessSandboxPolicy(mode="enforce"), _NoWrapBackend(), [tmp_path]
        )
    assert exc.value.reason == "sandbox.wrap.no-op"


def test_warn_with_no_backend_reports_the_gap(tmp_path):
    """P2-2: ``warn`` keeps going *and* says so, with no mechanism at all."""
    seen: list[str] = []
    argv = spawn_argv(
        "echo x",
        ProcessSandboxPolicy(mode="warn"),
        None,
        [tmp_path],
        emit=seen.append,
    )
    assert argv == ["/bin/sh", "-c", "echo x"]
    assert seen == ["sandbox.backend.missing"]


def test_warn_reports_when_wrap_returns_nothing(tmp_path):
    """P2-2: a ``warn`` run whose mechanism declines to wrap is not a green lie."""
    seen: list[str] = []
    argv = spawn_argv(
        "echo x",
        ProcessSandboxPolicy(mode="warn"),
        _NoWrapBackend(),
        [tmp_path],
        emit=seen.append,
    )
    assert argv == ["/bin/sh", "-c", "echo x"]
    assert seen == ["sandbox.wrap.no-op"]


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
    """The switches a *selected* tier must carry, and the ``--proc`` invariant.

    ``no-proc`` has no ``--unshare-pid``, so an unconditional assertion would
    fail on every host whose ``--proc`` + ``--unshare-pid`` combination is
    refused (this one).  The invariant A.1 pins is ``--proc`` iff
    ``--unshare-pid``: a tier may keep ``/proc`` only together with its own pid
    namespace.
    """
    backend = BwrapBackend(writable_paths=())
    argv = backend.wrap(["/bin/true"], [tmp_path])
    for flag in ("--die-with-parent", "--new-session"):
        assert flag in argv, argv
    assert ("--proc" in argv) == ("--unshare-pid" in argv), argv
    if backend.tier == "full":
        assert "--unshare-pid" in argv, argv
        assert "--proc" in argv, argv
    else:
        assert "--unshare-pid" not in argv, argv
        assert "--proc" not in argv, argv
        # /tmp and /proc are both tmpfs'd in this tier.
        assert argv.count("--tmpfs") >= 2, argv


def test_wrap_masks_after_the_tmpfs_and_the_binds(tmp_path):
    """A mask must come after ``--ro-bind / /``, ``--tmpfs /tmp`` and the binds.

    Before the recursive bind it is covered; before ``--tmpfs /tmp`` a mask
    under ``/tmp`` is erased; before a writable bind the bind silently unmasks
    it (deny must win).
    """
    base = Path(tempfile.mkdtemp(prefix="ai-coding-order-", dir=str(Path.home())))
    try:
        denied = base / "denied"
        denied.mkdir()
        (denied / "secret.txt").write_text("S\n", encoding="utf-8")
        backend = BwrapBackend(writable_paths=[denied], read_deny_paths=[denied])
        argv = backend.wrap(["/bin/true"], [tmp_path])
        mask = argv.index(str(denied), argv.index("--bind"))
        assert mask > argv.index("--ro-bind"), argv
        assert mask > argv.index("--tmpfs"), argv
        assert mask > argv.index("--bind"), argv
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---- V1 / V1b / V2: construction tiers ------------------------------------


@_NEEDS_BWRAP
def test_a_construction_tier_is_selected_and_reported(tmp_path):
    """V1: the tier is selected, and a downgrade is reported exactly once."""
    backend = BwrapBackend(writable_paths=())
    seen: list[str] = []
    spawn_argv(
        "echo x", ProcessSandboxPolicy(mode="enforce"), backend, [tmp_path], emit=seen.append
    )
    tier = backend.tier
    assert tier in ("full", "no-proc"), tier
    if tier == "full":
        assert seen == [], seen
    else:
        assert seen == [f"sandbox.tier.{tier}"], seen
        # One-shot: the probe is cached, so the report is too.
        spawn_argv(
            "echo x",
            ProcessSandboxPolicy(mode="enforce"),
            backend,
            [tmp_path],
            emit=seen.append,
        )
        assert seen == [f"sandbox.tier.{tier}"], seen


@_NEEDS_BWRAP
def test_proc_escape_is_closed_per_tier(tmp_path):
    """V1b: the ``/proc/<pid>/root`` escape is closed, asserted per tier.

    An unconditional assertion would be red on ``full`` (where ``/proc`` is
    present by design) and blind on ``no-proc`` (where the escape is the whole
    point).  The ``full`` branch is what pins the escape closed in CI -- this
    host refuses ``--proc`` + ``--unshare-pid`` outright, so it never runs here.
    """
    backend = BwrapBackend(writable_paths=())
    argv = spawn_argv("true", ProcessSandboxPolicy(mode="enforce"), backend, [tmp_path])
    # A.1's invariant: --proc and --unshare-pid are born together.
    assert ("--proc" in argv) == ("--unshare-pid" in argv), argv

    if backend.tier == "no-proc":
        # /proc is an empty tmpfs: no host pids, no /proc/<pid>/root.
        listing = _spawn("ls /proc", backend, [tmp_path], tmp_path)
        assert listing.returncode == 0
        assert listing.stdout.decode("utf-8", errors="replace").strip() == "", listing.stdout
        escaped = _spawn("cat /proc/1/root/etc/hostname", backend, [tmp_path], tmp_path)
        assert escaped.returncode != 0
        # A.4b's known cost, pinned so nobody "fixes" it back into an escape.
        assert _spawn("ps", backend, [tmp_path], tmp_path).returncode != 0
        assert _spawn("bash -c 'cat <(echo hi)'", backend, [tmp_path], tmp_path).returncode != 0
        return

    assert backend.tier == "full", backend.tier
    listing = _spawn("ls /proc", backend, [tmp_path], tmp_path)
    assert listing.returncode == 0
    assert listing.stdout.decode("utf-8", errors="replace").strip() != ""
    # pid 1 inside a pid namespace is the sandbox's own init, not the host's.
    comm = _spawn("cat /proc/1/comm", backend, [tmp_path], tmp_path)
    assert comm.stdout.decode("utf-8", errors="replace").strip() != _host_init_comm()
    # The host's pid namespace is not visible, so the host root is unreachable.
    host_pid = os.getpid()
    escaped = _spawn(
        f"cat /proc/{host_pid}/root/etc/hostname", backend, [tmp_path], tmp_path
    )
    assert escaped.returncode != 0


def test_the_mechanism_runs_without_cap_sys_admin():
    """V2: ``--unshare-user-try`` makes the mechanism available as root.

    Without it ``available()`` was always false here, so every real-subprocess
    case skipped and the write boundary never ran on a development machine.
    """
    if shutil.which("bwrap") is None:
        pytest.skip("bubblewrap is not installed")
    backend = BwrapBackend(writable_paths=())
    assert backend.available(), (
        "with --unshare-user-try the mechanism must run as root without "
        "CAP_SYS_ADMIN"
    )
    assert backend.tier != "none"


# ---- V3-V8: the read boundary --------------------------------------------


@_NEEDS_BWRAP
def test_a_sandboxed_process_cannot_read_ssh_keys(tmp_path, fake_home):
    """V3: ``~/.ssh`` is hidden by the default denylist."""
    ssh = fake_home / ".ssh"
    ssh.mkdir(parents=True, exist_ok=True)
    (ssh / "id_rsa").write_text("SECRET-KEY\n", encoding="utf-8")
    backend = _default_read_backend()
    outcome = _spawn(f"cat {ssh}/id_rsa", backend, [tmp_path], tmp_path)
    assert outcome.returncode != 0
    assert "SECRET-KEY" not in outcome.stdout.decode("utf-8", errors="replace")


@_NEEDS_BWRAP
def test_a_sandboxed_process_cannot_read_cloud_credentials(tmp_path, fake_home):
    """V4: ``~/.aws/credentials`` is hidden."""
    aws = fake_home / ".aws"
    aws.mkdir(parents=True, exist_ok=True)
    (aws / "credentials").write_text("AWSSECRET\n", encoding="utf-8")
    backend = _default_read_backend()
    outcome = _spawn(f"cat {aws}/credentials", backend, [tmp_path], tmp_path)
    assert outcome.returncode != 0
    assert "AWSSECRET" not in outcome.stdout.decode("utf-8", errors="replace")


@_NEEDS_BWRAP
def test_a_sandboxed_process_cannot_read_the_repos_own_api_key(tmp_path, fake_home):
    """V5: the repository's own user config is hidden, via the XDG root."""
    cfg_dir = fake_home / "config" / "ai-coding"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text("api_key: LIVE-KEY\n", encoding="utf-8")
    backend = _default_read_backend()
    # The mask follows XDG_CONFIG_HOME, not a hardcoded ~/.config (review P2-1).
    assert any(
        path == cfg_dir for path in backend.mask_report.masked
    ), backend.mask_report
    outcome = _spawn(f"cat {cfg_dir}/config.yaml", backend, [tmp_path], tmp_path)
    assert outcome.returncode != 0
    assert "LIVE-KEY" not in outcome.stdout.decode("utf-8", errors="replace")


def test_the_read_boundary_hides_a_directory_and_a_file(tmp_path):
    """V6: a directory becomes empty, a file becomes unreadable."""
    base = Path(tempfile.mkdtemp(prefix="ai-coding-mask-", dir=str(Path.home())))
    try:
        directory = base / "secrets"
        directory.mkdir()
        (directory / "inside.txt").write_text("S\n", encoding="utf-8")
        file = base / "token.txt"
        file.write_text("TOKEN\n", encoding="utf-8")

        backend = BwrapBackend(writable_paths=(), read_deny_paths=[directory, file])
        argv = backend.wrap(["/bin/true"], [tmp_path])
        idx = argv.index(str(directory))
        assert argv[idx - 1] == "--tmpfs", argv
        idx = argv.index(str(file))
        assert argv[idx - 1] == "/dev/null" and argv[idx - 2] == "--ro-bind", argv

        assert backend.mask_report.masked == (directory.resolve(), file.resolve())

        outcome = _spawn(f"cat {file}", backend, [tmp_path], tmp_path)
        assert outcome.returncode != 0
        assert "TOKEN" not in outcome.stdout.decode("utf-8", errors="replace")
        listing = _spawn(f"ls {directory}", backend, [tmp_path], tmp_path)
        assert listing.returncode == 0
        assert listing.stdout.decode("utf-8", errors="replace").strip() == ""
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_a_writable_path_under_a_read_deny_is_still_unreadable(tmp_path):
    """V6b: deny wins -- the mask is applied after the writable bind."""
    base = Path(tempfile.mkdtemp(prefix="ai-coding-overlap-", dir=str(Path.home())))
    try:
        shared = base / "shared"
        shared.mkdir()
        (shared / "secret.txt").write_text("S\n", encoding="utf-8")
        backend = BwrapBackend(writable_paths=[shared], read_deny_paths=[shared])
        argv = backend.wrap(["/bin/true"], [tmp_path])
        mask = argv.index(str(shared), argv.index("--bind"))
        assert mask > argv.index("--bind"), argv
        outcome = _spawn(f"cat {shared}/secret.txt", backend, [tmp_path], tmp_path)
        assert outcome.returncode != 0
        assert "S" not in outcome.stdout.decode("utf-8", errors="replace")
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_the_read_boundary_expands_globs(tmp_path):
    """V7: a glob is expanded into one mask per match."""
    base = Path(tempfile.mkdtemp(prefix="ai-coding-glob-", dir=str(Path.home())))
    try:
        keys = base / "keys"
        keys.mkdir()
        (keys / "ssh_host_ed25519_key").write_text("K1\n", encoding="utf-8")
        (keys / "ssh_host_rsa_key").write_text("K2\n", encoding="utf-8")
        (keys / "ssh_host_ed25519_key.pub").write_text("PUB\n", encoding="utf-8")
        backend = BwrapBackend(
            writable_paths=(), read_deny_paths=[keys / "ssh_host_*_key"]
        )
        assert set(backend.mask_report.masked) == {
            keys / "ssh_host_ed25519_key",
            keys / "ssh_host_rsa_key",
        }
        assert (keys / "ssh_host_ed25519_key.pub") not in backend.mask_report.masked
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_a_missing_read_deny_path_is_skipped_without_degraded(tmp_path):
    """V8: a path that does not exist is skipped, not a degraded fallback.

    ``~/.aws`` / ``~/.gnupg`` / ``~/.netrc`` usually do not exist on a
    development machine; reporting them would pollute ``/trace``'s degraded
    count.  The skip is recorded on the backend instead.
    """
    missing = Path("/nonexistent/ai-coding/definitely-missing")
    backend = BwrapBackend(writable_paths=(), read_deny_paths=[missing])
    assert missing in backend.mask_report.skipped
    assert backend.mask_report.failed == ()

    seen: list[str] = []
    spawn_argv(
        "echo x",
        ProcessSandboxPolicy(mode="warn"),
        backend,
        [tmp_path],
        emit=seen.append,
    )
    assert not [reason for reason in seen if "mask" in reason], seen


# ---- V12 / V13: AF_UNIX ---------------------------------------------------


@_NEEDS_BWRAP
def test_a_sandboxed_process_cannot_reach_the_dbus_socket(tmp_path):
    """V12: a masked pathname socket refuses the connection.

    The masked path is a character device, so ``connect()`` fails.  The kernel
    answers a non-socket pathname with ``ECONNREFUSED`` (``unix_find_other``),
    not ``ENOTSOCK`` -- the plan's ``ENOTSOCK`` was a mis-measurement, and this
    asserts the code that is actually produced.
    """
    base = Path(tempfile.mkdtemp(prefix="ai-coding-afunix-", dir=str(Path.home())))
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock = base / "bus.sock"
        server.bind(str(sock))
        server.listen(1)
        backend = BwrapBackend(writable_paths=(), af_unix_deny_paths=[sock])
        client = (
            "import socket\n"
            "s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
            "try:\n"
            f"    s.connect({str(sock)!r})\n"
            "    print('CONNECTED')\n"
            "except OSError as exc:\n"
            "    print('ERRNO', exc.errno)\n"
        )
        outcome = _spawn(f"python3 -c \"{client}\"", backend, [tmp_path], tmp_path)
        assert b"CONNECTED" not in outcome.stdout, outcome.stdout
        assert b"ERRNO 111" in outcome.stdout, outcome.stdout
    finally:
        server.close()
        shutil.rmtree(base, ignore_errors=True)


@_NEEDS_BWRAP
def test_seccomp_blocks_af_unix_when_enabled(tmp_path):
    """V13: the seccomp tier refuses ``socket(AF_UNIX)`` and nothing else.

    The filter is handed to bwrap through an inherited descriptor
    (``--seccomp <fd>``), so the case also pins the ``pass_fds`` channel.
    """
    backend = BwrapBackend(writable_paths=(), block_af_unix=True)
    assert backend.seccomp_fds, "the filter fd must be exposed for pass_fds"
    probe = (
        "import socket, errno\n"
        "try:\n"
        "    socket.socket(socket.AF_UNIX)\n"
        "    print('AFIX_OK')\n"
        "except OSError as exc:\n"
        "    print('AFIX_ERR', exc.errno, errno.errorcode.get(exc.errno))\n"
        "try:\n"
        "    socket.socket(socket.AF_INET)\n"
        "    print('INET_OK')\n"
        "except OSError as exc:\n"
        "    print('INET_ERR', exc.errno)\n"
    )
    outcome = _spawn(f"python3 -c \"{probe}\"", backend, [tmp_path], tmp_path)
    assert b"AFIX_ERR 1 EPERM" in outcome.stdout, outcome.stdout
    assert b"INET_OK" in outcome.stdout, outcome.stdout


# ---- V16: symlinks -------------------------------------------------------


@_NEEDS_BWRAP
def test_a_symlinked_deny_path_masks_the_target(tmp_path):
    """V16: a deny path that is a symlink hides its target, not the link.

    Masking the link itself is rejected by bwrap (``--tmpfs <link>`` /
    ``--ro-bind /dev/null <link>`` both fail with "No such file or directory"),
    so the mask is applied to ``resolve()``'s target -- which hides it through
    the link too.
    """
    base = Path(tempfile.mkdtemp(prefix="ai-coding-link-", dir=str(Path.home())))
    try:
        target = base / "keys"
        target.mkdir()
        (target / "id_rsa").write_text("SECRET\n", encoding="utf-8")
        link = base / "ssh"
        link.symlink_to(target)

        # Pins the rule: masking the link directly is an error.
        direct = subprocess.run(
            [
                "bwrap",
                "--unshare-user-try",
                "--ro-bind",
                "/",
                "/",
                "--tmpfs",
                str(link),
                "--",
                "/bin/true",
            ],
            capture_output=True,
            timeout=30,
        )
        assert direct.returncode != 0

        backend = BwrapBackend(writable_paths=(), read_deny_paths=[link])
        assert target.resolve() in backend.mask_report.masked
        outcome = _spawn(f"cat {link}/id_rsa", backend, [tmp_path], tmp_path)
        assert outcome.returncode != 0
        assert "SECRET" not in outcome.stdout.decode("utf-8", errors="replace")
    finally:
        shutil.rmtree(base, ignore_errors=True)
