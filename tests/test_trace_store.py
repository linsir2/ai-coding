"""TDD (R4a): trace storage — numbering, caps, retention, atomic summaries.

A trace is per turn, appended line by line, and must never grow without bound or
outlive its usefulness (docs/r4-analysis.md 4.2/3.4).
"""

import json
import os
import time
from pathlib import Path

from ai_coding.core.trace import TraceStore, canonical_request, digest_of, prefix_shared
from ai_coding.domain.message import ChatMessage


def _store(tmp_path: Path, **over: object) -> TraceStore:
    kwargs: dict[str, object] = {"max_turn_bytes": 100_000, "retention_days": 14}
    kwargs.update(over)
    return TraceStore(tmp_path / "traces", **kwargs)  # type: ignore[arg-type]


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
