"""A: typing during a running turn is queued, not dropped.

The input used to be disabled for the whole turn (the guard against two
concurrent `process_input` calls, spec §4.4).  That guard moves to a queue:
the input stays live, submissions during a turn wait, and the next one runs
when the current turn finishes.  These tests pin the observable behaviour and
the "never two turns at once" invariant.

The fake loop blocks on a gate the test controls, so "the turn is still
running" is deterministic rather than a sleep race.
"""

from __future__ import annotations

import asyncio

import pytest
from textual.widgets import Input, Static

from ai_coding.ui.textual_app import AICodingApp


def _app(**overrides):
    kwargs = {
        "loop": None,
        "session": object(),
        "sessions": object(),
        "workspace": "~/projects/ai-app",
        "model_name": "DeepSeek-V3",
    }
    kwargs.update(overrides)
    return AICodingApp(**kwargs)


class _GatedLoop:
    """Records call order; each call blocks until the test releases the gate.

    ``overlapped`` trips if a second turn ever starts while one is in flight.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.in_flight = False
        self.overlapped = False
        self.gate = asyncio.Event()

    async def process_input(self, *args, **kwargs):
        if self.in_flight:
            self.overlapped = True
        self.in_flight = True
        self.calls.append(args[1])
        await self.gate.wait()
        self.in_flight = False


async def _submit(pilot, app: AICodingApp, text: str) -> None:
    app.query_one("#prompt", Input).value = text
    await pilot.press("enter")
    await pilot.pause()


async def _wait_for(pilot, predicate, *, ticks: int = 50) -> None:
    for _ in range(ticks):
        await pilot.pause()
        if predicate():
            return


@pytest.mark.asyncio
async def test_input_stays_enabled_during_a_turn():
    loop = _GatedLoop()
    app = _app(loop=loop)
    async with app.run_test() as pilot:
        await _submit(pilot, app, "first")
        await _wait_for(pilot, lambda: loop.in_flight)
        assert loop.in_flight, "the turn never started"
        assert app.query_one("#prompt", Input).disabled is False, (
            "输入框在回合运行中被禁用了 —— 用户打不了字"
        )
        loop.gate.set()


@pytest.mark.asyncio
async def test_typing_during_a_turn_is_queued_and_runs_after():
    loop = _GatedLoop()
    app = _app(loop=loop)
    async with app.run_test() as pilot:
        await _submit(pilot, app, "first")
        await _wait_for(pilot, lambda: loop.in_flight)
        assert loop.calls == ["first"]
        await _submit(pilot, app, "second")
        assert loop.calls == ["first"], "第二条在第一条没结束时就开始跑了"
        loop.gate.set()
        await _wait_for(pilot, lambda: len(loop.calls) == 2)
        assert loop.calls == ["first", "second"], "排队的那条没有按序执行"
        assert loop.overlapped is False, "两个回合并发执行了"


@pytest.mark.asyncio
async def test_submit_when_idle_runs_immediately():
    loop = _GatedLoop()
    loop.gate.set()
    app = _app(loop=loop)
    async with app.run_test() as pilot:
        await _submit(pilot, app, "solo")
        await _wait_for(pilot, lambda: loop.calls == ["solo"])
        assert loop.calls == ["solo"]


@pytest.mark.asyncio
async def test_resume_is_refused_while_busy():
    loop = _GatedLoop()
    app = _app(loop=loop)
    async with app.run_test() as pilot:
        await _submit(pilot, app, "first")
        await _wait_for(pilot, lambda: loop.in_flight)
        await _submit(pilot, app, "/resume")
        await pilot.pause()
        # refused, not queued: no second call, and a notice explains why
        assert loop.calls == ["first"]
        assert list(app._queued) == [], "/resume 被排队了，应当直接拒绝"
        texts = [
            (w.content.plain if hasattr(w.content, "plain") else str(w.content))
            for w in app.query(Static)
        ]
        assert any("resume" in t for t in texts), "拒绝 /resume 时没有给出提示"
        loop.gate.set()
