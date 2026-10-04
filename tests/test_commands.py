"""Command palette: slash-command provider, open/close wiring, regression."""

from types import SimpleNamespace

import pytest
from textual.command import CommandPalette, DiscoveryHit
from textual.widgets import Input

from ai_coding.ui.commands import SLASH_COMMANDS, SlashCommandProvider
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
async def test_slash_opens_palette():
    app = _app()
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/"
        await pilot.pause()
        assert CommandPalette.is_open(app)


@pytest.mark.asyncio
async def test_discover_yields_every_slash_command():
    provider = SlashCommandProvider(SimpleNamespace(app=object()), None)
    hits = [hit async for hit in provider.discover()]
    assert len(hits) == len(SLASH_COMMANDS) == 18
    assert all(isinstance(hit, DiscoveryHit) for hit in hits)
    assert {hit.display for hit in hits} == {name for name, _ in SLASH_COMMANDS}


@pytest.mark.asyncio
async def test_search_empty_query_yields_every_command():
    provider = SlashCommandProvider(SimpleNamespace(app=object()), None)
    hits = [hit async for hit in provider.search("")]
    assert len(hits) == len(SLASH_COMMANDS) == 18


@pytest.mark.asyncio
async def test_search_filters_to_matching_commands():
    provider = SlashCommandProvider(SimpleNamespace(app=object()), None)
    hits = [hit async for hit in provider.search("plan")]
    assert any("plan" in (hit.text or "") for hit in hits)


@pytest.mark.asyncio
async def test_enter_executes_selected_command(monkeypatch):
    app = _app()
    seen: list[str] = []

    async def fake_handle_input(loop_, text, session, sessions, console):
        seen.append(text)
        from ai_coding.ui.repl import REPLAction

        return REPLAction.CONTINUE, session

    monkeypatch.setattr("ai_coding.ui.textual_app.handle_input", fake_handle_input)
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/"
        await pilot.pause()
        assert CommandPalette.is_open(app)
        for ch in "status":
            await pilot.press(ch)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        assert seen == ["/status"]


@pytest.mark.asyncio
async def test_palette_selection_records_the_user_turn(monkeypatch):
    app = _app()
    seen: list[str] = []

    async def fake_handle_input(loop_, text, session, sessions, console):
        seen.append(text)
        from ai_coding.ui.repl import REPLAction

        return REPLAction.CONTINUE, session

    monkeypatch.setattr("ai_coding.ui.textual_app.handle_input", fake_handle_input)
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/"
        await pilot.pause()
        assert CommandPalette.is_open(app)
        for ch in "status":
            await pilot.press(ch)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        assert seen == ["/status"]
        bubbles = app.query("Static.user-bubble")
        assert any("/status" in str(bubble.content) for bubble in bubbles)


@pytest.mark.asyncio
async def test_escape_closes_palette():
    app = _app()
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/"
        await pilot.pause()
        assert CommandPalette.is_open(app)
        await pilot.press("escape")
        await pilot.pause()
        assert not CommandPalette.is_open(app)


@pytest.mark.asyncio
async def test_input_cleared_after_palette_close():
    app = _app()
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/"
        await pilot.pause()
        assert CommandPalette.is_open(app)
        await pilot.press("escape")
        await pilot.pause()
        assert app.query_one("#prompt", Input).value == ""


@pytest.mark.asyncio
async def test_direct_slash_command_still_works(monkeypatch):
    app = _app()
    seen: list[str] = []

    async def fake_handle_input(loop_, text, session, sessions, console):
        seen.append(text)
        from ai_coding.ui.repl import REPLAction

        return REPLAction.CONTINUE, session

    monkeypatch.setattr("ai_coding.ui.textual_app.handle_input", fake_handle_input)
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/status"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        assert seen == ["/status"]
        assert not CommandPalette.is_open(app)


@pytest.mark.asyncio
async def test_slash_plan_does_not_open_palette():
    app = _app()
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/plan"
        await pilot.pause()
        assert not CommandPalette.is_open(app)
