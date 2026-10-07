"""TDD (R1a-2): OpenAIChatEngine — one model step, driven by an injected client.

Everything here is offline: the fake client's ``chat.completions.create`` returns
an async-iterable of chunk-shaped objects, so the streaming assembly logic is
exercised without touching the network or needing an API key.
"""

from types import SimpleNamespace

import pytest

from ai_coding.ai.engine import EngineError, FallbackWorthwhile
from ai_coding.ai.openai_engine import OpenAIChatEngine
from ai_coding.domain.message import ChatMessage
from ai_coding.domain.model_config import ModelConfig


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        self._it = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None


class _FakeCompletions:
    def __init__(self, chunks=None, error: Exception | None = None):
        self._chunks = chunks or []
        self._error = error
        self.kwargs: dict | None = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        if self._error is not None:
            raise self._error
        return _FakeStream(self._chunks)


class _FakeClient:
    def __init__(self, chunks=None, error: Exception | None = None):
        self.completions = _FakeCompletions(chunks, error)
        self.chat = SimpleNamespace(completions=self.completions)
        self.closed = False

    async def close(self):
        self.closed = True


def _tc_delta(index, *, id=None, name=None, arguments=None):
    return SimpleNamespace(
        index=index, id=id, function=SimpleNamespace(name=name, arguments=arguments)
    )


def _text_chunk(text, *, finish_reason=None, tool_calls=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=text, tool_calls=tool_calls),
                finish_reason=finish_reason,
            )
        ],
        usage=None,
    )


def _usage_chunk(prompt=10, completion=5, total=15):
    """The include_usage trailer: choices is empty, usage is set."""
    return SimpleNamespace(
        choices=[],
        usage=SimpleNamespace(
            prompt_tokens=prompt, completion_tokens=completion, total_tokens=total
        ),
    )


def _model(**over):
    kwargs = {
        "name": "test-model",
        "base_url": "https://example.test/v1",
        "api_key": "k",
    }
    kwargs.update(over)
    return ModelConfig(**kwargs)


def _engine(chunks=None, error=None, **over):
    client = _FakeClient(chunks, error)
    return OpenAIChatEngine(_model(**over), client=client), client


def _user(content="hi"):
    return ChatMessage(role="user", content=content, timestamp="t")


def test_build_request_messages_prepends_system_when_instructions():
    request = OpenAIChatEngine.build_request_messages([_user("hi")], "be terse")
    assert request[0] == {"role": "system", "content": "be terse"}
    assert request[1] == {"role": "user", "content": "hi"}


def test_build_request_messages_omits_system_when_no_instructions():
    request = OpenAIChatEngine.build_request_messages([_user("hi")], None)
    assert request == [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_step_streams_text_deltas_to_sink():
    engine, _ = _engine([_text_chunk("he"), _text_chunk("llo")])
    deltas: list[str] = []
    result = await engine.step([_user()], tools=[], on_delta=deltas.append)
    assert deltas == ["he", "llo"]
    assert result.text == "hello"


@pytest.mark.asyncio
async def test_step_assembles_tool_calls_from_stream():
    chunks = [
        _text_chunk(None, tool_calls=[_tc_delta(0, id="c1", name="read", arguments='{"pa')]),
        _text_chunk(None, tool_calls=[_tc_delta(0, arguments='th":"a"}')]),
    ]
    engine, _ = _engine(chunks)
    result = await engine.step([_user()], tools=[])
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "read"
    assert result.tool_calls[0].arguments == '{"path":"a"}'


@pytest.mark.asyncio
async def test_step_returns_finish_reason():
    chunks = [_text_chunk("x", finish_reason=None), _text_chunk("", finish_reason="stop")]
    engine, _ = _engine(chunks)
    result = await engine.step([_user()], tools=[])
    assert result.finish_reason == "stop"


@pytest.mark.asyncio
async def test_step_captures_usage_when_present():
    """The usage trailer has empty choices — it must not be skipped early."""
    chunks = [_text_chunk("x", finish_reason="stop"), _usage_chunk(10, 5, 15)]
    engine, _ = _engine(chunks)
    result = await engine.step([_user()], tools=[])
    assert result.usage == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }


