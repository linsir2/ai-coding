"""Todo / task-state contract (R1a).

The orchestration layer's first-class object: a durable task list that the model
maintains through the ``todo_write`` tool, that the loop renders into every
request, and whose open items are what gate the loop's stop condition
(``stop_policy="todo_aware"``).

Deliberately dependency-free (domain layer only).  ``depends_on`` is rendered and
used for "what's next", but is **not** enforced as a hard gate in R1 — see
docs/r1-analysis.md §4.1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, cast

TodoStatus = Literal["pending", "in_progress", "completed", "blocked", "dropped"]

VALID_STATUSES: frozenset[str] = frozenset(
    {"pending", "in_progress", "completed", "blocked", "dropped"}
)
TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "dropped"})

_MARKERS: dict[str, str] = {
    "pending": "[ ]",
    "in_progress": "[>]",
    "completed": "[x]",
    "blocked": "[!]",
    "dropped": "[-]",
}

RENDER_MAX_ITEMS = 40


@dataclass
class TodoItem:
    """A single actionable item in the task list."""

    id: str
    content: str
    status: TodoStatus = "pending"
    depends_on: list[str] = field(default_factory=list)
    evidence: str | None = None
    created_at: str = ""
    updated_at: str = ""

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "content": self.content,
            "status": self.status,
        }
        if self.depends_on:
            data["dependsOn"] = list(self.depends_on)
        if self.evidence:
            data["evidence"] = self.evidence
        if self.created_at:
            data["createdAt"] = self.created_at
        if self.updated_at:
            data["updatedAt"] = self.updated_at
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TodoItem:
        raw_status = str(data.get("status", "pending"))
        status = raw_status if raw_status in VALID_STATUSES else "pending"
        raw_deps = data.get("dependsOn") or data.get("depends_on") or []
        deps = [str(d) for d in raw_deps] if isinstance(raw_deps, list) else []
        evidence = data.get("evidence")
        return cls(
            id=str(data.get("id", "")),
            content=str(data.get("content", "")),
            status=cast(TodoStatus, status),
            depends_on=deps,
            evidence=None if evidence is None else str(evidence),
            created_at=str(data.get("createdAt", data.get("created_at", ""))),
            updated_at=str(data.get("updatedAt", data.get("updated_at", ""))),
        )


@dataclass
class TaskState:
    """The goal plus the ordered list of items the model is working through."""

    goal: str = ""
    items: list[TodoItem] = field(default_factory=list)

    # ---- mutations --------------------------------------------------------

    def add(
        self,
        content: str,
        *,
        depends_on: list[str] | None = None,
        ts: str = "",
        item_id: str | None = None,
    ) -> TodoItem:
        """Append a ``pending`` item and return it."""
        item = TodoItem(
            id=item_id or self._next_id(),
            content=content,
            status="pending",
            depends_on=list(depends_on or []),
            created_at=ts,
            updated_at=ts,
        )
        self.items.append(item)
        return item

    def update(
        self,
        item_id: str,
        *,
        status: str | None = None,
        content: str | None = None,
        evidence: str | None = None,
        ts: str = "",
    ) -> TodoItem:
        """Patch an item. Raises ``KeyError`` (unknown id) / ``ValueError`` (bad status)."""
        item = self.get(item_id)
        if item is None:
            raise KeyError(item_id)
        if status is not None:
            if status not in VALID_STATUSES:
                raise ValueError(
                    f"invalid status {status!r}; expected one of {sorted(VALID_STATUSES)}"
                )
            item.status = cast(TodoStatus, status)
        if content is not None:
            item.content = content
        if evidence is not None:
            item.evidence = evidence
        item.updated_at = ts
        return item

    def clear(self) -> None:
        self.items.clear()

    def _next_id(self) -> str:
        candidate = len(self.items) + 1
        taken = {item.id for item in self.items}
        while str(candidate) in taken:
            candidate += 1
        return str(candidate)

    # ---- reads ------------------------------------------------------------

    def get(self, item_id: str) -> TodoItem | None:
        for item in self.items:
            if item.id == item_id:
                return item
        return None

    def open_items(self) -> list[TodoItem]:
        return [item for item in self.items if not item.is_terminal()]

    def is_complete(self) -> bool:
        return not self.open_items()

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def done_count(self) -> int:
        return sum(1 for item in self.items if item.is_terminal())

    def unmet_dependencies(self, item: TodoItem) -> list[str]:
        """Ids this item waits on that are not terminal yet."""
        out: list[str] = []
        for dep in item.depends_on:
            target = self.get(dep)
            if target is None or not target.is_terminal():
                out.append(dep)
        return out

    def focus(self) -> TodoItem | None:
        """The item to work on next: first unblocked open item, else first open."""
        open_items = self.open_items()
        if not open_items:
            return None
        for item in open_items:
            if not self.unmet_dependencies(item):
                return item
        return open_items[0]

    # ---- rendering --------------------------------------------------------

    def render(self, *, max_items: int = RENDER_MAX_ITEMS) -> str:
        """Render for prompt injection / tool output. Stable and compact."""
        lines: list[str] = ["<task-state>"]
        if self.goal:
            lines.append(f"目标: {self.goal}")
        if not self.items:
            lines.append("(无任务项)")
            lines.append("</task-state>")
            return "\n".join(lines)

        lines.append(f"进度: {self.done_count}/{self.total}")
        shown = self.items[:max_items]
        for item in shown:
            marker = _MARKERS.get(item.status, "[ ]")
            suffix = ""
            unmet = self.unmet_dependencies(item)
            if unmet and not item.is_terminal():
                suffix = f" (blocked by {', '.join(unmet)})"
            lines.append(f"{marker} {item.id}. {item.content}{suffix}")
        if len(self.items) > len(shown):
            lines.append(f"... 另有 {len(self.items) - len(shown)} 项未显示")
        nxt = self.focus()
        if nxt is not None:
            lines.append(f"下一步: {nxt.id}. {nxt.content}")
        lines.append("</task-state>")
        return "\n".join(lines)

    # ---- serialization ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {"goal": self.goal, "items": [item.to_dict() for item in self.items]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskState:
        raw_items = data.get("items") or []
        items = [
            TodoItem.from_dict(entry)
            for entry in raw_items
            if isinstance(entry, dict) and entry.get("id")
        ]
        return cls(goal=str(data.get("goal", "")), items=items)


__all__ = [
    "TodoStatus",
    "VALID_STATUSES",
    "TERMINAL_STATUSES",
    "RENDER_MAX_ITEMS",
    "TodoItem",
    "TaskState",
]
