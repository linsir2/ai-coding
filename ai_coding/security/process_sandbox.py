"""OS-level process sandbox (M-D1/D2): write boundary, read boundary, AF_UNIX.

The file-tool path sandbox (``security/sandbox.py``) bounds *which paths a file
tool may touch*.  It says nothing about what a ``bash`` command or a background
job may do: the runner only set ``cwd`` and a process group.  This module is the
other half of that boundary -- the process layer.

Two layers, deliberately separate:

* **policy** (:class:`ProcessSandboxPolicy`) -- *should* this process be
  isolated, and to what degree.  It knows nothing about the mechanism.
* **mechanism** (:class:`SandboxBackend`, today :class:`BwrapBackend`) -- *how*
  to isolate.  It knows nothing about the policy.

``spawn_argv`` is the **single** wrap entry point.  Both callers (``bash_tool``
and ``core/jobs``) go through it, so one of them forgetting to wrap is not
possible without deleting the helper.  It returns a ``list[str]`` and never a
joined string: a joined string handed back to ``shell=True`` is exactly the
path that re-parses the wrapper's arguments and silently loses the isolation
(the P0-1 finding in ``docs/plan-m-d.md`` §A.1b).

**Scope.**  M-D1 built the write boundary; M-D2 adds the **read boundary** and
the **AF_UNIX** layer (``docs/plan-m-d2.md`` §B/§D):

* *write* -- an ``enforce`` process cannot create, modify or delete outside the
  workspace and the configured writable paths.
* *read* -- the default denylist hides ``~/.ssh``, ``~/.aws``, the repository's
  own credentials and the like.  ``--ro-bind / /`` makes the whole root
  readable, so the read face is a **denylist** (``read_deny_paths``), never
  derived from ``writable_paths``: the two are independent sides of one wall.
* *AF_UNIX* -- pathname sockets are masked; a seccomp tier can refuse
  ``socket(AF_UNIX)`` outright (the abstract namespace has no path to mask).
  Stated honestly: ``--ro-bind / /`` already binds the whole host root, so
  without the seccomp tier the "allow-list" is every pathname socket on the
  host minus the few masked ones -- path-level control is close to none.

The network wall (M-D2 §C) lives here too: ``off`` / ``deny`` / ``proxy``.
``deny`` is the default -- a read/write wall without a network wall leaks, since
what the sandbox reads can be sent straight out.  ``proxy`` binds one Unix
socket into the sandbox and puts a small bridge at ``argv[0]``; the bridge
turns the socket into a loopback ``http://`` proxy URL, because no HTTP client
accepts ``unix://`` as a proxy scheme (measured).

The proxy is the only **TCP/UDP** exit, not the only exit: AF_UNIX is a
parallel channel the proxy does not see, which is why the AF_UNIX masks are a
separate layer (M-D2 §D) rather than a consequence of the network wall.

**Construction tiers.**  The mechanism is built in tiers, strongest first, and
the first tier that actually runs is selected and reported.  A tier is a
*prefix*; the probe runs the **full** construction (``wrap(["/bin/true"],
roots)`` with every addition) because a probe that only tries the prefix is
fooled by an environment that accepts the prefix and rejects a later bind.

Two hard rules the tiers encode:

1. ``--proc`` and ``--unshare-pid`` are born together.  ``--ro-bind / /`` is a
   **recursive** bind, so the host's ``/proc`` is inside the sandbox regardless
   of ``--proc``; without ``--unshare-pid`` it shows host pids and
   ``/proc/<host-pid>/root`` bypasses every read mask.  The ``no-proc`` tier
   therefore mounts ``--tmpfs /proc`` -- the only thing that closes the escape
   (measured: ``--unshare-pid`` alone does not).
2. ``--unshare-user-try`` is present in every tier.  Running as root without
   ``CAP_SYS_ADMIN`` (a container's normal shape) makes the namespace
   construction fail with "Operation not permitted"; the flag asks for a user
   namespace and continues without one when that is refused, which is what
   makes the mechanism available here at all.

``available()`` runs the *same* construction ``wrap()`` would (against
``/bin/true``), not just ``which``: a probe that only checks ``which`` is
fooled by an environment that forbids the pid namespace, which fails only at
the real call.  The probe carries every addition that can fail for environmental
reasons -- the writable binds, the read/AF_UNIX masks and the seccomp filter --
so a tier is not reported as available when the real call would fail (review
P1-2).  The answer is cached per instance, because the composition root hands
one backend to both the tool and the job table.
"""

from __future__ import annotations

import glob
import os
import platform
import struct
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any, Literal, Protocol

from ai_coding.config.models import (
    DEFAULT_AF_UNIX_DENY_PATHS,
    NetworkConfig,
    ProcessSandboxConfig,
)

