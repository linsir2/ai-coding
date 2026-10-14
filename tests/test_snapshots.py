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
    restored = await store.restore("s/1", created={"new.txt"})

    assert (root / "a.txt").read_text(encoding="utf-8") == "v1\n"
    assert not (root / "new.txt").exists(), "a file the snapshot never knew is removed"
    assert sorted(restored) == ["a.txt", "new.txt"]


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

    restored = await store.restore("s/1", created={"created.txt"})

    assert (root / "user-draft.txt").exists(), "the user's own untracked file is untouched"
    assert (root / "typed-mid-turn.txt").exists(), (
        "restore deleted a file the turn never reported writing"
    )
    assert not (root / "created.txt").exists(), "the file the turn did report is gone"
    assert sorted(restored) == ["a.txt", "created.txt"]
