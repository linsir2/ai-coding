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
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from ai_coding.infra.time_utils import now_utc
from ai_coding.memory.tokenize import normalize

__all__ = ["DEFAULT_SCOPE", "MemoryNote", "NoteKind", "new_note"]

DEFAULT_SCOPE = "workspace"
_MIN_IMPORTANCE, _MAX_IMPORTANCE = 1, 5
#: A scene label is a short topic the model wrote; 80 characters is far past any
#: sensible label and keeps a runaway answer from bloating every row.
_MAX_SCENE_CHARS = 80


def normalize_scene(value: Any) -> str:
    """One scene label, coerced and bounded.

    The extractor is a model, so this accepts whatever came back and never
    raises: a non-string is stringified, whitespace stripped, and anything longer
    than :data:`_MAX_SCENE_CHARS` cut.  Empty means "no scene" — the value every
    note written before R11c has, and the value a note the model wrote without
    one keeps.
    """
    text = value if isinstance(value, str) else ("" if value is None else str(value))
    return text.strip()[:_MAX_SCENE_CHARS]


def _refs(raw: Any) -> tuple[str, ...]:
    """Coerce a reference list, tolerating anything a hand-edited file holds."""
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in raw if str(item).strip())


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
    # R11c: the topic group this note belongs to (free text the extractor wrote,
    # e.g. 「依赖管理」).  It is a *grouping and filtering* key only — it is not
    # part of ``dedupe_key``, so two notes with identical text still collapse,
    # and it carries no weight in scoring.  "" means ungrouped.
    scene: str = ""
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
    # R11c: where this note came from inside the transcript, as **positions**.
    # ``ChatMessage`` carries no id, so a reference is ``m<index>`` into
    # ``sessions/{id}.json``'s ``messages`` — which is stable because messages
    # are only ever appended (compaction works on copies). ``source_session``
    # already names the file.  Empty means "no position to point at": a note the
    # model wrote this turn, or one from before this field existed.
    source_message_ids: tuple[str, ...] = ()
    # R6d: where the memory came from — "tool" (a tool call in that turn succeeded)
    # or "model" (the turn only talked).  Provenance for observation and for a UI
    # that wants to show it; deliberately *not* a filter, because the user's own
    # statements are our best memories and they involve no tool call
    # (docs/r6-analysis.md 1.4 / 2.4, D-R6-10).  "" means "recorded before R6".
    evidence: str = ""
    #: F1: stake for staking/slashing — higher = more validated. 1.0 = default.
    stake: float = 1.0

    def __post_init__(self) -> None:
        if not str(self.id or "").strip():
            raise ValueError("memory note requires an id")
        if not str(self.text or "").strip():
            raise ValueError("memory note requires text")
        object.__setattr__(self, "text", str(self.text).strip())
        if not is_plausible_note_text(self.text):
            raise ValueError("memory note text looks like a serialized record")
        object.__setattr__(self, "kind", NoteKind.parse(self.kind))
        object.__setattr__(self, "scope", str(self.scope or DEFAULT_SCOPE))
        object.__setattr__(self, "scene", normalize_scene(self.scene))
        object.__setattr__(
            self,
            "importance",
            max(_MIN_IMPORTANCE, min(_MAX_IMPORTANCE, int(self.importance))),
        )
        stake_val = float(self.stake) if self.stake is not None else 1.0
        object.__setattr__(self, "stake", max(0.0, stake_val))

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

    def with_stake(self, stake: float, when: str | None = None) -> MemoryNote:
        """Return a new note with updated stake."""
        return replace(self, stake=max(0.0, stake), updated_time=when or now_utc())

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
        if self.scene:
            data["scene"] = self.scene
        if self.keywords:
            data["keywords"] = list(self.keywords)
        if self.source_message_ids:
            data["source_message_ids"] = list(self.source_message_ids)
        if self.evidence:
            data["evidence"] = self.evidence
        if self.stake != 1.0:
            data["stake"] = round(self.stake, 4)
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
            scene=normalize_scene(data.get("scene", "")),
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
            source_message_ids=_refs(data.get("source_message_ids")),
            evidence=str(data.get("evidence", "") or ""),
            stake=float(data.get("stake", 1.0)) if data.get("stake") is not None else 1.0,
        )


_SERIALIZED_PREFIXES = ('{', '[', ']', '}', '"')


def is_plausible_note_text(text: str) -> bool:
    """A memory is a sentence, not a serialized record.

    One real turn put twelve JSON fragments into ``.memory``; the loader accepted
    any non-empty text, so they stayed.  Positional rule, not JSON parsing: every
    fragment starts with a brace, bracket or quote.
    """
    body = str(text or "").strip()
    return bool(body) and body[0] not in _SERIALIZED_PREFIXES


def new_note(
    text: str,
    *,
    kind: NoteKind | str = NoteKind.FACT,
    importance: int = 3,
    scope: str = DEFAULT_SCOPE,
    scene: str = "",
    source_session: str = "",
    source_turn: int = 0,
    pinned: bool = False,
    evidence: str = "",
    note_id: str | None = None,
    when: str | None = None,
    source_message_ids: Sequence[str] = (),
) -> MemoryNote:
    """Create a fresh note with an id and timestamps filled in."""
    stamp = when or now_utc()
    return MemoryNote(
        id=note_id or uuid.uuid4().hex,
        text=text,
        kind=NoteKind.parse(kind),
        scope=scope,
        scene=scene,
        importance=importance,
        created_time=stamp,
        updated_time=stamp,
        source_session=source_session,
        source_turn=source_turn,
        pinned=pinned,
        evidence=evidence,
        source_message_ids=_refs(source_message_ids),
    )
