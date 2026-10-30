"""Where agents come from (M1b T1) — discovery, validation, precedence.

An agent is a markdown file with a YAML header.  This module is the only
place that reads those files, and it decides three things:

* **whether a file is an agent at all** — a file whose header or whose rule
  rows do not read is skipped *whole*, with the reason kept.  An agent that
  half-loaded is an agent whose rules nobody can explain, and a file that
  vanishes without trace is a file the author cannot debug.
* **which agent wins** — the same id in a nearer scope replaces the farther
  one *whole* (no field merging), the ``AGENTS.md`` convention: the nearer
  scope makes the more specific claim.  A project file is a different agent,
  not a patch of the user's.
* **what an agent may do** — its ``permissions`` rows, read by the *same*
  reader the session layer uses (``security.permissions.rules_from_dicts``).
  One reader, two callers; the action space is open on purpose (MCP's
  ``server__tool``, plugin tools), so an unknown action is a rule that never
  matches rather than a load failure.

The roots are **lowest precedence first** and the last one is the project's
— the shape ``Paths.agent_roots`` has (the user's global library, then the
project's).  ``source`` says which tier a file came from.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ai_coding.domain.agents import (
    DEFAULT_AGENT_ID,
    PLAN_AGENT_ID,
    PLAN_DIR,
    PRIMARY,
    SUBAGENT,
    AgentCatalog,
    AgentSpec,
)
from ai_coding.infra.frontmatter import as_str, parse_header, split_frontmatter
from ai_coding.security.permissions import rules_from_dicts

__all__ = ["builtin_catalog", "load_agents"]

#: The read-only whitelist ``explore`` runs under: everything is denied, then
#: the tools that cannot change the world are allowed back.  The order is the
#: semantics — a layer is resolved last-match-wins, so the allows have to
#: come *after* the deny.
_EXPLORE_ALLOWED = (
    "read",
    "glob",
    "grep",
    "webfetch",
    "websearch",
    "skill",
    "memory_search",
    "history_search",
)

#: The plan posture's soft tier (M2 T6): the tools that change the world.
#: These are the six the read-only overlay used to refuse structurally --
#: one rule set replaces the special case, and the tool surface prunes by
#: the same rule the gate judges by.
_PLAN_SOFT_DENY = (
    "edit",
    "shell",
    "subagent",
    # B0: the fan-out tool delegates to N sub-agents; plan mode does not
    # delegate (same policy as `subagent`, not a new exception).
    "parallel_research",
    "webfetch",
    "websearch",
    # M3 T8: the planning list is present_plan's, not todo_write's.  Cutting
    # the tool is what stops two owners from writing ``task.items`` in the
    # same turn; a prompt that only *asks* the model not to use it leaves the
    # tool in front of it.
    "todo_write",
)

#: The strict tier adds the tools that do not change the workspace but do
#: change the conversation or the plan: asking the human, writing memory,
#: starting a goal.  It does **not** deny the two exits -- a plan mode
#: with no way to submit a plan is a plan mode that never ends.
_PLAN_STRICT_DENY = (
    "question",
    "memory_write",
    "start_goal",
    "update_goal",
)


def _rule_row(action: str, effect: str) -> dict[str, Any]:
    return {"action": action, "resource": "*", "effect": effect}


def builtin_catalog(*, strict_readonly: bool = False) -> AgentCatalog:
    """The four agents that ship with the program.

    ``plan`` carries no rules yet: its read-only behaviour is still the
    session's ``readonly`` overlay, and turning that into rules is T5/T6's
    job (M2/M3).  ``explore`` is the one that has rules, because a
    delegation target is where "the child may not change the world" has to
    be enforced rather than advised.
    """
    explore_rows: tuple[dict[str, Any], ...] = (
        _rule_row("*", "deny"),
        *(_rule_row(action, "allow") for action in _EXPLORE_ALLOWED),
        # The agent layer sits **above** base, so the ``read * allow`` just
        # added would otherwise erase the base's dotenv question.  opencode's
        # explore agent repeats the same three rows for the same reason
        # (``plugin/agent.ts:121-124``).
        {"action": "read", "resource": "*.env", "effect": "ask"},
        {"action": "read", "resource": "*.env.*", "effect": "ask"},
        {"action": "read", "resource": "*.env.example", "effect": "allow"},
    )
    plan_deny = _PLAN_SOFT_DENY + (_PLAN_STRICT_DENY if strict_readonly else ())
    plan_rows: tuple[dict[str, Any], ...] = tuple(
        _rule_row(action, "deny") for action in plan_deny
    ) + (
        # The exception has to come after the blanket deny: the rule language
        # is last-match-wins.  A scoped allow also keeps write/edit in the
        # tool table (``wholly_disabled`` only removes a wholly denied action).
        {"action": "edit", "resource": f"{PLAN_DIR}/*", "effect": "allow"},
    )
    return AgentCatalog(
        specs={
            DEFAULT_AGENT_ID: AgentSpec(
                id=DEFAULT_AGENT_ID,
                name="Build",
                description="The default coding agent.",
            ),
            PLAN_AGENT_ID: AgentSpec(
                id=PLAN_AGENT_ID,
                name="Plan",
                description=(
                    "Planning agent; writes only the plan directory "
                    f"({PLAN_DIR}/)."
                ),
                permissions=plan_rows,
            ),
            "general": AgentSpec(
                id="general",
                name="General",
                description="General-purpose subagent.",
                kind=SUBAGENT,
            ),
            "explore": AgentSpec(
                id="explore",
                name="Explore",
                description="Read-only exploration subagent.",
                kind=SUBAGENT,
                body=(
                    "You explore and report. You read and search; you do not "
                    "write, edit or run commands."
                ),
                permissions=explore_rows,
            ),
        }
    )


def _rule_rows(value: object, agent_id: str) -> tuple[tuple[dict[str, Any], ...], str | None]:
    """The agent's rule rows, or the reason they do not read."""
    if value is None:
        return (), None
    if not isinstance(value, list):
        return (), "'permissions' must be a list"
    rows: list[dict[str, Any]] = []
    for entry in value:
        if not isinstance(entry, dict):
            return (), "'permissions' entries must be mappings"
        rows.append(dict(entry))
    # The same reader the session layer uses.  A row it cannot read means
    # this agent cannot be trusted with half its rules, so the whole file is
    # skipped — the reason names the agent and the row.
    _, errors = rules_from_dicts(rows, label=f"agent {agent_id} rule")
    if errors:
        return (), errors[0]
    return tuple(rows), None


