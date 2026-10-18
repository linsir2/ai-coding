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
    #: R1-1: the shell command proving this item (empty = not machine-checkable).
    #: The plan runner turns it into an acceptance contract for the step's turn;
    #: unwritten when empty, so old files read byte-identically.
    verify: str = ""

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
        if self.verify:
            data["verify"] = self.verify
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
            verify=str(data.get("verify", "") or ""),
            created_at=str(data.get("createdAt", data.get("created_at", ""))),
            updated_at=str(data.get("updatedAt", data.get("updated_at", ""))),
        )


@dataclass
class Blockers:
    """Why an item cannot start or finish yet (R9a).  Empty = ready.

    ``open``    - dependencies that exist and are not terminal yet;
    ``unknown`` - dependencies whose target is gone (legacy or hand-edited data);
    ``cycle``   - a blocking chain that comes back to this item, when there is one.
    """

    open: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()
    cycle: tuple[str, ...] = ()

    @property
    def any(self) -> bool:
        return bool(self.open or self.unknown or self.cycle)


@dataclass
class TaskState:
    """The goal plus the ordered list of items the model is working through."""

    goal: str = ""
    items: list[TodoItem] = field(default_factory=list)

    # ---- 11-spec §4: task lifecycle (armed tasks drive themselves) --------
    #: True while TaskDriver drives this task (explicit `/plan` arm or a boot
    #: resume).  Persisted so a crash between rounds leaves it resumable;
    #: written only when True (same discipline as every optional key).
    armed: bool = False
    #: Driver profile name; "plan" is the only one today.
    armed_profile: str = ""
    #: Rounds the driver has opened (audit / future caps), across crashes.
    rounds_run: int = 0
    #: Boot auto-resumes spent; the breaker disarms past the cap.
    resume_count: int = 0
    #: D-4 v2: provider-reported tokens this goal episode has spent.  Same
    #: episode-wide accounting as ``rounds_run`` (``arm_goal`` resets it, the
    #: driver accumulates per round) — a crash-resume continues the same
    #: budget instead of letting the breaker multiply it.  Written only when
    #: non-zero (same discipline as every optional key).
    tokens_used: int = 0
    #: D-4: the model's goal-state claim while a ``goal``-profile task runs:
    #: "" (pursuing) | "complete" | "blocked" (``update_goal``).  The goal
    #: driver reads it after each round and stops on a claim; written only when
    #: non-empty (same discipline as every optional key).
    goal_phase: str = ""

    # ---- mutations --------------------------------------------------------

    def arm_goal(self, objective: str) -> None:
        """D-4: the one place a goal episode starts (``start_goal`` / ``/goal``).

        A new episode resets the counters: the blocked threshold and the boot
        breaker both count *within* an episode, and an earlier episode's
        numbers are not evidence about this one.  The item list is kept --
        it is the model's own working context, not the episode's identity.
        """
        self.goal = objective
        self.goal_phase = ""
        self.rounds_run = 0
        self.resume_count = 0
        self.tokens_used = 0
        self.armed = True
        self.armed_profile = "goal"

    def add(
        self,
        content: str,
        *,
        depends_on: list[str] | None = None,
        verify: str = "",
        ts: str = "",
        item_id: str | None = None,
    ) -> TodoItem:
        """Append a ``pending`` item and return it."""
        item = TodoItem(
            id=item_id or self.next_id(),
            content=content,
            status="pending",
            depends_on=list(depends_on or []),
            verify=verify,
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
        verify: str | None = None,
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
        if verify is not None:
            item.verify = verify
        item.updated_at = ts
        return item

    def clear(self) -> None:
        """Empty the list, and with it the goal (R1-5b).

        The goal is the objective *of this list*; keeping it after the list
        is gone makes ``<task-state>`` advertise the session's opening
        question as the objective of a plan that no longer exists
        (measured: a plan turn ending in ``todo_write(clear)``).
        """
        self.items.clear()
        self.goal = ""

    def next_id(self) -> str:
        """The id ``add`` would assign next (public: the tool layer needs it, R9a)."""
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

    def blockers(self, item_id: str) -> Blockers:
        """The unmet dependencies of one item, with a diagnostic for legacy data.

        Pure: it reads the list and nothing else, so the tool layer can decide
        what to refuse without the domain knowing anything about policies.
        """
        item = self.get(item_id)
        if item is None:
            return Blockers()
        open_ids: list[str] = []
        unknown: list[str] = []
        for dep in item.depends_on:
            target = self.get(dep)
            if target is None:
                unknown.append(dep)
            elif not target.is_terminal():
                open_ids.append(dep)
        cycle = self._cycle_from(item_id) if (open_ids or unknown) else ()
        return Blockers(open=tuple(open_ids), unknown=tuple(unknown), cycle=cycle)

    def _cycle_from(self, item_id: str) -> tuple[str, ...]:
        """A non-terminal dependency chain that comes back to ``item_id``.

        Only legacy/hand-edited lists can contain one: ``add`` refuses unknown
        ids (Task 2) and there is no way to rewrite ``depends_on`` afterwards,
        so fresh data is a DAG by construction.
        """
        visited: set[str] = set()
        stack: list[tuple[str, tuple[str, ...]]] = [(item_id, (item_id,))]
        while stack:
            current, path = stack.pop()
            node = self.get(current)
            if node is None or node.is_terminal():
                continue
            for dep in node.depends_on:
                if dep == item_id:
                    return (*path, item_id)
                if dep in visited:
                    continue
                visited.add(dep)
                stack.append((dep, (*path, dep)))
        return ()

    def dependency_invariant(self) -> tuple[str, ...]:
        """Ids whose ``depends_on`` is not an existing, earlier item id.

        This is the acceptance yardstick for "the list is well formed": the tool
        refuses to create violations, and this method makes the ones inherited
        from older sessions visible instead of silent.
        """
        order = {item.id: index for index, item in enumerate(self.items)}
        bad: list[str] = []
        for item in self.items:
            for dep in item.depends_on:
                if dep == item.id or dep not in order or order[dep] >= order[item.id]:
                    bad.append(item.id)
                    break
        return tuple(bad)

    # ---- rendering --------------------------------------------------------

    def _body_lines(self, *, max_items: int) -> list[str]:
        """The list itself, without the ``<task-state>`` envelope.  Pure move
        of what ``render()`` used to build inline — one home for the format,
        two framings of it."""
        lines: list[str] = []
        if self.goal:
            lines.append(f"目标: {self.goal}")
        if not self.items:
            lines.append("(无任务项)")
            return lines

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
        return lines

    def render(self, *, max_items: int = RENDER_MAX_ITEMS) -> str:
        """Render for prompt injection / tool output. Stable and compact."""
        return "\n".join(["<task-state>", *self._body_lines(max_items=max_items), "</task-state>"])

    def render_display(self, *, max_items: int = RENDER_MAX_ITEMS) -> str:
        """Render for the human transcript: the same list, without the
        ``<task-state>`` envelope.  The tags are prompt format; leaking them
        into the UI is noise.  Only the UI uses this — the model keeps
        reading ``render()``."""
        return "\n".join(self._body_lines(max_items=max_items))

    # ---- serialization ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "goal": self.goal,
            "items": [item.to_dict() for item in self.items],
        }
        if self.armed:
            data["armed"] = True
        if self.armed_profile:
            data["armedProfile"] = self.armed_profile
        if self.rounds_run:
            data["roundsRun"] = int(self.rounds_run)
        if self.resume_count:
            data["resumeCount"] = int(self.resume_count)
        if self.tokens_used:
            data["tokensUsed"] = int(self.tokens_used)
        if self.goal_phase:
            data["goalPhase"] = self.goal_phase
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskState:
        raw_items = data.get("items") or []
        items = [
            TodoItem.from_dict(entry)
            for entry in raw_items
            if isinstance(entry, dict) and entry.get("id")
        ]
        return cls(
            goal=str(data.get("goal", "")),
            items=items,
            armed=bool(data.get("armed", False)),
            armed_profile=str(data.get("armedProfile", "") or ""),
            rounds_run=int(data.get("roundsRun", 0) or 0),
            resume_count=int(data.get("resumeCount", 0) or 0),
            tokens_used=int(data.get("tokensUsed", 0) or 0),
            goal_phase=str(data.get("goalPhase", "") or ""),
        )


__all__ = [
    "TodoStatus",
    "VALID_STATUSES",
    "TERMINAL_STATUSES",
    "RENDER_MAX_ITEMS",
    "Blockers",
    "TodoItem",
    "TaskState",
]
