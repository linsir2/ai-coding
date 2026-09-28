"""Cross-session aggregation for ``trace overview`` (R9b).

Pure wherever it can be: the maths takes rows and returns a report, so the table,
the JSON and the HTML index all share one set of numbers and the tests need no
filesystem.  ``build_report`` is the single disk-touching entry point, and it
degrades to zeros instead of raising - a broken trace must not break the report.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ai_coding.core.trace import TraceStore

__all__ = [
    "AggregateReport",
    "SessionRow",
    "aggregate_sessions",
    "build_report",
    "row_from_summary",
]


@dataclass(frozen=True)
class SessionRow:
    """One session's numbers, as read (or as summed for the totals row)."""

    session: str
    turns: int = 0
    files: int = 0
    steps: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    age_days: float = 0.0

    @property
    def mismatch(self) -> bool:
        """``summary.turns`` disagrees with the turn files on disk."""
        return self.turns != self.files

    @property
    def cache_ratio(self) -> float:
        return self.cached_tokens / self.prompt_tokens if self.prompt_tokens > 0 else 0.0


@dataclass(frozen=True)
class AggregateReport:
    rows: tuple[SessionRow, ...] = ()
    totals: SessionRow = field(default_factory=lambda: SessionRow(session="(all)"))

    @property
    def cache_ratio(self) -> float:
        return self.totals.cache_ratio


def _int(value: Any) -> int:
    """Tolerant int: anything unusable counts as zero (never raises)."""
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def row_from_summary(
    session: str,
    summary: Any,
    *,
    files: int,
    mtime: float,
    now: float,
) -> SessionRow:
    """One reading row; a missing/broken summary is "zeros", not an exception."""
    data: dict[str, Any] = summary if isinstance(summary, dict) else {}
    age_days = max(0.0, (now - mtime) / 86_400) if mtime else 0.0
    return SessionRow(
        session=session,
        turns=_int(data.get("turns")),
        files=int(files),
        steps=_int(data.get("steps")),
        prompt_tokens=_int(data.get("prompt_tokens")),
        completion_tokens=_int(data.get("completion_tokens")),
        cached_tokens=_int(data.get("cached_tokens")),
        age_days=age_days,
    )


def aggregate_sessions(rows: Sequence[SessionRow]) -> AggregateReport:
    """Sum the rows into one report, preserving their order.  Pure."""
    totals = SessionRow(
        session="(all)",
        turns=sum(row.turns for row in rows),
        files=sum(row.files for row in rows),
        steps=sum(row.steps for row in rows),
        prompt_tokens=sum(row.prompt_tokens for row in rows),
        completion_tokens=sum(row.completion_tokens for row in rows),
        cached_tokens=sum(row.cached_tokens for row in rows),
    )
    return AggregateReport(rows=tuple(rows), totals=totals)


def build_report(
    store: TraceStore, *, since_days: int = 0, now: float | None = None
) -> AggregateReport:
    """Read every session on disk and fold it into one report (R9b).

    ``since_days`` filters on the **directory mtime**, exactly like ``prune``, so
    "what prune would delete" and "what counts as old" can never disagree.
    """
    moment = time.time() if now is None else now
    cutoff = moment - since_days * 86_400 if since_days > 0 else None
    rows: list[SessionRow] = []
    for ref in store.sessions():
        if cutoff is not None and ref.mtime < cutoff:
            continue
        rows.append(
            row_from_summary(
                ref.session,
                store.read_summary(ref.session),
                files=ref.turns,
                mtime=ref.mtime,
                now=moment,
            )
        )
    return aggregate_sessions(rows)
