"""Context compression pipeline stages (pure, offline-testable).

Implements the money-value of the  four-layer history compression
(Master plan §3.3): it reshapes a ``list[ChatMessage]`` COPY so very long tool-heavy
histories fit within the model's context window without ever triggering a 400.

Pipeline order is a hard invariant — L3 persist -> L1 snip -> L2 placeholder ->
(L4 LLM summary) -> hard trim -> sanitize pairs.  Every stage operates on a shallow
copy and must never mutate the caller's list.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ai_coding.domain.message import ChatMessage

PERSISTED_OUTPUT_PREFIX = "<persisted-output"
PERSISTED_MARKER = PERSISTED_OUTPUT_PREFIX
SNIPPED_HEADER = "[snipped {n} messages]"
#: The bare shape, kept for callers that still render a placeholder by hand.
PREVIOUS_RESULT = "[Previous: used {tool}]"
#: The lead of an envelope: how much was elided, and where the original now lives.
ELIDED_NOTE = "[Previous: used {tool} · {chars} chars elided{where}]"
ELIDED_DIR = "transcripts/elided"
#: Rough room a note takes (the envelope line + the ellipsis line) when deciding
#: whether a placeholder would actually shrink a body.
_NOTE_ALLOWANCE = 160
HARD_TRIM_KEEP_TAIL = 20
_L4_BREAKER_THRESHOLD = 3
_MIN_PAGE_MESSAGES = 4


def estimate_tokens(text: str) -> int:
    """Rough heuristic: CJK chars count as ~1/2 token, all others ~1/4 token.

    Monotonic in length — used only to drive compression thresholds, not exact.
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if _is_cjk(ch))
    other = len(text) - cjk
    return math.ceil(cjk / 2) + math.ceil(other / 4)


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (0x4E00 <= o <= 0x9FFF) or (0x3400 <= o <= 0x4DBF) or (0xF900 <= o <= 0xFAFF)


def _text_bytes(content: str | None) -> int:
    return len((content or "").encode("utf-8"))


# ---------------------------------------------------------------- stage 1 (L1)


def snip_middle(
    messages: list[ChatMessage],
    max_messages: int,
    keep_head: int,
    keep_tail: int,
) -> list[ChatMessage]:
    """Trim the (invisible) middle of an over-long history into a placeholder."""
    if len(messages) <= max_messages:
        return messages
    head = messages[:keep_head]
    tail = messages[-keep_tail:]
    n = len(messages) - keep_head - keep_tail
    placeholder = ChatMessage(
        role="user", content=SNIPPED_HEADER.format(n=n), timestamp=""
    )
    return [*head, placeholder, *tail]


# ---------------------------------------------------------------- stage 2 (L2)


def elide_and_persist(body: str, tool: str, root: str | Path | None) -> str | None:
    """Write an elided body to ``root/transcripts/elided/``; return its path.

    Content-addressed: the name is a digest of the body, so the same result
    elided twice does not produce a second file, and a long session does not need
    a rotation policy.  Best-effort — ``None`` means "no path", and the envelope
    simply says less (R7b; the same discipline as L3, docs/r7-analysis.md 3.2).
    """
    if root is None or not body:
        return None
    directory = Path(root) / ELIDED_DIR
    path = directory / _elided_name(tool, body)
    if path.is_file():
        return str(path)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    except OSError:
        return None
    return str(path)


def _elided_name(tool: str, body: str) -> str:
    digest = hashlib.sha1(body.encode("utf-8")).hexdigest()[:12]
    return f"{tool or 'tool'}_{digest}.txt"


def previous_result_note(tool: str, chars: int, path: str | None) -> str:
    """The envelope line: what this was, how much is gone, and where it went."""
    where = f" · full output: {path}" if path else ""
    return ELIDED_NOTE.format(tool=tool or "tool", chars=chars, where=where)


def _elided_body(body: str, note: str, window: int) -> str:
    """The note, then the head and the tail of what it replaced."""
    if window <= 0:
        return note
    return f"{note}\n{body[:window]}\n…\n{body[-window:]}"


