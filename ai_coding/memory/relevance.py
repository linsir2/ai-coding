"""Recall: rank notes by BM25, then gate on relevance and on a share of the best score.

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

Ranking is **Okapi BM25** (:func:`bm25_scores`); the *relevance* that gates is still
**idf-weighted coverage** (:func:`coverage`), not BM25.  Those are two different jobs
and the split is deliberate:

  - **ranking** asks which note answers the question best, and BM25's term-frequency
    saturation and length normalisation are what make a short precise note beat a long
    vague one;
  - **relevance** asks how much of the query's informative part a note covers, which
    is a set question, and BM25 could not be normalised into it: its length
    normalisation gives short documents a bonus above the "ideal document", so the
    ratio saturates at 1.0 — measured on case 9, a short low-information note scored
    ``relevance=1.0`` and beat the longer note that actually answered the question.
    Coverage also only counts query terms the candidate set actually knows about, so
    CJK bigram noise cannot dilute a real match.

Two gates, two jobs, and they are not interchangeable:

  - ``min_relevance`` answers *is this note about the query at all*. 0.05 measured as
    no gate at all (see above);
  - ``relative_band`` answers *is this note in the running*, as a share of the best
    score in the same query. An absolute threshold cannot stand in for it: it does not
    transfer across queries. Measured on our own corpus the band is what moves FP@5
    (0.52 -> 0.35; removing it alone returns 0.40). The BM25 ranker is a wash at this
    corpus size — swapping it for plain coverage measured 0.32 — so it is kept for
    behaviour a 52-note corpus cannot demonstrate, not for a measured win.

:class:`RecallScorer` is the seam: a future embedding-based scorer implements the
same ``rank_notes`` call shape and the store/service layers do not change.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

from ai_coding.memory.note import MemoryNote
from ai_coding.memory.tokenize import tokenize

__all__ = [
    "RankingResult",
    "RecallParams",
    "RecallScorer",
    "ScoredNote",
    "bm25_scores",
    "coverage",
    "evidence_rank",
    "parse_time",
    "rank_notes",
    "rank_notes_detailed",
    "recency",
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
    """Tunable recall behaviour (defaults validated against the R3 cases).

    ``threshold`` and ``min_relevance`` are two different gates and both stay:
    the first is the floor on the blended score, the second is a hard floor on
    relevance alone. 0.05 measured as no gate at all (spec 1.1) — recency and
    importance sum to 0.35, above the 0.25 threshold — so it is 0.25 now.

    ``relative_band`` is the third gate, and note what it bands: the **blended
    score**, not relevance. Because that score carries ``0.20 * recency +
    0.15 * importance + kind_boost``, a note with ``relevance == 1.0`` can
    still be dropped for being old. So ``min_relevance = 0.25`` does *not* mean
    "kept if relevant" — relevance is necessary, not sufficient, and the two
    gates are not interchangeable in either direction.
    """

    threshold: float = 0.25
    min_relevance: float = 0.25
    relative_band: float = 0.55
    weight_rank: float = 0.55
    weight_recency: float = 0.20
    weight_importance: float = 0.15
    half_life_days: float = 14.0
    bm25_k1: float = 1.2
    bm25_b: float = 0.75
    recency_source: Literal["created", "accessed"] = "created"
    kind_boosts: Mapping[str, float] = field(
        default_factory=lambda: dict(DEFAULT_KIND_BOOSTS)
    )

    @classmethod
    def from_memory_config(cls, config: Any) -> RecallParams:
        """Build from ``MemoryConfig`` (duck-typed; fakes in tests work too)."""
        return cls(
            threshold=float(getattr(config, "recall_threshold", 0.25)),
            min_relevance=float(getattr(config, "recall_min_relevance", 0.25)),
            relative_band=float(getattr(config, "recall_relative_band", 0.55)),
            half_life_days=float(getattr(config, "recency_half_life_days", 14.0)),
        )


@dataclass(frozen=True)
class ScoredNote:
    """A note with the numbers that explain why it was (not) recalled."""

    note: MemoryNote
    score: float
    relevance: float
    matched_terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class RankingResult:
    """What a search kept, what it dropped, and why the two differ.

    ``rejected`` is not noise: it is the "closest but unproven" set that
    R10c hands back so a failed search can suggest a different query.
    """

    kept: tuple[ScoredNote, ...] = ()
    rejected: tuple[ScoredNote, ...] = ()
    terms_known: tuple[str, ...] = ()
    terms_unknown: tuple[str, ...] = ()
    no_vocabulary_match: bool = False


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


def bm25_scores(
    notes: Sequence[MemoryNote],
    query_tokens: Sequence[str],
    *,
    k1: float = 1.2,
    b: float = 0.75,
) -> dict[str, float]:
    """Okapi BM25 per note id.

    Ranking only — never a gate (spec 1.1: BM25's length normalisation saturates
    coverage at 1.0 for short notes, which is why relevance stays the gate).
    The caller is responsible for any threshold.

    Ids must be unique. The result is keyed by id, so two notes sharing one id
    cannot be told apart and their scores *sum* into a single entry. That is
    unreachable through :class:`~ai_coding.memory.store.MemoryStore`, which
    allocates unique ids, but this is a public function and the precondition is
    not enforced here — kept as documentation rather than a raise so that adding
    the check does not turn a silent merge into a new crash path on a hot loop.
    :func:`rank_notes_detailed` inherits the same precondition, and additionally
    reads these scores back per note, so a duplicate id there also hands both
    notes the merged score.
    """
    corpus = _corpus_of(notes)
    totals = {note.id: 0.0 for note in notes}
    if not corpus.n_docs:
        return totals
    for term in dict.fromkeys(query_tokens):
        freq_in_corpus = corpus.df.get(term, 0)
        if not freq_in_corpus:
            continue
        idf = _idf(term, corpus.df, corpus.n_docs)
        for index, tokens in enumerate(corpus.tokens):
            freq = tokens.count(term)
            if not freq:
                continue
            length = corpus.lengths[index] or 1
            norm = 1 - b + b * (length / corpus.avg_len)
            totals[corpus.notes[index].id] += idf * (freq * (k1 + 1)) / (freq + k1 * norm)
    return totals


@dataclass(frozen=True)
class _Corpus:
    """Everything BM25 needs, computed once per query instead of once per note."""

    notes: tuple[MemoryNote, ...]
    tokens: tuple[tuple[str, ...], ...]
    lengths: tuple[int, ...]
    avg_len: float
    df: Mapping[str, int]
    n_docs: int


def _corpus_of(notes: Sequence[MemoryNote]) -> _Corpus:
    tokenised = tuple(tuple(tokenize(note.text)) for note in notes)
    df: Counter[str] = Counter()
    for tokens in tokenised:
        df.update(set(tokens))
    lengths = tuple(len(tokens) for tokens in tokenised)
    return _Corpus(
        notes=tuple(notes),
        tokens=tokenised,
        lengths=lengths,
        avg_len=(sum(lengths) / len(lengths)) if lengths else 0.0,
        df=df,
        n_docs=len(notes),
    )


def evidence_rank(note: MemoryNote) -> int:
    """0 for a tool-backed note, 1 for everything else (R8b, D-3).

    It is a *tie-breaker*, not a weight: it is only consulted after the score and
    the relevance are equal, so it can never lift a weaker note over a stronger
    one (docs/r8-analysis.md 3.2).
    """
    return _EVIDENCE_RANK.get(str(getattr(note, "evidence", "") or ""), 1)


def _time_of(note: MemoryNote, params: RecallParams) -> str:
    if params.recency_source == "accessed" and note.last_used:
        return note.last_used
    return note.created_time


def rank_notes_detailed(
    notes: Sequence[MemoryNote],
    query: str,
    *,
    params: RecallParams | None = None,
    now: datetime | None = None,
    limit: int | None = None,
) -> RankingResult:
    """Rank with BM25, gate on relevance, then band against the best score.

    Two gates with two different jobs (spec 1.6): ``min_relevance`` decides
    whether a note is *about* the query at all, ``relative_band`` decides
    whether it is *in the running*. The band is a share of the best score in
    this query, which is why it cannot be replaced by an absolute threshold —
    an absolute one does not transfer across queries.

    The band applies to the blended score, not to relevance, so a note with
    ``relevance == 1.0`` can still be dropped for being old. And ``best_score``
    is taken over *every* scored note, before the ``min_relevance`` filter
    runs — so a note already rejected for irrelevance still raises the floor for
    everyone else. That ordering is deliberate (the band should not be defined
    in terms of a gate it might outrank) but it is a choice, not a forced one:
    no case has been found where it flips a keeper.
    """
    active = [note for note in notes if note.is_active]
    resolved = params or RecallParams()
    moment = now or datetime.now(timezone.utc)
    query_tokens = tokenize(query)
    if not query_tokens or not active:
        return RankingResult()

    corpus = _corpus_of(active)
    known = tuple(
        term for term in dict.fromkeys(query_tokens) if corpus.df.get(term, 0) > 0
    )
    unknown = tuple(
        term for term in dict.fromkeys(query_tokens) if corpus.df.get(term, 0) == 0
    )
    if not known:
        # Nothing in memory uses any of these words. Returning the freshest
        # notes would answer a question nobody asked (spec 1.1).
        return RankingResult(
            terms_unknown=unknown, no_vocabulary_match=True
        )

    raw = bm25_scores(active, query_tokens, k1=resolved.bm25_k1, b=resolved.bm25_b)
    best_raw = max(raw.values(), default=0.0) or 1.0
    scored: list[ScoredNote] = []
    for note in active:
        doc_tokens = tokenize(note.text)
        relevance = coverage(
            query_tokens, doc_tokens, df=corpus.df, n_docs=corpus.n_docs
        )
        freshness = recency(
            _time_of(note, resolved),
            now=moment,
            half_life_days=resolved.half_life_days,
        )
        importance = max(0.0, min((note.importance - 1) / 4, 1.0))
        boost = float(resolved.kind_boosts.get(note.kind.value, 0.0))
        score = (
            resolved.weight_rank * (raw.get(note.id, 0.0) / best_raw)
            + resolved.weight_recency * freshness
            + resolved.weight_importance * importance
            + boost
        )
        present = set(doc_tokens)
        scored.append(
            ScoredNote(
                note=note,
                score=score,
                relevance=relevance,
                matched_terms=tuple(term for term in known if term in present),
            )
        )

    # Over every scored note, not just the ones that cleared min_relevance --
    # see this function's docstring: an already-rejected note still sets the
    # floor for everyone else.
    best_score = max((item.score for item in scored), default=0.0)
    floor = resolved.relative_band * best_score
    kept: list[ScoredNote] = []
    rejected: list[ScoredNote] = []
    for item in scored:
        if (
            item.relevance >= resolved.min_relevance
            and item.score >= resolved.threshold
            and item.score >= floor
        ):
            kept.append(item)
        else:
            rejected.append(item)

    def order(item: ScoredNote) -> tuple[float, float, int, str]:
        return (-item.score, -item.relevance, evidence_rank(item.note), item.note.id)

    kept.sort(key=order)
    rejected.sort(key=order)
    if limit is not None and limit >= 0:
        kept = kept[:limit]
    return RankingResult(
        kept=tuple(kept),
        rejected=tuple(rejected),
        terms_known=known,
        terms_unknown=unknown,
    )


def rank_notes(
    notes: Iterable[MemoryNote],
    query: str,
    *,
    params: RecallParams | None = None,
    now: datetime | None = None,
    limit: int | None = None,
    scorer: RecallScorer | None = None,
) -> list[ScoredNote]:
    """Rank active notes for ``query``, dropping weak and irrelevant ones.

    The list-returning shape every existing caller already uses;
    ``rank_notes_detailed`` is where the diagnosis lives.
    """
    materialised = list(notes)
    resolved = params or RecallParams()
    moment = now or datetime.now(timezone.utc)
    if scorer is not None:
        return scorer(
            [note for note in materialised if note.is_active],
            query,
            params=resolved,
            now=moment,
            limit=limit,
        )
    return list(
        rank_notes_detailed(materialised, query, params=resolved, now=moment, limit=limit).kept
    )
