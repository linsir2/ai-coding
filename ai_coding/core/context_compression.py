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
from collections.abc import Awaitable, Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

from ai_coding.ai.llm_helpers import (
    CHECKPOINT_CLOSE,
    CHECKPOINT_OPEN,
    SINCE_CLOSE,
    SINCE_OPEN,
    checkpoint_body,
)
from ai_coding.core.events import LayerStat
from ai_coding.domain.message import CHECKPOINT_MARK, ChatMessage
from ai_coding.infra.text_clip import clip_head_tail

#: What one tool result keeps inside the summariser's input.  The result itself
#: already carries a persisted/elided path when it was trimmed, so the note stays
#: reachable; 2,000 characters is enough to see the shape of an error.
_SUMMARY_TOOL_CHARS = 2_000
#: How much rendered text one summary call may see.  Not a cap on what is
#: summarised -- ``summary_chunks`` splits anything longer into more calls.
_SUMMARY_CAP = 60_000

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
#: How much of the tail budget a user boundary must preserve to be worth
#: backtracking to.  Below this the tail keeps its token budget and starts
#: wherever the budget lands (with the tool-carrier repair).
_TAIL_BOUNDARY_FLOOR = 0.5


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


def _is_background_notice(message: ChatMessage) -> bool:
    """True for harness operations, not conversation (D-1 gap 5, oc parity:
    `compaction.ts:784` strips `metadata.background` from summary input).

    `[notice]` lines (background completions) are transient: the durable
    record is the job ledger and log on disk, recoverable through `job_list`.
    All three conditions are required -- `[steering]`, gate injections and
    stream continuations are synthetic too and stay in the summary, because
    they *are* what the turn did or was told, and a user-typed line that
    merely starts with the prefix is conversation, not operations.
    """
    return (
        message.role == "user"
        and bool(message.synthetic)
        and (message.content or "").startswith("[notice] ")
    )


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

    **This is a mid-turn guard, not a compression layer** (plan-m-e §A / M-E1).
    The ``while`` loop below takes only the *trailing run* of
    ``role == "tool"`` messages, so:

    * a turn that ended with an assistant reply has an empty batch and returns
      untouched — which is why ``persisted`` is almost never non-zero on the
      session file written at turn end (eval-04);
    * a large output that is followed by an assistant message is **not**
      persisted, by design: the moment it protects is "a tool just returned and
      the model has not answered yet", where a single 100KB body would otherwise
      make the next request a 400.

    The gate is ``total > max_result_bytes`` (half the context window) *and*
    ``single > per_result_bytes`` (30000).  The semantics are locked by a test
    (``test_l3_semantics_are_locked``) and made observable through
    ``LayerStat.skipped`` on ``CompactionEnded``, so this is a stated contract
    rather than an implicit one.
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


def _is_checkpoint_note(message: ChatMessage) -> bool:
    """Is this message the one that stands for everything before it?

    Same definition as :func:`split_checkpoint` — a bare prefix match would let
    a user message that merely *quotes* the marker be pinned by ``hard_trim``
    and cost a real message.
    """
    return bool(split_checkpoint([message])[0])


#: First line of the message that says "some history was dropped here".  A drop
#: the model cannot notice is a drop it will reason from as if nothing were
#: missing (same rule as the tool-output clip marker).
OMISSION_MARK = "[上下文超限]"


def hard_trim(
    messages: list[ChatMessage], keep_tail: int = HARD_TRIM_KEEP_TAIL
) -> list[ChatMessage]:
    """Blunt fallback: keep everything newer than the tail window, then sanitize.

    I2: the checkpoint note survives.  It is one message that stands for the
    whole head, so trimming it away removes the entire older conversation from
    the model's view while the tail alone is what is left -- the exact failure
    the note exists to prevent.
    """
    keep = messages[-keep_tail:] if keep_tail > 0 else list(messages)
    if keep and messages and keep[0] is not messages[0] and _is_checkpoint_note(messages[0]):
        keep = [messages[0], *keep]
    return sanitize_pairs(keep)


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
    """Text, tool-call arguments and the images — everything a request carries.

    ``tool_calls[].arguments`` used to be invisible here.  A ``write`` of a
    10 KB file is 2,500 tokens of request the estimate called zero, so the gate
    opened late and the overflow arrived before the gate did.
    """
    return (
        estimate_tokens(_joined_history_text(messages))
        + estimate_tokens(_tool_args_text(messages))
        + image_tokens(messages)
    )


def _tool_args_text(messages: Sequence[ChatMessage]) -> str:
    """The call arguments, which ``_joined_history_text`` deliberately omits."""
    parts: list[str] = []
    for message in messages:
        for call in getattr(message, "tool_calls", ()) or ():
            arguments = getattr(call, "arguments", "") or ""
            if arguments:
                parts.append(arguments)
    return "\n".join(parts)


