"""L0 (microcompact) and the post-compaction file refresh.

Two mechanisms borrowed from harnesses that have run much longer than ours:

* **L0 / microcompact** — Claude Code's ``microcompact`` and opencode's ``prune``
  both clear old tool output with no model call.  We had neither, so before any
  layer looked at it a tool-heavy exploration reached 355,107 tokens: L3 only
  examines the trailing batch by design, and L2 only the tail/delta.
* **file refresh** — Claude Code re-injects the recently-read files after a
  compaction.  A checkpoint carries *references* ("read agent_loop.py 201-400"),
  not bytes, which is why the model re-read 48 times in one turn.

Both are bounded and both are content-only: no message is removed, so pairing
and the append-only prefix the prompt cache depends on are untouched.
"""

import asyncio
from types import SimpleNamespace

from ai_coding.config.models import AIConfig
from ai_coding.core.context_compression import (
    ContextManager,
    _anchor_of,
    _history_tokens,
    estimate_tokens,
    install_checkpoint,
    microcompact_old_results,
    working_history_tokens,
)
from ai_coding.core.request_assembly import _observed_prompt_tokens
from ai_coding.domain.message import ChatMessage, ToolCallRef
from ai_coding.domain.session import SessionData

GOOD_SUMMARY = (
    "## 目标\n审计完成\n\n## 约束与要求\n无\n\n## 决策\n用 grep\n\n"
    "## 工作状态\n### 已完成\n读完了\n\n## 已尝试且失败\n无\n\n"
    "## 下一步\n写 CHANGES.md\n\n## 关键文件\nsrc/a.py\n\n## 重要上下文\n无"
)


def _tool_heavy(n: int, chars: int, tag: str = "t") -> list[ChatMessage]:
    out: list[ChatMessage] = []
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
                tool_name="read",
            )
        )
    return out


def _tokens(messages) -> int:
    return sum(estimate_tokens(m.content or "") for m in messages)


# ---- L0: microcompact -----------------------------------------------------


def test_microcompact_never_removes_a_message(tmp_path):
    """Content-only by design: the request must stay structurally valid and
    append-only, or ``sanitize_pairs`` starts rebuilding the prefix."""
    messages = _tool_heavy(20, 20_000)
    out = microcompact_old_results(
        messages, protect_tokens=20_000, window_budget=0, elide_root=tmp_path,
    )
    assert len(out) == len(messages)
    assert [m.role for m in out] == [m.role for m in messages]
    assert [m.tool_call_id for m in out] == [m.tool_call_id for m in messages]


def test_microcompact_clears_everything_outside_the_protected_window(tmp_path):
    messages = _tool_heavy(20, 20_000)
    out = microcompact_old_results(
        messages, protect_tokens=20_000, window_budget=0, elide_root=tmp_path,
    )
    elided = sum(1 for m in out if "chars elided" in (m.content or ""))
    assert elided >= 10, "the older half must be cleared"
    assert _tokens(out) < _tokens(messages) / 2


def test_microcompact_keeps_the_newest_results_verbatim(tmp_path):
    """The protected window is what the model is working from; clearing it
    would trade one re-read for another."""
    messages = _tool_heavy(20, 20_000)
    out = microcompact_old_results(
        messages, protect_tokens=20_000, window_budget=0, elide_root=tmp_path,
    )
    tails = [m for m in out if m.role == "tool"][-1]
    assert "chars elided" not in (tails.content or "")


def test_microcompact_keeps_the_four_thousand_char_window_when_asked(tmp_path):
    """The envelope keeps a head/tail window so an error message is still
    readable without a round trip to disk."""
    messages = _tool_heavy(6, 20_000)
    out = microcompact_old_results(
        messages, protect_tokens=1, window_chars=200, window_budget=4000,
        elide_root=tmp_path,
    )
    first = next(m for m in out if m.role == "tool")
    body = first.content or ""
    assert "chars elided" in body
    assert len(body) > 200, "the window must survive beside the envelope"
    assert "transcripts/elided" in body, "the envelope must name the artifact"


def test_microcompact_is_idempotent(tmp_path):
    messages = _tool_heavy(12, 20_000)
    once = microcompact_old_results(
        messages, protect_tokens=10_000, window_budget=0, elide_root=tmp_path,
    )
    twice = microcompact_old_results(
        once, protect_tokens=10_000, window_budget=0, elide_root=tmp_path,
    )
    assert [m.content for m in once] == [m.content for m in twice]


def test_microcompact_leaves_a_short_history_alone(tmp_path):
    messages = _tool_heavy(2, 100)
    assert microcompact_old_results(
        messages, protect_tokens=40_000, elide_root=tmp_path,
    ) == messages


def test_an_unwritable_disk_means_no_elision_at_all():
    """The fail-safe: an envelope with no artifact path is a deletion wearing a
    placeholder's clothes.  If the body cannot be written where the model can
    read it, the body stays — costing tokens is cheaper than costing the work.
    """
    messages = _tool_heavy(20, 20_000)
    out = microcompact_old_results(messages, protect_tokens=1, elide_root=None)
    assert [m.content for m in out] == [m.content for m in messages]
    assert not any("chars elided" in (m.content or "") for m in out)


# ---- L0 without a checkpoint: the per-request view -------------------------


def _real_session(messages) -> SessionData:
    session = SessionData(
        session_id="s1",
        title="",
        created_time="2026-01-01T00:00:00Z",
        last_access_time="",
    )
    session.messages = list(messages)
    return session


