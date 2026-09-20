"""System-prompt assembly (M3).

Design (m3-analysis §4.6): :class:`PromptAssembler` joins the base system prompt
with the optional project instructions, plan-mode segment, skills catalog, and
memory catalog into a single string handed to the AI engine each turn
(output of ``build`` is the ``instructions`` parameter of ``execute_turn``).
"""

from __future__ import annotations


class PromptAssembler:
    """Build the single system-prompt string from optional sections.

    Section order (master plan appendix A): base -> project instructions ->
    plan-mode segment -> skills catalog -> memory catalog.  Only non-empty sections
    are joined, separated by a blank line.
    """

    def __init__(self, base_instructions: str, plan_mode: bool = False) -> None:
        self._base = base_instructions
        self._plan_mode = plan_mode

    def build(
        self,
        project_instructions: str = "",
        skills_catalog: str = "",
        memory_catalog: str = "",
    ) -> str:
        sections: list[str] = [self._base.strip()]
        if project_instructions.strip():
            sections.append(
                "<system-reminder>Project instructions:\n"
                f"{project_instructions.strip()}\n</system-reminder>"
            )
        if self._plan_mode:
            sections.append(
                "<plan-mode>You are in plan mode: analyse and produce a step-by-step "
                "plan. Do not edit files or run side-effecting tools.</plan-mode>"
            )
        if skills_catalog.strip():
            sections.append(
                f"<skills>Available skills:\n{skills_catalog.strip()}</skills>"
            )
        if memory_catalog.strip():
            sections.append(
                f"<memory>Relevant prior notes:\n{memory_catalog.strip()}</memory>"
            )
        return "\n\n".join(s for s in sections if s)


__all__ = ["PromptAssembler"]
