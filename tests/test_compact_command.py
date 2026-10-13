"""TDD (R3b): compaction is budget-driven, plus an explicit user command.

Docs/r3-analysis.md 1.15: the M3 pipeline rewrote the middle of the history every
turn from turn 26 while 5.2% of the budget was used.  Now a turn compacts only when
``compress_if_needed`` says so, and ``/compact`` lets the user ask for one on
demand — which is also the only case where the *stored* history is replaced.
"""

from pathlib import Path

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.app.commands import handle_input
from ai_coding.app.slash import parse_slash
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.context_compression import ContextManager
from ai_coding.domain.loop import StepResult
from ai_coding.service.session_service import SessionService
from ai_coding.tools.registry import ToolRegistry


class _Engine(ChatEngine):
    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        return StepResult(text="好的", finish_reason="stop")


class _Console:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def info(self, message: str) -> None:
        self.lines.append(message)

    def error(self, message: str) -> None:
        self.lines.append(f"ERROR {message}")

    def delta(self, token: str) -> None:
        pass

    def rule(self, title: str = "") -> None:
        pass


def _settings(**overrides) -> AIConfig:
    base = {"stop_policy": "natural", "max_messages": 4, "max_context_tokens": 120}
    base.update(overrides)
    return AIConfig(**base)


def _loop(tmp_path: Path, settings: AIConfig) -> AgentLoop:
    context = ContextManager(settings, transcripts_root=tmp_path)
    return AgentLoop(
        _Engine(),
        SessionService(tmp_path / "sessions"),
        ToolRegistry(),
        context=context,
        settings=settings,
        workspace=str(tmp_path),
    )


def test_slash_parses_compact_and_memory():
    assert parse_slash("/compact").name == "compact"  # type: ignore[union-attr]
    assert parse_slash("/memory").name == "memory"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_a_short_turn_is_not_compacted(tmp_path):
    settings = _settings()
    loop = _loop(tmp_path, settings)
    session = loop.sessions.create()

    await loop.process_input(session, "第一轮")

    # Nothing was rewritten: the history is exactly user + assistant.
    assert [message.role for message in session.messages] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_compact_session_shrinks_and_persists(tmp_path):
    settings = _settings()
    loop = _loop(tmp_path, settings)
    session = loop.sessions.create()
    for index in range(12):
        await loop.process_input(session, f"第 {index} 轮：" + "内容" * 40)
    before = len(session.messages)

    report = await loop.compact_session(session)

    assert report["messages"] == before
    assert report["after"] < before
    assert len(session.messages) == report["after"]
    persisted = loop.sessions.load(session.session_id)
    assert persisted is not None and len(persisted.messages) == report["after"]


@pytest.mark.asyncio
async def test_repl_compact_stays_silent_on_success(tmp_path):
    """Success is silent on the console: the transcript row (── 压缩 ── +
    counts + usage) already reports it, and a second line would read as a
    second compaction.  The no-change path keeps its diagnostic line."""
    settings = _settings()
    loop = _loop(tmp_path, settings)
    sessions = loop.sessions
    session = sessions.create()
    for index in range(12):
        await loop.process_input(session, f"第 {index} 轮：" + "内容" * 40)
    console = _Console()

    action, returned = await handle_input(loop, "/compact", session, sessions, console)  # type: ignore[arg-type]

    assert returned is session
    assert not any("compact]" in line for line in console.lines), console.lines
    assert not any(line.startswith("ERROR") for line in console.lines)


@pytest.mark.asyncio
async def test_repl_compact_still_reports_when_nothing_changed(tmp_path):
    """The silence above is for success.  A no-op (tiny history, no
    summariser) keeps its one diagnostic line — otherwise /compact on a
    fresh session answers with nothing at all."""
    settings = _settings()
    loop = _loop(tmp_path, settings)
    sessions = loop.sessions
    session = sessions.create()
    await loop.process_input(session, "第一轮")
    console = _Console()

    await handle_input(loop, "/compact", session, sessions, console)  # type: ignore[arg-type]

    assert any("compact] messages" in line for line in console.lines), console.lines


@pytest.mark.asyncio
async def test_repl_memory_reports_status(tmp_path):
    settings = _settings()
    loop = _loop(tmp_path, settings)
    sessions = loop.sessions
    session = sessions.create()
    console = _Console()

    await handle_input(loop, "/memory", session, sessions, console)  # type: ignore[arg-type]

    assert any("memory]" in line for line in console.lines)


@pytest.mark.asyncio
async def test_r11c_repl_memory_lists_scenes_when_there_are_any(tmp_path):
    """`/memory` shows the scene map, but only when one exists — an unscoped
    library must not grow a dangling `scenes: -` (R11c)."""
    from ai_coding.memory.note import MemoryNote, NoteKind
    from ai_coding.memory.service import MemoryService
    from ai_coding.memory.store import MemoryStore

    settings = _settings()
    context = ContextManager(settings, transcripts_root=tmp_path)
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(MemoryNote(id="a", text="构建用 uv", scene="构建",
                              kind=NoteKind.DECISION))
    store.add_note(MemoryNote(id="b", text="发布走 Actions", scene="发布"))
    loop = AgentLoop(
        _Engine(),
        SessionService(tmp_path / "sessions"),
        ToolRegistry(),
        context=context,
        memory=MemoryService(store),
        settings=settings,
        workspace=str(tmp_path),
    )
    sessions = loop.sessions
    session = sessions.create()
    console = _Console()

    await handle_input(loop, "/memory", session, sessions, console)  # type: ignore[arg-type]

    line = next(line for line in console.lines if "memory]" in line)
    # Sorted like every other counter in this line (by code point).
    assert "scenes: 发布=1, 构建=1" in line
