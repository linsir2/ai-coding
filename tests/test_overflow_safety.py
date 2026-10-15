"""The context-overflow retry policy, pinned end to end.

The debt list asked for a guard in ``agent_loop``: "the overflow fallback does
not check whether output has started, unlike OpenCode's ``!outputStarted``".
Checking it found the premise wrong, and this file records what is actually
true.

The guard exists, one layer down.  ``openai_engine._stream`` keeps every chunk
it delivered in ``text_parts`` / ``acc``, and on any later failure raises an
``EngineError`` with **no kind**:

    if text_parts or acc:
        raise EngineError(f"stream failed after {len(text_parts)} text
                           chunk(s) ... had already been delivered")

An untyped failure never matches ``exc.kind == "context_overflow"`` in the
loop, so a post-output rejection cannot slip into the overflow path.  13 §2.5
(B) gives it its own response instead: one continuation carrying the
``[stream-interrupted]`` note — a blind retry would duplicate the text the
operator already saw — and the ordinary failure semantics after that.

A scripted engine can of course call ``on_delta`` and then raise a *typed*
overflow, and the loop would retry it.  No engine in this repository can: the
only two implementations are ``OpenAIChatEngine`` (guarded above) and
``FallbackEngine`` (pure delegation).  A guard for that shape would be a second
copy of a rule the engine already owns, on a path nothing takes — so it is not
written, and the test that would have demanded it is not kept.

What is left is the behaviour that must not regress: a rejection *before* any
output still compacts and retries, and after output the request is never
re-issued blind — the real engine test drives ``OpenAIChatEngine._stream`` to
pin both the delta path and the continuation's exact request.
"""

import pytest

from ai_coding.ai.engine import ChatEngine, EngineError
from ai_coding.config.models import AIConfig
from ai_coding.core.context_compression import ContextManager
from ai_coding.domain.session import SessionData
from ai_coding.service.session_service import SessionService
from tests.test_agent_loop_react import ScriptedEngine, _loop, _text


class _OverflowEngine(ChatEngine):
    """Rejects the request as too long, streaming first when asked to.

    ``stream_first`` is the measured case: a provider that answers with a 400
    only after the client already saw tokens.
    """

    def __init__(self, *, stream_first: bool) -> None:
        self.stream_first = stream_first
        self.requests: list[dict] = []

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.requests.append({"messages": list(messages)})
        if self.stream_first and on_delta is not None:
            on_delta("前半段已经到达用户眼前")
        raise EngineError("maximum context length exceeded", kind="context_overflow")

    async def aclose(self) -> None:
        pass


def _session(sessions: SessionData | SessionService):
    return sessions.create() if isinstance(sessions, SessionService) else sessions


def _fill(session: SessionData, count: int = 12) -> None:
    """History the compaction can actually shrink, or there is nothing to retry
    from and both branches raise for the same reason."""
    from ai_coding.domain.message import ChatMessage

    for index in range(count):
        session.add_message(
            ChatMessage(
                role="user" if index % 2 == 0 else "assistant",
                content=f"第{index}轮 " + "内容" * 200,
                timestamp="",
            )
        )


def _compactable(tmp_path) -> ContextManager:
    """A context that really removes messages, or the retry never happens either
    way and the test passes for the wrong reason."""
    async def summarizer(text: str) -> str:
        return "## 目标\n无"

    return ContextManager(
        AIConfig(max_context_tokens=500, max_messages=2),
        transcripts_root=tmp_path,
        summarizer=summarizer,
    )


@pytest.mark.asyncio
async def test_an_overflow_before_any_output_still_compacts_and_retries(tmp_path):
    """The I case that must not move.  Nothing reached the user, the history is
    the problem, and compacting is the only way to get an answer at all."""
    engine = ScriptedEngine(
        [EngineError("maximum context length exceeded", kind="context_overflow"), _text("好的")]
    )
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions, context=_compactable(tmp_path))
    session = _session(sessions)
    # Enough history that the compaction has something to remove, or there is
    # nothing to shrink and the retry never happens either way.
    _fill(session)

    await loop.process_input(session, "接着做")

    assert len(engine.requests) == 2, "no output had begun, so the retry is the point"


# ---- I: the plumbing must stay invisible ---------------------------------


