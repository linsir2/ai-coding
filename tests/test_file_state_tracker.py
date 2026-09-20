"""TDD (M2-2): FileStateTracker — stale-read protection."""

import time

from ai_coding.security.file_state_tracker import FileState, FileStateTracker


def test_initial_never_read(tmp_path):
    fst = FileStateTracker()
    p = tmp_path / "f.txt"
    p.write_text("hi")
    assert fst.check(p) == FileState.NEVER_READ


def test_after_read_clean(tmp_path):
    fst = FileStateTracker()
    p = tmp_path / "f.txt"
    p.write_text("hi")
    fst.record(p)
    assert fst.check(p) == FileState.CLEAN


def test_external_modification_stale(tmp_path):
    fst = FileStateTracker()
    p = tmp_path / "f.txt"
    p.write_text("v1")
    fst.record(p)
    time.sleep(0.01)
    p.write_text("v2")
    assert fst.check(p) == FileState.STALE


def test_after_reread_clean_again(tmp_path):
    fst = FileStateTracker()
    p = tmp_path / "f.txt"
    p.write_text("v1")
    fst.record(p)
    time.sleep(0.01)
    p.write_text("v2")
    assert fst.check(p) == FileState.STALE
    fst.record(p)
    assert fst.check(p) == FileState.CLEAN


def test_deleted_file_is_stale(tmp_path):
    fst = FileStateTracker()
    p = tmp_path / "f.txt"
    p.write_text("hi")
    fst.record(p)
    p.unlink()
    assert fst.check(p) == FileState.STALE


def test_forget_returns_to_never_read(tmp_path):
    fst = FileStateTracker()
    p = tmp_path / "f.txt"
    p.write_text("hi")
    fst.record(p)
    assert fst.check(p) == FileState.CLEAN
    fst.forget(p)
    assert fst.check(p) == FileState.NEVER_READ
