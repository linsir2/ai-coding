"""M-G4 golden corpus for the permission gate (the whole ``check()`` chain).

``redline_corpus`` freezes one judge: ``bash_red_line_with_fallback``.  The gate
is five steps -- hard tier, agent veto, the rule chain, a saved grant, the
workspace edge -- and only the first step's bash branch had a benchmark.  The
risk that matters lives in the other four: an agent veto reopened by a session
rule, a grant that outlives its scope, a path tool walking out of the workspace.

Same contract as ``redline_corpus`` (frozen rows, a verdict copied from a run, a
size floor per class); a different layer.  Re-freezing is part of the fix: a row
whose verdict moves is a behaviour change, so the row is updated *with* the
change and the benchmark goes red until it is.

Scope note: ``authorized_roots()`` / ``path_allowed()`` are NOT in ``check()``,
so "a read grant must not become a writable root" is out of scope here -- it is
covered by ``test_dispatch_grants.py`` / ``test_gate_grants.py`` and would need a
second judge.  Named rather than silently skipped.

Three row constraints, each with a test, because each failure is silent:

* ``ALLOW_GRANT_ANSWERS`` uses ``write`` / ``edit`` only.  ``check()``'s two
  ALLOW exits both return an empty reason, so ``rule`` cannot tell step 4 (a
  grant) from step 5 (a rule allow).  ``read`` is allowed anyway, so deleting
  step 4 would leave it ALLOW; ``write`` without a grant is WARN, so deleting
  step 4 is visible.
* Rows that test the workspace edge set ``workspace``.  With ``workspace=None``
  ``_outside_resource_reason`` returns immediately and the edge is off.
* ``grants`` are the literals ``grant_key()`` produces.  ``read`` is granted by
  **directory**, so ``read:src/a.py`` would never match; the rows use
  ``write:<path>`` / ``edit:<path>``, and a test pins that each key is exactly
  what the gate computes for that call.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from ai_coding.security.permissions import (
    NO_FILE_RULES,
    Effect,
    FileRules,
    Rule,
    Ruleset,
)

__all__ = [
    "ALLOW_GRANT_ANSWERS",
    "ALLOW_LABELS",
    "ALLOW_RULE_CHAIN",
    "ALLOW_SAFE",
    "CORPUS",
    "DENY_AGENT_VETO",
    "DENY_HARD_TIER",
    "DENY_LABELS",
    "DENY_RULE_CHAIN",
    "FLOORS",
    "KNOWN_FP",
    "KNOWN_MISS",
    "MIN_CORPUS",
    "MIN_KNOWN_FP",
    "MIN_KNOWN_MISS",
    "MUST_ALLOW",
    "MUST_DENY",
    "MUST_WARN",
    "PermissionCase",
    "WARN_ASKS",
    "WARN_LABELS",
    "WARN_OUTSIDE_WORKSPACE",
    "WORKSPACE",
]

#: The workspace the boundary rows are judged against.  Pure text: the gate is
#: asked before the tool runs and never touches the filesystem.
WORKSPACE = "/tmp/m-g4-workspace"


@dataclasses.dataclass(frozen=True)
class PermissionCase:
    """One row: the call, the gate configuration it needs, and its frozen answer.

    ``params`` is a tuple of pairs rather than a dict so the row is hashable
    (the corpus is deduplicated as a set); the values are strings only.
    """

    tool: str
    params: tuple[tuple[str, Any], ...]
    label: str
    #: ``PermissionDecision.name`` -- uppercase, which is what ``judge`` returns.
    verdict: str
    #: ``rule_of(Decision.reason)``: which layer decided, when it says.
    rule: str = ""
    #: The gate configuration this row is about.
    grants: tuple[str, ...] = ()
    denied_tools: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    skill_permissions: tuple[tuple[str, str], ...] = ()
    workspace: str | None = None
    rules: FileRules = NO_FILE_RULES
    #: The per-call arguments.
    session_rules: Ruleset = ()
    agent_rules: Ruleset = ()
    posture: str = "build"


def _bash(command: str, **rest: Any) -> PermissionCase:
    return PermissionCase(
        tool="bash", params=(("command", command),), label="deny_hard_tier",
        verdict="DENY", **rest,
    )


def _read(path: str, **rest: Any) -> PermissionCase:
    return PermissionCase(
        tool="read", params=(("path", path),), label="allow_safe",
        verdict="ALLOW", **rest,
    )


# ---- 1. the hard tier: a red line, a config denial, the skill policy --------

DENY_HARD_TIER: tuple[PermissionCase, ...] = (
    _bash("rm -rf /"),
    _bash("sudo rm -rf /etc"),
    _bash("rm -rf ~"),
    _bash("dd if=/dev/zero of=/dev/sda"),
    _bash("mkfs.ext4 /dev/sda1"),
    _bash("chmod -R 777 /"),
    _bash("shutdown now"),
    PermissionCase(
        tool="read", params=(("path", "a.py"),), label="deny_hard_tier",
        verdict="DENY", denied_tools=("read",),
    ),
    PermissionCase(
        tool="write", params=(("path", "a.py"),), label="deny_hard_tier",
        verdict="DENY", denied_tools=("write",),
    ),
    PermissionCase(
        tool="skill", params=(("action", "read"), ("name", "danger")),
        label="deny_hard_tier", verdict="DENY",
        skill_permissions=(("danger", "deny"),),
    ),
)

# ---- 2. the agent's veto is absolute ---------------------------------------
#
# Each row pairs an agent DENY with a session ALLOW for the same tool.  Step 2
# evaluates the agent layer *alone*, so the session rule must not reopen it.
# The reason is frozen too: "DENY" alone cannot tell this layer from any other.

_AGENT_VETO_REASON = "agent narrowed to nothing"
_AGENT_VETO = (
    Rule(action="*", resource="*", effect=Effect.DENY, reason=_AGENT_VETO_REASON),
)
_SESSION_REOPEN = (Rule(action="*", resource="*", effect=Effect.ALLOW),)


def _vetoed(tool: str, params: tuple[tuple[str, Any], ...]) -> PermissionCase:
    return PermissionCase(
        tool=tool, params=params, label="deny_agent_veto", verdict="DENY",
        rule=_AGENT_VETO_REASON,
        agent_rules=_AGENT_VETO, session_rules=_SESSION_REOPEN,
    )


DENY_AGENT_VETO: tuple[PermissionCase, ...] = (
    _vetoed("bash", (("command", "ls"),)),
    _vetoed("read", (("path", "a.py"),)),
    _vetoed("write", (("path", "a.py"),)),
    _vetoed("glob", (("pattern", "*.py"),)),
    _vetoed("grep", (("pattern", "target"),)),
    _vetoed("webfetch", (("url", "https://example.invalid/"),)),
)

# ---- 3. the rule chain: the last layer that speaks wins --------------------


def _user(*rules: Rule) -> FileRules:
    return FileRules(user=rules)


def _project(*rules: Rule) -> FileRules:
    return FileRules(project=rules)


def _policy(*rules: Rule) -> FileRules:
    return FileRules(policy=rules)


def _chain_denied(
    tool: str,
    params: tuple[tuple[str, Any], ...],
    reason: str,
    *,
    rules: FileRules = NO_FILE_RULES,
    session_rules: Ruleset = (),
) -> PermissionCase:
    """A rule-chain refusal, with the layer's own words frozen alongside it."""
    return PermissionCase(
        tool=tool, params=params, label="deny_rule_chain", verdict="DENY",
        rule=reason, rules=rules, session_rules=session_rules,
    )


