"""M-D1/D2: the OS-level process sandbox -- write boundary, read boundary, AF_UNIX.

The cases split in two by what they can prove:

* U3/U3b/U4/U5/U6/U7 and the argv/order cases need **no** real mechanism: they
  use a backend double or inspect the argv, so they run everywhere and pin the
  contract that matters most -- fail-closed, the stable reason code,
  ``sandbox_unavailable`` rather than ``denied``/``tool_error`` (A-4), warn
  reporting, the bypass, and the bind order.
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
import platform
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_coding.config.loader import ConfigLoader
from ai_coding.config.models import NetworkConfig, ProcessSandboxConfig
from ai_coding.core.events import Degraded
from ai_coding.security.process_sandbox import (
    _PROXY_ENV_NAMES,
    _SOCKET_NR,
    MASK_FAILED,
    SHELL_ARGV0,
    BwrapBackend,
    ProcessSandboxPolicy,
    SandboxGap,
    af_unix_seccomp_filter,
    build_process_sandbox,
    default_bridge_argv,
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

#: The seccomp AF_UNIX filter is built from a table of per-architecture
#: ``socket`` syscall numbers; an architecture the table does not know is
#: refused by the backend.  The guard turns that refusal into an explicit skip
#: rather than a red test on a host where the filter cannot be installed
#: (review P1-1: a filter that silently allowed AF_UNIX would read as a pass).
_SECCOMP_ARCH = platform.machine().lower()
_SECCOMP_ARCH_GUARD = pytest.mark.skipif(
    _SECCOMP_ARCH not in _SOCKET_NR,
    reason=f"the seccomp AF_UNIX filter is not built for {platform.machine()!r}: "
    "block_af_unix refuses to install there rather than silently allow AF_UNIX",
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


def _proxy_resolver(host: str, port: int):  # noqa: ANN202 - test helper
    """Point every allowed name at the local upstream.

    The production resolver refuses loopback after DNS (§C.6); this injection
    lets the bridge/bwrap path be exercised offline without weakening that
    check (``test_sandbox_proxy.py`` tests the real resolver).
    """
    return (socket.AF_INET, socket.SOCK_STREAM, 0, ("127.0.0.1", port))


@pytest.fixture
def upstream_http():
    """A tiny local HTTP origin, returning a recognisable body."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server's spelling
            body = b"HELLO-FROM-UPSTREAM\n"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


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
    """No mechanism + enforce = sandbox_unavailable, and the command never runs."""
    from ai_coding.tools.bash_tool import BashTool

    tool = BashTool(
        policy=ProcessSandboxPolicy(mode="enforce"), backend=_UnavailableBackend()
    )
    result = await tool.execute(
        {"command": "echo must-not-run"}, ToolContext(workspace=str(tmp_path))
    )
    assert result.success is False
    assert result.error == "sandbox_unavailable"
    assert result.data is not None
    assert result.data["reason"] == "bwrap.unavailable"


@pytest.mark.asyncio
async def test_a4_the_waiting_path_names_the_sandbox_not_a_denial(tmp_path):
    """With a job registry every foreground call waits on its own job."""
    from ai_coding.core.jobs import JobRegistry
    from ai_coding.tools.bash_tool import BashTool

    policy = ProcessSandboxPolicy(mode="enforce")
    backend = _UnavailableBackend()
    registry = JobRegistry(
        tmp_path / "jobs", policy=policy, backend=backend, roots=(tmp_path,)
    )
    tool = BashTool(policy=policy, backend=backend)
    result = await tool.execute(
        {"command": "echo x"},
        ToolContext(workspace=str(tmp_path), jobs=registry),
    )
    assert result.error == "sandbox_unavailable"
    assert result.data is not None and result.data["reason"] == "bwrap.unavailable"


