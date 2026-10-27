"""Optional semantic second opinion for memory recall (R10d).

This module is the only place in the memory subsystem that touches the network,
and it only ever does so when explicitly enabled.  Everything else — scoring,
gating, ranking — lives in :mod:`ai_coding.memory.relevance`, which never
imports this module.  :mod:`ai_coding.memory.service` asks this module for
cosines and hands them to the ranker; the ranker does not know where they came
from.

Two facts shape every decision here:

  - Vectors are cached on disk keyed by endpoint, model, dimensions and note
    content, so each note is embedded once ever.  Total embedding work is
    identical whether it happens eagerly or lazily; only *when* it happens
    differs.  This module embeds lazily, on read, so a write never pays for a
    search nobody makes.
  - An endpoint that has no ``/embeddings`` must cost exactly one failed call
    per process, not one per search.  The capability latch below is that one
    call.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import urllib.request
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ai_coding.config.models import EmbeddingsConfig
from ai_coding.memory.note import MemoryNote
from ai_coding.providers import PROVIDERS

_log = logging.getLogger(__name__)

__all__ = [
    "EmbeddingIndex",
    "EmbeddingsUnsupportedError",
    "Transport",
    "urllib_transport",
]

#: ``transport(model, inputs) -> vectors``.  Injected so every degradation path
#: is testable without a network; production builds one from ``urllib`` below.
Transport = Callable[[str, list[str]], Awaitable[list[list[float]]]]


class EmbeddingsUnsupportedError(Exception):
    """The endpoint answered, but it has no embedding capability (HTTP 404)."""


async def urllib_transport(
    base_url: str, api_key: str, timeout: float
) -> Transport:
    """Build the production transport: OpenAI-shaped ``/embeddings`` over stdlib."""

    async def _call(model: str, inputs: list[str]) -> list[list[float]]:
        payload = json.dumps({"model": model, "input": inputs}).encode("utf-8")
        request = urllib.request.Request(
            base_url.rstrip("/") + "/embeddings",
            data=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise EmbeddingsUnsupportedError(
                    f"POST {base_url.rstrip('/')}/embeddings -> 404"
                ) from exc
            raise
        items = sorted(body.get("data", []), key=lambda row: row.get("index", 0))
        return [list(map(float, row["embedding"])) for row in items]

    return _call


class EmbeddingIndex:
    """Vectors for notes, cached on disk, cosine against a query on demand."""

    def __init__(
        self,
        config: EmbeddingsConfig,
        models: Mapping[str, Any],
        *,
        transport: Transport | None = None,
        cache_path: str | Path | None = None,
    ) -> None:
        self._config = config
        self._models = models
        self._transport = transport
        self._path = Path(cache_path) if cache_path is not None else Path(config.cache_path)
        self._cache: dict[str, list[float]] | None = None
        self._unsupported = False
        self._seen_ok = False
        # M-H2: `provider` here is a key in `models`, not a provider id (that is
        # the field `models.<key>.provider`).  With a provider id in this slot the
        # index reports `available() == False` and the semantic tier silently
        # never runs -- so say which confusion it is.
        if (
            config.enabled
            and config.provider
            and config.provider not in models
            and config.provider in PROVIDERS
        ):
            _log.warning(
                "memory.embeddings.provider names a models key, not a provider id: "
                "%r is a provider id and %s has no such key, so the semantic tier "
                "will stay off; use one of %s",
                config.provider,
                "models",
                sorted(models),
            )

    # ---- configuration ----------------------------------------------------

    @property
    def _model(self) -> Any | None:
        return self._models.get(self._config.provider)

    def available(self) -> bool:
        """Whether a call could even be attempted.  No network, no IO."""
        if not self._config.enabled or not self._config.provider:
            return False
        model = self._model
        if model is None:
            return False
        return bool(str(getattr(model, "base_url", "") or "").strip())

    def _key(self, note: MemoryNote) -> str:
        """Cache identity of one note's vector.

        The endpoint is part of the key: the same model name served by two
        endpoints produces different vectors, and without the endpoint a stale
        vector would be served silently.  ``dedupe_key`` already covers the text,
        so an edited note re-embeds and the old entry is orphaned, not reused.
        """
        model = self._model
        base_url = str(getattr(model, "base_url", "") if model else "")
        parts = [
            base_url,
            str(self._config.model),
            str(int(self._config.dimensions)),
            note.dedupe_key,
        ]
        return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()

    # ---- capability -------------------------------------------------------

    async def provider_lacks_embeddings(self) -> bool:
        """True once the endpoint has proven it has no embedding capability.

        Probes exactly once per process; afterwards the answer is remembered.
        A supported endpoint that has already answered successfully is never
        probed.  This is the one failed call the design allows, instead of one
        per search.
        """
        if self._unsupported:
            return True
        if self._seen_ok:
            return False
        try:
            await self._call(["test"])
        except EmbeddingsUnsupportedError:
            self._unsupported = True
            return True
        return False

    async def _call(self, inputs: list[str]) -> list[list[float]]:
        transport = self._transport
        if transport is None:
            model = self._model
            base_url = str(getattr(model, "base_url", "") if model else "")
            api_key = str(getattr(model, "api_key", "") if model else "")
            transport = await urllib_transport(
                base_url, api_key, float(self._config.timeout_seconds)
            )
        try:
            vectors = await transport(str(self._config.model), list(inputs))
        except EmbeddingsUnsupportedError:
            self._unsupported = True
            raise
        self._seen_ok = True
        return vectors

    # ---- vectors ----------------------------------------------------------

    def _load(self) -> dict[str, list[float]]:
        if self._cache is not None:
            return self._cache
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            raw = {}
        cache: dict[str, list[float]] = {}
        if isinstance(raw, dict):
            for key, value in raw.items():
                if isinstance(value, list) and all(
                    isinstance(item, (int, float)) for item in value
                ):
                    cache[str(key)] = [float(item) for item in value]
        self._cache = cache
        return cache

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._cache or {}, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass  # the cache is a pure accelerator; losing it only costs recompute

    def _expected_dimensions(self) -> int | None:
        dimensions = int(self._config.dimensions)
        return dimensions if dimensions > 0 else None

    async def vectors_for(
        self, notes: Sequence[MemoryNote]
    ) -> dict[str, tuple[float, ...]]:
        """``{note.id: vector}``, computing and caching whatever is missing.

        One transport call for the whole batch, one disk write however many were
        missing.  A cached vector whose length disagrees with the configured
        dimensions is discarded and recomputed — unless dimensions is 0, which
        means "take whatever the endpoint returns".
        """
        cache = self._load()
        expected = self._expected_dimensions()
        missing: list[MemoryNote] = []
        missing_keys: list[str] = []
        for note in notes:
            key = self._key(note)
            cached = cache.get(key)
            if cached is not None and (
                expected is None or len(cached) == expected
            ):
                continue
            missing.append(note)
            missing_keys.append(key)
        if missing:
            # Batch: endpoints cap inputs per request, and one giant POST that
            # the endpoint rejects would fail the whole search.  Ten is safely
            # under every documented limit and keeps the failure unit small.
            for start in range(0, len(missing), 10):
                chunk = missing[start : start + 10]
                vectors = await self._call([note.text for note in chunk])
                for key, vector in zip(
                    missing_keys[start : start + 10], vectors, strict=True
                ):
                    cache[key] = [float(item) for item in vector]
            self._save()
        out: dict[str, tuple[float, ...]] = {}
        for note in notes:
            cached = cache.get(self._key(note))
            if cached is not None:
                out[note.id] = tuple(cached)
        return out

    async def query_vector(self, query: str) -> tuple[float, ...] | None:
        """Embed one query.  Never cached: queries are cheap and unbounded."""
        if not query.strip():
            return None
        vectors = await self._call([query])
        if not vectors:
            return None
        return tuple(float(item) for item in vectors[0])

    # ---- scoring ----------------------------------------------------------

    @staticmethod
    def cosine(left: Sequence[float], right: Sequence[float]) -> float:
        """Cosine similarity in [-1, 1]; 0.0 when either side has no length."""
        dot = sum(a * b for a, b in zip(left, right, strict=False))
        norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(
            sum(b * b for b in right)
        )
        if norm <= 0.0:
            return 0.0
        return max(-1.0, min(dot / norm, 1.0))

    async def cosine_scores(
        self, query: str, notes: Sequence[MemoryNote]
    ) -> dict[str, float]:
        """``{note.id: raw cosine}`` of the query against each note.

        Raw, not normalised: normalisation is the ranker's job, and the
        absolute gate needs the unnormalised number.  Notes with no vector are
        simply absent from the result.
        """
        if not query.strip() or not notes:
            return {}
        query_vector = await self.query_vector(query)
        if query_vector is None:
            return {}
        vectors = await self.vectors_for(notes)
        return {
            note_id: self.cosine(query_vector, vector)
            for note_id, vector in vectors.items()
        }
