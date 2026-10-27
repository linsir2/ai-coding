"""TDD (R4a): trace storage — numbering, caps, retention, atomic summaries.

A trace is per turn, appended line by line, and must never grow without bound or
outlive its usefulness (docs/r4-analysis.md 4.2/3.4).
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ai_coding.core.trace import TraceStore, canonical_request, digest_of, prefix_shared
from ai_coding.domain.message import ChatMessage


def _store(tmp_path: Path, **over: object) -> TraceStore:
    kwargs: dict[str, object] = {
        "max_turn_bytes": 100_000,
        "retention_days": 14,
        "workspace": tmp_path,
    }
    kwargs.update(over)
    return TraceStore(tmp_path / "traces", **kwargs)  # type: ignore[arg-type]


def _iso(epoch: float) -> str:
    """A trace line's ``ts`` for a given epoch second (millisecond precision)."""
    return (
        datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        + "Z"
    )


def _line(epoch: float) -> str:
    """One trace line carrying a real ``ts``, which is what ``last_activity`` reads.

    ``"{}"`` (what the pre-M-G2 tests wrote) has no ``ts``, so it exercises the
    directory-mtime fallback instead of the in-file timestamp.
    """
    return json.dumps({"v": 1, "ts": _iso(epoch), "evt": "X", "data": {}})


def test_begin_turn_creates_the_directory_and_numbers_turns(tmp_path):
    store = _store(tmp_path)

    first = store.begin_turn("sess-1")
    assert first.path.name == "turn-1.jsonl"
    assert first.path.parent == tmp_path / "traces" / "sess-1"
    assert first.path.parent.is_dir()

    second = store.begin_turn("sess-1")
    assert second.path.name == "turn-2.jsonl"


def test_turn_numbering_survives_a_fresh_process(tmp_path):
    store = _store(tmp_path)
    store.begin_turn("sess-1").write_line('{"a":1}')
    store.begin_turn("sess-1").write_line('{"a":2}')

    # a new store object (i.e. the next CLI invocation) continues the sequence
    again = _store(tmp_path).begin_turn("sess-1")
    assert again.path.name == "turn-3.jsonl"


def test_lines_are_appended_in_order(tmp_path):
    writer = _store(tmp_path).begin_turn("sess-1")
    assert writer.write_line('{"n":1}')
    assert writer.write_line('{"n":2}')

    lines = writer.path.read_text(encoding="utf-8").strip().splitlines()
    assert [json.loads(line)["n"] for line in lines] == [1, 2]


def test_the_byte_cap_truncates_and_marks(tmp_path):
    writer = _store(tmp_path, max_turn_bytes=60).begin_turn("sess-1")
    payload = '{"pad":"' + "x" * 40 + '"}'

    accepted = [writer.write_line(payload) for _ in range(10)]

    assert accepted.count(True) < 10, "the cap must eventually refuse a line"
    assert writer.truncated is True
    text = writer.path.read_text(encoding="utf-8")
    assert '"evt":"truncated"' in text.replace(" ", "")


def test_prune_drops_session_directories_past_retention(tmp_path):
    store = _store(tmp_path, retention_days=1)
    fresh = store.begin_turn("fresh")
    fresh.write_line('{"a":1}')
    stale_dir = tmp_path / "traces" / "stale"
    stale_dir.mkdir(parents=True)
    (stale_dir / "turn-1.jsonl").write_text('{"a":1}', encoding="utf-8")
    old = time.time() - 10 * 24 * 3600
    os.utime(stale_dir, (old, old))

    removed = store.prune()

    assert removed == 1
    assert not stale_dir.exists()
    assert fresh.path.exists()


# ---- R9b: listing sessions (docs/r9-analysis.md 3.2) ----


def test_r9_sessions_lists_directories_with_last_activity_and_turns(tmp_path):
    store = _store(tmp_path)
    stamp = time.time() - 60
    store.begin_turn("sess-1").write_line(_line(stamp))
    store.begin_turn("sess-1").write_line(_line(stamp + 1))
    (store.root / "stray-file.txt").write_text("not a session", encoding="utf-8")

    refs = store.sessions()

    assert [ref.session for ref in refs] == ["sess-1"]
    assert refs[0].turns == 2
    assert refs[0].path == store.session_dir("sess-1")
    # The in-file ts, not the directory mtime: an rsync rewrites the latter.
    assert refs[0].last_activity == pytest.approx(stamp + 1, abs=0.01)
    assert refs[0].last_activity != store.session_dir("sess-1").stat().st_mtime


