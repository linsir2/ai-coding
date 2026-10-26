"""TDD (M3-3): MemoryStore — disk-backed index, atomic writes, never raises."""

import json
import multiprocessing
import threading

from ai_coding.memory.note import new_note

# Re-import internals so a crash-on-missing-file change is caught explicitly.
from ai_coding.memory.store import (
    MemoryEntry,  # noqa: F401
    MemoryStore,
)


def test_add_and_all(tmp_path):
    store = MemoryStore(tmp_path / "memory.json")
    store.add("remember to use TDD")
    store.add("python 3.10")
    entries = store.all()
    assert len(entries) == 2
    assert any(e.text == "remember to use TDD" for e in entries)
    assert store.count == 2


def test_add_blank_returns_empty_id(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    assert store.add("   ") == ""


def test_search_keyword_substring(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    store.add("the build is flaky on windows")
    store.add("run tests with pytest")
    hits = store.search(["pytest"])
    assert len(hits) == 1
    assert hits[0].text == "run tests with pytest"


def test_search_no_keywords_returns_empty(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    store.add("something")
    assert store.search([]) == []


def test_delete_and_clear(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    eid = store.add("a note")
    assert store.delete(eid) is True
    assert store.delete(eid) is False  # already gone
    store.add("x")
    store.add("y")
    store.clear()
    assert store.count == 0


def test_persists_and_reloads(tmp_path):
    target = tmp_path / "m.json"
    store = MemoryStore(target)
    store.add("persisted text")
    store.add("second text")

    reloaded = MemoryStore(target)
    texts = {e.text for e in reloaded.all()}
    assert texts == {"persisted text", "second text"}


def test_io_error_swallowed(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "m.json")

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(type(store), "_save", boom)
    # add must not raise even when persistence fails
    assert store.add("still stored in memory") != ""
    assert store.count == 1


def test_corrupt_file_loaded_as_empty(tmp_path):
    target = tmp_path / "m.json"
    target.write_text("{not valid json", encoding="utf-8")
    store = MemoryStore(target)
    assert store.count == 0


# ---- M-C2: concurrent writers never corrupt the file -----------------------


def _concurrent_round(target, workers: int, per_worker: int, tag: str) -> None:
    """One round: ``workers`` threads, a barrier before every write."""
    store = MemoryStore(target)
    barrier = threading.Barrier(workers)
    errors: list[BaseException] = []

    def worker(seed: int) -> None:
        try:
            for index in range(per_worker):
                barrier.wait()
                store.add_note(new_note(f"{tag} note {seed}-{index}"))
        except BaseException as exc:  # noqa: BLE001 — surfaced by the assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(seed,)) for seed in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    data = json.loads(target.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and isinstance(data["notes"], list)
    reloaded = MemoryStore(target)
    assert reloaded.count == len(data["notes"])
    assert reloaded.count == workers * per_worker, "a serialized write was lost"


def test_concurrent_writes_never_corrupt(tmp_path):
    """Eight threads, a barrier before every write, several rounds: the save
    must hold the lock, or two writers land on one tmp path and append a second
    JSON document to the live file -- which ``_load`` then swallows into ``[]``.

    The race is narrow (a single ``write`` syscall), so one round is not enough
    to expose it: the rounds multiply the chance, and the fixed assertion is
    that *no* round ever leaves an unreadable file.
    """
    for round_index in range(5):
        _concurrent_round(
            tmp_path / f"m{round_index}.json", 8, 25, f"round {round_index}"
        )


def _writer_process(path: str, seed: int, barrier) -> None:
    store = MemoryStore(path)
    for index in range(25):
        barrier.wait()
        store.add_note(new_note(f"proc {seed}-{index}"))


def test_two_processes_never_corrupt(tmp_path):
    """Two processes over one workspace: the file must stay valid JSON at every
    moment, not just at the end (a cross-process writer cannot share the
    process-internal lock, so the rename is serialized with ``fcntl``)."""
    target = tmp_path / "m.json"
    barrier = multiprocessing.Barrier(2)
    stop = threading.Event()
    violations: list[str] = []

    def monitor() -> None:
        while not stop.is_set():
            try:
                data = json.loads(target.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            except ValueError:
                violations.append("invalid json")
                return
            if not isinstance(data, dict) or not isinstance(data.get("notes"), list):
                violations.append("bad shape")
                return

    watcher = threading.Thread(target=monitor)
    watcher.start()
    processes = [
        multiprocessing.Process(
            target=_writer_process, args=(str(target), seed, barrier)
        )
        for seed in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join()
    stop.set()
    watcher.join()

    assert violations == []
    data = json.loads(target.read_text(encoding="utf-8"))
    assert len(data["notes"]) > 0
