"""H1 regression: the tracker keys fd-verified paths, not resolved ones."""

from __future__ import annotations

from ai_coding.security.file_state_tracker import FileState, FileStateTracker


def test_stat_key_is_lexical_not_resolved(tmp_path):
    """A symlink alias and its target are distinct stat keys.

    Old code ``Path.resolve()``d the key, so a swap moved the snapshot;
    new code keys the fd-verified path (``opened.path``) itself.
    """
    target = tmp_path / "real.txt"
    target.write_text("x", encoding="utf-8")
    alias = tmp_path / "link.txt"
    alias.symlink_to(target)

    tracker = FileStateTracker()
    st = target.stat()
    tracker.record_stat(alias, mtime=st.st_mtime, size=st.st_size)

    assert tracker.check_stat(
        alias, mtime=st.st_mtime, size=st.st_size
    ) is FileState.CLEAN
    # The target was never recorded under its own lexical key.
    assert tracker.check_stat(
        target, mtime=st.st_mtime, size=st.st_size
    ) is FileState.NEVER_READ
