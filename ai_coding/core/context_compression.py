"""Context compression pipeline stages (pure, offline-testable).

Implements the money-value of the ThoughtCoding four-layer history compression
(Master plan §3.3): it reshapes a ``list[ChatMessage]`` COPY so very long tool-heavy
histories fit within the model's context window without ever triggering a 400.

Pipeline order is a hard invariant — L3 persist -> L1 snip -> L2 placeholder ->
(L4 LLM summary) -> hard trim -> sanitize pairs.  Every stage operates on a shallow
copy and must never mutate the caller's list.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

from ai_coding.domain.message import ChatMessage

PERSISTED_OUTPUT_PREFIX = "<persisted-output"
PERSISTED_MARKER = PERSISTED_OUTPUT_PREFIX
SNIPPED_HEADER = "[snipped {n} messages]"
PREVIOUS_RESULT = "[Previous: used {tool}]"
HARD_TRIM_KEEP_TAIL = 20


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


def placeholder_old_results(
    messages: list[ChatMessage],
    keep_recent: int,
) -> list[ChatMessage]:
    """Replace tool results older than ``keep_recent`` with a ``[Previous: used X]`` note."""
    tool_indices = [i for i, m in enumerate(messages) if m.role == "tool"]
    if len(tool_indices) <= keep_recent:
        return messages
    out = list(messages)
    for i in tool_indices[:-keep_recent]:
        m = messages[i]
        if m.content and PERSISTED_OUTPUT_PREFIX not in m.content:
            body = PREVIOUS_RESULT.format(tool=m.tool_name or "tool")
            out[i] = _tool_copy(m, body)
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
        marker = _persisted_marker(str(persisted_dir / name), _text_bytes(body), m.tool_name, body[:preview_len])
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


def hard_trim(messages: list[ChatMessage], keep_tail: int = HARD_TRIM_KEEP_TAIL) -> list[ChatMessage]:
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


__all__ = [
    "PERSISTED_OUTPUT_PREFIX",
    "PERSISTED_MARKER",
    "PREVIOUS_RESULT",
    "HARD_TRIM_KEEP_TAIL",
    "estimate_tokens",
    "snip_middle",
    "placeholder_old_results",
    "persist_large_outputs",
    "hard_trim",
    "sanitize_pairs",
]