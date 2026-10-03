"""WP3: full tool-call view resolved from session messages (pure, no Textual).

Events deliberately carry only previews (``output_preview`` ≤ 500, R1a-3:
"a 10MB tool output must never ride an event", ``core/trace.py``).  The session
file is where the untruncated text lives, so the transcript upgrades each fold
from preview to full at turn end by looking the call back up here.
"""

import json

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from ai_coding.core.events import ToolFinished, ToolStarted
from ai_coding.domain.message import ChatMessage, ToolCallRef, ToolOutput
from ai_coding.domain.run import tool_message, wrap_tool_output
from ai_coding.ui.blocks import _FULL_OUTPUT_LIMIT, blocks_from_event, full_call_view
from ai_coding.ui.transcript import Transcript


class _Harness(App[None]):
    def compose(self) -> ComposeResult:
        yield Transcript()


def _full_text(t) -> str:
    """Whole settled card text (kind + command + output + footer rows)."""
    return "\n".join(_card_texts(t))


def _assistant(call_id: str, arguments: str, name: str = "bash") -> ChatMessage:
    return ChatMessage(
        role="assistant",
        content=None,
        timestamp="",
        tool_calls=[ToolCallRef(id=call_id, name=name, arguments=arguments)],
    )


def _tool(call_id: str, output: str, **kwargs) -> ChatMessage:
    return tool_message(
        ToolOutput(call_id=call_id, tool_name="bash", output=output, **kwargs),
        timestamp="",
    )


def test_hit_returns_command_and_output_without_envelope():
    messages = [
        _assistant("c1", '{"command": "git log -1 --stat"}'),
        _tool("c1", "commit f9fd276\n 3 files changed"),
    ]
    view = full_call_view(messages, "c1")
    assert view.params == "$ git log -1 --stat"
    assert view.output == "commit f9fd276\n 3 files changed"
    assert "<tool_output" not in (view.output or "")
    assert "<data>" not in (view.output or "")


def test_data_block_and_ok_attr_are_stripped():
    content = wrap_tool_output(
        "bash", "real output", success=False, data={"exit_code": 1}
    )
    messages = [
        _assistant("c9", '{"command": "false"}'),
        ChatMessage(
            role="tool", content=content, timestamp="",
            tool_call_id="c9", tool_name="bash",
        ),
    ]
    view = full_call_view(messages, "c9")
    assert view.output == "real output"
    assert "exit_code" not in (view.output or "")


def test_unknown_call_id_returns_nones():
    messages = [_assistant("c1", '{"command": "ls"}'), _tool("c1", "x")]
    view = full_call_view(messages, "nope")
    assert view.params is None and view.output is None


def test_empty_output_yields_none_output_but_keeps_params():
    messages = [_assistant("c1", '{"command": "true"}'), _tool("c1", "")]
    view = full_call_view(messages, "c1")
    assert view.output is None
    assert view.params == "$ true"


def test_non_bash_tool_returns_raw_arguments():
    messages = [
        _assistant("c2", '{"path": "src/foo.py"}', name="read"),
        ChatMessage(
            role="tool",
            content=wrap_tool_output("read", "hello"),
            timestamp="",
            tool_call_id="c2",
            tool_name="read",
        ),
    ]
    view = full_call_view(messages, "c2")
    assert view.params == '{"path": "src/foo.py"}'
    assert view.output == "hello"


def test_bad_json_arguments_returned_raw():
    messages = [
        _assistant("c3", "{not json"),
        _tool("c3", "out"),
    ]
    view = full_call_view(messages, "c3")
    assert view.params == "{not json"


def test_last_tool_message_wins():
    messages = [
        _assistant("c1", '{"command": "x"}'),
        _tool("c1", "first"),
        _tool("c1", "second"),
    ]
    assert full_call_view(messages, "c1").output == "second"


