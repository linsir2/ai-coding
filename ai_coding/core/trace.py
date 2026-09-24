"""Trace recording: redaction, request digests, storage and the recorder (R4a).

Everything here is on the *observation* side of the loop, which means it must
satisfy two rules that pull in opposite directions:

* it must be honest — a trace is only worth keeping if it says what really
  happened (real token usage, real durations, real fallbacks);
* it must be harmless — it may never change a request, a message or a decision,
  and it may never break a turn (docs/r4-analysis.md P1/P5).

The other thing it must be is *safe*: a trace records tool parameters verbatim,
and ``config.local.yaml`` contains a live API key, so every string is redacted
before it can reach disk (§4.5).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_coding.core.events import (
    ContextAssembled,
    IterationStarted,
    ModelStepFinished,
    ScopedEvent,
    ToolFinished,
    ToolStarted,
    TurnFinished,
)
from ai_coding.domain.message import ChatMessage

__all__ = [
    "REDACTION_MARK",
    "TRACE_VERSION",
    "Redactor",
    "TraceStore",
    "TraceRecorder",
    "TurnWriter",
    "canonical_request",
    "digest_of",
    "now_iso",
    "prefix_shared",
    "short_json",
]

_log = logging.getLogger(__name__)

REDACTION_MARK = "«redacted»"
TRACE_VERSION = 1

# High-confidence shapes only.  A missed secret is worse than an ugly trace, but a
# pattern that eats ordinary prose makes the trace useless — so these all require
# a recognisable token shape (docs/r4-analysis.md 4.5).
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
    ),
)

# "api_key: abc" keeps the label (it is useful) and drops the value.
_KV_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?key|token|password|passwd|secret)\b\s*[:=]\s*\S+"
)


def _kv_replace(match: re.Match[str]) -> str:
    label = match.group(1)
    return f"{label}: {REDACTION_MARK}"


class Redactor:
    """Replace high-confidence secrets with a marker.  Pure and idempotent."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    def redact(self, text: str) -> str:
        if not self.enabled or not text:
            return text
        out = text
        for pattern in _SECRET_PATTERNS:
            out = pattern.sub(REDACTION_MARK, out)
        return _KV_PATTERN.sub(_kv_replace, out)

    def redact_obj(self, obj: Any) -> Any:
        """Walk a JSON-ish structure, redacting every string it contains."""
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, Mapping):
            return {key: self.redact_obj(value) for key, value in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.redact_obj(item) for item in obj]
        if isinstance(obj, set):
            return {self.redact_obj(item) for item in obj}
        return obj


def digest_of(text: str, limit: int = 4096) -> str:
    """A short fingerprint of a request's head — enough to spot a prefix change."""
    head = text[:limit].encode("utf-8", errors="replace")
    return hashlib.sha1(head).hexdigest()[:16]


def prefix_shared(previous: str | None, current: str) -> tuple[int, float]:
    """Longest common prefix length (chars) and its share of ``current``.

    This is the *estimate* half of cache accounting: it answers "did the front of
    the request move?", which is what actually costs a cache miss.  The provider's
    own ``cached_tokens`` is the truth; this is the fallback for endpoints that
    report nothing (docs/r4-analysis.md 4.4).
    """
    if not current:
        return 0, 0.0
    if not previous:
        return 0, 0.0
    limit = min(len(previous), len(current))
    index = 0
    while index < limit and previous[index] == current[index]:
        index += 1
    return index, index / len(current)


def canonical_request(
    instructions: str | None,
    messages: Sequence[ChatMessage],
    tools_chars: int = 0,
) -> str:
    """A stable rendering of what will be sent, for prefix comparison.

    Deliberately close to (not identical to) the engine's own mapping: the trace
    only needs a *stable* text so two consecutive requests can be compared, and
    the ratio is reported as an estimate (docs/r4-analysis.md 4.2/4.4).
    """
    parts: list[str] = [instructions or ""]
    for message in messages:
        calls = ""
        if message.tool_calls:
            calls = "|".join(f"{c.id}:{c.name}:{c.arguments}" for c in message.tool_calls)
        parts.append(
            f"{message.role}:{message.tool_call_id or ''}:{message.content or ''}:{calls}"
        )
    parts.append(f"tools:{tools_chars}")
    return "\n".join(parts)


def now_iso() -> str:
    """UTC timestamp for trace lines (millisecond precision)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def short_json(obj: Any) -> str:
    """Compact JSON for a trace line body."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


