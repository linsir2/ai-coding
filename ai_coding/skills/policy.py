"""One reading of ``ai.skill_permissions`` (E-5).

A deny is an operator boundary and must mean the same thing at every judge:
the gate (the model's read is refused), the catalog (the skill is not offered)
and the mention block (a user's @name does not inject it). The gate resolves
spellings through an injected resolver (it takes no registry dependency);
these helpers apply the same alias-aware normalization on the registry side.
"""

from __future__ import annotations

from collections.abc import Mapping

from ai_coding.skills.registry import SkillRegistry


def denied_skill_ids(
    registry: SkillRegistry | None, permissions: Mapping[str, str] | None
) -> frozenset[str]:
    """The canonical ids behind every ``deny`` entry, aliases resolved.

    An entry whose key matches no skill stays under its literal name: a typo
    still denies exactly the spelling it names, never more.
    """
    denied: set[str] = set()
    for key, value in (permissions or {}).items():
        if str(value) != "deny":
            continue
        skill = registry.resolve(str(key)) if registry is not None else None
        denied.add(skill.id if skill is not None else str(key))
    return frozenset(denied)


__all__ = ["denied_skill_ids"]