def test_over_limit_output_truncated_with_note():
    big = "y" * (_FULL_OUTPUT_LIMIT + 100)
    messages = [_assistant("c1", '{"command": "yes"}'), _tool("c1", big)]
    view = full_call_view(messages, "c1")
    assert view.output is not None
    assert len(view.output) <= _FULL_OUTPUT_LIMIT + 200
    assert "会话" in view.output  # points at the session file for the rest


@pytest.mark.asyncio
async def test_end_of_turn_upgrades_fold_preview_to_full():
    """After the turn, the fold shows the full command and output (WP3)."""
    full_output = "commit f9fd276\n 3 files changed, 3 insertions(+)"
    messages = [
        _assistant("c1", '{"command": "git log -1 --stat"}'),
        _tool("c1", full_output),
    ]
    async with _Harness().run_test(size=(80, 24)) as pilot:
        from textual.widgets import Collapsible

        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:25")
        await pilot.pause()
        started = blocks_from_event(
            ToolStarted(call_id="c1", tool_name="bash", params={"command": "git log -1 --stat"})
        )
        finished = blocks_from_event(
            ToolFinished(call_id="c1", tool_name="bash", success=True, output_preview="done")
        )
        assert started is not None and finished is not None
        t.append_block(started)
        t.append_block(finished)
        await t.end_assistant(messages)
        t.query_one(Collapsible).collapsed = False
        await pilot.pause()
        await pilot.pause()
        text = _full_text(t)
        assert "$ git log -1 --stat" in text
        assert "3 files changed" in text
        assert "done" not in text.split("$ git log")[0]


@pytest.mark.asyncio
async def test_backfill_without_messages_is_a_noop():
    """Old callers (and empty turns) keep today's preview behavior."""
    async with _Harness().run_test(size=(80, 24)) as pilot:
        from textual.widgets import Collapsible

        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:25")
        await pilot.pause()
        started = blocks_from_event(
            ToolStarted(call_id="c1", tool_name="bash", params={"command": "ls"})
        )
        finished = blocks_from_event(
            ToolFinished(call_id="c1", tool_name="bash", success=True, output_preview="done")
        )
        assert started is not None and finished is not None
        t.append_block(started)
        t.append_block(finished)
        await t.end_assistant()
        t.query_one(Collapsible).collapsed = False
        await pilot.pause()
        await pilot.pause()
        assert "done" in _live_text(t)


@pytest.mark.asyncio
async def test_run_turn_backfills_folds_from_session():
    """End to end: run_turn → session messages → folds show full text (WP3)."""
    from types import SimpleNamespace

    from textual.widgets import Collapsible, Input

    from tests.test_textual_app import _app

    full_output = "commit f9fd276\n 3 files changed, 3 insertions(+)"
    session = SimpleNamespace(
        messages=[
            _assistant("c1", '{"command": "git log -1 --stat"}'),
            _tool("c1", full_output),
        ]
    )

    class _Loop:
        async def process_input(self, session, text, *, on_delta=None, observer=None):
            assert observer is not None
            started = ToolStarted(
                call_id="c1", tool_name="bash", params={"command": "git log -1 --stat"}
            )
            observer.on_event(started)
            observer.on_event(
                ToolFinished(
                    call_id="c1", tool_name="bash", success=True, output_preview="done"
                )
            )
            if on_delta is not None:
                on_delta("answer text")
            return "answer text"

        async def aclose(self):
            return None

    app = _app(loop=_Loop(), session=session)
    async with app.run_test(size=(80, 24)) as pilot:
        app.query_one("#prompt", Input).value = "commit?"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()
        pilot.app.query_one(Collapsible).collapsed = False
        await pilot.pause()
        await pilot.pause()
        text = _full_text(pilot.app.query_one(Transcript))
        assert "$ git log -1 --stat" in text
        assert "3 files changed" in text