@pytest.mark.asyncio
async def test_step_tolerates_missing_usage():
    engine, _ = _engine([_text_chunk("x", finish_reason="stop")])
    result = await engine.step([_user()], tools=[])
    assert result.usage is None


# ---- R4a: prompt-cache details must survive the engine boundary ------------


def _usage_chunk_with_cache(**usage_fields):
    return SimpleNamespace(
        choices=[],
        usage=SimpleNamespace(**usage_fields),
    )


@pytest.mark.asyncio
async def test_step_carries_openai_prompt_cache_details():
    chunk = _usage_chunk_with_cache(
        prompt_tokens=100,
        completion_tokens=5,
        total_tokens=105,
        prompt_tokens_details=SimpleNamespace(cached_tokens=80),
    )
    engine, _ = _engine([_text_chunk("x", finish_reason="stop"), chunk])

    result = await engine.step([_user()], tools=[])

    assert result.usage is not None
    assert result.usage["cached_tokens"] == 80
    assert result.usage["prompt_tokens"] == 100


@pytest.mark.asyncio
async def test_step_carries_anthropic_cache_read_and_creation():
    chunk = _usage_chunk_with_cache(
        prompt_tokens=100,
        completion_tokens=5,
        total_tokens=105,
        cache_read_input_tokens=60,
        cache_creation_input_tokens=25,
    )
    engine, _ = _engine([_text_chunk("x", finish_reason="stop"), chunk])

    result = await engine.step([_user()], tools=[])

    assert result.usage is not None
    assert result.usage["cached_tokens"] == 60
    assert result.usage["cache_write_tokens"] == 25


@pytest.mark.asyncio
async def test_step_omits_cache_keys_when_the_endpoint_reports_none():
    chunk = _usage_chunk_with_cache(
        prompt_tokens=100,
        completion_tokens=5,
        total_tokens=105,
        prompt_tokens_details=SimpleNamespace(cached_tokens=None),
    )
    engine, _ = _engine([_text_chunk("x", finish_reason="stop"), chunk])

    result = await engine.step([_user()], tools=[])

    assert result.usage == {
        "prompt_tokens": 100,
        "completion_tokens": 5,
        "total_tokens": 105,
    }


@pytest.mark.asyncio
async def test_step_returns_none_text_when_only_tool_calls():
    chunks = [_text_chunk(None, tool_calls=[_tc_delta(0, id="c1", name="read")])]
    engine, _ = _engine(chunks)
    result = await engine.step([_user()], tools=[])
    assert result.text is None
    assert len(result.tool_calls) == 1


@pytest.mark.asyncio
async def test_step_raises_engine_error_on_client_failure():
    engine, _ = _engine(error=RuntimeError("boom"))
    with pytest.raises(EngineError):
        await engine.step([_user()], tools=[])


@pytest.mark.asyncio
async def test_step_does_not_append_extra_user_message():
    """Regression: the old path sent the user turn twice."""
    engine, client = _engine([_text_chunk("x", finish_reason="stop")])
    await engine.step([_user("only once")], tools=[])
    sent = client.completions.kwargs["messages"]
    assert [m["role"] for m in sent] == ["user"]
    assert sent[0]["content"] == "only once"


@pytest.mark.asyncio
async def test_step_omits_tools_when_empty():
    engine, client = _engine([_text_chunk("x", finish_reason="stop")])
    await engine.step([_user()], tools=[])
    assert "tools" not in client.completions.kwargs


@pytest.mark.asyncio
async def test_step_passes_tool_schemas_through():
    schema = {"type": "function", "function": {"name": "read", "parameters": {}}}
    engine, client = _engine([_text_chunk("x", finish_reason="stop")])
    await engine.step([_user()], tools=[schema])
    assert client.completions.kwargs["tools"] == [schema]


