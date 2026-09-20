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


def build_summarizer(model_config: Any) -> Summarizer:
    """Return an async ``(history_text) -> summary|None`` closure for L4."""

    async def summarize(text: str) -> str | None:
        try:
            out = await _chat(
                model_config,
                (
                    "你是资深研发助手。请把一段很长的编程会话早期历史压缩成精炼的要点"
                    "（中文），保留：已做决策、关键文件路径、打开的问题。用短句分行，"
                    "去掉寒暄与重复。"
                ),
                f"请压缩以下对话历史：\n\n{text[:_MAX_SUMMARY_CHARS]}",
                max_tokens=800,
            )
            return out or None
        except Exception:
            return None  # best-effort; never break the compression pipeline

    return summarize


def build_extractor(model_config: Any) -> Extractor:
    """Return an async ``(messages, conclusion) -> list[str]`` closure for memory."""

    async def extract(messages: Sequence[ChatMessage], conclusion: str | None) -> list[str]:
        try:
            text = _messages_to_text(messages, conclusion)
            if not text:
                return []
            out = await _chat(
                model_config,
                (
                    "你是记忆抽取器。从一段编码对话中提炼可复用的长期事实（用户的偏好、"
                    "项目约定、环境约束等）。每行一条记忆，纯文本，不要编号和破折号，"
                    "不要寒暄，不要写没有依据的推断。若没有新事实，输出空。"
                ),
                f"请从以下对话中抽取记忆：\n\n{text[:_MAX_EXTRACT_CHARS]}",
                max_tokens=300,
            )
            return _parse_memory_lines(out) if out else []
        except Exception:
            return []  # best-effort; never raise

    return extract


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


def _parse_memory_lines(text: str) -> list[str]:
    """Split LLM output into one memory note per non-empty line, stripped."""
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip().lstrip("-•*").strip()
        if len(line) < 2:
            continue
        out.append(line)
    return out


__all__ = [
    "build_summarizer",
    "build_extractor",
    "_chat",
    "_parse_memory_lines",
    "_messages_to_text",
]
