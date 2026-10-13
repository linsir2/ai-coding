"""Where things live: one user-global tier and one per-workspace tier.

The tool had exactly two user-global paths — the grants file and the MCP token
file — and each spelled the XDG lookup out for itself. Everything else, including
the config file that holds the API key, was resolved relative to the workspace,
so changing directory changed what the agent knows and could even stop it from
starting.

Two tiers, because two different things are being expressed:

* **user-global** — who is running this. Their API key, the skills they taught
  the agent, the servers they authenticated to. Follows the machine, not the
  repo. The convention is the one Codex and Claude Code already use for
  ``AGENTS.md``: a user-level file plus a repo-level one, both loaded.
* **workspace** — where the agent is allowed to touch files. Sessions, traces,
  transcripts, hooks and project skills stay here, because B's edits must never
  reach A and A's transcript says nothing useful about B.

Everything is resolved once, here. Three separate bugs came from paths being
computed in eight places with three different rules: the skills root skipped
``resolve()``, the embedding cache resolved against the process CWD rather than
the workspace, and ``AppWorld.workspace`` held the raw ``-w`` string while
``AgentLoop.workspace`` held the resolved one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

#: Directory name under the XDG roots. One string, so ``grants.json`` and
#: ``mcp-auth.json`` can never end up in different trees.
APP_DIR = "ai-coding"


def config_home() -> Path:
    """``$XDG_CONFIG_HOME/ai-coding`` or ``~/.config/ai-coding``.

    Holds settings a person configures once: the API key, the model, global
    skills.
    """
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base).expanduser() / APP_DIR


def data_home() -> Path:
    """``$XDG_DATA_HOME/ai-coding`` or ``~/.local/share/ai-coding``.

    Holds things that are data rather than configuration, and may be secrets:
    the MCP OAuth tokens, and (M9) the global skill library.
    """
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base).expanduser() / APP_DIR


def ancestors_up_to_home(base: Path | str) -> list[Path]:
    """``base`` then its parents, stopping short of the user's home directory.

    `~` is an ancestor of every workspace, so without this a file written at
    ``$HOME/config.local.yaml`` — or ``$HOME/CLAUDE.md`` — would be found for
    every project on the machine. Scope has to stop somewhere, and the user's
    own home is where it stops.

    The user's own settings have a named home of their own
    (:meth:`Paths.user_config`); project discovery must not guess at it.
    """
    base = Path(base)
    try:
        chain = [base, *base.parents]
        home = Path.home().resolve()
    except (RuntimeError, OSError, AttributeError):
        # A test double without ``parents`` or a home that cannot be determined:
        # a single directory is the honest answer, not an exception from a
        # discovery path that has to keep working.
        return [base]
    return chain[: chain.index(home)] if home in chain else chain


@dataclass(frozen=True)
class Paths:
    """Every path the app derives, computed from two roots.

    Construct it once at the entry point and pass it down; do not re-derive.
    """

    workspace: Path

    @classmethod
    def for_workspace(cls, workspace: str | Path) -> Paths:
        """Build from a raw ``-w`` value. Resolved once, here.

        ``expanduser`` because ``-w ~/proj`` is a normal thing to type, and
        ``resolve`` because a relative ``-w`` would otherwise make every derived
        path depend on the process CWD — which is exactly how the embedding
        cache came to follow ``cd``.
        """
        return cls(workspace=Path(workspace).expanduser().resolve())

    # ---- user-global tier ---------------------------------------------------

    @property
    def config_home(self) -> Path:
        return config_home()

    @property
    def data_home(self) -> Path:
        return data_home()

    @property
    def user_config(self) -> Path:
        """The global config file. Not required — a project may not have one."""
        return self.config_home / "config.yaml"

    @property
    def user_skills(self) -> Path:
        """The global skill library (M9). Created on first write, not on read."""
        return self.data_home / "skills"

    # ---- workspace tier -----------------------------------------------------

    @property
    def state(self) -> Path:
        """Everything the run leaves behind: sessions, traces, transcripts."""
        return self.workspace / ".ai-coding"

    @property
    def sessions(self) -> Path:
        return self.state / "sessions"

    @property
    def traces(self) -> Path:
        return self.state / "traces"

    @property
    def exports(self) -> Path:
        return self.state / "exports"

    @property
    def hooks(self) -> Path:
        return self.state / "hooks"

    @property
    def memory(self) -> Path:
        """The project's memory file.

        Workspace-scoped on purpose, and a decision with a cost: what you learn
        in one repo does not follow you to another. Globalising it means a scope
        field on every note and a cross-store recall merge, and it risks
        carrying one project's technical decisions into another. Recorded as a
        deliberate deferral, not an oversight (see ``docs/data-contracts.md``).
        """
        return self.workspace / ".memory"

    @property
    def user_memory(self) -> Path:
        """The user-tier memory file.

        In ``data_home`` next to the MCP tokens rather than in ``config_home``:
        it is accumulated state, not a setting, and a person editing their config
        must not be surprised to find it beside a memory store.
        """
        return self.data_home / "memory.json"

    @property
    def project_skills(self) -> Path:
        """The project's skill library, which wins over the global one (M9)."""
        return self.workspace / "skills"

    @property
    def compat_skill_roots(self) -> tuple[Path, ...]:
        """Workspace skill libraries written for other agents (r16/E-7).

        Claude Code's ``.claude/skills`` and the ``.agents/skills``
        convention, workspace-scoped only: a repo that ships its skills for
        several agents loads them here unmodified, and a personal library
        does not silently flood every project's catalog.
        """
        return (
            self.workspace / ".claude" / "skills",
            self.workspace / ".agents" / "skills",
        )

    # ---- derived ------------------------------------------------------------

    @property
    def skill_roots(self) -> tuple[Path, ...]:
        """Lowest precedence first. The project root is last, so it wins.

        Order is the precedence, and ``SkillRegistry`` resolves in it. A nearer
        scope beats a broader one, which is the same rule ``AGENTS.md`` follows:
        the repo's own instructions are facts about the repo; the user's are
        defaults. The compat roots sit between the two: a repo's foreign-layout
        skills are project assets, but its native ``skills/`` speaks loudest.
        """
        return (self.user_skills, *self.compat_skill_roots, self.project_skills)


__all__ = ["APP_DIR", "Paths", "config_home", "data_home"]
