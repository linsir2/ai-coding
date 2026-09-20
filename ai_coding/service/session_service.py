"""Session persistence against a sessions root directory (atomic writes).

Persistence shape reuses ``SessionData.to_dict/from_dict`` so files stay byte-compatible
with the Java ``sessions/*.json`` format.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

from ai_coding.domain.session import SessionData
from ai_coding.infra.json_utils import from_json_file, to_json_pretty
from ai_coding.infra.time_utils import now_utc


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
        if not target.is_file():
            return None
        return SessionData.from_dict(from_json_file(target))

    def list_sessions(self) -> list[SessionData]:
        if not self.root.is_dir():
            return []
        items: list[SessionData] = []
        for p in sorted(self.root.glob("*.json")):
            try:
                data = from_json_file(p)
            except (ValueError, OSError):
                continue
            summary: dict[str, Any] = dict(data)
            summary["messages"] = []  # list view drops message bodies
            items.append(SessionData.from_dict(summary))
        return items

    def delete(self, session_id: str) -> bool:
        target = self._path(session_id)
        if not target.is_file():
            return False
        target.unlink()
        return True


__all__ = ["SessionService"]
