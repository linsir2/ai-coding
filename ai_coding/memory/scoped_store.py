"""One store interface over two tiers: the repo's and the person's.

Everything used to live in ``<workspace>/.memory``, so a preference taught in
one project was gone in the next — which is why "change directory" read as "lose
the setup" for memory as well as for config and skills.

This class exists so that almost nothing else has to change. ``service.py``
touches ``self._store`` in 38 places; if this object answers to the same
interface as :class:`~ai_coding.memory.store.MemoryStore`, every one of those
sites keeps working and only the three that decide *whether to merge* need to
say which tier they mean.

Two properties are load-bearing, and both are here rather than in the service:

* **Dedupe is tier-scoped.** ``find_by_dedupe`` and ``find_similar`` take the
  tier they are about to write into and default to the workspace tier, which is
  what every caller before this meant. Searching the *union* instead would let a
  project fact — "this repo uses pytest" — match a user-tier "prefers pytest"
  and be merged into it, silently promoting project knowledge into every future
  project. Claude Code's #93960 is that class of leak with the serial numbers
  filed off.
* **Routing is conservative.** Only ``USER_SCOPE`` opens the user tier. A scope
  value nobody declared stays in the workspace, because a typo must not be able
  to write into the file that follows the person around.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ai_coding.memory.note import (
    DEFAULT_SCOPE,
    USER_SCOPE,
    MemoryNote,
    NoteKind,
)
from ai_coding.memory.relevance import RecallParams, ScoredNote, rank_notes
from ai_coding.memory.store import _DEFAULT_SIMILARITY, MemoryStore

__all__ = ["ScopedStore"]

#: How similar two notes must be, as text, to count as the same fact. Same
#: default the single-tier store uses, so behaviour does not shift with the
#: number of tiers.
_DEDUPE_SIMILARITY = float(_DEFAULT_SIMILARITY)


class ScopedStore:
    """The same surface as ``MemoryStore``, over a project tier and a user tier.

    Reads are the union of both; writes go to the tier the note's ``scope`` names.
    A ``MemoryStore`` whose file does not exist contributes nothing, so a machine
    that has never written a user-tier note behaves exactly as it did before —
    and ``user`` may be omitted entirely for a caller that only has one tier.
    """

    def __init__(self, project: MemoryStore, user: MemoryStore | None = None) -> None:
        self.project = project
        self.user = user

    # ---- tier resolution ---------------------------------------------------

    def _tier(self, scope: str) -> MemoryStore:
        """The store a note with this ``scope`` belongs to.

        ``USER_SCOPE`` and nothing else. Every other value — the default, a
        subagent's own marker, a typo — stays in the workspace. With no user tier
        configured there is nowhere else to go, so it collapses to the project.
        """
        if scope == USER_SCOPE and self.user is not None:
            return self.user
        return self.project

    def _holder(self, note_id: str) -> MemoryStore | None:
        """The store currently holding ``note_id``, or ``None``."""
        stores = [self.project] + ([self.user] if self.user is not None else [])
        for store in stores:
            if store.get(note_id) is not None:
                return store
        return None

    # ---- writes: routed by the note's scope --------------------------------

    def add_note(self, note: MemoryNote) -> str:
        return self._tier(note.scope).add_note(note)

    def update(self, note: MemoryNote) -> bool:
        holder = self._holder(note.id)
        return holder.update(note) if holder is not None else False

    def update_stake(self, note_id: str, new_stake: float) -> bool:
        holder = self._holder(note_id)
        return holder.update_stake(note_id, new_stake) if holder is not None else False

    def supersede(self, old_id: str, newer: MemoryNote) -> bool:
        holder = self._holder(old_id)
        return holder.supersede(old_id, newer) if holder is not None else False

    def pin(self, note_id: str, pinned: bool = True) -> MemoryNote | None:
        holder = self._holder(note_id)
        return holder.pin(note_id, pinned) if holder is not None else None

    def touch_notes(self, note_ids: list[str]) -> int:
        touched = 0
        for store, wanted in self._by_holder(note_ids):
            touched += store.touch_notes(wanted)
        return touched

    def delete(self, note_id: str) -> bool:
        holder = self._holder(note_id)
        return holder.delete(note_id) if holder is not None else False

    def clear(self) -> None:
        self.project.clear()
        if self.user is not None:
            self.user.clear()

    def evict(self, cap: int) -> list[str]:
        """Trim each tier to ``cap`` independently.

        One shared cap would let the user tier's entries evict the project's, or
        the reverse, and the loser would be whichever tier happened to be
        iterated first.
        """
        evicted = self.project.evict(cap)
        if self.user is not None:
            evicted = evicted + self.user.evict(cap)
        return evicted

    # ---- reads: the union --------------------------------------------------

    def all(self) -> list[MemoryNote]:
        every = self.project.all()
        if self.user is not None:
            every = every + self.user.all()
        return every

    def active(self, *, scope: str | None = None) -> list[MemoryNote]:
        """Both tiers, project first — or one tier when ``scope`` is given.

        ``active()`` is the union every read path wants. ``active(scope=...)``
        exists for the one place that must not cross tiers: consolidation pairs
        similar notes and retires one of them, and retiring a user-tier note in
        favour of a project-tier one would delete it from every other project
        while the replacement lived in only this one.

        Project first so that when the ranking has to break a tie it breaks
        toward the nearer scope; the actual tie-break is in
        ``relevance.order``, and this order only matters to calls that rank
        without it.
        """
        if scope == USER_SCOPE:
            return self.user.active() if self.user else []
        if scope is None:
            union = self.project.active()
            if self.user is not None:
                union = union + self.user.active()
            return union
        return self.project.active()

    @property
    def count(self) -> int:
        extra = self.user.count if self.user is not None else 0
        return self.project.count + extra

    def get(self, note_id: str) -> MemoryNote | None:
        found = self.project.get(note_id)
        return found if found is not None else (self.user.get(note_id) if self.user else None)

    def search_scored(
        self,
        query: str,
        *,
        params: RecallParams | None = None,
        now: datetime | None = None,
        limit: int | None = None,
    ) -> list[ScoredNote]:
        """Rank across both tiers, with the single ranking function.

        ``MemoryStore.search_scored`` is a thin delegate over
        ``rank_notes(self.active())``; this is the same call over the union, so
        the tiers cannot drift apart in how they are ordered.
        """
        return rank_notes(self.active(), query, params=params, now=now, limit=limit)

    # ---- dedupe: scoped to the tier about to be written --------------------

    def find_by_dedupe(
        self, dedupe_key: str, *, scope: str = DEFAULT_SCOPE
    ) -> MemoryNote | None:
        """The note with this key **in the tier being written into**.

        The default is the workspace tier, which is what every caller before
        layering meant by "is this fact already known".
        """
        store = self._tier(scope)
        return store.find_by_dedupe(dedupe_key)

    def find_similar(
        self,
        text: str,
        *,
        kind: NoteKind | None = None,
        threshold: float = _DEDUPE_SIMILARITY,
        scene: str | None = None,
        scope: str = DEFAULT_SCOPE,
    ) -> MemoryNote | None:
        """The closest active note of the same kind **in the tier being written
        into**, if it clears ``threshold``.

        Scoped on purpose. Searching the union would let a project fact merge
        into a user-tier note and travel to every other project from then on.
        """
        return self._tier(scope).find_similar(
            text, kind=kind, threshold=threshold, scene=scene
        )

    # ---- introspection (used by the panel) ----------------------------------

    def tiers(self) -> dict[str, list[MemoryNote]]:
        """``{"workspace": [...], "user": [...]}``, for anything that has to
        tell the operator which tier a note is in."""
        return {
            DEFAULT_SCOPE: self.project.all(),
            USER_SCOPE: self.user.all() if self.user is not None else [],
        }

    def by_id(self) -> dict[str, MemoryNote]:
        """Every note keyed by id, project tier first (so a display that iterates
        it shows the repo's own memories before the person's)."""
        found: dict[str, MemoryNote] = {}
        for note in (self.user.all() if self.user is not None else []):
            found[note.id] = note
        for note in self.project.all():
            found[note.id] = note
        return found

    # ---- the parts of the single-tier surface that have no union meaning ----

    def _by_holder(self, note_ids: list[str]) -> list[tuple[MemoryStore, list[str]]]:
        """Group ids by the store that holds them, for operations that must run
        per store."""
        buckets: dict[int, tuple[MemoryStore, list[str]]] = {}
        for note_id in note_ids:
            holder = self._holder(note_id)
            if holder is None:
                continue
            slot = buckets.setdefault(id(holder), (holder, []))
            slot[1].append(note_id)
        return list(buckets.values())


def _typed(store: Any) -> Any:  # pragma: no cover - documentation aid
    """Nothing to see here: kept so type checkers do not widen ``Any`` further."""
    return store
