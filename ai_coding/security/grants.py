"""Where a standing "always" is written down (G4b-2).

In-memory grants died with the process, so the same file was re-approved on every
restart.  Persisting them raises the one question that decides the design: where.

Not in the workspace.  ``.ai-coding/`` is the obvious home -- it is already the
per-workspace runtime directory, already ignored -- but ``git add -f`` commits
ignored files, so a cloned repository could ship ``.ai-coding/grants.json``
pre-authorising ``edit:.git/hooks/pre-commit``.  A grant turns the prompt off, so
that attack is silent, and a hook runs on the next git command.  A file under the
user's own config directory cannot be shipped by a repository, and neither the
file tools (blocked by ``Sandbox``) nor bash (never grantable, so always asked)
can write there without the operator seeing it.

Everything here is best effort, matching the rest of the project's persistence:
an unreadable or hand-mangled file degrades to "no grants", never to a refusal
to start.

M-D3: a grant can carry a lifetime.  The document keeps the v1 shape --
``workspaces[ws]`` is still a list of strings -- and gains an optional ``meta``
block beside it, so the file a pre-TTL build wrote still reads and the old
reader simply ignores the new key (the project's migration rule: add optional
keys, never bump the version).  ``meta[ws][key]`` holds ``grantedAt`` (epoch
seconds, always) and ``ttl`` (seconds, omitted for a permanent grant).  A key
in ``workspaces[ws]`` without a usable ``meta`` record is permanent; a key only
in ``meta`` is ignored.  ``load`` reads and never writes; ``prune`` is the one
method that drops expired rows, and the write paths call it for free.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeGuard

from ai_coding.paths import config_home

__all__ = [
    "DEFAULT_GRANT_TTL",
    "GRANTS_VERSION",
    "GrantStore",
    "grants_path",
]

#: Bumped when the document gains a shape the current reader cannot express.
GRANTS_VERSION = 1

#: How long a standing grant stays in force, in seconds (24h).  ``None`` means a
#: grant is permanent -- the explicit "always" of the pre-TTL shape, which every
#: v1 file already is.  Read from ``security.grant_ttl_seconds`` at the
#: composition root; overridable here for the same reason the clock is.
DEFAULT_GRANT_TTL: float | None = 86_400.0


def grants_path() -> Path:
    """The one grants file, shared by every workspace and keyed by path."""
    return config_home() / "grants.json"


def _is_number(value: Any) -> TypeGuard[int | float]:
    """A JSON number, and not the booleans that are ``int`` in Python."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class GrantStore:
    """The persisted half of a gate's grants: per workspace, best effort."""

    def __init__(
        self,
        workspace: str | Path,
        path: Path | None = None,
        *,
        clock: Callable[[], float] = time.time,
        ttl: float | None = DEFAULT_GRANT_TTL,
    ) -> None:
        self.workspace = str(Path(workspace).expanduser().resolve())
        self.path = Path(path) if path is not None else grants_path()
        self._clock = clock
        self._ttl = ttl

    # ---- reading -----------------------------------------------------------

    def load(self) -> set[str]:
        """The grants recorded for this workspace; ``set()`` for anything wrong.

        Never raises: a corrupt file, a missing one and a file written by a
        future version all mean the same thing to the caller -- no grants.

        **Read-only.**  Expired rows are filtered here and left on disk until
        :meth:`prune` is called by a write path; a reader that rewrote the file
        on every call would turn every startup into a write.
        """
        return self.load_with_expiry()[0]

    def load_with_expiry(self) -> tuple[set[str], dict[str, float]]:
        """The live grants, and the expiry (epoch seconds) of the ones that have one.

        ``workspaces[ws]`` is the authoritative list: a key it does not hold is
        ignored however much ``meta`` says about it, and a key it holds without
        a usable ``meta`` record is permanent.  Only a record carrying both
        ``grantedAt`` and ``ttl`` yields an entry in the expiry map -- that map
        is what seeds the gate's ``_grant_expiry``, so a persistent grant simply
        has no deadline to check.
        """
        document = self._read()
        meta = self._meta_map(document)
        now = self._clock()
        grants: set[str] = set()
        expiry: dict[str, float] = {}
        for key in self._workspace_keys(document):
            record = meta.get(key)
            if not isinstance(record, dict):
                grants.add(key)
                continue
            granted_at = record.get("grantedAt")
            ttl = record.get("ttl")
            if not _is_number(granted_at) or not _is_number(ttl):
                grants.add(key)
                continue
            deadline = float(granted_at) + float(ttl)
            if deadline < now:
                continue
            grants.add(key)
            expiry[key] = deadline
        return grants, expiry

    # ---- writing -----------------------------------------------------------

    def add(self, key: str) -> None:
        """Record one grant.  A failed write leaves it in memory only.

        Re-approving an existing key refreshes its ``grantedAt`` (extending the
        TTL) rather than appending a second row: "always" again means the clock
        restarts.
        """
        text = str(key).strip()
        if not text:
            return
        document = self._read()
        workspaces = document.get("workspaces")
        if not isinstance(workspaces, dict):
            workspaces = {}
        keys = workspaces.get(self.workspace)
        if not isinstance(keys, list):
            keys = []
        if text not in keys:
            keys.append(text)
        workspaces[self.workspace] = keys
        meta = document.get("meta")
        if not isinstance(meta, dict):
            meta = {}
        ws_meta = meta.get(self.workspace)
        if not isinstance(ws_meta, dict):
            ws_meta = {}
        record: dict[str, Any] = {"grantedAt": self._clock()}
        if self._ttl is not None:
            record["ttl"] = float(self._ttl)
        ws_meta[text] = record
        meta[self.workspace] = ws_meta
        document["version"] = GRANTS_VERSION
        document["workspaces"] = workspaces
        document["meta"] = meta
        self._prune_document(document)
        self._write(document)

    def prune(self) -> None:
        """Drop this workspace's expired grants from the file.

        The write paths call it for free; ``load`` never does.  ``grants.json``
        is read and written without a lock (as it always was, best-effort
        semantics), so the worst case across writers is a lost prune.
        """
        document = self._read()
        if self._prune_document(document):
            self._write(document)

    def revoke(self, key: str) -> bool:
        """Forget one grant: its list entry *and* its ``meta`` record.

        ``key`` is exact -- a posture-suffixed key removes only that variant,
        and the gate expands a bare name to every variant before calling here.
        Returns whether anything was removed.
        """
        text = str(key)
        if not text:
            return False
        document = self._read()
        removed = False
        workspaces = document.get("workspaces")
        if isinstance(workspaces, dict):
            keys = workspaces.get(self.workspace)
            if isinstance(keys, list) and text in keys:
                workspaces[self.workspace] = [k for k in keys if k != text]
                removed = True
        meta = document.get("meta")
        if isinstance(meta, dict):
            ws_meta = meta.get(self.workspace)
            if isinstance(ws_meta, dict) and text in ws_meta:
                ws_meta.pop(text, None)
                if ws_meta:
                    meta[self.workspace] = ws_meta
                else:
                    meta.pop(self.workspace, None)
                document["meta"] = meta
                removed = True
        if not removed:
            return False
        self._prune_document(document)
        document["version"] = GRANTS_VERSION
        document["workspaces"] = workspaces if isinstance(workspaces, dict) else {}
        self._write(document)
        return True

    def clear(self) -> None:
        """Drop this workspace's grants from the file.

        Both halves -- the list *and* the ``meta`` block -- so a revoked
        workspace leaves no orphaned expiry record behind.  The gate's own set is
        the truth for this process; clearing it is the gate's job
        (``revoke_all``), because a store used for reading must not silently
        disagree with the policy object it was injected into.
        """
        document = self._read()
        changed = False
        workspaces = document.get("workspaces")
        if isinstance(workspaces, dict) and self.workspace in workspaces:
            del workspaces[self.workspace]
            changed = True
        meta = document.get("meta")
        if isinstance(meta, dict) and self.workspace in meta:
            del meta[self.workspace]
            document["meta"] = meta
            changed = True
        if not changed:
            return
        document["version"] = GRANTS_VERSION
        document["workspaces"] = workspaces if isinstance(workspaces, dict) else {}
        self._write(document)

    # ---- internals ---------------------------------------------------------

    def _workspace_keys(self, document: dict[str, Any]) -> set[str]:
        workspaces = document.get("workspaces")
        if not isinstance(workspaces, dict):
            return set()
        keys = workspaces.get(self.workspace)
        if not isinstance(keys, list):
            return set()
        return {str(key) for key in keys if isinstance(key, str)}

    def _meta_map(self, document: dict[str, Any]) -> dict[str, Any]:
        meta = document.get("meta")
        if not isinstance(meta, dict):
            return {}
        ws_meta = meta.get(self.workspace)
        return ws_meta if isinstance(ws_meta, dict) else {}

    def _prune_document(self, document: dict[str, Any]) -> bool:
        """Drop this workspace's expired rows from ``document`` in place.

        Returns whether anything changed, so the caller can skip a write.
        """
        workspaces = document.get("workspaces")
        if not isinstance(workspaces, dict):
            return False
        keys = workspaces.get(self.workspace)
        if not isinstance(keys, list):
            return False
        meta = document.get("meta")
        ws_meta = meta.get(self.workspace) if isinstance(meta, dict) else None
        ws_meta = ws_meta if isinstance(ws_meta, dict) else {}
        now = self._clock()
        kept: list[str] = []
        changed = False
        for key in keys:
            if not isinstance(key, str):
                changed = True
                continue
            record = ws_meta.get(key)
            if not isinstance(record, dict):
                kept.append(key)
                continue
            granted_at = record.get("grantedAt")
            ttl = record.get("ttl")
            if (
                _is_number(granted_at)
                and _is_number(ttl)
                and float(granted_at) + float(ttl) < now
            ):
                ws_meta.pop(key, None)
                changed = True
                continue
            kept.append(key)
        if not changed:
            return False
        workspaces[self.workspace] = kept
        if isinstance(meta, dict):
            if ws_meta:
                meta[self.workspace] = ws_meta
            else:
                meta.pop(self.workspace, None)
            document["meta"] = meta
        return True

    def _read(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            return {}
        try:
            document = json.loads(raw)
        except ValueError:
            return {}
        return document if isinstance(document, dict) else {}

    def _write(self, document: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass  # best effort: the grant still holds for this process
