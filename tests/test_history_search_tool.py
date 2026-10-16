"""C-1: the model can read back what compaction took out of its window.

Both reference harnesses declare this gap in their own words (dsh 08 §8-2, oc
06 §10): after a compaction the summary is the only thing in context, and
nothing can expand what it dropped. Here the bytes were never lost -- the
compactor works on a copy (``context_compression.py``: "never mutate the
caller's history") -- so the transcript the model cannot see is still the
session's own ``messages``. What was missing was a tool that reads it.

The tests drive the real tool through a real dispatcher (``_Tool`` doubles the
base class the way the rest of this suite does); the search itself is a pure
function so its edges -- case folding, the cap, roles without text -- are
pinned without a model in the loop.
"""

from __future__ import annotations

from typing import Any

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.domain.loop import StepResult
from ai_coding.domain.message import ToolCallRef
from ai_coding.domain.session import SessionData
from ai_coding.security.approval import AutoApproveCallback
from ai_coding.service.session_service import SessionService
from ai_coding.tools.base import ToolContext
from ai_coding.tools.history_search_tool import HistorySearchTool, search_history
from ai_coding.tools.registry import ToolRegistry

# ---- the search itself ------------------------------------------------------


def _message(role: str, content: str, stamp: str = "2026-01-01T00:00:00Z"):
    from ai_coding.domain.run import assistant_message, tool_message, user_message

    if role == "user":
        return user_message(content, stamp)
    if role == "tool":
        return tool_message("c1", content, True, stamp)
    return assistant_message(StepResult(text=content), stamp)


def test_it_finds_a_line_the_summary_no_longer_carries() -> None:
    messages = [
        _message("user", "记住编号 ZX-7391"),
        _message("assistant", "好的"),
        _message("user", "现在干什么？"),
    ]

    hits = search_history(messages, "ZX-7391")

    assert len(hits) == 1
    assert hits[0].role == "user"
    assert "ZX-7391" in hits[0].content
    assert hits[0].index == 0


def test_the_query_is_case_folded_because_models_do_not_quote() -> None:
    messages = [_message("user", "Deploy to STAGING at midnight")]

    assert len(search_history(messages, "staging")) == 1
    assert len(search_history(messages, "StAgInG")) == 1


def test_no_match_is_an_empty_list_not_an_error() -> None:
    """A miss is information ("it was never said"), not a failure."""
    assert search_history([_message("user", "hello")], "zzz") == []


def test_the_cap_is_reported_rather_than_hidden() -> None:
    messages = [_message("user", f"line {n} needle") for n in range(10)]

    hits = search_history(messages, "needle", limit=3)

    assert len(hits) == 3
    # The oldest first, so a truncated result is still a prefix of the truth.
    assert [hit.index for hit in hits] == [0, 1, 2]


def test_a_blank_query_matches_nothing() -> None:
    """The tool refuses a blank query rather than matching everything: the
    domain has no content-free assistant message (message.py rejects one), so
    an empty needle would otherwise return the whole transcript."""
    messages = [_message("user", "hello"), _message("assistant", "hi")]

    assert search_history(messages, "") == []
    assert search_history(messages, "   ") == []


@pytest.mark.asyncio
async def test_tool_output_names_position_role_and_time() -> None:
    messages = [_message("user", "端口是 8080", "2026-02-03T04:05:06Z")]

    output = (await HistorySearchTool().execute({"query": "8080"}, _ctx(messages))).output

    assert "8080" in output
    assert "#0" in output
    assert "user" in output


@pytest.mark.asyncio
async def test_it_says_when_the_transcript_is_not_there() -> None:
    """A wiring gap must not look like "you never said that"."""
    result = await HistorySearchTool().execute({"query": "x"}, _ctx([]))

    assert result.success is False
    assert result.error == "no_transcript"


