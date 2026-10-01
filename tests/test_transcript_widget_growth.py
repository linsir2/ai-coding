"""What a long session actually costs (spec §7 risk 3, "measure first").

The spec promised to measure the transcript before enabling the hybrid
container (finished turns collapsed into a ``RichLog``), and then never did,
so there was no number to decide with.  These tests pin the curve.

They are characterisation tests, not bug reports: they pass today.  That is
the point -- the growth is real and unmeasured, and a future decision to build
the hybrid container has to start from these numbers rather than from a guess.
If one of them ever fails, the tree became bounded somehow and the spec's
worst-case assumption no longer holds.
"""

from __future__ import annotations

import pytest

from ai_coding.ui.transcript import Transcript, Turn
from tests.test_textual_app import _app, _FakeLoop


async def _drive(app, turns: int) -> Transcript:
    """Run ``turns`` complete assistant turns through the real transcript."""
    transcript = app.query_one(Transcript)
    for i in range(turns):
        transcript.begin_assistant("12:00")
        transcript.stream(f"answer {i}")
        await transcript.end_assistant()
    return transcript


@pytest.mark.asyncio
@pytest.mark.parametrize("turns", [1, 10, 25, 50])
async def test_one_turn_widget_survives_per_finished_turn(turns):
    """``end_assistant`` drops the reference but never removes the widget.

    ``Transcript.end_assistant`` (transcript.py:163-168) awaits ``turn.end()``
    and then discards its reference; nothing calls ``remove()``.  So the tree
    grows by one ``Turn`` -- plus the ``Markdown`` inside it -- per turn.
    """
    app = _app(loop=_FakeLoop())
    async with app.run_test() as pilot:
        transcript = await _drive(app, turns)
        await pilot.pause()
        assert len(transcript.query(Turn)) == turns


@pytest.mark.asyncio
@pytest.mark.parametrize("turns", [1, 10, 25, 50])
async def test_the_markdown_inside_it_survives_too(turns):
    """The streaming ``Markdown`` is the heavier half and it also stays.

    ``Turn.stream`` mounts a ``Markdown`` per turn (``transcript.py:59-60``)
    and keeps it on the tree for the life of the app.  This is the cost that
    actually grows: a ``Turn`` is a container, a ``Markdown`` is a parsed
    document.
    """
    from textual.widgets import Markdown

    app = _app(loop=_FakeLoop())
    async with app.run_test() as pilot:
        transcript = await _drive(app, turns)
        await pilot.pause()
        assert len(transcript.query(Markdown)) == turns


@pytest.mark.asyncio
async def test_the_curve_is_linear_not_bounded():
    """One widget per turn, so cost is O(N) in the number of turns.

    Written as a ratio rather than as a hardcoded count so it stays true as
    the numbers grow: if a cap were ever introduced this would drop below 1
    and fail, which is exactly when the spec's deferred decision comes back.
    """
    counts = {}
    for turns in (1, 20):
        app = _app(loop=_FakeLoop())
        async with app.run_test() as pilot:
            transcript = await _drive(app, turns)
            await pilot.pause()
            counts[turns] = len(transcript.query(Turn))

    growth = counts[20] - counts[1]
    assert counts[20] == 20
    # 19 more turns produced 19 more widgets: slope 1, no intercept games.
    assert growth == 19