def _render_message(message: ChatMessage) -> str:
    """One message as the summariser should read it.

    ``_joined_history_text`` writes ``[role] body``, which is right for a token
    estimate and wrong for a summary: it drops the tool name and arguments, so
    the model cannot tell "I ran the tests" from "I meant to".  That is exactly
    the material the ``## 已尝试且失败`` section is written from.
    """
    role = message.role or "user"
    body = (message.content or "").strip()
    calls = [
        f"{getattr(call, 'name', '?')}({_short_args(getattr(call, 'arguments', ''))})"
        for call in (getattr(message, "tool_calls", ()) or ())
    ]
    if role == "tool":
        name = getattr(message, "tool_name", "") or "tool"
        head = f"[tool:{name}]"
    elif calls:
        head = f"[{role}] → " + ", ".join(calls)
    else:
        head = f"[{role}]"
    if not body:
        return head
    if role == "tool":
        body = clip_head_tail(body, _SUMMARY_TOOL_CHARS)
    return f"{head} {body}"


def _short_args(arguments: Any, limit: int = 200) -> str:
    text = "" if arguments is None else str(arguments)
    return text if len(text) <= limit else text[:limit] + "…"


def render_for_summary(messages: Sequence[ChatMessage]) -> str:
    """The summariser's view of a run of messages: roles, calls, results."""
    lines = [
        _render_message(message)
        for message in messages
        if not _is_snip_placeholder(message) and not _is_background_notice(message)
    ]
    return "\n".join(line for line in lines if line)


def _split_text(text: str, cap: int) -> list[str]:
    """``text`` in pieces of at most ``cap``, at line boundaries where possible."""
    if len(text) <= cap:
        return [text]
    pieces: list[str] = []
    current: list[str] = []
    used = 0
    for line in text.splitlines():
        while len(line) > cap:  # one pathological line: slice it
            if current:
                pieces.append("\n".join(current))
                current, used = [], 0
            pieces.append(line[:cap])
            line = line[cap:]
        if current and used + len(line) + 1 > cap:
            pieces.append("\n".join(current))
            current, used = [], 0
        current.append(line)
        used += len(line) + 1
    if current:
        pieces.append("\n".join(current))
    return [piece for piece in pieces if piece]


def summary_chunks(messages: Sequence[ChatMessage], cap: int) -> list[str]:
    """Pack rendered messages into as many ``cap``-sized chunks as it takes.

    Every message lands in exactly one chunk.  The old path cut the *head* of
    the flattened transcript at 60,000 characters, so on a long session the
    newest work was always the part that vanished — the model then read its own
    checkpoint as "blocked: conversation truncated" and started over.
    """
    chunks: list[str] = []
    current: list[str] = []
    used = 0
    for message in messages:
        if _is_snip_placeholder(message) or _is_background_notice(message):
            continue
        for piece in _split_text(_render_message(message), cap):
            if current and used + len(piece) + 1 > cap:
                chunks.append("\n".join(current))
                current, used = [], 0
            current.append(piece)
            used += len(piece) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks or [""]


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


#: The head of one archived record -- exactly what :func:`_joined_history_text`
#: writes, which is why this lives beside it rather than beside its reader.
_ARCHIVE_HEAD = re.compile(r"^\[(user|assistant|tool|system)\] ?(.*)$")


def iter_archived_records(text: str) -> Iterator[tuple[str, str]]:
    """Read back what :meth:`ContextManager.archive_transcript` wrote, as
    ``(role, body)`` in the order it was written.

    The inverse of :func:`_joined_history_text`, and lossy in the one place the
    format is lossy: nothing is escaped, so a body line that itself starts with
    ``[user] `` opens a new record. Bodies may span lines -- the writer joins
    them with newlines -- so any line that is not a head continues the record
    above it.
    """
    role: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        head = _ARCHIVE_HEAD.match(line)
        if head:
            if role is not None:
                yield role, "\n".join(body).strip()
            role, first = head.group(1), head.group(2)
            body = [first]
        elif role is not None:
            body.append(line)
    if role is not None:
        yield role, "\n".join(body).strip()


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


def _checkpoint_note(summary: str) -> ChatMessage:
    """The one message that stands for everything the checkpoint covers."""
    return ChatMessage(
        role="user", content=f"{CHECKPOINT_MARK}\n{summary}", timestamp=""
    )


