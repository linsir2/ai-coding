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

TodoStatus = Literal[
    "pending", "in_progress", "completed", "blocked", "dropped", "skipped"
]

VALID_STATUSES: frozenset[str] = frozenset(
    {"pending", "in_progress", "completed", "blocked", "dropped", "skipped"}
)
#: ``skipped`` is terminal but *not* a success: its dependency never produced
#: the input it needed.  Being terminal keeps it out of ``open_items`` (a plan
#: whose only leftovers were skipped is finished); the dependency query below
#: still treats it as unsatisfied so a dependent can never look ready.
TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "dropped", "skipped"})

_MARKERS: dict[str, str] = {
    "pending": "[ ]",
    "in_progress": "[>]",
    "completed": "[x]",
    "blocked": "[!]",
    "dropped": "[-]",
    "skipped": "[~]",
}

RENDER_MAX_ITEMS = 40
#: Longest handoff digest the prompt carries.  A wave note is a contract, not a
#: transcript: sixteen lines of decisions/interfaces fit; a summary of the work
#: does not belong here (the receipts already carry it).
WAVE_NOTE_MAX_CHARS = 900


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
    #: r24/C: the repository paths this item is expected to touch.  A *grouping
    #: hint* only -- waves prefer to keep items sharing a path together -- never
    #: a correctness constraint.  Unwritten when empty.
    files: list[str] = field(default_factory=list)
    #: r24/C: ``write`` | ``research`` | ``verify``.  ``verify`` items are heavy
    #: test/build commands and close a wave; ``research`` items may run in a
    #: read-only sub-agent later.  Unwritten when default.
    kind: str = "write"
    #: r24/C: repair rounds this item already spent inside its wave.  Kept on the
    #: item (not the wave) so a retry after a resume knows where it stopped.
    #: Unwritten when zero.
    attempts: int = 0

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
        if self.files:
            data["files"] = list(self.files)
        if self.kind != "write":
            data["kind"] = self.kind
        if self.attempts:
            data["attempts"] = int(self.attempts)
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
        raw_files = data.get("files") or []
        files = [str(f) for f in raw_files] if isinstance(raw_files, list) else []
        evidence = data.get("evidence")
        try:
            attempts = int(data.get("attempts", 0) or 0)
        except (TypeError, ValueError):
            attempts = 0
        return cls(
            id=str(data.get("id", "")),
            content=str(data.get("content", "")),
            status=cast(TodoStatus, status),
            depends_on=deps,
            evidence=None if evidence is None else str(evidence),
            verify=str(data.get("verify", "") or ""),
            files=files,
            kind=str(data.get("kind", "write") or "write"),
            attempts=attempts,
            created_at=str(data.get("createdAt", data.get("created_at", ""))),
            updated_at=str(data.get("updatedAt", data.get("updated_at", ""))),
        )