DENY_RULE_CHAIN: tuple[PermissionCase, ...] = (
    # a user refusal stands on its own
    _chain_denied(
        "read", (("path", "secret.txt"),), "no secrets",
        rules=_user(Rule("read", "secret.txt", Effect.DENY, "no secrets")),
    ),
    # ... and the project narrows the user
    _chain_denied(
        "read", (("path", "a.py"),), "project says no",
        rules=FileRules(
            user=(Rule("read", "*", Effect.ALLOW),),
            project=(Rule("read", "*", Effect.DENY, "project says no"),),
        ),
    ),
    # ... and the session narrows the project
    _chain_denied(
        "read", (("path", "a.py"),), "this session says no",
        rules=_project(Rule("read", "*", Effect.ALLOW)),
        session_rules=(Rule("read", "*", Effect.DENY, "this session says no"),),
    ),
    # ... and the policy has the last word
    _chain_denied(
        "read", (("path", "a.py"),), "policy says no",
        rules=FileRules(
            project=(Rule("read", "*", Effect.ALLOW),),
            policy=(Rule("read", "*", Effect.DENY, "policy says no"),),
        ),
    ),
    _chain_denied(
        "glob", (("pattern", "*.py"),), "no globbing",
        rules=_user(Rule("glob", "*", Effect.DENY, "no globbing")),
    ),
    _chain_denied(
        "grep", (("pattern", "target"),), "no grepping",
        rules=_user(Rule("grep", "*", Effect.DENY, "no grepping")),
    ),
    _chain_denied(
        "todo_write", (("todos", "[]"),), "policy: no todo",
        rules=_policy(Rule("todo_write", "*", Effect.DENY, "policy: no todo")),
    ),
    _chain_denied(
        "webfetch", (("url", "https://example.invalid/"),), "no network here",
        rules=_project(Rule("webfetch", "*", Effect.DENY, "no network here")),
    ),
    _chain_denied(
        "write", (("path", "a.py"),), "session: no writes",
        rules=_project(Rule("write", "*", Effect.ALLOW)),
        session_rules=(Rule("write", "*", Effect.DENY, "session: no writes"),),
    ),
    _chain_denied(
        "history_search", (("query", "deps"),), "session: no history",
        session_rules=(
            Rule("history_search", "*", Effect.DENY, "session: no history"),
        ),
    ),
)

