"""P2-C3: the single-writer merge queue against a real temporary git repo."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ai_coding.core.merge_queue import MergeQueue, MergeQueueClosed, MergeTicket
from ai_coding.core.task_journal import TaskJournal
from ai_coding.core.worktree import WorktreeError, WorktreeManager
from ai_coding.core.write_coordinator import WriteCoordinator
from ai_coding.infra.file_utils import file_version

pytestmark = pytest.mark.asyncio


def _git_available() -> bool:
    import shutil

    return shutil.which("git") is not None


def _sh(args: list[str], cwd: Path) -> str:
    out = subprocess.run(args, cwd=str(cwd), capture_output=True, check=True, text=True)
    return out.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    if not _git_available():
        pytest.skip("git not available")
    root = tmp_path / "repo"
    root.mkdir()
    _sh(["git", "init", "-q", "-b", "main"], root)
    _sh(["git", "config", "user.email", "t@example.com"], root)
    _sh(["git", "config", "user.name", "tester"], root)
    (root / "f.txt").write_text("base\n", encoding="utf-8")
    _sh(["git", "add", "."], root)
    _sh(["git", "commit", "-qm", "base"], root)
    return root


def _head(repo: Path) -> str:
    return _sh(["git", "rev-parse", "HEAD"], repo).strip()


def _branch(repo: Path, name: str, files: dict[str, str]) -> str:
    _sh(["git", "checkout", "-qb", name], repo)
    for path, text in files.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _sh(["git", "add", "-A", "-f"], repo)
    _sh(["git", "commit", "-qm", name], repo)
    tip = _head(repo)
    _sh(["git", "checkout", "main"], repo)
    return tip


class _Policy:
    def __init__(self, mode: str = "off", bypass: bool = False) -> None:
        self.mode = mode
        self.bypass = bypass


class _Backend:
    """A test backend that runs the argv unchanged (no real bwrap)."""

    seccomp_fds: tuple[int, ...] = ()
    tier = "full"

    def available(self) -> bool:
        return True

    def gap(self) -> str:
        return "test.backend"

    def wrap(self, argv, roots):  # noqa: ANN001
        return list(argv)


def _queue(
    repo: Path,
    *,
    policy: _Policy | None = None,
    backend: _Backend | None = None,
    capacity: int = 8,
    verify_timeout: float = 5.0,
) -> MergeQueue:
    return MergeQueue(
        repo,
        worktree_manager=WorktreeManager(repo),
        journal=TaskJournal(repo),
        write_coordinator=WriteCoordinator(str(repo)),
        policy=policy or _Policy(),
        backend=backend,
        capacity=capacity,
        verify_timeout=verify_timeout,
        claim_timeout=1.0,
    )


def _ticket(branch: str, base: str, *, verify_cmd: str | None = None) -> MergeTicket:
    return MergeTicket(
        key=branch,
        source_task_id="t1",
        source_run_id="",
        source_name="probe",
        branch=branch,
        base_commit=base,
        verify_cmd=verify_cmd,
        session_id="s1",
    )


async def test_clean_merge_moves_main_and_keeps_branch(repo: Path) -> None:
    base = _head(repo)
    _branch(repo, "ai_sub_x", {"new.txt": "x\n"})
    queue = _queue(repo)
    ticket = _ticket("ai_sub_x", base)
    try:
        queue.enqueue(ticket)
        out = await queue.wait(ticket)
    finally:
        await queue.aclose()

    assert out["status"] == "merged"
    assert (repo / "new.txt").read_text(encoding="utf-8") == "x\n"
    assert ticket.outcome is out
    assert out["verified"] is False


async def test_already_merged_is_skipped(repo: Path) -> None:
    base = _head(repo)
    _branch(repo, "ai_sub_x", {"new.txt": "x\n"})
    queue = _queue(repo)
    try:
        first = _ticket("ai_sub_x", base)
        queue.enqueue(first)
        assert (await queue.wait(first))["status"] == "merged"
        head = _head(repo)

        second = _ticket("ai_sub_x", base)
        queue.enqueue(second)
        out = await queue.wait(second)
    finally:
        await queue.aclose()

    assert out["status"] == "merge_skipped"
    assert out["error"] == "nothing_to_merge"
    assert _head(repo) == head


async def test_conflict_leaves_main_unchanged(repo: Path) -> None:
    base = _head(repo)
    _branch(repo, "ai_sub_x", {"f.txt": "branch\n"})
    (repo / "f.txt").write_text("main\n", encoding="utf-8")
    _sh(["git", "add", "f.txt"], repo)
    _sh(["git", "commit", "-qm", "main change"], repo)
    before = _head(repo)
    queue = _queue(repo)
    ticket = _ticket("ai_sub_x", base)
    try:
        queue.enqueue(ticket)
        out = await queue.wait(ticket)
    finally:
        await queue.aclose()

    assert out["status"] == "conflict"
    assert "f.txt" in out["conflicting_paths"]
    assert _head(repo) == before
    assert (repo / "f.txt").read_text(encoding="utf-8") == "main\n"


async def test_dirty_tracked_main_is_refused(repo: Path) -> None:
    base = _head(repo)
    _branch(repo, "ai_sub_x", {"new.txt": "x\n"})
    (repo / "f.txt").write_text("dirty\n", encoding="utf-8")
    before = _head(repo)
    queue = _queue(repo)
    ticket = _ticket("ai_sub_x", base)
    try:
        queue.enqueue(ticket)
        out = await queue.wait(ticket)
    finally:
        await queue.aclose()

    assert out["status"] == "dirty"
    assert _head(repo) == before
    assert (repo / "f.txt").read_text(encoding="utf-8") == "dirty\n"


async def test_untracked_collision_is_refused(repo: Path) -> None:
    base = _head(repo)
    _branch(repo, "ai_sub_x", {"new.txt": "branch\n"})
    (repo / "new.txt").write_text("main\n", encoding="utf-8")
    queue = _queue(repo)
    ticket = _ticket("ai_sub_x", base)
    try:
        queue.enqueue(ticket)
        out = await queue.wait(ticket)
    finally:
        await queue.aclose()

    assert out["status"] == "dirty"
    assert (repo / "new.txt").read_text(encoding="utf-8") == "main\n"


async def test_ignored_collision_is_refused(repo: Path) -> None:
    base = _head(repo)
    (repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
    _sh(["git", "add", ".gitignore"], repo)
    _sh(["git", "commit", "-qm", "ignore"], repo)
    base = _head(repo)
    _branch(repo, "ai_sub_x", {"x.log": "branch\n"})
    (repo / "x.log").write_text("main\n", encoding="utf-8")
    queue = _queue(repo)
    ticket = _ticket("ai_sub_x", base)
    try:
        queue.enqueue(ticket)
        out = await queue.wait(ticket)
    finally:
        await queue.aclose()

    assert out["status"] == "dirty"
    assert (repo / "x.log").read_text(encoding="utf-8") == "main\n"


async def test_non_colliding_untracked_does_not_block(repo: Path) -> None:
    base = _head(repo)
    _branch(repo, "ai_sub_x", {"new.txt": "branch\n"})
    (repo / "other.txt").write_text("main\n", encoding="utf-8")
    queue = _queue(repo)
    ticket = _ticket("ai_sub_x", base)
    try:
        queue.enqueue(ticket)
        out = await queue.wait(ticket)
    finally:
        await queue.aclose()

    assert out["status"] == "merged"
    assert (repo / "new.txt").read_text(encoding="utf-8") == "branch\n"


async def test_verify_failure_leaves_main_unchanged(repo: Path) -> None:
    base = _head(repo)
    _branch(repo, "ai_sub_x", {"new.txt": "branch\n"})
    before = _head(repo)
    queue = _queue(repo, policy=_Policy("enforce"), backend=_Backend())
    ticket = _ticket("ai_sub_x", base, verify_cmd="exit 3")
    try:
        queue.enqueue(ticket)
        out = await queue.wait(ticket)
    finally:
        await queue.aclose()

    assert out["status"] == "verify_failed"
    assert out["verify"]["exit_code"] == 3
    assert _head(repo) == before
    assert not (repo / "new.txt").exists()


async def test_verify_requires_an_enforced_sandbox(repo: Path) -> None:
    base = _head(repo)
    _branch(repo, "ai_sub_x", {"new.txt": "branch\n"})
    queue = _queue(repo, policy=_Policy("off"))
    ticket = _ticket("ai_sub_x", base, verify_cmd="exit 0")
    try:
        queue.enqueue(ticket)
        out = await queue.wait(ticket)
    finally:
        await queue.aclose()

    assert out["status"] == "verify_unavailable"


async def test_enqueue_after_close_is_rejected(repo: Path) -> None:
    base = _head(repo)
    _branch(repo, "ai_sub_x", {"new.txt": "x\n"})
    queue = _queue(repo)
    await queue.aclose()
    with pytest.raises(MergeQueueClosed):
        queue.enqueue(_ticket("ai_sub_x", base))


async def test_two_tickets_are_serial_fifo(repo: Path) -> None:
    base = _head(repo)
    _branch(repo, "ai_sub_a", {"a.txt": "a\n"})
    _branch(repo, "ai_sub_b", {"b.txt": "b\n"})
    queue = _queue(repo)
    first = _ticket("ai_sub_a", base)
    second = _ticket("ai_sub_b", base)
    try:
        queue.enqueue(first)
        queue.enqueue(second)
        out1 = await queue.wait(first)
        out2 = await queue.wait(second)
    finally:
        await queue.aclose()

    assert out1["status"] == "merged"
    assert out2["status"] == "merged"
    assert (repo / "a.txt").read_text(encoding="utf-8") == "a\n"
    assert (repo / "b.txt").read_text(encoding="utf-8") == "b\n"


async def test_claim_busy_is_reported_and_released(repo: Path) -> None:
    base = _head(repo)
    _branch(repo, "ai_sub_x", {"new.txt": "x\n"})
    coord = WriteCoordinator(str(repo))
    claim = await coord.claim("other", repo, (), 0)
    assert claim is not None
    queue = MergeQueue(
        repo,
        worktree_manager=WorktreeManager(repo),
        journal=TaskJournal(repo),
        write_coordinator=coord,
        policy=_Policy(),
        backend=None,
        claim_timeout=0.05,
    )
    ticket = _ticket("ai_sub_x", base)
    try:
        queue.enqueue(ticket)
        out = await queue.wait(ticket)
    finally:
        await queue.aclose()
        claim.release()

    assert out["status"] == "busy"


async def test_journal_write_failure_is_reported(repo: Path) -> None:
    base = _head(repo)
    _branch(repo, "ai_sub_x", {"new.txt": "x\n"})
    (repo / ".ai-coding").write_text("not a directory", encoding="utf-8")
    queue = _queue(repo)
    from ai_coding.core.merge_queue import MergeJournalError

    with pytest.raises(MergeJournalError):
        queue.enqueue(_ticket("ai_sub_x", base))
    await queue.aclose()


async def test_verify_success_marks_verified(repo: Path) -> None:
    base = _head(repo)
    _branch(repo, "ai_sub_x", {"new.txt": "x\n"})
    queue = _queue(repo, policy=_Policy("enforce"), backend=_Backend())
    ticket = _ticket("ai_sub_x", base, verify_cmd="true")
    try:
        queue.enqueue(ticket)
        out = await queue.wait(ticket)
    finally:
        await queue.aclose()

    assert out["status"] == "merged"
    assert out["verified"] is True
    assert out["verify"]["exit_code"] == 0


async def test_branch_moved_during_verify_is_refused(repo: Path) -> None:
    base = _head(repo)
    _branch(repo, "ai_sub_x", {"new.txt": "x\n"})
    before = _head(repo)
    command = f"git -C {repo} branch -f ai_sub_x HEAD"
    queue = _queue(repo, policy=_Policy("enforce"), backend=_Backend())
    ticket = _ticket("ai_sub_x", base, verify_cmd=command)
    try:
        queue.enqueue(ticket)
        out = await queue.wait(ticket)
    finally:
        await queue.aclose()

    assert out["status"] == "branch_moved"
    assert _head(repo) == before


async def test_cleanup_failure_after_merge_is_reported_not_failed(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _head(repo)
    _branch(repo, "ai_sub_x", {"new.txt": "x\n"})
    queue = _queue(repo)
    manager = queue._worktrees

    async def _remove(_path):
        raise WorktreeError("cannot remove")

    monkeypatch.setattr(manager, "remove", _remove)
    ticket = _ticket("ai_sub_x", base)
    try:
        queue.enqueue(ticket)
        out = await queue.wait(ticket)
    finally:
        await queue.aclose()

    assert out["status"] == "merged", out
    assert "cannot remove" in out["cleanup_error"]
    assert (repo / "new.txt").exists()


async def test_head_moved_twice_reports_head_moved(repo: Path) -> None:
    base = _head(repo)
    _branch(repo, "ai_sub_x", {"new.txt": "x\n"})
    command = f"git -C {repo} commit --allow-empty -m move"
    queue = _queue(repo, policy=_Policy("enforce"), backend=_Backend())
    ticket = _ticket("ai_sub_x", base, verify_cmd=command)
    try:
        queue.enqueue(ticket)
        out = await queue.wait(ticket)
    finally:
        await queue.aclose()

    assert out["status"] == "head_moved"
    assert out["attempts"] == 2


async def test_scheduler_and_real_queue_share_the_merge_dataflow(repo: Path) -> None:
    from ai_coding.core.execution_slots import ExecutionSlotPool
    from ai_coding.core.scheduler import Scheduler
    from ai_coding.core.task_registry import TaskRegistry
    from ai_coding.domain.subagent import BackgroundTask

    base = _head(repo)
    _branch(repo, "ai_sub_x", {"new.txt": "x\n"})

    class _Runner:
        def task_list(self):
            return [BackgroundTask(
                run_id="run_a", name="probe", status="completed",
                started=1.0, ended=2.0, worktree_branch="ai_sub_x",
                base_commit=base,
            )]

        def close_info(self, target):
            return {}

    journal = TaskJournal(repo)
    queue = MergeQueue(
        repo,
        worktree_manager=WorktreeManager(repo),
        journal=journal,
        write_coordinator=WriteCoordinator(str(repo)),
        policy=_Policy(),
        backend=None,
        claim_timeout=1.0,
    )
    runner = _Runner()
    scheduler = Scheduler(
        runner, registry=TaskRegistry(runner), slots=ExecutionSlotPool(1),
        journal=journal, merge_queue=queue,
    )
    try:
        out = await scheduler.merge("run_a")
        view = scheduler.tasks().get("merge:ai_sub_x")
    finally:
        await scheduler.aclose()

    assert out["status"] == "merged"
    assert view is not None and view.kind == "merge"
    assert view.status == "merged"
    assert (repo / "new.txt").read_text(encoding="utf-8") == "x\n"


async def test_a_merged_ticket_reports_landed_file_changes(repo: Path) -> None:
    base = _head(repo)
    _branch(repo, "ai_sub_changes", {"new.txt": "x\n"})
    queue = _queue(repo)
    ticket = _ticket("ai_sub_changes", base)
    try:
        queue.enqueue(ticket)
        out = await queue.wait(ticket)
    finally:
        await queue.aclose()

    assert out["status"] == "merged"
    assert out["changed_paths"] == ["new.txt"]
    assert out["file_changes"] == [
        {
            "path": "new.txt",
            "kind": "write",
            "version": file_version(repo / "new.txt"),
        }
    ]


async def test_a_landed_rename_and_delete_are_reported(repo: Path) -> None:
    base = _head(repo)
    _sh(["git", "checkout", "-qb", "ai_sub_rename"], repo)
    (repo / "f.txt").unlink()
    (repo / "renamed.txt").write_text("renamed\n", encoding="utf-8")
    _sh(["git", "add", "-A"], repo)
    _sh(["git", "commit", "-qm", "rename"], repo)
    _sh(["git", "checkout", "main"], repo)
    queue = _queue(repo)
    ticket = _ticket("ai_sub_rename", base)
    try:
        queue.enqueue(ticket)
        out = await queue.wait(ticket)
    finally:
        await queue.aclose()

    assert out["status"] == "merged"
    changes = {(c["path"], c["kind"]) for c in out["file_changes"]}
    assert ("f.txt", "delete") in changes
    assert ("renamed.txt", "write") in changes


async def test_a_landed_symlink_is_not_a_forged_write_receipt(repo: Path) -> None:
    import os

    base = _head(repo)
    outside = repo.parent / "secret.txt"
    outside.write_text("secret\n", encoding="utf-8")
    _sh(["git", "checkout", "-qb", "ai_sub_link"], repo)
    os.symlink(outside, repo / "evil")
    _sh(["git", "add", "-A"], repo)
    _sh(["git", "commit", "-qm", "link"], repo)
    _sh(["git", "checkout", "main"], repo)
    queue = _queue(repo)
    ticket = _ticket("ai_sub_link", base)
    try:
        queue.enqueue(ticket)
        out = await queue.wait(ticket)
    finally:
        await queue.aclose()

    assert out["status"] == "merged"
    assert out.get("file_changes") == []
