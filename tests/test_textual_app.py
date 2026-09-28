"""The Textual shell: status bar, transcript area, input bar."""

import pytest
from textual.widgets import Input

from ai_coding.ui.status_bar import StatusBar
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


@pytest.mark.asyncio
async def test_shell_mounts_with_status_transcript_and_input():
    app = _app()
    async with app.run_test():
        assert app.query_one(StatusBar)
        assert app.query_one("#transcript")
        prompt = app.query_one("#prompt", Input)
        assert prompt.placeholder == "Type a message or enter a command..."
        assert app.query_one("#input-hint").content == "/ for commands"
        assert "Ready" in app.query_one("#status-right").content.plain
        assert "~/projects/ai-app" in app.query_one("#status-left").content.plain


@pytest.mark.asyncio
async def test_status_bar_switches_to_working():
    app = _app()
    async with app.run_test() as pilot:
        bar = app.query_one(StatusBar)
        bar.set_busy(True)
        await pilot.pause()
        assert "Working" in app.query_one("#status-right").content.plain
