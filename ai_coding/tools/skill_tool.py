"""skill tool — list and read skills from the shared registry.

The tool takes a :class:`~ai_coding.skills.registry.SkillRegistry`, not a
directory. It used to take a path and do its own directory walk and its own
frontmatter parsing, which meant two answers to "what skills exist" and a real
split-brain: ``cli.py`` built the registry from ``<workspace>/skills``
unconditionally while ``tools/builder.py`` registered this tool only when
``--skills`` was passed — so a plain ``ai-coding -w .`` advertised skills in the
system prompt that the model had no tool to load. One registry makes that
impossible to express.

Loading is by the name the catalog prints. A ``name:`` in the frontmatter that
differs from the directory name is accepted as an alias, so a skill copied from
Claude Code or Codex still loads under either spelling — but it is never a
second identity.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ai_coding.domain.message import ToolResult
from ai_coding.skills.registry import SkillRegistry
from ai_coding.skills.render import render_skill_content, skill_suffix
from ai_coding.tools.base import BaseTool, ToolContext


class SkillTool(BaseTool):
    """List and read skills. Zero API calls — purely local file reads."""

    def __init__(self, registry: SkillRegistry) -> None:
        self._registry = registry
        self._on_loaded: Callable[[str], None] | None = None

    def set_on_loaded(self, callback: Callable[[str], None]) -> None:
        """Set callback invoked when a skill is successfully loaded."""
        self._on_loaded = callback

    @property
    def name(self) -> str:
        return "skill"

    @property
    def description(self) -> str:
        return (
            "List available skills or read a specific skill's full instructions. "
            "Use 'list' to see what skills are available, then 'read' <name> to "
            "load a skill's content. Skills are local markdown files — no API calls."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "read"],
                    "description": "List skills or read one skill's content.",
                },
                "name": {
                    "type": "string",
                    "description": "Name of the skill to read (required for 'read').",
                },
            },
            "required": ["action"],
        }

    def list_skills(self) -> list[str]:
        """Every skill id the registry finds, broken ones included."""
        return self._registry.names()

    # Read-only: safe to run alongside sibling calls in the same message.
    parallel_safe = True
    mutates_world = False

    async def execute(
        self, params: dict[str, Any], ctx: ToolContext | None = None
    ) -> ToolResult:
        action = str(params.get("action", "")).lower()

        if action == "list":
            names = self.list_skills()
            if not names:
                return ToolResult(success=True, output="(no skills available)")
            lines = [
                f"  - {name}{skill_suffix(skill)}"
                for name in names
                for skill in (self._registry.get(name),)
                if skill is not None
            ]
            return ToolResult(
                success=True,
                output="Available skills:\n" + "\n".join(lines),
            )

        if action == "read":
            name = str(params.get("name", "")).strip()
            if not name:
                return ToolResult(
                    success=False, output="error: 'name' is required for 'read'"
                )
            if "/" in name or "\\" in name or ".." in name:
                return ToolResult(success=False, output="error: invalid skill name")
            skill = self._registry.resolve(name)
            if skill is None:
                return ToolResult(success=False, output=f"skill '{name}' not found")
            if self._on_loaded:
                self._on_loaded(skill.id)
            if not skill.is_healthy:
                # Reported rather than returned as instructions: handing a model
                # half-parsed frontmatter and calling it a skill is worse than
                # saying the file is broken, because the failure is then
                # indistinguishable from the skill being unhelpful.
                return ToolResult(
                    success=False,
                    output=f"skill '{skill.id}' 无法解析 frontmatter: {skill.error}",
                    data={"name": skill.id, "version": skill.version, "status": skill.status},
                )
            # An archived skill still reads: refusing would be a policy, and this
            # tool has no gate. It only says what it is — it does not recommend a
            # replacement, because we cannot know one.
            return ToolResult(
                success=True,
                output=render_skill_content(skill),
                data={
                    "name": skill.id,
                    "version": skill.version,
                    "status": skill.status,
                },
            )

        return ToolResult(
            success=False,
            output=f"error: unknown action '{action}' (valid: list, read)",
        )


__all__ = ["SkillTool"]
