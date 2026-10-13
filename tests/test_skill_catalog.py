"""The skill catalog and its budget.

Pure functions over parsed skills, so the bound can be tested without a request
— which is the only reason ``build_catalog`` exists at all.
"""

from pathlib import Path

from ai_coding.skills.catalog import (
    SKILL_CATALOG_BUDGET_CHARS,
    build_catalog,
    superseded_note,
)
from ai_coding.skills.registry import parse_skill


def _skill(tmp_path: Path, name: str, description: str, **frontmatter) -> object:
    lines = [f"name: {name}", f"description: {description}"]
    lines += [f"{key}: {value}" for key, value in frontmatter.items()]
    (tmp_path / name).mkdir(parents=True, exist_ok=True)
    target = tmp_path / name / "SKILL.md"
    target.write_text("---\n" + "\n".join(lines) + "\n---\nbody\n", encoding="utf-8")
    return parse_skill(target)


def test_a_skill_is_listed_with_its_description(tmp_path):
    skill = _skill(tmp_path, "bash", "run shell commands")

    text = build_catalog([skill])

    assert "- bash: run shell commands" in text
    assert "skill(action='read', name=...)" in text, "the catalog says how to load one"


def test_the_listing_tells_the_model_the_argument_name_that_exists(tmp_path):
    """The router used to say `skill(id="…")` for a tool whose schema has no
    `id`, injecting an instruction the model cannot follow."""
    from ai_coding.skills.registry import SkillRegistry
    from ai_coding.tools.skill_tool import SkillTool

    _skill(tmp_path, "bash", "d")
    schema = SkillTool(SkillRegistry(tmp_path)).params_json_schema["properties"]

    assert "name" in schema
    assert "id" not in schema, "nothing may name a parameter the tool lacks"
    assert "name=" in build_catalog([_skill(tmp_path, "bash", "d")])


def test_no_skills_means_no_catalog(tmp_path):
    assert build_catalog([]) == ""


def test_a_skill_directory_with_nothing_in_it_means_no_catalog(tmp_path):
    (tmp_path / "notes").mkdir()
    from ai_coding.skills.registry import SkillRegistry

    assert build_catalog(SkillRegistry(tmp_path).all()) == ""


# ---- the budget -----------------------------------------------------------


def test_the_catalog_never_exceeds_its_budget(tmp_path):
    """The MCP catalog learned this the hard way: a 240-char budget once
    produced 282 characters because the closing tag was added after the
    accounting."""
    skills = [_skill(tmp_path, f"s{i}", "x" * 400) for i in range(40)]

    for budget in (200, 400, 800, 1_500, 8_000):
        text = build_catalog(skills, budget_chars=budget)
        assert len(text) <= budget, f"budget {budget} produced {len(text)} characters"


def test_over_budget_the_description_goes_before_the_skill_does(tmp_path):
    """A skill with no catalog line is still reachable through
    ``skill(action='list')``, so losing the description costs less than losing
    the entry. The budget here fits every *name* and not every *description*."""
    skills = [_skill(tmp_path, f"s{i}", "y" * 600) for i in range(12)]

    text = build_catalog(skills, budget_chars=600)

    for index in range(12):
        assert f"- s{index}" in text, "every skill keeps its line"
    with_description = sum(
        1 for line in text.splitlines() if line.startswith("- s") and ": y" in line
    )
    assert 0 < with_description < 12, "some descriptions survive; not all of them"
    assert "(0 more skill" not in text


def test_a_budget_too_small_for_any_entry_yields_nothing_rather_than_a_rubbish_line(tmp_path):
    text = build_catalog([_skill(tmp_path, "bash", "d")], budget_chars=20)

    assert text == ""


def test_the_default_budget_matches_the_mcp_catalogs(tmp_path):
    from ai_coding.core.mcp import _CATALOG_BUDGET_CHARS

    assert SKILL_CATALOG_BUDGET_CHARS == _CATALOG_BUDGET_CHARS


# ---- archived -------------------------------------------------------------


def test_an_archived_skill_is_not_listed(tmp_path):
    """`status: archived` that still occupies the system prompt is decoration.
    It also still gets routed (docs/data-contracts.md) — the router reads the
    same object now, so one test of the object covers both."""
    skills = [_skill(tmp_path, "live", "d"), _skill(tmp_path, "dead", "d", status="archived")]

    text = build_catalog(skills)

    assert "- live:" in text
    assert "dead" not in text


def test_a_broken_skill_is_listed_and_says_why(tmp_path):
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "SKILL.md").write_text(
        "---\nname: [unclosed\ndescription: d\n---\nbody\n", encoding="utf-8"
    )
    from ai_coding.skills.registry import SkillRegistry

    registry = SkillRegistry(tmp_path)
    text = build_catalog(registry.all())

    assert "broken" in text
    assert "无法解析" in text, "a skill that silently vanishes cannot be debugged"


# ---- mid-session change ---------------------------------------------------


def test_no_change_means_no_note(tmp_path):
    skills = (_skill(tmp_path, "a", "d"),)

    assert superseded_note(("a",), skills) == ""


def test_a_new_skill_is_declared_because_the_prompt_only_grows(tmp_path):
    skills = (_skill(tmp_path, "a", "d"), _skill(tmp_path, "b", "d"))

    note = superseded_note(("a",), skills)

    assert "b" in note and "supersedes" in note


def test_a_removed_skill_is_withdrawn_explicitly(tmp_path):
    """A prompt cannot be edited, so a removal has to be written as a negation."""

    note = superseded_note(("a", "b"), (_skill(tmp_path, "a", "d"),))

    assert "b" in note and "do not use" in note


def test_a_skill_the_author_disabled_is_not_listed(tmp_path):
    quiet = _skill(tmp_path, "quiet", "d", **{"disable-model-invocation": True})
    loud = _skill(tmp_path, "loud", "d")

    text = build_catalog([quiet, loud])

    assert "quiet" not in text
    assert "loud" in text
