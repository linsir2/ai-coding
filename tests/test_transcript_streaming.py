"""Streaming prose must survive the Turn pipeline intact (WP4 repro).

Production evidence (``sessions/52a7a0c2…json`` vs screenshot): the session holds
a ~500-char assistant answer, the transcript rendered only its head (~60 chars)
after the turn completed (``Ready``).  The session text comes from
``StepResult.text`` (``agent_loop.py``), the transcript from ``on_delta`` chunks
through ``Turn.stream / flush / end`` (``transcript.py``) — so the loss is
between the delta sink and the ``Markdown`` DOM.

These tests drive the real ``AICodingApp`` (50ms flush timer included) with a
fake loop that streams like a real engine: many small deltas with real-time gaps
so the timer fires mid-stream, optionally straddling a tool call (the ``_stale``
path).  The assertion compares what was streamed against ``Markdown.source``.
On failure the message prints all three lengths (streamed / pending / DOM) so
the drop point is visible without re-instrumenting.
"""

from __future__ import annotations

import asyncio

import pytest
from textual.widgets import Collapsible

from ai_coding.core.events import ToolFinished, ToolStarted
from ai_coding.ui.blocks import blocks_from_event
from ai_coding.ui.transcript import Prose, Transcript, Turn
from tests.test_textual_app import _app

HEAD = "最近一次 commit 是 **f9fd27618b9dc656c4fc1b98f927c82cd449167f**（作者 linsir2)：\n\n"
BODY = "**提交信息：**\n> docs: three numbers and one function name a reader could rely on\n\n"
TAIL = (
    "**改动文件（3 个）：**\n- `ai_coding/memory/store.py`\n"
    "- `docs/data-contracts.md`\n- `docs/features.md`\n"
)
FULL = HEAD + BODY + TAIL
assert len(FULL) > 200  # the point: well above any preview constant, below _FLUSH_CHARS

# Byte-for-byte production payload from sessions/52a7a0c2…json (the turn that
# rendered ~2 lines).  If the pipeline is content-sensitive, this must redden.
PRODUCTION_ANSWER = (
    "最近一次 commit 是 **f9fd27618b9dc656c4fc1b98f927c82cd449167f**"
    "（作者 linsir2，2026-10-02 01:01:17 +0800）：\n\n**提交信息：**\n"
    "> docs: three numbers and one function name a reader could rely on\n\n"
    "**说明：** 这是一个文档修正 commit：\n"
    "- \"Six candidates touched is six uses, not nine.\"\n"
    "- \"The threshold gate is implied by the band floor at shipped defaults...\"\n"
    "- \"recall_with_diagnostics is the function with no await; recall_notes has one.\"\n\n"
    "**改动文件（3 个，共 3 处 +3/-3）：**\n"
    "- `ai_coding/memory/store.py`\n- `docs/data-contracts.md`\n- `docs/features.md`"
)


def _chunks(text: str, size: int = 7) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


class _StreamingLoop:
    """Fake engine: many small deltas with real gaps, optionally split by a tool."""

    def __init__(self, *, with_tool: bool, payload: str = FULL) -> None:
        self._with_tool = with_tool
        self._payload = payload
        self.sent: list[str] = []

    async def process_input(self, session, text, *, on_delta=None, observer=None):
        assert on_delta is not None
        deltas = _chunks(self._payload)
        half = len(deltas) // 2
        for i, piece in enumerate(deltas):
            on_delta(piece)
            self.sent.append(piece)
            if i == half and self._with_tool and observer is not None:
                started = blocks_from_event(
                    ToolStarted(call_id="c1", tool_name="bash", params={"command": "git log -1"})
                )
                assert started is not None
                observer.on_event(
                    ToolStarted(call_id="c1", tool_name="bash", params={"command": "git log -1"})
                )
                finished = blocks_from_event(
                    ToolFinished(
                        call_id="c1", tool_name="bash", success=True, output_preview="done"
                    )
                )
                assert finished is not None
                observer.on_event(
                    ToolFinished(
                        call_id="c1", tool_name="bash", success=True, output_preview="done"
                    )
                )
            if i % 10 == 9:
                await asyncio.sleep(0.06)  # let the 50ms flush timer fire mid-stream
        return self._payload

    async def aclose(self):
        return None


def _dom_text(app) -> tuple[str, int]:
    """(concatenated prose sources, leftover _pending across turns)."""
    sources = "\n".join(prose.source for prose in app.query(Prose))
    pending = sum(len(turn._pending) for turn in app.query(Turn))
    return sources, pending


async def _run(app, pilot, text: str = "最近一次commit是啥") -> None:
    app.query_one("#prompt").value = text
    await pilot.press("enter")
    for _ in range(12):
        await pilot.pause()


