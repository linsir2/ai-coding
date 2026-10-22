"""TDD (M1b T1): the agent's rules are a layer with a veto.

An agent narrows what a session may do.  Two facts make that work:

* the agent's layer sits **above** the base and the config's standing
  permissions but **below** the operator's files and the live session, so a
  person can always narrow further — and widen only within their own scope;
* the agent's **deny is absolute**.  A session ``/permissions allow write``
  must not reopen a write that ``explore`` denied, which is why the veto is
  evaluated on the agent layer alone, before the chain.

The rest is compatibility: with no agent rules, the gate must answer exactly
what it answered before.
"""

from __future__ import annotations

from ai_coding.security.permission_gate import PermissionDecision, PermissionGate
from ai_coding.security.permissions import Effect, FileRules, Rule


def _rule(action: str, resource: str, effect: Effect) -> Rule:
    return Rule(action=action, resource=resource, effect=effect)


def _explore() -> tuple[Rule, ...]:
    allowed = (
        "read",
        "glob",
        "grep",
        "webfetch",
        "websearch",
        "skill",
        "memory_search",
        "history_search",
    )
    return (
        _rule("*", "*", Effect.DENY),
        *(_rule(action, "*", Effect.ALLOW) for action in allowed),
    )


def _gate(**kwargs) -> PermissionGate:
    return PermissionGate(**kwargs)


# ---- the layer's position ---------------------------------------------------


def test_the_agent_layer_sits_between_standing_and_the_operator_files():
    gate = _gate()
    names = tuple(gate.layer_counts().keys())
    assert names == ("base", "standing", "agent", "user", "project", "session", "policy")


def test_layer_counts_with_agent_rules_counts_them():
    counts = _gate().layer_counts((), _explore())
    assert counts["agent"] == 9


# ---- the veto ---------------------------------------------------------------


def test_the_agent_deny_cannot_be_reopened_by_the_session():
    """The case that makes the veto necessary: a live ``allow`` loses."""
    gate = _gate()
    session_rules = (_rule("write", "*", Effect.ALLOW),)
    decision = gate.check(
        "write",
        {"path": "x.py"},
        session_rules=session_rules,
        agent_rules=_explore(),
    )
    assert decision.decision is PermissionDecision.DENY
    assert decision.reason


def test_the_agent_deny_cannot_be_reopened_by_a_saved_grant():
    gate = _gate(grants={"write:x.py"})
    decision = gate.check("write", {"path": "x.py"}, agent_rules=_explore())
    assert decision.decision is PermissionDecision.DENY


def test_an_operator_file_deny_still_beats_the_agent_allow():
    """The agent's allow only wins base/standing — not the person's files."""
    project = (_rule("read", "*.env", Effect.DENY),)
    gate = _gate(rules=FileRules(project=project))
    decision = gate.check("read", {"path": ".env"}, agent_rules=_explore())
    assert decision.decision is PermissionDecision.DENY


def test_the_agent_allow_wins_the_base_tier():
    """A read is allowed by the agent even though the base asks nothing."""
    gate = _gate()
    decision = gate.check("read", {"path": "x.py"}, agent_rules=_explore())
    assert decision.decision is PermissionDecision.ALLOW


def test_a_scoped_agent_deny_only_refuses_that_resource():
    agent_rules = (_rule("edit", "*.env", Effect.DENY),)
    gate = _gate()
    refused = gate.check("edit", {"path": ".env"}, agent_rules=agent_rules)
    other = gate.check("edit", {"path": "x.py"}, agent_rules=agent_rules)
    assert refused.decision is PermissionDecision.DENY
    assert other.decision is not PermissionDecision.DENY


def test_a_compound_command_is_vetoed_by_its_denied_segment():
    """A deny on the whole action refuses every bash command, segment by segment."""
    decision = _gate().check(
        "bash", {"command": "cd /tmp && rm -rf x"}, agent_rules=_explore()
    )
    assert decision.decision is PermissionDecision.DENY


# ---- the catalogue must agree with the verdict -------------------------------


