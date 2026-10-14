"""What files a workspace holds -- one answer, one place (r18/F15).

Two callers need this and neither should be able to drift from the other:
grep's Python fallback walks the tree, and ``files.find`` lists it for the
composer's ``@`` suggestions.  They used to carry byte-identical copies of the
ignore list and two ``rglob`` walks, which cannot prune -- ``.git`` and
``node_modules`` were descended into in full and then discarded one path at a
time, so the enumeration budget was spent on paths nobody could pick.

``walk_workspace`` prunes instead: an ignored directory is never entered.  Git's
own rules come from ``pathspec`` (gitwildmatch), applied per directory exactly
as git does -- the nearest ``.gitignore`` wins -- and applied unconditionally,
where ripgrep needs a repository before it looks at them.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

import pathspec

#: Directories a search or a listing never enters, vendored trees included.
#: One definition: grep's fallback, rg's ``--glob`` arguments and ``files.find``
#: all read this.
SKIP_DIRS = frozenset(
    {".git", ".venv", "venv", "node_modules", "__pycache__", ".ai-coding"}
)

#: Ceiling on what one walk will hand back.  A caller that needs more should
#: narrow the root rather than raise this.
MAX_ENTRIES = 20_000

_IGNORE_FILE = ".gitignore"


def _specs_for(directory: Path) -> list[pathspec.PathSpec[pathspec.Pattern]]:
    """The ignore rules that apply in ``directory`` (empty when it has none)."""
    ignore_file = directory / _IGNORE_FILE
    try:
        lines = ignore_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    # "gitignore" rather than the older "gitwildmatch" spelling, which pathspec
    # deprecated in 1.x -- both are git's semantics, only one is still current.
    if not lines:
        return []
    return [pathspec.PathSpec.from_lines("gitignore", lines)]


def walk_workspace(
    root: str | Path,
    *,
    respect_gitignore: bool = True,
    max_entries: int = MAX_ENTRIES,
) -> Iterator[tuple[PurePosixPath, bool]]:
    """Yield ``(relative posix path, is_dir)`` for everything under ``root``.

    Relative to ``root`` itself, so callers can render it without re-deriving.
    Directories are yielded before their contents.  Skipped directories are
    pruned, not filtered, and ``.gitignore`` rules stack the way git stacks
    them: a nested file adds to the ones above it for its own subtree.

    Breadth-first, so that a caller with a fixed number of entries to hand out
    spends them from the top of the tree downwards rather than descending into
    whichever subtree was pushed last (r20/F3).
    """
    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        return
    pending: list[tuple[Path, list[pathspec.PathSpec[pathspec.Pattern]]]] = [(base, [])]
    seen = 0
    while pending:
        directory, inherited = pending.pop(0)
        specs = list(inherited)
        if respect_gitignore:
            specs += _specs_for(directory)
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            if entry.name in SKIP_DIRS:
                continue  # pruned: never opened
            here = Path(entry.path)
            rel = PurePosixPath(here.relative_to(base).as_posix())
            is_dir = entry.is_dir(follow_symlinks=False)
            if respect_gitignore and specs:
                # A gitignore rule about a directory is written with a trailing
                # slash (``build/``) and pathspec only matches it when the probe
                # carries one -- and only when it is a str: match_file
                # normalises with os.sep, which a PurePosixPath has already
                # stripped.  Checking files alone left the ignored directory
                # itself offered for completion -- with nothing inside it.
                probe = rel.as_posix() + "/" if is_dir else rel.as_posix()
                if any(spec.match_file(probe) for spec in specs):
                    continue  # pruned: never opened
            seen += 1
            if seen > max_entries:
                return
            yield rel, is_dir
            if is_dir:
                pending.append((here, specs))


__all__ = ["MAX_ENTRIES", "SKIP_DIRS", "walk_workspace"]
