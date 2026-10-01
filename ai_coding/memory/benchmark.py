"""Retrieval evaluation primitives (R10a).

The yardstick for every scoring change in R10: labelled (query, relevant ids)
in, recall@k / MRR / false-positive share out.  Pure and offline — the corpus
lives in ``scripts/retrieval_corpus.json``, which is data, not logic.

Deliberately import-light: this module must not import ``store``, ``service``
or ``relevance``.  If it did, the benchmark would be measuring the very code
it is supposed to grade, and a refactor could move the numbers without any
change in retrieval quality.

Two of the metrics are worth naming precisely, because they answer different
questions:

  - ``false_positive_share``: of the notes a query actually got back, what
    fraction were not labelled relevant.  This is the "confident wrong answer"
    rate, and it is what the R10 gates are meant to move.
  - ``no_answer_ok``: how many of the questions with no answer in the corpus
    came back empty.  A retrieval change that raises recall by answering
    *everything* scores badly here, which is the point.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ai_coding.memory.note import MemoryNote, NoteKind

__all__ = ["EvalCase", "Metrics", "Ranker", "evaluate", "format_metrics", "load_corpus"]

Ranker = Callable[[str], Sequence[str]]


@dataclass(frozen=True)
class EvalCase:
    """One labelled query.  An empty ``relevant`` means nothing should answer it."""

    category: str
    query: str
    relevant: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Metrics:
    answerable: int = 0
    no_answer: int = 0
    recall: tuple[tuple[int, float], ...] = ()
    mrr: float = 0.0
    false_positive_share: float = 0.0
    no_answer_ok: int = 0
    missed: tuple[str, ...] = ()
    spurious: tuple[str, ...] = ()

    def recall_at(self, k: int) -> float:
        for size, value in self.recall:
            if size == k:
                return value
        return 0.0


def evaluate(
    rank: Ranker,
    cases: Sequence[EvalCase],
    *,
    ks: Sequence[int] = (1, 3, 5),
    fp_k: int = 5,
) -> Metrics:
    """Score one ranking function.

    A ranker that raises counts as "returned nothing" rather than killing the
    run: the harness has to survive the implementation it is grading.
    """
    depth = max(ks)
    hits = dict.fromkeys(ks, 0)
    reciprocal: list[float] = []
    fp_shares: list[float] = []
    missed: list[str] = []
    spurious: list[str] = []
    answerable = 0
    no_answer = 0
    for case in cases:
        try:
            returned = list(rank(case.query))
        except Exception:  # noqa: BLE001 — a broken ranker must not kill the harness
            returned = []
        if not case.relevant:
            no_answer += 1
            if returned[:fp_k]:
                spurious.append(case.query)
            continue
        answerable += 1
        # Recorded before the miss check on purpose: a query that returned five
        # notes and none of them relevant is the *worst* case, and it must not
        # be the one case FP@k never sees.
        top = returned[:fp_k]
        if top:
            junk = sum(1 for note_id in top if note_id not in case.relevant)
            fp_shares.append(junk / len(top))
        window = returned[:depth]
        position = next(
            (index for index, note_id in enumerate(window) if note_id in case.relevant),
            None,
        )
        if position is None:
            missed.append(case.query)
            continue
        rank_of = position + 1
        reciprocal.append(1 / rank_of)
        for k in ks:
            if rank_of <= k:
                hits[k] += 1
    denominator = answerable or 1
    return Metrics(
        answerable=answerable,
        no_answer=no_answer,
        recall=tuple((k, hits[k] / denominator) for k in ks),
        mrr=(sum(reciprocal) / denominator) if reciprocal else 0.0,
        false_positive_share=(sum(fp_shares) / len(fp_shares)) if fp_shares else 0.0,
        no_answer_ok=no_answer - len(spurious),
        missed=tuple(missed),
        spurious=tuple(spurious),
    )


def format_metrics(name: str, metrics: Metrics) -> str:
    """One table row, so the benchmark script and the acceptance script read alike."""
    parts = " ".join(f"R@{k}={metrics.recall_at(k):.2f}" for k, _ in metrics.recall)
    return (
        f"{name:24} {parts} MRR={metrics.mrr:.2f} "
        f"FP@5={metrics.false_positive_share:.2f} "
        f"no-answer={metrics.no_answer_ok}/{metrics.no_answer}"
    )


def load_corpus(path: str | Path, *, now: datetime) -> tuple[list[MemoryNote], list[EvalCase]]:
    """Read the JSON corpus into notes (with ages applied) and labelled questions.

    ``now`` is injected rather than read from the clock so recency is part of
    the fixture: the same corpus always produces the same timestamps, which is
    what lets two runs be compared.
    """
    data: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
    notes: list[MemoryNote] = []
    for entry in data.get("notes", []):
        created = (now - timedelta(days=int(entry.get("age_days", 0)))).isoformat()
        notes.append(
            MemoryNote(
                id=str(entry["id"]),
                text=str(entry["text"]),
                kind=NoteKind.parse(entry.get("kind", "fact")),
                importance=int(entry.get("importance", 3)),
                created_time=created,
                updated_time=created,
            )
        )
    cases = [
        EvalCase(
            category=str(entry.get("category", "lexical")),
            query=str(entry["query"]),
            relevant=frozenset(str(item) for item in entry.get("relevant", [])),
        )
        for entry in data.get("queries", [])
    ]
    return notes, cases