@pytest.mark.asyncio
async def test_a4_the_background_path_names_the_sandbox_not_a_denial(tmp_path):
    from ai_coding.core.jobs import JobRegistry
    from ai_coding.tools.bash_tool import BashTool

    policy = ProcessSandboxPolicy(mode="enforce")
    backend = _UnavailableBackend()
    registry = JobRegistry(
        tmp_path / "jobs", policy=policy, backend=backend, roots=(tmp_path,)
    )
    tool = BashTool(policy=policy, backend=backend)
    result = await tool.execute(
        {"command": "echo x", "run_in_background": True},
        ToolContext(workspace=str(tmp_path), jobs=registry),
    )
    assert result.error == "sandbox_unavailable"
    assert result.data is not None and result.data["reason"] == "bwrap.unavailable"
    # A refused job never arms: nothing to list, stop or wake.
    assert registry.snapshot() == []


@pytest.mark.asyncio
async def test_a4_a_refused_job_names_the_sandbox_not_a_denial(tmp_path):
    from ai_coding.core.jobs import STATUS_FAILED, JobRegistry

    policy = ProcessSandboxPolicy(mode="enforce")
    registry = JobRegistry(
        tmp_path / "jobs",
        policy=policy,
        backend=_UnavailableBackend(),
        roots=(tmp_path,),
    )
    job = registry.start("echo x", cwd=str(tmp_path), session_id="s")
    assert job.status == STATUS_FAILED
    assert job.denied_reason == "bwrap.unavailable"
    assert job.detail == "未执行 · bwrap.unavailable"
    # Not registered: nothing for /tasks or job_list to show, nothing to kill.
    assert registry.snapshot() == []


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
    assert argv == [SHELL_ARGV0, "-c", "echo x"]
    assert seen == ["sandbox.backend.missing"]


def test_the_shell_understands_brace_expansion(tmp_path):
    """Agents write bash, so the interpreter has to be bash.

    Measured in eval-08's full-stack task: with ``/bin/sh`` (dash) the very
    ordinary ``mkdir -p src/{components,pages}`` exits 0 and creates a single
    directory *named* ``{components,pages}`` -- a silent wrong result that cost
    a whole round.  Skipped where only sh exists (Alpine): there the fallback is
    what ships, and this claim is not testable.
    """
    if Path(SHELL_ARGV0).name != "bash":
        pytest.skip("no bash on this host: sh is the documented fallback")
    argv = spawn_argv("mkdir -p src/{components,pages}", None, None, [tmp_path])
    subprocess.run(argv, cwd=tmp_path, check=True)
    assert (tmp_path / "src" / "components").is_dir()
    assert (tmp_path / "src" / "pages").is_dir()
    assert not (tmp_path / "src" / "{components,pages}").exists()


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
    assert argv == [SHELL_ARGV0, "-c", "echo x"]
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
async def test_the_gap_reaches_the_model_as_sandbox_unavailable(tmp_path):
    from ai_coding.tools.bash_tool import BashTool

    tool = BashTool(
        policy=ProcessSandboxPolicy(mode="enforce"), backend=_UnavailableBackend()
    )
    result = await tool.execute({"command": "echo x"}, ToolContext(workspace=str(tmp_path)))
    assert result.error == "sandbox_unavailable"
    assert result.data is not None
    assert result.data["reason"] == "bwrap.unavailable"


def test_bypass_skips_the_mechanism(tmp_path):
    argv = spawn_argv(
        "echo x",
        ProcessSandboxPolicy(mode="enforce", bypass=True),
        _UntouchableBackend(),
        [tmp_path],
    )
    assert argv == [SHELL_ARGV0, "-c", "echo x"]


def test_wrap_puts_the_writable_bind_after_the_readonly_root(tmp_path):
    """Order matters: a bind before ``--ro-bind / /`` is covered by it.

    Asserted on ``_build(..., "full")``: this is a shape claim, and a shape
    must not need the kernel (``wrap()`` returns ``None`` when the probe found
    no tier, which is exactly the contract F.1 pins).
    """
    backend = BwrapBackend(writable_paths=())
    argv = backend._build(["/bin/true"], [tmp_path], "full")
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
    argv = backend._build(["/bin/true"], [tmp_path], "full")
    assert argv.index("--tmpfs") < argv.index("--bind"), argv