#: The mirror image of the agent veto: a session allow *does* reopen a user
#: refusal, because the session layer sits above the user's file.  Same call as a
#: `deny_rule_chain` row, opposite answer -- which is why the label has to say
#: which direction it is, not which layer it exercises.
ALLOW_RULE_CHAIN: tuple[PermissionCase, ...] = (
    PermissionCase(
        tool="read", params=(("path", "a.py"),), label="allow_rule_chain",
        verdict="ALLOW",
        rules=_user(Rule("read", "*", Effect.DENY)),
        session_rules=(Rule("read", "*", Effect.ALLOW),),
    ),
)

# ---- 4. a saved grant answers a question (step 4) --------------------------
#
# `write` / `edit` only: without a grant they are WARN, so deleting step 4 turns
# these rows WARN and the benchmark goes red.  `read` would stay ALLOW either way.

ALLOW_GRANT_ANSWERS: tuple[PermissionCase, ...] = (
    PermissionCase(
        tool="write", params=(("path", "a.py"),), label="allow_grant_answers",
        verdict="ALLOW", grants=("write:a.py",),
    ),
    PermissionCase(
        tool="edit", params=(("path", "a.py"),), label="allow_grant_answers",
        verdict="ALLOW", grants=("edit:a.py",),
    ),
    PermissionCase(
        tool="write", params=(("path", "src/b.py"),), label="allow_grant_answers",
        verdict="ALLOW", grants=("write:src/b.py",),
    ),
    PermissionCase(
        tool="edit", params=(("path", "src/b.py"),), label="allow_grant_answers",
        verdict="ALLOW", grants=("edit:src/b.py",),
    ),
    PermissionCase(
        tool="write", params=(("path", "docs/c.md"),), label="allow_grant_answers",
        verdict="ALLOW", grants=("write:docs/c.md",),
    ),
    PermissionCase(
        tool="edit", params=(("path", "docs/c.md"),), label="allow_grant_answers",
        verdict="ALLOW", grants=("edit:docs/c.md",),
    ),
)

