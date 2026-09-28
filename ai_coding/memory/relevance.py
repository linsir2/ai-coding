"""Recall scoring: relevance + recency + importance + kind, gated by a threshold.

Replaces ``MemoryStore.search`` (substring match, "break on first hit") which
returned the *oldest* matches in insertion order (cases 2 and 9 of
docs/r3-analysis.md) and injected anything that shared one common word (case 10).

Two prototype findings are encoded here as first-class rules:

  - the **relevance gate** (``min_relevance``): recency and importance alone add up
    to 0.35, which is above the default score threshold, so without a hard
    relevance floor an *irrelevant* but fresh and important note still gets
    injected (prototype G1);
  - parameters are explicit values on :class:`RecallParams`, so tuning never needs
    a call-site change and tests can pin behaviour.

Relevance is **idf-weighted coverage**, not BM25.  BM25 had to be normalised into
``[0,1]`` to fit the weighted sum, and its length normalisation gives short
documents a bonus above the "ideal document", so the ratio saturates at 1.0:
measured on case 9, a short low-information note scored ``relevance=1.0`` and beat
the longer note that actually answered the question.  Coverage asks the recall
question directly — *how much of the informative part of the query does this note
cover* — and only counts query terms the candidate set actually knows about, so
CJK bigram noise cannot dilute a real match.

:class:`RecallScorer` is the seam: a future embedding-based scorer implements the
same ``rank_notes`` call shape and the store/service layers do not change.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from ai_coding.memory.note import MemoryNote
from ai_coding.memory.tokenize import tokenize

__all__ = [
    "RecallParams",
    "RecallScorer",
    "ScoredNote",
    "coverage",
    "evidence_rank",
    "parse_time",
    "rank_notes",
    "recency",
    "score_note",
]

DEFAULT_KIND_BOOSTS: Mapping[str, float] = {
    "decision": 0.25,
    "preference": 0.15,
    "open_thread": 0.10,
}

#: Where a note came from, as a tie-breaker only (R8b, D-3).  `""` is data
#: harvested before R6d recorded provenance, so it ranks with "model" - the
#: tie-break must never punish an old row for missing a field that did not exist.
_EVIDENCE_RANK: Mapping[str, int] = {"tool": 0, "model": 1, "": 1}


@dataclass(frozen=True)
class RecallParams:
    """Tunable recall behaviour (defaults validated against the R3 cases)."""

    threshold: float = 0.25
    min_relevance: float = 0.05
    weight_relevance: float = 0.55
    weight_recency: float = 0.20
    weight_importance: float = 0.15
    half_life_days: float = 14.0
    kind_boosts: Mapping[str, float] = field(
        default_factory=lambda: dict(DEFAULT_KIND_BOOSTS)
    )

    @classmethod
    def from_memory_config(cls, config: Any) -> RecallParams:
        """Build from ``MemoryConfig`` (duck-typed; fakes in tests work too)."""
        return cls(
            threshold=float(getattr(config, "recall_threshold", 0.25)),
            min_relevance=float(getattr(config, "recall_min_relevance", 0.05)),
            half_life_days=float(getattr(config, "recency_half_life_days", 14.0)),
        )


@dataclass(frozen=True)
class ScoredNote:
    """A note with the two numbers that explain why it was (not) recalled."""

    note: MemoryNote
    score: float
    relevance: float


class RecallScorer(Protocol):
    """Seam for a future scorer (e.g. embeddings) — same shape as ``rank_notes``."""

    def __call__(
        self,
        notes: Sequence[MemoryNote],
        query: str,
        *,
        params: RecallParams,
        now: datetime,
        limit: int | None = ...,
    ) -> list[ScoredNote]: ...


@dataclass(frozen=True)
class _Stats:
    df: Counter[str]
    n_docs: int


def parse_time(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp (``Z`` or offset), ``None`` when unusable."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def recency(created_time: str, *, now: datetime, half_life_days: float) -> float:
    """``0.5 ** (age / half_life)`` in ``(0, 1]``; an unknown age counts as fresh."""
    created = parse_time(created_time)
    if created is None:
        return 1.0
    age_days = max(0.0, (now - created).total_seconds() / 86_400)
    if half_life_days <= 0:
        return 1.0
    return float(0.5 ** (age_days / half_life_days))


def _idf(term: str, df: Mapping[str, int], n_docs: int) -> float:
    freq = int(df.get(term, 0))
    return math.log(1 + (n_docs - freq + 0.5) / (freq + 0.5))


def coverage(
    query_tokens: Sequence[str],
    doc_tokens: Sequence[str],
    *,
    df: Mapping[str, int],
    n_docs: int,
) -> float:
    """IDF-weighted fraction of the query's corpus-known terms this document covers."""
    if not query_tokens or n_docs <= 0:
        return 0.0
    known = [term for term in set(query_tokens) if int(df.get(term, 0)) > 0]
    if not known:
        return 0.0
    total = sum(_idf(term, df, n_docs) for term in known)
    if total <= 0:
        return 0.0
    present = set(doc_tokens)
    hit = sum(_idf(term, df, n_docs) for term in known if term in present)
    return max(0.0, min(hit / total, 1.0))