@pytest.mark.asyncio
async def test_step_forwards_model_settings():
    engine, client = _engine(
        [_text_chunk("x", finish_reason="stop")],
        temperature=0.3,
        max_tokens=123,
        top_p=0.5,
    )
    await engine.step([_user()], tools=[])
    kwargs = client.completions.kwargs
    assert kwargs["model"] == "test-model"
    assert kwargs["temperature"] == 0.3
    assert kwargs["max_tokens"] == 123
    assert kwargs["top_p"] == 0.5
    assert kwargs["stream"] is True


@pytest.mark.asyncio
async def test_step_requests_usage_by_default():
    engine, client = _engine([_text_chunk("x", finish_reason="stop")])
    await engine.step([_user()], tools=[])
    assert client.completions.kwargs["stream_options"] == {"include_usage": True}


@pytest.mark.asyncio
async def test_step_can_disable_usage_request():
    client = _FakeClient([_text_chunk("x", finish_reason="stop")])
    engine = OpenAIChatEngine(_model(), client=client, stream_usage=False)
    await engine.step([_user()], tools=[])
    assert "stream_options" not in client.completions.kwargs


@pytest.mark.asyncio
async def test_aclose_closes_owned_client():
    engine = OpenAIChatEngine(_model())
    assert engine._owns_client is True
    fake = _FakeClient()
    engine._client = fake
    await engine.aclose()
    assert fake.closed is True


@pytest.mark.asyncio
async def test_aclose_does_not_close_injected_client():
    engine, client = _engine()
    assert engine._owns_client is False
    await engine.aclose()
    assert client.closed is False


def test_engine_rejects_non_model_config():
    with pytest.raises(TypeError):
        OpenAIChatEngine("not-a-model-config")  # type: ignore[arg-type]


# ---- provider failure classification and bounded retry (P0-1) --------------
#
# A duck-typed response keeps these tests off the SDK's vendored httpx name:
# the exception constructors only read ``.request``, ``.status_code`` and
# ``.headers``, so anything shaped like that is enough to classify on.


class _Resp:
    def __init__(self, status: int, headers: dict | None = None) -> None:
        self.request = object()
        self.status_code = status
        self.headers = headers or {}


class _Req:
    def __init__(self) -> None:
        self.method = "POST"
        self.url = "https://example.test/v1/chat/completions"


def _rate_limited(retry_after: str | None = None):
    headers = {"retry-after": retry_after} if retry_after else None
    import openai

    return openai.RateLimitError("slow down", response=_Resp(429, headers), body=None)


def _server_error():
    import openai

    return openai.InternalServerError("upstream is unwell", response=_Resp(503), body=None)


def _auth_error():
    import openai

    return openai.AuthenticationError("bad key", response=_Resp(401), body=None)


def _timeout():
    import openai

    return openai.APITimeoutError(request=_Req())


class _CutStream:
    """Yields its chunks, then fails mid-flight — output already reached the caller."""

    def __init__(self, chunks, error: Exception) -> None:
        self._chunks = list(chunks)
        self._error = error

    def __aiter__(self):
        self._it = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise self._error from None


