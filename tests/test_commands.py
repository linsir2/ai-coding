"""Command palette: slash-command provider, open/close wiring, regression."""

from types import SimpleNamespace

import pytest
from textual.command import CommandPalette, DiscoveryHit
from textual.widgets import Input

from ai_coding.domain.session import SessionData
from ai_coding.ui.commands import (
    SLASH_COMMANDS,
    ResumeProvider,
    SlashCommandProvider,
)
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
    assert len(hits) == len(SLASH_COMMANDS) == 16
    assert all(isinstance(hit, DiscoveryHit) for hit in hits)
    assert {hit.display for hit in hits} == {name for name, _ in SLASH_COMMANDS}


@pytest.mark.asyncio
async def test_search_empty_query_yields_every_command():
    provider = SlashCommandProvider(SimpleNamespace(app=object()), None)
    hits = [hit async for hit in provider.search("")]
    assert len(hits) == len(SLASH_COMMANDS) == 16


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
async def test_resume_provider_displays_first_message_preview():
    """A resume entry must identify the conversation, not just its ID."""
    preview = (
        "Use the checkout flow to reproduce the payment failure, then write "
        "a regression test for refunds."
    )
    session = SessionData(
        session_id="0123456789abcdef0123456789abcdef",
        title="",
        created_time="2026-09-30T10:00:00Z",
        last_access_time="2026-09-30T10:44:00Z",
        message_count=5,
        first_preview=preview,
    )
    provider = ResumeProvider(
        SimpleNamespace(
            app=SimpleNamespace(
                _sessions=SimpleNamespace(list_sessions=lambda: [session]),
                submit_slash=lambda text: None,
            )
        ),
        None,
    )

    (hit,) = [hit async for hit in provider.discover()]

    assert hit.display == preview[:77] + "..."
    assert "0123456789ab" in hit.help
    assert "5 msgs" in hit.help
    assert "2026-09-30" in hit.help


@pytest.mark.asyncio
async def test_resume_provider_selection_resumes_full_session_id():
    """Choosing a truncated list entry must resume the exact saved session."""
    session_id = "0123456789abcdef0123456789abcdef"
    submitted: list[str] = []
    session = SessionData(
        session_id=session_id,
        title="",
        created_time="2026-09-30T10:00:00Z",
        last_access_time="2026-09-30T10:44:00Z",
        message_count=5,
        first_preview="Use the checkout flow to reproduce the payment failure.",
    )
    provider = ResumeProvider(
        SimpleNamespace(
            app=SimpleNamespace(
                _sessions=SimpleNamespace(list_sessions=lambda: [session]),
                submit_slash=submitted.append,
            )
        ),
        None,
    )

    (hit,) = [hit async for hit in provider.discover()]
    hit.command()

    assert submitted == [f"/resume {session_id}"]


@pytest.mark.asyncio
async def test_resume_provider_search_matches_preview_text():
    """Typing a word from the first message must narrow the list."""
    sessions = [
        SessionData(
            session_id="a" * 32,
            title="",
            created_time="2026-09-30T10:00:00Z",
            last_access_time="2026-09-30T10:44:00Z",
            message_count=3,
            first_preview="Fix the payment refund bug",
        ),
        SessionData(
            session_id="b" * 32,
            title="",
            created_time="2026-09-29T09:00:00Z",
            last_access_time="2026-09-29T09:30:00Z",
            message_count=8,
            first_preview="Write the onboarding docs",
        ),
    ]
    provider = ResumeProvider(
        SimpleNamespace(
            app=SimpleNamespace(
                _sessions=SimpleNamespace(list_sessions=lambda: sessions),
                submit_slash=lambda text: None,
            )
        ),
        None,
    )

    hits = [hit async for hit in provider.search("refund")]

    assert len(hits) == 1
    assert hits[0].text == "Fix the payment refund bug"


@pytest.mark.asyncio
async def test_resume_provider_search_also_matches_the_session_id():
    sessions = [
        SessionData(
            session_id="abcdef0123456789" + "0" * 16,
            title="",
            created_time="2026-09-30T10:00:00Z",
            last_access_time="2026-09-30T10:44:00Z",
            message_count=3,
            first_preview="Something unrelated",
        ),
        SessionData(
            session_id="f" * 32,
            title="",
            created_time="2026-09-29T09:00:00Z",
            last_access_time="2026-09-29T09:30:00Z",
            message_count=8,
            first_preview="Another conversation",
        ),
    ]
    provider = ResumeProvider(
        SimpleNamespace(
            app=SimpleNamespace(
                _sessions=SimpleNamespace(list_sessions=lambda: sessions),
                submit_slash=lambda text: None,
            )
        ),
        None,
    )

    hits = [hit async for hit in provider.search("abcdef0123")]

    assert len(hits) == 1
    assert hits[0].text == "Something unrelated"


@pytest.mark.asyncio
async def test_resume_provider_excludes_the_current_session():
    """Resuming the conversation already open is a no-op, so hide it."""
    current = SessionData(
        session_id="c" * 32,
        title="",
        created_time="2026-09-30T10:00:00Z",
        last_access_time="2026-09-30T10:44:00Z",
        message_count=3,
        first_preview="The conversation I am already in",
    )
    other = SessionData(
        session_id="d" * 32,
        title="",
        created_time="2026-09-29T09:00:00Z",
        last_access_time="2026-09-29T09:30:00Z",
        message_count=8,
        first_preview="A different conversation",
    )
    provider = ResumeProvider(
        SimpleNamespace(
            app=SimpleNamespace(
                _sessions=SimpleNamespace(list_sessions=lambda: [current, other]),
                _session=current,
                submit_slash=lambda text: None,
            )
        ),
        None,
    )

    hits = [hit async for hit in provider.discover()]

    assert [hit.text for hit in hits] == ["A different conversation"]


@pytest.mark.asyncio
async def test_slash_plan_does_not_open_palette():
    app = _app()
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/plan"
        await pilot.pause()
        assert not CommandPalette.is_open(app)
