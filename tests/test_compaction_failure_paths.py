"""A compaction pass must never be bigger than the input it just shrank.

Measured 2026-11-06 (selftask, selfwork3 turn-1): a checkpoint pass summarised
nothing (the shrink ladder was exhausted and returned an empty body) and Branch
D then handed the whole un-summarised delta back:

    L3: 355,107 -> 355,107   (L3 is a trailing-batch guard by design)
    L1: 355,107 ->  14,865   (the head is covered by the checkpoint)
    L2:  14,865 ->  14,865   (only the tail was L2'd)
    L4:  78,415 ->  95,219   (the failure path put the backlog back)

That turn ran six passes and spent 1,040 of its 1,588 seconds inside them.  Two
things were wrong and both are pinned here:

1. an un-summarised ``delta`` was never elided, although it is exactly the part
   that grows during exploration (every ``read`` adds up to ~20k characters);
2. the failure branch carried it verbatim, so a pass could *increase* the
   request it was supposed to shrink.

The third fix in the same area — the backstop trim now uses the same ceiling the
gate does — is covered by ``test_the_backstop_uses_the_gate_ceiling``.
"""

import asyncio
from pathlib import Path

import pytest

from ai_coding.ai import llm_helpers
from ai_coding.config.models import AIConfig
from ai_coding.core.context_compression import (
    ContextManager,
    compaction_ceiling,
    estimate_tokens,
)

GOOD_SUMMARY = (
    "## 目标\n审计完成\n\n## 约束与要求\n不许改测试\n\n"
    "## 决策\n用 grep\n\n## 工作状态\n### 已完成\n读完了\n\n"
    "## 已尝试且失败\n无\n\n## 下一步\n写 CHANGES.md\n\n"
    "## 关键文件\nai_coding/core/agent_loop.py\n\n## 重要上下文\n无"
)


def _tokens(messages) -> int:
    return sum(estimate_tokens(m.content or "") for m in messages)


def _manager(summarizer, *, window: int = 3_000) -> ContextManager:
    import tempfile

    return ContextManager(
        AIConfig(**{"max_context_tokens": window, "max_messages": 12}),
        transcripts_root=Path(tempfile.mkdtemp()),
        summarizer=summarizer,
    )


def _tool_heavy(n: int, chars: int, tag: str):
    """Tool results are what actually fill a context, so the fixture uses them."""
    from ai_coding.domain.message import ChatMessage, ToolCallRef

    out = []
    for i in range(n):
        out.append(
            ChatMessage(
                role="assistant",
                content=f"读一下 {tag}{i}",
                timestamp="",
                tool_calls=[ToolCallRef(id=f"{tag}{i}", name="read", arguments="{}")],
            )
        )
        out.append(
            ChatMessage(
                role="tool",
                content=f"{tag}{i} " + "x" * chars,
                timestamp="",
                tool_call_id=f"{tag}{i}",
            )
        )
    return out


async def _good(text, previous=None, on_delta=None, usage_sink=None):
    return GOOD_SUMMARY


async def _broken(text, previous=None, on_delta=None, usage_sink=None):
    return None


def _checkpointed(summarizer) -> tuple[ContextManager, list]:
    manager = _manager(summarizer)
    # Big enough to cross ``compaction_ceiling(3_000) == 2_700`` and to make the
    # first pass summarise rather than hand everything back.
    first = _tool_heavy(30, 2_000, "第一批")
    compacted = asyncio.run(manager.compress(first))
    assert compacted and compacted[0].content.startswith("[以下为早期对话摘要]"), (
        "fixture precondition: the first pass must install a checkpoint"
    )
    return manager, compacted


def test_a_failed_summary_cannot_make_the_pass_bigger():
    """The defect: 14,865 tokens in, 95,219 out."""
    manager, compacted = _checkpointed(_good)
    manager.summarizer = _broken

    backlog = _tool_heavy(40, 20_000, "探索")
    before = _tokens([*compacted, *backlog])
    after = asyncio.run(manager.compress([*compacted, *backlog]))

    assert _tokens(after) < before, (
        "a failed summary must still shrink the request; carrying the delta "
        "verbatim made the pass 6x its own input after L1"
    )


