"""领域 F / M-F2: scene synonyms fold at read, filter and write time.

Contract (docs/data-contracts.md:868-869): ``normalize_scene`` does **not**
change -- the fold gate lives at read time and filter time, and the one-off
spelling merge is ``scripts/migrate_scenes.py``.

Measured gap: only the stored value folded, so a model writing
``scene="记忆模块"`` while the index holds ``记忆`` produced two labels, each
with one note, neither reaching ``governed_scenes()`` (>=3) -- so the filter
was silently dropped.
"""

from __future__ import annotations

import pytest

from ai_coding.memory.note import MemoryNote, normalize_scene
from ai_coding.memory.scenes import SCENE_SYNONYMS, fold_scene
from ai_coding.memory.service import MemoryService


def test_fold_scene_maps_a_synonym():
    assert fold_scene("记忆模块") == "记忆"
    assert fold_scene("单元测试") == "测试"
    assert fold_scene("db") == "数据库"


def test_fold_scene_leaves_an_ordinary_label_alone():
    assert fold_scene("性能优化") == "性能优化"


def test_normalize_scene_is_unchanged_by_the_fold():
    """The contract: normalize_scene must not grow the table."""
    assert normalize_scene("记忆模块") == "记忆模块"
    assert normalize_scene("db") == "db"


def test_fold_scene_is_idempotent():
    """A value must not be a key, or a second fold over-folds."""
    for key, value in SCENE_SYNONYMS.items():
        assert value not in SCENE_SYNONYMS, f"{value!r} is both a key and a value"
    for key in SCENE_SYNONYMS:
        assert fold_scene(fold_scene(key)) == fold_scene(key)


def _service(tmp_path) -> MemoryService:
    from ai_coding.memory.store import MemoryStore

    return MemoryService(
        store=MemoryStore(tmp_path / "memory.json"),
        config={"max_index_entries": 200, "recall_timeout_seconds": 0.0},
    )


def test_the_write_path_folds(tmp_path):
    """Otherwise a fresh "记忆模块" and a loaded "记忆" count as two labels."""
    svc = _service(tmp_path)
    svc.write_note("one", scene="记忆模块")
    assert svc.store.active()[0].scene == "记忆"


def test_the_filter_folds_the_argument(tmp_path):
    """The measured gap: the argument was compared raw."""
    svc = _service(tmp_path)
    for text in ("one", "two", "three"):
        svc.write_note(text, scene="记忆模块")
    hits, diag = svc.recall_with_diagnostics("one", scene="记忆模块")
    assert hits, "the filter must reach the folded label"
    assert diag.ignored_scene is None, "the label was dropped as 'not a group'"


def test_the_gate_before_the_filter_folds(tmp_path):
    """A label with two notes under one spelling and two under the synonym
    must be seen as one group of four -- the gate runs before the filter."""
    svc = _service(tmp_path)
    for text in ("a", "b"):
        svc.write_note(text, scene="记忆模块")
    for text in ("c", "d"):
        svc.write_note(text, scene="记忆")
    assert "记忆" in svc.governed_scenes()


def test_the_read_path_folds(tmp_path):
    store = _service(tmp_path).store
    path = tmp_path / "memory.json"
    path.write_text(
        '{"version": 2, "notes": ['
        '{"id": "n1", "text": "one", "scene": "记忆模块"},'
        '{"id": "n2", "text": "two", "scene": "记忆模块"}]}',
        encoding="utf-8",
    )
    from ai_coding.memory.store import MemoryStore

    loaded = MemoryStore(path)
    assert [note.scene for note in loaded.active()] == ["记忆", "记忆"]


def test_migrate_scenes_imports_the_one_table():
    """One table: migrate_scenes.py must not keep a second copy."""
    import scripts.migrate_scenes as migrate

    assert migrate.SCENE_SYNONYMS is SCENE_SYNONYMS