def test_the_catalogue_hides_what_the_agent_denies_wholly():
    gate = _gate()
    assert gate.hides("write", agent_rules=_explore()) is True
    assert gate.hides("bash", agent_rules=_explore()) is True
    assert gate.hides("read", agent_rules=_explore()) is False


def test_a_session_allow_cannot_bring_a_hidden_tool_back():
    """The catalogue predicate has to read the same veto the verdict does."""
    gate = _gate()
    session_rules = (_rule("write", "*", Effect.ALLOW),)
    assert gate.hides("write", session_rules=session_rules, agent_rules=_explore()) is True


# ---- compatibility ----------------------------------------------------------


def test_no_agent_rules_answers_exactly_as_before():
    gate = _gate()
    for tool, params in (
        ("read", {"path": "x.py"}),
        ("write", {"path": "x.py"}),
        ("bash", {"command": "ls"}),
        ("edit", {"path": "x.py"}),
        ("glob", {"pattern": "*.py"}),
    ):
        assert gate.check(tool, params).decision is gate.check(
            tool, params, agent_rules=()
        ).decision


# ---- M2 T6: the plan agent's own read-only rules --------------------------


def _plan_rules(strict: bool = False) -> tuple[Rule, ...]:
    from ai_coding.agents.registry import builtin_catalog

    return builtin_catalog(strict_readonly=strict).rules("plan")


def test_the_plan_agent_denies_the_world_changing_tools():
    """The plan posture is a rule now, not a special case in the dispatcher."""
    rules = _plan_rules()
    denied = {rule.action for rule in rules if rule.effect is Effect.DENY}
    assert "edit" in denied      # → write + edit
    assert "shell" in denied     # → bash
    assert "subagent" in denied  # → subAgent
    assert "webfetch" in denied
    assert "websearch" in denied


def test_the_plan_agent_keeps_the_reading_tools():
    """The plan agent only *denies*; the reading tools come from the base.

    A layer that allowed what the base already allows would be saying the
    same thing twice, and the second copy is the one that drifts.
    """
    rules = _plan_rules()
    assert all(rule.effect is Effect.DENY for rule in rules)

    gate = PermissionGate()
    for tool in ("read", "grep", "glob"):
        decision = gate.check(tool, {"path": "x", "pattern": "*"}, agent_rules=rules)
        assert decision.decision is PermissionDecision.ALLOW, tool


def test_the_soft_tier_does_not_deny_the_shell():
    """The soft tier is what the posture has always meant."""
    denied = {r.action for r in _plan_rules(strict=False) if r.effect is Effect.DENY}
    assert "question" not in denied
    assert "memory_write" not in denied


def test_the_strict_tier_denies_more():
    strict = {r.action for r in _plan_rules(strict=True) if r.effect is Effect.DENY}
    soft = {r.action for r in _plan_rules(strict=False) if r.effect is Effect.DENY}
    assert strict > soft
    assert "question" in strict


def test_the_strict_tier_keeps_the_two_exits():
    """A plan mode with no exit is a plan mode that never ends."""
    strict = {r.action for r in _plan_rules(strict=True) if r.effect is Effect.DENY}
    assert "present_plan" not in strict
    assert "finish_research" not in strict


def test_the_plan_rules_hide_exactly_what_the_readonly_overlay_hid():
    """The soft tier must be equivalent to what it replaces.

    The old posture refused every ``mutates_world`` tool; the new one is
    a rule set.  If the two disagree, the prompt and the tool surface
    start telling different stories.
    """
    from ai_coding.config.models import AIConfig, AppConfig
    from ai_coding.tools.builder import build_tool_registry

    registry = build_tool_registry(
        AppConfig(models={}, default_model="", ai=AIConfig()), ".", None
    )
    hidden_by_overlay = {t.name for t in registry.all_tools if t.mutates_world}

    gate = PermissionGate()
    rules = _plan_rules()
    hidden_by_rules = {
        t.name for t in registry.all_tools if gate.hides(t.name, agent_rules=rules)
    }

    assert hidden_by_rules == hidden_by_overlay