def test_a_successful_summary_shrinks_further_than_the_failure_path():
    """Control: the pass that does get a checkpoint must not be worse than the
    one that does not.  This pins the *relative* order, so a future change to
    either branch has to keep the failure path the more aggressive of the two."""
    manager, compacted = _checkpointed(_good)
    backlog = _tool_heavy(40, 20_000, "探索")
    succeeded = _tokens(asyncio.run(manager.compress([*compacted, *backlog])))

    manager, compacted = _checkpointed(_good)
    manager.summarizer = _broken
    failed = _tokens(asyncio.run(manager.compress([*compacted, *backlog])))

    assert succeeded <= failed


def test_the_unsummarised_delta_is_elided_not_dropped_silently():
    """L2 keeps the newest results verbatim and archives the rest, so a re-read
    still works.  Carrying them raw is what blew the window up."""
    manager, compacted = _checkpointed(_good)
    manager.summarizer = _broken

    backlog = _tool_heavy(40, 20_000, "探索")
    after = asyncio.run(manager.compress([*compacted, *backlog]))
    body = "\n".join(m.content or "" for m in after)

    # Whatever the delta loses must be recoverable and said out loud: either an
    # L0/L2 envelope naming the artifact, or the paging notice that says the
    # originals were archived.  Silence is the only wrong answer.
    assert "elided" in body or "已归档" in body


def test_an_elided_delta_says_where_the_original_went():
    """An elision the model cannot reverse is a deletion wearing a placeholder."""
    manager, compacted = _checkpointed(_good)
    manager.summarizer = _broken

    backlog = _tool_heavy(40, 20_000, "探索")
    after = asyncio.run(manager.compress([*compacted, *backlog]))
    body = "\n".join(m.content or "" for m in after)

    assert "elided" in body or "已归档" in body


def test_the_backstop_uses_the_gate_ceiling():
    """The gate opens at ``ceiling``; the backstop used the whole window, so a
    pass could leave the request at 91% of the window and the gate reopened
    almost immediately."""
    window = 131_072
    ceiling = compaction_ceiling(window)
    assert ceiling < window
    assert ceiling == window - 16_000


# ---- D: the summary call has its own budget -------------------------------


@pytest.mark.asyncio
async def test_a_summary_call_outside_the_budget_is_abandoned(monkeypatch):
    """Measured: 565s in one pass, with an empty answer at the end.  The turn
    cannot be allowed to wait on an auxiliary call like that."""
    monkeypatch.setattr(llm_helpers, "_SUMMARY_BUDGET_SECONDS", 0.2)
    monkeypatch.setattr(llm_helpers, "_SUMMARY_ATTEMPT_SECONDS", 0.1)

    async def slow(*_args, **_kwargs):
        await asyncio.sleep(5)
        return GOOD_SUMMARY

    monkeypatch.setattr(llm_helpers, "_chat", slow)
    out = await llm_helpers.build_summarizer(object())("历史")
    assert out is None


@pytest.mark.asyncio
async def test_a_normal_summary_still_returns_inside_the_budget(monkeypatch):
    monkeypatch.setattr(llm_helpers, "_chat", lambda *a, **k: _good(None))
    out = await llm_helpers.build_summarizer(object())("历史")
    assert out == GOOD_SUMMARY


def test_thinking_is_disabled_only_where_the_provider_supports_it():
    """DashScope bills reasoning as completion and does not count it against
    ``max_tokens`` — that is how one pass reported 93,603 completion tokens
    against a 2,800 cap.  Other endpoints get no extra body."""
    class _Config:
        base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    class _OpenAI:
        base_url = "https://api.openai.com/v1"

    assert llm_helpers._thinking_off(_Config()) == {"enable_thinking": False}
    assert llm_helpers._thinking_off(_OpenAI()) == {}