def _stats(notes: Sequence[MemoryNote]) -> _Stats:
    tokenised = [tokenize(note.text) for note in notes]
    df: Counter[str] = Counter()
    for tokens in tokenised:
        df.update(set(tokens))
    return _Stats(df=df, n_docs=len(notes))


def evidence_rank(note: MemoryNote) -> int:
    """0 for a tool-backed note, 1 for everything else (R8b, D-3).

    It is a *tie-breaker*, not a weight: it is only consulted after the score and
    the relevance are equal, so it can never lift a weaker note over a stronger
    one (docs/r8-analysis.md 3.2).
    """
    return _EVIDENCE_RANK.get(str(getattr(note, "evidence", "") or ""), 1)


def score_note(
    note: MemoryNote,
    query_tokens: Sequence[str],
    *,
    stats: _Stats,
    params: RecallParams,
    now: datetime,
) -> tuple[float, float]:
    """Return ``(score, relevance)`` for one note."""
    relevance = coverage(
        query_tokens, tokenize(note.text), df=stats.df, n_docs=stats.n_docs
    )
    freshness = recency(
        note.created_time, now=now, half_life_days=params.half_life_days
    )
    importance = max(0.0, min((note.importance - 1) / 4, 1.0))
    boost = float(params.kind_boosts.get(note.kind.value, 0.0))
    score = (
        params.weight_relevance * relevance
        + params.weight_recency * freshness
        + params.weight_importance * importance
        + boost
    )
    return score, relevance


def rank_notes(
    notes: Iterable[MemoryNote],
    query: str,
    *,
    params: RecallParams | None = None,
    now: datetime | None = None,
    limit: int | None = None,
    scorer: RecallScorer | None = None,
) -> list[ScoredNote]:
    """Rank active notes for ``query``, dropping weak and irrelevant ones."""
    active = [note for note in notes if note.is_active]
    resolved = params or RecallParams()
    moment = now or datetime.now(timezone.utc)
    if scorer is not None:
        return scorer(active, query, params=resolved, now=moment, limit=limit)
    query_tokens = tokenize(query)
    if not query_tokens or not active:
        return []
    stats = _stats(active)
    scored: list[ScoredNote] = []
    for note in active:
        score, relevance = score_note(
            note, query_tokens, stats=stats, params=resolved, now=moment
        )
        if relevance < resolved.min_relevance:  # G1: relevance is its own gate
            continue
        if score < resolved.threshold:
            continue
        scored.append(ScoredNote(note=note, score=score, relevance=relevance))
    scored.sort(
        key=lambda item: (
            -item.score,
            -item.relevance,
            evidence_rank(item.note),
            item.note.id,
        )
    )
    if limit is not None and limit >= 0:
        return scored[:limit]
    return scored
