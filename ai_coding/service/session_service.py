"""Session persistence against a sessions root directory (atomic writes).

Persistence shape reuses ``SessionData.to_dict/from_dict`` so files stay byte-compatible
with the Java ``sessions/*.json`` format.
"""

from __future__ import annotations

import os
import shutil
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


def _check_id(session_id: str) -> None:
    """Refuse ids that could escape the sessions directory (L3).

    Session ids are minted hex (or short test words); nothing legitimate ever
    carries a separator.  Loud, because a traversal attempt must never look
    like an ordinary miss.  Empty strings pass through untouched to preserve
    the current ``delete("") is False`` / ``load("") is None`` behaviour.
    """
    if any(part in session_id for part in ("/", "\\", "\x00")) or ".." in session_id:
        raise ValueError(f"invalid session id: {session_id!r}")


#: Subdirectory holding the pending-approval markers (P3).  Separate so the
#: ``*.json`` session scan cannot read one as a session.
_PENDING_DIR = "pending"

#: Subdirectory holding a posture flip queued while a turn ran (M1b T10).
#: Same reason as ``_PENDING_DIR``: the session scan globs the root, so a
#: subdirectory keeps a marker from being read as a phantom session.
_PENDING_POSTURE_DIR = "pending-posture"

#: Subdirectory holding a reminder waiting to be delivered (M2 T3).  Same
#: reason as ``_PENDING_POSTURE_DIR``: the session scan globs the root, so a
#: subdirectory keeps a marker from being read as a phantom session.
_PENDING_REMINDER_DIR = "pending-reminder"