# ---- 5. ordinary work is not refused ---------------------------------------

ALLOW_SAFE: tuple[PermissionCase, ...] = (
    _read("a.py"),
    _read("src/b.py"),
    _read("docs/c.md"),
    _read("README.md"),
    PermissionCase(
        tool="glob", params=(("pattern", "*.py"),), label="allow_safe",
        verdict="ALLOW",
    ),
    PermissionCase(
        tool="glob", params=(("pattern", "src/**/*.py"),), label="allow_safe",
        verdict="ALLOW",
    ),
    PermissionCase(
        tool="grep", params=(("pattern", "target"),), label="allow_safe",
        verdict="ALLOW",
    ),
    PermissionCase(
        tool="grep", params=(("pattern", "target"), ("path", "src")),
        label="allow_safe", verdict="ALLOW",
    ),
    PermissionCase(
        tool="todo_write", params=(("todos", "[]"),), label="allow_safe",
        verdict="ALLOW",
    ),
    PermissionCase(
        tool="webfetch", params=(("url", "https://example.invalid/"),),
        label="allow_safe", verdict="ALLOW",
    ),
    PermissionCase(
        tool="websearch", params=(("query", "python"),), label="allow_safe",
        verdict="ALLOW",
    ),
    PermissionCase(
        tool="memory_search", params=(("query", "deps"),), label="allow_safe",
        verdict="ALLOW",
    ),
    PermissionCase(
        tool="history_search", params=(("query", "deps"),), label="allow_safe",
        verdict="ALLOW",
    ),
    PermissionCase(
        tool="subagent", params=(("task", "read a.py"),), label="allow_safe",
        verdict="ALLOW",
    ),
    PermissionCase(
        tool="skill", params=(("action", "read"), ("name", "fine")),
        label="allow_safe", verdict="ALLOW",
        skill_permissions=(("fine", "allow"),),
    ),
)

# ---- 6. the tool asks ------------------------------------------------------
#
# `bash` is not on the allow list, so it asks; an unknown tool asks too.  Both
# are WARN, and both are contracts: bash asking on every call is deliberate.

WARN_ASKS: tuple[PermissionCase, ...] = (
    PermissionCase(
        tool="bash", params=(("command", "ls"),), label="warn_asks", verdict="WARN",
    ),
    PermissionCase(
        tool="bash", params=(("command", "git status"),), label="warn_asks",
        verdict="WARN",
    ),
    PermissionCase(
        tool="write", params=(("path", "a.py"),), label="warn_asks", verdict="WARN",
    ),
    PermissionCase(
        tool="edit", params=(("path", "a.py"),), label="warn_asks", verdict="WARN",
    ),
    PermissionCase(
        tool="no_such_tool", params=(), label="warn_asks", verdict="WARN",
    ),
    PermissionCase(
        tool="another_missing_tool", params=(("x", "1"),), label="warn_asks",
        verdict="WARN",
    ),
)

# ---- 7. the workspace edge -------------------------------------------------
#
# These rows must set `workspace`: with None the edge check returns immediately
# and an absolute outside path is ALLOW.

WARN_OUTSIDE_WORKSPACE: tuple[PermissionCase, ...] = (
    PermissionCase(
        tool="read", params=(("path", "/etc/passwd"),),
        label="warn_outside_workspace", verdict="WARN", workspace=WORKSPACE,
    ),
    PermissionCase(
        tool="read", params=(("path", "/root/.ssh/id_rsa"),),
        label="warn_outside_workspace", verdict="WARN", workspace=WORKSPACE,
    ),
    PermissionCase(
        tool="write", params=(("path", "/tmp/outside.txt"),),
        label="warn_outside_workspace", verdict="WARN", workspace=WORKSPACE,
        rules=_project(Rule("write", "*", Effect.ALLOW)),
    ),
    PermissionCase(
        tool="edit", params=(("path", "/tmp/outside2.txt"),),
        label="warn_outside_workspace", verdict="WARN", workspace=WORKSPACE,
        rules=_project(Rule("edit", "*", Effect.ALLOW)),
    ),
)