def test_r9_sessions_are_newest_first(tmp_path):
    store = _store(tmp_path)
    store.begin_turn("old").write_line(_line(1000.0))
    store.begin_turn("new").write_line(_line(2000.0))

    assert [ref.session for ref in store.sessions()] == ["new", "old"]


def test_r9_a_new_turn_file_refreshes_last_activity(tmp_path):
    """`--since` and `prune` share this basis, so it must hold."""
    store = _store(tmp_path)
    store.begin_turn("sess-1").write_line(_line(1000.0))
    assert store.sessions()[0].last_activity == pytest.approx(1000.0, abs=0.01)
    store.begin_turn("sess-1").write_line(_line(2000.0))

    assert store.sessions()[0].last_activity == pytest.approx(2000.0, abs=0.01)


def test_r9_prune_still_removes_only_expired_sessions(tmp_path):
    store = _store(tmp_path, retention_days=1)
    store.begin_turn("fresh").write_line(_line(time.time()))
    store.begin_turn("stale").write_line(_line(time.time() - 3 * 86400))

    assert store.prune() == 1
    assert store.session_dir("fresh").is_dir()
    assert not store.session_dir("stale").exists()


def test_r9_sessions_on_a_missing_root_is_empty(tmp_path):
    assert TraceStore(tmp_path / "nope", workspace=tmp_path).sessions() == ()


# ---- M-G2: the time source is the trace itself, not the directory -----------


def test_r9_an_rsync_does_not_move_last_activity(tmp_path):
    """The acceptance criterion: a copied tree keeps its real activity times.

    ``rsync`` without ``-t`` rewrites every directory's mtime to the transfer
    time.  Before M-G2 that made every session look brand new, so
    ``build_report(since_days=N)`` returned everything and ``prune`` never
    deleted anything again.
    """
    store = _store(tmp_path)
    stale = time.time() - 30 * 86400
    store.begin_turn("sess-1").write_line(_line(stale))
    # the rsync: the directory now looks like it was written a moment ago
    os.utime(store.session_dir("sess-1"), (time.time(), time.time()))

    assert store.sessions()[0].last_activity == pytest.approx(stale, abs=0.01)


def test_r9_prune_survives_a_newer_directory_mtime(tmp_path):
    store = _store(tmp_path, retention_days=1)
    store.begin_turn("sess-1").write_line(_line(time.time() - 3 * 86400))
    os.utime(store.session_dir("sess-1"), (time.time(), time.time()))

    assert store.prune() == 1
    assert not store.session_dir("sess-1").exists()


def test_r9_no_ts_falls_back_to_the_directory_mtime(tmp_path):
    """Hand-written or historical files carry no ``ts``; they keep an age."""
    store = _store(tmp_path)
    store.begin_turn("sess-1").write_line("{}")
    mtime = store.session_dir("sess-1").stat().st_mtime

    assert store.sessions()[0].last_activity == pytest.approx(mtime, abs=0.01)


def test_r9_an_empty_newest_turn_file_falls_back_to_the_previous_one(tmp_path):
    store = _store(tmp_path)
    store.begin_turn("sess-1").write_line(_line(1000.0))
    store.begin_turn("sess-1")  # turn-2 created, nothing written

    assert store.sessions()[0].last_activity == pytest.approx(1000.0, abs=0.01)


def test_r9_last_line_longer_than_the_tail_window(tmp_path):
    """``capture: full`` writes untrimmed payloads; one line can be megabytes."""
    store = _store(tmp_path, max_turn_bytes=10_000_000)
    stamp = 1234.0
    store.begin_turn("sess-1").write_line(
        json.dumps({"v": 1, "ts": _iso(stamp), "pad": "x" * 20_000})
    )

    assert store.sessions()[0].last_activity == pytest.approx(stamp, abs=0.01)


