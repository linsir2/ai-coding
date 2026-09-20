"""skill tool — list and read skill definitions from a skills directory.

Skills are subdirectories under ``skill_dir`` that each contain a ``SKILL.md``
file.  The tool lets the model list available skills and read a specific
skill's full text.  Zero API calls — purely local file reads.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ai_coding.domain.message import ToolResult
from ai_coding.tools.base import BaseTool


class SkillTool(BaseTool):
    """List and read skill definitions from a local skills directory."""

    def __init__(self, skill_dir: str | Path) -> None:
        self._skill_dir = Path(skill_dir)

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
        """Return all skill names found under the skills directory."""
        if not self._skill_dir.is_dir():
            return []
        names: list[str] = []
        for entry in sorted(self._skill_dir.iterdir()):
            if entry.is_dir() and (entry / "SKILL.md").is_file():
                names.append(entry.name)
        return names

    async def execute(self, params: dict[str, Any]) -> ToolResult:
        action = str(params.get("action", "")).lower()

        if action == "list":
            names = self.list_skills()
            if not names:
                return ToolResult(success=True, output="(no skills available)")
            lines = [f"  - {n}" for n in names]
            return ToolResult(
                success=True,
                output="Available skills:\n" + "\n".join(lines),
            )

        if action == "read":
            name = str(params.get("name", "")).strip()
            if not name:
                return ToolResult(success=False, output="error: 'name' is required for 'read'")
            # Sanitize: no path separators allowed
            if "/" in name or "\\" in name or ".." in name:
                return ToolResult(success=False, output="error: invalid skill name")
            skill_path = self._skill_dir / name / "SKILL.md"
            if not skill_path.is_file():
                return ToolResult(
                    success=False,
                    output=f"skill '{name}' not found",
                )
            try:
                content = skill_path.read_text(encoding="utf-8")
            except OSError as exc:
                return ToolResult(success=False, output=f"read error: {exc}")
            return ToolResult(success=True, output=f"# Skill: {name}\n\n{content}")

        return ToolResult(
            success=False,
            output=f"error: unknown action '{action}' (valid: list, read)",
        )


__all__ = ["SkillTool"]
