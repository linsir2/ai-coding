"""Disk-backed memory index, schema v2 (R3a).

M3 kept an append-only list of ``{id, text, created_time}``: it could store a
sentence but not manage one, which is why the same fact landed five times (case 3),
contradictions coexisted (case 4) and FIFO trimming deleted the *most important*
note first (case 5) — see docs/r3-analysis.md.

v2 stores :class:`~ai_coding.memory.note.MemoryNote` values and adds what those
cases need:

  - **content identity** (``dedupe_key``): adding the same fact twice is a no-op;
  - **similarity lookup** so a re-worded fact updates instead of duplicating;
  - **supersede** so a contradiction retires the old note *without deleting it*;
  - **value-based eviction** (importance, hits, recency) with pinned/decision
    protection instead of "drop the oldest";
  - **scored search** through :func:`ai_coding.memory.relevance.rank_notes`.

Invariants kept from M3: writes are atomic (tmp + ``os.replace``), a v1 file is
read-migrated, and **no method ever raises for an IO problem**.

The M3 API (``add`` / ``search`` / ``delete`` / ``clear``) survives as a deprecated
compatibility surface, so existing callers keep working unchanged.
"""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from ai_coding.infra.json_utils import from_json_file, to_json_pretty
from ai_coding.infra.time_utils import now_utc
from ai_coding.memory.note import MemoryNote, NoteKind, new_note
from ai_coding.memory.relevance import (
    RecallParams,
    ScoredNote,
    parse_time,
    rank_notes,
    recency,
)
from ai_coding.memory.tokenize import similarity

__all__ = ["MemoryEntry", "MemoryStore", "SCHEMA_VERSION"]

SCHEMA_VERSION = 2
_DEFAULT_SIMILARITY = 0.80
_EVICTION_HALF_LIFE_DAYS = 30.0

_T = TypeVar("_T")

# Backwards-compatible alias: M3 exported ``MemoryEntry``.
MemoryEntry = MemoryNote


def _load(path: Path) -> list[MemoryNote]:
    """Read v2 (``notes``) or migrate v1 (``entries``); never raise."""
    if not path.is_file():
        return []
    try:
        data = from_json_file(path)
    except (ValueError, OSError):
        return []
    if not isinstance(data, dict):
        return []
    raw: Any = data.get("notes")
    if not isinstance(raw, list):
        raw = data.get("entries")  # v1 migration (read-only)
    if not isinstance(raw, list):
        return []
    notes: list[MemoryNote] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            notes.append(MemoryNote.from_dict(item))
        except (ValueError, TypeError):
            continue
    return notes


