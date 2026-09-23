"""TDD (R3a): MemoryStore v2 — managed notes instead of an append-only list.

Cases 3/4/5 of docs/r3-analysis.md: the same fact stored five times, contradictions
living side by side, and FIFO eviction deleting the most important note.  The M3
API (``add``/``search``/``delete``) stays as a deprecated compatibility surface so
the existing store tests keep passing untouched; the new API is what the service
layer uses.
"""

import json

from ai_coding.memory.note import NoteKind, new_note
from ai_coding.memory.store import MemoryStore


def _store(tmp_path) -> MemoryStore:
    return MemoryStore(tmp_path / "m.json")


# ---- schema v2 and migration ------------------------------------------------


def test_saves_version_2(tmp_path):
    store = _store(tmp_path)
    store.add_note(new_note("项目用 uv 管理依赖"))
    payload = json.loads((tmp_path / "m.json").read_text(encoding="utf-8"))
    assert payload["version"] == 2
    assert payload["notes"][0]["text"] == "项目用 uv 管理依赖"


def test_v1_file_is_migrated_on_load(tmp_path):
    target = tmp_path / "m.json"
    target.write_text(
        json.dumps({"entries": [{"id": "old", "text": "项目用 uv", "created_time": "T"}]}),
        encoding="utf-8",
    )
    store = MemoryStore(target)
    note = store.get("old")
    assert note is not None
    assert note.text == "项目用 uv"
    assert note.kind is NoteKind.FACT
    assert note.importance == 3
    store.pin("old", True)  # a mutation rewrites the file as v2
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["version"] == 2
    assert payload["notes"][0]["pinned"] is True


def test_v2_round_trips_every_field(tmp_path):
    store = _store(tmp_path)
    note = new_note(
        "项目改用 poetry 管理依赖",
        kind=NoteKind.DECISION,
        importance=5,
        source_session="s1",
        source_turn=4,
        pinned=True,
    )
    store.add_note(note)
    reloaded = _store(tmp_path).get(note.id)
    assert reloaded == note


# ---- reconciliation --------------------------------------------------------


def test_add_note_is_idempotent_by_content(tmp_path):
    store = _store(tmp_path)
    first = store.add_note(new_note("记住：项目用 uv 管理依赖"))
    second = store.add_note(new_note("项目用 uv 管理依赖"))
    assert first == second
    assert store.count == 1  # case 3


def test_find_similar_ignores_lead_ins_and_kind_is_respected(tmp_path):
    store = _store(tmp_path)
    store.add_note(new_note("项目用 uv 管理依赖", kind=NoteKind.DECISION))
    assert store.find_similar("记住：项目用 uv 管理依赖", threshold=0.80) is not None
    assert store.find_similar("完全无关的另一件事", threshold=0.80) is None


def test_update_replaces_by_id_and_keeps_the_id(tmp_path):
    store = _store(tmp_path)
    note_id = store.add_note(new_note("项目用 uv 管理依赖"))
    changed = store.get(note_id)
    assert changed is not None
    assert store.update(changed.with_importance(5)) is True
    assert store.get(note_id).importance == 5
    assert store.update(new_note("不存在", note_id="nope")) is False


def test_supersede_keeps_the_old_note_but_hides_it_from_active(tmp_path):
    store = _store(tmp_path)
    old_id = store.add_note(new_note("项目用 uv 管理依赖", kind=NoteKind.DECISION))
    newer = new_note("项目改用 poetry 管理依赖", kind=NoteKind.DECISION)
    assert store.supersede(old_id, newer) is True

    old = store.get(old_id)
    assert old is not None and old.superseded_by == newer.id  # auditable
    assert old.text == "项目用 uv 管理依赖"
    assert {n.text for n in store.active()} == {"项目改用 poetry 管理依赖"}  # case 4


def test_supersede_unknown_target_is_a_no_op(tmp_path):
    store = _store(tmp_path)
    assert store.supersede("nope", new_note("x")) is False
    assert store.count == 0


# ---- retrieval -------------------------------------------------------------


def test_search_scored_ranks_and_filters(tmp_path):
    store = _store(tmp_path)
    store.add_note(new_note("payment service timeout config"))
    store.add_note(new_note("构建流程：make build 产物在 dist/"))
    hits = store.search_scored("make build 产物在哪", limit=3)
    assert [h.note.text for h in hits] == ["构建流程：make build 产物在 dist/"]


def test_search_scored_skips_superseded_notes(tmp_path):
    store = _store(tmp_path)
    old_id = store.add_note(new_note("项目用 uv 管理依赖"))
    store.supersede(old_id, new_note("项目改用 poetry 管理依赖"))
    texts = [h.note.text for h in store.search_scored("依赖怎么装")]
    assert texts == ["项目改用 poetry 管理依赖"]


# ---- eviction --------------------------------------------------------------


def test_evict_keeps_pinned_and_decisions(tmp_path):
    store = _store(tmp_path)
    store.add_note(
        new_note("安全红线：不得把密钥写进代码", kind=NoteKind.DECISION, pinned=True)
    )
    for index in range(4):
        store.add_note(new_note(f"琐碎事实 {index}"))

    removed = store.evict(cap=3)

    assert len(removed) == 2
    texts = {n.text for n in store.all()}
    assert "安全红线：不得把密钥写进代码" in texts  # case 5
    assert len([t for t in texts if t.startswith("琐碎事实")]) == 2


def test_evict_prefers_keeping_important_and_often_used_notes(tmp_path):
    store = _store(tmp_path)
    keep_id = store.add_note(new_note("重要约定", importance=5))
    used_id = store.add_note(new_note("经常用到的约定", importance=2))
    drop_id = store.add_note(new_note("一次性琐事", importance=1))
    for _ in range(4):
        store.update(store.get(used_id).touch())  # type: ignore[union-attr]

    removed = store.evict(cap=2)

    assert removed == [drop_id]
    assert {n.id for n in store.all()} == {keep_id, used_id}


def test_evict_does_nothing_below_cap(tmp_path):
    store = _store(tmp_path)
    store.add_note(new_note("a"))
    assert store.evict(cap=5) == []


# ---- deprecated M3 surface stays intact ------------------------------------


def test_deprecated_add_and_search_still_work(tmp_path):
    store = _store(tmp_path)
    assert store.add("the build is flaky on windows") != ""
    store.add("run tests with pytest")
    hits = store.search(["pytest"])
    assert [h.text for h in hits] == ["run tests with pytest"]
