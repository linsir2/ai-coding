"""The agent contract (M1b T1) — pure data, no IO, no imports.

An *agent* is the thing a session is speaking through.  Today the session
carries a posture flag (``mode``); this module introduces the entity that
flag was a stand-in for, so the posture can be *derived* from one authority
instead of being stored twice.

Why this file is in ``domain/`` and why it is so small:

* ``domain/session.py`` derives ``SessionData.mode`` from the agent, so the
  rule "which agent means plan" has to be readable from the bottom layer.
  ``domain/`` imports nothing outside itself, so that rule lives here.
* ``AgentSpec.permissions`` holds **raw rule rows**, not parsed rules.  A
  ``Rule`` is a ``security`` type, and ``domain`` may not import ``security``.
  The reader that turns rows into rules is the one the session layer already
  uses (``security.permissions.rules_from_dicts``) — one reader, two callers.
* ``AgentCatalog`` therefore has no ``rules()``: it is a map of specs and the
  queries a caller can make of that map.  Where the rows are read is the
  caller's business.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ai_coding.security.permissions import Rule

__all__ = [
    "PLAN_DIR",
    "PLAN_AGENT_ID",
    "AgentCatalog",
    "AgentSpec",
    "is_plan_agent",
]

#: The id of the built-in planning agent.  A session whose agent is this one
#: is *in plan mode*; every other agent is an ordinary working session.
PLAN_AGENT_ID = "plan"

#: The one place a planning agent may write, relative to the workspace.
#: It lives here because four readers need to agree on it: the agent's own
#: rule, both planning prompts, and the refusal the dispatcher hands the model.
#: A relative path (not ``Paths``) on purpose: rule resources are portable
#: text, and the absolute form is only ever needed for a human-facing message.
PLAN_DIR = ".ai-coding/plan"

#: The two kinds an agent may be.  A primary agent is a session's own agent;
#: a subagent is a target a parent may delegate to (``subAgent``).
PRIMARY = "primary"
SUBAGENT = "subagent"

#: The agent every unknown id falls back to.  Fail-safe: an agent nobody can
#: resolve runs with build's rules rather than with no rules at all.
DEFAULT_AGENT_ID = "build"


def is_plan_agent(agent_id: str) -> bool:
    """Whether this agent id means "plan".

    One definition, read by the session's derived ``mode``, by the posture
    ladder and by the command layer.  A custom agent is *not* a plan agent in
    M1b: plan's semantics (the review card, the research exit, the plan
    guard) are still tied to the built-in plan agent, and opening them to
    every custom agent is T5/T6's decision, not this batch's.
    """
    return agent_id == PLAN_AGENT_ID


@dataclass(frozen=True)
class AgentSpec:
    """One agent: who it is, what it may do, and what it says.

    Every field exists on every instance — a missing ``description`` is an
    empty description, not an absent key, so a caller never has to ask
    whether the key is there.
    """

    #: Identity.  The file name by default; a file may declare its own.
    id: str
    #: Human-facing name (the catalog prints it).
    name: str = ""
    #: One line of prose (the catalog prints it; the model may read it).
    description: str = ""
    #: ``primary`` or ``subagent``.
    kind: str = PRIMARY
    #: Not offered in listings.  Hidden is not forbidden: the agent is still
    #: reachable by id.
    hidden: bool = False
    #: The agent's own instructions, appended to the system prompt.
    body: str = ""
    #: The agent's rule rows, in the same JSON shape as the session's own
    #: rules.  Raw rows, not parsed rules — see the module docstring.
    permissions: tuple[dict[str, object], ...] = ()
    #: Where it came from: ``builtin`` / ``user`` / ``project``.
    source: str = "builtin"
    #: The file it was read from (empty for a built-in).
    path: str = ""

    @property
    def is_subagent(self) -> bool:
        return self.kind == SUBAGENT


@dataclass(frozen=True)
class AgentCatalog:
    """The agents this session may speak through.

    ``specs`` maps id to spec.  ``skipped`` carries the files that were
    *not* loaded and why — an agent file that fails to read must not vanish
    without trace, or the author has no way to tell what went wrong.
    """

    specs: Mapping[str, AgentSpec] = field(default_factory=dict)
    skipped: tuple[str, ...] = ()

    def get(self, agent_id: str) -> AgentSpec | None:
        """The agent with this id, or ``None``."""
        return self.specs.get(agent_id)

    def ids(self) -> tuple[str, ...]:
        """Every agent id, sorted."""
        return tuple(sorted(self.specs))

    def primary(self) -> tuple[AgentSpec, ...]:
        """The agents a session may speak through."""
        return tuple(
            self.specs[aid] for aid in sorted(self.specs) if self.specs[aid].kind == PRIMARY
        )

    def subagents(self) -> tuple[AgentSpec, ...]:
        """The agents a parent may delegate to."""
        return tuple(
            self.specs[aid] for aid in sorted(self.specs) if self.specs[aid].kind == SUBAGENT
        )

    def visible(self) -> tuple[AgentSpec, ...]:
        """The agents a listing offers, both kinds, minus the hidden ones.

        A listing shows the subagents too: they are not switchable, but they
        are the targets a ``subAgent`` call may name, and a table that hides
        half its rows is a table you have to guess at.
        """
        return tuple(
            self.specs[aid] for aid in sorted(self.specs) if not self.specs[aid].hidden
        )

    def resolve(self, agent_id: str) -> AgentSpec:
        """The agent with this id, or build.

        Fail-safe: an id nobody knows must still produce rules, and the only
        rules we can vouch for are build's.
        """
        found = self.specs.get(agent_id)
        if found is not None:
            return found
        fallback = self.specs.get(DEFAULT_AGENT_ID)
        if fallback is not None:
            return fallback
        return AgentSpec(id=DEFAULT_AGENT_ID)

    def default_id(self, configured: str) -> str:
        """The agent a new session starts in.

        Only a primary may be the default: a session cannot start by speaking
        through a delegation target.  An unknown or non-primary configured
        value falls back to build rather than failing the startup.
        """
        spec = self.specs.get(configured)
        if spec is None or spec.kind != PRIMARY:
            return DEFAULT_AGENT_ID
        return spec.id

    def rules(self, agent_id: str) -> tuple[Rule, ...]:
        """The parsed rule layer of one agent.

        The catalog holds **raw rows** (``domain`` may not import
        ``security``), so the parse happens here -- and it is the same
        reader the session layer uses, so an agent file and a session file
        are read by one definition of what a rule is.

        An unknown id has no layer: a session whose agent nobody can
        resolve runs with build's rules, and inventing a layer for a typo
        would change behaviour for a session that never opted in.
        """
        from ai_coding.security.permissions import rules_from_dicts

        spec = self.specs.get(agent_id)
        if spec is None:
            return ()
        rules, _errors = rules_from_dicts(
            spec.permissions, label=f"agent {spec.id} rule"
        )
        return rules


__all__ += ["DEFAULT_AGENT_ID", "PRIMARY", "SUBAGENT"]
