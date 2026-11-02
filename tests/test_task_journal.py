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
