#!/usr/bin/env python3
"""M-F3 / C.1: measure the recall cost model — three paths, three clocks.

Measured (service.py:510-567):

  * a **filtered** miss runs the probe (a second full ranking); a plain miss
    does not; and a hit is the slowest path of all, because it writes the whole
    file ``1 + len(hits)`` times;
  * ``elapsed`` is taken *before* those writes and before ``index_summary``, so
    ``recall_timeout_seconds`` compares against a number that excludes them.

Three clocks, because they answer different questions:

  * ``elapsed_seconds`` — the service's own measurement, ranking only;
  * write — ``1 + len(hits)`` whole-file serialisations, timed separately;
  * ``index_summary`` — the multi-pass full-index scan every recall runs.

The assertions are structural (call/write counts), never wall-clock: a shared CI
runner makes absolute-time assertions jittery.  p50/p95 are printed, not gated.

The store is built without ``add_note`` (O(N^2)): the notes are built in memory
and written as one JSON document, then loaded with ``MemoryStore(path)``.
``recall_timeout_seconds`` is explicitly 0 — otherwise the budget would skip the
writes and the write count would read 0.

    .venv/bin/python scripts/memory_latency_cases.py
    .venv/bin/python scripts/memory_latency_cases.py --scale 10000   # stress
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai_coding.memory.note import MemoryNote  # noqa: E402
from ai_coding.memory.service import MemoryService, _default_config  # noqa: E402
from ai_coding.memory.store import MemoryStore  # noqa: E402

SCALES = (50, 200, 1_000)
STRESS_SCALE = 10_000
REPEATS = 5
_STAMP = "2026-09-01T00:00:00+00:00"


class _Counter:
    """Counts the two seams the benchmark asserts on."""

    def __init__(self) -> None:
        self.rankings = 0
        self.writes = 0
        self._rank = None
        self._save = None

    def wrap(self) -> None:
        import ai_coding.memory.service as service_mod

        self._rank = service_mod.rank_notes_detailed
        self._save = MemoryStore._save

        def counting_rank(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            self.rankings += 1
            return self._rank(*args, **kwargs)

        def counting_save(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            self.writes += 1
            return self._save(*args, **kwargs)

        service_mod.rank_notes_detailed = counting_rank
        MemoryStore._save = counting_save  # type: ignore[method-assign]

    def unwrap(self) -> None:
        import ai_coding.memory.service as service_mod

        service_mod.rank_notes_detailed = self._rank
        MemoryStore._save = self._save  # type: ignore[method-assign]


def _build_store(path: Path, notes: int) -> MemoryService:
    """Build a store in one write, then load it; never ``add_note`` (O(N^2))."""
    rows = [
        MemoryNote(
            id=f"n{index}",
            text=f"fact number {index} about alpha",
            scene=f"scene-{index % 5}",
            created_time=_STAMP,
            updated_time=_STAMP,
        )
        for index in range(notes)
    ]
    path.write_text(
        json.dumps({"version": 2, "notes": [note.to_dict() for note in rows]}),
        encoding="utf-8",
    )
    # `_default_config`, not a dict: `MemoryService._get` reads with `getattr`,
    # so a dict config is silently ignored and every key falls back to default.
    # A namespace keeps the "no budget" precondition below actually in force.
    return MemoryService(
        store=MemoryStore(path),
        config=_default_config(
            recall_timeout_seconds=0.0,
            max_index_entries=200,
            max_per_turn_injections=3,
        ),
    )


def _plain_miss_query(notes: int) -> str:
    """A query the corpus *knows* but cannot answer.

    Five distinct numbers just below the store's range: each appears in exactly
    one note, so no note covers more than 1/5 = 0.20 of the query's IDF mass,
    under ``min_relevance`` 0.25.  The ranking therefore runs in full and the
    gate rejects all of it -- a plain miss, not the vocabulary early return that
    ``"nothing matches this"`` measures (0.70ms against 3.94ms for a full BM25
    pass over the same 200-note store).
    """
    top = notes - 1
    return " ".join(str(top - offset) for offset in range(5))


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct / 100.0
    low = int(math.floor(rank))
    high = int(math.ceil(rank))
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _recall(service: MemoryService, **kwargs) -> tuple[list, object, float, _Counter]:
    counter = _Counter()
    counter.wrap()
    try:
        started = time.perf_counter()
        hits, diagnostics = asyncio.run(service.recall_with_diagnostics(**kwargs))
        wall = time.perf_counter() - started
    finally:
        counter.unwrap()
    return hits, diagnostics, wall, counter


def _time_writes(service: MemoryService, hits: list, repeats: int) -> list[float]:
    """The hit path's write cost: one ``update_stake`` per hit plus one touch."""
    times: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter()
        for hit in hits:
            service.store.update_stake(hit.note.id, hit.note.stake * 1.2)
        service.store.touch_notes([hit.note.id for hit in hits])
        times.append(time.perf_counter() - started)
    return times


def _time_index_summary(service: MemoryService, repeats: int) -> list[float]:
    times: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter()
        service.index_summary()
        times.append(time.perf_counter() - started)
    return times


