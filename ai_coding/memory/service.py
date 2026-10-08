"""Memory service — recall, harvest and consolidation (R3a).

M3 had three actions (``remember`` / ``recall`` / ``dream``) that, measured against
ten cases (docs/r3-analysis.md), stored the same fact five times, ranked by
insertion order, kept contradictions alive, dropped the most important note first
and could not save an early decision from compaction (13 turns -> 100 junk notes).

This service keeps the three ideas and gives each a real implementation:

  - :meth:`recall_notes` — scored, thresholded and ranked, and it says *why*
    (id, kind, score, relevance) so a UI or a trace can explain an injection;
  - :meth:`harvest` — consumes **the turn's delta only** and reconciles every
    candidate against the index (ADD / UPDATE / SUPERSEDE / NOOP), so memory is a
    projection of the current state rather than an append-only log;
  - :meth:`consolidate` — merges near-duplicates, promotes frequently used notes,
    expires stale open threads and evicts by value rather than by age.

Plus :meth:`index_block`, the tiny always-on constraint block (R3b injects it at
the **tail** of the request, never into the system prompt — sections 1.13/1.16).

Invariants kept from M3: every public method degrades silently — memory never
raises at its callers.  "Never blocks the loop" is **no longer true** and is
dropped rather than softened: recall writes.  A hit calls
:meth:`MemoryStore.touch_notes`, which is a synchronous whole-file save, so a
turn now pays one file write per recall that returned something.  The invariant
that remains is the ``except Exception`` around each public path — a failing
recall degrades to ``[]``, never to an exception — and ``enabled=False`` means
"neither read nor write" (case 6).  ``harvest`` deliberately does **not** use the
offline fallback: without an extractor it writes nothing instead of storing raw
chatter.

``remember`` / ``recall`` / ``dream`` remain as deprecated M3-compatible wrappers.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from ai_coding.domain.message import ChatMessage
from ai_coding.memory.embeddings import EmbeddingIndex, EmbeddingsUnsupportedError
from ai_coding.memory.note import (
    MemoryNote,
    NoteKind,
    is_plausible_note_text,
    new_note,
    normalize_scene,
)
from ai_coding.memory.relevance import (
    RecallParams,
    ScoredNote,
    parse_time,
    rank_notes_detailed,
)
from ai_coding.memory.schema import HarvestCandidate
from ai_coding.memory.store import MemoryStore
from ai_coding.memory.tokenize import clean_query_for_recall, similarity

Extractor = Callable[
    [Sequence[ChatMessage], str | None],
    "list[Any] | Awaitable[list[Any]]",
]

__all__ = [
    "ConsolidateReport",
    "HarvestReport",
    "IndexSummary",
    "MemoryService",
    "RecallDiagnostics",
    "asyncio_guard",
    "is_extraction_allowed",
]

_PROMOTE_HITS = 3
_OPEN_THREAD_TTL_DAYS = 30.0

# The always-on block's own wrapper.  `index_max_chars` bounds the *rendered* block
# (that is what the model pays for), so the tags come out of the budget before any
# note does — R6e's regression test caught the bug where they did not.
_INDEX_OPEN = "<memory-constraints>"
_INDEX_CLOSE = "</memory-constraints>"
_INDEX_OVERHEAD = len(_INDEX_OPEN) + len(_INDEX_CLOSE) + 1


def _default_config(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "enabled": True,
        "auto_extract": True,
        "consolidate_threshold": 10,
        "consolidate_every_turns": 5,
        "max_index_entries": 200,
        "max_per_turn_injections": 3,
        "recall_threshold": 0.25,
        "recall_min_relevance": 0.25,
        "recall_relative_band": 0.55,
        "recall_fusion": "blend",
        "rrf_k": 60,
        "recency_half_life_days": 14.0,
        "recall_timeout_seconds": 2.0,
        "extraction_enabled": True,
        "extraction_extractors": None,
        "index_max_chars": 600,
        "detail_max_chars": 1200,
        "harvest_max_messages": 12,
        "dedupe_similarity": 0.80,
        "pinned_kinds": ["decision"],
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@dataclass(frozen=True)
class HarvestReport:
    """What one harvest did, with the ids needed to explain it."""

    added: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    superseded: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    evicted: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.superseded)


@dataclass(frozen=True)
class IndexSummary:
    """A bounded map of what is in memory, for the model to aim its next query at."""

    active: int = 0
    by_kind: Mapping[str, int] = field(default_factory=dict)
    recent: tuple[tuple[str, str, str], ...] = ()   # (id, kind, preview)
    # R11c: the same map one dimension over.  ``by_scene`` counts notes per
    # scene (ungrouped notes are not counted — "" is not a scene), and
    # ``top_scenes`` is the navigation line: ``(scene, count, heat)`` where heat
    # is the summed ``hits``, i.e. how often recall actually landed there.
    by_scene: Mapping[str, int] = field(default_factory=dict)
    top_scenes: tuple[tuple[str, int, int], ...] = ()

    #: How many scenes the miss payload may name.  A failed search must not
    #: hand the whole taxonomy back; five is enough to aim the next query.
    TOP_SCENES = 5


@dataclass(frozen=True)
class RecallDiagnostics:
    """Everything needed to explain a recall, and to retry it.

    ``semantic_unavailable`` is False in every case this round builds; it
    exists so the tool payload does not have to change shape when the
    optional embeddings tier lands.

    ``filtered_out`` counts what the *filters* cost a miss.  It cannot be
    ``len(ranked) - len(kept)`` any more, because filtering now happens before
    anything is scored and there is no longer a post-filter list to subtract
    from; when it is 0 there is nothing to blame on the filters.
    """

    terms_known: tuple[str, ...] = ()
    terms_unknown: tuple[str, ...] = ()
    near_misses: tuple[ScoredNote, ...] = ()
    index_summary: IndexSummary | None = None
    semantic_unavailable: bool = False
    no_vocabulary_match: bool = False
    filtered_out: int = 0
    # R11b: the recall budget.  `timed_out` means the ranking took longer than
    # `recall_timeout_seconds`, so the hits came back but the optional `touch`
    # write was skipped; `elapsed_seconds` is the measured rank time.
    timed_out: bool = False
    elapsed_seconds: float = 0.0


@dataclass(frozen=True)
class ConsolidateReport:
    """What one consolidation did."""

    merged: tuple[str, ...] = ()
    promoted: tuple[str, ...] = ()
    expired: tuple[str, ...] = ()
    evicted: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.merged or self.promoted or self.expired or self.evicted)


def _collect_recent_signal(messages: Sequence[ChatMessage]) -> list[str]:
    """Offline fallback signal (M3 behaviour, kept for the deprecated ``remember``)."""
    lines: list[str] = []
    for message in messages:
        if message.role not in ("user", "assistant"):
            continue
        body = (message.content or "").strip()
        if body:
            lines.append(body)
    return lines[-8:]


def is_extraction_allowed(config: Any, asset: str) -> bool:
    """The single semantic gate for the write side (R11a).

    ``asset`` is ``"harvest"`` (turn-end reconciliation) or ``"memory_write"``
    (the model's own write channel).  Missing or malformed configuration falls
    back to the historical always-on behaviour, so a partial yaml never
    silently disables writes; only an explicit opt-out does.
    """
    if getattr(config, "extraction_enabled", None) is False:
        return False
    extractors = getattr(config, "extraction_extractors", None)
    if extractors is None:
        return True
    if not isinstance(extractors, (list, tuple)):
        return True
    wanted = {str(item).strip().casefold() for item in extractors}
    return str(asset).strip().casefold() in wanted


class MemoryService:
    """Recalls, harvests and consolidates compact memories across turns."""

    def __init__(
        self,
        store: MemoryStore,
        extractor: Extractor | None = None,
        config: Any | None = None,
        embeddings: EmbeddingIndex | None = None,
    ) -> None:
        self._store = store
        self._extractor = extractor
        self._config = config or _default_config()
        self._embeddings = embeddings

    # ---- configuration helpers -------------------------------------------

    @property
    def store(self) -> MemoryStore:
        return self._store

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._config, "enabled", True))

    def _get(self, name: str, default: Any) -> Any:
        return getattr(self._config, name, default)

    def _params(self) -> RecallParams:
        return RecallParams.from_memory_config(self._config)

    # ---- recall -----------------------------------------------------------

    _PREVIEW_CHARS = 40
    _INDEX_RECENT = 3

    def index_summary(self) -> IndexSummary:
        """Counts by kind plus a few previews.  Bounded on purpose: a failed
        search must not return the whole index back into the context."""
        notes = self._store.active()
        by_kind: dict[str, int] = {}
        by_scene: dict[str, int] = {}
        heat: dict[str, int] = {}
        for note in notes:
            by_kind[note.kind.value] = by_kind.get(note.kind.value, 0) + 1
            if note.scene:
                by_scene[note.scene] = by_scene.get(note.scene, 0) + 1
                heat[note.scene] = heat.get(note.scene, 0) + note.hits
        newest = sorted(
            notes,
            key=lambda note: (_invert_time(note.updated_time or note.created_time), note.id),
        )[: self._INDEX_RECENT]
        # Busiest scenes first, name as the tie-break so two runs agree.
        top_scenes = tuple(
            (scene, by_scene[scene], heat[scene])
            for scene in sorted(heat, key=lambda name: (-heat[name], name))[
                : IndexSummary.TOP_SCENES
            ]
        )
        return IndexSummary(
            active=len(notes),
            by_kind=by_kind,
            recent=tuple(
                (note.id, note.kind.value, note.text[: self._PREVIEW_CHARS])
                for note in newest
            ),
            by_scene=by_scene,
            top_scenes=top_scenes,
        )

    def _candidates(
        self,
        *,
        kinds: Sequence[str] | None,
        evidence: str | None,
        scope: str | None,
        since_days: int | None,
        now: datetime,
        scene: str | None = None,
    ) -> list[MemoryNote]:
        """The set a query is allowed to match, decided before anything is scored.

        Filtering after the top-k is a real bug, not a style choice: ask for
        ``kind=decision`` with ``k=3`` when four facts outrank the one decision and
        the search returns nothing at all, while the note the model asked for is
        sitting right there in the index.

        ``since_days`` measures the window from ``now``, which is the caller's clock
        — the production path passes the real one.  ``0`` and negatives mean "no
        window" rather than a window nothing can be inside, and a note with no
        parsable ``updated_time`` *or* ``created_time`` is dropped rather than
        assumed inside the window, because a filter that lets unmeasurable notes
        through is a filter that lies about what it did.
        """
        wanted_kinds = {
            str(kind).strip().casefold() for kind in (kinds or []) if str(kind).strip()
        }
        wanted_evidence = str(evidence or "").strip().casefold()
        # 0 and negatives are not windows: a 0-day window would have to exclude
        # every note and a negative one puts the cutoff in the future, so both would
        # answer a question nobody asked.  They mean "no window", same as the tool
        # boundary's answer for them.
        #
        # `since_days or 0` rather than an `is not None` test: the annotated type is
        # `int | None` but callers are duck-typed, and `since_days=""` is what a
        # model writes for "I did not fill this in".  `float("")` raises, and this
        # whole call is wrapped in `recall_with_diagnostics`'s best-effort
        # `except Exception`, so the raise did not surface — it surfaced as a silent
        # empty recall, which is the one failure mode this path must not have.
        window = float(since_days or 0)
        cutoff = now - timedelta(days=window) if window > 0.0 else None
        selected: list[MemoryNote] = []
        for note in self._store.active():
            if wanted_kinds and note.kind.value not in wanted_kinds:
                continue
            if wanted_evidence == "tool" and (note.evidence or "") != "tool":
                continue
            if wanted_evidence == "model" and (note.evidence or "") == "tool":
                continue
            if scope is not None and note.scope != scope:
                continue
            if scene is not None and note.scene != scene:
                # R11c: exact match, like `scope`. The labels are free text the
                # model wrote, so a scene it guesses wrong matches nothing — and
                # `filtered_out` is what tells the model which filter did it.
                continue
            if cutoff is not None:
                stamp = parse_time(note.updated_time or note.created_time)
                if stamp is None or stamp < cutoff:
                    continue
            selected.append(note)
        return selected

    async def _semantic_scores(
        self, query: str, candidates: list[MemoryNote]
    ) -> tuple[dict[str, float] | None, bool]:
        """Cosines for the candidates, or (None, unavailable-flag).

        Returns ``(None, False)`` whenever the tier is off, unconfigured, or
        the endpoint has proven it cannot embed — all three are silent, because
        "no semantic opinion" is not an error.  Only a mid-call failure (network,
        timeout, malformed response) sets the flag, so the tool can say the tier
        was configured but unusable this time.
        """
        index = self._embeddings
        if index is None or not index.available() or not candidates:
            return None, False
        try:
            if await index.provider_lacks_embeddings():
                return None, False
            return await index.cosine_scores(query, candidates), False
        except EmbeddingsUnsupportedError:
            return None, False
        except Exception:  # noqa: BLE001 — recall is best-effort
            return None, True

    async def recall_with_diagnostics(
        self,
        query_or_history: str | Sequence[ChatMessage],
        *,
        k: int | None = None,
        now: datetime | None = None,
        limit: int | None = None,
        kinds: Sequence[str] | None = None,
        evidence: str | None = None,
        scope: str | None = None,
        since_days: int | None = None,
        scene: str | None = None,
    ) -> tuple[list[ScoredNote], RecallDiagnostics]:
        """Rank, and also report why — so a caller can say what to try next.

        The four filters decide the candidate set first (:meth:`_candidates`), so
        ``k`` is spent on notes the caller actually asked for.
        """
        if not self.enabled:
            return [], RecallDiagnostics()
        supply = int(self._get("max_per_turn_injections", 3)) if k is None else int(k)
        if supply <= 0:
            return [], RecallDiagnostics()
        query = _query_text(query_or_history)
        if not query.strip():
            return [], RecallDiagnostics()
        moment = now or _now()
        ceiling = supply if limit is None else limit
        try:
            started = time.monotonic()
            candidates = self._candidates(
                kinds=kinds,
                evidence=evidence,
                scope=scope,
                since_days=since_days,
                now=moment,
                scene=scene,
            )
            semantic, unavailable = await self._semantic_scores(query, candidates)
            result = rank_notes_detailed(
                candidates,
                query,
                params=self._params(),
                now=moment,
                limit=ceiling,
                semantic=semantic,
            )
            hits = list(result.kept)
            filtered_out = 0
            if not hits and any((kinds, evidence, scope, since_days, scene)):
                # `hits` is bound and empty, so the only question left is *which
                # measurement to report*.  The filtered ranking cannot answer it: a
                # filter that empties the candidate set leaves the ranking with no
                # corpus at all, and `rank_notes_detailed` returns a bare result for
                # that case — naming neither the known nor the unknown words.  The
                # miss text reads that absence as "the index knows every word you
                # used", which is the opposite of the truth.  The unfiltered index
                # can answer it either way, so it answers, hits or not: a hit makes
                # it a filtered miss, and a word it has never seen makes it a
                # vocabulary miss.
                probe = rank_notes_detailed(
                    self._store.active(),
                    query,
                    params=self._params(),
                    now=moment,
                    # No limit on purpose: `filtered_out` is a set size, and a slice
                    # would report min(cleared, k) in the same sentence as an index
                    # count of N.  This ranking only ever feeds diagnostic prose.
                    limit=None,
                    semantic=(
                        await self._semantic_scores(query, self._store.active())
                    )[0],
                )
                # Adopt unconditionally.  The probe is the only ranking here with
                # a corpus at all, so the filtered result carries no measurement
                # worth reporting: with an empty candidate set every query word is
                # trivially absent from it, which the miss text renders as "the
                # index knows every word you used" — the opposite of the truth.
                # The three-condition form discarded probes that carried no
                # diagnosis (reachable: an all-ASCII query whose words are all
                # known, filtered down to nothing — `terms_known` and
                # `terms_unknown` both empty, `no_vocabulary_match` False), and
                # those are exactly the cases the diagnosis exists for.
                result, filtered_out = probe, len(probe.kept)
            elapsed = time.monotonic() - started
        except Exception:  # noqa: BLE001 — recall is best-effort
            return [], RecallDiagnostics()
        try:
            timeout = float(self._get("recall_timeout_seconds", 2.0) or 0.0)
        except (TypeError, ValueError):
            timeout = 0.0  # unparseable config means no budget, never a failure
        timed_out = bool(timeout > 0.0 and elapsed > timeout)
        if hits and not timed_out:
            try:
                self._store.touch_notes([hit.note.id for hit in hits])
            except Exception:  # noqa: BLE001
                pass
        return hits, RecallDiagnostics(
            terms_known=result.terms_known,
            terms_unknown=result.terms_unknown,
            near_misses=result.rejected[:3],
            index_summary=self.index_summary(),
            no_vocabulary_match=result.no_vocabulary_match,
            filtered_out=filtered_out,
            semantic_unavailable=unavailable,
            timed_out=timed_out,
            elapsed_seconds=elapsed,
        )

    async def recall_notes(
        self,
        query_or_history: str | Sequence[ChatMessage],
        *,
        k: int | None = None,
        now: datetime | None = None,
        limit: int | None = None,
        kinds: Sequence[str] | None = None,
        evidence: str | None = None,
        scope: str | None = None,
        since_days: int | None = None,
        scene: str | None = None,
    ) -> list[ScoredNote]:
        """Rank memories for a query (or a message history); [] when disabled.

        Thin wrapper over :meth:`recall_with_diagnostics`, so the filters, the
        touch-on-hit and the suppression rules live in exactly one place.
        """
        hits, _diagnostics = await self.recall_with_diagnostics(
            query_or_history,
            k=k,
            now=now,
            limit=limit,
            kinds=kinds,
            evidence=evidence,
            scope=scope,
            since_days=since_days,
            scene=scene,
        )
        return hits

    # ---- the always-on constraint block ----------------------------------

    def index_block(
        self, *, scope: str | None = None, max_chars: int | None = None
    ) -> str:
        """Tiny block of pinned/decision notes (R3b injects it at the request tail)."""
        notes = self.index_notes(scope=scope, max_chars=max_chars)
        return self.render_index_block(notes)

    def index_notes(
        self, *, scope: str | None = None, max_chars: int | None = None
    ) -> list[MemoryNote]:
        """The notes the constraint block will carry (pinned first, then decisions)."""
        if not self.enabled:
            return []
        pinned_kinds = {
            str(kind).casefold() for kind in self._get("pinned_kinds", ["decision"])
        }
        budget = (
            int(self._get("index_max_chars", 600)) if max_chars is None else int(max_chars)
        )
        budget -= _INDEX_OVERHEAD
        if budget <= 0:
            return []
        selected = [
            note
            for note in self._store.active()
            if (note.pinned or note.kind.value in pinned_kinds)
            and (scope is None or note.scope == scope)
        ]
        selected.sort(
            key=lambda note: (
                not note.pinned,
                note.kind is not NoteKind.DECISION,
                _invert_time(note.created_time),
            )
        )
        chosen: list[MemoryNote] = []
        used = 0
        for note in selected:
            line = f"- [{note.kind.value}#{note.id[:8]}] {note.text}"
            if used + len(line) > budget and chosen:
                break
            chosen.append(note)
            used += len(line) + 1
            if used >= budget:
                break
        return chosen

    @staticmethod
    def render_index_block(notes: Sequence[MemoryNote]) -> str:
        """Render the constraint block (empty when there is nothing to say)."""
        if not notes:
            return ""
        lines = [f"- [{note.kind.value}#{note.id[:8]}] {note.text}" for note in notes]
        return "\n".join([_INDEX_OPEN, *lines, _INDEX_CLOSE])

    # ---- harvest ----------------------------------------------------------

    async def harvest(
        self,
        messages: Sequence[ChatMessage],
        *,
        turn_index: int = 0,
        session_id: str = "",
        summary: str | None = None,
        now: datetime | None = None,
        evidence: str = "",
        source_offset: int = 0,
    ) -> HarvestReport:
        """Reconcile this turn's delta into the index (never raises).

        ``source_offset`` is the index of ``messages[0]`` in the session's
        transcript, so the notes this call writes can point back at the exact
        messages they came from (R11c).
        """
        if not self.enabled:
            return HarvestReport()  # disabled means hands off, readers included
        if not is_extraction_allowed(self._config, "harvest"):
            return HarvestReport()  # gated off: leave the index untouched
        if not bool(self._get("auto_extract", True)) or self._extractor is None:
            # Nothing to extract, but the index cap is a store invariant.
            evicted = self._store.evict(int(self._get("max_index_entries", 200)))
            return HarvestReport(evicted=tuple(evicted))
        batch = list(messages)[-int(self._get("harvest_max_messages", 12)) :]
        # R11c: the positions this batch occupies in the session file.  Computed
        # from `batch` itself, *before* the synthetic hint/summary messages are
        # appended, so a ref can only name a message the extractor really read:
        # `source_offset` is where the caller's delta begins, and only the last
        # `harvest_max_messages` of it are shown.
        window_start = int(source_offset) + max(0, len(messages) - len(batch))
        refs = tuple(f"m{window_start + i}" for i in range(len(batch)))
        hint = self._known_memory_hint(batch)
        if hint:
            batch.append(ChatMessage(role="system", content=hint, timestamp=""))
        if summary:
            batch.append(ChatMessage(role="system", content=summary, timestamp=""))
        if not batch:
            return HarvestReport()
        try:
            raw = await asyncio_guard(self._extractor(batch, summary))
        except Exception:  # noqa: BLE001 — memory is best-effort
            return HarvestReport()
        candidates = [
            candidate
            for candidate in (_as_candidate(item) for item in raw)
            if candidate is not None
        ]
        if not candidates:
            return HarvestReport()
        return self._reconcile(
            candidates,
            turn_index=turn_index,
            session_id=session_id,
            now=now,
            evidence=evidence,
            source_refs=refs,
        )

    def write_note(
        self,
        text: str,
        *,
        kind: str = "fact",
        importance: int = 3,
        supersedes: str = "",
        turn: int = 0,
        session_id: str = "",
        scene: str = "",
    ) -> HarvestReport:
        """Write one memory the model asked for, through the same door as harvest (R7e-1).

        ``_reconcile`` is this service's only write path: dedupe, similarity merge,
        explicit supersede, protection and eviction all live there, so a
        model-written note cannot bypass any of them.  It carries
        ``evidence="model"`` so ``/memory`` can tell it from a harvested one, which is
        the R6d conclusion applied to the tool path (docs/r7e-analysis.md 3.1).
        """
        body = str(text or "").strip()
        if not self.enabled or not body:
            return HarvestReport()
        if not is_plausible_note_text(body):
            return HarvestReport()
        if not is_extraction_allowed(self._config, "memory_write"):
            return HarvestReport()
        target = str(supersedes or "").strip()
        candidate = HarvestCandidate(
            text=body,
            kind=NoteKind.parse(kind),
            importance=max(1, min(5, int(importance))),
            action="supersede" if target else "add",
            target_id=target or None,
            scene=normalize_scene(scene),
        )
        return self._reconcile(
            [candidate],
            turn_index=int(turn or 0),
            session_id=str(session_id or ""),
            now=None,
            evidence="model",
        )

    def _known_memory_hint(self, batch: Sequence[ChatMessage], limit: int = 5) -> str:
        """Show the extractor which existing notes this turn may be replacing.

        Without it a model cannot name a ``target_id``, so a contradiction such as
        "we moved from uv to poetry" (case 4) could only ever be added alongside.
        """
        query = clean_query_for_recall(" ".join(_collect_recent_signal(batch)))
        if not query.strip():
            return ""
        try:
            hits = self._store.search_scored(
                query, params=self._params(), now=_now(), limit=limit
            )
        except Exception:  # noqa: BLE001 — memory is best-effort
            return ""
        if not hits:
            return ""
        lines = ['<known-memory hint="取代或更新其中一条时，请在 JSON 里给出它的 id">']
        lines.extend(
            f"- [id={hit.note.id}] ({hit.note.kind.value}) {hit.note.text}" for hit in hits
        )
        lines.append("</known-memory>")
        return "\n".join(lines)

    def _reconcile(
        self,
        candidates: Sequence[HarvestCandidate],
        *,
        turn_index: int,
        session_id: str,
        now: datetime | None,
        evidence: str = "",
        source_refs: Sequence[str] = (),
    ) -> HarvestReport:
        added: list[str] = []
        updated: list[str] = []
        superseded: list[str] = []
        skipped: list[str] = []
        threshold = float(self._get("dedupe_similarity", 0.80))
        for candidate in candidates:
            text = candidate.text.strip()
            if not text:
                continue
            if not is_plausible_note_text(text):
                continue
            note = new_note(
                text,
                kind=candidate.kind,
                importance=candidate.importance,
                source_session=session_id,
                source_turn=turn_index,
                evidence=evidence,
                source_message_ids=source_refs,
                scene=candidate.scene,
            )
            if candidate.action == "noop":
                skipped.append(note.id)
                continue
            if candidate.action == "supersede" and candidate.target_id:
                if self._store.supersede(candidate.target_id, note):
                    superseded.append(note.id)
                else:
                    skipped.append(note.id)
                continue
            if candidate.action == "update" and candidate.target_id:
                target = self._store.get(candidate.target_id)
                if target is not None and self._store.update(
                    MemoryNote.from_dict({**target.to_dict(), "text": text})
                ):
                    updated.append(target.id)
                    continue
            existing = self._store.find_by_dedupe(note.dedupe_key)
            if existing is not None:
                # R11c: same content, so this is the *same* fact — but it may be
                # the first time anyone named its scene (harvest first, label
                # later). Backfilling is what keeps the scene map from staying
                # empty in exactly that, the most common, order.
                if note.scene and not existing.scene:
                    self._store.update(replace(existing, scene=note.scene))
                skipped.append(existing.id)
                continue
            similar = self._store.find_similar(
                text, kind=note.kind, scene=note.scene, threshold=threshold
            )
            if similar is not None:
                merged = MemoryNote.from_dict(
                    {
                        **similar.to_dict(),
                        "text": text if len(text) > len(similar.text) else similar.text,
                        "importance": max(similar.importance, note.importance),
                    }
                )
                self._store.update(merged)
                updated.append(similar.id)
                continue
            added.append(self._store.add_note(note))
        evicted = self._store.evict(int(self._get("max_index_entries", 200)))
        return HarvestReport(
            added=tuple(added),
            updated=tuple(updated),
            superseded=tuple(superseded),
            skipped=tuple(skipped),
            evicted=tuple(evicted),
        )

    # ---- consolidation ----------------------------------------------------

    async def consolidate(
        self, *, force: bool = False, now: datetime | None = None
    ) -> ConsolidateReport:
        """Merge, promote, expire and evict; skipped below the threshold."""
        if not self.enabled:
            return ConsolidateReport()
        threshold = int(self._get("consolidate_threshold", 10))
        if not force and self._store.count < threshold:
            return ConsolidateReport()
        moment = now or _now()
        similarity_threshold = float(self._get("dedupe_similarity", 0.80))
        merged: list[str] = []
        promoted: list[str] = []
        expired: list[str] = []
        active = self._store.active()
        retired: set[str] = set()
        for index, note in enumerate(active):
            if note.id in retired:
                continue
            for other in active[index + 1 :]:
                if other.id in retired or other.kind is not note.kind:
                    continue
                # R11c: scenes are separate groups.  Merging "the uv decision we
                # made for the build" into "the uv decision we made for the
                # release" would lose exactly the qualifier that makes either
                # note worth keeping.
                if other.scene != note.scene:
                    continue
                if similarity(note.text, other.text) < similarity_threshold:
                    continue
                keep, drop = _better_of(note, other)
                self._store.supersede(drop.id, keep.touch(moment.isoformat()))
                retired.add(drop.id)
                merged.append(drop.id)
        for note in self._store.active():
            if note.hits >= _PROMOTE_HITS and note.importance < 5:
                self._store.update(note.with_importance(note.importance + 1))
                promoted.append(note.id)
            elif note.kind is NoteKind.OPEN_THREAD and _age_days(
                note.created_time, moment
            ) > _OPEN_THREAD_TTL_DAYS:
                self._store.delete(note.id)
                expired.append(note.id)
        evicted = self._store.evict(int(self._get("max_index_entries", 200)))
        return ConsolidateReport(
            merged=tuple(merged),
            promoted=tuple(promoted),
            expired=tuple(expired),
            evicted=tuple(evicted),
        )

    # ---- introspection ----------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Counts for the CLI / REPL (never raises)."""
        notes = self._store.all()
        by_kind: dict[str, int] = {}
        by_evidence: dict[str, int] = {}
        by_scene: dict[str, int] = {}
        for note in notes:
            by_kind[note.kind.value] = by_kind.get(note.kind.value, 0) + 1
            key = note.evidence or "unknown"
            by_evidence[key] = by_evidence.get(key, 0) + 1
            if note.scene:
                by_scene[note.scene] = by_scene.get(note.scene, 0) + 1
        return {
            "total": len(notes),
            "active": len([note for note in notes if note.is_active]),
            "pinned": len([note for note in notes if note.pinned]),
            "by_kind": by_kind,
            # R6d: provenance, so /memory and the trace can say where memories came
            # from instead of only how many there are (docs/r6-analysis.md 2.4).
            "by_evidence": by_evidence,
            # R11c: the same for scenes; "" is not a scene and is not counted.
            "by_scene": by_scene,
            "enabled": self.enabled,
        }

    # ---- deprecated M3 surface -------------------------------------------

    async def recall(
        self, history: Sequence[ChatMessage], k: int | None = None
    ) -> list[str]:
        """Deprecated (M3): note texts. Use :meth:`recall_notes` for ids/scores."""
        hits = await self.recall_notes(history, k=k)
        return [hit.note.text for hit in hits]

    async def remember(
        self, messages: Sequence[ChatMessage], conclusion: str | None = None
    ) -> list[str]:
        """Deprecated (M3): extract and store. Use :meth:`harvest` instead."""
        if not self.enabled:
            return []
        try:
            texts = await self._remember_texts(messages, conclusion)
        except Exception:  # noqa: BLE001 — memory is best-effort
            return []
        ids: list[str] = []
        for text in texts:
            body = str(text).strip()
            if not body:
                continue
            note = new_note(body)
            if self._store.find_by_dedupe(note.dedupe_key) is not None:
                continue
            ids.append(self._store.add_note(note))
        self._store.evict(int(self._get("max_index_entries", 200)))
        return ids

    async def _remember_texts(
        self, messages: Sequence[ChatMessage], conclusion: str | None
    ) -> list[str]:
        if self._extractor is not None:
            results = await asyncio_guard(self._extractor(messages, conclusion))
            return [str(item) for item in results]
        return _collect_recent_signal(messages)

    async def dream(self, context: Sequence[ChatMessage] | None = None) -> list[str]:
        """Deprecated (M3): extract from ``context`` and tidy the index."""
        ids: list[str] = []
        if not self.enabled:
            return ids
        threshold = int(self._get("consolidate_threshold", 10))
        if self._store.count < threshold or self._extractor is None:
            return ids
        try:
            raw = await asyncio_guard(self._extractor(list(context or []), None))
        except Exception:  # noqa: BLE001 — memory is best-effort
            return ids
        for item in raw:
            candidate = _as_candidate(item)
            if candidate is None:
                continue
            note = new_note(
                candidate.text, kind=candidate.kind, importance=candidate.importance
            )
            if self._store.find_by_dedupe(note.dedupe_key) is None:
                ids.append(self._store.add_note(note))
        await self.consolidate(force=True)
        return ids


async def asyncio_guard(value: list[Any] | Awaitable[list[Any]]) -> list[Any]:
    """Coerce a sync or async extractor result into a list."""
    if hasattr(value, "__await__"):
        return list(await value)
    return list(value)


def _as_candidate(item: Any) -> HarvestCandidate | None:
    if isinstance(item, HarvestCandidate):
        return item if item.text.strip() else None
    body = str(item).strip()
    return HarvestCandidate(text=body) if body else None


def _query_text(source: str | Sequence[ChatMessage]) -> str:
    if isinstance(source, str):
        return clean_query_for_recall(source)
    return clean_query_for_recall("\n".join(_collect_recent_signal(source)))


def _better_of(left: MemoryNote, right: MemoryNote) -> tuple[MemoryNote, MemoryNote]:
    """Which of two near-duplicates to keep (value first, then recency)."""

    def rank(note: MemoryNote) -> tuple[int, int, str]:
        return (note.importance, note.hits, note.created_time)

    return (left, right) if rank(left) >= rank(right) else (right, left)


def _age_days(created_time: str, now: datetime) -> float:
    created = parse_time(created_time)
    if created is None:
        return 0.0
    return max(0.0, (now - created).total_seconds() / 86_400)


def _invert_time(created_time: str) -> str:
    """Sort key that puts newer timestamps first in an ascending sort."""
    if not created_time:
        return "\uffff"
    return "".join(chr(255 - ord(char)) for char in created_time)


def _now() -> datetime:
    return datetime.now(timezone.utc)