__all__ = [
    "BwrapBackend",
    "ProcessSandboxPolicy",
    "SandboxBackend",
    "SandboxGap",
    "SandboxMaskReport",
    "SandboxMode",
    "build_process_sandbox",
    "default_bridge_argv",
    "spawn_argv",
]

#: The three tiers, matching roadmap's ALLOW / WARN / DENY intent.
SandboxMode = Literal["off", "warn", "enforce"]

#: The one reason code for "the mechanism is not installed / cannot run here".
BWRAP_GAP = "bwrap.unavailable"

#: The reason code for a mechanism that answered "available" and then declined
#: to wrap.  A ``None`` from ``wrap()`` is the protocol's "nothing to wrap", but
#: under ``enforce`` that is a gap -- the command would run with no isolation at
#: all -- so it is refused rather than passed through.
SANDBOX_WRAP_GAP = "sandbox.wrap.no-op"

#: The reason code for "``network.mode='proxy'`` was asked for, but the proxy
#: could not start".  The wall degrades to ``deny`` -- never to ``off``, which
#: would silently hand the sandbox the host network (review P1-7).
PROXY_UNAVAILABLE = "sandbox.proxy.unavailable"

#: The reason code for "a read/AF_UNIX mask could not be constructed".  §B.2:
#: a mask that exists but fails (an IO error) is a **failure**, not a skip --
#: the read boundary would be silently weaker, so ``enforce`` refuses and
#: ``warn`` reports (review P3-2).
MASK_FAILED = "sandbox.mask.failed"

#: The construction tiers, strongest first.  The first one that runs is selected
#: and reported (``sandbox.tier.<name>``).  A tier is a *prefix*: the writable
#: binds, the read masks and the seccomp filter are appended by ``wrap()``.
#:
#: A tier without ``--proc`` + ``--unshare-pid`` **must** carry ``--tmpfs /proc``
#: -- ``--ro-bind / /`` is recursive, so the host's ``/proc`` (and ``/sys``,
#: ``/run``) come in regardless of ``--proc``, and without ``--unshare-pid``
#: they expose host pids.  ``--tmpfs /proc`` is the only thing measured to close
#: that escape (review P0-1).
_CONSTRUCTION_TIERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "full",
        (
            "--unshare-user-try",
            "--ro-bind",
            "/",
            "/",
            "--tmpfs",
            "/tmp",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--unshare-pid",
            "--die-with-parent",
            "--new-session",
        ),
    ),
    (
        "no-proc",
        (
            "--unshare-user-try",
            "--ro-bind",
            "/",
            "/",
            "--tmpfs",
            "/tmp",
            "--tmpfs",
            "/proc",
            "--dev",
            "/dev",
            "--die-with-parent",
            "--new-session",
        ),
    ),
)

_TIER_FLAGS: dict[str, tuple[str, ...]] = dict(_CONSTRUCTION_TIERS)

#: Proxy environment variables the sandbox must not inherit.  The bridge sets
#: the correct values after ``--unsetenv``; an inherited ``NO_PROXY`` would let
#: a whitelisted client bypass the bridge entirely (review P3-5).
_PROXY_ENV_NAMES: tuple[str, ...] = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)


def _bridge_script() -> Path:
    """The in-sandbox bridge shipped beside this module."""
    return Path(__file__).with_name("sandbox_bridge.py").resolve()


def default_bridge_argv() -> tuple[str, ...]:
    """The composition root's ``argv[0]`` prefix for ``proxy`` mode.

    A *program*, not a shell wrapper: ``wrap()`` still returns ``list[str]``,
    so no second parsing layer is introduced (the P0-1 rule).  The interpreter
    is the running one (``/usr`` and the package are readable through the
    recursive ``--ro-bind / /``).
    """
    return (sys.executable or "python3", str(_bridge_script()))


