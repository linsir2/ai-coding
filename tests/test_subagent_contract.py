"""TDD (r25): the frozen parent → child brief.

`SubagentBrief` is what our own code sends a child: a fixed envelope instead of
an ad-hoc paragraph per call site.  The caps are refusals, never clamps — a
silently shortened brief loses exactly the fact the child was asked to find.
"""

from __future__ import annotations

from ai_coding.domain.subagent import (
    BRIEF_MAX,
    CONSTRAINT_MAX,
    CONSTRAINTS_MAX,
    CONTEXT_MAX,
    OBJECTIVE_MAX,
    SCOPE_ITEM_MAX,
    SCOPE_ITEMS_MAX,
    SubagentBrief,
)


def test_a_minimal_brief_renders_task_only():
    brief = SubagentBrief(objective="找出登录入口")

    assert brief.problems() == ()
    assert brief.render() == "[TASK]\n找出登录入口"


def test_the_full_brief_renders_a_fixed_envelope():
    brief = SubagentBrief(
        objective="找出登录入口",
        scope=("src/auth/", "docs/"),
        context="目标：给房间加价格",
        constraints=("只读：不要写文件、不要运行命令。",),
    )

    assert brief.problems() == ()
    assert brief.render() == (
        "[TASK]\n"
        "找出登录入口\n"
        "\n"
        "[CONTEXT]\n"
        "目标：给房间加价格\n"
        "\n"
        "[SCOPE]\n"
        "- src/auth/\n"
        "- docs/\n"
        "\n"
        "[CONSTRAINTS]\n"
        "- 只读：不要写文件、不要运行命令。"
    )


def test_rendering_is_deterministic():
    brief = SubagentBrief("q", ("a",), "c", ("d",))
    assert brief.render() == brief.render()


def test_the_objective_is_required():
    assert SubagentBrief("   ").problems() == ("objective is required",)


def test_an_oversize_objective_is_refused_not_clamped():
    problems = SubagentBrief("x" * (OBJECTIVE_MAX + 1)).problems()
    assert problems and "objective" in problems[0]
    assert str(OBJECTIVE_MAX) in problems[0]


def test_an_oversize_context_is_refused():
    problems = SubagentBrief("q", context="x" * (CONTEXT_MAX + 1)).problems()
    assert problems and "context" in problems[0]


def test_too_many_scope_items_are_refused():
    scope = tuple(f"p{i}" for i in range(SCOPE_ITEMS_MAX + 1))
    problems = SubagentBrief("q", scope=scope).problems()
    assert any("scope" in problem for problem in problems)


def test_an_empty_or_oversize_scope_item_is_named():
    empty = SubagentBrief("q", scope=("a", "  ")).problems()
    assert any("scope item 2 is empty" in problem for problem in empty)

    long = SubagentBrief("q", scope=("x" * (SCOPE_ITEM_MAX + 1),)).problems()
    assert any("scope item 1" in problem for problem in long)


def test_constraint_caps_are_enforced():
    many = SubagentBrief(
        "q", constraints=tuple(f"c{i}" for i in range(CONSTRAINTS_MAX + 1))
    ).problems()
    assert any("constraints" in problem for problem in many)

    long = SubagentBrief("q", constraints=("x" * (CONSTRAINT_MAX + 1),)).problems()
    assert any("constraints item 1" in problem for problem in long)


def test_a_brief_over_the_total_cap_is_refused():
    """Each part is legal; together they exceed what a child should be sent."""
    brief = SubagentBrief(
        objective="x" * OBJECTIVE_MAX,
        context="y" * CONTEXT_MAX,
        scope=tuple("z" * SCOPE_ITEM_MAX for _ in range(SCOPE_ITEMS_MAX)),
        constraints=tuple("w" * CONSTRAINT_MAX for _ in range(CONSTRAINTS_MAX)),
    )
    rendered = len(brief.render())
    assert rendered > BRIEF_MAX
    assert any("brief is longer" in problem for problem in brief.problems())
