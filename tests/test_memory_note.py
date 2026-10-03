"""TDD (R3a): the MemoryNote contract (kind / scope / provenance / supersede).

Cases 4 and 5 of docs/r3-analysis.md need this: notes must be able to replace each
other *without being deleted* (auditable), and the store must be able to protect
pinned notes and decisions from eviction.  v1 files (``{id,text,created_time}``)
must still load.
"""

import pytest

from ai_coding.memory.note import MemoryNote, NoteKind, new_note


def test_defaults_are_conservative():
    note = MemoryNote(id="n1", text="项目用 uv 管理依赖")
    assert note.kind is NoteKind.FACT
    assert note.scope == "workspace"
    assert note.importance == 3
    assert note.hits == 0
    assert note.pinned is False
    assert note.is_active is True


def test_dedupe_key_ignores_case_space_punctuation_and_lead_ins():
    a = MemoryNote(id="n1", text="记住：项目用 uv 管理依赖")
    b = MemoryNote(id="n2", text="项目用UV，管理依赖")
    assert a.dedupe_key == b.dedupe_key


def test_dedupe_key_differs_for_different_facts():
    a = MemoryNote(id="n1", text="项目用 uv 管理依赖")
    b = MemoryNote(id="n2", text="项目改用 poetry 管理依赖")
    assert a.dedupe_key != b.dedupe_key


def test_importance_is_clamped():
    assert MemoryNote(id="n1", text="x", importance=99).importance == 5
    assert MemoryNote(id="n1", text="x", importance=0).importance == 1


def test_blank_text_is_rejected():
    with pytest.raises(ValueError):
        MemoryNote(id="n1", text="   ")


def test_kind_parsing_is_tolerant():
    assert MemoryNote.from_dict({"id": "n", "text": "x", "kind": "decision"}).kind is (
        NoteKind.DECISION
    )
    assert MemoryNote.from_dict({"id": "n", "text": "x", "kind": "???"}).kind is (
        NoteKind.FACT
    )


def test_round_trip_serialisation():
    note = new_note(
        "项目用 uv 管理依赖",
        kind=NoteKind.DECISION,
        importance=5,
        pinned=True,
        source_session="s1",
        source_turn=3,
    )
    again = MemoryNote.from_dict(note.to_dict())
    assert again == note
    assert again.kind is NoteKind.DECISION and again.pinned is True


def test_v1_shape_loads_with_defaults():
    """The existing .memory files only have id/text/created_time."""
    note = MemoryNote.from_dict(
        {"id": "old", "text": "项目用 uv 管理依赖", "created_time": "2026-01-01T00:00:00Z"}
    )
    assert note.kind is NoteKind.FACT
    assert note.importance == 3
    assert note.scope == "workspace"
    assert note.created_time == "2026-01-01T00:00:00Z"


def test_unknown_keys_are_ignored_and_bad_rows_raise():
    assert MemoryNote.from_dict({"id": "n", "text": "x", "future": 1}).text == "x"
    with pytest.raises(ValueError):
        MemoryNote.from_dict({"text": "no id"})
    with pytest.raises(ValueError):
        MemoryNote.from_dict({"id": "n"})


def test_supersede_marks_inactive_without_deleting():
    old = MemoryNote(id="old", text="项目用 uv 管理依赖", kind=NoteKind.DECISION)
    new = MemoryNote(
        id="new",
        text="项目改用 poetry 管理依赖",
        kind=NoteKind.DECISION,
        supersedes="old",
    )
    replaced = old.superseded_by_note(new)
    assert replaced.is_active is False
    assert replaced.superseded_by == "new"
    assert replaced.text == old.text  # the record survives (auditable)


def test_touch_counts_hits():
    note = MemoryNote(id="n", text="x")
    touched = note.touch("2026-09-23T00:00:00Z")
    assert touched.hits == 1
    assert touched.last_used == "2026-09-23T00:00:00Z"
    assert note.hits == 0  # frozen value object


# ---- R11c: source_message_ids — position references into the session file ---


def test_r11c_source_refs_round_trip():
    note = new_note("构建用 uv", source_session="s1", source_turn=2,
                    source_message_ids=("m13", "m14"))
    again = MemoryNote.from_dict(note.to_dict())
    assert again.source_message_ids == ("m13", "m14")
    assert again == note


def test_r11c_source_refs_default_empty_and_omitted_from_json():
    note = new_note("构建用 uv")
    assert note.source_message_ids == ()
    assert "source_message_ids" not in note.to_dict()


def test_r11c_source_refs_ignore_a_non_sequence():
    """Old rows have no such key, and a hand-edited file must not crash the load."""
    assert MemoryNote.from_dict({"id": "a", "text": "t"}).source_message_ids == ()
    note = MemoryNote.from_dict({"id": "a", "text": "t", "source_message_ids": "m3"})
    assert note.source_message_ids == ()


# ---- R11c: scene — the topic group a note belongs to -----------------------


def test_r11c_scene_defaults_empty_and_is_omitted_from_json():
    note = new_note("构建用 uv")
    assert note.scene == ""
    assert "scene" not in note.to_dict()


def test_r11c_scene_round_trip():
    note = new_note("构建用 uv", scene="构建")
    assert MemoryNote.from_dict(note.to_dict()) == note
    assert MemoryNote.from_dict(note.to_dict()).scene == "构建"


def test_r11c_scene_is_normalised_and_bounded():
    assert new_note("x", scene="  构建  ").scene == "构建"
    assert len(new_note("x", scene="构" * 200).scene) == 80
    # A non-string is coerced, never raised on: this comes from a model answer.
    assert new_note("x", scene=123).scene == "123"
    assert new_note("x", scene=None).scene == ""