def _wide_manager(tmp_path) -> ContextManager:
    return ContextManager(
        AIConfig(**{"max_context_tokens": 131_072, "max_messages": 50}),
        transcripts_root=tmp_path,
        summarizer=_good,
    )


def test_a_checkpointless_view_is_elided_and_reproducible(tmp_path):
    """Measured 2026-11-06 with the current pipeline: a 44-message / 154k-token
    history went to 23k for the one request that paid for a pass, no checkpoint
    was installed (L4 saw 37k, under the ceiling), and the gate — reading that
    23k as the context — let the next request go out as the raw **161k**, past a
    131k window.  L0 has to apply to the request itself, not only to a
    compaction's copy, or the shrink lasts exactly one iteration."""
    manager = _wide_manager(tmp_path)
    messages = _tool_heavy(20, 20_000)
    session = _real_session(messages)

    first = manager.checkpoint_view(messages, session=session)
    second = manager.checkpoint_view(messages, session=session)

    assert first is not None, "L0 must not require a checkpoint"
    assert _history_tokens(first) < _history_tokens(messages) // 2
    assert [m.content for m in first] == [m.content for m in second]


def test_the_cold_start_measurement_uses_the_view_not_the_raw_tail(tmp_path):
    """``working_history_tokens`` counts the post-checkpoint tail before L0, so
    it over-reported a real request by 6x (313,587 against 50,626) and reopened
    compaction at the start of every turn.  The gate must measure the view it is
    about to send."""
    manager = _wide_manager(tmp_path)
    messages = _tool_heavy(30, 20_000)
    session = _real_session(messages)
    install_checkpoint(
        session, GOOD_SUMMARY, consumed=2, anchor=_anchor_of(messages[:2])
    )

    view = manager.checkpoint_view(messages, session=session)

    assert view is not None
    measured = _observed_prompt_tokens(session, view)
    assert measured == _history_tokens(view)
    assert measured * 2 < working_history_tokens(session), (
        "the L0 view is what the request carries; the raw tail is not"
    )


# ---- C: the post-compaction file refresh ----------------------------------

def _session(reads: list[tuple[str, str]]):
    marks = [
        SimpleNamespace(path=path, start=1, end=1, total=1, version="v", call_id=cid)
        for path, cid in reads
    ]
    return SimpleNamespace(
        effects=SimpleNamespace(reads=marks),
        compaction_checkpoint="",
        compaction_consumed=0,
        compaction_anchor="",
        live_context_tokens=0,
        context_measure_invalid=False,
    )


async def _good(text, previous=None, on_delta=None, usage_sink=None):
    return GOOD_SUMMARY


def _manager(tmp_path, files: dict[str, str] | None, summarizer=_good):
    def reader(path: str) -> str | None:
        return (files or {}).get(path)

    return ContextManager(
        AIConfig(**{"max_context_tokens": 3_000, "max_messages": 12}),
        transcripts_root=tmp_path,
        summarizer=summarizer,
        file_reader=reader,
    )


def test_a_compaction_reinjects_the_recently_read_files(tmp_path):
    manager = _manager(tmp_path, {"src/a.py": "ALPHA-CANARY\n" * 20})
    session = _session([("src/a.py", "c1")])

    out = asyncio.run(
        manager.compress(_tool_heavy(30, 2_000, "第一批"), session=session)
    )
    body = "\n".join(m.content or "" for m in out)

    assert "压缩后重新载入" in body
    assert "ALPHA-CANARY" in body


def test_the_refresh_is_capped(tmp_path):
    """A refresh must never undo the shrink that made it necessary."""
    files = {f"src/f{i}.py": f"FILE-{i}\n" * 400 for i in range(10)}
    manager = _manager(tmp_path, files)
    session = _session([(f"src/f{i}.py", f"c{i}") for i in range(10)])

    out = asyncio.run(
        manager.compress(_tool_heavy(30, 2_000, "第一批"), session=session)
    )
    body = "\n".join(m.content or "" for m in out)

    assert body.count("### src/f") <= 5
    assert estimate_tokens(body) < 12_000


def test_a_missing_file_is_not_reinjected(tmp_path):
    manager = _manager(tmp_path, {})  # reader returns None for everything
    session = _session([("src/gone.py", "c1")])

    out = asyncio.run(
        manager.compress(_tool_heavy(30, 2_000, "第一批"), session=session)
    )
    body = "\n".join(m.content or "" for m in out)
    assert "压缩后重新载入" not in body


def test_no_reader_means_no_refresh(tmp_path):
    """Every existing caller (and every test) predates this mechanism."""
    manager = ContextManager(
        AIConfig(**{"max_context_tokens": 3_000, "max_messages": 12}),
        transcripts_root=tmp_path,
        summarizer=_good,
    )
    session = _session([("src/a.py", "c1")])

    out = asyncio.run(
        manager.compress(_tool_heavy(30, 2_000, "第一批"), session=session)
    )
    body = "\n".join(m.content or "" for m in out)
    assert "压缩后重新载入" not in body


def test_an_oversized_file_is_truncated_with_a_notice(tmp_path):
    manager = _manager(tmp_path, {"src/huge.py": "Z" * 200_000})
    session = _session([("src/huge.py", "c1")])

    out = asyncio.run(
        manager.compress(_tool_heavy(30, 2_000, "第一批"), session=session)
    )
    body = "\n".join(m.content or "" for m in out)
    assert "文件过长" in body
    assert len(body) < 60_000