@pytest.mark.asyncio
async def test_settled_fold_is_exactly_its_content_rows():
    """No dead blank space: the settled body must not fill available height."""
    full_output = "\n".join(f"line {i}" for i in range(6))
    messages = [
        _assistant("c1", '{"command": "git log -1 --stat"}'),
        _tool("c1", full_output),
    ]
    async with _Harness().run_test(size=(80, 24)) as pilot:
        from textual.widgets import Collapsible

        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:25")
        await pilot.pause()
        started = blocks_from_event(
            ToolStarted(call_id="c1", tool_name="bash", params={"command": "git log -1 --stat"})
        )
        finished = blocks_from_event(
            ToolFinished(call_id="c1", tool_name="bash", success=True, output_preview="done")
        )
        assert started is not None and finished is not None
        t.append_block(started)
        t.append_block(finished)
        await t.end_assistant(messages)
        t.query_one(Collapsible).collapsed = False
        await pilot.pause()
        await pilot.pause()
        body = t.query_one(".tool-full", Static)
        assert body.outer_size.height <= 10, (
            f"settled fold is {body.outer_size.height} rows for 7 content lines"
        )


def _card_texts(t) -> list[str]:
    """All settled-card rows in order (kind, command, output, footer)."""
    card = t.query_one(".tool-card")
    out = []
    for child in card.query(Static):
        content = child.content
        out.append(content.plain if hasattr(content, "plain") else str(content))
    return out


async def _settled_turn(t, pilot, *, tool_name="bash", params, finished_kwargs,
                        messages, call_id="c1"):
    from textual.widgets import Collapsible

    t.begin_assistant("10:25")
    await pilot.pause()
    started = blocks_from_event(
        ToolStarted(call_id=call_id, tool_name=tool_name, params=params)
    )
    finished = blocks_from_event(ToolFinished(call_id=call_id, **finished_kwargs))
    assert started is not None and finished is not None
    t.append_block(started)
    t.append_block(finished)
    await t.end_assistant(messages)
    t.query_one(Collapsible).collapsed = False
    await pilot.pause()
    await pilot.pause()


@pytest.mark.asyncio
async def test_settled_card_has_kind_command_output_footer():
    from textual.widgets import Collapsible

    from ai_coding.ui.transcript import Transcript

    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        await _settled_turn(
            t, pilot,
            params={"command": "git log -1 --stat"},
            finished_kwargs={"tool_name": "bash", "success": True,
                             "output_preview": "done"},
            messages=[
                _assistant("c1", '{"command": "git log -1 --stat"}'),
                _tool("c1", "commit f9fd276\n 3 files changed"),
            ],
        )
        rows = _card_texts(t)
        assert rows[0] == "Shell"
        assert rows[1] == "$ git log -1 --stat"
        assert "3 files changed" in "\n".join(rows)
        assert rows[-1] == "✓ 成功"
        title = t.query_one(Collapsible).title
        assert "已运行命令" in title


@pytest.mark.asyncio
async def test_settled_card_failure_footer():
    from ai_coding.ui.transcript import Transcript

    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        await _settled_turn(
            t, pilot,
            params={"command": "false"},
            finished_kwargs={"tool_name": "bash", "success": False,
                             "output_preview": "", "error": "tool_error"},
            messages=[
                _assistant("c1", '{"command": "false"}'),
                _tool("c1", "boom", success=False),
            ],
        )
        rows = _card_texts(t)
        assert rows[-1] == "✗ 失败"
        assert "boom" in "\n".join(rows)


@pytest.mark.asyncio
async def test_settled_card_unknown_tool_falls_back_to_its_name():
    from ai_coding.ui.transcript import Transcript

    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        await _settled_turn(
            t, pilot, tool_name="mcp__get_sum",
            params={"a": 1},
            finished_kwargs={"tool_name": "mcp__get_sum", "success": True,
                             "output_preview": "3"},
            messages=[
                _assistant("c1", '{"a": 1}', name="mcp__get_sum"),
                ChatMessage(
                    role="tool",
                    content=wrap_tool_output("mcp__get_sum", "3"),
                    timestamp="",
                    tool_call_id="c1",
                    tool_name="mcp__get_sum",
                ),
            ],
        )
        rows = _card_texts(t)
        assert rows[0] == "mcp__get_sum"


