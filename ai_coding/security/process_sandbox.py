"""OS-level process sandbox (M-D1): the write boundary, policy and mechanism apart.

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

**Scope, stated plainly.**  This round guarantees the **write** boundary only:
a process under ``enforce`` cannot create, modify or delete outside the
workspace and the configured writable paths.  It does **not** guarantee a read
boundary -- ``--ro-bind / /`` leaves ``~/.ssh/id_rsa``, ``~/.aws/credentials``
and ``/etc/shadow`` readable inside the sandbox.  That is a deliberate scope cut,
not an oversight, and ``tests/test_process_sandbox.py`` pins the reverse
assertion so "sandbox" is never misread as "isolation".  The network wall is
likewise not built here (``docs/plan-m-d.md`` §七).

The environment this module is developed in cannot run the mechanism: bubblewrap
is not installed as delivered, and this kernel (6.18) refuses the namespace
construction even once it is -- ``bwrap --unshare-pid --proc /proc`` answers
"Creating new namespace failed: Operation not permitted".  ``available()``
therefore answers honestly and ``enforce`` refuses, rather than pretending a
sandbox exists.  The write boundary itself was verified here with a variant that
drops only ``--proc``: a write outside the bound root fails with "Read-only file
system" and a write inside succeeds.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from ai_coding.config.models import ProcessSandboxConfig

__all__ = [
    "BwrapBackend",
    "ProcessSandboxPolicy",
    "SandboxBackend",
    "SandboxGap",
    "SandboxMode",
    "build_process_sandbox",
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
    """

    mode: SandboxMode = "enforce"
    bypass: bool = False
    #: Extra writable paths beyond the roots (build caches and the like).  The
    #: default in config carries ``~/.cache``; ``~/.ssh`` / ``~/.aws`` /
    #: ``~/.gnupg`` are never in it.  Widening this narrows the sandbox.
    writable_paths: tuple[Path, ...] = ()


class SandboxBackend(Protocol):
    """The mechanism: actually isolate.  A pure argv transform.

    The protocol has exactly one job -- turn a command into a command that runs
    isolated -- and it never touches ``preexec_fn``, which would reintroduce the
    multi-threaded unsafety that made the argv approach the chosen one.
    """

    name: str

    def available(self) -> bool: ...

    def gap(self) -> str:
        """The **single** source of the reason code when ``available`` is false."""
        ...

    def wrap(self, argv: Sequence[str], roots: Sequence[Path]) -> list[str] | None:
        """The wrapped command; ``None`` means "nothing to wrap"."""
        ...


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
    wrapped = backend.wrap(argv, roots)
    if wrapped is None:
        if policy.mode == "enforce":
            raise SandboxGap(SANDBOX_WRAP_GAP)
        if emit is not None:
            emit(SANDBOX_WRAP_GAP)
        return argv
    return wrapped


class BwrapBackend:
    """The mechanism, backed by the external ``bwrap`` binary (Flatpak's own).

    bwrap's semantics are "you get what you bind", which is an allow-list by
    construction -- the opposite of the "allow-default denylist that is always
    one rule short" the research calls out.  The write boundary is built by
    binding the whole root read-only and then binding the writable roots on
    top of it.

    The order matters and is not the one ``docs/plan-m-d.md`` §A.2 lists
    literally (implementation-review finding): a ``--tmpfs /tmp`` placed *after*
    ``--bind <root>`` hides a root that lives under ``/tmp`` -- measured here,
    ``echo x > /tmp/ws/in.txt`` answered "Directory nonexistent" and the real
    directory stayed empty.  pytest's ``tmp_path`` is under ``/tmp``, so the
    literal order would fail U2 in CI.  The invariant the plan actually states
    ("``--bind`` must come after ``--ro-bind / /``") is kept; the tmpfs/proc/dev
    mounts go *before* the binds so a root under ``/tmp`` survives them.

    No ``preexec_fn``: the wrapper is pure argv, so the runner's concurrent
    threads are unaffected.

    ``available()`` runs the *same* construction ``wrap()`` would (against
    ``-- true``), not just ``which``: a probe that only checks ``which`` is
    fooled by an environment that forbids the pid namespace, which fails only
    at the real call.  The answer is cached per instance, because the
    composition root hands one backend to both the tool and the job table.
    """

    name = "bwrap"

    def __init__(
        self, *, binary: str = "bwrap", writable_paths: Sequence[Path] = ()
    ) -> None:
        self._binary = binary
        self._writable = tuple(Path(path).expanduser().resolve() for path in writable_paths)
        self._cache: bool | None = None

    def available(self) -> bool:
        if self._cache is None:
            self._cache = self._probe()
        return self._cache

    def gap(self) -> str:
        return BWRAP_GAP

    def wrap(self, argv: Sequence[str], roots: Sequence[Path]) -> list[str]:
        prefix = [self._binary, "--ro-bind", "/", "/"]
        prefix += ["--tmpfs", "/tmp", "--proc", "/proc", "--dev", "/dev"]
        for root in roots:
            prefix += ["--bind", str(root), str(root)]
        for path in self._writable:
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError:
                continue
            prefix += ["--bind", str(path), str(path)]
        prefix += [
            "--unshare-pid",
            "--die-with-parent",
            "--new-session",
            "--",
        ]
        return prefix + list(argv)

    def _probe(self) -> bool:
        try:
            proc = subprocess.run(
                self.wrap(["/bin/true"], ()),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10.0,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0


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
    policy = ProcessSandboxPolicy(
        mode=cfg.mode,
        bypass=bool(cfg.allow_bypass),
        writable_paths=writable,
    )
    backend = BwrapBackend(writable_paths=writable)
    return policy, backend
