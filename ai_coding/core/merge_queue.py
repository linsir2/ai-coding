"""P2-C3: the single-writer merge queue.

The queue owns the *merge attempt*: it resolves the source branch, proves the
candidate in a temporary worktree, optionally verifies it there, and only then
fast-forwards the main working tree.  It never resolves the source task (that is
``Scheduler._merge_source``) and never decides whether a task is settled.

Deliberately absent: cross-process queues, automatic retries after a conflict,
success caches, and a second task table.  ``TaskJournal`` records the ticket;
``TaskView`` is the read projection.
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ai_coding.core.gitcmd import git
from ai_coding.core.task_journal import TaskJournal
from ai_coding.core.worktree import WorktreeError, WorktreeManager
from ai_coding.core.write_coordinator import WriteCoordinator
from ai_coding.infra.subprocess_runner import run_process_async
from ai_coding.security.permission_gate import bash_denied
from ai_coding.security.process_sandbox import SandboxGap, spawn_argv

__all__ = [
    "MergeJournalError",
    "MergeQueue",
    "MergeQueueClosed",
    "MergeQueueFull",
    "MergeTicket",
]

_SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
_VERIFY_OUTPUT_CHARS = 4000


class MergeQueueFull(RuntimeError):
    """The bounded queue has no room for another ticket."""


class MergeJournalError(RuntimeError):
    """The queue could not record a queued/merging ticket."""


class MergeQueueClosed(RuntimeError):
    """The queue was closed (shutdown) and accepts no new tickets."""


@dataclass
class MergeTicket:
    """One explicit merge request; ``key`` is the source branch."""

    key: str
    source_task_id: str = ""
    source_run_id: str = ""
    source_name: str = ""
    branch: str = ""
    tip: str = ""
    base_commit: str | None = None
    verify_cmd: str | None = None
    session_id: str = ""
    status: str = "queued"
    phase: str = "queued"
    started: float = field(default_factory=time.monotonic)
    ended: float | None = None
    attempts: int = 1
    outcome: dict[str, Any] | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    on_update: Callable[[MergeTicket], None] | None = None

    def __post_init__(self) -> None:
        if not self.key:
            self.key = self.branch
        if not self.branch:
            self.branch = self.key


@dataclass
class _Failure(Exception):
    status: str
    error: str = ""
    conflicting_paths: tuple[str, ...] = ()
    worktree_path: str | None = None

    def outcome(self, *, attempts: int) -> dict[str, Any]:
        return {
            "status": self.status,
            "error": self.error or self.status,
            "conflicting_paths": list(self.conflicting_paths),
            "attempts": attempts,
            "worktree_path": self.worktree_path,
        }


class MergeQueue:
    """One worker, one FIFO, one main-tree writer."""

    def __init__(
        self,
        repo_root: str | Path,
        *,
        worktree_manager: WorktreeManager,
        journal: TaskJournal,
        write_coordinator: WriteCoordinator,
        policy: Any,
        backend: Any,
        capacity: int = 32,
        verify_timeout: float = 120.0,
        claim_timeout: float = 30.0,
        max_attempts: int = 2,
    ) -> None:
        self._root = Path(repo_root).expanduser().resolve()
        self._worktrees = worktree_manager
        self._journal = journal
        self._writes = write_coordinator
        self._policy = policy
        self._backend = backend
        self._capacity = max(1, int(capacity))
        self._verify_timeout = max(0.01, float(verify_timeout))
        self._claim_timeout = max(0.0, float(claim_timeout))
        self._max_attempts = max(1, int(max_attempts))
        self._queue: asyncio.Queue[MergeTicket] = asyncio.Queue(
            maxsize=self._capacity
        )
        self._worker: asyncio.Task[None] | None = None
        self._current: MergeTicket | None = None
        self._closed = False

    # ---- public API -------------------------------------------------------

    def enqueue(self, ticket: MergeTicket) -> None:
        """Queue one ticket; the caller then awaits :meth:`wait`."""
        if self._closed:
            raise MergeQueueClosed("merge queue is closed")
        if self._queue.full():
            raise MergeQueueFull("merge queue is full")
        if not self._append(ticket, "merge_queued"):
            raise MergeJournalError("could not write merge_queued")
        try:
            self._queue.put_nowait(ticket)
        except asyncio.QueueFull as exc:  # a same-loop race; keep the journal honest
            self._append(ticket, "merge_failed", error="busy")
            raise MergeQueueFull("merge queue is full") from exc
        ticket.status = "queued"
        self._notify(ticket)
        self._ensure_worker()

    async def wait(self, ticket: MergeTicket) -> dict[str, Any]:
        """Wait for one ticket; cancellation leaves the ticket running."""
        await ticket.done.wait()
        return ticket.outcome or {"status": ticket.status}

    async def aclose(self) -> None:
        """Stop taking work, settle queued tickets, and close the worker.

        A ticket already in its landing phase is allowed to finish: the git
        command may be past the point of no return, and releasing the write
        claim while it still runs would break the single-writer guarantee.
        """
        if self._closed:
            return
        self._closed = True
        worker = self._worker
        if worker is not None:
            current = self._current
            if current is not None and current.phase == "landing":
                await asyncio.gather(worker, return_exceptions=True)
            else:
                if current is not None:
                    current.cancel_event.set()
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            self._worker = None
        while True:
            try:
                ticket = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._finish(ticket, "cancelled", {"status": "cancelled",
                                               "error": "cancelled"})

    # ---- worker -----------------------------------------------------------

    def _ensure_worker(self) -> None:
        if self._closed or (self._worker is not None and not self._worker.done()):
            return
        self._worker = asyncio.create_task(self._worker_loop(), name="merge-queue")

    async def _worker_loop(self) -> None:
        while not self._closed:
            ticket = await self._queue.get()
            self._current = ticket
            try:
                await self._run_ticket(ticket)
            except asyncio.CancelledError:
                self._finish(ticket, "cancelled", {"status": "cancelled",
                                                   "error": "cancelled"})
                raise
            except Exception as exc:  # noqa: BLE001 — one ticket must not kill the worker
                self._finish(ticket, "merge_failed", {
                    "status": "merge_failed", "error": f"worker: {exc}",
                })
            finally:
                self._current = None

    async def _run_ticket(self, ticket: MergeTicket) -> None:
        ticket.status = "merging"
        ticket.phase = "merging"
        self._notify(ticket)
        if not self._append(ticket, "merging"):
            self._finish(ticket, "merge_failed", {
                "status": "merge_failed", "error": "journal_failed",
            })
            return

        attempts = 0
        while attempts < self._max_attempts:
            attempts += 1
            ticket.attempts = attempts
            result = await self._attempt(ticket)
            if result.get("status") != "retry":
                self._finish(ticket, str(result.get("status")), result)
                return
        self._finish(ticket, "head_moved", {
            "status": "head_moved",
            "error": "head_moved",
            "attempts": attempts,
        })

    # ---- one attempt ------------------------------------------------------

    async def _attempt(self, ticket: MergeTicket) -> dict[str, Any]:
        holder: dict[str, Any] = {"path": None}
        try:
            result = await self._attempt_inner(ticket, holder)
        except BaseException:
            await self._cleanup(holder)
            raise
        cleanup_error = await self._cleanup(holder)
        if cleanup_error:
            if result.get("status") == "merged":
                result["cleanup_error"] = cleanup_error
            else:
                result.update(
                    status="unrecoverable",
                    error=f"cleanup_failed: {cleanup_error}",
                    worktree_path=holder.get("path"),
                )
        return result

    async def _attempt_inner(
        self, ticket: MergeTicket, holder: dict[str, Any]
    ) -> dict[str, Any]:
        claim = None
        temp_path: str | None = None
        try:
            try:
                head, tip, base = await self._resolve_refs(ticket)
            except _Failure as failure:
                return failure.outcome(attempts=ticket.attempts)

            if await self._is_ancestor(tip, head):
                return {"status": "merge_skipped", "error": "nothing_to_merge"}
            changed = await self._changed_paths(base, tip)
            if changed is None:
                return {"status": "merge_failed", "error": "diff_failed"}
            if not changed:
                has_diff = await self._has_diff(base, tip)
                if has_diff is None:
                    return {"status": "merge_failed", "error": "diff_failed"}
                if not has_diff:
                    return {"status": "merge_skipped", "error": "nothing_to_merge"}
            dirty = await self._dirty_paths(changed)
            if dirty:
                return {"status": "dirty", "error": "dirty",
                        "conflicting_paths": list(dirty)}

            claim = await self._writes.claim(
                f"merge:{ticket.branch}", self._root, (), self._claim_timeout
            )
            if claim is None:
                return {"status": "busy", "error": "busy"}

            head0 = await self._rev_parse("HEAD")
            base0 = await self._merge_base(head0, tip)
            if base0 is None:
                return {"status": "invalid_ref", "error": "merge_base_failed"}
            if head0 != head or base0 != base or changed:
                if await self._is_ancestor(tip, head0):
                    return {"status": "merge_skipped",
                            "error": "nothing_to_merge"}
                changed = await self._changed_paths(base0, tip)
                if changed is None:
                    return {"status": "merge_failed", "error": "diff_failed"}
                if not changed:
                    has_diff = await self._has_diff(base0, tip)
                    if has_diff is None:
                        return {"status": "merge_failed", "error": "diff_failed"}
                    if not has_diff:
                        return {"status": "merge_skipped",
                                "error": "nothing_to_merge"}
                dirty = await self._dirty_paths(changed)
                if dirty:
                    return {"status": "dirty", "error": "dirty",
                            "conflicting_paths": list(dirty)}

            conflict = await self._merge_tree_conflicts(head0, tip)
            if conflict:
                return {"status": "conflict", "error": "conflict",
                        "conflicting_paths": list(conflict)}

            try:
                temp = await self._worktrees.checkout(
                    f"merge_{ticket.key}", head0
                )
            except WorktreeError as exc:
                return {"status": "unrecoverable", "error": str(exc)}
            temp_path = str(temp.path)
            holder["path"] = temp_path
            candidate, conflict_paths = await self._merge_candidate(str(temp.path), tip)
            if candidate is None:
                if conflict_paths:
                    return {"status": "conflict", "error": "conflict",
                            "conflicting_paths": list(conflict_paths)}
                return {"status": "merge_failed", "error": "candidate_failed"}
        finally:
            if claim is not None:
                claim.release()
                claim = None

        # Verify without holding the main-tree write lock.
        ticket.phase = "verify"
        verified, verify = await self._verify(ticket, temp_path)
        if verify.get("status") not in (None, "ok"):
            return {
                "status": verify["status"],
                "error": verify["status"],
                "verify": verify.get("verify"),
                "worktree_path": temp_path,
            }

        ticket.phase = "landing"
        claim = await self._writes.claim(
            f"merge:{ticket.branch}", self._root, (), self._claim_timeout
        )
        if claim is None:
            return {"status": "busy", "error": "busy"}
        try:
            try:
                now_tip = await self._rev_parse(ticket.branch)
                now_head = await self._rev_parse("HEAD")
            except _Failure as failure:
                return failure.outcome(attempts=ticket.attempts)
            if now_tip != tip:
                return {"status": "branch_moved", "error": "branch_moved",
                        "worktree_path": temp_path}
            if now_head != head0:
                return {"status": "retry", "error": "head_moved"}
            changed_now = await self._changed_paths(base0, tip)
            if changed_now is None:
                return {"status": "merge_failed", "error": "diff_failed"}
            dirty_now = await self._dirty_paths(changed_now)
            if dirty_now:
                return {"status": "dirty", "error": "dirty",
                        "conflicting_paths": list(dirty_now)}
            outcome = await self._ff_only(candidate)
            if outcome is not None:
                return outcome
            main_head = await self._rev_parse("HEAD")
            return {
                "status": "merged",
                "branch": ticket.branch,
                "base_commit": ticket.base_commit,
                "commit": candidate,
                "main_head": main_head,
                "verified": verified,
                "verify": verify.get("verify"),
                "attempts": ticket.attempts,
                "worktree_path": temp_path,
            }
        finally:
            if claim is not None:
                claim.release()
                claim = None

    async def _cleanup(self, holder: dict[str, Any]) -> str | None:
        path = holder.get("path")
        if not path:
            return None
        holder["path"] = None
        try:
            await self._worktrees.remove(str(path))
        except WorktreeError as exc:
            return str(exc)
        return None

    # ---- git helpers ------------------------------------------------------

    async def _git(self, *args: str, merge_stderr: bool = True) -> tuple[int, str]:
        try:
            return await git(self._root, *args, timeout=60.0,
                             merge_stderr=merge_stderr)
        except (OSError, TimeoutError) as exc:
            return 1, str(exc)

    async def _rev_parse(self, ref: str) -> str:
        code, text = await self._git("rev-parse", "--verify", "--end-of-options",
                                     f"{ref}^{{commit}}", merge_stderr=False)
        value = text.strip()
        if code != 0 or not _SHA_RE.match(value):
            raise _Failure("invalid_ref", f"rev-parse {ref}: {value or 'failed'}")
        return value

    async def _merge_base(self, left: str, right: str) -> str | None:
        code, text = await self._git("merge-base", left, right, merge_stderr=False)
        value = text.strip()
        return value if code == 0 and _SHA_RE.match(value) else None

    async def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        code, _ = await self._git("merge-base", "--is-ancestor", ancestor,
                                  descendant, merge_stderr=False)
        return code == 0

    async def _changed_paths(self, base: str, tip: str) -> list[str] | None:
        code, text = await self._git(
            "diff", "--name-only", "-z", f"{base}..{tip}", "--",
            merge_stderr=False,
        )
        if code != 0:
            return None
        return [part for part in text.split("\0") if part]

    async def _has_diff(self, base: str, tip: str) -> bool | None:
        code, _ = await self._git(
            "diff", "--quiet", base, tip, "--", merge_stderr=False
        )
        if code == 0:
            return False
        if code == 1:
            return True
        return None

    async def _dirty_paths(self, changed: Iterable[str]) -> set[str]:
        wanted = {_norm(path) for path in changed if path}
        dirty: set[str] = set()
        code, _ = await self._git("diff-index", "--quiet", "HEAD", "--",
                                  merge_stderr=False)
        if code not in (0, 1):
            return {"<git-status-failed>"}
        if code == 1:
            dirty.add("<tracked>")
        code, text = await self._git(
            "status", "--porcelain", "-z", "--untracked-files=all",
            "--no-renames", merge_stderr=False,
        )
        if code != 0:
            return {"<git-status-failed>"}
        untracked = {_norm(_status_path(part)) for part in text.split("\0") if part}
        code, ignored_text = await self._git(
            "ls-files", "-o", "-i", "--exclude-standard", "-z",
            merge_stderr=False,
        )
        if code == 0:
            untracked |= {_norm(item) for item in ignored_text.split("\0") if item}
        code, assumed = await self._git("ls-files", "-v", "-z", merge_stderr=False)
        if code == 0:
            for part in assumed.split("\0"):
                if part[:1] in ("h", "S") and len(part) > 2:
                    untracked.add(_norm(part[2:]))
        for path in untracked:
            if _collides(path, wanted):
                dirty.add(path)
        return dirty

    async def _resolve_refs(self, ticket: MergeTicket) -> tuple[str, str, str]:
        branch = ticket.branch
        code, _ = await self._git("check-ref-format", "--branch", branch,
                                  merge_stderr=False)
        if code != 0:
            raise _Failure("invalid_ref", "bad branch name")
        tip = await self._rev_parse(branch)
        head = await self._rev_parse("HEAD")
        base = await self._merge_base(head, tip)
        if base is None:
            raise _Failure("invalid_ref", "merge-base failed")
        ticket.tip = tip
        return head, tip, base

    async def _merge_tree_conflicts(self, head: str, tip: str) -> list[str] | None:
        code, text = await self._git(
            "merge-tree", "--write-tree", "--name-only", "-z", "--no-messages",
            head, tip, merge_stderr=False,
        )
        if code == 0:
            return []
        if code != 1:
            return []  # old git/unknown failure: let the temp worktree decide
        fields = [part for part in text.split("\0") if part]
        return fields[1:] if fields else []

    async def _merge_candidate(
        self, workdir: str, tip: str
    ) -> tuple[str | None, list[str]]:
        code, _ = await git(
            workdir,
            "-c", "core.hooksPath=/dev/null",
            "-c", "commit.gpgsign=false",
            "-c", "user.name=ai-coding",
            "-c", "user.email=merge@local",
            "merge", "--no-ff", "--no-verify", tip,
            timeout=60.0,
        )
        if code != 0:
            _, text = await git(
                workdir, "diff", "--name-only", "--diff-filter=U", "-z",
                timeout=60.0, merge_stderr=False,
            )
            return None, [part for part in text.split("\0") if part]
        code, text = await git(
            workdir, "rev-parse", "HEAD", timeout=60.0, merge_stderr=False
        )
        value = text.strip()
        return (value if code == 0 and _SHA_RE.match(value) else None), []

    async def _ff_only(self, candidate: str) -> dict[str, Any] | None:
        code, text = await self._git("merge", "--ff-only", candidate)
        if code == 0:
            return None
        return {"status": "merge_failed", "error": f"ff_only_failed: {text.strip()}"}

    # ---- verify -----------------------------------------------------------

    async def _verify(
        self, ticket: MergeTicket, workdir: str | None
    ) -> tuple[bool, dict[str, Any]]:
        command = (ticket.verify_cmd or "").strip()
        if not command:
            return False, {"status": "ok", "verify": None}
        if workdir is None:
            return False, {"status": "unrecoverable",
                           "verify": {"exit_code": None, "output": ""}}
        reason = bash_denied(command)
        if reason:
            return False, {"status": "denied", "verify": {
                "exit_code": None, "output": reason,
            }}
        if (
            self._policy is None
            or getattr(self._policy, "mode", "off") != "enforce"
            or bool(getattr(self._policy, "bypass", False))
            or self._backend is None
            or not self._backend.available()
        ):
            return False, {"status": "verify_unavailable",
                           "verify": {"exit_code": None, "output": ""}}
        try:
            argv = spawn_argv(
                command, self._policy, self._backend,
                roots=[Path(workdir)],
            )
        except SandboxGap as exc:
            return False, {"status": "verify_unavailable",
                           "verify": {"exit_code": None, "output": exc.reason}}
        env = dict(os.environ)
        env["GIT_OPTIONAL_LOCKS"] = "0"
        outcome = await run_process_async(
            argv,
            shell=False,
            cwd=workdir,
            env=env,
            timeout=self._verify_timeout,
            max_output_chars=_VERIFY_OUTPUT_CHARS,
            cancel=ticket.cancel_event,
            pass_fds=getattr(self._backend, "seccomp_fds", ()),
        )
        output = (outcome.stdout or "") + (outcome.stderr or "")
        verify = {
            "exit_code": outcome.returncode,
            "output": output[-_VERIFY_OUTPUT_CHARS:],
            "timed_out": outcome.timed_out,
        }
        if outcome.error is not None or outcome.timed_out or outcome.cancelled:
            return False, {"status": "verify_failed", "verify": verify}
        if outcome.returncode != 0:
            return False, {"status": "verify_failed", "verify": verify}
        return True, {"status": "ok", "verify": verify}

    # ---- journal / notification ------------------------------------------

    def _append(self, ticket: MergeTicket, event: str, **extra: Any) -> bool:
        return self._journal.append(
            ticket.session_id,
            event,
            task_id=f"merge:{ticket.branch}",
            parent_id=ticket.source_task_id,
            kind="merge",
            branch=ticket.branch,
            tip=ticket.tip,
            base_commit=ticket.base_commit,
            name=f"merge:{ticket.branch}",
            run_id=ticket.source_run_id,
            key=ticket.key,
            **extra,
        )

    def _finish(
        self, ticket: MergeTicket, status: str, outcome: dict[str, Any]
    ) -> None:
        if ticket.done.is_set():
            return
        ticket.status = status
        ticket.outcome = dict(outcome)
        ticket.ended = time.monotonic()
        event = (
            "merged" if status == "merged"
            else "merge_skipped" if status == "merge_skipped"
            else "cancelled" if status == "cancelled"
            else "merge_failed"
        )
        if not self._append(ticket, event, error=outcome.get("error")):
            ticket.outcome["journal_error"] = True
        self._notify(ticket)
        ticket.done.set()

    def _notify(self, ticket: MergeTicket) -> None:
        if ticket.on_update is None:
            return
        try:
            ticket.on_update(ticket)
        except Exception:  # noqa: BLE001 — a view update cannot kill the worker
            pass


def _norm(path: str) -> str:
    return os.path.normcase(path.strip().lstrip("./"))


def _status_path(part: str) -> str:
    # ``git status --porcelain -z`` emits ``XY<space>path``; a rename emits two
    # NUL-separated paths (disabled here with --no-renames).
    return part[3:] if len(part) > 3 else part


def _collides(path: str, changed: set[str]) -> bool:
    if path in changed:
        return True
    for item in changed:
        if path.startswith(item + "/") or item.startswith(path + "/"):
            return True
    return False
