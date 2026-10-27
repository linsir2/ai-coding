"""领域 F / M-F3: the recall cost model, measured -- not assumed.

Measured (service.py:510-567):
  * a **filtered** miss runs the probe (a second full ranking); a plain miss
    does not -- and a hit is the slowest path of all, because it writes the
    whole file ``1 + len(hits)`` times;
  * ``elapsed`` is taken *before* those writes, and before ``index_summary`` --
    so ``recall_timeout_seconds`` compares against a number that excludes them.

The assertions are structural (call/write counts), never wall-clock: a shared
CI runner makes absolute-time assertions jittery.

A "plain miss" needs *known* query words.  ``"nothing matches this"`` has none,
so ``rank_notes_detailed`` returns at the vocabulary check (relevance.py:451)
and the column would measure an early return -- 0.70ms against 3.94ms for a
full BM25 pass over the same 200-note store.  The five distinct rare numbers
below put 1/5 = 0.20 coverage on every note, under ``min_relevance`` 0.25, so
the ranking really runs and the gate rejects all of it.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from ai_coding.memory.service import MemoryService, _default_config
from ai_coding.memory.store import MemoryStore


def _service(tmp_path: Path, notes: int) -> tuple[MemoryService, object]:
    path = tmp_path / "memory.json"
    payload = {
        "version": 2,
        "notes": [
            {
                "id": f"n{i}",
                "text": f"fact number {i} about alpha",
                "scene": "",
                "kind": "fact",
            }
            for i in range(notes)
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    # `_default_config`, not a dict: `MemoryService._get` reads with `getattr`,
    # so a dict config is silently ignored and every key falls back to default.
    svc = MemoryService(
        store=MemoryStore(path),
        config=_default_config(
            max_index_entries=200,
            recall_timeout_seconds=0.0,
            max_per_turn_injections=3,
        ),
    )
    return svc, payload


#: Known words, no note covering enough of them: a real plain miss.
def _plain_miss_query(notes: int) -> str:
    """Five distinct numbers just below the store's range.

    Each is in exactly one note, so no note covers more than 1/5 = 0.20 of the
    query's IDF mass, under ``min_relevance`` 0.25 -- the ranking runs in full
    and the gate rejects all of it.  ``"nothing matches this"`` has no known
    words at all, so it returns at the vocabulary check (relevance.py:451) and
    measures an early return instead.
    """
    top = notes - 1
    return " ".join(str(top - offset) for offset in range(5))


class _Counter:
    """Counts calls to the two seams the benchmark asserts on.

    Patched through pytest's ``monkeypatch``, which restores both attributes on
    teardown: a bare assignment would leak the wrappers for the rest of the
    process.
    """

    def __init__(self, monkeypatch) -> None:
        import ai_coding.memory.service as service_mod

        self.rankings = 0
        self.writes = 0
        self._rank = service_mod.rank_notes_detailed
        self._save = MemoryStore._save
        monkeypatch.setattr(service_mod, "rank_notes_detailed", self._count_rank)
        monkeypatch.setattr(MemoryStore, "_save", self._count_save)

    def _count_rank(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        self.rankings += 1
        return self._rank(*args, **kwargs)

    def _count_save(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        self.writes += 1
        return self._save(*args, **kwargs)


def test_the_fixture_budget_is_actually_in_force(tmp_path):
    """The "no budget" precondition this file relies on must be pinned.

    A dict config is silently ignored (`_get` reads with `getattr`), so
    ``recall_timeout_seconds`` would quietly be the 2.0s default -- and a slow
    runner would then skip the writes and read 0, a false red.
    """
    svc, _ = _service(tmp_path, 1)
    assert svc._get("recall_timeout_seconds", 1.0) == 0.0


@pytest.mark.asyncio
async def test_a_filtered_miss_ranks_twice(tmp_path, monkeypatch):
    """A *filtered* miss runs the probe; the probe is a second full ranking.

    ``since_days`` is the filter here, not ``scene``: an unknown scene is
    dropped by the ``governed_scenes`` gate before the probe decision, so it
    never reaches it (service.py:481-488).  ``since_days=1`` empties the
    candidate set whatever the wall clock says.
    """
    svc, _ = _service(tmp_path, 20)
    counter = _Counter(monkeypatch)
    await svc.recall_with_diagnostics(_plain_miss_query(20), since_days=1)
    assert counter.rankings == 2, "the probe must run a second full ranking"


@pytest.mark.asyncio
async def test_a_plain_miss_ranks_once(tmp_path, monkeypatch):
    svc, _ = _service(tmp_path, 20)
    counter = _Counter(monkeypatch)
    hits, diag = await svc.recall_with_diagnostics(_plain_miss_query(20))
    assert not hits
    assert not diag.no_vocabulary_match, "the fixture must not take the early return"
    assert counter.rankings == 1, "a plain miss must not run the probe"


@pytest.mark.asyncio
async def test_a_hit_writes_the_whole_file_once_per_hit_plus_one(
    tmp_path, monkeypatch
):
    svc, _ = _service(tmp_path, 20)
    counter = _Counter(monkeypatch)
    hits, _ = await svc.recall_with_diagnostics("fact")
    assert hits, "the fixture must produce a hit"
    assert counter.writes == 1 + len(hits), (
        f"expected {1 + len(hits)} writes, got {counter.writes}"
    )


@pytest.mark.asyncio
async def test_the_timeout_excludes_the_writes(tmp_path, monkeypatch):
    """``elapsed`` is taken before the writes -- pin that, because the
    threshold is compared against it.

    Structural, not wall-clock: the first write's own timestamp is the
    boundary.  ``elapsed`` is the service's ranking-only window, so it must
    close before that write starts; moving the measurement past the writes
    pushes it over the boundary and turns this red.
    """
    svc, _ = _service(tmp_path, 20)
    writes: list[float] = []
    original_save = MemoryStore._save

    def stamp_save(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        writes.append(time.monotonic())
        return original_save(*args, **kwargs)

    monkeypatch.setattr(MemoryStore, "_save", stamp_save)
    started = time.monotonic()
    hits, diag = await svc.recall_with_diagnostics("fact")
    assert hits
    assert writes, "the hit path must write"
    assert diag.elapsed_seconds <= writes[0] - started, (
        "elapsed must close before the writes it is supposed to exclude"
    )


def test_a_large_store_can_be_built_without_add_note(tmp_path):
    """``add_note`` is O(N^2); the benchmark builds the JSON directly."""
    svc, payload = _service(tmp_path, 1_000)
    assert len(svc.store.active()) == 1_000
    assert payload["version"] == 2


def test_the_store_loads_a_hand_written_file(tmp_path):
    path = tmp_path / "memory.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "notes": [{"id": "n1", "text": "one", "scene": "记忆模块"}],
            }
        ),
        encoding="utf-8",
    )
    store = MemoryStore(path)
    assert [note.text for note in store.active()] == ["one"]
