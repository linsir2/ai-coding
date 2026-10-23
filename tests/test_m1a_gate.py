"""M1a slice 2: the gate asks the rules.

The gate used to answer from three tables of tool names.  Now it asks a rule
language: the base layer is *derived* from the two lists that are the truth
today, the config's standing permissions become rules, and the operator's own
files become layers above them.

This file pins the parts that are new: that the base layer cannot drift from
the lists it came from, that a later layer overrides an earlier one, that the
policy file has the last word, that a rule may name the postures it is active
in, and that a rule's refusal outranks a saved grant.

The old gate's own answers are pinned in
``tests/test_permission_gate_equivalence.py``.
"""

from __future__ import annotations

from ai_coding.security.permission_gate import PermissionDecision, PermissionGate
from ai_coding.security.permissions import Effect, FileRules, Rule


def _deny(action: str, resource: str = "*", **kwargs) -> Rule:
    return Rule(action=action, resource=resource, effect=Effect.DENY, **kwargs)


def _allow(action: str, resource: str = "*", **kwargs) -> Rule:
    return Rule(action=action, resource=resource, effect=Effect.ALLOW, **kwargs)


def _ask(action: str, resource: str = "*", **kwargs) -> Rule:
    return Rule(action=action, resource=resource, effect=Effect.ASK, **kwargs)


# ---- R11/R12: the base layer is derived, not copied -------------------------


def test_the_base_layer_has_one_rule_per_live_list_entry() -> None:
    """The base layer is *constructed* from the two lists, so it cannot drift.

    A hand-copied list is how ``finish_research`` went missing once (N1):
    the tool was added to the allow list and nobody copied it into the rule
    table.  If the count matches, the count was derived.
    """
    gate = PermissionGate()

    base = gate._base_rules()

    # M3 T9 added three **resource-scoped** read rules (dotenv ask/ask/allow).
    # They are the one part of the base layer that is not derived from a tool
    # list, so they are counted separately; the derivation invariant still
    # holds for every list entry.
    from ai_coding.security.permission_gate import CREDENTIAL_READ_RULES

    assert len(base) == (
        len(gate._allow_tools) + len(gate._warn_tools) + len(CREDENTIAL_READ_RULES)
    )


def test_every_allowed_tool_is_allowed_and_every_warned_tool_is_asked() -> None:
    """The lists themselves, walked: the base layer answers for each one.

    This is the regression N1 was, written so it cannot come back: add a
    tool to a list and this test fails until the base layer covers it.
    """
    gate = PermissionGate()

    for name in sorted(gate._allow_tools):
        assert gate.check(name, {}).decision is PermissionDecision.ALLOW, name
    for name in sorted(gate._warn_tools):
        assert gate.check(name, {"command": "ls"}).decision is PermissionDecision.WARN, name


def test_the_research_exit_is_in_the_base_layer() -> None:
    """``finish_research`` was the tool N1 lost; name it, so it cannot be lost."""
    gate = PermissionGate()

    assert gate.check("finish_research", {"summary": "x"}).decision is PermissionDecision.ALLOW


# ---- R7: a later layer overrides an earlier one -----------------------------


def test_the_project_file_overrides_the_user_file() -> None:
    """opencode's rule for its own ``permissions`` key: the project wins."""
    gate = PermissionGate(
        rules=FileRules(
            user=(_deny("edit"),),
            project=(_allow("edit"),),
        )
    )

    assert gate.check("edit", {"path": "src/app.py"}).decision is PermissionDecision.ALLOW


def test_the_session_layer_overrides_the_project_file() -> None:
    """The session layer is the newest word, so it wins."""
    gate = PermissionGate(rules=FileRules(project=(_allow("edit"),)))

    decision = gate.check("edit", {"path": "src/app.py"}, session_rules=(_deny("edit"),))

    assert decision.decision is PermissionDecision.DENY


def test_a_later_layer_can_narrow_an_earlier_one() -> None:
    """The rule's own resource is what narrows it -- the later layer wins."""
    gate = PermissionGate(
        rules=FileRules(
            user=(_allow("edit"),),
            project=(_deny("edit", "*.env"),),
        )
    )

    assert gate.check("edit", {"path": ".env"}).decision is PermissionDecision.DENY
    assert gate.check("edit", {"path": "src/app.py"}).decision is PermissionDecision.ALLOW


# ---- R8: the policy file has the last word ----------------------------------


