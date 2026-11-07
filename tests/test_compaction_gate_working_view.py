"""A: the compaction gate must measure the request that will actually be sent.

Measured live on 2026-10-30 (hotel wave run, qwen3.8-flash configured without a
``context_window``): one turn of 21 iterations emitted 21 ``CompactionStarted``
events and paid five summaries, because the gate read
``_history_tokens(session.messages)`` -- and the budget path only ever compacts a
*copy*, so ``session.messages`` never shrinks.  Once the original history crossed
the ceiling, every later iteration looked over budget again, while the provider
reported ~30k in a 48k window.

The rule these tests pin:

* when the provider (or the in-turn step reading) has a real measurement, that
  number is *the* measurement;
* the character estimate is only the cold-start fallback;
* the fallback itself counts the checkpoint plus the messages after it, not the
  full history the checkpoint already stands for.
"""

import asyncio
import tempfile
from pathlib import Path

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.context_compression import (
    ContextManager,
    _anchor_of,
    _history_tokens,
    install_checkpoint,
    working_history_tokens,
)
from ai_coding.core.request_assembly import _LiveReadingWatch, _observed_prompt_tokens
from ai_coding.domain.loop import StepResult
from ai_coding.domain.message import ChatMessage
from ai_coding.domain.session import SessionData
from ai_coding.service.session_service import SessionService
from ai_coding.tools.registry import ToolRegistry

WINDOW = 48_000  # ceiling = 32_000
CEILING = 32_000


def _manager() -> ContextManager:
    return ContextManager(
        AIConfig(**{"max_context_tokens": WINDOW}),
        transcripts_root=Path(tempfile.mkdtemp()),
        summarizer=None,
    )


def _big_history(chars: int = 140_000) -> list[ChatMessage]:
    """One message whose character estimate is above the ceiling."""
    return [ChatMessage(role="user", content="x" * chars, timestamp="")]


def _session(messages: list[ChatMessage] | None = None) -> SessionData:
    session = SessionData(
        session_id="s1", title="", created_time="2026-01-01T00:00:00Z", last_access_time=""
    )
    session.messages = list(messages or [])
    return session


# ---- 1. the provider number wins over a larger estimate --------------------


def test_the_gate_opens_on_the_provider_count_when_the_estimate_is_larger():
    """The measured defect: estimate 35k (over the 32k ceiling) while the
    provider says the request is 30k.  Compacting here buys nothing and costs a
    summary call, so the gate must stay shut."""
    manager = _manager()
    history = _big_history()

    assert manager.should_compact(history) is True, "by estimate alone: over"
    assert manager.should_compact(history, observed_tokens=30_000) is False, (
        "the provider measured the live request; the stale estimate must not reopen the gate"
    )


def test_the_gate_opens_when_the_provider_count_is_over_the_ceiling():
    manager = _manager()

    assert manager.should_compact(_big_history(), observed_tokens=CEILING + 1) is True


def test_without_a_provider_count_the_gate_uses_the_estimate():
    manager = _manager()

    assert manager.should_compact(_big_history(), observed_tokens=0) is True
    assert manager.should_compact(
        [ChatMessage(role="user", content="small", timestamp="")], observed_tokens=0
    ) is False


def test_a_measured_saturation_needs_substantial_growth_to_reopen():
    """R2-2: a 34k request in a 32k ceiling was compacted, then 35k -- a few
    hundred tokens of new tool output -- reopened the gate on the next step.
    The saturation is recorded in the *provider's* measure, so only real
    growth buys another pass."""
    manager = _manager()
    manager._saturated_at_tokens = CEILING + 5_000
    history = [ChatMessage(role="user", content="x" * 4_000, timestamp="")]

    assert manager.should_compact(history, observed_tokens=CEILING + 5_500) is False
    assert manager.should_compact(history, observed_tokens=CEILING + 8_000) is True


def test_compress_if_needed_records_the_measured_size_as_saturation():
    """The pass that opened on the provider count must record that count --
    not the character estimate, which is a different unit."""
    manager = _manager()
    messages = [ChatMessage(role="user", content="x" * 200, timestamp="")]

    asyncio.run(manager.compress_if_needed(messages, observed_tokens=CEILING + 1))
    assert manager._saturated_at_tokens >= CEILING + 1
    assert manager.should_compact(messages, observed_tokens=CEILING + 1) is False


# ---- 2. the fallback counts the checkpoint, not the full history -----------