@pytest.mark.asyncio
async def test_deltas_still_reach_the_caller_unchanged(tmp_path):
    """The wrapper must be transparent.  A delta sink that loses or alters text
    breaks the transcript, not just the retry policy."""
    seen: list[str] = []
    engine = ScriptedEngine([_text("完整回答")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)

    def sink(text: str) -> None:
        seen.append(text)

    await loop.process_input(_session(sessions), "说点什么", on_delta=sink)

    assert seen == ["完整回答"]


@pytest.mark.asyncio
async def test_no_delta_sink_does_not_break_the_loop(tmp_path):
    """`on_delta=None` is the ordinary non-TUI path; the wrapper must not assume
    a sink exists just because it needs to count calls."""
    engine = ScriptedEngine([_text("回答")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)

    result = await loop.process_input(_session(sessions), "说点什么", on_delta=None)

    assert result.startswith("回答")


@pytest.mark.asyncio
async def test_a_failure_that_is_not_an_overflow_is_untouched(tmp_path):
    """The gate is specific to a context overflow.  Any other EngineError must
    propagate exactly as it does today."""
    engine = ScriptedEngine([EngineError("rate limited", kind="rate_limit")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)

    result = await loop.process_input(_session(sessions), "说点什么")

    assert "rate limited" in result
    assert len(engine.requests) == 1


@pytest.mark.asyncio
async def test_a_second_overflow_is_still_refused_once_compaction_was_tried(tmp_path):
    """`reactive_compacted` already makes compaction once per turn.  That guard
    must survive: this is a second compaction, not a first."""
    engine = ScriptedEngine(
        [
            EngineError("maximum context length exceeded", kind="context_overflow"),
            EngineError("maximum context length exceeded", kind="context_overflow"),
        ]
    )
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions, context=_compactable(tmp_path))
    session = _session(sessions)
    _fill(session)

    result = await loop.process_input(session, "说点什么")

    assert "maximum context length" in result
    assert len(engine.requests) == 2


# ---- P: the ordinary paths still work ------------------------------------


@pytest.mark.asyncio
async def test_a_normal_turn_with_a_sink_completes(tmp_path):
    engine = ScriptedEngine([_text("完整回答")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    seen: list[str] = []

    def sink(text: str) -> None:
        seen.append(text)

    result = await loop.process_input(_session(sessions), "说点什么", on_delta=sink)

    assert result.startswith("完整回答")
    assert seen == ["完整回答"]


# ---- the real engine, not a stand-in --------------------------------------


@pytest.mark.asyncio
async def test_the_real_engine_streaming_first_earns_one_continuation(tmp_path):
    """The one test that is not a stand-in for the thing it checks.

    Every test above scripts a fake engine whose ``step`` calls ``on_delta``.
    This one drives the real ``OpenAIChatEngine._stream`` — the code that owns
    the ``on_delta`` call — against a client that yields a content chunk and
    only *then* rejects the request.  13 §2.5: the delta reaching the operator
    through the real engine is what earns the single continuation; the request
    is never re-issued *blind* (the continuation carries the
    ``[stream-interrupted]`` note), and a second failure keeps the ordinary
    error semantics.
    """
    from types import SimpleNamespace

    import openai

    from ai_coding.ai.openai_engine import OpenAIChatEngine
    from ai_coding.domain.model_config import ModelConfig
    from ai_coding.service.session_service import SessionService

    class _Resp:
        """The minimum an ``openai`` status error reads off its response."""

        request = object()
        status_code = 400
        headers: dict = {}

    overflow = openai.BadRequestError(
        "maximum context length exceeded", response=_Resp(), body=None
    )

    class _MidStreamFailure:
        """A stream that produces one delta and then fails, as a provider that
        rejects after the client has already seen tokens."""

        def __init__(self) -> None:
            self._items = [
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content="前半段", tool_calls=None),
                            finish_reason=None,
                        )
                    ],
                    usage=None,
                ),
                overflow,
            ]

        def __aiter__(self):
            self._index = 0
            return self

        async def __anext__(self):
            item = self._items[self._index]
            self._index += 1
            if isinstance(item, BaseException):
                raise item
            return item

    class _Client:
        def __init__(self) -> None:
            self.calls = 0
            self.last_messages: list = []
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self._create)
            )

        async def _create(self, **kwargs):
            self.calls += 1
            self.last_messages = list(kwargs.get("messages") or [])
            return _MidStreamFailure()

        async def close(self) -> None:
            pass

    engine = OpenAIChatEngine(
        ModelConfig(name="m", base_url="http://x/v1", api_key="k"), client=_Client()
    )
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions, context=_compactable(tmp_path))
    session = sessions.create()
    _fill(session)

    result = await loop.process_input(session, "接着做")

    assert engine._client.calls == 2, (
        "one broken stream plus its one continuation: the delta reached the "
        "operator through the real engine (13 §2.5), and a blind re-send would "
        "duplicate the text they already saw"
    )
    rendered = "\n".join(
        str(m.get("content") or "") for m in engine._client.last_messages
    )
    assert "[stream-interrupted]" in rendered, (
        "the second request must carry the continuation note, not repeat the ask"
    )
    assert "maximum context length" in result
