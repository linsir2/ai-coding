"""Thinking timeline: the model's reasoning text survives the stack.

Three production claims, none about rendering (the transcript that used to
show it is gone; these are the data-layer invariants behind it):

* the engine hands the gateway's thinking through instead of dropping it;
* the session persists it, and reads back None when the key is absent;
* the request builder must never re-feed it to the model.
"""


def test_engine_accumulates_reasoning_deltas():
    """The gateway's thinking must survive the engine, not fall on the floor."""
    from types import SimpleNamespace

    from ai_coding.ai.openai_engine import OpenAIChatEngine
    from ai_coding.domain.message import ChatMessage
    from ai_coding.domain.model_config import ModelConfig

    def chunk(content=None, reasoning=None):
        delta = SimpleNamespace(content=content, tool_calls=None)
        if reasoning is not None:
            delta.reasoning_content = reasoning
        return SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=None)

    async def run(chunks):
        class FakeStream:
            def __init__(self):
                self._it = iter(chunks)

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self._it)
                except StopIteration:
                    raise StopAsyncIteration from None

        class FakeCompletions:
            async def create(self, **kwargs):
                return FakeStream()

        client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
        engine = OpenAIChatEngine(
            ModelConfig(name="m", base_url="https://example.test/v1", api_key="k"),
            client=client,
        )
        return await engine.step(
            [ChatMessage(role="user", content="hi", timestamp="t")], tools=[])

    import asyncio

    result = asyncio.run(run([chunk("hi", "let me "), chunk("there", "think")]))
    assert result.text == "hithere"
    assert result.reasoning == "let me think"
    bare = asyncio.run(run([chunk("hi")]))
    assert bare.reasoning is None


def test_reasoning_round_trips_through_the_session():
    """Persisted like everything else; absent key reads back None (compat)."""
    from ai_coding.domain.message import ChatMessage
    from ai_coding.domain.session import SessionData, _message_from_dict, _message_to_dict

    msg = ChatMessage(role="assistant", content="done", timestamp="t",
                      reasoning="because [k=v]")
    assert _message_to_dict(msg)["reasoning"] == "because [k=v]"
    assert _message_from_dict(_message_to_dict(msg)).reasoning == "because [k=v]"
    plain = ChatMessage(role="assistant", content="done", timestamp="t")
    assert "reasoning" not in _message_to_dict(plain)
    assert _message_from_dict({"role": "assistant", "content": "x",
                               "timestamp": "t"}).reasoning is None
    _ = SessionData  # session shape unchanged apart from the message key


def test_assistant_message_carries_reasoning_but_mapping_ignores_it():
    """The loop persists thinking; the request builder must never re-feed it."""
    from ai_coding.ai.mapping import to_chat_message
    from ai_coding.domain.loop import StepResult
    from ai_coding.domain.run import assistant_message

    step = StepResult(text="done", reasoning="hmm", finish_reason="stop")
    assert assistant_message(step, "t").reasoning == "hmm"
    wire = to_chat_message(assistant_message(step, "t"))
    assert "reasoning" not in wire
    assert wire["content"] == "done"