def _anchor_of(messages: Sequence[ChatMessage]) -> str:
    """A stable name for "this exact run of messages".

    ``ChatMessage.id`` is empty for every message the loop creates (only the
    server's note objects carry one), so comparing ids compared ``"" == ""``
    and the anchor never invalidated anything.  A content fingerprint over the
    *whole covered prefix* is stable across save/reload and changes exactly when
    the history the checkpoint describes is no longer the history in front of
    it: an undo, a rewind, an edited turn, or ``/compact`` rewriting the list.

    Fingerprinting one boundary message was not enough — an edit anywhere
    inside the covered prefix left the checkpoint describing text that no
    longer exists.
    """
    digest = hashlib.sha1()
    for message in messages:
        digest.update(f"{message.role}\0{message.content or ''}\0".encode())
        for call in getattr(message, "tool_calls", ()) or ():
            digest.update(
                f"{getattr(call, 'name', '')}\0{getattr(call, 'arguments', '')}\0".encode()
            )
    return digest.hexdigest()[:16]


def read_checkpoint(session: Any, messages: Sequence[ChatMessage]) -> tuple[str, int]:
    """The checkpoint this history already has, and how many messages it covers.

    Two sources, in order:

    1. the session's own fields — written by this module after every install, and
       the only source that survives the fact that ``session.messages`` is never
       rewritten by the budget path;
    2. a checkpoint note at ``messages[0]`` — what ``/compact`` and the
       provider-overflow rescue leave behind, since both replace
       ``session.messages`` with ``[note, *tail]`` (``compaction.py:362``).

    Without (2) a ``/compact`` would invalidate the checkpoint it just wrote and
    the next request would re-summarise the whole history from scratch.

    ``_anchor_of(messages[:used])`` is the anchor: undo, rewind and any external
    rewrite change it, and a checkpoint that no longer describes the history in
    front of it must not be carried forward.
    """
    previous = str(getattr(session, "compaction_checkpoint", "") or "")
    used = int(getattr(session, "compaction_consumed", 0) or 0)
    anchor = str(getattr(session, "compaction_anchor", "") or "")
    if (
        previous
        and anchor
        and 0 < used <= len(messages)
        and _anchor_of(messages[:used]) == anchor
    ):
        return previous, used
    head, _ = split_checkpoint(messages[:1])
    if head:
        return head, 1
    return "", 0


def install_checkpoint(
    session: Any, summary: str, *, consumed: int, anchor: str
) -> None:
    """Record that ``summary`` now covers ``messages[:consumed]``."""
    if session is None or consumed <= 0:
        return
    session.compaction_checkpoint = summary
    session.compaction_consumed = int(consumed)
    session.compaction_anchor = str(anchor)


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


def _trailing_tool_batch(messages: list[ChatMessage]) -> list[ChatMessage]:
    """The trailing run of ``role == "tool"`` messages — L3's whole input.

    Extracted so the observable ``LayerStat`` and the stage itself cannot
    disagree about what L3 examined.
    """
    end = len(messages)
    start = end
    while start > 0 and messages[start - 1].role == "tool":
        start -= 1
    return messages[start:end]


def _older_tool_count(messages: list[ChatMessage], keep_recent: int) -> int:
    """How many tool results L2 may look at (everything older than the recent tail)."""
    tool_count = sum(1 for m in messages if m.role == "tool")
    return max(0, tool_count - int(keep_recent))


