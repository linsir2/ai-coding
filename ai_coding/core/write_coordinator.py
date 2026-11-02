"""P2-B1: who is allowed to write what, right now.

The dispatcher asks for a :class:`Claim` before running a call that can touch
the workspace; a background ``bash`` job takes a :class:`Hold` that survives
the call and is released by the job's settlement.  Both live in one occupancy
table protected by :class:`threading.Lock`, because claims are awaited on the
event-loop thread while holds are released from job watcher threads.

The key is ``(root, relative-path)``.  ``root`` is the workspace the call was
made against, so an isolated sub-agent's worktree never conflicts with the
main tree.  A ``*`` entry (an opaque ``bash`` command, ``mcp_call``) excludes
every path in the same root; a path only excludes that path in the same root.

Nothing here is a lease or a fence: a foreground claim is released in the
dispatcher's ``finally`` (cancellation included), and the one long-held case —
a promoted background command — is released by the job table's own
``on_settle`` callback.  That is enough for one process, which is the contract
this batch has.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

#: Poll interval while waiting for a conflicting claim to be released.
POLL_SECONDS = 0.05
#: A run older than this is not evidence of a concurrent overlapping write.
RUN_TTL_SECONDS = 600.0
#: How many recent runs ``overlap`` compares against.
MAX_RUNS = 20

#: ``(root, relative path)``; the relative path ``*`` means the whole root.
Key = tuple[str, str]

__all__ = [
    "MAX_RUNS",
    "POLL_SECONDS",
    "RUN_TTL_SECONDS",
    "Claim",
    "WriteCoordinator",
]


def _norm_root(root: str | os.PathLike[str]) -> str:
    """The workspace key: resolved, so two spellings are one root."""
    return str(os.path.realpath(os.path.expanduser(str(root or "."))))


def _relpath(path: str, root: str) -> str:
    """A path as it is compared: workspace-relative, or absolute when outside."""
    try:
        relative = os.path.relpath(path, root)
    except ValueError:  # pragma: no cover - different drives on Windows
        return os.path.normpath(path)
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        return os.path.normpath(path)
    return os.path.normpath(relative)


@dataclass
class Claim:
    """A granted write scope.  ``release`` is synchronous and idempotent."""

    owner: str
    keys: tuple[Key, ...]
    _coordinator: WriteCoordinator = field(repr=False)
    _released: bool = field(default=False, repr=False)

    def release(self) -> None:
        with self._coordinator._lock:
            if self._released:
                return
            self._released = True
            for key in self.keys:
                self._coordinator._drop(key, self.owner)


@dataclass
class _Run:
    key: str
    paths: frozenset[str]
    origin_session: str
    at: float


class WriteCoordinator:
    """The process-wide write occupancy table for one workspace."""

    def __init__(self, workspace: str | os.PathLike[str]) -> None:
        self.workspace = _norm_root(workspace)
        #: ``key -> owner -> re-entrant count``.  Empty owner entries are
        #: removed so ``any(...)`` stays meaningful.
        self._held: dict[Key, dict[str, int]] = {}
        #: Hold tokens are separate from claims: a job callback releases the
        #: hold it was given and cannot accidentally release a claim.  Each
        #: token remembers ``(key, owner)`` so the release is exact.
        self._holds: dict[str, tuple[Key, str]] = {}
        self._runs: list[_Run] = []
        self._lock = threading.Lock()

    # ---- async claims -----------------------------------------------------

    async def claim(
        self,
        owner: str,
        root: str | os.PathLike[str],
        paths: Sequence[str] | Iterable[str],
        timeout: float,
    ) -> Claim | None:
        """Take ``paths`` in ``root`` for ``owner``, waiting at most ``timeout``.

        ``paths=()`` means "this whole workspace" (``*``).  The same owner may
        re-enter (counted), so a dispatcher claim and a bash tool's hold on the
        same key compose instead of deadlocking.  A non-positive timeout still
        tries once: a free path is claimed, a busy one is refused immediately.
        """
        root_s = _norm_root(root)
        keys = self._keys(root_s, paths)
        budget = max(0.0, float(timeout))
        deadline = time.monotonic() + budget
        while True:
            with self._lock:
                if not self._conflicts(owner, keys):
                    for key in keys:
                        self._owners(key)[owner] = self._owners(key).get(owner, 0) + 1
                    return Claim(owner=owner, keys=keys, _coordinator=self)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            await asyncio.sleep(min(POLL_SECONDS, remaining))

    # ---- synchronous holds ------------------------------------------------

    def acquire_hold(
        self, owner: str, root: str | os.PathLike[str]
    ) -> str | None:
        """Take the whole root synchronously; return a token, or ``None``.

        Called before ``JobRegistry.start`` (which is itself synchronous) and
        released by the job's ``on_settle`` callback.  Re-entrant for the same
        owner, exactly like :meth:`claim`.
        """
        key = (_norm_root(root), "*")
        with self._lock:
            if self._conflicts(owner, (key,)):
                return None
            self._owners(key)[owner] = self._owners(key).get(owner, 0) + 1
            token = uuid.uuid4().hex
            self._holds[token] = (key, owner)
            return token

    def release_hold(self, token: str | None) -> None:
        """Release one hold.  Idempotent: the job and the tool may both call."""
        if token is None:
            return
        with self._lock:
            held = self._holds.pop(token, None)
            if held is None:
                return
            key, owner = held
            self._drop(key, owner)

    # ---- run overlap (B2 consumes; B1 only maintains the table) -----------

    def register_run(
        self, key: str, paths: Iterable[str], origin_session: str = ""
    ) -> None:
        """Remember a finished run's changed paths for later comparisons.

        A multi-turn child registers once per turn.  Within the window the
        same key **unions** the new paths into the old set (and refreshes its
        timestamp) instead of replacing it: turn 2 that changed only ``b.py``
        must not erase turn 1's ``a.py`` from the overlap evidence.  An empty
        path set records nothing -- there is nothing to compare.
        """
        wanted = frozenset(self._relative(path) for path in paths)
        if not wanted:
            return
        key = str(key)
        now = time.monotonic()
        with self._lock:
            cutoff = now - RUN_TTL_SECONDS
            existing = next(
                (run for run in self._runs if run.key == key and run.at >= cutoff),
                None,
            )
            if existing is not None:
                existing.paths = existing.paths | wanted
                existing.origin_session = str(origin_session) or existing.origin_session
                existing.at = now
            else:
                self._runs = [run for run in self._runs if run.key != key]
                self._runs.append(
                    _Run(
                        key=key,
                        paths=wanted,
                        origin_session=str(origin_session),
                        at=now,
                    )
                )
            self._runs = [run for run in self._runs if run.at >= cutoff][-MAX_RUNS:]

    def overlap(self, key: str, paths: Iterable[str]) -> tuple[dict[str, Any], ...]:
        """Other recent runs that changed at least one of ``paths``.

        ``key`` excludes itself.  Paths are compared workspace-relative, so the
        absolute form a tool reports and the relative form a ledger keeps are
        the same fact.
        """
        wanted = frozenset(self._relative(path) for path in paths)
        if not wanted:
            return ()
        cutoff = time.monotonic() - RUN_TTL_SECONDS
        with self._lock:
            runs = [run for run in self._runs if run.at >= cutoff]
        hits: list[dict[str, Any]] = []
        for run in runs:
            if run.key == key:
                continue
            common = wanted & run.paths
            if common:
                hits.append(
                    {
                        "key": run.key,
                        "origin_session": run.origin_session,
                        "paths": tuple(sorted(common)),
                    }
                )
        return tuple(hits)

    # ---- internals --------------------------------------------------------

    def _keys(self, root: str, paths: Sequence[str] | Iterable[str]) -> tuple[Key, ...]:
        values = tuple(str(path) for path in paths)
        if not values:
            return ((root, "*"),)
        keys: list[Key] = []
        for path in values:
            key = (root, _relpath(path, root))
            if key not in keys:
                keys.append(key)
        return tuple(keys)

    def _relative(self, path: str) -> str:
        value = str(path)
        if os.path.isabs(value):
            return _relpath(value, self.workspace)
        return os.path.normpath(value)

    def _owners(self, key: Key) -> dict[str, int]:
        owners = self._held.get(key)
        if owners is None:
            owners = {}
            self._held[key] = owners
        return owners

    def _conflicts(self, owner: str, keys: Sequence[Key]) -> bool:
        for key in keys:
            root, relative = key
            for (held_root, held_relative), owners in self._held.items():
                if held_root != root or not owners:
                    continue
                if held_relative != "*" and relative != "*" and held_relative != relative:
                    continue
                if any(name != owner and count > 0 for name, count in owners.items()):
                    return True
        return False

    def _drop(self, key: Key, owner: str) -> None:
        owners = self._held.get(key)
        if owners is None:
            return
        count = owners.get(owner, 0)
        if count <= 1:
            owners.pop(owner, None)
        else:
            owners[owner] = count - 1
        if not owners:
            self._held.pop(key, None)
