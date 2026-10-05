"""Thinking timeline (Project A, TDD): make the process visible.

Two gaps, both measured against the reference screenshots (icon rows with
text interleaved) rather than today's transcript (tool cards + final prose
and nothing between):

* A1: the model's reasoning text is dropped on the floor — the engine reads
  ``delta.content`` only.  Completed reasoning per step renders as one
  collapsed dim fold ("thinking first" is not faked: the gateway only ever
  hands us completed text per step, so streaming fidelity is explicitly out
  of scope for v1).
* A2: delegated work is invisible in the TUI transcript — child events ride
  ``ScopedEvent`` since R0-3 but ``blocks_from_event`` drops them all.
"""

import pytest

from ai_coding.core.events import (
    AssistantMessage,
    ModelStepFinished,
    ScopedEvent,
    ToolFinished,
    ToolStarted,
)


def _started():
    return ToolStarted(call_id="c1", tool_name="bash",
                       params={"command": "ls"}, iteration=1)


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


def test_plain_assistant_message_still_maps_to_nothing():
    """Prose travels the delta stream — mapping the event too would double it."""
    from ai_coding.ui.blocks import blocks_from_event

    assert blocks_from_event(AssistantMessage(text="hi", tool_calls=())) is None


def test_reasoning_assistant_message_maps_to_a_capped_dim_block():
    from ai_coding.ui.blocks import blocks_from_event

    block = blocks_from_event(AssistantMessage(
        text="hi", tool_calls=(), reasoning="plan [k=v] first"))
    assert block is not None and block.kind == "reasoning" and block.style == "dim"
    assert "plan [k=v] first" in block.text
    long = blocks_from_event(AssistantMessage(
        text="hi", tool_calls=(), reasoning="r" * 5000))
    assert long is not None and len(long.text) < 5000
    assert "完整内容见会话文件" in long.text


def test_scoped_tool_events_render_as_nested_lines():
    from ai_coding.ui.blocks import blocks_from_event

    line = blocks_from_event(ScopedEvent(scope="subagent:probe", parent="root",
                                         event=_started()))
    assert line is not None and "subagent:probe" in line.text and "ls" in line.text
    done = blocks_from_event(ScopedEvent(
        scope="s", parent="p",
        event=ToolFinished(call_id="c1", tool_name="bash", success=False,
                           output_preview="", error="boom")))
    assert done is not None and "✗" in done.text


def test_scoped_noise_stays_silent():
    """Chunks and telemetry stay in the trace — the transcript is not a log."""
    from ai_coding.ui.blocks import blocks_from_event

    assert blocks_from_event(ScopedEvent(
        scope="s", parent="p", event=_chunk())) is None
    assert blocks_from_event(ScopedEvent(
        scope="s", parent="p", event=ModelStepFinished(index=1))) is None


def _chunk():
    from ai_coding.core.events import ToolChunk

    return ToolChunk(call_id="c1", tool_name="bash", stream="stdout", text="x")


def test_scoped_assistant_text_is_capped_and_silent_when_empty():
    from ai_coding.ui.blocks import blocks_from_event

    full = blocks_from_event(ScopedEvent(
        scope="s", parent="p",
        event=AssistantMessage(text="x" * 500, tool_calls=())))
    assert full is not None and len(full.text) <= 200
    empty = blocks_from_event(ScopedEvent(
        scope="s", parent="p", event=AssistantMessage(text=None, tool_calls=())))
    assert empty is None


@pytest.mark.asyncio
async def test_reasoning_block_mounts_a_collapsed_thinking_fold():
    """One collapsed fold per step — qwen-class reasoning runs long."""
    from textual.app import App, ComposeResult

    from ai_coding.ui.blocks import Block
    from ai_coding.ui.transcript import Transcript

    class _Harness(App[None]):
        def compose(self) -> ComposeResult:
            yield Transcript()

    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:25")
        t.append_block(Block("reasoning", "plan [k=v] first", "dim"))
        await pilot.pause()
        folds = list(t.query(".reasoning-fold"))
        assert len(folds) == 1 and folds[0].collapsed is True
        assert "思考过程" in str(folds[0].title)


@pytest.mark.asyncio
async def test_replay_renders_reasoning_as_a_collapsed_fold():
    from textual.app import App, ComposeResult

    from ai_coding.domain.message import ChatMessage
    from ai_coding.ui.transcript import Transcript

    class _Harness(App[None]):
        def compose(self) -> ComposeResult:
            yield Transcript()

    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        await t.replay_history([
            ChatMessage(role="assistant", content="done [k=v]",
                        timestamp="t", reasoning="because reasons"),
        ])
        await pilot.pause()
        folds = list(t.query(".reasoning-fold"))
        assert len(folds) == 1 and folds[0].collapsed is True
