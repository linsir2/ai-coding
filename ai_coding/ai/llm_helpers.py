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

from ai_coding.domain.message import ChatMessage
from ai_coding.memory.schema import parse_candidates

Summarizer = Any  # Callable[[str], Awaitable[str | None]]
Extractor = Any  # Callable[[Sequence[ChatMessage], str | None], Awaitable[list[str]]]

_MAX_SUMMARY_CHARS = 60_000
_MAX_EXTRACT_CHARS = 40_000


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
SUMMARY_SECTIONS = ("## 目标", "## 已完成", "## 关键文件", "## 待办")

_SUMMARY_SYSTEM_PROMPT = (
    "你是资深研发助手。请把一段很长的编程会话早期历史压缩成精炼的要点（中文），"
    "去掉寒暄与重复。只输出下面四个小节，不要前言、不要结语：\n"
    + "\n".join(SUMMARY_SECTIONS)
    + "\n每节用短句分行；没有内容的节写「无」。"
)

#: Appended on the one retry, so the model knows what was wrong with the last
#: answer rather than being asked the same thing again.
_SUMMARY_NUDGE = (
    "\n\n注意：上一次回复没有使用要求的小节结构。请只输出以 "
    + " / ".join(SUMMARY_SECTIONS)
    + " 开头的正文，不要任何其它文字。"
)


def summary_is_usable(text: str) -> bool:
    """True when a model reply is shaped like a summary (G5-3).

    Deliberately coarse — at least one required section heading — because the
    cost of the two mistakes is asymmetric: accepting prose installs a useless
    summary that the agent then reasons from, while rejecting a good one only
    degrades to the trimmed history the pipeline already falls back on.  This is
    the same check the reference implementations make.
    """
    body = (text or "").strip()
    if not body:
        return False
    return any(section in body for section in SUMMARY_SECTIONS)


def build_summarizer(model_config: Any) -> Summarizer:
    """Return an async ``(history_text) -> summary|None`` closure for L4.

    Asks once, checks the shape, and asks once more before giving up (G5-3).
    ``None`` means "no usable summary", which is what makes the pipeline fall
    back to the trimmed history -- a *better* outcome than installing a reply
    that ignored the format.
    """

    async def summarize(text: str) -> str | None:
        prompt = f"请压缩以下对话历史：\n\n{text[:_MAX_SUMMARY_CHARS]}"
        for attempt in range(2):
            try:
                out = await _chat(
                    model_config,
                    _SUMMARY_SYSTEM_PROMPT,
                    prompt if attempt == 0 else prompt + _SUMMARY_NUDGE,
                    max_tokens=800,
                )
            except Exception:
                return None  # best-effort; never break the compression pipeline
            if summary_is_usable(out):
                return out
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
    "例如「依赖管理」）；能明确归组就填，不确定就省略这个字段。\n"
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
    "summary_is_usable",
    "build_summarizer",
    "build_extractor",
    "_chat",
    "_messages_to_text",
]
