"""The resume picker screen and history replay (TDD: resume overhaul).

Bare ``/resume`` no longer uses the Textual command palette — a palette row
cannot carry a date group header — so it pushes ``ResumeScreen``.  Choosing a
session must also make its history visible: the transcript is cleared and
rebuilt from the loaded messages (tool cards collapsed, final text only —
old sessions never stored the live chunks, so replaying a stream would be
fiction).
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from textual.widgets import Input, OptionList

from ai_coding.domain.message import ChatMessage, ToolCallRef, ToolOutput
from ai_coding.domain.run import tool_message
from ai_coding.domain.session import SessionData
from ai_coding.ui.resume_screen import ResumeScreen
from ai_coding.ui.textual_app import AICodingApp
from ai_coding.ui.transcript import Prose

_NOW = datetime.now(timezone.utc)
_YESTERDAY_NOON = (_NOW.astimezone() - timedelta(days=1)).replace(
    hour=12, minute=0, second=0, microsecond=0
)


def _session(sid, last_access, preview, messages=()):
    return SessionData(
        session_id=sid,
        title="",
        created_time=last_access,
        last_access_time=last_access,
        messages=list(messages),
        message_count=len(messages),
        first_preview=preview,
    )


def _history():
    """A saved turn: user text with brackets, an assistant step that ran a
    tool, the tool result, and the follow-up prose — also with brackets."""
    return [
        ChatMessage(
            role="user", content="看看日志 [INFO] env", timestamp="2026-10-03T08:01:00Z"
        ),
        ChatMessage(
            role="assistant",
            content=None,
            timestamp="2026-10-03T08:01:05Z",
            tool_calls=[
                ToolCallRef(id="c1", name="bash", arguments='{"command": "tail build.log"}')
            ],
        ),
        tool_message(
            ToolOutput(call_id="c1", tool_name="bash", output="build [k=v] done", success=True),
            "2026-10-03T08:01:06Z",
        ),
        ChatMessage(
            role="assistant",
            content="构建通过了 [k=v]，可以继续 call[0]。",
            timestamp="2026-10-03T08:01:09Z",
        ),
    ]


def _app(sessions, loop=None):
    return AICodingApp(
        loop=loop,
        session=object(),
        sessions=sessions,
        workspace="~/projects/ai-app",
        model_name="DeepSeek-V3",
    )


class _FakeLoop:
    async def process_input(self, session, text, *, on_delta=None, observer=None):
        return "ok"


def _sessions_stub(full):
    listed = SessionData(
        session_id=full.session_id, title="", created_time=full.created_time,
        last_access_time=full.last_access_time, messages=[], message_count=len(full.messages),
        first_preview=full.first_preview,
    )
    return SimpleNamespace(
        list_sessions=lambda: [listed],
        resolve=lambda prefix: [listed] if listed.session_id.startswith(prefix) else [],
        load=lambda sid: full if sid == full.session_id else None,
    )


@pytest.mark.asyncio
async def test_picker_groups_newest_first_with_day_headers():
    """Today above Yesterday above the absolute date — the screenshot order."""
    sessions = [
        _session("n" * 32, _NOW.isoformat(), "newest [bracket] question"),
        _session("y" * 32, _YESTERDAY_NOON.isoformat(), "yesterday question"),
        _session("o" * 32, "2020-01-15T12:00:00Z", "old question"),
    ]
    app = _app(SimpleNamespace(list_sessions=lambda: sessions))
    async with app.run_test() as pilot:
        await app.push_screen(ResumeScreen(sessions))
        await pilot.pause()
        items = app.screen.query_one("#resume-list", OptionList)
        texts = [str(option.prompt) for option in items._options]
        assert texts[0] == "Today"
        assert "newest [bracket] question" in texts[1]
        assert texts[2] == "Yesterday"
        assert "yesterday question" in texts[3]
        assert texts[4] == "Wed Jan 15 2020"
        assert "old question" in texts[5]


@pytest.mark.asyncio
async def test_picker_search_filters_across_preview_and_id():
    sessions = [
        _session("a" * 32, "2026-10-03T08:00:00Z", "Fix the payment refund bug"),
        _session("b" * 32, "2026-10-02T08:00:00Z", "Write the onboarding docs"),
    ]
    app = _app(SimpleNamespace(list_sessions=lambda: sessions))
    async with app.run_test() as pilot:
        await app.push_screen(ResumeScreen(sessions))
        await pilot.pause()
        app.screen.query_one("#resume-search", Input).value = "refund"
        await pilot.pause()
        await pilot.pause()
        items = app.screen.query_one("#resume-list", OptionList)
        texts = [str(option.prompt) for option in items._options]
        assert len(texts) == 2  # header + the one match
        assert "refund bug" in texts[1]


@pytest.mark.asyncio
async def test_picker_escape_cancels_without_touching_the_session():
    sessions = [_session("a" * 32, "2026-10-03T08:00:00Z", "whatever")]
    current = object()
    app = _app(SimpleNamespace(list_sessions=lambda: sessions))
    app._session = current
    async with app.run_test() as pilot:
        await app.push_screen(ResumeScreen(sessions))
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app._session is current


@pytest.mark.asyncio
async def test_resume_rebuilds_history_with_brackets_intact():
    """The point of the whole change: after /resume the old conversation is
    visible — user text, model prose and the settled tool card, brackets and
    all, cards collapsed."""
    from textual.widgets import Collapsible

    full = _session("a" * 32, "2026-10-03T08:01:00Z", "看看日志", _history())
    app = _app(_sessions_stub(full))
    async with app.run_test() as pilot:
        app.query_one("#prompt", Input).value = "/resume aaaaaaaaaaaa"
        await pilot.press("enter")
        for _ in range(10):
            await pilot.pause()
        texts = "\n".join(
            w.content.plain if hasattr(w.content, "plain") else str(w.content)
            for w in app.query("Static")
            if not isinstance(w, Prose)
        )
        assert "看看日志 [INFO] env" in texts
        assert "build [k=v] done" in texts
        assert app.query_one(Prose).source == "构建通过了 [k=v]，可以继续 call[0]。"
        cards = list(app.query(Collapsible))
        assert cards and all(card.collapsed for card in cards)
        assert "tail build.log" in str(cards[0].title)


@pytest.mark.asyncio
async def test_resume_clears_the_previous_transcript():
    """Switching sessions must not leave the old conversation on screen."""
    full = _session("a" * 32, "2026-10-03T08:01:00Z", "看看日志", _history())
    app = _app(_sessions_stub(full), loop=_FakeLoop())
    async with app.run_test() as pilot:
        app.query_one("#prompt", Input).value = "stale content that must go"
        await pilot.press("enter")
        for _ in range(6):
            await pilot.pause()
        app.query_one("#prompt", Input).value = "/resume aaaaaaaaaaaa"
        await pilot.press("enter")
        for _ in range(10):
            await pilot.pause()
        texts = "\n".join(
            w.content.plain if hasattr(w.content, "plain") else str(w.content)
            for w in app.query("Static")
            if not isinstance(w, Prose)
        )
        assert "stale content that must go" not in texts
        assert "看看日志 [INFO] env" in texts


@pytest.mark.asyncio
async def test_picker_hides_the_session_already_open():
    """Resuming the conversation already open is a no-op, so hide it."""
    sessions = [
        _session("c" * 32, _NOW.isoformat(), "the conversation I am already in"),
        _session("d" * 32, _YESTERDAY_NOON.isoformat(), "a different conversation"),
    ]
    app = _app(SimpleNamespace(list_sessions=lambda: sessions))
    async with app.run_test() as pilot:
        await app.push_screen(ResumeScreen(sessions, exclude_id="c" * 32))
        await pilot.pause()
        items = app.screen.query_one("#resume-list", OptionList)
        texts = [str(option.prompt) for option in items._options]
        assert not any("already in" in text for text in texts)
        assert any("different conversation" in text for text in texts)


@pytest.mark.asyncio
async def test_picking_a_session_switches_and_replays():
    """End to end through the real picker: bare /resume, choose, history visible."""
    full = _session("a" * 32, "2026-10-03T08:01:00Z", "看看日志", _history())
    app = _app(_sessions_stub(full), loop=_FakeLoop())
    async with app.run_test() as pilot:
        app.query_one("#prompt", Input).value = "/resume"
        await pilot.press("enter")
        for _ in range(6):
            await pilot.pause()
        items = app.screen.query_one("#resume-list", OptionList)
        items.focus()
        items.highlighted = 1  # past the day header, onto the session row
        await pilot.pause()
        await pilot.press("enter")
        for _ in range(12):
            await pilot.pause()
        assert app._session is full
        assert app.query_one(Prose).source == "构建通过了 [k=v]，可以继续 call[0]。"


@pytest.mark.asyncio
async def test_resume_rebuilds_plan_step_headers():
    """Step deliveries recorded by /plan-run reappear as headers on resume:
    matched to the step's own user bubble, never streamed, never invented."""
    from ai_coding.domain.message import ChatMessage
    from ai_coding.domain.plan_delivery import StepDelivery

    messages = [
        ChatMessage(
            role="user",
            content="只做这一步，不要做别的：加边界测试\n（这是计划步骤 2，只做这一项。）",
            timestamp="2026-10-03T08:01:00Z",
        ),
        ChatMessage(
            role="assistant", content="加好了。", timestamp="2026-10-03T08:01:09Z"
        ),
    ]
    full = _session("a" * 32, "2026-10-03T08:01:00Z", "加边界测试", messages)
    full.step_deliveries = [
        StepDelivery(step_id="2", statement="加边界测试", status="completed",
                     attempts=1, ac=[], summary="加边界测试", turns=(2, 2)),
    ]
    app = _app(_sessions_stub(full))
    async with app.run_test() as pilot:
        app.query_one("#prompt", Input).value = "/resume aaaaaaaaaaaa"
        await pilot.press("enter")
        for _ in range(10):
            await pilot.pause()
        texts = "\n".join(
            w.content.plain if hasattr(w.content, "plain") else str(w.content)
            for w in app.query("Static")
            if not isinstance(w, Prose)
        )
        assert "步骤" in texts and "加边界测试" in texts
        assert "完成" in texts
