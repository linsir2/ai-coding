"""TDD (r16/E-5): one reading of ai.skill_permissions — deny is a boundary.

The gate judges a call, the catalog decides what is offered, the mention block
decides what a user's @name receives. All three normalize aliases through this
module so a deny cannot be bypassed by the other spelling.
"""

from ai_coding.skills.policy import denied_skill_ids
from ai_coding.skills.registry import SkillRegistry


def _skill(root, name, alias=None, status=None):
    lines = [f"name: {alias or name}", "description: d"]
    if status:
        lines.append(f"status: {status}")
    (root / name).mkdir(parents=True, exist_ok=True)
    (root / name / "SKILL.md").write_text("---\n" + "\n".join(lines) + "\n---\nbody\n")


def test_a_deny_key_is_normalized_through_the_registry(tmp_path):
    _skill(tmp_path, "tidy", alias="clean")
    registry = SkillRegistry(tmp_path)

    assert denied_skill_ids(registry, {"clean": "deny"}) == frozenset({"tidy"})
    assert denied_skill_ids(registry, {"tidy": "deny"}) == frozenset({"tidy"})


def test_non_deny_values_are_not_boundaries(tmp_path):
    _skill(tmp_path, "tidy")
    registry = SkillRegistry(tmp_path)

    assert denied_skill_ids(registry, {"tidy": "ask"}) == frozenset()
    assert denied_skill_ids(registry, {}) == frozenset()


def test_an_unresolvable_key_still_denies_its_literal_name(tmp_path):
    assert denied_skill_ids(SkillRegistry(tmp_path), {"ghost": "deny"}) == frozenset({"ghost"})
