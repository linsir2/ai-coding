"""R20/RC-7: the loop sends an answer back when it cites code never read.

The three-tier decision is unit-tested in ``tests/test_read_ledger.py``.  What is
tested here is the *behaviour* around it: the correction is issued exactly once,
the answer is delivered on the second try, and a clean answer is never touched.

Shape follows ``tests/test_interventions_loop.py`` (self-contained scripted
engine + real ``AgentLoop``).
"""

from __future__ import annotations

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.domain.loop import StepResult
from ai_coding.domain.message import ToolResult
from ai_coding.security.approval import AutoApproveCallback
from ai_coding.service.session_service import SessionService
from ai_coding.tools.base import BaseTool
from ai_coding.tools.registry import ToolRegistry


class _Reader(BaseTool):
    """A read tool that reports the range it returned, like the real one."""

    name = "read"
    parallel_safe = True

    @property
    def description(self) -> str:
        return "reads the named file"

    @property
    def params_json_schema(self) -> dict:
        return {"type": "object", "properties": {"path": {"type": "string"}}}

    async def execute(self, params, ctx=None) -> ToolResult:
        from ai_coding.tools.base import resolve_ctx

        note_read = resolve_ctx(ctx).note_read
        if note_read is not None:
            note_read(str(params.get("path", "")), 1, 3, 3)
        return ToolResult(success=True, output="one\ntwo\nthree")


class _Greper(BaseTool):
    """A grep tool: its output is a ``path:line: text`` listing."""

    name = "grep"
    parallel_safe = True

    @property
    def description(self) -> str:
        return "searches file contents"

    @property
    def params_json_schema(self) -> dict:
        return {"type": "object", "properties": {"pattern": {"type": "string"}}}

    async def execute(self, params, ctx=None) -> ToolResult:
        return ToolResult(success=True, output="x.py:3: the hit")


class _ScriptedEngine(ChatEngine):
    def __init__(self, steps: list) -> None:
        self._steps = list(steps)
        self.requests: list[dict] = []

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.requests.append({"messages": list(messages), "tools": list(tools)})
        if not self._steps:
            raise AssertionError("engine called more times than the script allows")
        item = self._steps.pop(0)
        if on_delta is not None and item.text:
            on_delta(item.text)
        return item

    async def aclose(self) -> None:
        return None


def _text(text: str) -> StepResult:
    return StepResult(text=text, finish_reason="stop")


def _loop(engine: ChatEngine, sessions: SessionService, **settings) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(_Reader())
    registry.register(_Greper())
    return AgentLoop(
        engine,
        sessions,
        registry,
        approval=AutoApproveCallback(),
        settings=AIConfig(**settings),
        workspace=".",
    )


def _corrections(session) -> list[str]:
    return [
        m.content or ""
        for m in session.messages
        if m.synthetic and "没有读过的位置" in (m.content or "")
    ]


@pytest.mark.asyncio
async def test_an_answer_citing_an_unread_range_is_sent_back_once(tmp_path):
    """D1: the model read 61-110 and answered about 18-46 -- the exact shape of
    T2's hallucination."""
    engine = _ScriptedEngine([
        _text("缺陷在 `a.py` 第 18-46 行，构造函数没有长度保护。"),
        _text("更正：`a.py` 第 81 行没有长度保护；18-46 行我沒有读过，标为未确认。"),
    ])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    session = sessions.create()
    session.note_read("a.py", 61, 110, 117, workspace=".")

    answer = await loop.process_input(session, "找缺陷")

    assert len(_corrections(session)) == 1
    assert "18-46" in _corrections(session)[0]
    assert session.claim_retries == 1
    assert "更正" in answer
    assert "18-46" not in answer.split("更正")[0]


@pytest.mark.asyncio
async def test_the_same_answer_is_accepted_the_second_time(tmp_path):
    """D2: the guard is one correction per turn, not a loop.  Without the cap the
    model could never finish a turn whose answer it cannot verify."""
    engine = _ScriptedEngine([
        _text("缺陷在 `a.py` 第 18-46 行。"),
        _text("缺陷在 `a.py` 第 18-46 行。"),
    ])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    session = sessions.create()
    session.note_read("a.py", 61, 110, 117, workspace=".")

    await loop.process_input(session, "找缺陷")

    assert len(_corrections(session)) == 1, "the check must not loop"
    assert loop.last_outcome.status.value != "failed"


