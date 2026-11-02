"""TDD (P2-B3): the on-disk settlement-notice lane.

The in-memory ``SteeringQueue`` dies with the process, so a child that settled
while the app was closing was gone forever.  The store keeps the same line on
disk until the loop has actually written it into the conversation; delivery
deletes only what was delivered, which makes the lane at-least-once.
"""

from __future__ import annotations

import json

from ai_coding.core.notice_store import NoticeStore, clip_notice


def test_append_and_pending_round_trip(tmp_path):
    store = NoticeStore.for_workspace(tmp_path)
    first = store.append("s1", "first line")
    second = store.append("s1", "second line")

    rows = store.pending("s1")

    assert [row["text"] for row in rows] == ["first line", "second line"]
    assert [row["id"] for row in rows] == [first, second]
    assert all(row["session"] == "s1" for row in rows)
    assert all(row["created_at"] for row in rows)


def test_each_session_has_its_own_file(tmp_path):
    store = NoticeStore.for_workspace(tmp_path)
    store.append("s1", "one")
    store.append("s2", "two")

    assert [row["text"] for row in store.pending("s1")] == ["one"]
    assert [row["text"] for row in store.pending("s2")] == ["two"]
    assert (tmp_path / ".ai-coding" / "notices" / "s1.jsonl").is_file()


def test_a_missing_session_file_is_empty(tmp_path):
    store = NoticeStore.for_workspace(tmp_path)
    assert store.pending("never") == []


def test_deliver_removes_only_the_named_ids(tmp_path):
    store = NoticeStore.for_workspace(tmp_path)
    first = store.append("s1", "keep")
    second = store.append("s1", "deliver me")
    third = store.append("s1", "keep too")

    store.deliver("s1", [second])

    rows = store.pending("s1")
    assert [row["id"] for row in rows] == [first, third]
    assert [row["text"] for row in rows] == ["keep", "keep too"]


def test_deliver_of_an_unknown_id_changes_nothing(tmp_path):
    store = NoticeStore.for_workspace(tmp_path)
    kept = store.append("s1", "keep")

    store.deliver("s1", ["nope"])

    assert [row["id"] for row in store.pending("s1")] == [kept]


def test_deliver_all_leaves_an_empty_lane(tmp_path):
    store = NoticeStore.for_workspace(tmp_path)
    ids = [store.append("s1", "a"), store.append("s1", "b")]

    store.deliver("s1", ids)

    assert store.pending("s1") == []


def test_rows_are_plain_json_lines(tmp_path):
    store = NoticeStore.for_workspace(tmp_path)
    store.append("s1", "one")
    path = tmp_path / ".ai-coding" / "notices" / "s1.jsonl"

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["text"] == "one"


def test_clip_notice_matches_the_queue_budget():
    assert clip_notice("short") == "short"
    clipped = clip_notice("x" * 2100)
    assert len(clipped) == 2001
    assert clipped.endswith("…")


def test_clip_notice_strips_whitespace_before_cutting():
    """The queue stores stripped text; the disk lane must agree byte-for-byte."""
    assert clip_notice("  short  ") == "short"
    padded = "  " + "z" * 2100 + "  "
    clipped = clip_notice(padded)
    assert clipped == "z" * 2000 + "…"


def test_the_stored_text_is_clipped_too(tmp_path):
    store = NoticeStore.for_workspace(tmp_path)
    store.append("s1", "  " + "y" * 3000 + "  ")

    text = store.pending("s1")[0]["text"]
    assert len(text) == 2001
    assert text.startswith("y") and text.endswith("…")
