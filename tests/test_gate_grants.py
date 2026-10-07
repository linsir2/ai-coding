"""TDD (G4b-1): a standing grant is a permission decision, held by the gate.

It used to be a ``set`` inside the Textual app, which meant three things: the
REPL could never grant anything (its callback only parsed y/n/s, so a REPL user
answered every write by hand, forever), the trace could not see what the operator
had authorised, and the policy layer had no idea any of it existed.  The grant
is a permission, so it lives with the permissions.
"""

from __future__ import annotations

from typing import Any

import pytest

from ai_coding.security.approval import (
    ApprovalResult,
    CLIApprovalCallback,
)
from ai_coding.security.permission_gate import PermissionDecision, PermissionGate


def _gate(**over: Any) -> PermissionGate:
    return PermissionGate(**over)


# ---- the gate remembers, and only what it was shown ------------------------


def test_a_granted_call_is_allowed_without_asking_again():
    gate = _gate()
    assert gate.check("edit", {"path": "src/a.py"}) == PermissionDecision.WARN

    key = gate.grant("edit", {"path": "src/a.py"})

    assert key == "edit:src/a.py"
    assert gate.check("edit", {"path": "src/a.py"}) == PermissionDecision.ALLOW


def test_a_grant_covers_only_the_resource_it_names():
    gate = _gate()
    gate.grant("edit", {"path": "src/a.py"})

    assert gate.check("edit", {"path": "src/b.py"}) == PermissionDecision.WARN
    assert gate.check("write", {"path": "src/a.py"}) == PermissionDecision.WARN


def test_an_ungrantable_call_records_nothing():
    """bash is excluded by policy, so an "always" on it must not become a
    standing permission for every command."""
    gate = _gate()

    assert gate.grant("bash", {"command": "ls"}) is None
    assert gate.check("bash", {"command": "ls"}) == PermissionDecision.WARN


def test_a_hard_deny_outranks_a_grant():
    """The ordering is the contract: deny, then grant, then the static tables.

    Today the only deny-able tool is bash, which is also un-grantable, so this
    cannot fire through ``grant()``.  It is pinned for the day a *path* rule
    denies something (the remaining half of G4-2), because then a grant that
    precedes the deny would silently reopen the hole.
    """
    gate = _gate(grants={"bash:rm -rf /"})

    assert gate.check("bash", {"command": "rm -rf /"}) == PermissionDecision.DENY


def test_grants_can_be_seeded_at_construction():
    """What persistence hands the gate: a set it did not have to earn."""
    gate = _gate(grants={"edit:src/a.py"})

    assert gate.check("edit", {"path": "src/a.py"}) == PermissionDecision.ALLOW
    assert gate.check("edit", {"path": "src/b.py"}) == PermissionDecision.WARN


def test_a_seeded_grant_for_an_excluded_tool_does_nothing():
    """Belt and braces: even if the store somehow holds a key the current rules
    would never mint, it grants nothing."""
    gate = _gate(grants={"bash:ls"})

    assert gate.check("bash", {"command": "ls"}) == PermissionDecision.WARN


# ---- the callback owns the answer vocabulary -------------------------------


def test_always_is_its_own_approval_result():
    assert ApprovalResult.ALWAYS.value == "always"
    assert ApprovalResult.ALWAYS is not ApprovalResult.YES


@pytest.mark.asyncio
async def test_the_cli_offers_and_parses_always():
    """"a" has to reach the core as ALWAYS, not be flattened to YES -- the
    difference between "this once" and "remember it" is the whole feature."""
    answers = iter(["a"])
    callback = CLIApprovalCallback(input_fn=lambda _prompt: next(answers))

    result = await callback.approve("edit", {"path": "a.py"}, "reason")

    assert result is ApprovalResult.ALWAYS


@pytest.mark.asyncio
async def test_the_cli_prompt_advertises_always():
    seen: list[str] = []

    def capture(prompt: str) -> str:
        seen.append(prompt)
        return "n"

    callback = CLIApprovalCallback(input_fn=capture)
    await callback.approve("edit", {"path": "a.py"}, "reason")

    assert "a" in seen[0].split("Allow?")[-1], seen[0]


@pytest.mark.asyncio
async def test_y_is_still_just_yes():
    callback = CLIApprovalCallback(input_fn=lambda _p: "y")

    assert await callback.approve("edit", {"path": "a.py"}, "") is ApprovalResult.YES


@pytest.mark.asyncio
async def test_an_unknown_answer_is_still_no():
    callback = CLIApprovalCallback(input_fn=lambda _p: "whatever")

    assert await callback.approve("edit", {"path": "a.py"}, "") is ApprovalResult.NO
