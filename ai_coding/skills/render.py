"""What a loaded skill looks like to the model — one renderer, two callers.

The skill tool's ``read`` and the @mention injection must hand the model the
same shape; two formatters would drift the day one of them learned about the
base directory or the file list first. Format mirrors OpenCode's
``Skill.prepare``/``toModelOutput`` (packages/core/src/skill.ts:36-65): body,
then where relative paths resolve from, then a sampled file listing.
"""

from __future__ import annotations

from ai_coding.skills.registry import DEFAULT_VERSION, Skill

#: Files sampled into the listing.  OpenCode's scanner uses the same cap
#: (skill.ts:57-63): enough to reveal a scripts/ or reference/ layout, small
#: enough that a skill with a media folder does not spend a request on it.
MAX_SKILL_FILES = 10


def skill_suffix(skill: Skill) -> str:
    """The read/list marker: only what differs from the default is shown.

    A v1 active skill keeps its exact pre-R11c text, so an unversioned
    directory of skills does not suddenly sprout noise.
    """
    out = ""
    if skill.version != DEFAULT_VERSION:
        out += f" (v{skill.version})"
    if skill.is_archived:
        out += " [archived]"
    if not skill.is_healthy:
        out += " [broken]"
    return out


def skill_files(skill: Skill, *, limit: int = MAX_SKILL_FILES) -> list[str]:
    """Absolute paths beside ``SKILL.md``, sorted and capped.

    Recursive and dot-inclusive like OpenCode's scan; ``SKILL.md`` itself is
    excluded because the model already has its body. Best effort: a directory
    that cannot be walked lists what it can.
    """
    root = skill.path.parent
    try:
        found = sorted(
            str(path)
            for path in root.rglob("*")
            if path.is_file() and path.name != "SKILL.md"
        )
    except OSError:
        return []
    return found[:limit]


def render_skill_content(skill: Skill, *, limit: int = MAX_SKILL_FILES) -> str:
    """The full ``<skill_content>`` block: what loading a skill means."""
    files = skill_files(skill, limit=limit)
    return "\n".join(
        [
            f'<skill_content name="{skill.id}">',
            f"# Skill: {skill.id}{skill_suffix(skill)}",
            "",
            skill.body,
            "",
            f"Base directory for this skill: {skill.path.parent}",
            "Relative paths in this skill (e.g., scripts/, reference/) are relative "
            "to this base directory.",
            "Note: file list is sampled.",
            "",
            "<skill_files>",
            *(f"<file>{path}</file>" for path in files),
            "</skill_files>",
            "</skill_content>",
        ]
    )


__all__ = ["MAX_SKILL_FILES", "render_skill_content", "skill_files", "skill_suffix"]