class MemoryStore:
    """A disk-backed memory index that never raises on IO errors.

    Concurrent **reads** are safe: every read path takes the lock.  Concurrent
    **writes** are not, and this class does not claim they are.

    :meth:`_mutate` releases the lock *before* :meth:`_save`, and ``_save`` reads
    ``self._notes`` while holding none.  Two threads therefore serialise onto the
    **same** ``<path>.tmp``; ``os.replace`` renames that inode to the live path while
    the second thread still has it open, so its remaining writes land on the live
    file and append a second JSON document.  Measured (4 threads, barrier before
    **every** write): 4-6 of 25 trials left a file that ``json.loads`` rejects, and
    ``_load`` swallows that ``ValueError`` and returns ``[]`` — **every note gone,
    not one**.  A barrier only before the write *loop* measures 0/25: the race needs
    the two saves to overlap per write, not merely to start together.  Without any
    synchronisation the rate is 3/200.  A single-writer store is fine; that is
    every real path in this repo.

    Fixing it means holding the lock across the save, which changes the persistence
    shape for every caller.  R10 reviewed this and chose to record it rather than
    restructure persistence inside a retrieval round.

    **Do not move a store write onto a thread.** Recall touches notes on the read
    path, and the obvious way to stop that blocking the event loop is
    ``asyncio.to_thread(store.touch_notes, ids)``.  That is the one change that
    turns this from latent into live: today every path runs on the single event
    loop thread and ``recall_with_diagnostics`` contains no ``await``, so the writes
    serialise; off-thread they stop doing so.  Move the save inside the lock (or
    to a per-write tmp path) *before* anything touches this store from a thread.
    ``scripts/r10_cases.py::recall_survives_concurrent_harvest`` runs concurrent
    recalls against concurrent harvests and reports what it sees, which is
    **survival, not the absence of this hazard** — it imposes no per-write barrier,
    so it cannot exhibit the corruption, and 32 runs of it never did.
    """

    def __init__(self, path: str | Path = Path(".memory")) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()
        self._notes: list[MemoryNote] = _load(self._path)

    # ---- persistence ------------------------------------------------------

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = to_json_pretty(
                {
                    "version": SCHEMA_VERSION,
                    "notes": [note.to_dict() for note in self._notes],
                }
            )
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            pass  # memory persistence is best-effort; never raise

    def _mutate(self, fn: Callable[[], _T]) -> _T:
        """Run a mutation and swallow any persistence error (memory never raises)."""
        with self._lock:
            result = fn()
        try:
            self._save()
        except Exception:  # noqa: BLE001 — persistence is best-effort
            pass
        return result

    # ---- creation / updates ----------------------------------------------

    def add_note(self, note: MemoryNote) -> str:
        """Store ``note`` unless its content is already known; return the id used."""

        def _do() -> str:
            for index, existing in enumerate(self._notes):
                if existing.dedupe_key != note.dedupe_key:
                    continue
                better = replace(
                    existing,
                    importance=max(existing.importance, note.importance),
                    pinned=existing.pinned or note.pinned,
                )
                if better != existing:
                    self._notes[index] = better
                return existing.id
            self._notes.append(note)
            return note.id

        return self._mutate(_do)

    def update(self, note: MemoryNote) -> bool:
        """Replace the stored note with the same id; ``False`` if it is unknown."""
        return self._mutate(lambda: _replace_by_id(self._notes, note))

    def supersede(self, old_id: str, newer: MemoryNote) -> bool:
        """Retire ``old_id`` in favour of ``newer`` (which is stored too)."""

        def _do() -> bool:
            old = next((n for n in self._notes if n.id == old_id), None)
            if old is None:
                return False
            if not any(n.dedupe_key == newer.dedupe_key for n in self._notes):
                self._notes.append(newer)
            _replace_by_id(self._notes, old.superseded_by_note(newer))
            return True

        return self._mutate(_do)

    def pin(self, note_id: str, pinned: bool = True) -> MemoryNote | None:
        """Mark a note as protected from eviction (or clear the flag)."""

        def _do() -> MemoryNote | None:
            for index, note in enumerate(self._notes):
                if note.id != note_id:
                    continue
                updated = replace(note, pinned=pinned, updated_time=now_utc())
                self._notes[index] = updated
                return updated
            return None

        return self._mutate(_do)

    def touch_notes(self, note_ids: Sequence[str]) -> int:
        """Record one use per id; return how many were actually touched.

        Batched through ``_mutate`` so a five-hit recall writes the file once.
        Unknown ids are ignored rather than raising: this is bookkeeping on the
        read path and must never become a way for recall to fail.
        """
        wanted = set(note_ids)
        if not wanted:
            return 0

        def _do() -> int:
            stamp = now_utc()
            touched = 0
            for index, note in enumerate(self._notes):
                if note.id not in wanted:
                    continue
                self._notes[index] = note.touch(stamp)
                touched += 1
            return touched

        return self._mutate(_do)

    def delete(self, note_id: str) -> bool:
        """Delete by id; returns True when removed."""
        return self._mutate(lambda: _drop_by_id(self._notes, note_id))

    def clear(self) -> None:
        self._mutate(lambda: self._notes.clear())

    # ---- reads ------------------------------------------------------------

    def all(self) -> list[MemoryNote]:
        with self._lock:
            return list(self._notes)

    def active(self) -> list[MemoryNote]:
        """Notes that are not superseded (what recall and eviction consider)."""
        return [note for note in self.all() if note.is_active]

    def get(self, note_id: str) -> MemoryNote | None:
        with self._lock:
            return next((note for note in self._notes if note.id == note_id), None)

    def find_by_dedupe(self, dedupe_key: str) -> MemoryNote | None:
        with self._lock:
            return next(
                (note for note in self._notes if note.dedupe_key == dedupe_key), None
            )

    def find_similar(
        self,
        text: str,
        *,
        kind: NoteKind | None = None,
        threshold: float = _DEFAULT_SIMILARITY,
    ) -> MemoryNote | None:
        """Most similar *active* note of the same kind, if it clears ``threshold``."""
        best: MemoryNote | None = None
        best_score = threshold
        for note in self.active():
            if kind is not None and note.kind is not kind:
                continue
            score = similarity(text, note.text)
            if score >= best_score and (best is None or score > best_score):
                best, best_score = note, score
        return best

    def search_scored(
        self,
        query: str,
        *,
        params: RecallParams | None = None,
        now: datetime | None = None,
        limit: int | None = None,
    ) -> list[ScoredNote]:
        """Rank active notes for ``query`` (the R3 retrieval path)."""
        return rank_notes(self.active(), query, params=params, now=now, limit=limit)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._notes)

    def __len__(self) -> int:
        return self.count

    # ---- eviction ---------------------------------------------------------

    def evict(self, cap: int) -> list[str]:
        """Trim to ``cap`` notes, dropping the least valuable ones first.

        ``pinned`` notes and decisions are kept unless the cap cannot be honoured
        otherwise (case 5: the security rule used to be the first thing deleted).
        """

        def _do() -> list[str]:
            if cap < 0 or len(self._notes) <= cap:
                return []
            moment = _now()
            excess = len(self._notes) - cap
            removable = [n for n in self._notes if not _is_protected(n)]
            removable.sort(key=lambda n: (_value_of(n, moment), n.created_time, n.id))
            victims = removable[:excess]
            if len(victims) < excess:
                # Protected notes alone exceed the cap: fall back to pure value.
                pool = sorted(
                    self._notes, key=lambda n: (_value_of(n, moment), n.created_time, n.id)
                )
                victims = pool[:excess]
            victim_ids = {note.id for note in victims}
            self._notes = [n for n in self._notes if n.id not in victim_ids]
            return list(victim_ids)

        return self._mutate(_do)

    # ---- deprecated M3 surface -------------------------------------------

    def add(self, text: str) -> str:
        """Deprecated (M3): append one fact note. Use :meth:`add_note` instead."""
        body = (text or "").strip()
        if not body:
            return ""
        note_id = uuid.uuid4().hex
        self._mutate(lambda: self._notes.append(new_note(body, note_id=note_id)))
        return note_id

    def search(self, keywords: Sequence[str], limit: int = 20) -> list[MemoryNote]:
        """Deprecated (M3): substring match in insertion order, no ranking.

        Use :meth:`search_scored` (relevance + recency + importance + threshold).
        """
        if not keywords:
            return []
        keys = [key.casefold() for key in keywords if (key or "").strip()]
        if not keys:
            return []
        hits: list[MemoryNote] = []
        with self._lock:
            for note in self._notes:
                body = note.text.casefold()
                if any(key in body for key in keys):
                    hits.append(note)
                if len(hits) >= limit:
                    break
        return hits


def _replace_by_id(notes: list[MemoryNote], note: MemoryNote) -> bool:
    for index, current in enumerate(notes):
        if current.id == note.id:
            notes[index] = note
            return True
    return False


def _drop_by_id(notes: list[MemoryNote], note_id: str) -> bool:
    for index, current in enumerate(notes):
        if current.id == note_id:
            notes.pop(index)
            return True
    return False


def _is_protected(note: MemoryNote) -> bool:
    return note.pinned or note.kind is NoteKind.DECISION


def _value_of(note: MemoryNote, moment: datetime) -> float:
    """Higher is more worth keeping: importance, usage, freshness."""
    freshness = recency(
        note.created_time, now=moment, half_life_days=_EVICTION_HALF_LIFE_DAYS
    )
    return note.importance + 0.5 * min(note.hits, 4) + 0.5 * freshness


def _now() -> datetime:
    return parse_time(now_utc()) or datetime.now(timezone.utc)
