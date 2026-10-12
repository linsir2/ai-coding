"""The skill catalog: what the model is told exists, inside a budget.

A skill catalog sits in the **system prompt** — the most expensive and most
cache-sensitive slot there is — and this one had no bound at all: N skills ×
unbounded descriptions went in verbatim. The MCP catalog on the same project
already solved this (`build_mcp_catalog`, 8,000 chars, every character
accounted for before it is spent), so this is that shape applied to skills.

One thing is deliberately different. A remote tool with no catalog line is
**unreachable**; a skill with no catalog line is not, because the model can call
``skill(action="list")`` and then read it. So the budget degrades instead of
dropping:

    full entry  ->  name only  ->  named as omitted

This is also why no usage-based eviction is needed. Claude Code drops the
descriptions of the least-invoked skills when its listing overflows, which is a
rich-get-richer policy with a cold-start deadlock: a never-invoked skill has no
usage, so it is evicted first, so its keywords are gone, so the model cannot
match it, so it stays never-invoked. Here a skill is never evicted, only
shortened.
"""

from ai_coding.skills.registry import Skill

#: Same bound as ``core/mcp.py::_CATALOG_BUDGET_CHARS``. Both are system-prompt
#: catalogs competing for the same prefix; one number for both is the point.
SKILL_CATALOG_BUDGET_CHARS = 8_000

#: Longest description kept in full before it is shortened. Claude Code caps the
#: name+description entry at 1,536 chars; a skill description is a sentence or
#: two, so anything past this is prose that belongs in the body.
_MAX_DESCRIPTION_CHARS = 200

#: Room kept for the trailing count line, sized for three digits.
_OMITTED_NOTE_RESERVE = len("(999 more skill(s) omitted: catalog budget)") + 1

_HEADER = (
    "<skills>Available skills. Read one with skill(action='read', name=...).\n"
    "A description says when to reach for it; the name is the only argument.\n"
)


def _entry(skill: Skill) -> list[str]:
    """The full listing for one skill."""
    if skill.error is not None:
        # Listed, not dropped: Codex issue #11385 was a skill that vanished
        # behind a warning line nobody reads.
        return [f"- {skill.id}: [无法解析 frontmatter] {skill.error}"]
    text = _short_description(skill)
    lines = [f"- {skill.id}: {text}" if text else f"- {skill.id}"]
    if skill.triggers:
        parts = []
        if skill.triggers.keywords:
            parts.append("keywords: " + ", ".join(skill.triggers.keywords[:3]))
        if skill.triggers.tools:
            parts.append("tools: " + ", ".join(skill.triggers.tools[:3]))
        if skill.triggers.failure_patterns:
            parts.append("on error: " + ", ".join(skill.triggers.failure_patterns[:2]))
        if parts:
            lines.append(f"  Triggers: {'; '.join(parts)}")
    if skill.phase:
        lines.append(f"  [phase: {skill.phase}]")
    return lines


def _short_description(skill: Skill) -> str:
    text = skill.description
    if len(text) > _MAX_DESCRIPTION_CHARS:
        text = text[:_MAX_DESCRIPTION_CHARS].rstrip() + "…"
    return text


def _listing(skill: Skill) -> list[str]:
    if skill.error is not None:
        return [f"- {skill.id}: [无法解析]"]
    if not skill.description:
        return [f"- {skill.id}"]
    return [f"- {skill.id}: {_short_description(skill)}"]


def build_catalog(
    skills: tuple[Skill, ...] | list[Skill],
    budget_chars: int = SKILL_CATALOG_BUDGET_CHARS,
) -> str:
    """The catalog text, bounded by ``budget_chars``. ``''`` for no skills.

    Pure, like ``build_mcp_catalog``: the budget is the whole reason this
    function exists and it has to be testable without a request.

    The budget is spent in three ordered passes rather than per skill, because
    spending it per skill lets one long description crowd every other skill out
    of the listing entirely — the same reason ``build_mcp_catalog`` gives every
    server one tool before any server gets a second:

        1. every skill gets its **name** — the floor, and what makes it loadable
        2. every skill gets its **description** — what says when to reach for it
        3. skills get their **trigger and phase hints** — the decoration

    So what degrades under pressure is the hint, then the description, and only
    then the name.

    Archived skills are not listed. They stay readable by name — ``status:
    archived`` means "do not recommend this proactively", which is what Claude
    Code's ``disable-model-invocation`` does, not "this is gone".
    """
    listed = [skill for skill in skills if not skill.is_archived]
    if not listed:
        return ""

    # Everything the output contains is accounted for before anything is added:
    # the header, the newline after it, the closing tag, and the note that may
    # or may not be needed. Leaving the tag out is how a 240-char budget once
    # produced 282.
    used = len(_HEADER) + 1 + len("</skills>") + _OMITTED_NOTE_RESERVE
    blocks: dict[str, list[str]] = {}
    omitted: list[Skill] = []

    def offer(skill: Skill, entry: list[str]) -> bool:
        """Accept a better entry for this skill, or leave the existing one."""
        nonlocal used
        cost = len("\n".join(entry)) + 1
        if used + cost > budget_chars:
            return False
        used += cost
        blocks[skill.id] = entry
        return True

    # Pass 1 — the name, so every skill inside the budget is loadable.
    for skill in listed:
        entry = (
            [f"- {skill.id}: [无法解析 frontmatter]"]
            if skill.error is not None
            else [f"- {skill.id}"]
        )
        if not offer(skill, entry):
            omitted.append(skill)

    # Pass 2 — the description, one per skill, in id order.
    for skill in listed:
        if skill.error is not None or skill.id in {s.id for s in omitted}:
            continue
        text = _short_description(skill)
        if text:
            offer(skill, [f"- {skill.id}: {text}"])

    # Pass 3 — hints. Independent of whether the description fitted: a phase
    # lock is a routing constraint the model should see even for a skill whose
    # description was too long to afford.
    for skill in listed:
        if skill.error is None and skill.id not in {s.id for s in omitted}:
            offer(skill, _entry(skill))

    lines = ["\n".join(entry) for entry in blocks.values()]
    if omitted:
        note = f"({len(omitted)} more skill(s) not shown: " + ", ".join(s.id for s in omitted) + ")"
        if used + len(note) + 1 <= budget_chars:
            lines.append(note)
    if not lines:
        # A catalog whose whole content is "not shown" tells the model nothing
        # it could act on. Empty is the honest answer, and it is what an empty
        # skills root already means.
        return ""
    return _HEADER + "\n".join(lines) + "\n</skills>"


def superseded_note(previous: tuple[str, ...], current: tuple[Skill, ...]) -> str:
    """The line to append when the listing changed mid-session.

    ``skill_review`` runs every few turns and writes new skills, so the catalog
    is *not* byte-stable within a session — three places in our own docs claim
    it is. Rather than pretend, the change is declared. A prompt only grows, so
    removing a skill has to be written as a negation: OpenCode v2 spells this
    out for the same reason ("The following skill IDs are no longer available
    and must not be used").

    Returns ``''`` when nothing changed, so the common case costs nothing.
    """
    names = tuple(skill.id for skill in current)
    if names == previous:
        return ""
    gone = [name for name in previous if name not in names]
    text = "The available skills have changed. This list supersedes the previous one."
    if gone:
        text += " No longer available, do not use: " + "、".join(gone) + "."
    return text


__all__ = [
    "SKILL_CATALOG_BUDGET_CHARS",
    "build_catalog",
    "superseded_note",
]
