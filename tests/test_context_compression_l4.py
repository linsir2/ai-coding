"""TDD (M3-2): ContextManager orchestration — L4 summary, circuit breaker, hard trim."""

from types import SimpleNamespace

import pytest

from ai_coding.core.context_compression import HARD_TRIM_KEEP_TAIL, ContextManager
from ai_coding.domain.message import ChatMessage, ToolCallRef


def _settings(**overrides) -> SimpleNamespace:
    base = {
        "max_context_tokens": 100,
        "max_messages": 20,
        "snip_keep_head": 2,
        "snip_keep_tail": 6,
        "keep_recent_tool_results": 2,
        "per_result_persist_bytes": 1000,
        "l4_keep_tail": 2,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _msgs(n: int) -> list[ChatMessage]:
    return [ChatMessage(role="user", content=f"m{i}", timestamp=f"t{i}") for i in range(n)]


def _user(content: str) -> ChatMessage:
    return ChatMessage(role="user", content=content, timestamp="t")


async def _none(**_: object) -> str | None:
    return None


class _Fake:
    def __init__(self, replies: list[str | None] | None = None) -> None:
        self.replies = replies or []
        self.calls: list[str] = []

    async def __call__(self, text: str) -> str | None:
        self.calls.append(text)
        if not self.replies:
            return "SUMMARY"
        return self.replies.pop(0)


@pytest.mark.asyncio
async def test_compress_returns_new_list_not_mutating_input(tmp_path):
    msgs = _msgs(12)
    snap = list(msgs)
    cm = ContextManager(_settings(), transcripts_root=tmp_path)
    out = await cm.compress(msgs)
    assert out is not msgs
    assert msgs == snap  # original untouched
    assert cm._l4_failures == 0


@pytest.mark.asyncio
async def test_l4_skips_when_below_threshold(tmp_path):
    fake = _Fake()
    cm = ContextManager(_settings(max_context_tokens=1_000_000), tmp_path, summarizer=fake)
    out = await cm.compress(_msgs(4))
    assert fake.calls == []  # summarizer not invoked under budget
    assert out == _msgs(4)


@pytest.mark.asyncio
async def test_l4_returns_summary_plus_tail(tmp_path):
    fake = _Fake(["digested"])
    cm = ContextManager(_settings(max_context_tokens=5), tmp_path, summarizer=fake)
    msgs = _msgs(8)
    original = list(msgs)
    out = await cm.compress(msgs)
    assert len(fake.calls) == 1  # called once
    # first message is the summary note; tail preserved
    assert "digested" in out[0].content
    assert len(out) == 1 + 2  # summary + l4_keep_tail(2)
    assert msgs == original  # input untouched


@pytest.mark.asyncio
async def test_l4_no_summarizer_skips_and_keeps_history(tmp_path):
    cm = ContextManager(_settings(max_context_tokens=5), tmp_path, summarizer=None)
    msg_list = _msgs(8)
    out = await cm.compress(msg_list)
    # no summarizer -> history degrades via hard-trim, never crashes
    assert isinstance(out, list)
    assert cm._l4_failures == 0


@pytest.mark.asyncio
async def test_l4_circuit_breaker_after_3_failures(tmp_path):
    async def failing(_: str) -> str | None:
        raise RuntimeError("boom")

    cm = ContextManager(_settings(max_context_tokens=5), tmp_path, summarizer=failing)
    # three failing turns trip the breaker
    for _ in range(3):
        await cm.compress(_msgs(8))
    assert cm._l4_failures >= 3
    # even a healthy summarizer is now skipped (broken)
    cm2_calls = _Fake()
    broken = ContextManager(
        _settings(max_context_tokens=5), tmp_path, summarizer=cm2_calls
    )
    broken._l4_failures = 3
    await broken.compress(_msgs(8))
    assert cm2_calls.calls == []


@pytest.mark.asyncio
async def test_l4_success_resets_counter(tmp_path):
    state = {"n": 0}

    async def flaky(text: str) -> str | None:
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("first fails")
        return "recovered summary"

    cm = ContextManager(_settings(max_context_tokens=5), tmp_path, summarizer=flaky)
    await cm.compress(_msgs(8))  # fails -> counter 1
    assert cm._l4_failures == 1
    await cm.compress(_msgs(8))  # succeeds -> reset 0
    assert cm._l4_failures == 0


@pytest.mark.asyncio
async def test_hard_trim_keeps_tail_and_drops_oldest(tmp_path):
    # snip disabled (max_messages high), summarizer absent -> hard trim alone shrinks
    cm = ContextManager(
        _settings(max_messages=50, max_context_tokens=2),
        tmp_path,
        summarizer=None,
    )
    out = await cm.compress(_msgs(30))
    assert len(out) == HARD_TRIM_KEEP_TAIL


@pytest.mark.asyncio
async def test_compress_sanitizes_orphan_pairs(tmp_path):
    cm = ContextManager(_settings(), tmp_path, summarizer=None)
    call = ToolCallRef(id="c1", name="bash", arguments="{}")
    orphan_on_call = ChatMessage(
        role="assistant", content="text", timestamp="t", tool_calls=[call]
    )
    out = await cm.compress([_user("hi"), orphan_on_call])
    # orphan call (no matching tool result) is stripped, message kept
    assert out[-1].content == "text"
    assert all((m.tool_calls or []) == [] for m in out if m.role == "assistant")


@pytest.mark.asyncio
async def test_l4_persists_audit_copy(tmp_path):
    fake = _Fake(["s"])
    cm = ContextManager(_settings(max_context_tokens=5), tmp_path, summarizer=fake)
    await cm.compress(_msgs(8))
    assert list((tmp_path / "transcripts").glob("summary_*.txt"))