def placeholder_old_results(
    messages: list[ChatMessage],
    keep_recent: int,
    *,
    window_chars: int = 200,
    window_budget: int = 4000,
    elide_root: str | Path | None = None,
) -> list[ChatMessage]:
    """Replace tool results older than ``keep_recent`` with a recoverable envelope (R7b).

    Four rules, and each one is a test:

    1. a body that a placeholder would not actually shrink is **left alone**;
    2. otherwise it becomes an envelope naming the artifact, plus a head/tail
       window of ``window_chars`` on each side;
    3. the **total window is bounded** (``window_budget``) and spent newest-first —
       the results that were superseded most recently are the likeliest to still
       matter, and once the budget is gone the envelope is all that is left (the
       path is always there, so nothing is unrecoverable);
    4. pairing fields (``tool_call_id``/``tool_name``) and the message count are
       untouched, and a ``<persisted-output>`` marker is skipped — L3 already
       persisted that body.
    """
    tool_indices = [i for i, m in enumerate(messages) if m.role == "tool"]
    if len(tool_indices) <= keep_recent:
        return messages
    out = list(messages)
    budget = max(0, int(window_budget))
    for i in reversed(tool_indices[:-keep_recent]):
        m = messages[i]
        body = m.content or ""
        if not body or PERSISTED_OUTPUT_PREFIX in body:
            continue
        window = min(int(window_chars), budget) if budget > 0 else 0
        if len(body) <= 2 * window + _NOTE_ALLOWANCE:
            continue  # rule 1: not worth a placeholder at all
        path = elide_and_persist(body, m.tool_name or "tool", elide_root)
        note = previous_result_note(m.tool_name or "tool", len(body), path)
        out[i] = _tool_copy(m, _elided_body(body, note, window))
        budget -= window
    return out


# ---------------------------------------------------------------- stage 3 (L3)


def persist_large_outputs(
    messages: list[ChatMessage],
    root: str | Path,
    max_result_bytes: int,
    per_result_bytes: int,
    preview_len: int = 2000,
) -> list[ChatMessage]:
    """Persist huge trailing tool outputs to ``root/transcripts/persisted/``.

    Only the single-block outputs larger than ``per_result_bytes`` are written;
    the in-history body becomes a ``<persisted-output>`` marker with a preview.
    """
    end = len(messages)
    start = end
    while start > 0 and messages[start - 1].role == "tool":
        start -= 1
    batch = messages[start:end]
    if not batch:
        return messages
    total = sum(_text_bytes(m.content) for m in batch)
    if total <= max_result_bytes:
        return messages

    persisted_dir = Path(root) / "transcripts" / "persisted"
    out = list(messages)
    for i in range(start, end):
        m = messages[i]
        body = m.content or ""
        if _text_bytes(body) <= per_result_bytes:
            continue
        name = _persisted_name(m.tool_name or "tool", body)
        try:
            persisted_dir.mkdir(parents=True, exist_ok=True)
            (persisted_dir / name).write_text(body, encoding="utf-8")
        except OSError:
            continue  # degrade to keeping the original payload
        marker = _persisted_marker(
            str(persisted_dir / name),
            _text_bytes(body),
            m.tool_name or "tool",
            body[:preview_len],
        )
        out[i] = _tool_copy(m, marker)
    return out


def _persisted_name(tool_name: str, body: str) -> str:
    digest = hashlib.sha1(body.encode("utf-8")).hexdigest()[:12]
    return f"{tool_name}_{digest}.txt"


def _persisted_marker(path: str, byte_size: int, tool_name: str, preview: str) -> str:
    return (
        f'<persisted-output path="{path}" bytes="{byte_size}" tool="{tool_name}">\n'
        f"{preview}\n"
        f"</persisted-output>"
    )


# ---------------------------------------------------------------- stage 4 (L4) — orchestrator only


def _tool_copy(m: ChatMessage, content: str) -> ChatMessage:
    """Build a new tool message with a different body but the same pairing fields."""
    return ChatMessage(
        role="tool",
        content=content,
        timestamp=m.timestamp,
        tool_call_id=m.tool_call_id,
        tool_name=m.tool_name,
    )


# ---------------------------------------------------------------- fallback / sanitize