def test_working_history_tokens_counts_the_checkpoint_and_the_delta_only():
    messages = [
        ChatMessage(role="user", content=f"消息{index}" + "内容" * 500, timestamp="")
        for index in range(200)
    ]
    session = _session(messages)
    install_checkpoint(
        session,
        "早期对话的摘要",
        consumed=180,
        anchor=_anchor_of(messages[:180]),
    )

    working = working_history_tokens(session)
    full = _history_tokens(messages)

    assert full > CEILING, "the fixture must start over the ceiling"
    assert working < full // 2, (
        f"the checkpoint already stands for 180 of 200 messages; "
        f"working={working} full={full}"
    )


def test_working_history_tokens_without_a_checkpoint_is_the_full_history():
    messages = [ChatMessage(role="user", content="x" * 4_000, timestamp="")]

    assert working_history_tokens(_session(messages)) == _history_tokens(messages)


def test_a_checkpoint_does_not_make_the_gate_ignore_the_provider_count():
    """R2-2r: a 135k *estimate* against an 11k *bill* compacted every other step.

    The estimate counts tool bodies before L2/L3 shrink them; the provider
    measured the request that actually went out.  Once a checkpoint exists the
    gate used to discard the measured number entirely and fall back to the
    inflated estimate.
    """
    messages = [
        ChatMessage(
            role="tool",
            content="大段工具输出" * 8_000,
            tool_call_id=f"c{index}",
            timestamp="",
        )
        for index in range(40)
    ]
    session = _session(messages)
    install_checkpoint(
        session,
        "早期对话的摘要",
        consumed=39,
        anchor=_anchor_of(messages[:39]),
    )

    manager = _manager()
    # The provider measured the request that went out: it fits.
    assert manager.should_compact(messages, observed_tokens=CEILING - 1_000) is False
    # And a genuine overflow still opens the gate.
    assert manager.should_compact(messages, observed_tokens=CEILING + 1) is True


# ---- 3. the loop records the real measurement after every step -------------


class _Engine(ChatEngine):
    def __init__(self, steps):  # type: ignore[no-untyped-def]
        self._steps = list(steps)

    async def step(self, messages, *, tools, instructions=None, on_delta=None):  # type: ignore[no-untyped-def]
        return self._steps.pop(0)

    async def aclose(self) -> None:
        return None


def _loop(engine, tmp_path):  # type: ignore[no-untyped-def]
    return AgentLoop(
        engine,
        SessionService(tmp_path / "sessions"),
        ToolRegistry(),
        workspace=str(tmp_path),
        settings=AIConfig(stop_policy="natural", max_tool_iterations=3),
    )


@pytest.mark.asyncio
async def test_the_loop_records_the_provider_context_after_each_step(tmp_path):
    engine = _Engine(
        [
            StepResult(
                text="done",
                finish_reason="stop",
                usage={
                    "prompt_tokens": 30_000,
                    "completion_tokens": 10,
                    "total_tokens": 30_010,
                    "context_tokens": 29_586,
                },
            )
        ]
    )
    session = _session()

    await _loop(engine, tmp_path).process_input(session, "hi")

    assert session.live_context_tokens == 29_586, (
        "the gate and the meter need the step's own reading, not the end-of-turn aggregate"
    )


@pytest.mark.asyncio
async def test_a_provider_without_context_tokens_falls_back_to_the_step_prompt(tmp_path):
    """DashScope's compatible mode reports only prompt/completion/total.  Zero
    here would send the gate back to the raw-history estimate on every step --
    the exact thrash this file exists to stop."""
    engine = _Engine(
        [
            StepResult(
                text="done",
                finish_reason="stop",
                usage={"prompt_tokens": 11_185, "completion_tokens": 1_381, "total_tokens": 12_566},
            )
        ]
    )
    session = _session()

    await _loop(engine, tmp_path).process_input(session, "hi")

    assert session.live_context_tokens == 11_185


# ---- 4. the in-turn reading is transient -----------------------------------


def test_live_prompt_tokens_prefers_the_in_turn_measurement():
    session = _session()
    session.live_context_tokens = 12_345

    assert session.live_prompt_tokens() == 12_345

    session.live_context_tokens = 0
    assert session.live_prompt_tokens() == 0, "no usage row yet: the old reading"


def test_live_context_tokens_is_not_persisted():
    session = _session()
    session.live_context_tokens = 999

    assert "liveContextTokens" not in session.to_dict(), (
        "a process-local measurement must not enter the on-disk format"
    )


# ---- 5. the assembler's observed value prefers live, then the working view --