class SandboxGap(Exception):
    """The mechanism is unavailable and the policy says stop.

    ``reason`` is a **stable** code (``"bwrap.unavailable"``,
    ``"sandbox.backend.missing"``).  The catch sites in ``bash_tool`` and
    ``core/jobs`` turn it into a denial; the code is what an operator reads in a
    log or a panel, so it must not be a free-form sentence.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ProcessSandboxPolicy:
    """Whether to isolate, and to what degree.  Says nothing about *how*.

    * ``off``      -- do not isolate (an MCP child, an explicit bypass).
    * ``warn``     -- isolate, but keep going when the mechanism is missing,
      reporting the fallback instead of failing.
    * ``enforce``  -- isolate, and **refuse** when the mechanism is missing
      (fail-closed).

    The default is ``enforce``: roadmap's acceptance criterion is "a process
    under ``enforce`` cannot write outside the workspace", and a default of
    ``off`` would make that criterion empty out of the box.  ``None`` is the
    only way to say "leave the behaviour exactly as it was" -- tool constructors
    default to ``None``, and the composition root passes the real policy.

    ``bypass`` is roadmap's named explicit allow: the operator said, for this
    run, not to isolate.

    ``writable_paths`` is the write face (an allow-list); ``read_deny_paths`` is
    the read face (a denylist).  They are **not** derived from each other:
    "writable implies readable" does not hold -- a path may be writable and
    still hidden (the mask is applied after the bind, so deny wins).
    """

    mode: SandboxMode = "enforce"
    bypass: bool = False
    #: Extra writable paths beyond the roots (build caches and the like).  The
    #: default in config carries ``~/.cache``; ``~/.ssh`` / ``~/.aws`` /
    #: ``~/.gnupg`` are never in it.  Widening this narrows the sandbox.
    writable_paths: tuple[Path, ...] = ()
    #: Paths hidden from the sandbox.  Directories become empty (``--tmpfs``),
    #: files become unreadable (``--ro-bind /dev/null``).  Globs are expanded at
    #: construction time.  Empty = the read boundary is off.
    read_deny_paths: tuple[Path, ...] = ()
    #: Pathname AF_UNIX sockets to mask.  Abstract sockets have no path and are
    #: only reachable through ``block_af_unix``.
    af_unix_deny_paths: tuple[Path, ...] = ()
    #: Install a seccomp filter refusing ``socket(AF_UNIX)`` (all namespaces,
    #: including abstract).  Off by default: it breaks legitimate IPC.
    block_af_unix: bool = False
    #: The network wall (M-D2 §C): ``off`` / ``deny`` / ``proxy``, default
    #: ``deny``.  The mechanism reads the same object; the policy carries it so
    #: the two layers describe one decision.
    network: NetworkConfig = dataclass_field(default_factory=NetworkConfig)


@dataclass(frozen=True)
class SandboxMaskReport:
    """What the read/AF_UNIX masking pass did, computed once per backend.

    A **construction-time summary**, not a per-call event: a skipped path is a
    normal case (``~/.aws`` usually does not exist) and must not pollute
    ``/trace``'s degraded count, while a failed mask is a real problem.  Kept on
    the backend so a panel or a log can read it once.
    """

    masked: tuple[Path, ...] = ()
    skipped: tuple[Path, ...] = ()
    failed: tuple[tuple[Path, str], ...] = ()


class SandboxBackend(Protocol):
    """The mechanism: actually isolate.  A pure argv transform.

    The protocol has exactly one job -- turn a command into a command that runs
    isolated -- and it never touches ``preexec_fn``, which would reintroduce the
    multi-threaded unsafety that made the argv approach the chosen one.
    """

    name: str

    @property
    def tier(self) -> str:
        """The selected construction tier: ``"full"`` / ``"no-proc"`` / ``"none"``."""
        ...

    def available(self) -> bool: ...

    def gap(self) -> str:
        """The **single** source of the reason code when ``available`` is false."""
        ...

    def wrap(self, argv: Sequence[str], roots: Sequence[Path]) -> list[str] | None:
        """The wrapped command; ``None`` means "nothing to wrap"."""
        ...


def _tier_report(backend: SandboxBackend) -> str | None:
    """The one-shot degraded report for a downgraded construction tier.

    ``full`` is not a downgrade, so it reports nothing; ``no-proc`` reports
    ``sandbox.tier.no-proc`` **once per backend** (the probe is cached, so the
    report is too -- not one per bash call).  A backend that is not a
    :class:`BwrapBackend` has no one-shot flag, so it reports every time; the
    only such backends are test doubles.
    """
    reporter = getattr(backend, "take_tier_report", None)
    if callable(reporter):
        result = reporter()
        return None if result is None else str(result)
    tier = backend.tier
    return None if tier == "full" else f"sandbox.tier.{tier}"


def _network_report(backend: SandboxBackend) -> str | None:
    """The one-shot report for a proxy that could not start.

    A backend without the reporter (a test double) has no proxy, so it reports
    nothing.  Like the tier report, this is consumed once per backend -- the
    composition root's probe already cached the answer.
    """
    reporter = getattr(backend, "take_network_report", None)
    if callable(reporter):
        result = reporter()
        return None if result is None else str(result)
    return None


def _mask_report(backend: SandboxBackend) -> str | None:
    """The one-shot report for a mask that could not be constructed (§B.2).

    ``SandboxMaskReport.failed`` was never consumed anywhere, so the plan's
    "a failed mask is refused under ``enforce`` / reported under ``warn``" rule
    did not exist: a broken read boundary simply ran weaker and nobody knew
    (review P3-2).  ``spawn_argv`` reads it and turns it into a refusal or a
    report.

    A test double without the reporter but with a ``mask_report`` attribute is
    read directly, so the contract can be pinned without a real bwrap.
    """
    reporter = getattr(backend, "take_mask_report", None)
    if callable(reporter):
        result = reporter()
        return None if result is None else str(result)
    report = getattr(backend, "mask_report", None)
    failed = getattr(report, "failed", None)
    return MASK_FAILED if failed else None


def spawn_argv(
    command: str,
    policy: ProcessSandboxPolicy | None,
    backend: SandboxBackend | None,
    roots: Sequence[Path],
    *,
    emit: Callable[[str], None] | None = None,
) -> list[str]:
    """A shell command as a directly spawnable argv (normalised + wrapped).

    Both callers share this one entry point.  Normalisation has exactly one
    shape -- ``["/bin/sh", "-c", command]`` -- and ``shell`` is always ``False``
    at the spawn site.

    Fail-closed, in both directions a mechanism can fail:

    * ``enforce`` with no mechanism raises :class:`SandboxGap`; the callers
      catch it and answer ``denied``.
    * ``enforce`` with a mechanism that answers "available" but then declines
      to wrap (``wrap()`` returns ``None``) raises too -- a silent pass here
      would run the command with no isolation at all.
    * ``warn`` continues in both cases and reports the fallback through
      ``emit`` (a stable reason code, not a sentence), never a green lie.

    A downgraded construction tier (``no-proc``) is reported the same way: the
    operator must be able to see that the sandbox is weaker than ``full``.
    """
    argv = ["/bin/sh", "-c", command]
    if policy is None or policy.mode == "off" or policy.bypass:
        return argv
    if backend is None:
        if policy.mode == "enforce":
            raise SandboxGap("sandbox.backend.missing")
        if emit is not None:
            emit("sandbox.backend.missing")
        return argv
    if not backend.available():
        if policy.mode == "enforce":
            raise SandboxGap(backend.gap())
        if emit is not None:
            emit(backend.gap())
        return argv
    report = _tier_report(backend)
    if report is not None and emit is not None:
        emit(report)
    network_report = _network_report(backend)
    if network_report is not None and emit is not None:
        emit(network_report)
    wrapped = backend.wrap(argv, roots)
    if wrapped is None:
        if policy.mode == "enforce":
            raise SandboxGap(SANDBOX_WRAP_GAP)
        if emit is not None:
            emit(SANDBOX_WRAP_GAP)
        return argv
    # ``wrap()`` populated the mask report; a mask that could not be built is a
    # real failure, not a skip (§B.2 / review P3-2).  Read it here so the
    # read boundary cannot silently run weaker.
    mask_report = _mask_report(backend)
    if mask_report is not None:
        if policy.mode == "enforce":
            raise SandboxGap(MASK_FAILED)
        if emit is not None:
            emit(mask_report)
    return wrapped


_GLOB_CHARS = "*?["


def _expand_globs(patterns: Sequence[Path]) -> tuple[Path, ...]:
    """Expand the glob patterns among ``patterns``; leave the rest alone.

    bwrap has no glob, so ``/etc/ssh/ssh_host_*_key`` is expanded here (read
    directory + fnmatch) into one mask per match.  Shared by the read boundary
    and the AF_UNIX masks so the two cannot drift (review P2-4).

    A **construction-time snapshot**: a key file created after the sandbox was
    built is not re-masked.  That is a capability boundary, recorded in
    ``docs/plan-m-d2.md`` §7.1.
    """
    expanded: list[Path] = []
    for pattern in patterns:
        text = str(pattern)
        if any(char in text for char in _GLOB_CHARS):
            for match in glob.glob(text):
                expanded.append(Path(match))
        else:
            expanded.append(pattern)
    return tuple(expanded)


# ---- seccomp: refuse socket(AF_UNIX) --------------------------------------

#: BPF instruction classes / modes (Linux ``linux/filter.h``).
_BPF_LD = 0x00
_BPF_JMP = 0x05
_BPF_RET = 0x06
_BPF_W = 0x00
_BPF_ABS = 0x20
_BPF_JEQ = 0x10
_BPF_K = 0x00

#: ``seccomp_data`` actions (``linux/seccomp.h``).
_SECCOMP_RET_ALLOW = 0x7FFF0000
_SECCOMP_RET_ERRNO = 0x00050000
_EPERM = 1

#: ``socket(2)`` syscall number by architecture.  A hardcoded ``41`` is
#: **x86_64 only**: on aarch64, 41 is ``faccessat``, so a filter written against
#: it silently *allows* ``socket(AF_UNIX)`` -- the exact P1-1 hole (the filter
#: matches the wrong syscall, so the wall is a no-op and the sandbox looks green).
#: The number is derived from the architecture the backend runs on, and an
#: architecture this table does not know is **refused**, never guessed.
#:
#: The sandbox runs on the same host, so ``platform.machine()`` is the arch the
#: filter will actually be evaluated under; there is no cross-arch case here.
_SOCKET_NR: dict[str, int] = {
    "x86_64": 41,
    "amd64": 41,
    "i386": 359,
    "i486": 359,
    "i586": 359,
    "i686": 359,
    "aarch64": 198,
    "arm64": 198,
    "armv7l": 281,
    "armv6l": 281,
    "riscv64": 198,
}

_AF_UNIX = 1


class UnsupportedSeccompArch(ValueError):
    """The seccomp filter cannot be built for this architecture."""


def _socket_syscall_number() -> int:
    """The ``socket(2)`` syscall number for the running architecture.

    Raises :class:`UnsupportedSeccompArch` for an architecture the table does
    not know, so ``block_af_unix=True`` fails loudly instead of installing a
    filter that allows every AF_UNIX socket.
    """
    machine = platform.machine().lower()
    number = _SOCKET_NR.get(machine)
    if number is None:
        raise UnsupportedSeccompArch(
            f"socket(2) syscall number is unknown for architecture {machine!r}; "
            "refusing to install a seccomp filter that would silently allow "
            "AF_UNIX sockets"
        )
    return number


def _sock_filter(code: int, jt: int = 0, jf: int = 0, k: int = 0) -> bytes:
    """One ``struct sock_filter`` (8 bytes, little-endian)."""
    return struct.pack("<HBBI", code, jt, jf, k)


def af_unix_seccomp_filter() -> bytes:
    """A BPF program: ``socket(AF_UNIX, ...)`` -> ``EPERM``, everything else allowed.

    The program is **not empty** on purpose: an empty filter makes
    ``prctl(PR_SET_SECCOMP)`` fail with ``EINVAL`` (measured), so the filter
    must carry at least one instruction.

    ``seccomp_data`` layout: ``nr`` at offset 0, ``args[0]`` at offset 16.  The
    domain is the low 32 bits of ``args[0]``, which is all ``AF_UNIX`` needs.

    The ``socket`` syscall number is looked up for the running architecture;
    a hardcoded 41 would be a silent no-op on aarch64 (review P1-1).
    """
    return b"".join(
        (
            _sock_filter(_BPF_LD | _BPF_W | _BPF_ABS, k=0),  # A = syscall nr
            _sock_filter(
                _BPF_JMP | _BPF_JEQ | _BPF_K,
                jt=0,
                jf=3,
                k=_socket_syscall_number(),
            ),
            _sock_filter(_BPF_LD | _BPF_W | _BPF_ABS, k=16),  # A = args[0]
            _sock_filter(_BPF_JMP | _BPF_JEQ | _BPF_K, jt=0, jf=1, k=_AF_UNIX),
            _sock_filter(_BPF_RET | _BPF_K, k=_SECCOMP_RET_ERRNO | _EPERM),
            _sock_filter(_BPF_RET | _BPF_K, k=_SECCOMP_RET_ALLOW),
        )
    )


def _seccomp_filter_fd() -> int:
    """Write the filter to a temp file and return the open descriptor.

    bwrap's ``--seccomp`` takes a **file descriptor**, so the caller must pass
    it (``subprocess``'s ``pass_fds``); the file itself is unlinked at once --
    the descriptor keeps the bytes alive.  The fd is owned by the backend and
    lives as long as it does.
    """
    fd, path = tempfile.mkstemp(prefix="ai-coding-seccomp-")
    try:
        os.write(fd, af_unix_seccomp_filter())
        os.lseek(fd, 0, os.SEEK_SET)
    except BaseException:
        os.close(fd)
        os.unlink(path)
        raise
    os.unlink(path)
    return fd


class BwrapBackend:
    """The mechanism, backed by the external ``bwrap`` binary (Flatpak's own).

    bwrap's semantics are "you get what you bind", which is an allow-list by
    construction for the write face -- the opposite of the "allow-default
    denylist that is always one rule short" the research calls out.  The write
    boundary is built by binding the whole root read-only and then binding the
    writable roots on top of it; the read boundary is built by *masking* the
    denylist on top of that.

    **Order matters.**  The read masks come *after* ``--ro-bind / /`` (a mask
    before it is covered by the recursive bind) **and** after ``--tmpfs /tmp``
    (a mask under ``/tmp`` would be erased by the tmpfs) **and** after the
    writable binds (a writable bind would otherwise silently unmask a denied
    path -- deny wins).  The seccomp filter is appended last.

    **Symlinks.**  A mask is applied to ``resolve()``'s target, never to the
    link itself: ``--tmpfs <link>`` and ``--ro-bind /dev/null <link>`` both make
    bwrap fail with "No such file or directory" (measured, bubblewrap 0.9.0).
    Resolving first hides the target, which hides it through the link too.

    No ``preexec_fn``: the wrapper is pure argv, so the runner's concurrent
    threads are unaffected.
    """

    name = "bwrap"

    def __init__(
        self,
        *,
        binary: str = "bwrap",
        writable_paths: Sequence[Path] = (),
        read_deny_paths: Sequence[Path] = (),
        af_unix_deny_paths: Sequence[Path] | None = None,
        block_af_unix: bool = False,
        network: NetworkConfig | None = None,
        proxy: Any = None,
        bridge_argv: Sequence[str] | None = None,
        bridge_script: Path | None = None,
    ) -> None:
        self._binary = binary
        self._writable = tuple(Path(path).expanduser().resolve() for path in writable_paths)
        self._read_deny = tuple(Path(path).expanduser() for path in read_deny_paths)
        af_unix = DEFAULT_AF_UNIX_DENY_PATHS if af_unix_deny_paths is None else af_unix_deny_paths
        self._af_unix_deny = tuple(Path(path).expanduser() for path in af_unix)
        self._block_af_unix = bool(block_af_unix)
        self._network = network if network is not None else NetworkConfig()
        self._proxy = proxy
        self._bridge_argv = (
            tuple(bridge_argv) if bridge_argv is not None else default_bridge_argv()
        )
        self._bridge_script = (
            Path(bridge_script) if bridge_script is not None else _bridge_script()
        )
        self._proxy_socket = (
            Path(self._network.proxy_socket).expanduser().resolve()
            if self._network.proxy_socket is not None
            else None
        )
        self._proxy_unavailable = self._compute_proxy_unavailable()
        self._network_emitted = False
        self._closed = False
        self._cache: bool | None = None
        self._tier: str = "none"
        self._tier_emitted = False
        self._mask_cache: tuple[list[str], SandboxMaskReport] | None = None
        self._mask_emitted = False
        self._seccomp_fd: int | None = None
        if self._block_af_unix:
            self._seccomp_fd = _seccomp_filter_fd()

    # ---- network -----------------------------------------------------------

    def _compute_proxy_unavailable(self) -> bool:
        """Whether ``proxy`` mode has to fall back to ``deny``.

        Never to ``off``: a proxy that is not there means *no* network, not the
        host network.  A ``proxy`` mode without a socket is a configuration
        error the config layer rejects; treating it as unavailable here keeps a
        hand-built backend fail-closed instead of crashing.
        """
        if self._network.mode != "proxy":
            return False
        if self._proxy_socket is None or self._proxy is None:
            return True
        return not bool(getattr(self._proxy, "available", False))

    @property
    def network_mode(self) -> str:
        """The effective mode: ``proxy`` degrades to ``deny``, never ``off``."""
        if self._network.mode == "proxy":
            return "deny" if self._proxy_unavailable else "proxy"
        return self._network.mode

    def take_network_report(self) -> str | None:
        """One-shot ``sandbox.proxy.unavailable`` report, or ``None``."""
        if self._network_emitted or not self._proxy_unavailable:
            return None
        self._network_emitted = True
        return PROXY_UNAVAILABLE

    # ---- tier selection ----------------------------------------------------

    @property
    def tier(self) -> str:
        """The selected construction tier: ``"full"`` / ``"no-proc"`` / ``"none"``."""
        if self._cache is None:
            self._probe()
        return self._tier

    def take_tier_report(self) -> str | None:
        """The one-shot degraded report for a downgraded tier, or ``None``.

        ``no-proc`` reports ``sandbox.tier.no-proc``; the cost that carries is
        that ``/proc`` is unavailable inside the sandbox (``ps``,
        ``readlink /proc/self/exe``, and bash process substitution ``<(...)``
        all break, because ``--dev /dev``'s ``/dev/fd`` link dangles).
        ``full`` is not a downgrade and reports nothing.
        """
        if self._cache is None:
            self._probe()
        if self._tier != "no-proc" or self._tier_emitted:
            return None
        self._tier_emitted = True
        return f"sandbox.tier.{self._tier}"

    def available(self) -> bool:
        if self._cache is None:
            self._cache = self._probe()
        return bool(self._cache)

    def gap(self) -> str:
        return BWRAP_GAP

    def _probe(self) -> bool:
        """Try each tier with the **full** construction; keep the first that runs."""
        for tier_name, _flags in _CONSTRUCTION_TIERS:
            if self._try_tier(tier_name):
                self._tier = tier_name
                return True
        self._tier = "none"
        return False

    def _try_tier(self, tier: str) -> bool:
        try:
            proc = subprocess.run(
                self._build(["/bin/true"], (), tier),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10.0,
                pass_fds=self._seccomp_fds(),
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0

    def _seccomp_fds(self) -> tuple[int, ...]:
        if self._seccomp_fd is None:
            return ()
        return (self._seccomp_fd,)

    @property
    def seccomp_fds(self) -> tuple[int, ...]:
        """The descriptors the caller must keep open and pass to the child.

        ``bwrap --seccomp FD`` reads the filter from an inherited descriptor, so
        ``subprocess``'s ``pass_fds`` is the only channel that reaches it while
        ``wrap()`` still returns a plain ``list[str]``.
        """
        return self._seccomp_fds()

    # ---- masking -----------------------------------------------------------

    @property
    def mask_report(self) -> SandboxMaskReport:
        """The construction-time summary of the read/AF_UNIX masks."""
        return self._mask_arguments()[1]

    def take_mask_report(self) -> str | None:
        """One-shot ``sandbox.mask.failed`` report, or ``None`` (review P3-2).

        The mask report is a construction-time summary; the report is consumed
        once per backend so a panel or log sees the failure once, not once per
        bash call (the same one-shot shape as the tier and proxy reports).
        """
        if self._mask_emitted or not self.mask_report.failed:
            return None
        self._mask_emitted = True
        return MASK_FAILED

    def _protected_paths(self) -> tuple[Path, ...]:
        """Paths a read mask must never cover: the bridge and the proxy socket.

        A mask over the bridge makes ``execvp`` fail with ``ENOENT`` and the
        whole ``proxy`` mode unusable; a mask over the proxy socket makes
        ``--bind`` fail.  Both are resolved and excluded before masking (review
        P1-3).  An ancestor directory is excluded too: ``--tmpfs`` on the parent
        would hide the child just as effectively.
        """
        protected: list[Path] = []
        try:
            protected.append(self._bridge_script.resolve())
        except OSError:
            pass
        if self._proxy_socket is not None:
            protected.append(self._proxy_socket)
        return tuple(protected)

    @staticmethod
    def _covers_protected(resolved: Path, protected: Sequence[Path]) -> bool:
        for path in protected:
            if path == resolved or path.is_relative_to(resolved):
                return True
        return False

    def _mask_arguments(self) -> tuple[list[str], SandboxMaskReport]:
        if self._mask_cache is not None:
            return self._mask_cache
        args: list[str] = []
        masked: list[Path] = []
        skipped: list[Path] = []
        failed: list[tuple[Path, str]] = []
        seen: set[Path] = set()
        protected = self._protected_paths()
        for pattern in (*self._read_deny, *self._af_unix_deny):
            for path in _expand_globs((pattern,)):
                try:
                    resolved = path.resolve()
                except OSError as exc:
                    failed.append((path, str(exc)))
                    continue
                if resolved in seen:
                    continue
                if self._covers_protected(resolved, protected):
                    # Skipped by design, but a silent skip is exactly how a
                    # broken read boundary hides: record it (review P3-3).
                    seen.add(resolved)
                    skipped.append(path)
                    continue
                # A path under a directory the operator cannot traverse (the
                # classic case: /root/.ssh on a CI runner that is not root)
                # raises PermissionError from exists()/is_dir() -- and an
                # uncaught one here aborts the whole spawn.  Treat it as
                # "not ours to mask": the sandbox still gets every mask that
                # could be built, and the skip is recorded, not silent.
                try:
                    present = resolved.exists()
                    is_dir = present and resolved.is_dir()
                except OSError as exc:
                    seen.add(resolved)
                    skipped.append(path)
                    failed.append((path, f"unreadable: {exc}"))
                    continue
                if not present:
                    seen.add(resolved)
                    skipped.append(path)
                    continue
                seen.add(resolved)
                if is_dir:
                    args += ["--tmpfs", str(resolved)]
                else:
                    args += ["--ro-bind", "/dev/null", str(resolved)]
                masked.append(resolved)
        report = SandboxMaskReport(
            masked=tuple(masked), skipped=tuple(skipped), failed=tuple(failed)
        )
        self._mask_cache = (args, report)
        return self._mask_cache

    # ---- construction ------------------------------------------------------

    def wrap(self, argv: Sequence[str], roots: Sequence[Path]) -> list[str] | None:
        if self._cache is None:
            self._probe()
        tier = self._tier if self._tier != "none" else "full"
        return self._build(argv, roots, tier)

    def _build(self, argv: Sequence[str], roots: Sequence[Path], tier: str) -> list[str]:
        flags = _TIER_FLAGS.get(tier)
        if flags is None:
            raise ValueError(f"unknown construction tier: {tier!r}")
        prefix = [self._binary, *flags]
        for root in roots:
            prefix += ["--bind", str(root), str(root)]
        for path in self._writable:
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError:
                continue
            prefix += ["--bind", str(path), str(path)]
        prefix += self._mask_arguments()[0]
        if self._seccomp_fd is not None:
            # bwrap reads the filter to EOF, which leaves the descriptor at its
            # end; the next spawn would read an empty program and fail with
            # EINVAL.  Rewind before every construction.
            os.lseek(self._seccomp_fd, 0, os.SEEK_SET)
            prefix += ["--seccomp", str(self._seccomp_fd)]
        mode = self.network_mode
        if mode in ("deny", "proxy"):
            prefix += ["--unshare-net"]
        # Clean the inherited proxy environment in **every** mode (review P2-5):
        # an inherited HTTP_PROXY/NO_PROXY is a channel a whitelisted client can
        # use to bypass the bridge, and the ``deny`` tier used to inherit it
        # untouched.  Only ``proxy`` re-sets the correct values, and it does so
        # through the in-sandbox bridge's environment.
        for name in _PROXY_ENV_NAMES:
            prefix += ["--unsetenv", name]
        if mode == "proxy":
            if self._proxy_socket is None:  # defensive: network_mode says proxy
                raise ValueError("network.mode='proxy' without a proxy socket")
            # After --tmpfs /tmp and after the masks: a proxy socket under /tmp
            # must survive the tmpfs, and a mask must never cover it.
            prefix += ["--bind", str(self._proxy_socket), str(self._proxy_socket)]
        prefix += ["--"]
        if mode == "proxy":
            bridge = [*self._bridge_argv, "--unix", str(self._proxy_socket)]
            if self._network.bridge_port:
                bridge += ["--port", str(self._network.bridge_port)]
            bridge += ["--"]
            return prefix + bridge + list(argv)
        return prefix + list(argv)

    def aclose(self) -> None:
        """Release the long-lived pieces the composition root owns.

        The proxy process is per-workspace and must not outlive ``AppWorld``;
        the seccomp descriptor is a file descriptor this backend opened.  Both
        are best-effort: shutdown must never fail a run.
        """
        if self._closed:
            return
        self._closed = True
        proxy = self._proxy
        closer = getattr(proxy, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001 - shutdown is best-effort
                pass
        if self._seccomp_fd is not None:
            try:
                os.close(self._seccomp_fd)
            except OSError:
                pass
            self._seccomp_fd = None


def build_process_sandbox(
    app_cfg: Any, workspace: str | Path
) -> tuple[ProcessSandboxPolicy, SandboxBackend]:
    """The policy + mechanism pair the composition root hands to both callers.

    One backend instance for ``BashTool`` and ``JobRegistry``: two instances
    would mean two probes and, worse, two different ideas of whether the
    mechanism exists.  ``roots`` is *not* built here -- it is the caller's
    ``Sandbox.roots``, so the process layer and the path layer share one
    writable face by construction.
    """
    cfg = getattr(app_cfg, "process_sandbox", None)
    if not isinstance(cfg, ProcessSandboxConfig):
        cfg = ProcessSandboxConfig()
    writable = tuple(Path(path).expanduser() for path in cfg.writable_paths or ())
    read_deny = tuple(Path(path).expanduser() for path in cfg.read_deny_paths or ())
    af_unix_deny = tuple(Path(path).expanduser() for path in cfg.af_unix_deny_paths or ())
    network = cfg.network
    if network.mode == "proxy" and cfg.block_af_unix:
        # The config model rejects this pair; a duck-typed namespace does not,
        # so keep the composition root fail-closed too.
        raise ValueError(
            "block_af_unix and network.mode='proxy' are mutually exclusive"
        )
    proxy = _start_sandbox_proxy(network)
    policy = ProcessSandboxPolicy(
        mode=cfg.mode,
        bypass=bool(cfg.allow_bypass),
        writable_paths=writable,
        read_deny_paths=read_deny,
        af_unix_deny_paths=af_unix_deny,
        block_af_unix=bool(cfg.block_af_unix),
        network=network,
    )
    backend = BwrapBackend(
        writable_paths=writable,
        read_deny_paths=read_deny,
        af_unix_deny_paths=af_unix_deny,
        block_af_unix=bool(cfg.block_af_unix),
        network=network,
        proxy=proxy,
        bridge_argv=default_bridge_argv(),
    )
    return policy, backend


def _start_sandbox_proxy(network: NetworkConfig) -> Any:
    """Start the proxy synchronously; ``None`` means "degrade to deny".

    ``bind()`` returning is the readiness signal: the very first sandboxed
    command may already carry ``--bind <socket>``, so a lazily started proxy
    would fail the spawn.  A failure is not raised -- the backend turns ``None``
    into ``deny`` plus ``sandbox.proxy.unavailable``, never into ``off`` (review
    P1-7).
    """
    if network.mode != "proxy":
        return None
    if network.proxy_socket is None:  # the model rejects this; defensive
        return None
    from ai_coding.server.sandbox_proxy import SandboxProxy

    proxy = SandboxProxy(network.proxy_socket, allow=network.allow)
    try:
        proxy.start()
    except OSError:
        return None
    return proxy
