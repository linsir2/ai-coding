"""Skill directory registry (M3).

Design (m3-analysis §4.7): scans a skills root for top-level directories that
contain a ``SKILL.md``, exposes their names and a catalog for the system prompt,
and can load a single skill's ``SKILL.md`` full text (parsing the frontmatter
``description`` for the catalog).  Independent from the M2 ``SkillTool``; an empty
directory yields no catalog (mirrors "empty dir does not register skill tool").
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

_SKILL_FILE = "SKILL.md"
_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_DESC = re.compile(r"(?im)^description\s*:\s*(.+)$")
_VERSION = re.compile(r"(?im)^version\s*:\s*(.+)$")
_STATUS = re.compile(r"(?im)^status\s*:\s*(.+)$")
_PHASE = re.compile(r"(?im)^phase\s*:\s*(.+)$")
_PATTERN_DNA = re.compile(r"(?im)^pattern_dna\s*:\s*(.+)$")
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
        """``- name: description`` lines with trigger hints; '' when empty.

        M4: includes trigger hints so the model knows when to load each skill.
        """
        lines: list[str] = []
        for name in self.names():
            desc = self.load(name)
            line = f"- {name}"
            if desc:
                summary = _parse_description(desc)
                if summary:
                    line += f": {summary}"
            # M4: add trigger hints
            triggers = self.triggers(name)
            if triggers:
                hint = _format_catalog_trigger_hint(triggers)
                if hint:
                    line += f"\n  Triggers: {hint}"
            # F2: add phase lock hint
            meta = self.meta(name)
            if meta.get("phase"):
                line += f"\n  [phase: {meta['phase']}]"
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
            "phase": _frontmatter_value(text, _PHASE) or "",
            "pattern_dna": _frontmatter_value(text, _PATTERN_DNA) or "",
        }

    # ---- write (M1) ---------------------------------------------------------

    def write(self, name: str, content: str) -> Path | None:
        """Atomically write ``skills/<name>/SKILL.md``.  Returns path or None.

        Refuses names that would escape the skill directory (``/``, ``..``).
        Uses ``os.replace`` so a crash mid-write leaves the old file intact
        instead of a truncated one (same contract as ``MemoryStore._save``).
        """
        if not name or "/" in name or ".." in name or len(name) > 80:
            return None
        target = self.root / name / _SKILL_FILE
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".tmp")
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, target)
            return target
        except OSError:
            return None

    # ---- triggers (M2) ------------------------------------------------------

    def triggers(self, name: str) -> dict[str, list[str]] | None:
        """Parse ``triggers`` from the skill's frontmatter.

        Returns ``{"keywords": [], "tools": [], "task_types": [], "failure_patterns": []}``
        when the triggers block is present, ``None`` when the file/frontmatter/triggers
        is missing or unparseable.  Uses ``yaml.safe_load`` (same dependency as
        ``config/loader.py``) because triggers is a map of arrays — regex parsing
        would be brittle for nested YAML.
        """
        text = self.load(name)
        if text is None:
            return None
        fm_match = _FRONTMATTER.search(text)
        if not fm_match:
            return None
        try:
            data = yaml.safe_load(fm_match.group(1))
        except yaml.YAMLError:
            # YAML parse failed (e.g., special chars like << in values).
            # Try regex fallback for triggers.
            return _parse_triggers_regex(fm_match.group(1))
        if not isinstance(data, dict):
            return None
        if "triggers" not in data:
            return None
        triggers = data["triggers"]
        if triggers is None:
            return {
                "keywords": [],
                "tools": [],
                "task_types": [],
                "failure_patterns": [],
            }
        if not isinstance(triggers, dict):
            return None
        return {
            "keywords": _as_str_list(triggers.get("keywords")),
            "tools": _as_str_list(triggers.get("tools")),
            "task_types": _as_str_list(triggers.get("task_types")),
            "failure_patterns": _as_str_list(triggers.get("failure_patterns")),
        }


def _as_str_list(value: object) -> list[str]:
    """Normalize a frontmatter value to ``list[str]``.

    YAML allows ``keywords: "single"`` (scalar) or ``keywords: [a, b]`` (sequence);
    this normalizes both to ``list[str]``.  ``None`` or non-list/non-str → ``[]``.
    """
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return []


def _parse_description(text: str) -> str | None:
    match = _FRONTMATTER.search(text)
    header = match.group(1) if match else ""
    if not header:
        return None
    desc = _DESC.search(header)
    if not desc:
        return None
    return desc.group(1).strip().strip(_YAML_SCALAR_STRIP) or None


def _format_catalog_trigger_hint(triggers: dict[str, list[str]]) -> str:
    """Format trigger hints for the catalog display."""
    parts: list[str] = []
    if triggers.get("keywords"):
        parts.append(f"keywords: {', '.join(triggers['keywords'][:3])}")
    if triggers.get("tools"):
        parts.append(f"tools: {', '.join(triggers['tools'][:3])}")
    if triggers.get("failure_patterns"):
        parts.append(f"on error: {', '.join(triggers['failure_patterns'][:2])}")
    return "; ".join(parts)


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


def _parse_triggers_regex(header: str) -> dict[str, list[str]]:
    """Fallback triggers parser when YAML fails (e.g., special chars in values).

    Uses regex to extract trigger arrays from frontmatter.
    """
    result: dict[str, list[str]] = {}
    for key in ("keywords", "tools", "task_types", "failure_patterns"):
        match = re.compile(rf"(?im)^{key}\s*:\s*\[(.+)\]").search(header)
        if match:
            items = [
                item.strip().strip("'\"") for item in match.group(1).split(",")
            ]
            result[key] = [item for item in items if item]
    return result


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