def test_the_policy_file_overrides_the_project_file() -> None:
    """The policy class may only tighten, and it is the last word."""
    gate = PermissionGate(
        rules=FileRules(
            project=(_allow("edit"),),
            policy=(_deny("edit", "*.env"),),
        )
    )

    assert gate.check("edit", {"path": ".env"}).decision is PermissionDecision.DENY
    assert gate.check("edit", {"path": "src/app.py"}).decision is PermissionDecision.ALLOW


# ---- R9: the posture ladder --------------------------------------------------


def test_a_rule_scoped_to_a_posture_is_inactive_in_the_others() -> None:
    """A rule may name the postures it is active in."""
    gate = PermissionGate(rules=FileRules(user=(_deny("edit", postures=("build",)),)))

    assert (
        gate.check("edit", {"path": "f.py"}, posture="build").decision
        is PermissionDecision.DENY
    )
    assert (
        gate.check("edit", {"path": "f.py"}, posture="plan").decision
        is PermissionDecision.WARN
    )
    assert (
        gate.check("edit", {"path": "f.py"}, posture="readonly").decision
        is PermissionDecision.WARN
    )


def test_a_rule_without_postures_is_active_in_all_of_them() -> None:
    """The base layer has no postures, so it is active everywhere."""
    gate = PermissionGate()

    for posture in ("plan", "readonly", "build"):
        assert (
            gate.check("write", {"path": "f.py"}, posture=posture).decision
            is PermissionDecision.WARN
        )


# ---- the new capabilities ----------------------------------------------------


def test_a_rule_can_refuse_what_the_base_allowed() -> None:
    """A scoped refusal: the tool stays, the call is refused."""
    gate = PermissionGate(
        rules=FileRules(user=(_deny("edit", "*.env", reason="环境变量文件不能改"),))
    )

    decision = gate.check("edit", {"path": ".env"})

    assert decision.decision is PermissionDecision.DENY
    assert decision.reason == "环境变量文件不能改"


def test_a_rule_can_allow_what_the_base_asked_about() -> None:
    """An allow rule silences the base's question."""
    gate = PermissionGate(rules=FileRules(user=(_allow("write"),)))

    assert (
        gate.check("write", {"path": "f.py", "content": "x"}).decision
        is PermissionDecision.ALLOW
    )


def test_a_rule_can_ask_where_the_base_allowed() -> None:
    """A question is a question, even over an allow."""
    gate = PermissionGate(rules=FileRules(user=(_ask("read"),)))

    decision = gate.check("read", {"path": "f.py"})

    assert decision.decision is PermissionDecision.WARN
    assert decision.reason == "tool 'read' requires confirmation"


def test_a_rules_refusal_outranks_a_saved_grant() -> None:
    """A grant answers a question; it does not reopen a refusal."""
    gate = PermissionGate(
        grants=["edit:src/app.py"],
        rules=FileRules(user=(_deny("edit"),)),
    )

    assert gate.check("edit", {"path": "src/app.py"}).decision is PermissionDecision.DENY


def test_a_saved_grant_still_answers_a_question() -> None:
    """The other half: a grant still rescues what the rules only asked about."""
    gate = PermissionGate(grants=["edit:src/app.py"])

    assert gate.check("edit", {"path": "src/app.py"}).decision is PermissionDecision.ALLOW


def test_a_rule_about_one_resource_does_not_touch_another() -> None:
    """The resource is the point: a rule names one thing, not the action."""
    gate = PermissionGate(rules=FileRules(user=(_deny("edit", "*.env"),)))

    assert gate.check("edit", {"path": ".env"}).decision is PermissionDecision.DENY
    assert gate.check("edit", {"path": "src/app.py"}).decision is PermissionDecision.WARN


def test_a_bash_rule_is_matched_per_command() -> None:
    """A compound command is judged command by command."""
    gate = PermissionGate(rules=FileRules(user=(_deny("bash", "rm -rf *"),)))

    assert (
        gate.check("bash", {"command": "cd /tmp && rm -rf x"}).decision
        is PermissionDecision.DENY
    )
    assert gate.check("bash", {"command": "cd /tmp"}).decision is PermissionDecision.WARN


def test_a_bash_red_line_outranks_a_rule_that_allows() -> None:
    """The red line is above the rules: no rule may reopen it."""
    gate = PermissionGate(rules=FileRules(user=(_allow("bash"),)))

    decision = gate.check("bash", {"command": "rm -rf /"})

    assert decision.decision is PermissionDecision.DENY