# ---- the corpus and its floors ---------------------------------------------

#: The holes the benchmark found and did not fix.  Listed, not hidden: a fix
#: shows up as a number dropping, exactly like ``redline_corpus.KNOWN_MISS``.
#:
#: `grep` / `glob` with an **absolute** path outside the workspace is allowed,
#: while `read` / `write` with the same path asks.  ``_outside_resource_reason``
#: runs ``_directory_of`` on the resource for ``_DIR_TOOLS``, and that helper
#: strips the leading ``/`` -- so ``/etc`` becomes the relative ``etc`` and the
#: "relative is inside by construction" early return fires.  A real bypass of the
#: *asking* layer (the sandbox still enforces the write boundary), and out of
#: M-G4's scope: this milestone is a benchmark, and the plan says not to change
#: ``check()``'s behaviour.  Named here so it is visible and cannot be forgotten.
KNOWN_MISS: tuple[PermissionCase, ...] = (
    PermissionCase(
        tool="grep", params=(("pattern", "secret"), ("path", "/etc")),
        label="known_miss", verdict="WARN", workspace=WORKSPACE,
    ),
)

#: Nothing is expected to be refused-but-allowed here yet.
KNOWN_FP: tuple[PermissionCase, ...] = ()

CORPUS: tuple[PermissionCase, ...] = (
    DENY_HARD_TIER
    + DENY_AGENT_VETO
    + DENY_RULE_CHAIN
    + ALLOW_RULE_CHAIN
    + ALLOW_GRANT_ANSWERS
    + ALLOW_SAFE
    + WARN_ASKS
    + WARN_OUTSIDE_WORKSPACE
    + KNOWN_MISS
    + KNOWN_FP
)

#: A row whose label says "deny" must be denied: the false-negative direction.
MUST_DENY: tuple[PermissionCase, ...] = (
    tuple(c for c in DENY_HARD_TIER if c.verdict == "DENY")
    + tuple(c for c in DENY_AGENT_VETO if c.verdict == "DENY")
    + tuple(c for c in DENY_RULE_CHAIN if c.verdict == "DENY")
)

#: A row whose label says "ordinary work" must not be refused: the FP direction.
MUST_ALLOW: tuple[PermissionCase, ...] = (
    ALLOW_SAFE + ALLOW_GRANT_ANSWERS + ALLOW_RULE_CHAIN
)

#: The rows that must ask rather than answer.
MUST_WARN: tuple[PermissionCase, ...] = WARN_ASKS + WARN_OUTSIDE_WORKSPACE

#: Which labels mean which direction.  The benchmark reads these rather than the
#: tuples above, so a synthetic row built in a test is counted the same way.
DENY_LABELS = frozenset({"deny_hard_tier", "deny_agent_veto", "deny_rule_chain"})
ALLOW_LABELS = frozenset(
    {"allow_grant_answers", "allow_rule_chain", "allow_safe"}
)
WARN_LABELS = frozenset({"warn_asks", "warn_outside_workspace"})

#: Floors **per class**, not per direction: an aggregate floor lets one class
#: shrink to nothing while another grows.  `DENY_AGENT_VETO` is the one that
#: matters most -- its test loops over the class, so an empty class would pass
#: vacuously.
FLOORS: dict[str, int] = {
    "deny_hard_tier": 10,
    "deny_agent_veto": 6,
    "deny_rule_chain": 10,
    "allow_rule_chain": 1,
    "allow_grant_answers": 6,
    "allow_safe": 15,
    "warn_asks": 6,
    "warn_outside_workspace": 4,
}

#: The known-gap classes are listed rather than floored at a minimum, so their
#: floors are zero: a fix removes the row and the count drops.
MIN_KNOWN_MISS = 0
MIN_KNOWN_FP = 0

#: A corpus thinned to nothing would pass vacuously.
MIN_CORPUS = 55
