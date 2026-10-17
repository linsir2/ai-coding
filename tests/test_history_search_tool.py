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

from pathlib import Path
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
from ai_coding.tools import history_search_tool as history_search_module
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


# ---- D-6: the second corpus -- what /compact replaced ----------------------
#
# ``transcripts/archive/`` is workspace-shared (ContextManager has no session
# concept), so an archive hit is *not* "this conversation": every hit says which
# file it came from. Output wording is English like every other tool's.

_CANARY = "HOKKA-7734"


def _archive(workspace, stem: str, body: str) -> None:
    root = Path(workspace) / "transcripts" / "archive"
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{stem}.md").write_text(body, encoding="utf-8")


def _ws_ctx(messages: list, workspace) -> ToolContext:
    from ai_coding.domain.task import TaskState

    return ToolContext(task=TaskState(), session_messages=tuple(messages), workspace=str(workspace))


@pytest.mark.asyncio
async def test_it_finds_a_phrase_that_only_the_archive_holds(tmp_path) -> None:
    """The gap D-6 closes: /compact replaces session.messages with a summary, so
    before this the words were on disk and unreachable by the model. The body is
    two lines because that is what a real one looks like -- a reader that split
    on newlines would report half a sentence."""
    _archive(tmp_path, "20261017-081203-a1b2c3d4", f"[user] 部署口令是 {_CANARY}\n第二行也要在")

    result = await HistorySearchTool().execute(
        {"query": "HOKKA"}, _ws_ctx([_message("assistant", "done")], tmp_path)
    )

    assert result.success is True
    assert _CANARY in result.output
    assert "第二行也要在" in result.output, "the whole record comes back, not its first line"


@pytest.mark.asyncio
async def test_the_hit_line_says_which_file_it_came_from(tmp_path) -> None:
    _archive(tmp_path, "20261017-081203-a1b2c3d4", f"[user] 口令 {_CANARY}")

    output = (
        await HistorySearchTool().execute({"query": "HOKKA"}, _ws_ctx([], tmp_path))
    ).output

    assert any(line.startswith("20261017-081203-a1b2c3d4") for line in output.splitlines()), output


@pytest.mark.asyncio
async def test_a_session_hit_is_still_labelled_as_this_session(tmp_path) -> None:
    """Source is not decoration: an archive belongs to the workspace and may
    come from another conversation, so a hit that does not say which is worse
    than no hit."""
    output = (
        await HistorySearchTool().execute(
            {"query": "8080"}, _ws_ctx([_message("user", "端口是 8080")], tmp_path)
        )
    ).output

    assert any(line.startswith("session") for line in output.splitlines()), output


@pytest.mark.asyncio
async def test_an_oversized_archive_is_skipped_and_says_so(tmp_path) -> None:
    """A megabyte of history in one file is not worth the read, and the model has
    to be told the difference between 'not there' and 'not looked at'."""
    _archive(tmp_path, "20261017-081203-a1b2c3d4", f"[user] {_CANARY}\n" + "x" * 1_100_000)
    live = [_message("user", "端口是 8080")]

    result = await HistorySearchTool().execute({"query": "HOKKA"}, _ws_ctx(live, tmp_path))

    assert _CANARY not in result.output
    assert "over size limit" in result.output, result.output


@pytest.mark.asyncio
async def test_the_newest_archives_win_the_slots(tmp_path) -> None:
    """Filenames lead with a zero-padded stamp, so sorting descending by name is
    sorting by time -- the newest conversation is the one being recalled."""
    for index in range(24):
        _archive(
            tmp_path, f"202610{index + 1:02d}-000000-{index:08x}", f"[user] 第{index}条无关内容"
        )
    _archive(tmp_path, "20261231-235959-deadbeef", f"[user] 最新里的 {_CANARY}")

    output = (
        await HistorySearchTool().execute({"query": "HOKKA"}, _ws_ctx([], tmp_path))
    ).output

    assert _CANARY in output


@pytest.mark.asyncio
async def test_the_scan_reports_how_many_files_it_skipped(tmp_path, monkeypatch) -> None:
    """Twenty files is the cap. Hitting it is fine; hiding it is not."""
    for index in range(23):
        _archive(tmp_path, f"20261017-0000{index:02d}-{index:08x}", f"[user] 第{index}条无关内容")

    reads: list[str] = []
    real = history_search_module.search_archived_text

    def spy(text, stamp, query, *, limit):
        reads.append(stamp)
        return real(text, stamp, query, limit=limit)

    monkeypatch.setattr(history_search_module, "search_archived_text", spy)

    live = [_message("user", "端口是 8080")]
    result = await HistorySearchTool().execute({"query": "HOKKA"}, _ws_ctx(live, tmp_path))

    assert len(reads) <= 20, f"read {len(reads)} files, the cap is 20"
    assert "23" in result.output, result.output


@pytest.mark.asyncio
async def test_session_hits_that_fill_the_limit_skip_the_archives(tmp_path) -> None:
    """If this conversation already answers the question, reading other people's
    history is cost for nothing -- but the model must be told that is why."""
    _archive(tmp_path, "20261017-081203-a1b2c3d4", f"[user] 归档里的 {_CANARY}")
    messages = [_message("user", f"8080 出现在第{n}条") for n in range(10)]

    result = await HistorySearchTool().execute(
        {"query": "8080", "limit": 3}, _ws_ctx(messages, tmp_path)
    )

    assert _CANARY not in result.output
    assert "archives not scanned" in result.output, result.output


@pytest.mark.asyncio
async def test_a_workspace_without_archives_behaves_exactly_as_before(tmp_path) -> None:
    """Most sessions have never been compacted by hand. Nothing may change for
    them, including the wording of a plain no-match."""
    result = await HistorySearchTool().execute(
        {"query": "HOKKA"}, _ws_ctx([_message("user", "端口是 8080")], tmp_path)
    )

    assert result.success is True
    assert result.output == "no match for 'HOKKA' in 1 messages of this session", (
        "no second corpus, no new words -- this is the output C-1 shipped"
    )
    assert result.data["matches"] == 0
