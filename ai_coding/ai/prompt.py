"""System-prompt assembly (M3).

Design (m3-analysis §4.6): :class:`PromptAssembler` joins the base system prompt
with the optional project instructions, the methods section, plan-mode segment,
and skills catalog into a single string handed to the AI engine each turn
(output of ``build`` is the ``instructions`` parameter of ``execute_turn``).  The
memory block is not a section here: it rides the volatile tail
(``core/request_assembly.py``), like every other per-iteration block.
"""

from __future__ import annotations

#: The composition root's default system prompt.  A sub-agent in ``task_only`` mode
#: gets exactly this instead of its parent's assembled prompt (R7e-4), so the text
#: has one home instead of being hardcoded in cli.py and the runner.
BASE_INSTRUCTIONS = (
    "You are an AI coding assistant. Respond to the user's coding questions "
    "clearly and concisely, in the same language the user writes in."
)

#: C-2 (13 §2.3 A): the tool-methodology section.
#:
#: Both reference harnesses teach "how to work" in the system prompt and both
#: prune it by the tool surface: dsh has every tool register its own section
#: and return "" when the tool is not visible
#: (``packages/fs/tool-fs/src/read.ts:69-75``), opencode renders
#: ``${OPENCODE_TOOL_GUIDANCE}`` from the request's own tool names
#: (``session/system-prompt.ts:9-28``).  dsh also states the ownership rule this
#: table follows -- *"Per-tool semantics and selection guidance live in tool
#: descriptions. Prompt sections carry only cross-call habits"* -- so nothing
#: here repeats a schema description.
#:
#: The table is the single source: ``METHODS_SECTION`` is what it renders with
#: no surface given, and a surface only chooses which rows apply.  The rows are
#: ~2x the six static lines this replaced, which is the size both harnesses sit
#: at for the same cluster (dsh ~1000 chars, opencode 737); test_prompt.py:139
#: keeps a ceiling on it.
#:
#: It rides every assembled prompt on purpose, sub-agents included: the tool
#: discipline is as true for a child as for its parent.  A child does get less
#: -- its stack holds no subAgent, so that row is not in its prompt.

#: Habits that belong to no single tool: they survive any surface.
GENERAL_METHODS: tuple[str, ...] = (
    "Read before write: never edit a file you have not read this turn.",
    "Declare parallel-safe tool calls truthfully; independent reads may batch.",
    "When blocked after two attempts, ask the user instead of repeating the same call.",
    "Verify the change (run the relevant test/command) before reporting it done.",
)

#: Cross-call habits per tool, keyed by tool name. A tool absent from the
#: request's surface takes its rows with it.
TOOL_GUIDANCE: dict[str, tuple[str, ...]] = {
    "bash": (
        "After a command, read its exit status and output before you act on it "
        "or claim success.",
    ),
    "read": (
        "Use read — not shell commands like cat, sed, head or tail — to inspect "
        "text files.",
        "Read a line range with offset/limit instead of dumping a whole file.",
    ),
    "glob": ("Use glob — not shell find — to discover files by path pattern.",),
    "grep": ("Use grep — not shell grep or rg — to search file contents.",),
    "write": ("Write replaces a whole file; prefer edit for a targeted change.",),
    "subAgent": (
        "While a background sub-agent runs, keep working on independent steps; "
        "do not sleep or poll for it — its result arrives on its own.",
    ),
}


def _render_methods(tools: tuple[str, ...] | None) -> str:
    """The methods section for a tool surface.

    ``None`` means the caller does not know its surface, so nothing is pruned --
    that is the whole point of the default (a sub-agent and every defensive path
    must keep the full text).  An empty tuple is a real answer: a surface with
    no tools in it keeps the general discipline and nothing else.
    """
    rows = list(GENERAL_METHODS)
    if tools is None:
        for guidance in TOOL_GUIDANCE.values():
            rows.extend(guidance)
    else:
        for name in tools:
            rows.extend(TOOL_GUIDANCE.get(name, ()))
    return "<methods>\n" + "".join(f"- {row}\n" for row in rows) + "</methods>"


#: The unpruned text, for callers that do not know their surface (and for the
#: byte-exactness tests). Derived from the table above, never hand-written.
METHODS_SECTION = _render_methods(None)


class PromptAssembler:
    """Build the single system-prompt string from optional sections.

    Section order (13 §2.3): base -> project instructions -> methods ->
    plan-mode segment -> skills catalog.  M1b adds the agent's own
    instructions between the project's and the methods: who is speaking is
    more specific than the project's conventions, and the methods are
    general guidance.  Only non-empty sections are joined, separated by a
    blank line; ``methods`` is never empty.
    """

    def __init__(self, base_instructions: str) -> None:
        self._base = base_instructions

    def build(
        self,
        project_instructions: str = "",
        skills_catalog: str = "",
        #: M1b: the current agent's own instructions.  Empty (the default,
        #: and the case for build/plan/general today) produces no section.
        agent_instructions: str = "",
        #: 18 §10.5 (P2): the session's prompt posture, "normal" | "plan".
        #: Unknown values read as normal (fail-safe, same rule as persistence).
        mode: str = "normal",
        #: C-2: the tool names this request actually carries. ``None`` (the
        #: default) prunes nothing -- a caller that does not know its surface
        #: must not silently lose discipline. A tuple prunes the rows of every
        #: tool that is not in it.
        tools: tuple[str, ...] | None = None,
    ) -> str:
        sections: list[str] = [self._base.strip()]
        if project_instructions.strip():
            sections.append(
                "<system-reminder>Project instructions:\n"
                f"{project_instructions.strip()}\n</system-reminder>"
            )
        if agent_instructions.strip():
            sections.append(
                "<system-reminder>Agent instructions:\n"
                f"{agent_instructions.strip()}\n</system-reminder>"
            )
        sections.append(_render_methods(tools))
        if mode == "plan":
            sections.append(
                # The discriminator comes first, the two exits after it.  The
                # old order said "every request is answered with a plan" and
                # left the research exit as a late exception, so a survey
                # request came back with a plan -- which then hit the
                # workspace gate and sent the turn into a random retry loop
                # (measured, production session 135cc71c).  The model has to
                # learn *which* exit the request wants before it learns what
                # the exits are.
                "<plan-mode>You are in plan mode. Decide first which exit the "
                "request wants: a request that asks for changes (implement, "
                "modify, add, fix, refactor) needs a plan, and a request that "
                "needs no implementation (a survey, an analysis, a comparison) "
                "needs a conclusion. The answer is one of those two, never "
                "with the finished work. To submit the plan: explore only "
                "enough to plan, then submit it with present_plan — a concise "
                "markdown summary, and in its items array every executable "
                "step with its verify command. Approval writes those items and "
                "starts execution automatically; do not track the planning with "
                "a todo list, do not write the analysis or files as your final "
                "answer, and do not mark items completed — the execution phase "
                "does the reading and the work. To close it without a plan: "
                "finish_research(summary) — the conclusion is the answer, and "
                "no plan is submitted. "
                "Explore with the tools this request carries; the ones that "
                "are missing are the ones you may not use. "
                "Do not treat 'go ahead' or an answer to your question as "
                "approval.</plan-mode>"
            )
        if skills_catalog.strip():
            sections.append(
                f"<skills>Available skills:\n{skills_catalog.strip()}</skills>"
            )
        return "\n\n".join(s for s in sections if s)


__all__ = [
    "BASE_INSTRUCTIONS",
    "GENERAL_METHODS",
    "METHODS_SECTION",
    "TOOL_GUIDANCE",
    "PromptAssembler",
]
