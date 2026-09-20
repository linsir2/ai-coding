"""Memory service — recall / remember / dream (M3).

Design (m3-analysis §4.5): a thin orchestration layer over :class:`MemoryStore`.
``remember`` persists extracted conclusions, ``recall`` injects the most relevant
past notes into the next system prompt, and ``dream`` periodically consolidates.

The LLM path is injected via ``extractor(texts, conclusion) -> list[str]``; when it is
``None`` an offline fallback simply grabs recent user/assistant text lines.  Every
public method degrades silently — memory never raises (invariant §5-11).
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from types import SimpleNamespace
from typing import Any

from ai_coding.domain.message import ChatMessage
from ai_coding.memory.store import MemoryStore

Extractor = Callable[
    [Sequence[ChatMessage], str | None],
    "list[str] | Awaitable[list[str]]",
]

_WORD_SPLIT = re.compile(r"[^\w\u4e00-\u9fff]+")


def _default_config(**overrides: Any) -> SimpleNamespace:
    base = {
        "enabled": True,
        "auto_extract": True,
        "consolidate_threshold": 10,
        "max_index_entries": 200,
        "max_per_turn_injections": 5,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _keywords_from(text: str) -> list[str]:
    return [tok for tok in _WORD_SPLIT.split(text.casefold()) if len(tok) > 1]


def _collect_recent_signal(messages: Sequence[ChatMessage]) -> list[str]:
    """Offline fallback signal for recall/remember: recent user text lines."""
    lines: list[str] = []
    for m in messages:
        if m.role not in ("user", "assistant"):
            continue
        body = (m.content or "").strip()
        if body:
            lines.append(body)
    return lines[-8:]


class MemoryService:
    """Recalls, remembers, and consolidates compact memories across turns."""

    def __init__(
        self,
        store: MemoryStore,
        extractor: Extractor | None = None,
        config: Any | None = None,
    ) -> None:
        self._store = store
        self._extractor = extractor
        self._config = config or _default_config()

    # ---- remember ---------------------------------------------------------

    async def remember(
        self,
        messages: Sequence[ChatMessage],
        conclusion: str | None = None,
    ) -> list[str]:
        """Persist extracted memories. Returns ids of newly stored entries."""
        ids: list[str] = []
        if not getattr(self._config, "enabled", True):
            return ids
        try:
            texts = await self._remember_texts(messages, conclusion)
        except Exception:
            return ids  # memory is best-effort; never raise
        for text in texts:
            stored = self._store.add(text)
            if stored:
                ids.append(stored)
        self._maybe_trim()
        return ids

    async def _remember_texts(
        self,
        messages: Sequence[ChatMessage],
        conclusion: str | None,
    ) -> list[str]:
        if self._extractor is not None:
            return await asyncio_guard(self._extractor(messages, conclusion))
        return _collect_recent_signal(messages)

    # ---- recall -----------------------------------------------------------

    async def recall(
        self,
        history: Sequence[ChatMessage],
        k: int | None = None,
    ) -> list[str]:
        """Return up to ``k`` existing memory texts most relevant to the history."""
        supply = (
            int(getattr(self._config, "max_per_turn_injections", 5))
            if k is None
            else k
        )
        if supply <= 0:
            return []
        try:
            keywords = _keywords_from(" ".join(_collect_recent_signal(history)))
            entries = self._store.search(keywords, limit=supply)
        except Exception:
            return []
        return [e.text for e in entries]

    # ---- dream ------------------------------------------------------------

    async def dream(self, context: Sequence[ChatMessage] | None = None) -> list[str]:
        """Consolidate when the index grows past the threshold. Returns new ids."""
        ids: list[str] = []
        threshold = int(getattr(self._config, "consolidate_threshold", 10))
        if self._store.count < threshold:
            return ids
        try:
            if self._extractor is None:
                return ids  # no LLM extractor -> nothing to consolidate offline
            texts = await asyncio_guard(
                self._extractor(list(context or []), None)
            )
        except Exception:
            return ids
        for text in texts:
            stored = self._store.add(text)
            if stored:
                ids.append(stored)
        self._maybe_trim()
        return ids

    # ---- internal ---------------------------------------------------------

    def _maybe_trim(self) -> None:
        cap = int(getattr(self._config, "max_index_entries", 200))
        entries = self._store.all()
        if len(entries) <= cap:
            return
        for e in entries[: len(entries) - cap]:
            self._store.delete(e.id)


async def asyncio_guard(value: list[str] | Awaitable[list[str]]) -> list[str]:
    """Coerce a sync or async extractor result into a list."""
    if hasattr(value, "__await__"):
        return list(await value)
    return list(value)


__all__ = ["MemoryService"]