@pytest.mark.asyncio
async def test_a_citation_inside_the_read_range_is_never_touched(tmp_path):
    """D3: a clean answer must not pay for the check."""
    engine = _ScriptedEngine([_text("缺陷在 `a.py` 第 81 行。")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    session = sessions.create()
    session.note_read("a.py", 61, 110, 117, workspace=".")

    answer = await loop.process_input(session, "找缺陷")

    assert _corrections(session) == []
    assert session.claim_retries == 0
    assert "81" in answer


@pytest.mark.asyncio
async def test_a_bare_line_number_without_a_path_is_ignored(tmp_path):
    """D4: there is nothing to bind it to, and challenging it would be noise."""
    engine = _ScriptedEngine([_text("缺陷在第 18-46 行。")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    session = sessions.create()

    await loop.process_input(session, "找缺陷")

    assert _corrections(session) == []


@pytest.mark.asyncio
async def test_two_files_in_one_answer_skip_the_check(tmp_path):
    """D5: last-path-wins binding would challenge a citation that was right.

    ``never.py`` was never touched and ``a.py`` was read; the answer names the
    unread one first.  Without the ambiguity guard the whole answer is judged
    against ``never.py`` and challenged -- one wasted iteration at the budget's
    end.
    """
    engine = _ScriptedEngine([
        _text("`never.py` 第 1-10 行与 `a.py` 第 1-10 行行为一致。"),
    ])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    session = sessions.create()
    session.note_read("a.py", 1, 10, 10, workspace=".")

    await loop.process_input(session, "对比两个文件")

    assert _corrections(session) == []


@pytest.mark.asyncio
async def test_a_citation_to_a_never_touched_file_is_sent_back(tmp_path):
    """D6: tier 1 -- the model named a file this session never opened at all."""
    engine = _ScriptedEngine([
        _text("缺陷在 `never.py` 第 1-10 行。"),
        _text("更正：`never.py` 未确认，我没有读过它。"),
    ])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    session = sessions.create()
    session.note_read("a.py", 1, 10, 10, workspace=".")

    await loop.process_input(session, "找缺陷")

    assert len(_corrections(session)) == 1
    assert "never.py" in _corrections(session)[0]


@pytest.mark.asyncio
async def test_a_citation_to_a_grep_only_file_is_accepted(tmp_path):
    """D7: tier 2.  grep showed the file, so the model may name it; the ledger
    cannot say which lines it saw, so the check stays quiet."""
    engine = _ScriptedEngine([_text("缺陷在 `grep_only.py` 第 3 行。")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    session = sessions.create()
    session.note_seen(["grep_only.py"])

    await loop.process_input(session, "找缺陷")

    assert _corrections(session) == []


@pytest.mark.asyncio
async def test_the_loop_records_the_paths_its_tool_results_named(tmp_path):
    """``seen_paths`` is accumulated from tool output, not derived from the
    transcript: compaction keeps only a 2,000-character excerpt of each tool
    result, so a derived set would forget the early ones and challenge a
    legitimate citation.

    grep is the tool that proves it: the file was never read, so the ledger has
    no mark for it, and the answer naming it must pass on tier 2 alone.
    """
    from ai_coding.domain.message import ToolCallRef

    engine = _ScriptedEngine([
        StepResult(
            text=None,
            tool_calls=[ToolCallRef(id="c1", name="grep", arguments='{"pattern": "hit"}')],
            finish_reason="tool_calls",
        ),
        _text("缺陷在 `x.py` 第 3 行。"),
    ])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    session = sessions.create()

    await loop.process_input(session, "找缺陷")

    assert "x.py" in session.seen_paths
    assert _corrections(session) == []


@pytest.mark.asyncio
async def test_a_reads_own_content_does_not_count_as_seeing_other_files(tmp_path):
    """A read's output is the *content* of a file, which names every module it
    mentions.  Counting those would let a citation of a never-opened file pass
    as "seen" and silently disable tier 1 -- the strongest signal there is."""
    from ai_coding.domain.message import ToolCallRef

    class _ChattyReader(_Reader):
        async def execute(self, params, ctx=None) -> ToolResult:
            await super().execute(params, ctx)
            return ToolResult(
                success=True,
                output="import helper.py  # see helper.py for the parser",
            )

    registry = ToolRegistry()
    registry.register(_ChattyReader())
    engine = _ScriptedEngine([
        StepResult(
            text=None,
            tool_calls=[ToolCallRef(id="c1", name="read", arguments='{"path": "a.py"}')],
            finish_reason="tool_calls",
        ),
        _text("缺陷在 `helper.py` 第 1 行。"),
        _text("更正：`helper.py` 我没有读过，标为未确认。"),
    ])
    sessions = SessionService(tmp_path / "sessions")
    loop = AgentLoop(
        engine,
        sessions,
        registry,
        approval=AutoApproveCallback(),
        settings=AIConfig(),
        workspace=".",
    )
    session = sessions.create()

    await loop.process_input(session, "找缺陷")

    assert "helper.py" not in session.seen_paths
    assert len(_corrections(session)) == 1


@pytest.mark.asyncio
async def test_a_turn_of_new_evidence_is_never_nagged(tmp_path):
    """RC-1, end to end.  The unit tests pin the ladder; this pins the wiring --
    if ``TurnBooks.snapshot`` stopped carrying ``novel_findings``, the gate would
    fire on a turn that was learning, which is the whole T2 failure."""
    from ai_coding.core.events import RecordingObserver
    from ai_coding.domain.message import ToolCallRef

    class _NovelReader(BaseTool):
        """A read whose body depends on the path, so every call is a new finding."""

        name = "read"
        parallel_safe = True

        @property
        def description(self) -> str:
            return "reads the named file"

        @property
        def params_json_schema(self) -> dict:
            return {"type": "object", "properties": {"path": {"type": "string"}}}

        async def execute(self, params, ctx=None) -> ToolResult:
            return ToolResult(success=True, output=f"body of {params.get('path', '')}")

    registry = ToolRegistry()
    registry.register(_NovelReader())
    engine = _ScriptedEngine(
        [
            StepResult(
                text=None,
                tool_calls=[
                    ToolCallRef(id=f"c{i}", name="read", arguments=f'{{"path": "f{i}.py"}}')
                ],
                finish_reason="tool_calls",
            )
            for i in range(1, 7)
        ]
        + [_text("结论：没有真实缺陷")]
    )
    sessions = SessionService(tmp_path / "sessions")
    observer = RecordingObserver()
    loop = AgentLoop(
        engine,
        sessions,
        registry,
        approval=AutoApproveCallback(),
        settings=AIConfig(max_tool_iterations=12),
        workspace=".",
        observer=observer,
    )
    session = sessions.create()

    await loop.process_input(session, "找缺陷")

    stalled = [
        e for e in observer.events if type(e).__name__ == "SearchStalled"
    ]
    assert not [e for e in stalled if e.level in (1, 2)], (
        f"a novel-only read sweep was nagged: {stalled}"
    )


@pytest.mark.asyncio
async def test_seen_paths_survive_a_compaction(tmp_path):
    """The reason ``seen_paths`` is accumulated rather than derived from the
    transcript: compaction keeps only a 2,000-character excerpt of each tool
    result, so a derived set would forget the early ones and challenge a
    legitimate citation on the next turn."""
    from ai_coding.domain.message import ToolCallRef

    engine = _ScriptedEngine([
        StepResult(
            text=None,
            tool_calls=[ToolCallRef(id="c1", name="grep", arguments='{"pattern": "hit"}')],
            finish_reason="tool_calls",
        ),
        _text("缺陷在 `x.py` 第 3 行。"),
    ])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    session = sessions.create()

    await loop.process_input(session, "找缺陷")
    before = session.seen_paths
    assert "x.py" in before

    await loop.compactor.compact_now(session, timeout=10.0, reason="manual")

    assert session.seen_paths == before, "compaction must not shrink the ledger"
    assert "x.py" in session.seen_paths


@pytest.mark.asyncio
async def test_the_correction_is_issued_after_the_acceptance_proofs(tmp_path):
    """Decision 7: the check runs after ``_verify_acceptance``.  Issuing it
    before would skip the proofs, and T2's hallucination landed at the budget's
    end where a later retry would never be seen."""
    order: list[str] = []
    engine = _ScriptedEngine([
        _text("缺陷在 `a.py` 第 18-46 行。"),
        _text("更正：`a.py` 第 81 行没有长度保护。"),
    ])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    session = sessions.create()
    session.note_read("a.py", 61, 110, 117, workspace=".")
    original = loop._verify_acceptance

    async def _tracked(session, books):
        order.append("acceptance")
        await original(session, books)

    loop._verify_acceptance = _tracked  # type: ignore[method-assign]
    loop._reject_unsupported_claims = _tracking_reject(loop, order)  # type: ignore[method-assign]

    await loop.process_input(session, "找缺陷")

    # Both iterations run the pair; in each one the proofs go first.  (The second
    # iteration's check returns False -- the correction chance is spent.)
    assert order[:2] == ["acceptance", "claims"]
    assert all(
        order[i] == "acceptance" and order[i + 1] == "claims"
        for i in range(0, len(order) - 1, 2)
    )


def _tracking_reject(loop, order):
    original = loop._reject_unsupported_claims

    def _wrapped(session, text, index):
        order.append("claims")
        return original(session, text, index)

    return _wrapped