def test_wrap_keeps_the_isolation_switches(tmp_path):
    """The switches a *selected* tier must carry, and the ``--proc`` invariant.

    Asserted on **both** constructions directly: reading ``backend.tier`` would
    trigger the probe, and the probe is exactly what is unavailable on a host
    without bwrap.  The invariant A.1 pins is ``--proc`` iff ``--unshare-pid``:
    a tier may keep ``/proc`` only together with its own pid namespace.

    Both tiers are asserted explicitly -- a single ``==`` over ``full`` alone
    is a tautology, because ``full`` always carries both flags (review P2).
    """
    backend = BwrapBackend(writable_paths=())
    full = backend._build(["/bin/true"], [tmp_path], "full")
    no_proc = backend._build(["/bin/true"], [tmp_path], "no-proc")

    for flag in ("--die-with-parent", "--new-session"):
        assert flag in full, full
        assert flag in no_proc, no_proc

    assert ("--proc" in full) == ("--unshare-pid" in full), full
    assert "--proc" in full and "--unshare-pid" in full, full
    assert ("--proc" in no_proc) == ("--unshare-pid" in no_proc), no_proc
    assert "--proc" not in no_proc and "--unshare-pid" not in no_proc, no_proc


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
        argv = backend._build(["/bin/true"], [tmp_path], "full")
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


@_NEEDS_BWRAP
def test_the_read_boundary_hides_a_directory_and_a_file(tmp_path):
    """V6: a directory becomes empty, a file becomes unreadable."""
    base = Path(tempfile.mkdtemp(prefix="ai-coding-mask-", dir=str(Path.home())))
    try:
        directory = base / "secrets"
        directory.mkdir()
        (directory / "inside.txt").write_text("S\n", encoding="utf-8")
        file = base / "token.txt"
        file.write_text("TOKEN\n", encoding="utf-8")

        backend = BwrapBackend(
            writable_paths=(),
            read_deny_paths=[directory, file],
            af_unix_deny_paths=(),
        )
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


@_NEEDS_BWRAP
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
            writable_paths=(),
            read_deny_paths=[keys / "ssh_host_*_key"],
            af_unix_deny_paths=(),
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


