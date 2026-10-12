"""Installing, listing and removing skills.

A skill is not a file the agent reads once. It is an instruction the model will
follow from then on, with the same weight as anything in the system prompt, so
installing one is a supply-chain decision rather than a file copy. The rules
below come from that:

* nothing is fetched without the caller saying where it came from;
* a name is never overwritten without ``force`` — a skill you wrote and one
  another project wrote can collide, and the collision is not the installer��s
  to resolve;
* a symlink is refused, because a repository can carry
  ``skills/x/SKILL.md -> /etc/shadow`` and nothing in the listing looks wrong;
* a skill that does not parse, or has no description, is refused. The
  description is the only thing the model has when deciding whether to load a
  skill, so one without it is invisible in the catalog and cannot be chosen.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ai_coding.skills.registry import Skill, parse_skill

_SKILL_FILE = "SKILL.md"


class InstallError(RuntimeError):
    """A refused install. The message is what the operator reads."""


@dataclass
class InstallResult:
    installed: list[str] = field(default_factory=list)
    replaced: list[str] = field(default_factory=list)
    source: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.installed or self.replaced)


def _inspect(path: Path) -> Skill:
    """Parse and check one candidate, or raise :class:`InstallError`."""
    if path.is_symlink() or (path / _SKILL_FILE).is_symlink():
        raise InstallError(f"{path}: refusing a symlink; a real skill is a real file")
    target = path / _SKILL_FILE
    if not target.is_file():
        raise InstallError(f"{path}: no SKILL.md")
    skill = parse_skill(target)
    if not skill.is_healthy:
        raise InstallError(f"{path}: cannot parse its frontmatter ({skill.error})")
    if not skill.description:
        raise InstallError(
            f"{path}: no description. The description is the only thing the model "
            f"has when deciding whether to load a skill."
        )
    return skill


#: Where a repository puts its skills. Checked in this order so a directory
#: that is *itself* a skill still works, and so the common layouts need no
#: special-casing by the caller.
SKILL_CONTAINERS = ("skills", ".claude/skills", ".agents/skills")


def _skill_dirs(directory: Path) -> list[Path]:
    return [
        child
        for child in sorted(directory.iterdir())
        if child.is_dir()
        and not child.name.startswith(".")
        and (child / _SKILL_FILE).is_file()
    ]


def _candidates(source: Path) -> list[Path]:
    """``[source]`` if it is one skill, else the skills it holds.

    A directory that *is* a skill wins over one that contains skills, so
    ``skill install ./my-skill`` cannot be captured by a stray ``my-skill/skills``
    subdirectory. The repository layouts are then tried, which is what makes
    ``skill install ./some-repo`` and ``skill install owner/repo`` do the same
    thing.
    """
    if (source / _SKILL_FILE).is_file():
        return [source]
    found = _skill_dirs(source)
    if found:
        return found
    for container in SKILL_CONTAINERS:
        nested = source / container
        if nested.is_dir():
            inner = _skill_dirs(nested)
            if inner:
                return inner
    raise InstallError(
        f"{source}: no SKILL.md here, directly inside it, or under "
        f"{' / '.join(SKILL_CONTAINERS)}"
    )


def install_from_path(
    source: str | Path,
    library: str | Path,
    *,
    force: bool = False,
) -> InstallResult:
    """Copy one skill, or every skill under a directory, into ``library``."""
    source_path = Path(source).expanduser().resolve()
    library_path = Path(library).expanduser()
    if not source_path.is_dir():
        raise InstallError(f"{source_path}: not a directory")

    result = InstallResult(source=str(source_path))
    planned: list[tuple[Path, str]] = []
    for candidate in _candidates(source_path):
        skill = _inspect(candidate)
        name = skill.id
        if "/" in name or ".." in name:
            raise InstallError(f"{candidate}: refusing the name {name!r}")
        existing = library_path / name
        if existing.exists():
            if not force:
                raise InstallError(
                    f"{existing} already exists; pass --force to replace it"
                )
            result.replaced.append(name)
        else:
            result.installed.append(name)
        planned.append((candidate, name))

    for candidate, name in planned:
        target = library_path / name
        staging = library_path / f".{name}.incoming"
        if staging.exists():
            shutil.rmtree(staging)
        shutil.copytree(candidate, staging, symlinks=False)
        if target.exists():
            shutil.rmtree(target)
        staging.rename(target)
    return result


# ---- git -------------------------------------------------------------------


def git_source_url(source: str) -> str:
    """``owner/repo`` becomes a GitHub URL; anything with a scheme is used as
    given.

    The bare form exists because it is how people actually name a repository,
    and guessing wrong is visible immediately (it clones something, or nothing).
    """
    if "://" in source or source.startswith("git@"):
        return source
    if source.count("/") == 1 and not source.startswith("/"):
        return f"https://github.com/{source}.git"
    raise InstallError(f"{source}: not a repository reference (use owner/repo or a URL)")


async def install_from_git(
    source: str,
    library: str | Path,
    *,
    force: bool = False,
    timeout: float = 60.0,
) -> InstallResult:
    """Shallow-clone ``source`` and install the skills it carries.

    Cloned into a temporary directory and installed from there, so the remote
    never gets to choose a destination. The clone is depth 1 and bounded: an
    unbounded ``git clone`` is a way to make the user wait without knowing why.
    """
    import tempfile

    from ai_coding.infra.subprocess_runner import run_process_async

    url = git_source_url(source)
    with tempfile.TemporaryDirectory(prefix="ai-coding-skill-") as scratch:
        outcome = await run_process_async(
            ("git", "clone", "--depth", "1", "--quiet", url, scratch),
            timeout=timeout,
        )
        if outcome.timed_out:
            raise InstallError(f"git clone timed out after {timeout:g}s")
        if outcome.error is not None or (outcome.returncode or 0) != 0:
            detail = (outcome.stderr or outcome.error or "").strip()[:200]
            raise InstallError(f"git clone failed: {detail}")
        for folder in SKILL_CONTAINERS:
            candidate = Path(scratch) / folder
            if candidate.is_dir() and any(candidate.iterdir()):
                result = install_from_path(candidate, library, force=force)
                result.source = url
                return result
        raise InstallError(f"{url}: no {' / '.join(SKILL_CONTAINERS)} directory in it")


# ---- listing and removing --------------------------------------------------


def list_installed(library: str | Path) -> list[dict[str, str]]:
    """One row per installed skill. Dot-directories are not skills."""
    root = Path(library).expanduser()
    if not root.is_dir():
        return []
    rows: list[dict[str, str]] = []
    for child in sorted(root.iterdir()):
        if child.name.startswith(".") or not child.is_dir():
            continue
        if not (child / _SKILL_FILE).is_file():
            continue
        skill = parse_skill(child / _SKILL_FILE)
        rows.append(
            {
                "name": child.name,
                "description": skill.description,
                "status": "broken" if not skill.is_healthy else skill.status,
            }
        )
    return rows


def remove_skill(library: str | Path, name: str) -> bool:
    """Delete a skill directory. ``False`` when there was nothing to delete."""
    if "/" in name or ".." in name or not name or name.startswith("."):
        return False
    target = Path(library).expanduser() / name
    if not target.is_dir():
        return False
    shutil.rmtree(target)
    return True


__all__ = [
    "InstallError",
    "InstallResult",
    "git_source_url",
    "install_from_git",
    "install_from_path",
    "list_installed",
    "remove_skill",
]
