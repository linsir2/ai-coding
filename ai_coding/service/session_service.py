"""Session persistence against a sessions root directory (atomic writes).

Persistence shape reuses ``SessionData.to_dict/from_dict`` so files stay byte-compatible
with the Java ``sessions/*.json`` format.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from ai_coding.domain.message import CHECKPOINT_MARK
from ai_coding.domain.session import SessionData
from ai_coding.infra.json_utils import from_json_file, to_json_pretty
from ai_coding.infra.time_utils import now_utc


def _access_rank(value: Any) -> tuple[int, str]:
    """Sort key for the resume list: newest ``last_access_time`` first.

    ISO-8601 ``Z`` strings sort chronologically as plain strings, so parsing
    is only a validity gate — a missing or garbled timestamp sinks to the
    bottom instead of raising, because one bad file must not hide the rest.
    """
    if isinstance(value, str) and value:
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
        else:
            return (0, value)
    return (1, "")


#: A preview is one line by definition; measured on the compaction checkpoint,
#: an unbounded preview rendered the whole summary single-lined in the header.
_PREVIEW_CHARS = 80


def _first_user_preview(rows: Any) -> str:
    """A normalized excerpt of the first *user-written* message, for the picker.

    The list view drops message bodies, so this has to be taken while the file is
    still parsed.  Whitespace is collapsed and the excerpt is capped because the
    picker renders it on one line; an assistant-first session is skipped rather
    than shown, because an empty body is exactly what made the old list
    unreadable.  A compaction checkpoint note is stored as a user-role message
    but was not written by the user, so it is skipped too (measured: after
    ``/compact`` the header showed the whole summary).
    """
    if not isinstance(rows, list):
        return ""
    for row in rows:
        if not isinstance(row, dict) or row.get("role") != "user":
            continue
        text = row.get("content")
        if isinstance(text, str) and text.strip():
            collapsed = " ".join(text.split())
            if collapsed.startswith(CHECKPOINT_MARK):
                continue
            return collapsed[:_PREVIEW_CHARS]
    return ""


class SessionService:
    """Create, save, load, list, and delete sessions under a root directory."""

    def __init__(self, root: str | Path = Path("sessions")) -> None:
        self.root = Path(root)

    def _path(self, session_id: str) -> Path:
        return self.root / f"{session_id}.json"

    def create(self, title: str = "") -> SessionData:
        ts = now_utc()
        return SessionData(
            session_id=uuid.uuid4().hex,
            title=title,
            created_time=ts,
            last_access_time=ts,
        )

    def save(self, session: SessionData) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self._path(session.session_id)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(to_json_pretty(session.to_dict()), encoding="utf-8")
        os.replace(tmp, target)  # atomic on POSIX

    def load(self, session_id: str) -> SessionData | None:
        target = self._path(session_id)
        if target.is_file():
            session = SessionData.from_dict(from_json_file(target))
            # P2 (补充式方案 06): a file that says a turn is open is the crash
            # signal.  Derived on load, not stored: it describes this read, and
            # the next turn clears ``open_turn`` (and with it this notice).
            session.recovered_interrupted = session.open_turn is not None
            return session
        # Not an exact id: fall back to a prefix, but only when it is unique.
        # Resuming the first of several matches would silently open the wrong
        # conversation, so an ambiguous prefix is a miss.
        matches = self.resolve(session_id)
        return matches[0] if len(matches) == 1 else None

    def resolve(self, prefix: str) -> list[SessionData]:
        """Every saved session whose id starts with ``prefix``.

        Returns the matches rather than one of them so a caller can refuse an
        ambiguous prefix and show the candidates.  A full 32-char id is an exact
        hit, since session ids are unique.
        """
        if self._path(prefix).is_file():
            exact = self.load(prefix)
            return [exact] if exact is not None else []
        return [s for s in self.list_sessions() if s.session_id.startswith(prefix)]

    def list_sessions(self) -> list[SessionData]:
        if not self.root.is_dir():
            return []
        items: list[SessionData] = []
        for p in sorted(self.root.glob("*.json")):
            try:
                data = from_json_file(p)
            except (ValueError, OSError):
                continue
            rows = data.get("messages")
            count = len(rows) if isinstance(rows, list) else 0
            summary: dict[str, Any] = dict(data)
            summary["messages"] = []  # list view drops message bodies
            item = SessionData.from_dict(summary)
            # The bodies are gone but the number is still known: the file was
            # just parsed in full, so anything that reads ``.messages`` from a
            # listed session would be reporting the zero written two lines up.
            item.message_count = count
            item.first_preview = _first_user_preview(rows)
            items.append(item)
        # The resume list leads with the conversation just left: newest
        # access first.  Valid stamps sort as strings (ISO-8601 Z, newest
        # last); broken ones keep filename order at the bottom.
        valid = [s for s in items if _access_rank(s.last_access_time)[0] == 0]
        broken = [s for s in items if _access_rank(s.last_access_time)[0] != 0]
        valid.sort(key=lambda s: s.last_access_time, reverse=True)
        broken.sort(key=lambda s: s.session_id)
        return valid + broken

    def delete(self, session_id: str) -> bool:
        target = self._path(session_id)
        if not target.is_file():
            return False
        target.unlink()
        return True


__all__ = ["SessionService"]
