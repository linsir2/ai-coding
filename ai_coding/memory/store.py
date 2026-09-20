"""Disk-backed memory index (M3).

Design (m3-analysis §4.4): a small JSON-file-backed index used to persist compact
memory entries across turns.  Writes are atomic (tmp + ``os.replace``) and every IO
failure is swallowed — memory must never break the agent loop (invariant §5-11).

Persistence shape (independent, reusable):
    {"entries": [{"id": str, "text": str, "created_time": str}]}
"""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from ai_coding.infra.json_utils import from_json_file, to_json_pretty
from ai_coding.infra.time_utils import now_utc


class MemoryEntry:
    """A single stored memory record."""

    __slots__ = ("id", "text", "created_time")

    def __init__(self, id: str, text: str, created_time: str) -> None:
        self.id = id
        self.text = text
        self.created_time = created_time

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "text": self.text, "created_time": self.created_time}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryEntry:
        return cls(
            id=str(data.get("id", "")),
            text=str(data.get("text", "")),
            created_time=str(data.get("created_time", "")),
        )


def _load(path: Path) -> list[MemoryEntry]:
    if not path.is_file():
        return []
    try:
        data = from_json_file(path)
    except (ValueError, OSError):
        return []
    raw = data.get("entries", []) if isinstance(data, dict) else []
    entries: list[MemoryEntry] = []
    for item in raw:
        if isinstance(item, dict):
            try:
                entries.append(MemoryEntry.from_dict(item))
            except (ValueError, TypeError):
                continue
    return entries


class MemoryStore:
    """A thread-safe, disk-backed memory index that never raises on IO errors."""

    def __init__(self, path: str | Path = Path(".memory")) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._entries = _load(self._path)

    # ---- persistence ------------------------------------------------------

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = to_json_pretty(
                {"entries": [e.to_dict() for e in self._entries]}
            )
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            pass  # memory persistence is best-effort; never raise

    # ---- mutations --------------------------------------------------------

    def _mutate(self, fn: Callable[[], None]) -> None:
        """Run a mutation and swallow any persistence error (memory never raises)."""
        with self._lock:
            fn()
            try:
                self._save()
            except Exception:
                pass

    def add(self, text: str) -> str:
        """Add one text entry; returns its id ('' when the text is blank)."""
        text = (text or "").strip()
        if not text:
            return ""
        entry = MemoryEntry(id=uuid.uuid4().hex, text=text, created_time=now_utc())
        self._mutate(lambda: self._entries.append(entry))
        return entry.id

    def delete(self, entry_id: str) -> bool:
        """Delete by id; returns True when removed."""
        removed = False

        def _do() -> None:
            nonlocal removed
            idx = [i for i, e in enumerate(self._entries) if e.id == entry_id]
            if idx:
                self._entries.pop(idx[0])
                removed = True

        self._mutate(_do)
        return removed

    def clear(self) -> None:
        self._mutate(lambda: self._entries.clear())

    # ---- reads ------------------------------------------------------------

    def all(self) -> list[MemoryEntry]:
        with self._lock:
            return list(self._entries)

    def search(self, keywords: Sequence[str], limit: int = 20) -> list[MemoryEntry]:
        """Return entries whose text matches at least one keyword (substring)."""
        if not keywords:
            return []
        keys = [k.casefold() for k in keywords if (k or "").strip()]
        if not keys:
            return []
        hits: list[MemoryEntry] = []
        with self._lock:
            for e in self._entries:
                body = e.text.casefold()
                if any(k in body for k in keys):
                    hits.append(e)
                if len(hits) >= limit:
                    break
        return hits

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._entries)

    def __len__(self) -> int:
        return self.count


__all__ = ["MemoryStore", "MemoryEntry"]
