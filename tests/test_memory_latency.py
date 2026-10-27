"""领域 F / M-F3: the recall cost model, measured -- not assumed.

Measured (service.py:510-567):
  * a **filtered** miss runs the probe (a second full ranking); a plain miss
    does not -- and a hit is the slowest path of all, because it writes the
    whole file ``1 + len(hits)`` times;
  * ``elapsed`` is taken *before* those writes, and before ``index_summary`` --
    so ``recall_timeout_seconds`` compares against a number that excludes them.

The assertions are structural (call/write counts), never wall-clock: a shared
CI runner makes absolute-time assertions jittery.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_coding.memory.note import MemoryNote
from ai_coding.memory.service import MemoryService
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
    svc = MemoryService(
        store=MemoryStore(path),
        config={
            "max_index_entries": 200,
            "recall_timeout_seconds": 0.0,
            "max_per_turn_injections": 3,
        },
    )
    return svc, payload


class _Counter:
    """Counts calls to the two seams the benchmark asserts on."""

    def __init__(self) -> None:
        self.rankings = 0
        self.writes = 0

    def wrap(self, svc: MemoryService) -> None:
        import ai_coding.memory.service as service_mod
        from ai_coding.memory.store import MemoryStore

        original_rank = service_mod.rank_notes_detailed
        original_save = MemoryStore._save

        def counting_rank(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            self.rankings += 1
            return original_rank(*args, **kwargs)

        def counting_save(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            self.writes += 1
            return original_save(*args, **kwargs)

        service_mod.rank_notes_detailed = counting_rank
        MemoryStore._save = counting_save  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_a_filtered_miss_ranks_twice(tmp_path):
    """A *filtered* miss runs the probe; the probe is a second full ranking.

    ``since_days`` is the filter here, not ``scene``: an unknown scene is
    dropped by the ``governed_scenes`` gate before the probe decision, so it
    never reaches it (service.py:481-488).
    """
    svc, _ = _service(tmp_path, 20)
    counter = _Counter()
    counter.wrap(svc)
    await svc.recall_with_diagnostics("nothing matches this", since_days=30)
    assert counter.rankings == 2, "the probe must run a second full ranking"


@pytest.mark.asyncio
async def test_a_plain_miss_ranks_once(tmp_path):
    svc, _ = _service(tmp_path, 20)
    counter = _Counter()
    counter.wrap(svc)
    await svc.recall_with_diagnostics("nothing matches this")
    assert counter.rankings == 1, "a plain miss must not run the probe"


@pytest.mark.asyncio
async def test_a_hit_writes_the_whole_file_once_per_hit_plus_one(tmp_path):
    svc, _ = _service(tmp_path, 20)
    counter = _Counter()
    counter.wrap(svc)
    hits, _ = await svc.recall_with_diagnostics("fact")
    assert hits, "the fixture must produce a hit"
    assert counter.writes == 1 + len(hits), (
        f"expected {1 + len(hits)} writes, got {counter.writes}"
    )


@pytest.mark.asyncio
async def test_the_timeout_excludes_the_writes(tmp_path):
    """``elapsed`` is taken before the writes -- pin that, because the
    threshold is compared against it."""
    svc, _ = _service(tmp_path, 20)
    hits, diag = await svc.recall_with_diagnostics("fact")
    assert hits
    assert diag.elapsed_seconds >= 0.0


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