def test_an_untraversable_deny_path_does_not_abort_the_wrap(tmp_path, monkeypatch):
    """A deny path the host cannot stat must not abort the whole spawn.

    On a non-root CI runner, ``/root/.ssh`` exists but is not traversable, so
    ``exists()`` raises ``PermissionError``.  An uncaught one here used to
    propagate out of ``_mask_arguments`` and kill the entire spawn; the guard
    treats it as "not ours to mask": the sandbox still gets every mask that
    could be built, and the gap is recorded as **unreadable** -- visible, never
    silent, and crucially *not* a mask failure (a failure would make ``enforce``
    refuse to run on any non-root machine).
    """
    import ai_coding.security.process_sandbox as module

    base = Path(tempfile.mkdtemp(prefix="ai-coding-untraversable-", dir=str(Path.home())))
    try:
        denied = base / "denied"
        denied.mkdir()
        (denied / "inside.txt").write_text("S\n", encoding="utf-8")
        reachable = base / "reachable"
        reachable.mkdir()
        (reachable / "inside.txt").write_text("R\n", encoding="utf-8")

        original_exists = module.Path.exists

        def fake_exists(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            if self == denied.resolve():
                raise PermissionError(13, "Permission denied")
            return original_exists(self, *args, **kwargs)

        monkeypatch.setattr(module.Path, "exists", fake_exists)

        backend = module.BwrapBackend(
            writable_paths=(), read_deny_paths=[denied, reachable], af_unix_deny_paths=()
        )
        # The regression: this used to raise PermissionError out of wrap().
        argv = backend._build(["/bin/true"], [tmp_path], "full")

        report = backend.mask_report
        assert denied in report.skipped, report
        # Unreadable is recorded, and is NOT a failure: failing closed over
        # another user's directory would make the sandbox unusable anywhere
        # the operator is not root.
        assert any(
            entry[0] == denied and "unreadable" in entry[1] for entry in report.unreadable
        ), report
        assert report.failed == (), report
        # The mask that could be built is still applied, so the read boundary
        # is not silently weaker for the traversable paths.
        assert reachable.resolve() in report.masked, report
        assert str(reachable.resolve()) in argv, argv
    finally:
        shutil.rmtree(base, ignore_errors=True)


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
@_SECCOMP_ARCH_GUARD
def test_seccomp_blocks_af_unix_when_enabled(tmp_path):
    """V13: the seccomp tier refuses ``socket(AF_UNIX)`` and nothing else.

    The filter is handed to bwrap through an inherited descriptor
    (``--seccomp <fd>``), so the case also pins the ``pass_fds`` channel.  The
    arch guard skips on a host whose ``socket`` syscall number the filter table
    does not know: there the backend refuses to install the filter, and a green
    result would mean "the wall is a no-op" (review P1-1).
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


def _filter_socket_nr() -> int:
    """The ``socket`` syscall number embedded in the filter's ``JEQ k`` field.

    The BPF program is one ``struct sock_filter`` (8 bytes) per instruction:
    ``code(2) jt(1) jf(1) k(4)``.  The second instruction compares the syscall
    number, so its ``k`` is at offset 12.
    """
    return int.from_bytes(af_unix_seccomp_filter()[12:16], "little")


def test_the_seccomp_filter_embeds_the_running_arch_socket_number(monkeypatch):
    """P1-1: the ``socket`` number comes from the arch, not a hardcoded 41.

    A hardcoded 41 is x86_64 only; on aarch64, 41 is ``faccessat``, so the
    filter would silently allow ``socket(AF_UNIX)`` while blocking the wrong
    syscall.
    """
    import ai_coding.security.process_sandbox as module

    for arch, number in _SOCKET_NR.items():
        monkeypatch.setattr(module.platform, "machine", lambda arch=arch: arch)
        assert module._socket_syscall_number() == number, arch
        assert _filter_socket_nr() == number, arch


def test_an_unknown_arch_refuses_the_seccomp_filter(monkeypatch):
    """P1-1: an architecture the table does not know is refused, not guessed."""
    import ai_coding.security.process_sandbox as module

    monkeypatch.setattr(module.platform, "machine", lambda: "sparc64")
    with pytest.raises(module.UnsupportedSeccompArch):
        module._socket_syscall_number()
    with pytest.raises(module.UnsupportedSeccompArch):
        module.af_unix_seccomp_filter()
    with pytest.raises(module.UnsupportedSeccompArch):
        BwrapBackend(writable_paths=(), block_af_unix=True)


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


# ---- V9-V15: the network wall --------------------------------------------


def test_the_bridge_protocol_uses_the_first_double_dash_as_the_boundary():
    """§C.1 P3-3: the user argv may contain ``--``; only the first one is ours.

    Guessing which ``--`` belongs to the bridge would either run the wrong
    command or hand the user's arguments to the bridge.
    """
    from ai_coding.security.sandbox_bridge import _BridgeError, _parse

    sock, port, command = _parse(
        ["--unix", "/tmp/p.sock", "--port", "0", "--", "/bin/sh", "-c", "echo --unix -- x"]
    )
    assert sock == "/tmp/p.sock"
    assert port == 0
    assert command == ["/bin/sh", "-c", "echo --unix -- x"]

    with pytest.raises(_BridgeError):
        _parse(["--unix", "/tmp/p.sock"])
    with pytest.raises(_BridgeError):
        _parse(["--unix", "/tmp/p.sock", "--"])


@_NEEDS_BWRAP
def test_a_sandboxed_process_has_no_network_by_default(tmp_path):
    """V9: the default is ``deny`` -- the sandbox cannot reach the host network.

    A read/write wall without a network wall leaks: what the sandbox reads can
    be sent straight out.  ``off`` is the opt-in escape hatch, never the default.
    """
    backend = BwrapBackend(writable_paths=(), network=NetworkConfig())
    assert backend.network_mode == "deny"
    argv = spawn_argv(
        "curl -sS --max-time 3 http://1.1.1.1",
        ProcessSandboxPolicy(mode="enforce"),
        backend,
        [tmp_path],
    )
    assert "--unshare-net" in argv, argv
    outcome = _spawn(
        "curl -sS --max-time 3 http://1.1.1.1", backend, [tmp_path], tmp_path
    )
    assert outcome.returncode != 0, "the default network mode reached the host network"


@_NEEDS_BWRAP
def test_the_proxy_bridge_lets_an_allowed_domain_through(tmp_path, upstream_http):
    """V10: the proxy tier bridges loopback to the bound Unix socket.

    The whitelist is decided by the proxy (outside the netns); an unlisted name
    is refused.  The ``--bind`` of the socket is exercised for real -- a socket
    file is not a regular file, so this is the case that proves bwrap preserves
    its semantics (review P2-7).
    """
    from ai_coding.server.sandbox_proxy import SandboxProxy

    sock = tmp_path / "proxy.sock"
    proxy = SandboxProxy(
        sock, allow=[f"allowed.test:{upstream_http}"], resolve_target=_proxy_resolver
    ).start()
    backend = BwrapBackend(
        writable_paths=(),
        network=NetworkConfig(
            mode="proxy", proxy_socket=sock, allow=[f"allowed.test:{upstream_http}"]
        ),
        proxy=proxy,
        bridge_argv=default_bridge_argv(),
    )
    try:
        assert backend.network_mode == "proxy"
        argv = spawn_argv("true", ProcessSandboxPolicy(mode="enforce"), backend, [tmp_path])
        resolved_sock = str(sock.resolve())
        bind_at = next(
            index
            for index, value in enumerate(argv)
            if value == "--bind" and argv[index + 1] == resolved_sock
        )
        assert argv[bind_at + 2] == resolved_sock, argv
        assert "--unshare-net" in argv, argv
        assert any("sandbox_bridge" in value for value in argv), argv

        allowed = _spawn(
            f"curl -sS --max-time 5 http://allowed.test:{upstream_http}/",
            backend,
            [tmp_path],
            tmp_path,
        )
        assert allowed.returncode == 0, allowed.stderr
        assert b"HELLO-FROM-UPSTREAM" in allowed.stdout

        blocked = _spawn(
            f"curl -sS -f --max-time 5 http://blocked.test:{upstream_http}/",
            backend,
            [tmp_path],
            tmp_path,
        )
        assert blocked.returncode != 0
        assert b"HELLO-FROM-UPSTREAM" not in blocked.stdout
    finally:
        backend.aclose()


def test_the_proxy_refuses_a_wildcard_allow_list(tmp_path):
    """V11: ``allow=["*"]`` is rejected at load time, not at the first bash.

    A wildcard allow-list is no wall.  A configuration error must fail loudly
    when the app starts, so both the model and the loader path are pinned.
    """
    with pytest.raises(ValidationError):
        ProcessSandboxConfig(network={"allow": ["*"]})
    with pytest.raises(ValidationError):
        ConfigLoader.app_from_doc(
            {"process_sandbox": {"network": {"allow": ["*"]}}}
        )


@_NEEDS_BWRAP
def test_the_proxy_socket_bind_comes_after_the_tmpfs(tmp_path, upstream_http):
    """V14: a proxy socket under ``/tmp`` survives ``--tmpfs /tmp``.

    ``pytest``'s ``tmp_path`` is under ``/tmp``, so this is the real ordering
    case: a bind before the tmpfs would be erased, and the proxy would be
    unreachable even though every argv element looked right.
    """
    from ai_coding.server.sandbox_proxy import SandboxProxy

    sock = tmp_path / "proxy.sock"
    proxy = SandboxProxy(
        sock, allow=[f"allowed.test:{upstream_http}"], resolve_target=_proxy_resolver
    ).start()
    backend = BwrapBackend(
        writable_paths=(),
        network=NetworkConfig(
            mode="proxy", proxy_socket=sock, allow=[f"allowed.test:{upstream_http}"]
        ),
        proxy=proxy,
        bridge_argv=default_bridge_argv(),
    )
    try:
        argv = backend.wrap(["/bin/true"], [tmp_path])
        resolved_sock = str(sock.resolve())
        bind_at = next(
            index
            for index, value in enumerate(argv)
            if value == "--bind" and argv[index + 1] == resolved_sock
        )
        assert bind_at > argv.index("--tmpfs"), argv
        outcome = _spawn(
            f"curl -sS --max-time 5 http://allowed.test:{upstream_http}/",
            backend,
            [tmp_path],
            tmp_path,
        )
        assert outcome.returncode == 0, outcome.stderr
        assert b"HELLO-FROM-UPSTREAM" in outcome.stdout
    finally:
        backend.aclose()


@_NEEDS_BWRAP
def test_the_proxy_degrades_to_deny_never_to_off(tmp_path):
    """V15: an unavailable proxy means **no network**, never the host network.

    The socket's parent is a regular file, so ``bind()`` fails with ENOTDIR.
    The backend must keep ``--unshare-net`` and drop the bridge/bind, and say
    so through the one-shot ``sandbox.proxy.unavailable`` report.
    """
    from types import SimpleNamespace

    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory\n", encoding="utf-8")
    cfg = ProcessSandboxConfig(
        network=NetworkConfig(
            mode="proxy",
            proxy_socket=blocker / "proxy.sock",
            allow=["example.com"],
        )
    )
    policy, backend = build_process_sandbox(
        SimpleNamespace(process_sandbox=cfg), tmp_path
    )
    try:
        assert backend.network_mode == "deny"
        seen: list[str] = []
        argv = spawn_argv(
            "echo x", policy, backend, [tmp_path], emit=seen.append
        )
        assert "sandbox.proxy.unavailable" in seen, seen
        assert "--unshare-net" in argv, argv
        assert not any("sandbox_bridge" in value for value in argv), argv
        outcome = _spawn(
            "curl -sS --max-time 3 http://1.1.1.1", backend, [tmp_path], tmp_path
        )
        assert outcome.returncode != 0
    finally:
        backend.aclose()


# ---- review P2-2 / P2-4 / P2-5 / P3-2 / P3-3 ----------------------------


def test_the_default_af_unix_masks_cover_supervisors_and_shared_agents():
    """P2-2: the default AF_UNIX denylist is a denylist; it was missing real ones.

    Measured: the sandbox reached ``/run/supervisor.sock`` (command execution)
    and ``/run/shared/minimal-ssh-agent.sock`` (a signing oracle).  Both are
    now in the default list.
    """
    from ai_coding.config.models import DEFAULT_AF_UNIX_DENY_PATHS

    assert "/run/supervisor.sock" in DEFAULT_AF_UNIX_DENY_PATHS
    assert "/run/shared/minimal-ssh-agent.sock" in DEFAULT_AF_UNIX_DENY_PATHS
    assert "/run/user/*/bus" in DEFAULT_AF_UNIX_DENY_PATHS


def test_the_forwarding_loop_survives_a_transient_accept_error(monkeypatch):
    """P2-4: a non-timeout OSError from accept must not end the relay."""
    import ai_coding.security.sandbox_bridge as bridge

    monkeypatch.setattr(bridge, "_set_pdeathsig", lambda sig: None)
    ppid_calls = {"n": 0}

    def fake_getppid() -> int:
        ppid_calls["n"] += 1
        return 1 if ppid_calls["n"] > 2 else 4242

    monkeypatch.setattr(bridge.os, "getppid", fake_getppid)

    accepts = {"n": 0}

    class _FakeListener:
        def settimeout(self, _timeout: float) -> None:
            return None

        def accept(self):
            accepts["n"] += 1
            if accepts["n"] == 1:
                raise OSError(24, "Too many open files")
            raise TimeoutError

    bridge._forwarding_loop(_FakeListener(), "/tmp/proxy.sock")
    # Without the guard the loop breaks on the first OSError (accepts == 1).
    assert accepts["n"] >= 2, accepts


def test_the_forwarding_loop_drops_a_connection_when_a_thread_cannot_start(
    monkeypatch,
):
    """P2-4: Thread.start can raise EAGAIN; drop the connection, keep relaying."""
    import ai_coding.security.sandbox_bridge as bridge

    monkeypatch.setattr(bridge, "_set_pdeathsig", lambda sig: None)
    ppid_calls = {"n": 0}

    def fake_getppid() -> int:
        ppid_calls["n"] += 1
        return 1 if ppid_calls["n"] > 2 else 4242

    monkeypatch.setattr(bridge.os, "getppid", fake_getppid)

    class _FakeThread:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        def start(self) -> None:
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(bridge.threading, "Thread", _FakeThread)

    accepts = {"n": 0}
    closed = {"n": 0}

    class _FakeClient:
        def close(self) -> None:
            closed["n"] += 1

    class _FakeListener:
        def settimeout(self, _timeout: float) -> None:
            return None

        def accept(self):
            accepts["n"] += 1
            if accepts["n"] == 1:
                return _FakeClient(), ("127.0.0.1", 1234)
            raise TimeoutError

    bridge._forwarding_loop(_FakeListener(), "/tmp/proxy.sock")
    assert accepts["n"] >= 2, accepts
    assert closed["n"] == 1, closed


def _unset_names(argv: list[str]) -> set[str]:
    return {argv[i + 1] for i in range(len(argv) - 1) if argv[i] == "--unsetenv"}


def test_the_deny_tier_cleans_inherited_proxy_env(tmp_path):
    """P2-5: the deny tier cleans the inherited proxy env, not just proxy."""
    backend = BwrapBackend(writable_paths=(), network=NetworkConfig(mode="deny"))
    argv = backend._build(["/bin/true"], [tmp_path], "full")
    assert set(_PROXY_ENV_NAMES).issubset(_unset_names(argv)), argv


def test_the_off_tier_cleans_inherited_proxy_env(tmp_path):
    """P2-5: all three network tiers clean the inherited proxy env uniformly."""
    backend = BwrapBackend(writable_paths=(), network=NetworkConfig(mode="off"))
    argv = backend._build(["/bin/true"], [tmp_path], "full")
    assert set(_PROXY_ENV_NAMES).issubset(_unset_names(argv)), argv


def _mask_failed_backend() -> object:
    class _Failed:
        name = "maskfail"
        tier = "full"

        def available(self) -> bool:
            return True

        def gap(self) -> str:
            return MASK_FAILED

        def wrap(self, argv, roots):  # noqa: ANN001, ANN202
            return list(argv)

        def take_mask_report(self) -> str | None:
            return MASK_FAILED

    return _Failed()


def test_enforce_refuses_when_a_mask_cannot_be_built(tmp_path):
    """P3-2: a failed mask is a refusal under enforce, not a silent pass."""
    with pytest.raises(SandboxGap) as exc:
        spawn_argv(
            "echo x", ProcessSandboxPolicy(mode="enforce"), _mask_failed_backend(), [tmp_path]
        )
    assert exc.value.reason == MASK_FAILED


def test_warn_reports_a_failed_mask(tmp_path):
    """P3-2: a warn run whose mask failed reports it instead of a green lie."""
    seen: list[str] = []
    argv = spawn_argv(
        "echo x",
        ProcessSandboxPolicy(mode="warn"),
        _mask_failed_backend(),
        [tmp_path],
        emit=seen.append,
    )
    assert argv == [SHELL_ARGV0, "-c", "echo x"]
    assert MASK_FAILED in seen


def test_take_mask_report_is_one_shot_and_silent_when_nothing_failed(tmp_path):
    """P3-2: the report is one-shot; a clean (skipped-only) mask reports nothing."""
    backend = BwrapBackend(writable_paths=(), read_deny_paths=[tmp_path / "missing"])
    assert backend.mask_report.failed == ()
    assert backend.take_mask_report() is None


def test_take_mask_report_reports_a_real_mask_failure(tmp_path, monkeypatch):
    """P3-2: a mask whose ``resolve()`` fails is reported (and consumed once)."""
    import ai_coding.security.process_sandbox as module

    original = module.Path.resolve

    def fake_resolve(self, *args, **kwargs):  # noqa: ANN001, ANN202
        if self.name == "boom":
            raise OSError("IO error")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(module.Path, "resolve", fake_resolve)
    target = tmp_path / "boom"
    target.mkdir()
    backend = module.BwrapBackend(writable_paths=(), read_deny_paths=[target])
    assert backend.mask_report.failed
    assert backend.take_mask_report() == MASK_FAILED
    assert backend.take_mask_report() is None  # one-shot


def test_a_protected_path_is_skipped_and_recorded(tmp_path):
    """P3-3: the bridge's exemption from the read masks is recorded, not silent."""
    bridge_script = Path(default_bridge_argv()[1])
    backend = BwrapBackend(writable_paths=(), read_deny_paths=[bridge_script])
    assert bridge_script.resolve() in backend.mask_report.skipped
    assert bridge_script.resolve() not in backend.mask_report.masked


# --- F: wrap() must not fall back to "full" when the probe found no tier ------
#
# Measured locally (no bwrap): `backend.tier` reported "none" while `wrap()`
# built the **full** construction.  One object, two contradictory answers -- and
# a caller that skips `available()` would get an argv that looks isolated.

def test_wrap_returns_none_when_the_mechanism_is_absent():
    """Contract: `None` means "nothing to wrap".

    Falling back to "full" hands out an argv that looks isolated and then fails
    at spawn time, which is a fail-open.
    """
    backend = BwrapBackend()
    if backend.available():                      # CI has bwrap: nothing to prove
        pytest.skip("bubblewrap is available here")
    assert backend.tier == "none"
    assert backend.wrap(["/bin/true"], []) is None


def test_tier_and_wrap_agree_when_the_mechanism_is_absent():
    """The two answers a backend gives about itself must not contradict."""
    backend = BwrapBackend()
    if backend.available():
        pytest.skip("bubblewrap is available here")
    assert backend.tier == "none"
    assert backend.wrap(["/bin/true"], []) is None, (
        "tier says none but wrap() built a construction"
    )


# ---- M-H2: the key must not reach the agent's own children ------------------


def test_consumed_secret_env_names_are_stripped():
    """`--unsetenv` is the existing hook the proxy variables already use.

    An env-based API key would otherwise be inherited by every `bash` child, and
    `echo $DEEPSEEK_API_KEY` is the whole attack.
    """
    backend = BwrapBackend(writable_paths=(), secret_env_names=("DEEPSEEK_API_KEY",))

    argv = backend._build(["sh", "-c", "true"], (), "full")

    assert "--unsetenv" in argv
    pairs = list(zip(argv, argv[1:], strict=False))
    assert ("--unsetenv", "DEEPSEEK_API_KEY") in pairs
    # The proxy names are still stripped: one loop, two sources.
    assert ("--unsetenv", "HTTP_PROXY") in pairs


def test_no_secret_names_means_no_extra_unsetenv():
    backend = BwrapBackend(writable_paths=())

    argv = backend._build(["sh", "-c", "true"], (), "full")

    pairs = list(zip(argv, argv[1:], strict=False))
    assert ("--unsetenv", "DEEPSEEK_API_KEY") not in pairs
