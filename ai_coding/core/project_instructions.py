"""Project-instruction loading (M3).

``ProjectInstructionsLoader`` reads the workspace's project-memory file
(``CLAUDE.md`` / ``AGENTS.md``), truncated so it can never blow the context budget.
"""

from __future__ import annotations

from pathlib import Path

from ai_coding.paths import ancestors_up_to_home

_TRUNCATION_NOTICE = "\n\n[project instructions truncated]"

#: Separator between levels. A blank line and a marker, so a reader can tell
#: where one file's conventions end and the next begins; without it the
#: boundaries are invisible and a cut mid-file reads as the file simply ending
#: there.
_SEPARATOR = "\n\n---\n\n"


class ProjectInstructionsLoader:
    """The instructions files that apply to a workspace, broad scope first.

    Every directory from the workspace root up to (but never including) the
    user's home directory contributes at most one file — ``CLAUDE.md`` beats
    ``AGENTS.md`` within a level, as it always did.

    Walking up was missing: the old loader read the workspace root alone, so a
    session started in ``/projects/sub`` silently lost ``/projects/CLAUDE.md``,
    with no error and no warning. The home-directory boundary is the same one
    config discovery uses, and is not negotiable in the other direction: unless
    it is there, a stray ``~/CLAUDE.md`` would inject itself into every project
    on the machine.
    """

    def __init__(
        self,
        root: str | Path,
        names: tuple[str, ...] = ("CLAUDE.md", "AGENTS.md"),
        max_bytes: int = 65_536,
    ) -> None:
        self._root = Path(root)
        self._names = names
        self._max_bytes = max_bytes

    def paths(self) -> list[Path]:
        """Every applicable file's path, broadest scope first."""
        found: list[Path] = []
        for directory in reversed(ancestors_up_to_home(self._root)):
            for name in self._names:
                candidate = directory / name
                if candidate.is_file():
                    found.append(candidate)
                    break
        return found

    def find_path(self) -> Path | None:
        """The nearest file — the one whose declarations are most specific.

        Kept because callers use it to show the operator *where* the rules came
        from; ``load`` is now the whole stack.
        """
        found = self.paths()
        return found[-1] if found else None

    def load(self) -> str:
        """All applicable instructions, ``max_bytes`` across the whole stack.

        The budget is **one** across all the levels, not one per file: each level
        getting its own 64 KiB is how four parents each fill it and the model
        receives four times the budget.
        """
        parts: list[str] = []
        remaining = self._max_bytes
        for path in self.paths():
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if not text.strip():
                continue
            # The separator costs bytes too, and counting it only when it is
            # used is how a bound like this one gets exceeded.
            if parts:
                remaining -= len(_SEPARATOR.encode("utf-8"))
            size = len(text.encode("utf-8"))
            if size > remaining:
                # The level is named: a policy silently cutting one file's
                # section mid-sentence reads as the file having ended there.
                parts.append(_truncate(text, remaining, f" (src/{path.parent.name})"))
                break
            parts.append(text)
            remaining -= size
        if not parts:
            return ""
        return _SEPARATOR.join(parts)


def _truncate(text: str, max_bytes: int, marker: str = "") -> str:
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    # Both the notice and the level it belongs to are inside `max_bytes`, so
    # the result really is bounded — appending them after the fact is how a
    # bounded budget produces a longer answer.
    room = max_bytes - len(_TRUNCATION_NOTICE.encode("utf-8")) - len(marker.encode("utf-8"))
    head = text.encode("utf-8")[:max(0, room)].decode("utf-8", errors="ignore")
    return head.rstrip() + _TRUNCATION_NOTICE + marker


__all__ = ["ProjectInstructionsLoader"]