def _check_by_scene_bound(service: MemoryService) -> list[str]:
    """`by_scene` is bounded by the note count, never by a fixed label cap.

    A store built through harvest/consolidate is additionally capped by
    `max_index_entries`; the 1_000/10_000 stress scales deliberately bypass
    eviction, so they must not be used to pin that number.  The invariant that
    always holds is the one asserted here: labels cannot outnumber notes.
    """
    summary = service.index_summary()
    active = len(service.store.active())
    if len(summary.by_scene) > active:
        return [
            f"by_scene has {len(summary.by_scene)} labels over {active} notes"
        ]
    return []


def _run_scale(scale: int, repeats: int) -> list[str]:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as directory:
        service = _build_store(Path(directory) / "memory.json", scale)
        failures.extend(_check_by_scene_bound(service))

        # A plain miss must have *known* words, or `rank_notes_detailed` returns
        # at the vocabulary check (relevance.py:451) and this column measures an
        # early return rather than a ranking.
        plain_query = _plain_miss_query(scale)
        hit_hits, _, hit_wall, hit_counter = _recall(
            service, query_or_history="fact number 0", k=3
        )
        plain_hits, _, plain_wall, plain_counter = _recall(
            service, query_or_history=plain_query
        )
        # The probe ranks the *unfiltered* index, so the filter has to actually
        # empty the candidate set first.  `since_days=1` does that whatever the
        # wall clock says, which is the coupling `_STAMP` used to depend on.
        _, _, _, filtered_counter = _recall(
            service, query_or_history=plain_query, since_days=1
        )

        hit_elapsed: list[float] = []
        plain_elapsed: list[float] = []
        filtered_elapsed: list[float] = []
        for _ in range(repeats):
            _, hit_diag, _, _ = _recall(service, query_or_history="fact number 0", k=3)
            hit_elapsed.append(float(hit_diag.elapsed_seconds))
            _, plain_diag, _, _ = _recall(service, query_or_history=plain_query)
            plain_elapsed.append(float(plain_diag.elapsed_seconds))
            _, filtered_diag, _, _ = _recall(
                service, query_or_history=plain_query, since_days=1
            )
            filtered_elapsed.append(float(filtered_diag.elapsed_seconds))

        # Structural assertions: counts, never wall-clock.
        if not hit_hits:
            failures.append(f"scale={scale}: the hit fixture produced no hit")
        if hit_counter.writes != 1 + len(hit_hits):
            failures.append(
                f"scale={scale}: hit writes={hit_counter.writes}, "
                f"expected 1 + len(hits)={1 + len(hit_hits)}"
            )
        if plain_counter.rankings != 1:
            failures.append(
                f"scale={scale}: a plain miss ranked {plain_counter.rankings} times, expected 1"
            )
        if plain_diag.no_vocabulary_match:
            failures.append(
                f"scale={scale}: the plain-miss fixture took the vocabulary "
                f"early return, so its elapsed does not measure a ranking"
            )
        if filtered_counter.rankings != 2:
            failures.append(
                f"scale={scale}: a filtered miss ranked "
                f"{filtered_counter.rankings} times, expected 2"
            )
        if plain_hits:
            failures.append(f"scale={scale}: the plain-miss fixture produced a hit")
        if hit_wall <= plain_wall:
            failures.append(
                f"scale={scale}: hit wall {hit_wall:.6f}s did not exceed "
                f"plain-miss wall {plain_wall:.6f}s"
            )

        write_times = _time_writes(service, hit_hits, repeats)
        summary_times = _time_index_summary(service, repeats)

        print(f"scale={scale}  repeats={repeats}")
        print(
            f"  hit           rankings={hit_counter.rankings}  writes={hit_counter.writes}"
            f"  elapsed p50/p95={_percentile(hit_elapsed, 50) * 1e3:.3f}/"
            f"{_percentile(hit_elapsed, 95) * 1e3:.3f}ms  wall={hit_wall * 1e3:.3f}ms"
        )
        print(
            f"  plain-miss    rankings={plain_counter.rankings}  writes={plain_counter.writes}"
            f"  elapsed p50/p95={_percentile(plain_elapsed, 50) * 1e3:.3f}/"
            f"{_percentile(plain_elapsed, 95) * 1e3:.3f}ms  wall={plain_wall * 1e3:.3f}ms"
        )
        print(
            f"  filtered-miss rankings={filtered_counter.rankings}  "
            f"writes={filtered_counter.writes}"
            f"  elapsed p50/p95={_percentile(filtered_elapsed, 50) * 1e3:.3f}/"
            f"{_percentile(filtered_elapsed, 95) * 1e3:.3f}ms"
        )
        print(
            f"  write micro-benchmark     p50={_percentile(write_times, 50) * 1e3:.3f}ms"
            f"  p95={_percentile(write_times, 95) * 1e3:.3f}ms"
        )
        print(
            f"  index_summary micro-bench p50={_percentile(summary_times, 50) * 1e3:.3f}ms"
            f"  p95={_percentile(summary_times, 95) * 1e3:.3f}ms"
        )
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--scale",
        type=int,
        default=None,
        help=f"explicit scale (e.g. {STRESS_SCALE}); default: {SCALES}",
    )
    parser.add_argument("--repeats", type=int, default=REPEATS)
    args = parser.parse_args(argv)

    scales = (args.scale,) if args.scale else SCALES
    failures: list[str] = []
    for scale in scales:
        failures.extend(_run_scale(scale, args.repeats))
    if failures:
        for line in failures:
            print("ASSERTION FAILED:", line)
        return 1
    print("all structural assertions held")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
