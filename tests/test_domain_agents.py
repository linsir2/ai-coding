"""TDD (M1b T1): the agent contract is pure data — no IO, no security.

The layer this module lives in is the reason for most of what is asserted
here: ``domain/`` must not import anything outside itself, so the catalog
cannot carry parsed rules (a ``Rule`` is a ``security`` type).  It carries the
**raw rows** instead, and the reader that turns them into rules is the same
one the session layer uses.
"""

from __future__ import annotations

from ai_coding.domain.agents import (
    PLAN_AGENT_ID,
    AgentCatalog,
    AgentSpec,
    is_plan_agent,
)


def test_plan_is_the_only_plan_agent():
    assert PLAN_AGENT_ID == "plan"
    assert is_plan_agent("plan") is True
    assert is_plan_agent("build") is False
    assert is_plan_agent("") is False


def test_a_spec_carries_raw_rule_rows():
    """The rules stay rows: parsing them is the security layer's job."""
    spec = AgentSpec(id="explore", kind="subagent")
    assert spec.permissions == ()
    assert AgentSpec(
        id="x",
        permissions=({"action": "read", "resource": "*", "effect": "allow"},),
    ).permissions[0]["action"] == "read"


def test_a_spec_defaults_to_a_primary_with_no_body():
    spec = AgentSpec(id="reviewer")
    assert spec.kind == "primary"
    assert spec.hidden is False
    assert spec.body == ""
    assert spec.source == "builtin"
    assert spec.path == ""


def _catalog() -> AgentCatalog:
    return AgentCatalog(
        specs={
            "build": AgentSpec(id="build", name="Build"),
            "plan": AgentSpec(id="plan", name="Plan"),
            "general": AgentSpec(id="general", kind="subagent"),
            "explore": AgentSpec(id="explore", kind="subagent", hidden=True),
        }
    )


def test_the_catalog_answers_which_agents_are_which():
    catalog = _catalog()
    assert catalog.ids() == ("build", "explore", "general", "plan")
    assert sorted(spec.id for spec in catalog.primary()) == ["build", "plan"]
    assert sorted(spec.id for spec in catalog.subagents()) == ["explore", "general"]


def test_a_hidden_agent_is_not_listed_but_is_still_reachable():
    """Q6: hidden means "do not offer", not "cannot use" (OpenCode's meaning)."""
    catalog = _catalog()
    assert "explore" not in [spec.id for spec in catalog.visible()]
    assert catalog.get("explore") is not None
    assert catalog.resolve("explore").id == "explore"


def test_an_unknown_agent_resolves_to_build():
    """Fail-safe: an agent nobody can resolve runs with build's rules."""
    assert _catalog().resolve("typo").id == "build"


def test_the_default_agent_must_be_a_primary():
    catalog = _catalog()
    assert catalog.default_id("plan") == "plan"
    assert catalog.default_id("build") == "build"
    assert catalog.default_id("explore") == "build"
    assert catalog.default_id("typo") == "build"
    assert catalog.default_id("") == "build"


def test_skipped_files_are_carried_not_swallowed():
    catalog = AgentCatalog(specs={}, skipped=("/repo/agents/broken.md: bad mode",))
    assert catalog.skipped == ("/repo/agents/broken.md: bad mode",)
