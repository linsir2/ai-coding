"""The agent subsystem (M1b T1): the table of agents a session may speak through.

The contract lives in :mod:`ai_coding.domain.agents` (pure data, readable
from the bottom layer); this package is where the files come from.
"""

from ai_coding.agents.registry import builtin_catalog, load_agents
from ai_coding.domain.agents import (
    DEFAULT_AGENT_ID,
    PLAN_AGENT_ID,
    PRIMARY,
    SUBAGENT,
    AgentCatalog,
    AgentSpec,
    is_plan_agent,
)

__all__ = [
    "DEFAULT_AGENT_ID",
    "PLAN_AGENT_ID",
    "PRIMARY",
    "SUBAGENT",
    "AgentCatalog",
    "AgentSpec",
    "builtin_catalog",
    "is_plan_agent",
    "load_agents",
]
