"""TDD (M4-2): WorktreeManager — git worktree create/remove/list isolation.

These tests run real ``git`` locally (no network).  They either skip or assert
``can_isolate()`` is False when git is unavailable / the dir isn't a repo.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from ai_coding.core import worktree
from ai_coding.core.worktree import WorktreeError, WorktreeManager

pytestmark = pytest.mark.asyncio


def _git_available() -> bool:
    return shutil.which("git") is not None


def _sh(args: list[str], cwd: Path) -> str:
    out = subprocess.run(
        args, cwd=str(cwd), capture_output=True, check=True, text=True
    )
    return out.stdout


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    if not _git_available():
        pytest.skip("git not available")
    repo = tmp_path / "repo"
    repo.mkdir()
    _sh(["git", "init", "-q", "-b", "main"], repo)
    _sh(["git", "config", "user.email", "t@example.com"], repo)
    _sh(["git", "config", "user.name", "tester"], repo)
    (repo / "f.txt").write_text("hello\n", encoding="utf-8")
    _sh(["git", "add", "."], repo)
    _sh(["git", "commit", "-qm", "init"], repo)
    return repo


async def test_can_isolate_true_in_git_repo(git_repo: Path) -> None:
    assert await WorktreeManager(git_repo).can_isolate() is True


async def test_can_isolate_false_outside_repo(tmp_path: Path) -> None:
    assert await WorktreeManager(tmp_path / "nope").can_isolate() is False


async def test_create_produces_workdir_and_registers(git_repo: Path) -> None:
    mgr = WorktreeManager(git_repo)
    wt = await mgr.create("coder")
    try:
        assert wt.path.exists()
        assert (wt.path / "f.txt").exists()  # checked out content
        assert wt.branch.startswith("ai_sub_")
        listed = await mgr.list()
        assert str(wt.path) in listed
    finally:
        await mgr.remove(str(wt.path))


async def test_remove_cleans_up(git_repo: Path) -> None:
    mgr = WorktreeManager(git_repo)
    wt = await mgr.create("scratch")
    await mgr.remove(str(wt.path))
    assert not wt.path.exists() or not wt.path.is_dir()
    assert str(wt.path) not in await mgr.list()


async def test_temporary_scoped_cleanup(git_repo: Path) -> None:
    mgr = WorktreeManager(git_repo)
    inside: Path | None = None
    async with mgr.temporary("scope") as wt:
        inside = wt.path
        assert inside.exists()
    assert inside is not None and not inside.exists()
    assert str(inside) not in await mgr.list()


async def test_remove_unknown_path_raises(git_repo: Path) -> None:
    mgr = WorktreeManager(git_repo)
    with pytest.raises(WorktreeError):
        await mgr.remove(str(git_repo / "does-not-exist"))


async def test_worktrees_land_under_repo_worktrees(git_repo: Path) -> None:
    mgr = WorktreeManager(git_repo)
    wt = await mgr.create("x")
    assert str(wt.path).startswith(str(git_repo / "worktrees"))
    await mgr.remove(str(wt.path))


# ---- R2b: bounded git ---------------------------------------------------
# ``_git`` used to await ``communicate()`` with no timeout at all, so one stuck
# git call froze the whole turn (docs/r2-analysis.md 1.4-B).  An unrealistically
# small timeout is the honest way to exercise the real code path.


async def test_can_isolate_is_false_when_git_times_out(git_repo: Path) -> None:
    assert await WorktreeManager(git_repo, timeout=0.001).can_isolate() is False


async def test_list_returns_empty_when_git_times_out(git_repo: Path) -> None:
    assert await WorktreeManager(git_repo, timeout=0.001).list() == []


async def test_create_raises_worktree_error_when_git_times_out(git_repo: Path) -> None:
    with pytest.raises(WorktreeError, match="timed out"):
        await WorktreeManager(git_repo, timeout=0.001).create("slow")


async def test_git_spawn_failure_is_mapped(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A runner-level failure must not leak as an OSError to the caller.

    r17/T1: the invocation seam moved to ``core/gitcmd.git`` (worktrees and
    snapshots now share one bounded git call), so the patch targets it — the
    behaviour under test is unchanged.
    """

    async def _spawn_error(*_args: object, **_kwargs: object) -> tuple[int, str]:
        raise OSError("FileNotFoundError: git")

    monkeypatch.setattr(worktree, "git", _spawn_error)
    mgr = WorktreeManager(git_repo)
    assert await mgr.can_isolate() is False
    with pytest.raises(WorktreeError, match="could not run git"):
        await mgr.create("x")
