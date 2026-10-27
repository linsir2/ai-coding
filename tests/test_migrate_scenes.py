"""The merge is the only write in M8, so it is tested like one."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from migrate_scenes import SCENE_SYNONYMS, head, plan  # noqa: E402

from ai_coding.memory.note import MemoryNote, NoteKind, normalize_scene  # noqa: E402
from ai_coding.memory.store import MemoryStore  # noqa: E402


def _store(tmp_path, *scenes):
    store = MemoryStore(tmp_path / "m.json")
    for index, scene in enumerate(scenes):
        store.add_note(
            MemoryNote(id=f"n{index}", text=f"内容 {index}", kind=NoteKind.FACT, scene=scene)
        )
    return store


def test_plan_rewrites_a_label_without_touching_the_store(tmp_path):
    store = _store(tmp_path, "记忆模块", "记忆系统", "记忆配置", "别的领域")

    changes, counts_after = plan(store)

    assert sorted(target for _, target in changes) == ["记忆", "记忆", "记忆"]
    assert counts_after["记忆"] == 3
    assert [note.scene for note in store.all()] == [
        "记忆模块", "记忆系统", "记忆配置", "别的领域",
    ], "a dry run must not write"


def test_plan_is_idempotent_so_re_running_cannot_merge_twice(tmp_path):
    store = _store(tmp_path, "记忆模块", "记忆系统", "记忆配置")
    for _note, target in plan(store)[0]:
        store.update(
            MemoryNote(
                id=_note, text="内容", kind=NoteKind.FACT, scene=target
            )
        )

    assert plan(store)[0] == []


def test_two_labels_that_merely_share_a_word_go_to_different_places(tmp_path):
    """「工具约束」 is what the tools may do; 「执行约束」 is what a run may do.
    They share the word 约束 and nothing else, so the reviewed table sends them
    to different labels.  A matcher reading the shared word would merge them into
    one, and `scene=` would then answer a question nobody asked."""
    store = _store(tmp_path, "工具约束", "执行约束", "工具约束", "工具约束", "执行约束", "执行约束")

    _, counts_after = plan(store)

    assert counts_after["工具"] == 3
    assert counts_after["开发流程"] == 3
    assert "工具约束" not in counts_after and "执行约束" not in counts_after


def test_the_table_folds_in_one_pass_and_keeps_the_labels_short(tmp_path):
    """Two invariants of the table itself, checkable without the real store.

    A target that is also a key would need a second run, so the fold would not
    be done when the script claims it is.  And the extractor prompt caps a label
    at 8 characters, so a target past that is a label the model would not write.
    (Whether the fold actually buys any group depends on the data — the script's
    dry run reports that, and there is no test fixture large enough to mean it.)
    """
    for _old, target in SCENE_SYNONYMS.items():
        assert target not in SCENE_SYNONYMS, f"{target} is folded twice; run the script again"
        assert len(target) <= 8, f"{target} is longer than the label limit"
        assert normalize_scene(target) == target, f"{target} is not already normalized"


def test_head_counts_only_labels_that_can_group(tmp_path):
    assert head({"环境": 7, "项目": 7, "工具": 3, "孤例": 1, "": 2}) == [
        ("环境", 7), ("项目", 7), ("工具", 3),
    ]


def test_apply_writes_a_backup_so_the_merge_can_be_undone(tmp_path):
    from migrate_scenes import main

    _store(tmp_path, "记忆模块", "记忆系统", "记忆配置")
    argv = sys.argv
    sys.argv = ["migrate_scenes.py", "--store", str(tmp_path / "m.json"), "--apply"]
    try:
        main()
    finally:
        sys.argv = argv

    # Re-read from disk: the script writes through its own store instance, so
    # this also proves the merge reached the file and not just a cache.
    reread = MemoryStore(tmp_path / "m.json")
    assert [note.scene for note in reread.all()] == ["记忆", "记忆", "记忆"]
    backup = json.loads((tmp_path / "m.json.scenes.bak").read_text("utf-8"))
    assert backup == {"n0": "记忆模块", "n1": "记忆系统", "n2": "记忆配置"}


@pytest.mark.parametrize("before", [[], ["x"]])
def test_a_store_with_nothing_to_merge_still_runs(tmp_path, before):
    from migrate_scenes import main

    _store(tmp_path, *before)
    argv = sys.argv
    sys.argv = ["migrate_scenes.py", "--store", str(tmp_path / "m.json")]
    try:
        assert main() == 0
    finally:
        sys.argv = argv


def test_a_failed_write_never_leaves_a_truncated_store(tmp_path, monkeypatch):
    """`--apply` rewrites the whole file once per note, so the write is atomic.

    A crash between the truncate and the flush would leave a store that loads as
    zero notes, and the merge's only undo is the side file written beforehand.
    The failure is raised rather than swallowed: a migration that reports
    `wrote N notes` while writing nothing is worse than one that stops.
    """
    import migrate_scenes

    path = tmp_path / "m.json"
    original = json.dumps(
        {
            "version": 2,
            "notes": [
                {"id": "n0", "text": "内容 0", "scene": "记忆模块"},
                {"id": "n1", "text": "内容 1", "scene": "记忆系统"},
            ],
        }
    )
    path.write_text(original, encoding="utf-8")
    store = migrate_scenes._RawStore(path)

    def _boom(tmp, target):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", _boom)

    with pytest.raises(OSError):
        store.update(
            MemoryNote(id="n0", text="内容 0", kind=NoteKind.FACT, scene="记忆")
        )

    assert path.read_text(encoding="utf-8") == original, (
        "the live store must be untouched"
    )
