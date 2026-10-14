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
import inspect
import math
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from ai_coding.ai.llm_helpers import (
    CHECKPOINT_CLOSE,
    CHECKPOINT_OPEN,
    SINCE_CLOSE,
    SINCE_OPEN,
    checkpoint_body,
)
from ai_coding.domain.message import CHECKPOINT_MARK, ChatMessage

PERSISTED_OUTPUT_PREFIX = "<persisted-output"
PERSISTED_MARKER = PERSISTED_OUTPUT_PREFIX
SNIPPED_HEADER = "[snipped {n} messages]"
#: ``CHECKPOINT_MARK`` is re-exported from ``domain.message`` so the compressor
#: keeps one definition with the two other readers (session preview, frontend).
#: The string is public enough that a pasted log could contain it, which is why
#: recognising it also goes through ``checkpoint_body``'s shape check.
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


def compaction_ceiling(window: int) -> int:
    """The usage level at which compaction is due (OpenCode parity).

    ``window - max(10% of window, 16k once the window is >= 32k)``
    (compaction.ts ``calculateCeiling``): a percentage alone leaves a small
    model no room to finish its answer, and no floor at all leaves a big
    model compacting constantly.  The measure compared against it stays
    ``max(observed, estimate)`` — the provider's own count when there is
    one (M11-0e), not a second estimate.
    """
    window = int(window or 0)
    if window <= 0:
        return window
    reserve = max(window // 10, 16_000 if window >= 32_000 else 0)
    return window - reserve


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
    if len(messages) <= keep_head + keep_tail:
        # head and tail would overlap, and trimming that copies the head: a
        # checkpoint at the head is then installed twice, and every trimmed
        # message is sent twice.  Going over budget is temporary; duplication is
        # corruption.  Measured live with max_messages=12 — see
        # tests/test_compaction_shape.py.
        return messages
    head = messages[:keep_head]
    tail = messages[-keep_tail:]
    n = len(messages) - keep_head - keep_tail
    placeholder = ChatMessage(
        role="user", content=SNIPPED_HEADER.format(n=n), timestamp=""
    )
    return [*head, placeholder, *tail]


def _is_snip_placeholder(message: ChatMessage) -> bool:
    """True for a ``snip_middle`` deletion marker — and only for one.

    The marker is a public string a user could theoretically type, so the
    match is the whole template shape (``[snipped <n> messages]``), not a
    substring: prose merely mentioning a snip must survive.
    """
    body = (message.content or "").strip()
    return bool(_SNIP_PLACEHOLDER_RE.match(body))


_SNIP_PLACEHOLDER_RE = re.compile(r"\[snipped \d+ messages\]")


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


def _history_tokens(messages: list[ChatMessage]) -> int:
    """Text plus the images, for every threshold that decides about compressing."""
    return estimate_tokens(_joined_history_text(messages)) + image_tokens(messages)


#: What one image costs the model's context (r21).
#:
#: This used to be ``bytes // 4`` -- the base64 character count.  That is the
#: request's *byte* weight, not the model's token count, and the two differ by
#: two orders of magnitude: a 1 MB screenshot is billed at roughly 1,500 tokens
#: but was charged 262,144, which is past the 230,400 ceiling of a 256k window.
#: One pasted screenshot therefore opened the compaction gate on a session with
#: a single turn; compaction found nothing worth summarising and every iteration
#: announced "无可压缩内容".
#:
#: Vision APIs downscale an image before tokenising it and cap the result, so
#: the cost is dominated by resolution and saturates -- a bigger file is not a
#: proportionally bigger request.  A flat, documented estimate is closer to the
#: truth than a size-proportional one, and it still keeps an image visible to
#: the budget, which is what r19/F3 set out to do.
_IMAGE_TOKENS = 1_600


def image_tokens(messages: list[ChatMessage]) -> int:
    """What the images a request carries cost it (r19/F3, corrected by r21).

    ``ImageRef.bytes`` being present is what says the image was measured; an
    older session without it keeps costing nothing rather than being invented a
    size.  What changed is the *amount*: the base64 length is not the token
    count, and charging it made one screenshot look like a full context.
    """
    count = sum(1 for message in messages for ref in message.images if ref.bytes > 0)
    return _IMAGE_TOKENS * count


def _joined_history_text(messages: list[ChatMessage]) -> str:
    """Flatten messages into a single text block for token estimation / summarization."""
    parts: list[str] = []
    for m in messages:
        body = (m.content or "").strip()
        if not body:
            continue
        parts.append(f"[{m.role}] {body}")
    return "\n".join(parts)


def split_checkpoint(
    messages: Sequence[ChatMessage],
) -> tuple[str, list[ChatMessage]]:
    """Separate the previous compaction's verdict from what came after it.

    Measured: on the second compaction the old summary *was* reaching the
    summariser, as ``[user] [以下为早期对话摘要]…`` — indistinguishable from a
    message the user typed, under a prompt that said "compress this
    conversation". The model therefore summarised the summary like anything
    else, and every later pass rewrote it with nothing asking it to carry
    anything forward.

    Only one thing can be our checkpoint: the note ``_run_l4`` returns at the
    head of the compacted history.  Position and marker are not enough — the
    marker is a public string and a user's first message can carry it verbatim —
    so the shape is decided by ``checkpoint_body``, the same definition the
    summariser accepts against.  Three private copies of "is this a checkpoint"
    would drift; two readers of one function cannot.

    Pure, total, and a no-op on a clean history — which is what keeps the first
    compaction bit-identical.
    """
    if not messages:
        return "", list(messages)
    head = messages[0]
    body = head.content or ""
    if head.role != "user" or not body.startswith(CHECKPOINT_MARK):
        return "", list(messages)
    verdict = checkpoint_body(body[len(CHECKPOINT_MARK) :])
    if verdict is None:
        return "", list(messages)
    return verdict, list(messages[1:])


def _ensure_pinned(
    messages: list[ChatMessage], pinned: Sequence[ChatMessage]
) -> list[ChatMessage]:
    """Keep the live user instruction through every stage.

    L1/L4/hard-trim cut by position; the instruction can be older than the tail
    window (hotel T15: index 80 fell out of a 20-message tail) and the model
    then re-anchors on whatever survived.  A missing pin is re-appended at the
    end of the history — closest to the tail blocks, where recency helps.
    """
    for pin in pinned:
        if any(
            m is pin or (m.role == pin.role and m.content == pin.content)
            for m in messages
        ):
            continue
        messages = [*messages, pin]
    return messages


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
        context_window: int | None = None,
    ) -> None:
        # ``context_window`` is the resolved number (config or model metadata).
        # It wins over the raw setting because resolving it once at the
        # composition root is what keeps the policy and the meter from
        # disagreeing about how big the window is.
        self._max_context_tokens = int(
            context_window or getattr(ai_settings, "max_context_tokens", 0) or 48_000
        )
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
        # P3-2 refactor: the L4 tail is token-budgeted, not counted.  Six
        # messages can be six one-liners or six 30KB tool dumps — a count
        # bounds neither cost nor coverage.  The budget is capped at a
        # quarter of the window so the tail alone can never approach the
        # post-compaction paging target (half the window).
        self._l4_tail_tokens = int(getattr(ai_settings, "l4_tail_tokens", 15_000))
        self._compress_target_ratio = float(
            getattr(ai_settings, "compress_target_ratio", 0.5)
        )
        self._root = Path(transcripts_root)
        self._summarizer = summarizer
        self._l4_failures = 0
        #: The last L4 summary call's own usage (prompt/completion/total), for
        #: the compaction funnel's Ended event.  Never fed to the meter.
        self.last_summary_usage: dict[str, int] | None = None
        #: Whether the last pipeline run actually attempted an L4 summary.
        #: The funnel reads it to tell "nothing worth summarising" (skip,
        #: muted) from "the summary call failed" (failure, red).
        self.last_summary_attempted = False
        # L3 triggers at (roughly) half the context budget, mirroring the Java rule.
        self._max_result_bytes = max(1, self._max_context_tokens // 2)

    async def compress(
        self,
        messages: list[ChatMessage],
        *,
        on_summary: Callable[[str], None] | None = None,
        on_delta: Callable[[str], None] | None = None,
        pinned: Sequence[ChatMessage] = (),
        force: bool = False,
    ) -> list[ChatMessage]:
        """Run the four-layer pipeline once (M3 semantics, unchanged)."""
        # Per-call bookkeeping resets here, the call boundary: a run that
        # never reaches the summariser must not report the previous call's
        # usage or attempt as its own (the funnel reads both).
        self.last_summary_usage = None
        self.last_summary_attempted = False
        out = list(messages)  # copy — never mutate the caller's history
        out = persist_large_outputs(
            out,
            root=self._root,
            max_result_bytes=self._max_result_bytes,
            per_result_bytes=self._per_result_bytes,
        )
        # What L4 must be shown is decided and captured BEFORE the snip, because
        # the snip is the stage that destroys the evidence. Measured on a real
        # session: `/compact` on 74 messages produced a bare
        # `[snipped 51 messages]` and asked the model nothing — and once the
        # order was "fixed" the summariser was still handed that marker, so the
        # model wrote a summary of a sentence saying something had been deleted.
        # A snip without a summary is deletion wearing a placeholder's clothes;
        # a summary of the placeholder is deletion wearing a summary's clothes.
        #
        # The reference (not the text) is captured here: the surviving tail is
        # only known after the snip, and the head text is the full list minus
        # exactly what survives — so a snipped-away message always reaches the
        # summariser, and a verbatim-kept one is never summarised twice.
        l4_source = out
        snipped = snip_middle(out, self._max_messages, self._keep_head, self._keep_tail)
        dropped = len(snipped) < len(out)
        if dropped:
            self._archive_dropped_history(out)
        out = snipped
        out = placeholder_old_results(
            out,
            self._keep_recent_results,
            window_chars=self._l2_window_chars,
            window_budget=self._l2_window_budget,
            elide_root=self._root,
        )
        _, surviving_tail = self.split_tail(out)
        l4_head = self._l4_head_text_excluding(l4_source, surviving_tail)

        should_summarise = (
            (force and not self._l4_since_empty(out))
            or self._l4_should_run(out)
            or dropped
        )
        if should_summarise and self._summarizer is not None:
            self.last_summary_attempted = True
            out = await self._run_l4(
                out, head_text=l4_head, on_summary=on_summary, on_delta=on_delta
            )

        if _history_tokens(out) > self._max_context_tokens:
            out = hard_trim(out, HARD_TRIM_KEEP_TAIL)

        out = _ensure_pinned(out, pinned)
        return sanitize_pairs(out)

    # ---- R3b: the compaction policy -----------------------------------

    def should_compact(
        self, messages: list[ChatMessage], observed_tokens: int = 0
    ) -> bool:
        """True once the context reaches the ceiling.

        ``estimate_tokens`` counts characters. Measured live, it read ~60k for a
        history the provider billed at 237,157 — the gap is tool output (JSON,
        code, base64) where four-characters-per-token is simply wrong. A gate that
        only reads the estimate therefore cannot open until the request has
        already failed, which is what it did.

        So the gate takes the provider's own count when there is one, and takes
        the larger of the two. A first session has no usage yet, and 0 keeps the
        pre-existing estimate-only behaviour there.
        """
        ceiling = compaction_ceiling(self._max_context_tokens)
        observed = max(int(observed_tokens or 0), _history_tokens(messages))
        return observed >= ceiling

    async def compress_if_needed(
        self,
        messages: list[ChatMessage],
        *,
        force: bool = False,
        on_summary: Callable[[str], None] | None = None,
        on_delta: Callable[[str], None] | None = None,
        pinned: Sequence[ChatMessage] = (),
        observed_tokens: int = 0,
    ) -> list[ChatMessage]:
        """Compact **only** when the budget says so (or the user asked).

        Below the soft water mark the history is returned untouched — that is what
        keeps the prompt-cache prefix stable (measured: 92-93% hit rate with
        append-only turns, 26% when the middle was rewritten every turn).
        """
        if not force and not self.should_compact(messages, observed_tokens):
            return messages
        out = await self.compress(
            messages, on_summary=on_summary, on_delta=on_delta, pinned=pinned, force=force
        )
        target = int(self._max_context_tokens * self._compress_target_ratio)
        guard = 0
        while (
            _history_tokens(out) > target
            and len(out) > _MIN_PAGE_MESSAGES
            and guard < 4
        ):
            out = hard_trim(out, max(_MIN_PAGE_MESSAGES, len(out) // 2))
            guard += 1
        out = _ensure_pinned(out, pinned)
        return out

    # ---- L4 -----------------------------------------------------------

    def _l4_should_run(self, messages: list[ChatMessage]) -> bool:
        if self._l4_failures >= _L4_BREAKER_THRESHOLD:
            return False
        return estimate_tokens(_joined_history_text(messages)) > self._max_context_tokens

    def split_tail(
        self, messages: list[ChatMessage]
    ) -> tuple[list[ChatMessage], list[ChatMessage]]:
        """Split history into (head, tail) by token budget (OpenCode parity).

        The tail keeps the most recent messages that fit the budget, then
        backtracks to a user boundary so question-answer pairs stay whole,
        and never comes back empty (a tail of zero would leave the next
        request with a summary of nothing recent).  A count cannot do any
        of this: six messages bound neither cost nor coverage.
        """
        budget = min(int(self._l4_tail_tokens), self._max_context_tokens // 4)
        if not messages:
            return [], []
        tail: list[ChatMessage] = []
        used = 0
        for message in reversed(messages):
            cost = estimate_tokens(message.content or "") + image_tokens([message])
            if tail and used + cost > budget:
                break
            tail.append(message)
            used += cost
        tail.reverse()
        while len(tail) > 1 and (tail[0].role != "user" or tail[0].synthetic):
            tail.pop(0)
        head = messages[: len(messages) - len(tail)] if tail else list(messages)
        if not head and len(messages) > 1:
            # Everything fit the budget: keep only the latest exchange
            # verbatim and summarize the rest (OpenCode parity).  Without
            # this a small /compact installs a summary of an empty input
            # beside a full verbatim tail — cost with zero information.
            # r18/F3: anchor on a message the human wrote.  A trailing synthetic
            # message would otherwise start the verbatim tail, sending the
            # user's own instruction into the summary.
            latest = max(
                (
                    index
                    for index, m in enumerate(messages)
                    if m.role == "user" and not m.synthetic
                ),
                default=len(messages) - 1,
            )
            tail = messages[latest:]
            head = messages[:latest]
        return head, tail

    def _l4_since_empty(self, messages: list[ChatMessage]) -> bool:
        """True when there is nothing new for a forced summary to say.

        A second /compact with no new turns must not burn two model calls on
        an empty prompt: the head is then either empty (a single message has
        nothing to split off) or an old checkpoint with nothing since it.
        """
        head, _ = self.split_tail(messages)
        if not head:
            return True
        _, rest = split_checkpoint(head)
        return not _joined_history_text(rest).strip()

    def _l4_head_text(self, messages: list[ChatMessage]) -> str:
        """The text L4 is meant to summarise, with the checkpoint labelled apart.

        With no checkpoint this is exactly what it has always been — the joined
        transcript — so the first compaction does not move.
        """
        return self._l4_head_text_excluding(messages, self.split_tail(messages)[1])

    def _l4_head_text_excluding(
        self,
        full: list[ChatMessage],
        surviving: Sequence[ChatMessage],
    ) -> str:
        """The head text over ``full`` minus what survives verbatim.

        ``surviving`` is the tail the pipeline kept (same objects); everything
        else in ``full`` is what L4 must preserve, including messages a snip
        already dropped elsewhere.  Snip markers never qualify: they describe
        a deletion, and summarising one is how the model once wrote a summary
        of a sentence saying something had been deleted (M11-0).
        """
        kept = {id(message) for message in surviving}
        head = [
            message
            for message in full
            if id(message) not in kept and not _is_snip_placeholder(message)
        ]
        checkpoint, rest = split_checkpoint(head)
        body = _joined_history_text(rest)
        if not checkpoint:
            return body
        return (
            f"{CHECKPOINT_OPEN}\n{checkpoint}\n{CHECKPOINT_CLOSE}\n\n"
            f"{SINCE_OPEN}\n{body}\n{SINCE_CLOSE}"
        )

    async def _call_summarizer(
        self,
        head_text: str,
        *,
        on_delta: Callable[[str], None] | None = None,
    ) -> str | None:
        """Call the injected summarizer, tolerating its age.

        New fakes accept ``(text, previous, on_delta, usage_sink)``; old ones
        take a single ``text``.  Probing by ``TypeError`` keeps every existing
        caller working without a flag day (same pattern as
        ``Compactor._compress``'s ``inspect`` probe).  Usage from the streaming
        path is summed into :attr:`last_summary_usage` for the Ended event.
        """
        summarizer: Any = self._summarizer
        assert summarizer is not None
        usage_sink: list[dict[str, int]] = []
        result: Any = None
        try:
            result = summarizer(head_text, None, on_delta=on_delta, usage_sink=usage_sink)
            if inspect.isawaitable(result):
                result = await result
        except TypeError:
            try:
                result = summarizer(head_text, None)
                if inspect.isawaitable(result):
                    result = await result
            except TypeError:
                result = summarizer(head_text)
                if inspect.isawaitable(result):
                    result = await result
        total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        for entry in usage_sink:
            for key in total:
                value = entry.get(key, 0) if isinstance(entry, dict) else 0
                if isinstance(value, int):
                    total[key] += value
        self.last_summary_usage = total if any(total.values()) else None
        if result is not None and not isinstance(result, str):
            return None
        return result

    async def _run_l4(
        self,
        messages: list[ChatMessage],
        *,
        head_text: str | None = None,
        on_summary: Callable[[str], None] | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> list[ChatMessage]:
        _, tail = self.split_tail(messages)
        head_text = self._l4_head_text(messages) if head_text is None else head_text
        self._persist_audit_copy(head_text)
        summary: str | None
        try:
            summary = await self._call_summarizer(head_text, on_delta=on_delta)
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
        # The summary now describes the loss, so the deletion marker would
        # read as a second, separate loss beside it.  It stays whenever no
        # summary was installed (that path returns `messages` above) — a
        # deletion without a summary keeps its placeholder, per M11-0.
        tail = [message for message in tail if not _is_snip_placeholder(message)]
        return [note, *tail]

    def _archive_dropped_history(self, messages: list[ChatMessage]) -> None:
        """The messages a compaction is about to remove, kept where a human can read them.

        Compression answers "what does the model still see". It does not answer
        "what did we say", and the transcript pane goes on rendering the
        compacted history — so without this the only record of those messages is
        a content-hashed audit file nobody can locate. The file is dated so the
        newest is obvious on disk, and it lives outside the context: the model
        never reads it unless something asks it to.
        """
        text = _joined_history_text(messages)
        try:
            root = self._root / "transcripts" / "archive"
            root.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
            (root / f"{stamp}-{digest}.md").write_text(text, encoding="utf-8")
        except OSError:
            pass  # best-effort; an unwritable disk must not fail a turn

    def _persist_audit_copy(self, text: str) -> None:
        try:
            root = self._root / "transcripts"
            root.mkdir(parents=True, exist_ok=True)
            name = f"summary_{hashlib.sha1(text.encode('utf-8')).hexdigest()[:12]}.txt"
            (root / name).write_text(text, encoding="utf-8")
        except OSError:
            pass  # best-effort audit artifact; never breaks compression


__all__ = [
    "CHECKPOINT_MARK",
    "ELIDED_DIR",
    "ELIDED_NOTE",
    "PERSISTED_OUTPUT_PREFIX",
    "PERSISTED_MARKER",
    "PREVIOUS_RESULT",
    "HARD_TRIM_KEEP_TAIL",
    "compaction_ceiling",
    "elide_and_persist",
    "estimate_tokens",
    "previous_result_note",
    "snip_middle",
    "split_checkpoint",
    "placeholder_old_results",
    "persist_large_outputs",
    "hard_trim",
    "sanitize_pairs",
    "ContextManager",
]
