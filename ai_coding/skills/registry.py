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
_YAML_SCALAR_STRIP = '"\''


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


def _parse_description(text: str) -> str | None:
    match = _FRONTMATTER.search(text)
    header = match.group(1) if match else ""
    if not header:
        return None
    desc = _DESC.search(header)
    if not desc:
        return None
    return desc.group(1).strip().strip(_YAML_SCALAR_STRIP) or None


__all__ = ["SkillRegistry", "_parse_description"]