@dataclass
class Blockers:
    """Why an item cannot start or finish yet (R9a).  Empty = ready.

    ``open``    - dependencies that exist and are not terminal yet;
    ``unknown`` - dependencies whose target is gone (legacy or hand-edited data);
    ``cycle``   - a blocking chain that comes back to this item, when there is one;
    ``skipped`` - dependencies that were given up (r24/C): terminal, but they never
                  produced the input this item needs, so it can never run either.
    """

    open: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()
    cycle: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()

    @property
    def any(self) -> bool:
        return bool(self.open or self.unknown or self.cycle or self.skipped)


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
    #: M-C3: the fingerprint the last boot resume left behind.  Compared
    #: **boot to boot**: an unchanged fingerprint means the resume opened no
    #: round, produced no real message and settled no step.  A tuple of four
    #: ints (rounds, tokens, non-synthetic messages, settled steps), stored as
    #: a JSON list; written only when set.
    resume_fingerprint: tuple[int, ...] | None = None
    #: M-C3: consecutive resumes that made no progress.  Reset by any progress;
    #: at the limit the task is disarmed with a ``ResumeStalled`` report.
    no_progress_resumes: int = 0
    #: r24/C: the previous wave's handoff digest.  It is re-rendered into every
    #: request (the seed alone would be compacted away mid-wave), and written
    #: only when non-empty.  This is a plan-scoped contract, not a log.
    wave_note: str = ""
    #: r24/C: the plan episode's provider-reported tokens, kept separate from
    #: the goal episode's ``tokens_used`` so the two profiles cannot spend each
    #: other's budget.  Written only when non-zero.
    plan_tokens_used: int = 0
    #: C3: the last wave's structured handoff (``domain/plan_wave.py``).  One
    #: consumer -- the next wave's seed -- and only the newest wave is kept.
    #: Written only when non-empty, like every other optional key.
    wave_handoff: dict[str, Any] | None = None
    #: C3 lifetime state, never persisted: a wave is running right now, and
    #: which item ids it covers.  ``BaseTool.wave_only`` hides ``wave_report``
    #: until this is True, and the wire's ``wave`` block reads it.
    wave_active: bool = False
    wave_items: tuple[str, ...] = ()
    #: C3: the model's raw ``wave_report`` for the wave in progress.  The
    #: executor merges it with its own facts at wave end; never persisted.
    wave_report: dict[str, Any] | None = None

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
        # M-C3: the resume gate counts within an episode too.
        self.no_progress_resumes = 0
        self.resume_fingerprint = None
        self.armed = True
        self.armed_profile = "goal"

    def disarm(self) -> bool:
        """Give the task up: not armed, and owned by nobody.

        Nine places did this by hand, and they had to agree: an armed task
        with no profile is one the boot resume will not pick up
        (``server/world.py``'s profile whitelist), so a half-disarmed task is
        a task nobody finishes and nobody can see.  One method, so they
        cannot drift.

        Returns whether the task was armed -- the caller's cue that
        something changed and the state is worth persisting.  Idempotent:
        giving up a task twice reports the second time that there was
        nothing left to give up.
        """
        was_armed = bool(self.armed)
        self.armed = False
        self.armed_profile = ""
        return was_armed

    def add(
        self,
        content: str,
        *,
        depends_on: list[str] | None = None,
        verify: str = "",
        files: list[str] | None = None,
        kind: str = "write",
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
            files=list(files or []),
            kind=kind or "write",
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

    def skipped_dependencies(self, item: TodoItem) -> list[str]:
        """Ids this item waits on that were given up (r24/C).

        A skipped dependency is terminal, so ``unmet_dependencies`` no longer
        sees it -- but it never produced the input this item needs.  The wave
        executor propagates ``skipped`` along this edge; the render says so, so
        the model does not mistake the item for ready work.
        """
        out: list[str] = []
        for dep in item.depends_on:
            target = self.get(dep)
            if target is not None and target.status == "skipped":
                out.append(dep)
        return out

    def skip_dependents(self, item_id: str, *, ts: str = "") -> list[str]:
        """Give up every non-terminal item that depends on ``item_id``.

        A skipped dependency produced no input, so its dependents can never
        run -- and leaving them pending makes the plan look stalled instead of
        finished.  The walk is transitive (a skipped child skips its own
        children) and touches only non-terminal items, so a dependent that was
        already completed keeps its receipt.

        Returns the ids this call changed, in discovery order; the executor
        writes one ``skipped`` receipt per id.  Pure domain mutation: it emits
        nothing, because the runner owns the wire.
        """
        children: dict[str, list[str]] = {}
        for item in self.items:
            for dep in item.depends_on:
                children.setdefault(dep, []).append(item.id)
        skipped: list[str] = []
        seen: set[str] = {item_id}
        queue: list[str] = [item_id]
        while queue:
            parent = queue.pop(0)
            for child_id in children.get(parent, []):
                if child_id in seen:
                    continue
                seen.add(child_id)
                child = self.get(child_id)
                if child is None or child.is_terminal():
                    continue
                child.status = "skipped"
                child.evidence = f"dependency skipped: {parent}"
                if ts:
                    child.updated_at = ts
                skipped.append(child_id)
                queue.append(child_id)
        return skipped

    def focus(self) -> TodoItem | None:
        """The item to work on next: first unblocked open item, else first open."""
        open_items = self.open_items()
        if not open_items:
            return None
        for item in open_items:
            if not self.blockers(item.id).any:
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
        skipped: list[str] = []
        for dep in item.depends_on:
            target = self.get(dep)
            if target is None:
                unknown.append(dep)
            elif target.status == "skipped":
                skipped.append(dep)
            elif not target.is_terminal():
                open_ids.append(dep)
        cycle = self._cycle_from(item_id) if (open_ids or unknown) else ()
        return Blockers(
            open=tuple(open_ids), unknown=tuple(unknown), cycle=cycle,
            skipped=tuple(skipped),
        )

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
        if self.wave_note:
            note = self.wave_note.strip()
            if len(note) > WAVE_NOTE_MAX_CHARS:
                note = note[: WAVE_NOTE_MAX_CHARS - 1] + "…"
            lines.append(f"交接: {note}")
        if not self.items:
            lines.append("(无任务项)")
            return lines

        lines.append(f"进度: {self.done_count}/{self.total}")
        shown = self.items[:max_items]
        for item in shown:
            marker = _MARKERS.get(item.status, "[ ]")
            reasons: list[str] = []
            unmet = self.unmet_dependencies(item)
            if unmet:
                reasons.append("blocked by " + ", ".join(unmet))
            gone = self.skipped_dependencies(item)
            if gone:
                reasons.append("dependency skipped: " + ", ".join(gone))
            suffix = f" ({'; '.join(reasons)})" if reasons and not item.is_terminal() else ""
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
        # M-C3: same discipline -- a task with no resume history gains no key.
        if self.resume_fingerprint is not None:
            data["resumeFingerprint"] = [int(value) for value in self.resume_fingerprint]
        if self.no_progress_resumes:
            data["noProgressResumes"] = int(self.no_progress_resumes)
        if self.wave_note:
            data["waveNote"] = self.wave_note
        if self.wave_handoff:
            data["waveHandoff"] = dict(self.wave_handoff)
        if self.plan_tokens_used:
            data["planTokensUsed"] = int(self.plan_tokens_used)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskState:
        raw_items = data.get("items") or []
        items = [
            TodoItem.from_dict(entry)
            for entry in raw_items
            if isinstance(entry, dict) and entry.get("id")
        ]
        raw_fingerprint = data.get("resumeFingerprint")
        fingerprint = (
            tuple(int(value) for value in raw_fingerprint)
            if isinstance(raw_fingerprint, list)
            else None
        )
        return cls(
            goal=str(data.get("goal", "")),
            items=items,
            armed=bool(data.get("armed", False)),
            armed_profile=str(data.get("armedProfile", "") or ""),
            rounds_run=int(data.get("roundsRun", 0) or 0),
            resume_count=int(data.get("resumeCount", 0) or 0),
            tokens_used=int(data.get("tokensUsed", 0) or 0),
            goal_phase=str(data.get("goalPhase", "") or ""),
            resume_fingerprint=fingerprint,
            no_progress_resumes=int(data.get("noProgressResumes", 0) or 0),
            wave_note=str(data.get("waveNote", "") or ""),
            wave_handoff=(
                dict(data["waveHandoff"])
                if isinstance(data.get("waveHandoff"), dict)
                else None
            ),
            plan_tokens_used=int(data.get("planTokensUsed", 0) or 0),
        )


__all__ = [
    "TodoStatus",
    "VALID_STATUSES",
    "TERMINAL_STATUSES",
    "RENDER_MAX_ITEMS",
    "WAVE_NOTE_MAX_CHARS",
    "Blockers",
    "TodoItem",
    "TaskState",
]