class _SequencedCompletions:
    """``create`` walks ``outcomes``: an exception to raise, or chunks to stream."""

    def __init__(self, outcomes) -> None:
        self._outcomes = list(outcomes)
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        outcome = self._outcomes[min(self.calls - 1, len(self._outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return _FakeStream(outcome)


class _SequencedClient:
    def __init__(self, outcomes) -> None:
        self.completions = _SequencedCompletions(outcomes)
        self.chat = SimpleNamespace(completions=self.completions)

    async def close(self):
        return None


class _CutClient:
    def __init__(self, chunks, error: Exception) -> None:
        self.completions = SimpleNamespace(create=self._create(chunks, error))
        self.chat = SimpleNamespace(completions=self.completions)
        self.calls = 0

    def _create(self, chunks, error):
        async def create(**kwargs):
            self.calls += 1
            return _CutStream(chunks, error)

        return create

    async def close(self):
        return None


def _fast_engine(client, **over):
    """Retries without spending wall-clock: the delay is a pure function."""
    return OpenAIChatEngine(_model(retry_base_seconds=0.0, **over), client=client)


@pytest.mark.asyncio
async def test_a_rate_limit_is_retried_and_the_next_attempt_succeeds():
    client = _SequencedClient([_rate_limited(), [_text_chunk("hi", finish_reason="stop")]])
    engine = _fast_engine(client)

    step = await engine.step([_user()], tools=[])

    assert step.text == "hi"
    assert client.completions.calls == 2


@pytest.mark.asyncio
async def test_an_authentication_error_is_not_retried():
    """Retrying a rejected key cannot help, and each try costs a round trip.

    M2 widened the contract: a rejected key is now ``FallbackWorthwhile``, so
    the engine layer can be composed into a chain that tries another provider.
    Still an ``EngineError``, so every existing handler is unaffected.
    """
    client = _SequencedClient([_auth_error()])
    engine = _fast_engine(client)

    with pytest.raises(FallbackWorthwhile) as excinfo:
        await engine.step([_user()], tools=[])

    assert client.completions.calls == 1
    assert "auth" in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_a_timeout_is_retried():
    client = _SequencedClient([_timeout(), [_text_chunk("hi", finish_reason="stop")]])
    engine = _fast_engine(client)

    assert (await engine.step([_user()], tools=[])).text == "hi"
    assert client.completions.calls == 2


@pytest.mark.asyncio
async def test_retries_stop_at_max_retries():
    """A permanently broken endpoint must cost a bounded number of calls, not hang."""
    client = _SequencedClient([_server_error()])
    engine = _fast_engine(client, max_retries=3)

    with pytest.raises(EngineError):
        await engine.step([_user()], tools=[])

    assert client.completions.calls == 4  # max_retries + 1


@pytest.mark.asyncio
async def test_a_failure_after_text_started_is_not_retried():
    """The deltas already reached the caller; a retry would print them twice.

    This is the reason the retry lives behind a partial-output guard rather than
    simply wrapping the stream.
    """
    client = _CutClient([_text_chunk("已经显示的半句话")], _server_error())
    engine = _fast_engine(client)

    with pytest.raises(EngineError) as excinfo:
        await engine.step([_user()], tools=[])

    assert client.calls == 1
    assert "already" in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_zero_retries_restores_the_old_behaviour():
    client = _SequencedClient([_server_error()])
    engine = _fast_engine(client, max_retries=0)

    with pytest.raises(EngineError):
        await engine.step([_user()], tools=[])

    assert client.completions.calls == 1


def test_classify_failure_uses_status_codes_not_just_sdk_classes():
    import openai

    from ai_coding.ai.openai_engine import FailureKind, classify_failure

    assert classify_failure(_rate_limited()) is FailureKind.RATE_LIMITED
    assert classify_failure(_server_error()) is FailureKind.SERVER_ERROR
    assert classify_failure(_timeout()) is FailureKind.TRANSPORT
    assert classify_failure(openai.APIConnectionError(request=_Req())) is FailureKind.TRANSPORT
    # A gateway can answer 429/503 without the SDK naming the subclass.
    assert classify_failure(
        openai.APIStatusError("odd", response=_Resp(429), body=None)
    ) is FailureKind.RATE_LIMITED
    assert classify_failure(
        openai.APIStatusError("odd", response=_Resp(502), body=None)
    ) is FailureKind.SERVER_ERROR
    # A rejected key is a different problem from a rejected request: the first is
    # worth another provider, the second is worth nothing (M2 §5).
    assert classify_failure(_auth_error()) is FailureKind.AUTH_ERROR
    assert classify_failure(
        openai.APIStatusError("odd", response=_Resp(403), body=None)
    ) is FailureKind.AUTH_ERROR
    assert classify_failure(
        openai.APIStatusError("odd", response=_Resp(400), body=None)
    ) is FailureKind.FATAL
    # A rejected *context* is a different verdict from a rejected *request*: the
    # first is fixable by compacting the history, the second is not (G5-1).  The
    # only signal the provider gives is the text, which is why it is matched.
    assert classify_failure(
        openai.BadRequestError(
            "This model's maximum context length is 65536 tokens",
            response=_Resp(400),
            body=None,
        )
    ) is FailureKind.CONTEXT_OVERFLOW
    assert classify_failure(
        openai.APIStatusError("request too large", response=_Resp(413), body=None)
    ) is FailureKind.CONTEXT_OVERFLOW
    # A 413 whose text says nothing about size stays fatal: guessing here would
    # rewrite the operator's history for a failure compaction cannot fix.
    assert classify_failure(
        openai.APIStatusError("weird gateway error", response=_Resp(413), body=None)
    ) is FailureKind.FATAL
    assert classify_failure(RuntimeError("boom")) is FailureKind.FATAL
    assert classify_failure(ValueError("bad json")) is FailureKind.FATAL


def test_retry_delay_grows_exponentially_and_is_capped():
    from ai_coding.ai.openai_engine import FailureKind, retry_delay

    kind = FailureKind.SERVER_ERROR
    kwargs = {"base": 2.0, "cap": 60.0, "retry_after": None}
    assert retry_delay(0, kind, **kwargs) == 2.0
    assert retry_delay(1, kind, **kwargs) == 4.0
    assert retry_delay(2, kind, **kwargs) == 8.0
    assert retry_delay(10, kind, **kwargs) == 60.0  # capped


def test_retry_after_header_wins_for_a_rate_limit_and_is_still_capped():
    from ai_coding.ai.openai_engine import FailureKind, retry_delay

    limited = FailureKind.RATE_LIMITED
    assert retry_delay(0, limited, base=2.0, cap=60.0, retry_after=7.0) == 7.0
    # A provider asking for five minutes must not eat the turn.
    assert retry_delay(0, limited, base=2.0, cap=60.0, retry_after=300.0) == 60.0
    # Only a rate limit has an opinion; a 5xx does not.
    assert retry_delay(0, FailureKind.SERVER_ERROR, base=2.0, cap=60.0, retry_after=7.0) == 2.0


def test_retry_after_is_read_from_the_header_and_ignored_when_unparsable():
    from ai_coding.ai.openai_engine import _retry_after

    assert _retry_after(_rate_limited("7")) == 7.0
    assert _retry_after(_rate_limited("Wed, 21 Oct 2026 07:28:00 GMT")) is None
    assert _retry_after(_rate_limited()) is None
    assert _retry_after(_server_error()) is None
    assert _retry_after(RuntimeError("boom")) is None


@pytest.mark.asyncio
async def test_engine_reports_each_retry_to_on_retry():
    """Without an out-of-band channel the engine is silent, so the operator
    waits and cannot tell a rate-limit backoff from a stalled turn."""
    seen: list[tuple[str, int]] = []
    client = _SequencedClient([
        _rate_limited(),
        _server_error(),
        [_text_chunk("好", finish_reason="stop")],
    ])
    engine = OpenAIChatEngine(
        _model(retry_base_seconds=0.0),
        client=client,
        on_retry=lambda *, kind, attempt, delay_seconds, reason: seen.append((kind, attempt)),
    )

    await engine.step([_user()], tools=[])

    assert seen == [("rate_limited", 1), ("server_error", 2)]


@pytest.mark.asyncio
async def test_engine_works_without_an_on_retry_hook():
    """The hook is optional: a teacher asking for one must not change the default
    engine, and today's callers that never pass it keep working."""
    client = _SequencedClient([_server_error(), [_text_chunk("好", finish_reason="stop")]])

    assert (await _fast_engine(client).step([_user()], tools=[])).text == "好"


# ---- M2: what is worth another model, and what is not ----------------------


@pytest.mark.asyncio
async def test_exhausted_retries_raise_fallback_worthwhile():
    """A transient failure that used up its budget is the one case where trying
    a different provider can still produce an answer."""
    client = _SequencedClient([_rate_limited()])
    engine = _fast_engine(client, max_retries=2)

    with pytest.raises(FallbackWorthwhile):
        await engine.step([_user()], tools=[])

    assert client.completions.calls == 3


@pytest.mark.asyncio
async def test_a_bad_request_is_not_fallback_worthwhile():
    """400 is a property of the messages we sent, not of the server: the same
    request fails identically on every provider, so a chain would only burn the
    next model's whole budget to learn nothing."""
    import openai

    client = _SequencedClient([openai.BadRequestError("nope", response=_Resp(400), body=None)])
    engine = _fast_engine(client, max_retries=2)

    with pytest.raises(EngineError) as excinfo:
        await engine.step([_user()], tools=[])

    assert not isinstance(excinfo.value, FallbackWorthwhile)
    assert client.completions.calls == 1


@pytest.mark.asyncio
async def test_a_failure_after_text_started_is_not_fallback_worthwhile():
    """The invariant that makes M2 safe (docs spec §5): once text or a partial
    tool call has reached the operator, no other model may take over — it would
    repeat the prose and splice a half-built tool call."""
    client = _CutClient([_text_chunk("partial")], _server_error())
    engine = _fast_engine(client)

    with pytest.raises(EngineError) as excinfo:
        await engine.step([_user()], tools=[])

    assert not isinstance(excinfo.value, FallbackWorthwhile)


@pytest.mark.asyncio
async def test_a_stream_failure_before_any_output_is_fallback_worthwhile():
    """The other side of the same boundary: nothing was delivered, so the turn
    is intact and another model can still answer it."""
    client = _SequencedClient([_server_error()])
    engine = _fast_engine(client, max_retries=1)

    with pytest.raises(FallbackWorthwhile):
        await engine.step([_user()], tools=[])


# ---- G5-1: a context overflow is recoverable, but not here ------------------


def _context_overflow():
    import openai

    return openai.BadRequestError(
        "This model's maximum context length is 65536 tokens. "
        "However, you requested 70000 tokens",
        response=_Resp(400),
        body=None,
    )


@pytest.mark.asyncio
async def test_a_context_overflow_is_not_retried_here():
    """Asking again with the same messages earns the same rejection, so the
    retry budget would be spent learning nothing.  The loop compacts first."""
    client = _SequencedClient([_context_overflow()])
    engine = _fast_engine(client, max_retries=3)

    with pytest.raises(EngineError):
        await engine.step([_user()], tools=[])

    assert client.completions.calls == 1


@pytest.mark.asyncio
async def test_a_context_overflow_never_tries_another_model():
    """The invariant from M2 (docs spec §5) that G5-1 must not break.

    The same messages overflow every model in the chain, and ``ModelConfig``
    carries no window, so a fallback would be a blind guess that burns the chain
    before the loop gets a chance to compact.
    """
    client = _SequencedClient([_context_overflow()])
    engine = _fast_engine(client, max_retries=1)

    with pytest.raises(EngineError) as excinfo:
        await engine.step([_user()], tools=[])

    assert not isinstance(excinfo.value, FallbackWorthwhile)


@pytest.mark.asyncio
async def test_the_failure_carries_its_kind_rather_than_its_wording():
    """The loop decides whether to compact by reading ``kind``.  Re-deriving it
    from the message would make a human-readable string a contract -- the exact
    mistake M2 removed when it moved ``kind`` onto the exception."""
    client = _SequencedClient([_context_overflow()])
    engine = _fast_engine(client)

    with pytest.raises(EngineError) as excinfo:
        await engine.step([_user()], tools=[])

    assert excinfo.value.kind == "context_overflow"


@pytest.mark.asyncio
async def test_an_ordinary_bad_request_does_not_claim_to_be_an_overflow():
    """A 400 with nothing about size must not carry the kind that makes the loop
    rewrite the operator's history."""
    import openai

    client = _SequencedClient([
        openai.BadRequestError("invalid tool schema", response=_Resp(400), body=None)
    ])
    engine = _fast_engine(client)

    with pytest.raises(EngineError) as excinfo:
        await engine.step([_user()], tools=[])

    assert excinfo.value.kind != "context_overflow"
