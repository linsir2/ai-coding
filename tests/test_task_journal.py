"""P2-C2: the task journal folds events and tolerates a crash tail."""

from __future__ import annotations

from ai_coding.core.task_journal import TaskJournal


def test_fold_uses_the_last_event(tmp_path):
    journal = TaskJournal(tmp_path)
    journal.append("s1", "queued", task_id="t1", parent_id="p1")
    journal.append("s1", "running", task_id="t1")
    journal.append("s1", "settled", task_id="t1", run_id="r1", commit="abc")

    records = journal.records("s1")
    assert len(records) == 1
    record = records[0]
    assert record.task_id == "t1"
    assert record.run_id == "r1"
    assert record.commit == "abc"
    assert record.is_terminal is True


def test_pending_after_settled_reopens_the_task(tmp_path):
    journal = TaskJournal(tmp_path)
    journal.append("s1", "running", task_id="t1")
    journal.append("s1", "settled", task_id="t1")
    journal.append("s1", "pending", task_id="t1")

    record = journal.records("s1")[0]
    assert record.last_event == "pending"
    assert record.is_terminal is False
    assert [item.task_id for item in journal.pending("s1")] == ["t1"]


def test_half_written_tail_is_skipped_and_the_next_append_still_parses(tmp_path):
    journal = TaskJournal(tmp_path)
    journal.append("s1", "queued", task_id="t1")
    path = journal.root / "s1.jsonl"
    path.write_text(path.read_text(encoding="utf-8") + '{"broken": ', encoding="utf-8")
    journal.append("s1", "settled", task_id="t1")

    records = journal.records("s1")
    assert len(records) == 1
    assert records[0].last_event == "settled"


def test_recovery_scans_every_session(tmp_path):
    journal = TaskJournal(tmp_path)
    journal.append("s1", "queued", task_id="a")
    journal.append("s2", "running", task_id="b")
    journal.append("s2", "settled", task_id="c")

    pending = journal.pending()
    assert {item.task_id for item in pending} == {"a", "b"}


# ---- P2-C3: merge events and additive facts -------------------------------


def test_merge_events_are_terminal():
    from ai_coding.core.task_journal import TERMINAL_EVENTS

    assert {"merged", "merge_skipped", "merge_failed"} <= TERMINAL_EVENTS


def test_merge_fields_fold_and_last_event_wins(tmp_path):
    journal = TaskJournal(tmp_path)
    journal.append(
        "s1", "merge_queued", task_id="merge:ai_sub_x", parent_id="t1",
        kind="merge", branch="ai_sub_x", base_commit="a" * 40,
        name="merge:ai_sub_x", tip="b" * 40,
    )
    journal.append(
        "s1", "merged", task_id="merge:ai_sub_x", parent_id="t1",
        kind="merge", branch="ai_sub_x", base_commit="a" * 40,
        name="merge:ai_sub_x", commit="c" * 40, error=None,
    )

    record = journal.records("s1")[0]
    assert record.kind == "merge"
    assert record.name == "merge:ai_sub_x"
    assert record.base_commit == "a" * 40
    assert record.commit == "c" * 40
    assert record.is_terminal is True


def test_merge_queued_and_merging_are_pending(tmp_path):
    journal = TaskJournal(tmp_path)
    for event in ("merge_queued", "merging"):
        journal.append("s1", event, task_id=f"merge:{event}", kind="merge")

    pending = {item.task_id for item in journal.pending("s1")}
    assert pending == {"merge:merge_queued", "merge:merging"}


def test_journal_append_reports_failure(tmp_path):
    blocker = tmp_path / ".ai-coding"
    blocker.write_text("not a directory", encoding="utf-8")
    journal = TaskJournal(tmp_path)

    assert journal.append("s1", "queued", task_id="t1") is False