def test_observed_prompt_tokens_prefers_the_live_measurement():
    session = _session(_big_history())
    session.live_context_tokens = 12_345

    assert _observed_prompt_tokens(session) == 12_345


def test_observed_prompt_tokens_falls_back_to_the_working_view():
    messages = [
        ChatMessage(role="user", content=f"消息{index}" + "内容" * 500, timestamp="")
        for index in range(200)
    ]
    session = _session(messages)
    install_checkpoint(session, "摘要", consumed=180, anchor=_anchor_of(messages[:180]))

    observed = _observed_prompt_tokens(session)

    assert observed == working_history_tokens(session), (
        "with no provider reading, the estimate must describe the checkpoint view"
    )
    assert observed < _history_tokens(messages)


def test_observed_prompt_tokens_ignores_a_previous_turns_measurement():
    """956c45cb: a new turn inherited the last closed turn's 24k reading.

    The synthetic-message loop grew the raw history to 2.45M tokens without a
    model step, so ``live_context_tokens`` was 0.  The gate then read turn 1's
    usage row and skipped compaction, sending the whole poisoned history; the
    provider timed out twice.  A row from an older turn describes a request
    that no longer exists.
    """
    messages = [
        ChatMessage(role="user", content=f"消息{index}" + "内容" * 500, timestamp="")
        for index in range(200)
    ]
    session = _session(messages)
    session.turn_count = 3
    session.record_turn_usage(1, {"prompt_tokens": 24_916, "context_tokens": 24_916})

    observed = _observed_prompt_tokens(session)

    assert observed == working_history_tokens(session)
    assert observed > CEILING
    assert _manager().should_compact(messages, observed) is True


# ---- 6. CMP-05: a frozen provider reading must not silence the gate --------


def test_a_frozen_reading_is_downgraded_after_three_repeats():
    watch = _LiveReadingWatch()
    session = _session()
    session.live_context_tokens = 4_978
    local = 10_000
    returned = []
    locals_seen = []
    for _ in range(4):
        locals_seen.append(local)
        returned.append(watch.observe(session, local))
        local = int(local * 1.06)

    assert returned[:3] == [None, None, None], "three consistent repeats first"
    assert returned[3] == locals_seen[3], "the fourth call uses the local estimate"


def test_a_changed_reading_restarts_the_count():
    watch = _LiveReadingWatch()
    session = _session()
    session.live_context_tokens = 4_978
    watch.observe(session, 10_000)
    watch.observe(session, 10_600)
    watch.observe(session, 11_236)

    session.live_context_tokens = 5_500
    assert watch.observe(session, 11_910) is None, "a new reading is trusted again"
    assert watch.observe(session, 12_625) is None


def test_the_count_resets_when_the_turn_changes():
    watch = _LiveReadingWatch()
    session = _session()
    session.turn_count = 1
    session.live_context_tokens = 4_978
    for local in (10_000, 10_600, 11_236, 11_910):
        watch.observe(session, local)
    assert watch._repeats == 3  # noqa: SLF001 - the heuristic under test

    session.turn_count = 2
    assert watch.observe(session, 12_625) is None
    assert watch._repeats == 0  # noqa: SLF001 - a new turn observes afresh


def test_observed_prompt_tokens_falls_back_to_the_working_view_when_frozen():
    session = _session([ChatMessage(role="user", content="x" * 4_000, timestamp="")])
    session.live_context_tokens = 4_978
    watch = _LiveReadingWatch()
    observed_values = []
    local_values = []
    for _ in range(4):
        observed_values.append(_observed_prompt_tokens(session, watch=watch))
        local_values.append(working_history_tokens(session))
        session.messages.append(
            ChatMessage(role="user", content="y" * 4_000, timestamp="")
        )

    assert observed_values[:3] == [4_978, 4_978, 4_978]
    assert observed_values[3] == local_values[3]


def test_a_live_reading_does_not_scan_the_whole_history(monkeypatch):
    """P2-1: the provider's count is the answer, so the O(n) local estimate is
    only paid once the frozen-reading heuristic can actually use it.  The old
    code called ``working_history_tokens`` on every iteration with a live
    reading, rescanning the entire history for a number it usually discarded."""
    from ai_coding.core import request_assembly

    session = _session(_big_history())
    session.live_context_tokens = 12_345
    calls: list[int] = []
    monkeypatch.setattr(
        request_assembly,
        "working_history_tokens",
        lambda _session: calls.append(1) or 1,
    )

    observed = _observed_prompt_tokens(session, watch=_LiveReadingWatch())

    assert observed == 12_345
    assert calls == [], "a moving/first reading must not scan the history"

