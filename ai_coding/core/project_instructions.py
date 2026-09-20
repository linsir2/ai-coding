"""Project-instruction loading (M3).

``ProjectInstructionsLoader`` reads the workspace's project-memory file
(``CLAUDE.md`` / ``AGENTS.md``), truncated so it can never blow the context budget.
"""

from __future__ import annotations

from pathlib import Path

_TRUNCATION_NOTICE = "\n\n[project instructions truncated]"


class ProjectInstructionsLoader:
    """Load the highest-priority project instructions file under a workspace root."""

    def __init__(
        self,
        root: str | Path,
        names: tuple[str, ...] = ("CLAUDE.md", "AGENTS.md"),
        max_bytes: int = 65_536,
    ) -> None:
        self._root = Path(root)
        self._names = names
        self._max_bytes = max_bytes

    def find_path(self) -> Path | None:
        """Return the first existing instructions file (highest priority first)."""
        for name in self._names:
            candidate = self._root / name
            if candidate.is_file():
                return candidate
        return None

    def load(self) -> str:
        """Return the instructions text, truncated to ``max_bytes``; '' when absent."""
        path = self.find_path()
        if path is None:
            return ""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return ""
        return _truncate(text, self._max_bytes)


def _truncate(text: str, max_bytes: int) -> str:
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    head = text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
    return head.rstrip() + _TRUNCATION_NOTICE


__all__ = ["ProjectInstructionsLoader"]
