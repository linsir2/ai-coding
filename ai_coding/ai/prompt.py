"""System-prompt assembly (M3).

Design (m3-analysis §4.6): :class:`PromptAssembler` joins the base system prompt
with the optional project instructions, the methods section, plan-mode segment,
skills catalog, and memory catalog into a single string handed to the AI engine
each turn (output of ``build`` is the ``instructions`` parameter of
``execute_turn``).
"""

from __future__ import annotations

#: The composition root's default system prompt.  A sub-agent in ``task_only`` mode
#: gets exactly this instead of its parent's assembled prompt (R7e-4), so the text
#: has one home instead of being hardcoded in cli.py and the runner.
BASE_INSTRUCTIONS = (
    "You are an AI coding assistant. Respond to the user's coding questions "
    "clearly and concisely, in the same language the user writes in."
)

#: 13 §2.3 (A): the tool-methodology section.  Both reference harnesses teach
#: "how to work" in the system prompt (dsh: per-tool sections; opencode:
#: system.txt); this is our first layer of it -- six lines, static, no knobs.
#: It rides every assembled prompt on purpose, sub-agents included: the tool
#: discipline is exactly as true for a child as it is for the parent.
METHODS_SECTION = (
    "<methods>\n"
    "- Read before write: never edit a file you have not read this turn.\n"
    "- Locate with search; read with line ranges instead of dumping whole files.\n"
    "- After running a command, check its exit code and output before claiming success.\n"
    "- Declare parallel-safe tool calls truthfully; independent reads may batch.\n"
    "- When blocked after two attempts, ask the user instead of repeating the same call.\n"
    "- Verify the change (run the relevant test/command) before reporting it done.\n"
    "</methods>"
)


class PromptAssembler:
    """Build the single system-prompt string from optional sections.

    Section order (13 §2.3): base -> project instructions -> methods ->
    plan-mode segment -> skills catalog.  Only non-empty sections are joined,
    separated by a blank line; ``methods`` is never empty.
    """

    def __init__(self, base_instructions: str, plan_mode: bool = False) -> None:
        self._base = base_instructions
        self._plan_mode = plan_mode

    def build(
        self,
        project_instructions: str = "",
        skills_catalog: str = "",
    ) -> str:
        sections: list[str] = [self._base.strip()]
        if project_instructions.strip():
            sections.append(
                "<system-reminder>Project instructions:\n"
                f"{project_instructions.strip()}\n</system-reminder>"
            )
        sections.append(METHODS_SECTION)
        if self._plan_mode:
            sections.append(
                "<plan-mode>You are in plan mode: analyse and produce a step-by-step "
                "plan. Do not edit files or run side-effecting tools.</plan-mode>"
            )
        if skills_catalog.strip():
            sections.append(
                f"<skills>Available skills:\n{skills_catalog.strip()}</skills>"
            )
        return "\n\n".join(s for s in sections if s)


__all__ = ["BASE_INSTRUCTIONS", "METHODS_SECTION", "PromptAssembler"]
