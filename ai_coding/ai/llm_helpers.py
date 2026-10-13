"""Real-LLM helpers (M3 integration) — L4 summarizer + memory extractor.

These are the concrete injections for two pluggable seams that default to
``None`` (offline/no-op): :class:`ContextManager`'s ``summarizer`` and
:class:`MemoryService`'s ``extractor``.  Each is a small OpenAI-compatible
``chat/completions`` call built on the same ``ModelConfig`` the main loop uses,
so the flash/pro model already configured works here too.

Both helpers are deliberately best-effort: any network/parse failure degrades to
``None`` / ``[]`` so compression and memory never break (invariants §5-11).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from openai import AsyncOpenAI

from ai_coding.ai.openai_engine import _looks_like_context_overflow
from ai_coding.domain.message import ChatMessage
from ai_coding.memory.schema import parse_candidates

Summarizer = Any  # Callable[[str], Awaitable[str | None]]
Extractor = Any  # Callable[[Sequence[ChatMessage], str | None], Awaitable[list[str]]]

_MAX_SUMMARY_CHARS = 60_000
_MAX_EXTRACT_CHARS = 40_000
#: One seven-section set is roughly this many characters.  It is also the floor
#: under the growth bound: the template grew from four sections to seven, so a
#: first merge can legitimately more than double a short predecessor, and
#: rejecting that would drop the summary entirely (see docs/r11-analysis.md 12.2).
_MIN_SUMMARY_CHARS = 1_200
_CREATE_TOKENS = 1_200
_MERGE_TOKENS = 1_600
_TRUNCATION_NOTE = "\n[历史过长，此处截断]"

#: OpenCode's shrink ladder (compaction.ts:92): when the provider rejects a
#: request as too big, retry at 70%/50%/35% of the size actually sent, then give
#: up.  The anchor is the first *sent* size, not the raw history, so a history
#: that only overflowed after ``_clip_for_summary`` truncation still shrinks
#: from where it actually is.
_SHRINK_STEPS = (0.7, 0.5, 0.35)

#: The two halves of a checkpoint-aware summary input, and the labels that
#: separate them.  They live here rather than next to the compressor because
#: they are the prompt's wire format — ``core/context_compression.py`` imports
#: them the same way it already imports ``_chat``.
CHECKPOINT_OPEN = "<previous-checkpoint>"
CHECKPOINT_CLOSE = "</previous-checkpoint>"
SINCE_OPEN = "<conversation-since-checkpoint>"
SINCE_CLOSE = "</conversation-since-checkpoint>"


async def _chat(
    model_config: Any,
    system: str,
    user: str,
    max_tokens: int,
) -> str:
    """One-shot chat completion against the configured model. Raises on failure."""
    client = AsyncOpenAI(
        base_url=model_config.base_url,
        api_key=model_config.api_key,
        timeout=getattr(model_config, "timeout", 60),
        max_retries=0,
    )
    try:
        resp = await client.chat.completions.create(
            model=model_config.name,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens,
            temperature=0.2,
        )
    finally:
        await client.close()
    return (resp.choices[0].message.content or "").strip()


#: The sections an L4 summary must use.  They are the prompt's contract *and*
#: the shape check's vocabulary: if the prompt stops naming one, every summary
#: becomes a failure, and if the check accepts anything, a reply that says
#: "好的，我总结了一下" is installed as the record of what happened.
#:
#: Seven, matching OpenCode's ``SUMMARY_TEMPLATE``: with four there was nowhere
#: to put the user's constraints or the reasons behind a decision, so a merge
#: had to crush them into "目标" or drop them. Those are exactly the two kinds
#: of knowledge a checkpoint exists to carry forward.
SUMMARY_SECTIONS = (
    "## 目标",
    "## 约束与要求",
    "## 决策",
    "## 工作状态",
    "## 下一步",
    "## 关键文件",
    "## 重要上下文",
)

#: Under "## 工作状态".  Reported so the prompt can name them; not part of the
#: shape check, because ``## `` is a substring of ``### `` and a top-level
#: check would accept a subsection as the whole section.
_WORK_STATE_SUBHEADS = ("### 已完成", "### 进行中", "### 阻塞")

_INPUT_SHAPE = (
    "输入固定分成两部分，用 XML 标签隔开：\n"
    f"{CHECKPOINT_OPEN} —— 上一次压缩得出的结论。若该标签内为空，说明这是首次压缩，"
    "没有已有结论。\n"
    f"{SINCE_OPEN} —— 上一次压缩之后新发生的对话原文。\n"
)

_MERGE_RULES = (
    "\n若" + CHECKPOINT_OPEN + " 非空，本次是**更新**而不是重写：\n"
    "· 新内容优先，但默认保留旧结论里仍然有效的信息；\n"
    "· 仅当新内容明确推翻、取代、解决或使其失效时，才移除旧信息；\n"
    "· 只更新各小节的内容，不要新增、删除或改名字节；\n"
    "· 逐字保留文件路径、命令、标识符、错误串、URL。\n"
    "· 若" + CHECKPOINT_OPEN + " 为空，则直接总结" + SINCE_OPEN + "。\n"
)

_SUMMARY_SYSTEM_PROMPT = (
    "你是资深研发助手。你要把一段很长的编程会话压缩成一份 checkpoint，"
    "交给另一个 agent 接着干活。只输出下面的小节，不要前言、不要结语：\n"
    # The sub-headings have to sit immediately under their parent in the
    # template the model is shown.  Listed after all seven top-level sections,
    # a real run appended them to whichever section came last — they landed
    # under "重要上下文" instead of "工作状态".
    + "\n".join(SUMMARY_SECTIONS[:3])
    + "\n## 工作状态\n" + "\n".join(_WORK_STATE_SUBHEADS) + "\n"
    + "\n".join(SUMMARY_SECTIONS[4:]) + "\n"
    "每节用短句分行；没有内容的节写「无」。\n"
    + _INPUT_SHAPE
    + _MERGE_RULES
    + "\n不要描述你在做总结这件事，不要调用工具。"
)

#: Appended on the one retry, so the model knows what was wrong with the last
#: answer rather than being asked the same thing again.
_SUMMARY_NUDGE = (
    "\n\n注意：上一次回复没有使用要求的小节结构。请只输出以 "
    + " / ".join(SUMMARY_SECTIONS)
    + " 开头的正文，不要任何其它文字。"
)


def checkpoint_body(text: str) -> str | None:
    """The canonical checkpoint in ``text``, or ``None`` if there is not one.

    One definition of "what counts as a checkpoint", read by the two places that
    have to agree: the summariser that decides whether to accept a reply, and the
    compressor that decides whether a stored note is one.  They used to answer
    separately — a substring test and a prefix test — and disagreed, so a reply
    carrying a preamble was installed and then never found again, and R11's merge
    silently stopped working for that session.  Same shape as the meter and the
    gate both answering "which usage is still live" on their own (M11-0g).

    A heading matches when the whole trimmed line is one, which is what OpenCode
    does (``filled``, compaction.ts:269).  Substring matching cannot tell
    ``### 已完成`` from ``## 工作状态`` — ``## `` sits inside the former — and
    it accepts prose that merely mentions a heading.

    Returns from the first heading onward, so a preamble is dropped rather than
    rejected: it costs a paid retry to reject, and the retry usually does not
    help.  ``docs/r11-analysis.md`` §14.
    """
    lines = (text or "").strip().split("\n")
    for index, line in enumerate(lines):
        if line.strip() in SUMMARY_SECTIONS:
            return "\n".join(lines[index:]).strip()
    return None


def _clip_for_summary(text: str, cap: int = _MAX_SUMMARY_CHARS) -> str:
    """Trim an over-long history, and say that it was trimmed.

    The slice used to end in silence, so a third compaction could lose its tail
    and leave nothing to notice the loss by.  ``cap`` lets the shrink ladder
    (docs/r15-summary-shrink.md) reuse the same "say it was trimmed" logic at
    each lower budget instead of slicing again silently.
    """
    if len(text) <= cap:
        return text
    return text[:cap] + _TRUNCATION_NOTE


def summary_within_growth_bound(previous_chars: int, summary: str) -> bool:
    """True when a merged checkpoint has not run away from its predecessor.

    OpenCode needed a whole one-shot rewrite path (#48058) because an
    "update" prompt that preserves everything makes the checkpoint grow on
    every pass.  Here ``max_tokens`` already bounds it structurally, but a model
    that ignores the instruction can still return a transcript dump, and the
    floor is there so the seven-section template — fatter than the four-section
    one it replaced — is not mistaken for runaway growth on a first merge
    (docs/r11-analysis.md 12.2).

    No predecessor means nothing to have grown from, so nothing to check.
    """
    if previous_chars <= 0:
        return True
    return len(summary) <= max(previous_chars * 2, _MIN_SUMMARY_CHARS)


def build_summarizer(model_config: Any) -> Summarizer:
    """Return an async ``(history_text) -> summary|None`` closure for L4.

    Asks once, checks the shape, and asks once more before giving up (G5-3).
    When the provider rejects the request as *too big*, the same two asks
    repeat at each lower size of ``_SHRINK_STEPS`` before giving up
    (docs/r15-summary-shrink.md).  ``None`` means "no usable summary", which is
    what makes the pipeline fall back to the trimmed history -- a *better*
    outcome than installing a reply that ignored the format.
    """

    async def summarize(text: str, previous: str | None = None) -> str | None:
        max_tokens = _MERGE_TOKENS if previous else _CREATE_TOKENS
        base = _clip_for_summary(text)
        caps = [len(base)] + [max(1, int(len(base) * step)) for step in _SHRINK_STEPS]
        previous_len = len(previous or "")
        for cap in caps:
            prompt = (
                f"请{('更新' if previous else '压缩')}以下对话：\n"
                f"\n{_clip_for_summary(text, cap)}"
            )
            too_big = False
            for attempt in range(2):
                try:
                    out = await _chat(
                        model_config,
                        _SUMMARY_SYSTEM_PROMPT,
                        prompt if attempt == 0 else prompt + _SUMMARY_NUDGE,
                        max_tokens=max_tokens,
                    )
                except Exception as exc:
                    if _looks_like_context_overflow(exc):
                        too_big = True
                        break
                    return None  # not an overflow: shrinking the input cannot help
                body = checkpoint_body(out)
                if body is None:
                    continue
                if not summary_within_growth_bound(previous_len, body):
                    continue
                return body
            if not too_big:
                return None  # shape/growth failure: stay on the NUDGE axis, no shrink
        return None

    return summarize


def build_extractor(model_config: Any) -> Extractor:
    """Return the memory extractor: structured candidates, with a soft fallback.

    R3 changed the contract (docs/r3-analysis.md section 6.7): the answer should be
    a JSON array of ``{text, kind, importance, action, target_id}`` so a
    contradiction can *supersede* an existing note instead of piling up beside it.
    ``harvest`` also sends a ``<known-memory>`` block listing the ids of related
    notes, which is where ``target_id`` comes from.

    Not everything degrades politely: if the model answers in the old
    one-line-per-memory style, or wraps the JSON in prose, ``parse_candidates``
    accepts both; if it answers with nonsense, this returns ``[]`` rather than
    raising (memory is best-effort by invariant).
    """

    async def extract(
        messages: Sequence[ChatMessage], conclusion: str | None
    ) -> list[Any]:
        try:
            text = _messages_to_text(messages, conclusion)
            if not text:
                return []
            out = await _chat(
                model_config,
                _EXTRACT_SYSTEM_PROMPT,
                f"请从以下对话中抽取记忆：\n\n{text[:_MAX_EXTRACT_CHARS]}",
                max_tokens=400,
            )
            if not out:
                return []
            # One parse path.  There used to be a second fallback here that
            # split the answer into plain lines with no guard at all, so a
            # malformed answer became a memory twice over: `parse_candidates`
            # returned `[]`, then this re-read the same broken JSON.  A broken
            # answer is a failed harvest; `parse_candidates` already owns the
            # legacy line-per-note format, with the guard.
            return list(parse_candidates(out))
        except Exception:
            return []  # best-effort; never raise

    return extract


_EXTRACT_SYSTEM_PROMPT = (
    "你是记忆抽取器。从一段编码对话中提炼**可复用的长期记忆**：用户偏好、项目约定、"
    "已做的决策、环境约束。\n"
    "若对话中给出了 <known-memory> 已知记忆（带 id），当你要写的内容是**取代**或"
    "**更新**其中某一条时，必须输出 action=supersede/update 并给出它的 target_id；"
    "内容与已知记忆重复时输出 action=noop；否则 action=add。\n"
    "可加 scene 字段（不超过 8 个字的**话题标签**，用于把同一主题的记忆归组，"
    "例如「依赖管理」）；一条记忆只归一个组，能归组就填，归不了组就省略这个字段。"
    "如果对话里给了 <scene-groups>，那是这个记忆库已经在用的分组，能归入就原样用其中一个，"
    "不要为了这一条记忆再造一个近义的新标签。\n"
    "只输出一个 JSON 数组，不要寒暄、不要解释、不要代码块以外的文字。元素形如：\n"
    '[{"text": "项目用 uv 管理依赖", "kind": "decision", "importance": 5, '
    '"scene": "依赖管理", "action": "add"}]\n'
    'kind ∈ fact|decision|preference|open_thread，importance ∈ 1..5。'
    "没有可写入的新事实时输出 []。"
)


def _messages_to_text(
    messages: Sequence[ChatMessage],
    conclusion: str | None,
) -> str:
    parts: list[str] = []
    for m in messages:
        body = (m.content or "").strip()
        if body:
            parts.append(f"[{m.role}] {body}")
    if conclusion:
        parts.append(f"[conclusion] {conclusion}")
    return "\n".join(parts)


__all__ = [
    "SUMMARY_SECTIONS",
    "checkpoint_body",
    "summary_within_growth_bound",
    "build_summarizer",
    "build_extractor",
    "_chat",
    "_messages_to_text",
]