def _live_text(t) -> str:
    """Read back the running fold body (a `.tool-live` Static)."""
    static = t.query_one(".tool-live", Static)
    content = static.content
    return content.plain if hasattr(content, "plain") else str(content)


async def _live_turn(t, pilot, chunks=(), call_id="c1", tool_name="bash",
                     params=None, success=True, preview="done", error=None):
    """Drive start → chunks → finish, without ending the turn."""
    from textual.widgets import Collapsible

    t.begin_assistant("10:25")
    await pilot.pause()
    started = blocks_from_event(
        ToolStarted(call_id=call_id, tool_name=tool_name,
                    params=params or {"command": "ls"})
    )
    assert started is not None
    t.append_block(started)
    for i, text in enumerate(chunks):
        t.append_block(_chunk_block(call_id, tool_name, text, i))
    finished = blocks_from_event(
        ToolFinished(call_id=call_id, tool_name=tool_name, success=success,
                     output_preview=preview, error=error)
    )
    assert finished is not None
    t.append_block(finished)
    t.query_one(Collapsible).collapsed = False
    await pilot.pause()
    await pilot.pause()


def _chunk_block(call_id, tool_name, text, seq):
    from ai_coding.core.events import ToolChunk
    from ai_coding.ui.blocks import blocks_from_event as bfe

    block = bfe(ToolChunk(call_id, tool_name, "stdout", text, seq))
    assert block is not None
    return block


@pytest.mark.asyncio
async def test_live_chunks_accumulate_in_order():
    from ai_coding.ui.transcript import Transcript

    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        await _live_turn(t, pilot, chunks=["part1-", "part2"])
        text = _live_text(t)
        assert "part1-" in text and "part2" in text
        assert text.index("part1-") < text.index("part2")


@pytest.mark.asyncio
async def test_live_body_caps_with_note_and_no_session_pointer():
    from ai_coding.ui.transcript import _LIVE_OUTPUT_LIMIT, Transcript

    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        await _live_turn(t, pilot, chunks=["x" * (_LIVE_OUTPUT_LIMIT + 100)])
        text = _live_text(t)
        assert len(text) <= _LIVE_OUTPUT_LIMIT + 200
        assert "会话" not in text  # mid-run the session does not have it yet


@pytest.mark.asyncio
async def test_backfill_overrides_live_fragments():
    from ai_coding.ui.transcript import Transcript

    full_output = "commit f9fd276\n 3 files changed"
    messages = [
        _assistant("c1", '{"command": "git log -1 --stat"}'),
        _tool("c1", full_output),
    ]
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        await _live_turn(t, pilot, chunks=["frag-"])
        await t.end_assistant(messages)
        await pilot.pause()
        await pilot.pause()
        text = _full_text(t)
        assert "3 files changed" in text


@pytest.mark.asyncio
async def test_chunk_to_unknown_call_is_ignored():
    from ai_coding.ui.transcript import Transcript

    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:25")
        await pilot.pause()
        t.append_block(_chunk_block("ghost", "bash", "nope", 0))
        await pilot.pause()
        assert not t.query(".tool-live")


@pytest.mark.asyncio
async def test_live_body_stays_exact_height():

    from ai_coding.ui.transcript import Transcript

    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        await _live_turn(t, pilot, chunks=["a\nb\nc"])
        body = t.query_one(".tool-live", Static)
        assert body.outer_size.height <= 6, (
            f"live body is {body.outer_size.height} rows for ~4 content lines"
        )


@pytest.mark.asyncio
async def test_long_command_survives_intact_in_card_body():
    """Item C: the 60-char chip truncation must never touch the card body."""
    from ai_coding.ui.transcript import Transcript

    long_command = (
        "git log --pretty=format:'%H %an %ad %s' --date=short -100 -- "
        + "some/" * 40 + "path.py"
    )
    assert len(long_command) > 200
    messages = [
        _assistant("c1", json.dumps({"command": long_command})),
        _tool("c1", "ok-output"),
    ]
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        await _settled_turn(
            t, pilot,
            params={"command": long_command},
            finished_kwargs={"tool_name": "bash", "success": True,
                             "output_preview": "ok-output"},
            messages=messages,
        )
        assert long_command in _full_text(t)


