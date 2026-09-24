"""The memory note contract (R3a).

The M3 store kept ``{id, text, created_time}`` — enough to store a sentence, not
enough to *manage* one.  Cases 4 and 5 of docs/r3-analysis.md need more:

  - **kind** so decisions and preferences can be boosted and protected;
  - **scope** so two projects in one workspace do not contaminate each other;
  - **provenance** (session, turn) so a note can be explained and audited;
  - **supersedes / superseded_by** so a contradiction *replaces* the old note
    without destroying it (rollback stays possible);
  - **hits / last_used / pinned / importance** so eviction can be value-based.

``from_dict`` accepts v1 rows, so existing ``.memory`` files keep working.
"""

from __future__ import annotations

import enum
import hashlib
import uuid
from dataclasses import dataclass, replace
from typing import Any

from ai_coding.infra.time_utils import now_utc
from ai_coding.memory.tokenize import normalize

__all__ = ["DEFAULT_SCOPE", "MemoryNote", "NoteKind", "new_note"]

DEFAULT_SCOPE = "workspace"
_MIN_IMPORTANCE, _MAX_IMPORTANCE = 1, 5


class NoteKind(str, enum.Enum):
    """What kind of thing a note is (drives boosting, protection and eviction)."""

    FACT = "fact"
    DECISION = "decision"
    PREFERENCE = "preference"
    OPEN_THREAD = "open_thread"

    @classmethod
    def parse(cls, raw: object) -> NoteKind:
        if isinstance(raw, cls):
            return raw
        try:
            return cls(str(raw).strip().casefold())
        except ValueError:
            return cls.FACT


@dataclass(frozen=True)
class MemoryNote:
    """One durable memory, with enough metadata to be managed rather than stored."""

    id: str
    text: str
    kind: NoteKind = NoteKind.FACT
    scope: str = DEFAULT_SCOPE
    importance: int = 3
    created_time: str = ""
    updated_time: str = ""
    source_session: str = ""
    source_turn: int = 0
    supersedes: str | None = None
    superseded_by: str | None = None
    hits: int = 0
    last_used: str = ""
    pinned: bool = False
    keywords: tuple[str, ...] = ()
    # R6d: where the memory came from — "tool" (a tool call in that turn succeeded)
    # or "model" (the turn only talked).  Provenance for observation and for a UI
    # that wants to show it; deliberately *not* a filter, because the user's own
    # statements are our best memories and they involve no tool call
    # (docs/r6-analysis.md 1.4 / 2.4, D-R6-10).  "" means "recorded before R6".
    evidence: str = ""

    def __post_init__(self) -> None:
        if not str(self.id or "").strip():
            raise ValueError("memory note requires an id")
        if not str(self.text or "").strip():
            raise ValueError("memory note requires text")
        object.__setattr__(self, "text", str(self.text).strip())
        object.__setattr__(self, "kind", NoteKind.parse(self.kind))
        object.__setattr__(self, "scope", str(self.scope or DEFAULT_SCOPE))
        object.__setattr__(
            self,
            "importance",
            max(_MIN_IMPORTANCE, min(_MAX_IMPORTANCE, int(self.importance))),
        )

    # ---- derived ----------------------------------------------------------

    @property
    def dedupe_key(self) -> str:
        """Stable identity of the *content* (ignores case, spacing, lead-ins)."""
        return hashlib.sha1(normalize(self.text).encode("utf-8")).hexdigest()[:16]

    @property
    def is_active(self) -> bool:
        return not self.superseded_by

    # ---- transitions (frozen value object: every change returns a new note) --

    def touch(self, when: str | None = None) -> MemoryNote:
        """Record one successful recall."""
        return replace(self, hits=self.hits + 1, last_used=when or now_utc())

    def superseded_by_note(self, newer: MemoryNote, when: str | None = None) -> MemoryNote:
        """Mark this note replaced by ``newer`` — the original text is preserved."""
        return replace(self, superseded_by=newer.id, updated_time=when or now_utc())

    def with_importance(self, importance: int, when: str | None = None) -> MemoryNote:
        return replace(self, importance=importance, updated_time=when or now_utc())

    # ---- serialisation ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "text": self.text,
            "kind": self.kind.value,
            "scope": self.scope,
            "importance": self.importance,
            "created_time": self.created_time,
            "updated_time": self.updated_time,
            "source_session": self.source_session,
            "source_turn": self.source_turn,
            "hits": self.hits,
            "last_used": self.last_used,
            "pinned": self.pinned,
        }
        if self.supersedes:
            data["supersedes"] = self.supersedes
        if self.superseded_by:
            data["superseded_by"] = self.superseded_by
        if self.keywords:
            data["keywords"] = list(self.keywords)
        if self.evidence:
            data["evidence"] = self.evidence
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryNote:
        """Load a note; v1 rows (id/text/created_time) get sane defaults."""
        note_id = str(data.get("id", "") or "").strip()
        text = str(data.get("text", "") or "").strip()
        if not note_id or not text:
            raise ValueError("memory note needs both id and text")
        raw_keywords = data.get("keywords")
        keywords = (
            tuple(str(item) for item in raw_keywords)
            if isinstance(raw_keywords, (list, tuple))
            else ()
        )
        return cls(
            id=note_id,
            text=text,
            kind=NoteKind.parse(data.get("kind", NoteKind.FACT.value)),
            scope=str(data.get("scope", DEFAULT_SCOPE) or DEFAULT_SCOPE),
            importance=int(data.get("importance", 3) or 3),
            created_time=str(data.get("created_time", "") or ""),
            updated_time=str(data.get("updated_time", "") or ""),
            source_session=str(data.get("source_session", "") or ""),
            source_turn=int(data.get("source_turn", 0) or 0),
            supersedes=data.get("supersedes") or None,
            superseded_by=data.get("superseded_by") or None,
            hits=int(data.get("hits", 0) or 0),
            last_used=str(data.get("last_used", "") or ""),
            pinned=bool(data.get("pinned", False)),
            keywords=keywords,
            evidence=str(data.get("evidence", "") or ""),
        )


def new_note(
    text: str,
    *,
    kind: NoteKind | str = NoteKind.FACT,
    importance: int = 3,
    scope: str = DEFAULT_SCOPE,
    source_session: str = "",
    source_turn: int = 0,
    pinned: bool = False,
    evidence: str = "",
    note_id: str | None = None,
    when: str | None = None,
) -> MemoryNote:
    """Create a fresh note with an id and timestamps filled in."""
    stamp = when or now_utc()
    return MemoryNote(
        id=note_id or uuid.uuid4().hex,
        text=text,
        kind=NoteKind.parse(kind),
        scope=scope,
        importance=importance,
        created_time=stamp,
        updated_time=stamp,
        source_session=source_session,
        source_turn=source_turn,
        pinned=pinned,
        evidence=evidence,
    )