class TurnWriter:
    """Append-only writer for one turn's trace, bounded by a byte cap.

    Once the cap is reached the writer emits a single ``truncated`` marker and
    refuses everything after it — a half-written line is never worth keeping.
    """

    def __init__(self, path: Path, *, max_bytes: int) -> None:
        self.path = path
        self.max_bytes = max(1, int(max_bytes))
        self.truncated = False
        self._written = 0
        self._failed = False

    def _append(self, line: str) -> bool:
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            self._failed = True
            return False
        self._written += len(line.encode("utf-8")) + 1
        return True

    def write_line(self, line: str) -> bool:
        """Append one line.  Returns ``False`` if it was dropped."""
        if self._failed or self.truncated:
            return False
        if self._written + len(line.encode("utf-8")) + 1 > self.max_bytes:
            self.truncated = True
            self._append(
                short_json(
                    {
                        "v": TRACE_VERSION,
                        "ts": now_iso(),
                        "evt": "truncated",
                        "data": {"reason": "max_turn_bytes", "limit": self.max_bytes},
                    }
                )
            )
            return False
        return self._append(line)


class TraceStore:
    """Where traces live, how they are numbered, capped and cleaned up."""

    def __init__(
        self,
        root: str | Path,
        *,
        max_turn_bytes: int = 2_000_000,
        retention_days: int = 14,
    ) -> None:
        self.root = Path(root)
        self.max_turn_bytes = max(1, int(max_turn_bytes))
        self.retention_days = max(0, int(retention_days))

    def session_dir(self, session_id: str) -> Path:
        return self.root / (session_id or "unknown")

    def begin_turn(self, session_id: str) -> TurnWriter:
        """Open the next turn file for ``session_id`` (numbering is on disk)."""
        directory = self.session_dir(session_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"turn-{self.next_turn_number(session_id)}.jsonl"
        # Touch it now: numbering is derived from the directory listing, so two
        # turns starting back-to-back must not both claim the same file name.
        try:
            path.touch(exist_ok=True)
        except OSError:
            _log.debug("trace file touch failed", exc_info=True)
        return TurnWriter(path, max_bytes=self.max_turn_bytes)

    def next_turn_number(self, session_id: str) -> int:
        directory = self.session_dir(session_id)
        highest = 0
        if directory.is_dir():
            for entry in directory.glob("turn-*.jsonl"):
                stem = entry.stem.removeprefix("turn-")
                if stem.isdigit():
                    highest = max(highest, int(stem))
        return highest + 1

    def summary_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "summary.json"

    def read_summary(self, session_id: str) -> dict[str, Any]:
        """The session summary as it stands on disk (``{}`` when absent/broken)."""
        path = self.summary_path(session_id)
        if not path.is_file():
            return {}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def update_summary(self, session_id: str, payload: Mapping[str, Any]) -> None:
        """Merge ``payload`` into the session summary, atomically."""
        path = self.summary_path(session_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            merged: dict[str, Any] = {}
            if path.is_file():
                try:
                    loaded = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        merged = loaded
                except (OSError, ValueError):
                    merged = {}
            merged.update(payload)
            temp = path.with_suffix(".json.tmp")
            temp.write_text(short_json(merged), encoding="utf-8")
            os.replace(temp, path)
        except OSError:
            _log.debug("trace summary write failed", exc_info=True)

    def prune(self, now: float | None = None) -> int:
        """Delete session directories whose last write is past retention."""
        if self.retention_days <= 0 or not self.root.is_dir():
            return 0
        cutoff = (now if now is not None else time.time()) - self.retention_days * 86400
        removed = 0
        for entry in self.root.iterdir():
            if not entry.is_dir():
                continue
            try:
                if entry.stat().st_mtime >= cutoff:
                    continue
                for child in sorted(entry.rglob("*"), reverse=True):
                    if child.is_file():
                        child.unlink()
                    else:
                        child.rmdir()
                entry.rmdir()
            except OSError:
                continue
            removed += 1
        return removed


_PREVIEW_LIMIT = 500


def _trim(value: Any, limit: int) -> Any:
    """Trim long strings anywhere in a trace payload (recorder-side guard)."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "…"
    if isinstance(value, Mapping):
        return {key: _trim(item, limit) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_trim(item, limit) for item in value]
    return value


class TraceRecorder:
    """A ``LoopObserver`` that writes one JSONL file per turn (R4a).

    It owns no opinion about the loop: it labels events with their span, redacts
    strings, keeps a running ledger of the turn's tokens, and swallows every
    failure.  A trace that breaks must never break the thing it observes.
    """

    def __init__(
        self,
        store: TraceStore,
        session_id: str,
        *,
        redactor: Redactor | None = None,
        capture: str = "previews",
    ) -> None:
        self.store = store
        self.session_id = session_id
        self.redactor = redactor if redactor is not None else Redactor()
        self.capture = capture if capture in {"previews", "full"} else "previews"
        self.failures = 0
        self.turn = 0
        self._writer: TurnWriter | None = None
        # Kept after the turn ends so ``/trace`` still has something to show.
        self.last_path: Path | None = None
        self._iteration = 0
        self._turn_usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}
        self._turn_steps = 0

    # ---- public ------------------------------------------------------------

    @property
    def current_path(self) -> Path | None:
        if self._writer is not None:
            return self._writer.path
        return self.last_path

    def on_event(self, event: Any) -> None:
        """Record one event.  Never raises (docs/r4-analysis.md P1)."""
        try:
            self._record(event)
        except Exception:  # noqa: BLE001 — observation must not break the turn
            self.failures += 1
            _log.debug("trace recorder failed on %s", type(event).__name__, exc_info=True)

    # ---- internals ---------------------------------------------------------

    def _record(self, event: Any) -> None:
        if self._writer is None:
            self.turn += 1
            self._writer = self.store.begin_turn(self.session_id)
            self._turn_usage = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cached_tokens": 0,
            }
            self._turn_steps = 0

        name, payload, span, parent, iteration, scope = self._describe(event)
        line: dict[str, Any] = {
            "v": TRACE_VERSION,
            "ts": now_iso(),
            "session": self.session_id,
            "turn": self.turn,
            "span": span,
            "parent": parent,
            "iter": iteration,
            "evt": name,
            "data": payload,
        }
        if scope:
            line["scope"] = scope
        if not self._writer.write_line(short_json(line)):
            if self._writer.truncated:
                return
            self.failures += 1
            return

        self._accumulate(event)
        if name == "TurnFinished":
            self._finish_turn()

    def _finish_turn(self) -> None:
        if self._writer is not None:
            self.last_path = self._writer.path
        existing = self.store.read_summary(self.session_id)
        usage = self._turn_usage
        self.store.update_summary(
            self.session_id,
            {
                "turns": int(existing.get("turns", 0)) + 1,
                "steps": int(existing.get("steps", 0)) + self._turn_steps,
                "prompt_tokens": int(existing.get("prompt_tokens", 0))
                + usage["prompt_tokens"],
                "completion_tokens": int(existing.get("completion_tokens", 0))
                + usage["completion_tokens"],
                "cached_tokens": int(existing.get("cached_tokens", 0))
                + usage["cached_tokens"],
            },
        )
        self._writer = None

    def _accumulate(self, event: Any) -> None:
        inner = event.event if isinstance(event, ScopedEvent) else event
        if isinstance(inner, ModelStepFinished):
            self._turn_steps += 1
            usage = inner.usage or {}
            self._turn_usage["prompt_tokens"] += int(usage.get("prompt_tokens", 0) or 0)
            self._turn_usage["completion_tokens"] += int(
                usage.get("completion_tokens", 0) or 0
            )
            self._turn_usage["cached_tokens"] += int(inner.cached_tokens or 0)

    def _describe(
        self, event: Any
    ) -> tuple[str, Any, str, str, int, str]:
        """Return ``(name, payload, span, parent, iteration, scope)``."""
        scope = ""
        if isinstance(event, ScopedEvent):
            scope = event.scope
            inner = event.event
            name, payload, span, _parent, iteration, _inner_scope = self._describe(inner)
            return name, payload, span, event.parent, iteration, scope

        name = type(event).__name__
        payload = self.redactor.redact_obj(dataclasses.asdict(event))
        if self.capture == "previews":
            payload = _trim(payload, _PREVIEW_LIMIT)

        if isinstance(event, IterationStarted):
            self._iteration = event.index
            return name, payload, f"i{event.index}", "turn", event.index, scope
        if isinstance(event, ModelStepFinished):
            return name, payload, f"step{event.index}", f"i{event.index}", event.index, scope
        if isinstance(event, ContextAssembled):
            self._iteration = event.iteration
            return name, payload, f"i{event.iteration}", "turn", event.iteration, scope
        if isinstance(event, (ToolStarted, ToolFinished)):
            iteration = event.iteration or self._iteration
            return name, payload, event.call_id, f"i{iteration}", iteration, scope
        if isinstance(event, TurnFinished):
            return name, payload, "turn", "", self._iteration, scope
        return name, payload, f"i{self._iteration}", "turn", self._iteration, scope