def hard_trim(
    messages: list[ChatMessage], keep_tail: int = HARD_TRIM_KEEP_TAIL
) -> list[ChatMessage]:
    """Blunt fallback: keep everything newer than the tail window, then sanitize."""
    return sanitize_pairs(messages[-keep_tail:])


def sanitize_pairs(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Enforce the strict-pairing invariant after any structural edit.

    Drops orphan ``tool`` results and strips tool calls that have no matching result.
    Non-destructive: produces new messages when a call list is reduced.
    """
    declared = {
        c.id
        for m in messages
        if m.role == "assistant" and m.tool_calls
        for c in m.tool_calls
    }
    kept_ids = {
        m.tool_call_id for m in messages if m.role == "tool" and m.tool_call_id in declared
    }
    out: list[ChatMessage] = []
    for m in messages:
        if m.role == "tool":
            if m.tool_call_id in declared:
                out.append(m)
            continue
        if m.role == "assistant" and m.tool_calls:
            calls = [c for c in m.tool_calls if c.id in kept_ids]
            if not calls and not m.content:
                continue  # drop an empty assistant carrier
            out.append(
                ChatMessage(
                    role=m.role,
                    content=m.content,
                    timestamp=m.timestamp,
                    tool_call_id=m.tool_call_id,
                    tool_name=m.tool_name,
                    tool_calls=calls or None,
                )
            )
            continue
        out.append(m)
    return out


def _joined_history_text(messages: list[ChatMessage]) -> str:
    """Flatten messages into a single text block for token estimation / summarization."""
    parts: list[str] = []
    for m in messages:
        body = (m.content or "").strip()
        if not body:
            continue
        parts.append(f"[{m.role}] {body}")
    return "\n".join(parts)


class ContextManager:
    """Orchestrates the four-layer compression pipeline over a history copy.

    Order is a hard invariant: L3 persist -> L1 snip -> L2 placeholder ->
    L4 LLM summary (with circuit breaker) -> hard trim -> sanitize pairs.
    The caller's message list is never mutated — ``compress`` works on a copy.
    """

    def __init__(
        self,
        ai_settings: Any,
        transcripts_root: str | Path,
        summarizer: Callable[[str], Awaitable[str | None]] | None = None,
    ) -> None:
        self._max_context_tokens = int(getattr(ai_settings, "max_context_tokens", 48000))
        self._max_messages = int(getattr(ai_settings, "max_messages", 50))
        self._keep_head = int(getattr(ai_settings, "snip_keep_head", 3))
        self._keep_tail = int(getattr(ai_settings, "snip_keep_tail", 20))
        self._keep_recent_results = int(
            getattr(ai_settings, "keep_recent_tool_results", 3)
        )
        # R7b: what an elided body keeps (per side) and what all of them may keep
        # together — the window is bounded, the artifact path is not (it costs one
        # line and it is what makes the elision reversible).
        self._l2_window_chars = int(getattr(ai_settings, "l2_window_chars", 200))
        self._l2_window_budget = int(getattr(ai_settings, "l2_window_budget", 4000))
        self._per_result_bytes = int(getattr(ai_settings, "per_result_persist_bytes", 30000))
        self._l4_keep_tail = int(getattr(ai_settings, "l4_keep_tail", 6))
        # R3b: compaction is budget-triggered, not count-triggered (see below).
        self._compress_soft_ratio = float(getattr(ai_settings, "compress_soft_ratio", 0.8))
        self._compress_target_ratio = float(
            getattr(ai_settings, "compress_target_ratio", 0.5)
        )
        self._root = Path(transcripts_root)
        self._summarizer = summarizer
        self._l4_failures = 0
        # L3 triggers at (roughly) half the context budget, mirroring the Java rule.
        self._max_result_bytes = max(1, self._max_context_tokens // 2)

    async def compress(
        self,
        messages: list[ChatMessage],
        *,
        on_summary: Callable[[str], None] | None = None,
    ) -> list[ChatMessage]:
        """Run the four-layer pipeline once (M3 semantics, unchanged)."""
        out = list(messages)  # copy — never mutate the caller's history
        out = persist_large_outputs(
            out,
            root=self._root,
            max_result_bytes=self._max_result_bytes,
            per_result_bytes=self._per_result_bytes,
        )
        out = snip_middle(out, self._max_messages, self._keep_head, self._keep_tail)
        out = placeholder_old_results(
            out,
            self._keep_recent_results,
            window_chars=self._l2_window_chars,
            window_budget=self._l2_window_budget,
            elide_root=self._root,
        )

        if self._l4_should_run(out) and self._summarizer is not None:
            out = await self._run_l4(out, on_summary=on_summary)

        if estimate_tokens(_joined_history_text(out)) > self._max_context_tokens:
            out = hard_trim(out, HARD_TRIM_KEEP_TAIL)

        return sanitize_pairs(out)

    # ---- R3b: the compaction policy -----------------------------------

    def should_compact(self, messages: list[ChatMessage]) -> bool:
        """True only once the estimate crosses the soft water mark."""
        soft = int(self._max_context_tokens * self._compress_soft_ratio)
        return estimate_tokens(_joined_history_text(messages)) > soft

    async def compress_if_needed(
        self,
        messages: list[ChatMessage],
        *,
        force: bool = False,
        on_summary: Callable[[str], None] | None = None,
    ) -> list[ChatMessage]:
        """Compact **only** when the budget says so (or the user asked).

        Below the soft water mark the history is returned untouched — that is what
        keeps the prompt-cache prefix stable (measured: 92-93% hit rate with
        append-only turns, 26% when the middle was rewritten every turn).
        """
        if not force and not self.should_compact(messages):
            return messages
        out = await self.compress(messages, on_summary=on_summary)
        target = int(self._max_context_tokens * self._compress_target_ratio)
        guard = 0
        while (
            estimate_tokens(_joined_history_text(out)) > target
            and len(out) > _MIN_PAGE_MESSAGES
            and guard < 4
        ):
            out = hard_trim(out, max(_MIN_PAGE_MESSAGES, len(out) // 2))
            guard += 1
        return out

    # ---- L4 -----------------------------------------------------------

    def _l4_should_run(self, messages: list[ChatMessage]) -> bool:
        if self._l4_failures >= _L4_BREAKER_THRESHOLD:
            return False
        return estimate_tokens(_joined_history_text(messages)) > self._max_context_tokens

    async def _run_l4(
        self,
        messages: list[ChatMessage],
        *,
        on_summary: Callable[[str], None] | None = None,
    ) -> list[ChatMessage]:
        head, tail = messages[:-self._l4_keep_tail], messages[-self._l4_keep_tail:]
        head_text = _joined_history_text(head)
        self._persist_audit_copy(head_text)
        summary: str | None
        try:
            summary = await self._summarizer(head_text)  # type: ignore[misc]
            self._l4_failures = 0
        except Exception:
            self._l4_failures += 1
            summary = None
        if not summary:
            return messages  # degrade to the trimmed history
        if on_summary is not None:
            try:
                on_summary(summary)
            except Exception:  # noqa: BLE001 — a caller callback must not break us
                pass
        note = ChatMessage(
            role="user", content=f"[以下为早期对话摘要]\n{summary}", timestamp=""
        )
        return [note, *tail]

    def _persist_audit_copy(self, text: str) -> None:
        try:
            root = self._root / "transcripts"
            root.mkdir(parents=True, exist_ok=True)
            name = f"summary_{hashlib.sha1(text.encode('utf-8')).hexdigest()[:12]}.txt"
            (root / name).write_text(text, encoding="utf-8")
        except OSError:
            pass  # best-effort audit artifact; never breaks compression


__all__ = [
    "ELIDED_DIR",
    "ELIDED_NOTE",
    "PERSISTED_OUTPUT_PREFIX",
    "PERSISTED_MARKER",
    "PREVIOUS_RESULT",
    "HARD_TRIM_KEEP_TAIL",
    "elide_and_persist",
    "estimate_tokens",
    "previous_result_note",
    "snip_middle",
    "placeholder_old_results",
    "persist_large_outputs",
    "hard_trim",
    "sanitize_pairs",
    "ContextManager",
]
