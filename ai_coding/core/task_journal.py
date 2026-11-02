"""Task-level journal (P2-C2).

One append-only JSONL per spawning session under
``<workspace>/.ai-coding/tasks/``.  It records *task* facts only -- file writes
still belong to ``write_ahead``.  Appends are synchronous and guarded by a
thread lock because the runner settles from synchronous callbacks (its
``_finish_background``/``_reap`` are not coroutines).

A crash can leave a half-written last line.  Append repairs that by writing a
newline first when the file does not end with one; readers skip unparsable
lines.  Recovery folds the events per task: the last event wins, except that a
``pending`` after ``settled`` re-opens the task (mailbox follow-up not yet run).
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["TaskJournal", "TaskRecordState", "TERMINAL_EVENTS"]

#: Events that close a task for recovery purposes.
TERMINAL_EVENTS = frozenset({"settled", "cancelled", "stopped", "closed"})


@dataclass
class TaskRecordState:
    """The folded view of one task id."""

    task_id: str
    parent_id: str = ""
    kind: str = "child"
    state: str = "queued"
    turn: int = 0
    session_id: str = ""
    run_id: str = ""
    branch: str | None = None
    commit: str | None = None
    error: str | None = None
    key: str = ""
    digest: str = ""
    last_event: str = "queued"
    events: list[str] = field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.last_event in TERMINAL_EVENTS


class TaskJournal:
    """Append-only task journal (one directory for the whole workspace)."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root) / ".ai-coding" / "tasks"
        self._lock = threading.Lock()

    # ---- writes -----------------------------------------------------------

    def append(self, session_id: str, event: str, **fields: Any) -> None:
        """Append one event; best-effort (a journal must not break a turn)."""
        row = {"v": 1, "event": event, "session_id": session_id, **fields}
        target = self._path(session_id)
        with self._lock:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                needs_newline = False
                if target.exists() and target.stat().st_size > 0:
                    with target.open("rb") as handle:
                        handle.seek(-1, os.SEEK_END)
                        needs_newline = handle.read(1) != b"\n"
                with target.open("a", encoding="utf-8") as handle:
                    if needs_newline:
                        handle.write("\n")
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            except OSError:
                return  # journal is observability, never a turn breaker

    # ---- reads ------------------------------------------------------------

    def records(self, session_id: str | None = None) -> list[TaskRecordState]:
        """Folded records for one session, or every session when ``None``."""
        paths = (
            [self._path(session_id)]
            if session_id is not None
            else sorted(self.root.glob("*.jsonl"))
        )
        out: list[TaskRecordState] = []
        for path in paths:
            out.extend(self._fold(self._read(path)))
        return out

    def pending(self, session_id: str | None = None) -> list[TaskRecordState]:
        """Records that are not closed (recovery marks them needs_reconciliation)."""
        return [record for record in self.records(session_id) if not record.is_terminal]

    # ---- internals --------------------------------------------------------

    def _path(self, session_id: str) -> Path:
        safe = session_id.strip() or "_orphan"
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in safe)
        return self.root / f"{safe}.jsonl"

    @staticmethod
    def _read(path: Path) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return rows
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue  # a half-written crash tail
            if isinstance(row, dict) and row.get("task_id"):
                rows.append(row)
        return rows

    @staticmethod
    def _fold(rows: Iterable[dict[str, Any]]) -> list[TaskRecordState]:
        records: dict[str, TaskRecordState] = {}
        for row in rows:
            task_id = str(row.get("task_id") or "")
            if not task_id:
                continue
            record = records.get(task_id)
            if record is None:
                record = TaskRecordState(task_id=task_id)
                records[task_id] = record
            event = str(row.get("event") or "")
            record.events.append(event)
            record.last_event = event
            record.state = event
            record.parent_id = str(row.get("parent_id") or record.parent_id)
            record.kind = str(row.get("kind") or record.kind)
            record.session_id = str(row.get("session_id") or record.session_id)
            record.run_id = str(row.get("run_id") or record.run_id)
            record.branch = row.get("branch") or record.branch
            record.commit = row.get("commit") or record.commit
            record.error = row.get("error") or record.error
            record.key = str(row.get("key") or record.key)
            record.digest = str(row.get("digest") or record.digest)
            if row.get("turn") is not None:
                record.turn = int(row["turn"])
        return list(records.values())
