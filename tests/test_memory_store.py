"""TDD (M3-3): MemoryStore — disk-backed index, atomic writes, never raises."""


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
