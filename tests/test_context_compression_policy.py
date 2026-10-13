"""TDD (R3b): when to compact — budget, not message count.

Measured on the M3 pipeline (docs/r3-analysis.md 1.15): with the defaults the first
L1 trim happened at turn 26 while only **5.2%** of the token budget was used, so the
middle of the history was rewritten every turn and prompt-cache hits collapsed to
26%.  ``compress_if_needed`` skips compaction entirely until the soft water mark is
reached, compacts once, and drives the result down to the target water mark so the
next turns are append-only again.

``ContextManager.compress`` keeps its exact M3 semantics (its own tests are
unchanged) — this policy layer sits on top of it.
"""

import pytest

from ai_coding.core.context_compression import (
    ContextManager,
    _joined_history_text,
    estimate_tokens,
)
from ai_coding.domain.message import ChatMessage


def _cfg(**overrides):
    base = {
        "max_context_tokens": 10_000,
        "max_messages": 400,
        "snip_keep_head": 1,
        "snip_keep_tail": 3,
        "keep_recent_tool_results": 3,
        "per_result_persist_bytes": 30_000,
        "compress_target_ratio": 0.5,
    }
    base.update(overrides)
    return type("C", (), base)()


def _msgs(count: int, chars: int = 200) -> list[ChatMessage]:
    out: list[ChatMessage] = []
    for index in range(count):
        role = "user" if index % 2 == 0 else "assistant"
        out.append(ChatMessage(role=role, content=f"{index}:" + "x" * chars, timestamp=""))
    return out


@pytest.mark.asyncio
async def test_under_budget_is_left_completely_alone(tmp_path):
    manager = ContextManager(_cfg(max_messages=4), transcripts_root=tmp_path)
    history = _msgs(12)
    out = await manager.compress_if_needed(history)
    assert out is history  # not even a copy: the request stays append-only


@pytest.mark.asyncio
async def test_over_budget_compacts_once(tmp_path):
    manager = ContextManager(
        _cfg(max_context_tokens=400, max_messages=4), transcripts_root=tmp_path
    )
    history = _msgs(12)
    out = await manager.compress_if_needed(history)
    assert out is not history
    assert len(out) < len(history)


@pytest.mark.asyncio
async def test_compaction_reaches_the_target_water_mark(tmp_path):
    manager = ContextManager(
        _cfg(max_context_tokens=600, max_messages=8), transcripts_root=tmp_path
    )
    history = _msgs(20)
    out = await manager.compress_if_needed(history)
    target = int(600 * 0.5)
    assert estimate_tokens(_joined_history_text(out)) <= target


@pytest.mark.asyncio
async def test_force_compacts_below_the_budget(tmp_path):
    manager = ContextManager(_cfg(max_messages=4), transcripts_root=tmp_path)
    history = _msgs(12)
    out = await manager.compress_if_needed(history, force=True)
    assert len(out) < len(history)


@pytest.mark.asyncio
async def test_should_compact_follows_the_ceiling(tmp_path):
    manager = ContextManager(_cfg(max_context_tokens=400), transcripts_root=tmp_path)
    assert manager.should_compact(_msgs(2)) is False
    assert manager.should_compact(_msgs(40)) is True


@pytest.mark.asyncio
async def test_l4_summary_is_reported_to_the_caller(tmp_path):
    seen: list[str] = []

    async def summarizer(text: str) -> str:
        return "早期摘要：决定了用 uv"

    manager = ContextManager(
        _cfg(max_context_tokens=120, max_messages=400),
        transcripts_root=tmp_path,
        summarizer=summarizer,
    )
    out = await manager.compress_if_needed(_msgs(10), on_summary=seen.append)
    assert seen == ["早期摘要：决定了用 uv"]
    assert any("早期摘要" in (message.content or "") for message in out)


@pytest.mark.asyncio
async def test_no_summary_callback_when_l4_does_not_run(tmp_path):
    seen: list[str] = []
    manager = ContextManager(_cfg(max_messages=4), transcripts_root=tmp_path)
    await manager.compress_if_needed(_msgs(12), on_summary=seen.append)
    assert seen == []
