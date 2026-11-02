"""Orchestration contract (P2-C1).

One request shape for every delegated run.  C1 only needs the *child* kind: a
background row and a fan-out question are registered as task views, not as
requests, so the request stays a thin identity envelope around the P2-A
payload.

Deliberately absent (C1 has no consumer for them):
  - ``idempotency_key`` / ``timeout_s`` -- there is no journal and no
    cross-restart dedupe yet (C2 adds them);
  - ``parent`` / ``observer`` / ``emit`` / ``worktree_manager`` -- they are
    per-call channels, passed as ``Scheduler`` method arguments.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from ai_coding.domain.subagent import SubagentTask

__all__ = ["TaskRequest"]


@dataclass(frozen=True)
class TaskRequest:
    """A delegated run's identity plus the P2-A task payload."""

    id: str
    task: SubagentTask
    name: str = "sub-agent"
    kind: str = "child"
    origin_session: str = ""
    parent_span: str = ""

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("TaskRequest.id must not be empty")
        if self.kind not in ("child", "fanout"):
            raise ValueError(f"unknown TaskRequest.kind: {self.kind}")
        if self.kind == "fanout" and not (
            self.task.readonly and not self.task.isolate
        ):
            raise ValueError("a fanout request must be readonly and not isolated")

    def render(self) -> str:
        """The child's task text, byte-identical to ``SubagentTask.render()``."""
        return self.task.render()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "origin_session": self.origin_session,
            "parent_span": self.parent_span,
            "task": self.task.to_dict(),
        }

    @classmethod
    def for_child(
        cls,
        task: SubagentTask,
        *,
        name: str = "sub-agent",
        origin_session: str = "",
        parent_span: str = "",
        id: str | None = None,
    ) -> TaskRequest:
        """Build a child request; ``id`` is generated when the caller omits it."""
        return cls(
            id=id or uuid.uuid4().hex,
            task=task,
            name=name or "sub-agent",
            kind="child",
            origin_session=origin_session,
            parent_span=parent_span,
        )