def _parse_agent(
    path: Path, text: str, source: str
) -> tuple[AgentSpec | None, str | None]:
    """One file: a spec, or the reason there is not one."""
    header, body = split_frontmatter(text)
    data, error = parse_header(header)
    if data is None:
        return None, error or "the frontmatter could not be read"
    agent_id = as_str(data.get("id")) or path.stem
    kind = (as_str(data.get("mode")) or PRIMARY).casefold()
    if kind not in (PRIMARY, SUBAGENT):
        return None, f"'mode' must be {PRIMARY} or {SUBAGENT} (got {kind!r})"
    rows, problem = _rule_rows(data.get("permissions"), agent_id)
    if problem is not None:
        return None, problem
    return (
        AgentSpec(
            id=agent_id,
            name=as_str(data.get("name")),
            description=as_str(data.get("description")),
            kind=kind,
            hidden=as_str(data.get("hidden")).casefold() == "true",
            body=body.strip(),
            permissions=rows,
            source=source,
            path=str(path),
        ),
        None,
    )


def load_agents(
    roots: Sequence[Path | str], *, strict_readonly: bool = False
) -> AgentCatalog:
    """Read every agent file under ``roots`` and merge them into a catalog.

    ``roots`` are **lowest precedence first** and the last one is the
    project's.  A file's id replaces a lower-precedence agent *whole*.

    ``strict_readonly`` selects the strict tier of the built-in plan agent
    (M2 T6): the posture refuses more than the soft tier does.
    """
    specs: dict[str, AgentSpec] = dict(
        builtin_catalog(strict_readonly=strict_readonly).specs
    )
    skipped: list[str] = []
    ordered = [Path(root) for root in roots]
    for index, root in enumerate(ordered):
        # The last root is the project's; the ones before it are the user's.
        source = "project" if index == len(ordered) - 1 else "user"
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                skipped.append(f"{path}: {exc}")
                continue
            spec, problem = _parse_agent(path, text, source)
            if spec is None:
                skipped.append(f"{path}: {problem}")
                continue
            specs[spec.id] = spec
    return AgentCatalog(specs=specs, skipped=tuple(skipped))