#: Sibling directory holding the undo file-bytes sidecars (P4).  Mirrors the
#: root ``AgentLoop`` builds its ``UndoFileStore`` on
#: (``<workspace>/.ai-coding/undo``); named here so ``delete`` can take a
#: session's sidecar with it instead of orphaning redo bytes.
_UNDO_DIR = "undo"


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

    def __init__(
        self,
        root: str | Path = Path("sessions"),
        *,
        default_agent: str = "build",
        default_readonly: bool = False,
    ) -> None:
        """``root`` is where the session files live.

        ``default_agent`` / ``default_readonly`` are what a **new** session
        starts in (M1b).  They live here rather than in the command layer so
        that every way of making a session — ``/clear``, the IPC
        ``session.create``, a boot resume — inherits the same starting point.
        """
        self.root = Path(root)
        self._default_agent = str(default_agent or "build")
        self._default_readonly = bool(default_readonly)

    def _path(self, session_id: str) -> Path:
        _check_id(session_id)
        return self.root / f"{session_id}.json"

    # ---- pending-approval marker (P3) --------------------------------------
    #
    # Why a sidecar and not a session field: the approval wait sits inside the
    # tool batch, ahead of any checkpoint, so writing it to the session means
    # saving mid-batch -- and ``TurnCloser.save`` normalizes tool pairing,
    # which drops an assistant carrier whose result has not been written back
    # yet.  That destroys the very call the marker exists to preserve
    # (verified).  A small file keeps the session file untouched, so the save
    # cadence every other test pins does not change.

    def _pending_path(self, session_id: str) -> Path:
        _check_id(session_id)
        # Its own directory, not ``<id>.pending.json`` beside the sessions: the
        # session scan globs ``*.json``, so a sibling marker was listed by
        # ``list_sessions`` as a phantom empty session (caught by real-chain
        # verification, not by a unit test).
        return self.root / _PENDING_DIR / f"{session_id}.json"

    def mark_pending_approval(self, session_id: str, request: dict[str, Any] | None) -> None:
        """Record (or clear) the approval one session is waiting on.

        Best-effort like ``save``: a failure to record must not end the turn,
        and a failure to clear only costs a spurious re-ask after a crash.
        """
        path = self._pending_path(session_id)
        try:
            if request is None:
                path.unlink(missing_ok=True)
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(to_json_pretty(dict(request)), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass  # the turn's own answer still stands; only durability is lost

    def _read_pending_approval(self, session_id: str) -> dict[str, Any] | None:
        path = self._pending_path(session_id)
        if not path.is_file():
            return None
        try:
            data = from_json_file(path)
        except (ValueError, OSError):
            return None
        return data if isinstance(data, dict) else None

    # ---- pending-posture marker (M1b T10) -----------------------------------
    #
    # Why a sidecar and not a session field: a posture flip is queued while a
    # turn is *running*, and writing the session then saves mid-batch, which
    # normalizes tool pairing and drops an assistant carrier whose result has
    # not been written back yet (the same trap the approval marker documents).
    # The sidecar leaves the session file — and therefore the save cadence —
    # untouched, while still surviving a crash.

    def _pending_posture_path(self, session_id: str) -> Path:
        _check_id(session_id)
        return self.root / _PENDING_POSTURE_DIR / f"{session_id}.json"

    def mark_pending_posture(
        self, session_id: str, payload: dict[str, Any] | None
    ) -> None:
        """Record (or clear) the posture flip one session has queued.

        Best-effort like ``save``: a failure to record must not end the turn
        (the in-memory pending still applies in this process), and a failure
        to clear only costs a stale flip after a crash.
        """
        path = self._pending_posture_path(session_id)
        try:
            if payload is None:
                path.unlink(missing_ok=True)
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(to_json_pretty(dict(payload)), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass

    def _read_pending_posture(self, session_id: str) -> dict[str, Any] | None:
        path = self._pending_posture_path(session_id)
        if not path.is_file():
            return None
        try:
            data = from_json_file(path)
        except (ValueError, OSError):
            return None
        return data if isinstance(data, dict) else None

    # ---- pending-reminder marker (M2 T3) -----------------------------------
    #
    # The same shape as the posture marker, for the same reason: the
    # reminder is queued while a turn runs, and writing the session then
    # would normalize the tool pairing and drop an in-flight carrier.

    def _pending_reminder_path(self, session_id: str) -> Path:
        _check_id(session_id)
        return self.root / _PENDING_REMINDER_DIR / f"{session_id}.json"

    def mark_pending_reminder(
        self, session_id: str, payload: list[dict[str, Any]] | None
    ) -> None:
        """Record (or clear) the reminders one session has queued."""
        path = self._pending_reminder_path(session_id)
        try:
            if payload is None:
                path.unlink(missing_ok=True)
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(to_json_pretty(list(payload)), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass

    def _read_pending_reminder(self, session_id: str) -> list[dict[str, Any]] | None:
        path = self._pending_reminder_path(session_id)
        if not path.is_file():
            return None
        try:
            data = from_json_file(path)
        except (ValueError, OSError):
            return None
        if not isinstance(data, list):
            return None
        return [row for row in data if isinstance(row, dict)]

    def create(self, title: str = "") -> SessionData:
        ts = now_utc()
        return SessionData(
            session_id=uuid.uuid4().hex,
            title=title,
            created_time=ts,
            last_access_time=ts,
            agent=self._default_agent,
            readonly=self._default_readonly,
        )

    def save(self, session: SessionData) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        # R07: a save is an access.  Only ``create`` used to stamp this field,
        # so boot's "newest armed" scan and the /resume list sorted by
        # creation time -- the session someone just used could lose to a
        # staler one.
        session.last_access_time = now_utc()
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
            # P3: the approval the session was waiting on, if the marker is
            # still there -- i.e. the process died mid-wait.
            session.pending_approval = self._read_pending_approval(session_id)
            # M1b T10: a posture flip queued while the turn ran, if the
            # process died before the boundary that would have applied it.
            session.pending_posture = self._read_pending_posture(session_id)
            # M2 T3: the same for a reminder that was queued but not yet
            # delivered.
            session.pending_reminders = self._read_pending_reminder(session_id)
            return session
        # Not an exact id: fall back to a prefix, but only when it is unique.
        # Resuming the first of several matches would silently open the wrong
        # conversation, so an ambiguous prefix is a miss.
        matches = self.resolve(session_id)
        if len(matches) != 1:
            return None
        # L1: ``resolve`` rows are the message-gutted list view; a file whose
        # name and sessionId field disagree would resolve to itself forever.
        # Reloading by a *different* id terminates (exact hit or clean miss);
        # a self-match is an honest miss, not a session.
        matched = matches[0].session_id
        return self.load(matched) if matched != session_id else None

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
        # M1b: drop the posture marker even when the session file is gone --
        # a session that crashed before its first save leaves only the marker,
        # and an orphan would keep reporting a flip for a session nobody has.
        self._pending_posture_path(session_id).unlink(missing_ok=True)
        self._pending_reminder_path(session_id).unlink(missing_ok=True)
        if not target.is_file():
            return False
        target.unlink()
        # P3: an orphan marker would keep reporting a pending approval for a
        # session that no longer exists.
        self._pending_path(session_id).unlink(missing_ok=True)
        # P4: same orphan class for the undo file-bytes sidecar — redo bytes
        # for a deleted session are unreachable, so drop the directory.
        shutil.rmtree(self.root.parent / _UNDO_DIR / session_id, ignore_errors=True)
        return True


__all__ = ["SessionService"]
