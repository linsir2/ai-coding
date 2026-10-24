"""TDD (r17/T1): git-stash snapshots and per-file restore.

Reference behaviour: OpenCode captures a tree per step and restores files from
it (snapshot.ts:90-131, git.ts:596-623).  We take one stash-shaped snapshot per
user turn and pin it under refs/ai-coding/ so gc cannot collect it.

``git stash create`` does not capture untracked files, so the untracked list is
recorded as a blob (a ref may point at a blob) and restore removes only files
that baseline does not name.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ai_coding.core.snapshots import SnapshotStore


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        [
            "git", "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-qm", "init", "--allow-empty",
        ],
        cwd=root,
        check=True,
    )
    (root / "a.txt").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "add", "a.txt"],
        cwd=root,
        check=True,
    )
    return root


@pytest.mark.asyncio
async def test_capture_then_restore_brings_back_the_pre_turn_content(tmp_path):
    root = _repo(tmp_path)
    store = SnapshotStore(root)

    ref = await store.capture("s/1")
    assert ref is not None and await store.exists("s/1")

    # the "model" edits a tracked file and creates a new one
    (root / "a.txt").write_text("broken\n", encoding="utf-8")
    (root / "new.txt").write_text("new\n", encoding="utf-8")

    # r18/F4: the turn tells us it created new.txt; the tracked edit needs no
    # such hint -- git already knows a.txt changed since the snapshot.
    report = await store.restore("s/1", created={"new.txt"})

    assert (root / "a.txt").read_text(encoding="utf-8") == "v1\n"
    assert not (root / "new.txt").exists(), "a file the snapshot never knew is removed"
    assert sorted(report.restored) == ["a.txt", "new.txt"]


@pytest.mark.asyncio
async def test_a_file_that_cannot_be_deleted_is_reported_not_raised(tmp_path, refused):
    """restore is a transaction: it either says what came back or it says what
    did not.  Raising halfway through used to leave the caller -- and the user
    -- believing the whole tree had been rewound, when the files before the
    unreadable one had already gone and the ones after it had not."""
    root = _repo(tmp_path)
    store = SnapshotStore(root)
    await store.capture("s/1")
    locked = root / "ro"
    locked.mkdir()
    (locked / "kept.txt").write_text("x\n", encoding="utf-8")
    (locked / "after_in_locked.txt").write_text("z\n", encoding="utf-8")
    (root / "after.txt").write_text("y\n", encoding="utf-8")
    with refused(locked / "kept.txt"):
        report = await store.restore("s/1", created={"ro/kept.txt"})

        assert "ro/kept.txt" in report.failed, (
            f"the undeletable file is not reported as failed: {report}"
        )
        assert (locked / "kept.txt").exists(), (
            "the refused file must survive the restore"
        )


@pytest.mark.asyncio
async def test_restore_counts_only_what_it_actually_did(tmp_path):
    """The report is the undo's evidence.  A path git refused to check out was
    not restored, and saying otherwise is how undo came to claim a clean
    rewind it had not performed."""
    root = _repo(tmp_path)
    store = SnapshotStore(root)
    await store.capture("s/1")
    (root / "a.txt").write_text("broken\n", encoding="utf-8")
    (root / "new.txt").write_text("new\n", encoding="utf-8")

    report = await store.restore("s/1", created={"new.txt"})

    assert sorted(report.restored) == ["a.txt", "new.txt"]
    assert report.failed == []


@pytest.mark.asyncio
async def test_a_clean_tree_snapshots_head(tmp_path):
    root = _repo(tmp_path)
    store = SnapshotStore(root)

    assert await store.capture("s/1") is not None
    (root / "a.txt").write_text("broken\n", encoding="utf-8")

    await store.restore("s/1")

    assert (root / "a.txt").read_text(encoding="utf-8") == "v1\n"


@pytest.mark.asyncio
async def test_a_non_repo_degrades_to_no_snapshot(tmp_path):
    (tmp_path / "plain").mkdir()
    store = SnapshotStore(tmp_path / "plain")

    assert await store.capture("s/1") is None
    assert await store.exists("s/1") is False


@pytest.mark.asyncio
async def test_prune_keeps_the_newest_refs_only(tmp_path):
    root = _repo(tmp_path)
    store = SnapshotStore(root, keep=2)
    for n in (1, 2, 3):
        await store.capture(f"s/{n}")

    await store.prune("s")

    assert await store.exists("s/3") and await store.exists("s/2")
    assert not await store.exists("s/1")


@pytest.mark.asyncio
async def test_only_the_files_the_turn_reported_are_deleted(tmp_path):
    """r18/F4 replaced the untracked baseline with the turn's own report.

    A file the human created -- before the turn or while it ran -- is not in that
    report, so it stays.  This is the rule that a baseline could not express:
    "untracked now" is the same for a file the model wrote and one the user did.
    """
    root = _repo(tmp_path)
    (root / "user-draft.txt").write_text("mine\n", encoding="utf-8")  # untracked before the turn
    store = SnapshotStore(root)

    await store.capture("s/1")
    (root / "a.txt").write_text("broken\n", encoding="utf-8")
    (root / "created.txt").write_text("agent\n", encoding="utf-8")
    (root / "typed-mid-turn.txt").write_text("mine too\n", encoding="utf-8")

    report = await store.restore("s/1", created={"created.txt"})

    assert (root / "user-draft.txt").exists(), "the user's own untracked file is untouched"
    assert (root / "typed-mid-turn.txt").exists(), (
        "restore deleted a file the turn never reported writing"
    )
    assert not (root / "created.txt").exists(), "the file the turn did report is gone"
    assert sorted(report.restored) == ["a.txt", "created.txt"]