@pytest.mark.asyncio
@pytest.mark.parametrize("with_tool", [False, True])
async def test_streamed_prose_survives_the_turn_intact(with_tool: bool):
    loop = _StreamingLoop(with_tool=with_tool)
    app = _app(loop=loop)
    async with app.run_test() as pilot:
        await _run(app, pilot)
        sources, pending = _dom_text(app)
        streamed = "".join(loop.sent)
        assert streamed == loop._payload, "harness bug: fake loop did not stream fully"
        assert pending == 0, f"_pending leaked {pending} chars past end_assistant"
        assert HEAD.strip()[:20] in sources, (
            f"head lost — streamed {len(streamed)}, DOM {len(sources)}, pending {pending}"
        )
        assert "docs/features.md" in sources, (
            f"TAIL LOST — streamed {len(streamed)}, DOM {len(sources)}, pending {pending}\n"
            f"DOM was:\n{sources}"
        )
        assert len(sources) >= len(loop._payload), (
            f"truncated — streamed {len(streamed)}, DOM {len(sources)}, pending {pending}"
        )


@pytest.mark.asyncio
async def test_rendered_markdown_height_matches_its_source():
    """``.source`` full but visually 2 lines is still a truncation (WP4-render).

    Production showed ~2 rendered lines with the ``[turn]`` markers directly
    below — i.e. the ``Markdown`` widget itself was short, not merely
    scrolled.  This pins the rendered height, not just the text buffer.
    """
    loop = _StreamingLoop(with_tool=True)
    app = _app(loop=loop)
    async with app.run_test(size=(80, 24)) as pilot:
        await _run(app, pilot)
        await pilot.pause()
        await pilot.pause()
        mds = list(app.query(Prose))
        # A tool call mid-turn splits prose across Prose widgets
        # (Turn finalizes the current block in start_tool), so aggregate.
        height = sum(md.outer_size.height for md in mds)
        total_source = sum(len(md.source) for md in mds)
        assert total_source >= len(FULL), (
            f"text buffer lost chars: {total_source} < {len(FULL)}"
        )
        assert height >= 10, (
            f"Markdown rendered {height} lines in {len(mds)} widgets "
            f"for a ~16-line answer; sources hold {total_source} chars"
        )


@pytest.mark.asyncio
async def test_outer_scroll_survives_an_expanded_tool_fold():
    """Opening a fold must not trap the scroll (WP3-scroll).

    Production: after expanding the ``bash`` fold the wheel did nothing and the
    earlier content was unreachable.  The fold's ``RichLog`` (focusable by
    default) swallows scroll events meant for the transcript.
    """
    loop = _StreamingLoop(with_tool=True)
    app = _app(loop=loop)
    async with app.run_test(size=(80, 24)) as pilot:
        await _run(app, pilot)
        transcript = app.query_one(Transcript)
        col = app.query(Collapsible)[0]
        col.collapsed = False
        await pilot.pause()
        transcript.scroll_home(animate=False)
        await pilot.pause()
        assert transcript.scroll_y == 0, "outer transcript no longer scrolls after expand"
        transcript.scroll_end(animate=False)
        await pilot.pause()
        assert transcript.is_vertical_scroll_end

@pytest.mark.asyncio
async def test_production_payload_replays_intact():
    """Exact production answer, byte-for-byte, through the same pipeline."""
    loop = _StreamingLoop(with_tool=True, payload=PRODUCTION_ANSWER)
    app = _app(loop=loop)
    async with app.run_test(size=(80, 24)) as pilot:
        await _run(app, pilot)
        await pilot.pause()
        sources, pending = _dom_text(app)
        assert pending == 0
        assert "改动文件" in sources, f"production tail lost — DOM:\n{sources}"
        assert len(sources) >= len(PRODUCTION_ANSWER), (
            f"production truncated — streamed {len(PRODUCTION_ANSWER)}, "
            f"DOM {len(sources)}, pending {pending}"
        )


@pytest.mark.asyncio
async def test_overflow_turns_keep_a_working_scroll():
    """WP4b: overflowing prose must engage the scrollbar and stay reachable.

    Textual's ``Markdown`` grows asynchronously after mount and the outer
    ``VerticalScroll`` never counts that growth: no scrollbar, dead wheel,
    earlier turns unreachable ("上一轮消失").  This pins the scroll contract
    at the Transcript level so the widget behind it can be swapped freely.
    """
    from ai_coding.ui.transcript import Transcript

    prose = ("回答甲乙丙丁戊己庚辛壬癸" * 4 + "。\n" + "z" * 100 + "\n") * 6
    assert len(prose) > 800

    app = _app(loop=None)
    async with app.run_test(size=(80, 16)) as pilot:
        transcript = app.query_one(Transcript)
        for _ in range(2):
            transcript.begin_assistant("10:25")
            for i in range(0, len(prose), 40):
                transcript.stream(prose[i : i + 40])
                if i % 400 == 0:
                    await pilot.pause()
            await transcript.end_assistant()
            for _ in range(6):
                await pilot.pause()
        assert transcript.show_vertical_scrollbar, "overflow produced no scrollbar"
        transcript.scroll_end(animate=False)
        await pilot.pause()
        assert transcript.is_vertical_scroll_end
        transcript.scroll_home(animate=False)
        await pilot.pause()
        assert transcript.scroll_y == 0, "cannot scroll back to earlier turns"
        # Replace-on-flush must not leak widgets: one Prose per turn here.
        from ai_coding.ui.transcript import Prose

        assert len(app.query(Prose)) == 2
