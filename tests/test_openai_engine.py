"""TDD (R1a-2): OpenAIChatEngine — one model step, driven by an injected client.

Everything here is offline: the fake client's ``chat.completions.create`` returns
an async-iterable of chunk-shaped objects, so the streaming assembly logic is
exercised without touching the network or needing an API key.
"""

from types import SimpleNamespace

import pytest

from ai_coding.ai.engine import EngineError
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