def test_r9_equal_last_activity_breaks_ties_by_session_id(tmp_path):
    store = _store(tmp_path)
    for name in ("b", "a"):
        store.begin_turn(name).write_line(_line(1000.0))

    assert [ref.session for ref in store.sessions()] == ["a", "b"]


def test_r9_sessions_are_ordered_by_activity_not_directory_mtime(tmp_path):
    """An rsync'd directory must not jump to the top of the list.

    The two orders disagree here on purpose: ``old`` has the older trace and the
    newer directory, which is exactly what a copy produces.
    """
    store = _store(tmp_path)
    now = time.time()
    store.begin_turn("old").write_line(_line(now - 3600))
    store.begin_turn("new").write_line(_line(now))
    os.utime(store.session_dir("old"), (now + 60, now + 60))

    assert [ref.session for ref in store.sessions()] == ["new", "old"]


def test_parse_ts_rejects_malformed_values():
    from ai_coding.core.trace import _parse_ts

    assert _parse_ts(None) is None
    assert _parse_ts(1234) is None
    assert _parse_ts("not a timestamp") is None
    assert _parse_ts("2026-10-27T07:24:54.623Z") == pytest.approx(1793085894.623)


def test_summary_is_written_atomically_and_merges(tmp_path):
    store = _store(tmp_path)
    store.update_summary("sess-1", {"turns": 1, "tokens": 100})
    store.update_summary("sess-1", {"turns": 2, "tokens": 350})

    data = json.loads((tmp_path / "traces" / "sess-1" / "summary.json").read_text("utf-8"))
    assert data == {"turns": 2, "tokens": 350}
    # no temp file left behind
    leftovers = [p.name for p in (tmp_path / "traces" / "sess-1").iterdir() if ".tmp" in p.name]
    assert leftovers == []


def test_prefix_shared_measures_the_common_head():
    previous = "abcdefghij"
    assert prefix_shared(previous, previous) == (10, 1.0)
    shared, ratio = prefix_shared(previous, "abcdZZZZZZZZZZ")
    assert shared == 4
    assert ratio == 4 / len("abcdZZZZZZZZZZ")


def test_prefix_shared_handles_missing_previous():
    assert prefix_shared(None, "anything") == (0, 0.0)
    assert prefix_shared("anything", "") == (0, 0.0)


def test_canonical_request_covers_system_history_and_tools():
    messages = [
        ChatMessage(role="user", content="hi", timestamp=""),
        ChatMessage(role="assistant", content="there", timestamp=""),
    ]
    schemas = '[{"name":"read"}]'
    text = canonical_request("SYS", messages, schemas)
    assert "SYS" in text
    assert "hi" in text and "there" in text
    # The schema *text* is included, not just its length: schemas are a real part
    # of the prompt (measured ~5k chars), so a count alone understates the request.
    assert schemas in text
    assert len(canonical_request("SYS", messages, "x" * 4000)) > 4000
    # an identical request is byte-identical; changing the system prompt moves it
    assert text == canonical_request("SYS", messages, schemas)
    assert text != canonical_request("SYS2", messages, schemas)


def test_digest_is_stable_and_short():
    assert digest_of("hello world") == digest_of("hello world")
    assert digest_of("hello world") != digest_of("hello worlds")
    assert len(digest_of("x" * 10_000)) == 16


def test_prune_refuses_a_root_outside_the_workspace(tmp_path):
    """``prune`` walks and deletes whatever is under ``root``.

    Without a workspace to compare against it must refuse rather than guess: a
    ``root`` of ``/home/me`` would take the user's home with it.  Fail-closed --
    "the check is the store's own, not its caller's".
    """
    outside = tmp_path / "outside"
    old = outside / "old-session"
    old.mkdir(parents=True)
    (old / "turn-1.jsonl").write_text("{}\n", encoding="utf-8")
    past = time.time() - 3 * 86400
    os.utime(old, (past, past))

    store = TraceStore(outside, retention_days=1)

    assert store.prune() == 0
    assert old.is_dir(), "prune deleted a root it cannot prove is safe"