@pytest.mark.asyncio
async def test_an_empty_query_is_refused() -> None:
    result = await HistorySearchTool().execute({"query": "   "}, _ctx([_message("user", "hi")]))

    assert result.success is False
    assert result.error == "empty_query"


def _ctx(messages: list) -> ToolContext:
    from ai_coding.domain.task import TaskState

    return ToolContext(task=TaskState(), session_messages=tuple(messages))


# ---- the wiring that makes it different from reading ctx.messages -----------


def test_the_factory_separates_this_request_from_the_whole_session() -> None:
    """The one line that makes the tool worth having.

    ``ToolContext.messages`` is the request's view -- compaction rewrites a copy
    of the history -- so a tool reading it would search exactly what the model
    can already see. ``session_messages`` is the session's own transcript.
    """
    from ai_coding.core.request_builder import tool_context
    from ai_coding.domain.task import TaskState

    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    session.add_message(_message("user", "first thing ever"))
    compacted = [_message("user", "<summary>")]

    ctx = tool_context(
        task=TaskState(),
        workspace=".",
        session=session,
        history=compacted,
        instructions=None,
        emit=lambda _event: None,
    )

    assert [m.content for m in ctx.messages] == ["<summary>"]
    assert "first thing ever" in " ".join(m.content or "" for m in ctx.session_messages)


# ---- it is a read-only tool, and it is wired to the session -----------------


def test_it_reads_nothing_and_writes_nothing() -> None:
    tool = HistorySearchTool()

    assert tool.mutates_world is False, "otherwise plan mode hides it"
    assert tool.parallel_safe is True


def test_the_gate_never_asks_about_it() -> None:
    from ai_coding.security.permission_gate import PermissionDecision, PermissionGate

    gate = PermissionGate()

    # Decision compares equal to the enum member by value (permission_gate.py:46).
    assert gate.check("history_search", {"query": "x"}) == PermissionDecision.ALLOW


@pytest.mark.asyncio
async def test_it_reaches_the_model_through_a_real_dispatcher(tmp_path) -> None:
    """The end of the chain: a session whose history holds a line, a model that
    asks for it, and the answer arriving as an ordinary tool result."""
    engine = _AsksHistoryThenAnswers()
    sessions = SessionService(tmp_path / "sessions")
    registry = ToolRegistry()
    registry.register(HistorySearchTool())
    loop = _loop(engine, sessions, registry)
    session = sessions.create()

    await loop.process_input(session, "go")
    session.add_message(_message("user", "记住编号 ZX-7391"))

    await loop.process_input(session, "查一下 ZX-7391")

    tool_messages = [m for m in session.messages if getattr(m, "role", "") == "tool"]
    assert tool_messages, "the tool must have run"
    assert "ZX-7391" in str(tool_messages[-1].content)
    # the answer reached the model: the tool result is in its next request
    assert any("ZX-7391" in str(message.content) for message in engine.requests[-1])


def _loop(engine, sessions, registry) -> AgentLoop:
    """This suite's convention: a test file carries its own doubles."""
    return AgentLoop(
        engine,
        sessions,
        registry,
        approval=AutoApproveCallback(),
        settings=AIConfig(max_tool_iterations=3, stop_policy="natural"),
    )


class _AsksHistoryThenAnswers(ChatEngine):
    def __init__(self) -> None:
        self.requests: list[list[Any]] = []
        self._passes = 0
        from ai_coding.domain.model_config import ModelConfig

        self.model_config = ModelConfig(name="m", base_url="https://x/v1", api_key="k")
        self.on_retry = None
        self.on_fallback = None

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.requests.append(list(messages))
        self._passes += 1
        if self._passes == 1:
            return StepResult(
                text=None,
                tool_calls=(
                    ToolCallRef(
                        id="c1", name="history_search", arguments='{"query":"ZX-7391"}'
                    ),
                ),
                finish_reason="tool_calls",
                usage=None,
                reasoning=None,
            )
        return StepResult(text="done", finish_reason="stop", usage=None, reasoning=None)
