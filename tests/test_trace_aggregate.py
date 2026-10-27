"""TDD (R9b): cross-session aggregation — pure maths, one disk entry point."""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ai_coding.core.aggregate import SessionRow, aggregate_sessions, build_report
from ai_coding.core.trace import TraceStore


def _store(tmp_path: Path) -> TraceStore:
    return TraceStore(tmp_path / "traces", workspace=tmp_path)


def _iso(epoch: float) -> str:
    return (
        datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        + "Z"
    )


def _record(
    store: TraceStore,
    session: str,
    *,
    turns: int,
    summary: dict | None,
    ts: float | None = None,
) -> None:
    line = "{}" if ts is None else json.dumps({"v": 1, "ts": _iso(ts), "evt": "X"})
    for _ in range(turns):
        store.begin_turn(session).write_line(line)
    if summary is not None:
        (store.session_dir(session) / "summary.json").write_text(
            json.dumps(summary), encoding="utf-8"
        )


def test_r9_rows_sum_into_totals():
    rows = (
        SessionRow("a", turns=2, files=2, steps=3, prompt_tokens=100,
                   completion_tokens=10, cached_tokens=80),
        SessionRow("b", turns=1, files=1, steps=1, prompt_tokens=50,
                   completion_tokens=5, cached_tokens=0),
    )
    report = aggregate_sessions(rows)
    assert report.totals.turns == 3
    assert report.totals.steps == 4
    assert report.totals.prompt_tokens == 150
    assert report.totals.completion_tokens == 15
    assert report.totals.cached_tokens == 80
    assert [row.session for row in report.rows] == ["a", "b"]  # order preserved


def test_r9_cache_ratio_and_zero_prompt():
    report = aggregate_sessions((SessionRow("a", prompt_tokens=200, cached_tokens=50),))
    assert report.cache_ratio == 0.25
    empty = aggregate_sessions(())
    assert empty.cache_ratio == 0.0
    assert empty.totals.turns == 0


def test_r9_a_missing_summary_counts_zero_and_flags_mismatch(tmp_path):
    store = _store(tmp_path)
    _record(store, "sess-1", turns=2, summary=None)

    row = build_report(store).rows[0]

    assert row.turns == 0 and row.files == 2
    assert row.mismatch is True


def test_r9_a_corrupt_summary_is_not_fatal(tmp_path):
    store = _store(tmp_path)
    _record(store, "sess-1", turns=1, summary=None)
    (store.session_dir("sess-1") / "summary.json").write_text("not json", encoding="utf-8")

    row = build_report(store).rows[0]

    assert row.files == 1 and row.turns == 0


def test_r9_since_filters_by_in_file_ts_and_keeps_order(tmp_path):
    """The time source is the trace itself, so an rsync cannot move it.

    ``since_days`` used to read the session *directory* mtime, which an
    ``rsync`` without ``-t`` rewrites to the transfer time -- every session
    then looked brand new and the window returned everything.
    """
    store = _store(tmp_path)
    stale = time.time() - 30 * 86400
    _record(store, "old", turns=1, summary={"turns": 1}, ts=stale)
    _record(store, "new", turns=1, summary={"turns": 1}, ts=time.time())

    assert [row.session for row in build_report(store, since_days=7).rows] == ["new"]
    assert [row.session for row in build_report(store).rows] == ["new", "old"]


def test_r9_age_comes_from_the_in_file_ts_not_the_directory(tmp_path):
    store = _store(tmp_path)
    stale = time.time() - 5 * 86400
    _record(store, "sess-1", turns=1, summary={"turns": 1}, ts=stale)
    # the rsync: the directory looks fresh, the trace does not
    os.utime(store.session_dir("sess-1"), (time.time(), time.time()))

    row = build_report(store).rows[0]

    assert row.age_days == pytest.approx(5.0, abs=0.05)
