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
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ai_coding.paths import config_home
from typing import Any

__all__ = ["GrantStore", "grants_path", "GRANTS_VERSION"]

#: Bumped when the document gains a shape the current reader cannot express.
GRANTS_VERSION = 1


def grants_path() -> Path:
    """The one grants file, shared by every workspace and keyed by path."""
    return config_home() / "grants.json"


class GrantStore:
    """The persisted half of a gate's grants: per workspace, best effort."""

    def __init__(self, workspace: str | Path, path: Path | None = None) -> None:
        self.workspace = str(Path(workspace).expanduser().resolve())
        self.path = Path(path) if path is not None else grants_path()

    # ---- reading -----------------------------------------------------------

    def load(self) -> set[str]:
        """The grants recorded for this workspace; ``set()`` for anything wrong.

        Never raises: a corrupt file, a missing one and a file written by a
        future version all mean the same thing to the caller -- no grants.
        """
        document = self._read()
        workspaces = document.get("workspaces")
        if not isinstance(workspaces, dict):
            return set()
        keys = workspaces.get(self.workspace)
        if not isinstance(keys, list):
            return set()
        return {str(key) for key in keys if isinstance(key, str)}

    # ---- writing -----------------------------------------------------------

    def add(self, key: str) -> None:
        """Record one grant.  A failed write leaves it in memory only."""
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
        document["version"] = GRANTS_VERSION
        document["workspaces"] = workspaces
        self._write(document)

    def clear(self) -> None:
        """Drop this workspace's grants from the file.

        The gate's own set is the truth for this process; clearing it is the
        gate's job (``revoke_all``), because a store used for reading must not
        silently disagree with the policy object it was injected into.
        """
        document = self._read()
        workspaces = document.get("workspaces")
        if not isinstance(workspaces, dict) or self.workspace not in workspaces:
            return
        del workspaces[self.workspace]
        document["version"] = GRANTS_VERSION
        document["workspaces"] = workspaces
        self._write(document)

    # ---- internals ---------------------------------------------------------

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