def _rewritten_count(
    before: list[ChatMessage], after: list[ChatMessage]
) -> int:
    """How many bodies were replaced in place (the message count is untouched).

    L2 and L3 both rewrite a body and keep the message, so this is their
    ``rewritten``; comparing content (not identity) is what makes a no-op —
    the stage handing back the caller's own list — count as zero.
    """
    return sum(
        1
        for a, b in zip(before, after, strict=False)
        if (a.content or "") != (b.content or "")
    )


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
        # R3: the window is writable (``/model`` repoints it at the active
        # model), so the L3 threshold is derived by the setter below rather
        # than recomputed here -- otherwise a switch to a smaller model would
        # keep persisting at the old model's half.
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
        #: I2: how much *new* material earns a summary call.  Below it the delta
        #: rides into the request verbatim (branch C), which is what keeps a
        #: long turn from paying for one summary per iteration -- measured at
        #: 68.8% of the whole turn's prompt tokens before this existed.
        self._summary_delta_tokens = int(
            getattr(ai_settings, "summary_delta_tokens", 4_000) or 4_000
        )
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
        #: The per-layer account of the last ``compress`` run, at the
        #: pipeline boundary (L4's exit when it ran, else L2's).  ``None``
        #: before the first call — a run that never ran must not report the
        #: previous call's layers, exactly like ``last_summary_usage``.
        #: ``Compactor._compress_once`` reads it to fill
        #: ``CompactionEnded.layers`` (plan-m-e §C.1).
        self.last_layer_stats: list[LayerStat] | None = None
        # L3 triggers at (roughly) half the context budget, mirroring the Java
        # rule.  Derived by the ``_max_context_tokens`` setter above, so a
        # switch to a smaller model moves the threshold with the window.

    @property
    def _max_context_tokens(self) -> int:
        """The window the pipeline is anchored on; writable so ``/model`` can
        repoint it at the active model (R-3).

        The backing field is ``_context_tokens``: the name stays the contract
        the meter and the pipeline read, and the setter re-derives the L3
        threshold, which is half the window.  ``_max_result_bytes`` used to be
        computed once in ``__init__``, so a smaller target model left the old
        model's threshold in force and could overrun the new window.
        """
        return self._context_tokens

    @_max_context_tokens.setter
    def _max_context_tokens(self, value: int) -> None:
        self._context_tokens = int(value)
        self._max_result_bytes = max(1, self._context_tokens // 2)

    @property
    def summarizer(self) -> Callable[[str], Awaitable[str | None]] | None:
        """The L4 summarizer; writable so ``/model`` can repoint it (H3).

        The field is read in two places (``_call_summarizer`` and the L4 gate),
        so without this property assigning ``context.summarizer = ...`` would
        create an attribute nobody reads -- a silent no-op.
        """
        return self._summarizer

    @summarizer.setter
    def summarizer(
        self, value: Callable[[str], Awaitable[str | None]] | None
    ) -> None:
        self._summarizer = value

    async def compress(
        self,
        messages: list[ChatMessage],
        *,
        on_summary: Callable[[str], None] | None = None,
        on_delta: Callable[[str], None] | None = None,
        pinned: Sequence[ChatMessage] = (),
        force: bool = False,
        session: Any | None = None,
        page: bool = True,
    ) -> list[ChatMessage]:
        """Run the four-layer pipeline once.

        ``page=False`` leaves the final window trim to the caller, so
        ``compress_if_needed`` can own both the trim and its single
        ``[上下文超限]`` declaration instead of announcing twice.

        I2 changes *what L4 is shown*, not the layer order (L3→L1→L2→L4 is
        still the invariant M-E2 pinned).  Two things are new:

        * when the history already has a checkpoint, only the messages **after
          it** go to the summariser, and the checkpoint is passed as
          ``previous`` so the call is an update rather than a rewrite;
        * when that delta is small, no model call happens at all and the delta
          rides into the request verbatim — the alternative (summarising every
          iteration) is what burned 68.8% of eval-08's T2 prompt tokens.
        """
        # Per-call bookkeeping resets here, the call boundary: a run that
        # never reaches the summariser must not report the previous call's
        # usage or attempt as its own (the funnel reads both).
        self.last_summary_usage = None
        self.last_summary_attempted = False
        self.last_layer_stats = None
        stats: list[LayerStat] = []
        out = list(messages)  # copy — never mutate the caller's history
        # L3: the mid-turn guard.  The stat turns its semantics observable —
        # "the trailing run was K messages, we persisted R and skipped the
        # rest" — instead of leaving them in a comment (plan-m-e §A/M-E1).
        l3_before = _history_tokens(out)
        l3_in = out
        out = persist_large_outputs(
            out,
            root=self._root,
            max_result_bytes=self._max_result_bytes,
            per_result_bytes=self._per_result_bytes,
        )
        l3_rewritten = _rewritten_count(l3_in, out)
        l3_batch = _trailing_tool_batch(l3_in)
        stats.append(
            LayerStat(
                name="L3",
                removed=0,
                rewritten=l3_rewritten,
                skipped=len(l3_batch) - l3_rewritten,
                before_tokens=l3_before,
                after_tokens=_history_tokens(out),
            )
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
        previous, used = read_checkpoint(session, messages)

        if previous:
            # ---- the checkpoint path (I2) --------------------------------
            # The head is already accounted for: the note replaces it.  So L1
            # has nothing left to do (the note bounds the head) and the tail is
            # cut from ``l4_source`` directly -- a true suffix, which is what
            # makes ``delta`` exact.  Slicing the *post-L1* list by length is
            # how a boundary message used to fall between the two.
            head, tail = self.split_tail(l4_source)
            if (not head or len(head) < used) and 0 < used <= len(l4_source):
                # ``split_tail`` either handed the whole history back (everything
                # fitted the budget) or cut before the checkpoint's boundary.
                # Either way the checkpoint already stands for
                # ``messages[:used]``; sending it again pays twice and keeps the
                # checkpoint from ever advancing.
                head, tail = l4_source[:used], l4_source[used:]
            stats.append(
                LayerStat(
                    name="L1",
                    removed=0,
                    rewritten=0,
                    skipped=len(head),
                    before_tokens=_history_tokens(l4_source),
                    after_tokens=_history_tokens(tail),
                )
            )
            delta = head[used:] if used <= len(head) else []
            # L2 is deliberately skipped here.  ``tail`` is what the pipeline
            # chose to keep verbatim inside a budget of ``window // 4``; eliding
            # its bodies can only remove information the model is about to need,
            # and it is what turned a 12k tail into 1-3k of envelope text
            # (eval-08 T2: the model re-read ``agent_loop.py`` four times).
            stats.append(
                LayerStat(
                    name="L2",
                    removed=0,
                    rewritten=0,
                    skipped=_older_tool_count(tail, self._keep_recent_results),
                    before_tokens=_history_tokens(tail),
                    after_tokens=_history_tokens(tail),
                )
            )
            l4_before = _history_tokens(delta)
            breaker_open = self._l4_failure_count(session) >= _L4_BREAKER_THRESHOLD
            wants_summary = bool(delta) and (
                force
                or (
                    not breaker_open
                    and _history_tokens(delta) >= self._summary_delta_tokens
                )
            )
            if wants_summary and self._summarizer is not None:
                self.last_summary_attempted = True
                fresh = await self._run_l4(
                    delta,
                    previous=previous,
                    on_summary=on_summary,
                    on_delta=on_delta,
                    session=session,
                )
                if fresh:
                    # D9: the messages the note now stands for are still
                    # readable -- ``history_search`` promises as much.
                    self.archive_transcript(delta)
                    install_checkpoint(
                        session,
                        fresh,
                        consumed=len(head),
                        anchor=_anchor_of(messages[: len(head)]),
                    )
                    out = [_checkpoint_note(fresh), *tail]
                else:
                    # Branch D: the summary failed, so the checkpoint must not
                    # advance -- but the previous one still describes
                    # ``messages[:used]``, and dropping it here would take the
                    # whole head with it (the tail alone is not the history).
                    out = [_checkpoint_note(previous), *delta, *tail]
            else:
                # Branch C: the delta is small enough to carry verbatim.  This
                # is the branch that stops "summarise on every iteration".
                out = [_checkpoint_note(previous), *delta, *tail]
            stats.append(
                LayerStat(
                    name="L4",
                    removed=max(0, len(l4_source) - len(out)),
                    rewritten=0,
                    skipped=0 if wants_summary else 1,
                    before_tokens=l4_before,
                    after_tokens=_history_tokens(out),
                )
            )
        else:
            # ---- the first compaction (unchanged shape) ------------------
            # What L4 must be shown is decided and captured BEFORE the snip,
            # because the snip is the stage that destroys the evidence.  A snip
            # without a summary is deletion wearing a placeholder's clothes.
            l1_before = _history_tokens(out)
            l1_count = len(out)
            snipped = snip_middle(
                out, self._max_messages, self._keep_head, self._keep_tail
            )
            dropped = len(snipped) < len(out)
            if dropped:
                self.archive_transcript(out)
            out = snipped
            stats.append(
                LayerStat(
                    name="L1",
                    removed=max(0, l1_count - len(out)),
                    rewritten=0,
                    skipped=0,
                    before_tokens=l1_before,
                    after_tokens=_history_tokens(out),
                )
            )
            # The split is taken here, after the snip and **before** L2: the
            # snip keeps the original objects, so ``surviving_tail`` is still a
            # true suffix of ``l4_source`` and both identity operations work.
            # Taken after L2 it was not: L2 hands back new objects, so
            # ``_delta_by_identity`` could not exclude the tail (the summary
            # was handed the whole history again) and ``_cut_index`` matched
            # nothing and returned ``len(l4_source)`` -- a checkpoint claiming
            # to cover the verbatim tail, which then vanished from the next
            # request.  Measured on eval-08 T2: after-compaction context fell
            # to 323 tokens and the model re-read the same files four times.
            _, surviving_tail = self.split_tail(out)
            l4_head = self._delta_by_identity(l4_source, surviving_tail)
            cut = self._cut_index(l4_source, surviving_tail)
            should_summarise = (
                (force and not self._l4_since_empty(out))
                or self._l4_should_run(out, session)
                or (dropped and self._l4_failure_count(session) < _L4_BREAKER_THRESHOLD)
            )
            l2_before = _history_tokens(out)
            tail_before = _history_tokens(surviving_tail)
            # L2 runs on the tail only.  The head is about to be replaced by the
            # summary, so eliding it would only degrade the summary; the tail is
            # what the model keeps verbatim, so eliding *that* is what turned a
            # 12k tail into 1-3k of envelope text.
            kept_tail = placeholder_old_results(
                surviving_tail,
                self._keep_recent_results,
                window_chars=self._l2_window_chars,
                window_budget=self._l2_window_budget,
                elide_root=self._root,
            )
            l2_rewritten = _rewritten_count(surviving_tail, kept_tail)
            attempted = bool(should_summarise and self._summarizer is not None and l4_head)
            fresh = None
            if attempted:
                self.last_summary_attempted = True
                l4_count = len(out)
                fresh = await self._run_l4(
                    l4_head,
                    on_summary=on_summary,
                    on_delta=on_delta,
                    session=session,
                )
            if fresh and cut > 0:
                self.archive_transcript(l4_head)
                install_checkpoint(
                    session,
                    fresh,
                    consumed=cut,
                    anchor=_anchor_of(messages[:cut]),
                )
                out = [_checkpoint_note(fresh), *kept_tail]
                l2_after = l2_before - (tail_before - _history_tokens(kept_tail))
            else:
                # No checkpoint was installed, so nothing may be dropped -- and
                # the request still has to fit.  This is the one path where L2
                # covers the whole history: it shrinks bodies (archived,
                # windowed) without removing a message.
                l2_in = out
                out = placeholder_old_results(
                    out,
                    self._keep_recent_results,
                    window_chars=self._l2_window_chars,
                    window_budget=self._l2_window_budget,
                    elide_root=self._root,
                )
                l2_rewritten = _rewritten_count(l2_in, out)
                l2_after = _history_tokens(out)
            stats.append(
                LayerStat(
                    name="L2",
                    removed=0,
                    rewritten=l2_rewritten,
                    skipped=max(
                        0,
                        _older_tool_count(surviving_tail, self._keep_recent_results)
                        - l2_rewritten,
                    ),
                    before_tokens=l2_before,
                    after_tokens=l2_after,
                )
            )
            if attempted:
                stats.append(
                    LayerStat(
                        name="L4",
                        removed=max(0, l4_count - len(out)),
                        rewritten=0,
                        skipped=0 if fresh else 1,
                        before_tokens=l2_after,
                        after_tokens=_history_tokens(out),
                    )
                )
        self.last_layer_stats = stats

        if page and _history_tokens(out) > self._max_context_tokens:
            out = self._announce_omissions(out, hard_trim(out, HARD_TRIM_KEEP_TAIL))

        out = _ensure_pinned(out, pinned)
        return sanitize_pairs(out)

    @staticmethod
    def _delta_by_identity(
        source: Sequence[ChatMessage], tail: Sequence[ChatMessage]
    ) -> list[ChatMessage]:
        """``source`` minus what survives verbatim, decided by object identity.

        Not by length: ``snip_middle`` inserts a placeholder and
        ``placeholder_old_results`` replaces messages with new objects, so
        ``len(source) - len(tail)`` can land one message short and that message
        then appears in neither the summary nor the request.
        """
        kept = {id(message) for message in tail}
        return [message for message in source if id(message) not in kept]

    @staticmethod
    def _cut_index(
        source: Sequence[ChatMessage], tail: Sequence[ChatMessage]
    ) -> int:
        """The index in ``source`` where the verbatim ``tail`` begins.

        The counterpart of :meth:`_delta_by_identity`, and identity-based for
        the same reason: a snip placeholder inside ``tail`` is not in
        ``source``, so subtracting lengths lands one message short.
        """
        kept = {id(message) for message in tail}
        cut = len(source)
        while cut > 0 and id(source[cut - 1]) in kept:
            cut -= 1
        return cut

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
        session: Any | None = None,
    ) -> list[ChatMessage]:
        """Compact **only** when the budget says so (or the user asked).

        Below the soft water mark the history is returned untouched — that is what
        keeps the prompt-cache prefix stable (measured: 92-93% hit rate with
        append-only turns, 26% when the middle was rewritten every turn).
        """
        if not force and not self.should_compact(messages, observed_tokens):
            return messages
        out = await self.compress(
            messages,
            on_summary=on_summary,
            on_delta=on_delta,
            pinned=pinned,
            force=force,
            session=session,
            page=False,
        )
        target = int(self._max_context_tokens * self._compress_target_ratio)
        trimmed = out
        guard = 0
        while (
            _history_tokens(trimmed) > target
            and len(trimmed) > _MIN_PAGE_MESSAGES
            and guard < 4
        ):
            trimmed = hard_trim(trimmed, max(_MIN_PAGE_MESSAGES, len(trimmed) // 2))
            guard += 1
        out = self._announce_omissions(out, trimmed)
        out = _ensure_pinned(out, pinned)
        return out

    def _announce_omissions(
        self, before: list[ChatMessage], after: list[ChatMessage]
    ) -> list[ChatMessage]:
        """Say what paging dropped, and archive it.

        Paging only runs once the summary has already failed (or been skipped
        by the breaker) and the history is still over target, so the messages
        it removes are ones the checkpoint does *not* cover.  Dropping them is
        unavoidable at that point -- the request has to fit -- but doing it
        silently is not: the model would go on reasoning as if it had read
        them.  The originals are archived first, so ``history_search`` can
        still reach them.

        Compared by *content*, not by identity: ``hard_trim`` ends in
        ``sanitize_pairs``, which rebuilds every assistant message that carries
        tool calls.  An identity diff would count those survivors as dropped,
        inflate the number the model reads, and archive them a second time.
        """
        pool: dict[tuple[str, str], int] = {}
        for message in after:
            key = (message.role, message.content or "")
            pool[key] = pool.get(key, 0) + 1
        dropped: list[ChatMessage] = []
        for message in before:
            key = (message.role, message.content or "")
            if pool.get(key, 0) > 0:
                pool[key] -= 1
                continue
            dropped.append(message)
        if not dropped:
            return after
        self.archive_transcript(dropped)
        notice = ChatMessage(
            role="user",
            content=(
                f"{OMISSION_MARK} 为放进上下文窗口，已省略 {len(dropped)} 条较早消息；"
                "原文已归档，需要时用 history_search 取回。"
            ),
            timestamp="",
            synthetic=True,
        )
        if after and _is_checkpoint_note(after[0]):
            return [after[0], notice, *after[1:]]
        return [notice, *after]

    # ---- L4 -----------------------------------------------------------

    def _l4_should_run(
        self, messages: list[ChatMessage], session: Any | None = None
    ) -> bool:
        """Is this history past the point where a summary is owed?

        Compared against the *compaction ceiling*, not the raw window: the gate
        that opens compaction is ``should_compact``, which uses the ceiling, so
        measuring against the window meant the gate could open while L4 still
        refused to run — the request then went out with a bare
        ``[snipped N messages]`` and no summary at all.
        """
        if self._l4_failure_count(session) >= _L4_BREAKER_THRESHOLD:
            return False
        return _history_tokens(messages) >= compaction_ceiling(self._max_context_tokens)

    def _l4_failure_count(self, session: Any | None) -> int:
        """Consecutive L4 failures — per session when one is known.

        The counter used to be an attribute of the shared ``ContextManager``, so
        three failures in one session disabled summarisation for every session
        in the process (the composition root builds one manager).
        """
        if session is None:
            return self._l4_failures
        return int(getattr(session, "compaction_failures", 0) or 0)

    def _note_l4_failure(self, session: Any | None, *, failed: bool) -> None:
        if session is None:
            self._l4_failures = self._l4_failures + 1 if failed else 0
            return
        session.compaction_failures = (
            int(getattr(session, "compaction_failures", 0) or 0) + 1 if failed else 0
        )

    def _message_cost(self, message: ChatMessage) -> int:
        """What one message costs the request: body, tool arguments, images.

        The tail budget used to count the body alone, so a turn of ``write``
        calls (10 KB of arguments each, no body) fitted "for free" and blew the
        budget the moment it was sent.
        """
        return (
            estimate_tokens(message.content or "")
            + estimate_tokens(_tool_args_text([message]))
            + image_tokens([message])
        )

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
            cost = self._message_cost(message)
            if tail and used + cost > budget:
                break
            tail.append(message)
            used += cost
        tail.reverse()
        start = len(messages) - len(tail)
        # Prefer a user boundary so question-answer pairs stay whole -- but only
        # while it does not throw the budget away.  An agent turn is *one* user
        # message followed by dozens of assistant/tool steps, so the old
        # unconditional backtrack popped all the way down to a single message:
        # measured on eval-08 T2, a 48k window with a 12k tail budget left the
        # model 323-2,600 tokens after every compaction, which is why it kept
        # re-reading the same files.  A boundary that keeps less than half the
        # budget is not worth its cost.
        candidate = next(
            (
                index
                for index, message in enumerate(tail)
                if message.role == "user" and not message.synthetic
            ),
            None,
        )
        if candidate is not None and candidate > 0:
            discarded = sum(self._message_cost(message) for message in tail[:candidate])
            if used - discarded >= budget * _TAIL_BOUNDARY_FLOOR:
                start += candidate
                tail = tail[candidate:]
        # A tail that starts on a tool result is orphaned: the assistant message
        # that declared the call sits in the head, so ``sanitize_pairs`` deletes
        # the result -- the newest thing the model learned would reach neither
        # the request nor the summary.  Walk back to the carrier.  This is the
        # common shape at the end of a tool-heavy turn, not a corner case.
        while tail and tail[0].role == "tool" and start > 0:
            start -= 1
            tail.insert(0, messages[start])
        head = messages[:start]
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

    async def _call_summarizer(
        self,
        head_text: str,
        *,
        previous: str | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> str | None:
        """Call the injected summarizer, tolerating its age.

        New fakes accept ``(text, previous, on_delta, usage_sink)``; old ones
        take a single ``text``.  Probing by ``TypeError`` keeps every existing
        caller working without a flag day (same pattern as
        ``Compactor._compress``'s ``inspect`` probe).  Usage from the streaming
        path is summed into :attr:`last_summary_usage` for the Ended event.

        ``previous`` is forwarded as data now, not just as a mode switch: it is
        what makes the call an *update*.  It used to be hard-wired to ``None``
        here, so ``_MERGE_TOKENS`` and the growth bound were unreachable.
        """
        summarizer: Any = self._summarizer
        assert summarizer is not None
        usage_sink: list[dict[str, int]] = []
        result: Any = None
        try:
            result = summarizer(
                head_text, previous, on_delta=on_delta, usage_sink=usage_sink
            )
            if inspect.isawaitable(result):
                result = await result
        except TypeError:
            try:
                result = summarizer(head_text, previous)
                if inspect.isawaitable(result):
                    result = await result
            except TypeError:
                # A summariser that predates the two-argument form cannot be
                # told about the checkpoint out of band, so the labels go into
                # the text it does receive.  Without this the previous verdict
                # silently vanished on every old fake (and any injected
                # summariser written before I2).
                wrapped = head_text
                if previous:
                    wrapped = (
                        f"{CHECKPOINT_OPEN}\n{previous}\n{CHECKPOINT_CLOSE}\n\n"
                        f"{SINCE_OPEN}\n{head_text}\n{SINCE_CLOSE}"
                    )
                result = summarizer(wrapped)
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
        previous: str = "",
        head_text: str | None = None,
        on_summary: Callable[[str], None] | None = None,
        on_delta: Callable[[str], None] | None = None,
        session: Any | None = None,
    ) -> str | None:
        """Summarise ``messages`` (the delta) on top of ``previous``.

        Returns the summary body, or ``None`` when the call failed or the reply
        was unusable — the caller decides what to do, and every caller's answer
        is "install nothing, drop nothing".

        The input is **chunked**, never clipped.  A long delta used to be cut to
        its first 60,000 characters, which on a long session is the *oldest*
        part: the newest work never reached the summariser, so the checkpoint
        froze at the opening of the conversation (eval-08 T2: two summaries 21
        iterations apart, both "blocked: conversation truncated").

        Chunks are merged in order, each one an *update* of the last, so the
        first chunk carries ``previous`` and the last carries the newest facts.
        """
        if head_text is not None:  # an explicit text (old callers, tests)
            chunks = [head_text]
        else:
            chunks = summary_chunks(messages, _SUMMARY_CAP)
        if not chunks or not any(chunk.strip() for chunk in chunks):
            return None
        self._persist_audit_copy("\n\n".join(chunks))
        summary = previous or None
        try:
            for chunk in chunks:
                if not chunk.strip():
                    continue
                summary = await self._call_summarizer(
                    chunk, previous=summary, on_delta=on_delta
                )
                if not summary:
                    self._note_l4_failure(session, failed=True)
                    return None
            self._note_l4_failure(session, failed=False)
        except Exception:
            self._note_l4_failure(session, failed=True)
            return None
        if not summary:
            return None
        if on_summary is not None:
            try:
                on_summary(summary)
            except Exception:  # noqa: BLE001 — a caller callback must not break us
                pass
        return summary

    def archive_transcript(self, messages: list[ChatMessage]) -> None:
        """The messages a compaction is about to remove, kept where a human -- and
        ``history_search`` -- can read them.

        Compression answers "what does the model still see". It does not answer
        "what did we say", and the transcript pane goes on rendering the
        compacted history — so without this the only record of those messages
        is a content-hashed audit file nobody can locate. The file is dated so
        the newest is obvious on disk, and it lives outside the context: the
        model never reads it unless something asks it to.

        Public because ``/compact`` has the same need from the other side. The
        budget path drops messages from the *model's view* and leaves the
        session file whole; ``compact_now`` replaces ``session.messages`` with a
        summary, and this is the last moment the original words exist anywhere.
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
    "LayerStat",
    "PERSISTED_OUTPUT_PREFIX",
    "PERSISTED_MARKER",
    "PREVIOUS_RESULT",
    "HARD_TRIM_KEEP_TAIL",
    "compaction_ceiling",
    "elide_and_persist",
    "estimate_tokens",
    "iter_archived_records",
    "previous_result_note",
    "snip_middle",
    "split_checkpoint",
    "placeholder_old_results",
    "persist_large_outputs",
    "hard_trim",
    "sanitize_pairs",
    "ContextManager",
]
