"""Skill directory registry (M3).

Design (m3-analysis §4.7): scans a skills root for top-level directories that
contain a ``SKILL.md``, exposes their names and a catalog for the system prompt,
and can load a single skill's ``SKILL.md`` full text (parsing the frontmatter
``description`` for the catalog).  Independent from the M2 ``SkillTool``; an empty
directory yields no catalog (mirrors "empty dir does not register skill tool").
"""

from __future__ import annotations

import re
from pathlib import Path

_SKILL_FILE = "SKILL.md"
_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_DESC = re.compile(r"(?im)^description\s*:\s*(.+)$")
_VERSION = re.compile(r"(?im)^version\s*:\s*(.+)$")
_STATUS = re.compile(r"(?im)^status\s*:\s*(.+)$")
_YAML_SCALAR_STRIP = '"\''
#: The only two states a skill may declare.  Anything else reads as `active`
#: rather than inventing a third state nobody handles downstream.
SKILL_STATUSES = ("active", "archived")
DEFAULT_VERSION = "1"


class SkillRegistry:
    """Read-only view over a skills directory tree."""

    def __init__(self, skill_dir: str | Path = Path("skills")) -> None:
        self.root = Path(skill_dir)

    # ---- discovery --------------------------------------------------------

    def names(self) -> list[str]:
        """Top-level directories that contain a ``SKILL.md``, sorted."""
        if not self.root.is_dir():
            return []
        result: list[str] = []
        for child in sorted(self.root.iterdir()):
            if child.is_dir() and (child / _SKILL_FILE).is_file():
                result.append(child.name)
        return result

    @property
    def is_empty(self) -> bool:
        return not self.names()

    # ---- catalog ----------------------------------------------------------

    def catalog_text(self) -> str:
        """``- name: description`` lines for the prompt; '' when empty."""
        lines: list[str] = []
        for name in self.names():
            desc = self.load(name)
            line = f"- {name}"
            if desc:
                summary = _parse_description(desc)
                if summary:
                    line += f": {summary}"
            lines.append(line)
        return "\n".join(lines) if lines else ""

    # ---- load -------------------------------------------------------------

    def load(self, name: str) -> str | None:
        """SKILL.md full text for ``name``, or None when absent."""
        target = self.root / name / _SKILL_FILE
        if not target.is_file():
            return None
        try:
            return target.read_text(encoding="utf-8")
        except OSError:
            return None

    # ---- metadata (R11c) ---------------------------------------------------

    def meta(self, name: str) -> dict[str, str | None]:
        """``{name, version, status, description}`` read from the frontmatter.

        A missing file reports the *defaults* rather than a version we made up:
        "there is no version here" and "the version is 1" are different claims,
        and only the first is true for a skill that does not exist.  Callers that
        need to tell them apart check ``description is None``.

        The catalog is untouched by this (``catalog_text`` feeds the system
        prompt, which has to stay byte-stable within a session).
        """
        text = self.load(name)
        if text is None:
            return {"name": name, "version": DEFAULT_VERSION, "status": "active",
                    "description": None}
        return {
            "name": name,
            "version": _parse_version(text),
            "status": _parse_status(text),
            "description": _parse_description(text),
        }


def _parse_description(text: str) -> str | None:
    match = _FRONTMATTER.search(text)
    header = match.group(1) if match else ""
    if not header:
        return None
    desc = _DESC.search(header)
    if not desc:
        return None
    return desc.group(1).strip().strip(_YAML_SCALAR_STRIP) or None


def _frontmatter_value(text: str, pattern: re.Pattern[str]) -> str | None:
    """One scalar out of the frontmatter block, or None when it is not there."""
    match = _FRONTMATTER.search(text)
    header = match.group(1) if match else ""
    if not header:
        return None
    found = pattern.search(header)
    if not found:
        return None
    # Only the first line of a value counts: YAML would allow continuations, and
    # reading them wrong is worse than not reading them at all.
    return found.group(1).splitlines()[0].strip().strip(_YAML_SCALAR_STRIP) or None


def _parse_version(text: str) -> str:
    """The declared version, or ``"1"``.

    A **string**, deliberately: there is no semver comparison here, only
    display and a "is this the default?" test, so ``1.10`` must not be able to
    sort below ``1.9`` later.
    """
    return _frontmatter_value(text, _VERSION) or DEFAULT_VERSION


def _parse_status(text: str) -> str:
    """``active`` or ``archived``; anything unrecognised reads as ``active``."""
    declared = (_frontmatter_value(text, _STATUS) or "").casefold()
    return declared if declared in SKILL_STATUSES else "active"


__all__ = [
    "DEFAULT_VERSION",
    "SKILL_STATUSES",
    "SkillRegistry",
    "_parse_description",
    "_parse_status",
    "_parse_version",
]
