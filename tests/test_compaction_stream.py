"""TDD (P3-2 slice 2): summarizer streams deltas and reports its own usage.

Contract: old callers keep `await summarize(text, previous) -> str|None`.
New callers may pass `on_delta` (per-chunk sink) and `usage_sink` (list the
call appends one usage dict to).  No existing test is touched.
"""

from __future__ import annotations

import pytest

import ai_coding.ai.llm_helpers as llm_helpers


def _ok_summary() -> str:
    # I2: the acceptance check requires the template's shape, so the "good"
    # fixture has to be a complete checkpoint.
    from ai_coding.ai.llm_helpers import SUMMARY_SECTIONS

    return "\n".join(f"{section}\n压缩完成" for section in SUMMARY_SECTIONS)


@pytest.mark.asyncio
async def test_summarizer_forwards_chunks_to_on_delta_and_records_usage(monkeypatch):
    full = _ok_summary()
    chunks = [full[:20], full[20:]]
    seen: list[str] = []

    async def fake_chat_stream(model_config, system, user, max_tokens, on_delta=None):
        for part in chunks:
            if on_delta is not None:
                on_delta(part)
        return "".join(chunks), {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    monkeypatch.setattr(llm_helpers, "_chat_stream", fake_chat_stream)
    summarize = llm_helpers.build_summarizer(object())
    usage_sink: list[dict] = []

    out = await summarize("内容" * 100, None, on_delta=seen.append, usage_sink=usage_sink)

    assert out == _ok_summary()
    assert seen == chunks
    assert usage_sink == [{"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}]


@pytest.mark.asyncio
async def test_summarizer_without_stream_args_keeps_old_contract(monkeypatch):
    async def fake_chat(model_config, system, user, max_tokens):
        return _ok_summary()

    monkeypatch.setattr(llm_helpers, "_chat", fake_chat)
    summarize = llm_helpers.build_summarizer(object())

    out = await summarize("内容" * 100, None)

    assert out == _ok_summary()


@pytest.mark.asyncio
async def test_summarizer_overflow_still_shrinks_on_the_stream_path(monkeypatch):
    import openai

    class _Resp:
        request = object()
        status_code = 400
        headers: dict = {}

    def _overflow() -> Exception:
        return openai.BadRequestError(
            "This model's maximum context length is 32768 tokens. "
            "However, you requested 42000 tokens",
            response=_Resp(),
            body=None,
        )

    calls: list[str] = []
    seen: list[str] = []

    async def fake_chat_stream(model_config, system, user, max_tokens, on_delta=None):
        calls.append(user)
        if len(calls) == 1:
            raise _overflow()
        if on_delta is not None:
            on_delta(_ok_summary())
        return _ok_summary(), {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}

    monkeypatch.setattr(llm_helpers, "_chat_stream", fake_chat_stream)
    summarize = llm_helpers.build_summarizer(object())

    out = await summarize("内容" * 30000, None, on_delta=seen.append, usage_sink=[])

    assert out == _ok_summary()
    assert len(calls) >= 2
    assert len(calls[1]) < len(calls[0])
    assert seen == [_ok_summary()]


# ---- ContextManager: on_delta passthrough + usage bookkeeping --------------


def _l4_settings(**overrides):
    from types import SimpleNamespace

    base = {
        "max_context_tokens": 5,
        "max_messages": 20,
        "snip_keep_head": 2,
        "snip_keep_tail": 6,
        "keep_recent_tool_results": 2,
        "per_result_persist_bytes": 1000,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _l4_msgs(n: int):
    from ai_coding.domain.message import ChatMessage

    return [ChatMessage(role="user", content=f"m{i}", timestamp=f"t{i}") for i in range(n)]


@pytest.mark.asyncio
async def test_context_manager_forwards_on_delta_to_summarizer(tmp_path):
    from ai_coding.core.context_compression import ContextManager

    seen: list[str] = []

    async def fake_summarizer(text, previous=None, on_delta=None, usage_sink=None):
        if on_delta is not None:
            on_delta("## 目标\n")
        if usage_sink is not None:
            usage_sink.append({"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10})
        return "## 目标\n压缩完成"

    cm = ContextManager(_l4_settings(), transcripts_root=tmp_path, summarizer=fake_summarizer)
    out = await cm.compress(_l4_msgs(8), on_delta=seen.append)

    assert seen == ["## 目标\n"]
    assert "压缩完成" in out[0].content


@pytest.mark.asyncio
async def test_context_manager_records_last_summary_usage(tmp_path):
    from ai_coding.core.context_compression import ContextManager

    async def fake_summarizer(text, previous=None, on_delta=None, usage_sink=None):
        if usage_sink is not None:
            usage_sink.append({"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10})
        return "## 目标\n压缩完成"

    cm = ContextManager(_l4_settings(), transcripts_root=tmp_path, summarizer=fake_summarizer)
    assert getattr(cm, "last_summary_usage", None) in (None, {})

    await cm.compress(_l4_msgs(8))

    usage = getattr(cm, "last_summary_usage", None)
    assert usage is not None
    assert usage["total_tokens"] == 10


@pytest.mark.asyncio
async def test_context_manager_still_works_with_a_single_arg_summarizer(tmp_path):
    """Old fakes take one arg; threading on_delta must not break them."""
    from ai_coding.core.context_compression import ContextManager

    async def old_fake(text):
        return "## 目标\n旧摘要"

    cm = ContextManager(_l4_settings(), transcripts_root=tmp_path, summarizer=old_fake)
    out = await cm.compress(_l4_msgs(8), on_delta=lambda s: None)

    assert "旧摘要" in out[0].content
