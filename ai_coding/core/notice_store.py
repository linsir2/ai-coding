"""P2-B3: the on-disk settlement-note lane.

A background child settles long after the turn that started it may have ended,
and the in-memory ``SteeringQueue`` dies with the process.  This store keeps the
same line on disk until a loop has actually written it into the conversation:

* ``append`` is one JSON line per notice, opened ``O_APPEND`` so two processes
  cannot overwrite each other's tail;
* ``pending`` reads every undelivered line for one session;
* ``deliver`` removes only the ids the loop delivered, via a read-filter-replace,
  so an undelivered line always survives.

The semantics are deliberately **at-least-once**: a crash between writing the
message and deleting the row repeats one notice, it never loses one.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from ai_coding.infra.time_utils import now_utc

__all__ = ["NOTICE_MAX_CHARS", "NoticeStore", "clip_notice"]

#: Same budget ``SteeringQueue`` defaults to: disk and memory must agree on the
#: text byte-for-byte, or deduplication would produce two messages.
NOTICE_MAX_CHARS = 2_000
_TRUNCATION = "…"


def clip_notice(text: str, max_chars: int = NOTICE_MAX_CHARS) -> str:
    """Strip and cut one notice to the queue's budget, marking the cut.

    ``strip`` matches ``SteeringQueue.put``: the memory lane stores stripped
    text, so a disk row that kept leading/trailing whitespace would never
    deduplicate against the same line arriving through the queue.
    """
    body = str(text or "").strip()
    if max_chars <= 0 or len(body) <= max_chars:
        return body
    return body[:max_chars] + _TRUNCATION


class NoticeStore:
    """``<workspace>/.ai-coding/notices/<session_id>.jsonl``."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)

    @classmethod
    def for_workspace(cls, workspace: str | os.PathLike[str]) -> NoticeStore:
        return cls(Path(workspace) / ".ai-coding" / "notices")

    def append(self, session_id: str, text: str) -> str | None:
        """Persist one notice; return its id (``None`` for an unusable session)."""
        if not _usable_session(session_id):
            return None
        record = {
            "id": uuid.uuid4().hex,
            "session": session_id,
            "text": clip_notice(text),
            "created_at": now_utc(),
        }
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with self._path(session_id).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            return None
        return str(record["id"])

    def pending(self, session_id: str) -> list[dict[str, Any]]:
        """Every unread notice for ``session_id``, oldest first."""
        if not _usable_session(session_id):
            return []
        try:
            lines = self._path(session_id).read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        rows: list[dict[str, Any]] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(row, dict) and row.get("id") and row.get("text"):
                rows.append(row)
        return rows

    def deliver(self, session_id: str, ids: list[str] | tuple[str, ...]) -> None:
        """Delete exactly ``ids``; everything else stays on disk."""
        wanted = {str(item) for item in ids if item}
        if not wanted or not _usable_session(session_id):
            return
        rows = self.pending(session_id)
        kept = [row for row in rows if str(row.get("id")) not in wanted]
        if len(kept) == len(rows):
            return
        path = self._path(session_id)
        tmp = path.with_suffix(".tmp")
        try:
            if kept:
                tmp.write_text(
                    "".join(
                        json.dumps(row, ensure_ascii=False) + "\n" for row in kept
                    ),
                    encoding="utf-8",
                )
            else:
                tmp.write_text("", encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            # Best-effort: the row stays and is redelivered, never lost.
            return

    def _path(self, session_id: str) -> Path:
        return self.root / f"{session_id}.jsonl"


def _usable_session(session_id: str) -> bool:
    """A session id names a file: refuse path separators and traversal."""
    value = str(session_id or "")
    if not value or value in {".", ".."}:
        return False
    return not any(char in value for char in ("/", "\\", os.sep))