@pytest.mark.asyncio
async def test_chunk_from_bare_thread_cannot_crash():
    """ToolChunks arrive on reader threads without an app context.

    Production crashed with NoActiveAppError: `loop.call_soon_threadsafe`
    copies the *reader thread's* (empty) context, so everything downstream of
    a chunk — including widget construction/mount — runs with no active app.
    ``Collapsible.compose`` touches ``self.app`` and dies there.  The chunk
    path must therefore be pure data (buffer only); rendering happens on the
    flush timer / end_tool, which own a proper context.
    """
    import threading

    from ai_coding.core.events import ToolChunk
    from ai_coding.ui.blocks import blocks_from_event
    from ai_coding.ui.transcript import Transcript

    errors: list[BaseException] = []

    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:25")
        await pilot.pause()
        started = blocks_from_event(
            ToolStarted(call_id="c1", tool_name="bash", params={"command": "ls"})
        )
        assert started is not None
        t.append_block(started)
        await pilot.pause()

        def fire():
            try:
                block = blocks_from_event(
                    ToolChunk("c1", "bash", "stdout", "chunk-text", 0)
                )
                assert block is not None
                t.append_block(block)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        worker = threading.Thread(target=fire)
        worker.start()
        worker.join(timeout=10)
        assert not worker.is_alive()
        assert errors == [], f"bare-thread chunk raised: {errors!r}"
        t.flush()  # what the app's 50ms timer does: drain + render on UI context
        for _ in range(6):
            await pilot.pause()
        assert "chunk-text" in _live_text(t)


# Textual markup in arbitrary text: for Static(markup=True) a `[` opens a tag.
# Program output hits both faces of it — MarkupError ("Expected markup value",
# e.g. "[path=/tmp/x]") and silent loss ("env [k=v] set" -> "env  set").
LOG_OUTPUT = "[INFO] [path=/tmp/x] [level=debug]\nbuild [k=v] done\n"


def _render_now(widget: Static) -> str:
    """Force the render path that parses markup, and read back plain text."""
    visual = widget.render()
    return getattr(visual, "plain", None) or str(visual)


@pytest.mark.asyncio
async def test_settled_card_survives_brackets_in_output():
    from ai_coding.ui.transcript import Transcript

    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        await _settled_turn(
            t, pilot,
            params={"command": "tail -f build.log"},
            finished_kwargs={"tool_name": "bash", "success": True,
                             "output_preview": "x"},
            messages=[
                _assistant("c1", '{"command": "tail -f build.log"}'),
                _tool("c1", LOG_OUTPUT),
            ],
        )
        text = _render_now(t.query_one(".tool-full", Static))
        assert "[INFO] [path=/tmp/x]" in text
        assert "build [k=v] done" in text


@pytest.mark.asyncio
async def test_settled_card_survives_brackets_in_command():
    from ai_coding.ui.transcript import Transcript

    command = "git log --pretty=format:'%h [%an] %s' -3"
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        await _settled_turn(
            t, pilot,
            params={"command": command},
            finished_kwargs={"tool_name": "bash", "success": True,
                             "output_preview": "ok"},
            messages=[
                _assistant("c1", json.dumps({"command": command})),
                _tool("c1", "ok"),
            ],
        )
        assert "[%an]" in _render_now(t.query_one(".tool-cmd", Static))


@pytest.mark.asyncio
async def test_live_body_survives_brackets_in_params_and_chunks():
    from ai_coding.ui.transcript import Transcript

    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        await _live_turn(
            t, pilot,
            params={"pattern": "[abc]*.py"},
            chunks=["[INFO] building\n", "done [k=v]\n"],
        )
        text = _render_now(t.query_one(".tool-live", Static))
        assert "[abc]*.py" in text
        assert "[INFO] building" in text
        assert "done [k=v]" in text
