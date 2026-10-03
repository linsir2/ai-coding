"""Transcript: message headers, streaming markdown, blocks, auto-scroll."""

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Collapsible, Static

from ai_coding.ui.blocks import Block
from ai_coding.ui.transcript import Prose, Transcript, Turn


class _Harness(App[None]):
    def compose(self) -> ComposeResult:
        yield Transcript()


def _texts(transcript: Transcript) -> str:
    out = []
    for widget in transcript.query(Static):
        content = widget.content
        out.append(content.plain if hasattr(content, "plain") else str(content))
    return "\n".join(out)


def _body_text(transcript: Transcript) -> str:
    """Live fold body text (`.tool-live` Statics, since the RichLog retired)."""
    out = []
    for widget in transcript.query(".tool-live"):
        if not isinstance(widget, Static):
            continue
        content = widget.content
        out.append(content.plain if hasattr(content, "plain") else str(content))
    return "\n".join(out)


@pytest.mark.asyncio
async def test_user_message_has_header_and_bubble():
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.append_user("分析一下代码结构", "10:24")
        await pilot.pause()
        header = t.query_one(".msg-header", Static)
        assert "You" in header.content.plain
        assert "10:24" in header.content.plain
        assert "分析一下代码结构" in _texts(t)


@pytest.mark.asyncio
async def test_streamed_markdown_lands_after_end():
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:25")
        t.stream("## 标题\n\n")
        t.stream("**粗体**")
        await t.end_assistant()
        await pilot.pause()
        md = t.query_one(Prose)
        assert "标题" in md.source
        assert "粗体" in md.source


@pytest.mark.asyncio
async def test_blocks_render_in_order():
    class _B:
        def __init__(self, kind, text):
            self.kind, self.text = kind, text

        def render(self):
            from rich.text import Text
            return Text(self.text)

    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.append_block(_B("tool-start", "▸ bash  ls"))
        t.append_block(_B("tool-end", "✓ bash"))
        await pilot.pause()
        assert "▸ bash" in _texts(t)
        assert "✓ bash" in _texts(t)


class _Nth:
    def __init__(self, i):
        self.kind, self.text = "turn", f"line {i}"

    def render(self):
        from rich.text import Text
        return Text(self.text)


@pytest.mark.asyncio
async def test_pinned_transcript_follows_the_end():
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        for i in range(60):
            t.append_block(_Nth(i))
        await pilot.pause()
        assert t.is_vertical_scroll_end


@pytest.mark.asyncio
async def test_clear_removes_everything():
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.append_user("hi", "10:00")
        await pilot.pause()
        t.clear()
        await pilot.pause()
        assert len(t.query(Static)) == 0


@pytest.mark.asyncio
async def test_flush_before_mount_does_not_lose_text():
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:25")
        t.stream("x" * 5000)  # ≥ _FLUSH_CHARS → flush() in the same tick as the mount
        await t.end_assistant()
        await pilot.pause()
        assert "x" in t.query_one(Prose).source


@pytest.mark.asyncio
async def test_one_turn_per_assistant_pair():
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:25")
        await t.end_assistant()
        await pilot.pause()
        assert len(t.query(Turn)) == 1
        t.begin_assistant("10:26")
        await t.end_assistant()
        await pilot.pause()
        assert len(t.query(Turn)) == 2


@pytest.mark.asyncio
async def test_tool_blocks_become_collapsibles():
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:25")
        t.append_block(
            Block(
                "tool",
                "{'command': 'ls'}",
                call_id="c1",
                tool_name="bash",
                tool_target="ls",
                tool_status="running",
            )
        )
        await pilot.pause()
        coll = t.query_one(Collapsible)
        # Card title: status mark + verb + target (no English tool name).
        # The hand-built Block carries no verb, so the table fallback filled it.
        assert coll.title == "▸ 正在运行命令 · ls"
        assert coll.collapsed


@pytest.mark.asyncio
async def test_finished_tool_shows_its_failure_reason():
    """A failed tool must say why it failed.

    Real tools report failures in ``output`` (see ``read_tool.py``: ``success=
    False, output="file not found: …"``) while ``error`` carries dispatcher's
    machine code; ``blocks_from_event`` folds both into ``Block.text``.  Before
    the fix that text was dropped on the floor — the title said ``✗ read`` and
    the body held nothing but the params.
    """
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:25")
        await pilot.pause()
        t.append_block(
            Block("tool", "{'path': 'a.py'}", call_id="c1", tool_name="read",
                  tool_target="a.py", tool_status="running")
        )
        t.append_block(Block("tool-chunk", "│ line one", call_id="c1"))
        t.append_block(
            Block("tool", "file not found: a.py", call_id="c1", tool_name="read",
                  tool_status="failed")
        )
        await pilot.pause()
        coll = t.query_one(Collapsible)
        coll.collapsed = False
        await pilot.pause()
        await pilot.pause()
        body = t.query_one(".tool-live", Static)
        content = body.content
        text = content.plain if hasattr(content, "plain") else str(content)
        assert "file not found: a.py" in text
        assert "line one" in text


@pytest.mark.asyncio
async def test_finished_tool_shows_its_output_summary():
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:25")
        await pilot.pause()
        t.append_block(
            Block("tool", "{'path': 'a.py'}", call_id="c1", tool_name="write",
                  tool_target="a.py", tool_status="running")
        )
        t.append_block(
            Block("tool", "wrote 42 lines", call_id="c1", tool_name="write",
                  tool_status="ok")
        )
        await pilot.pause()
        coll = t.query_one(Collapsible)
        coll.collapsed = False
        await pilot.pause()
        await pilot.pause()
        body = t.query_one(".tool-live", Static)
        content = body.content
        text = content.plain if hasattr(content, "plain") else str(content)
        assert "wrote 42 lines" in text


@pytest.mark.asyncio
async def test_finished_tool_title_keeps_the_target():
    """``ToolFinished`` carries no params, so the target cannot be re-derived.

    ``blocks_from_event`` has nothing to compute ``tool_target`` from at finish
    time; the only way to keep it in the title is to remember the verb/target
    the ``ToolStarted`` block built.
    """
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:25")
        await pilot.pause()
        t.append_block(
            Block("tool", "", call_id="c1", tool_name="read", tool_target="a.py",
                  tool_status="running")
        )
        t.append_block(
            Block("tool", "ok", call_id="c1", tool_name="read", tool_status="ok")
        )
        await pilot.pause()
        assert t.query_one(Collapsible).title == "✓ 已读取文件 · a.py"


@pytest.mark.asyncio
async def test_finished_tool_with_no_outcome_writes_nothing():
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:25")
        await pilot.pause()
        t.append_block(
            Block("tool", "{'path': 'a.py'}", call_id="c1", tool_name="read",
                  tool_target="a.py", tool_status="running")
        )
        await pilot.pause()
        before = _body_text(t)
        t.append_block(
            Block("tool", "", call_id="c1", tool_name="read", tool_status="ok")
        )
        await pilot.pause()
        assert _body_text(t) == before


@pytest.mark.asyncio
async def test_end_tool_for_an_unknown_call_is_silent():
    """A late event must not raise; it just has nowhere to go."""
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:25")
        await pilot.pause()
        t.append_block(
            Block("tool", "orphan", call_id="never-started", tool_name="read",
                  tool_status="ok")
        )
        await pilot.pause()
        assert not t.query(Collapsible)


@pytest.mark.asyncio
async def test_tool_title_is_coloured_by_outcome():
    """▸ / ✓ / ✗ must not all look the same.

    One ``$warning`` rule covered all three marks, so a failure was
    indistinguishable from a success without reading the glyph.
    """
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:25")
        await pilot.pause()
        for call_id, status in (("c1", "running"), ("c2", "running"), ("c3", "running")):
            t.append_block(
                Block("tool", "body", call_id=call_id, tool_name="bash",
                      tool_status=status)
            )
        t.append_block(
            Block("tool", "done", call_id="c2", tool_name="bash", tool_status="ok")
        )
        t.append_block(
            Block("tool", "boom", call_id="c3", tool_name="bash", tool_status="failed")
        )
        await pilot.pause()
        running, ok, failed = t.query(Collapsible)
        for coll, expected in ((running, "tool-running"), (ok, "tool-ok"),
                               (failed, "tool-failed")):
            assert expected in coll.classes, f"{coll.title!r} -> {sorted(coll.classes)}"
        # the running class is swapped, not accumulated
        assert "tool-running" not in ok.classes
        assert "tool-running" not in failed.classes


@pytest.mark.asyncio
async def test_tool_chunks_land_inside_collapsible():
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:25")
        await pilot.pause()
        t.append_block(
            Block("tool", "", call_id="c1", tool_name="bash", tool_status="running")
        )
        t.append_block(Block("tool-chunk", "hello output", call_id="c1"))
        t.flush()  # the app's 50ms timer tick: drain chunk buffer + render
        await pilot.pause()
        coll = t.query_one(Collapsible)
        coll.collapsed = False
        await pilot.pause()
        await pilot.pause()
        assert "hello output" in _body_text(t)


@pytest.mark.asyncio
async def test_header_precedes_markdown_without_a_pause():
    """The header is mounted eagerly, so it holds the first slot.

    ``compose()`` runs a message-loop tick after attach; a ``stream()`` in the
    same tick would otherwise land its Prose above the header.  The real app
    always awaits a model step in between, so only same-tick callers hit this —
    and that is exactly the contract this test pins down.
    """
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:25")
        t.stream("hello")  # no pause: same tick as begin_assistant
        await t.end_assistant()
        await pilot.pause()
        turn = t.query_one(Turn)
        assert [type(ch).__name__ for ch in turn.children][:2] == ["Static", "Prose"]


@pytest.mark.asyncio
async def test_header_is_mounted_once():
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:25")
        t.stream("a")
        t.stream("b")
        await t.end_assistant()
        await pilot.pause()
        assert len(t.query_one(Turn).query(".msg-header")) == 1


@pytest.mark.asyncio
async def test_turn_with_no_text_still_gets_a_header():
    """A tool-only turn, or a turn whose loop raised, never calls ``stream()``.

    The header must not depend on the first delta arriving.
    """
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:25")
        await t.end_assistant()
        await pilot.pause()
        turn = t.query_one(Turn)
        assert [type(ch).__name__ for ch in turn.children] == ["Static"]
        assert "Nexus" in t.query_one(".msg-header", Static).content.plain


@pytest.mark.asyncio
async def test_stream_without_begin_assistant_still_gets_a_header():
    """``Transcript.stream`` builds its own Turn; it must not skip the header."""
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.stream("orphan delta")
        await t.end_assistant()
        await pilot.pause()
        turn = t.query_one(Turn)
        assert [type(ch).__name__ for ch in turn.children][:2] == ["Static", "Prose"]


@pytest.mark.asyncio
async def test_text_and_tools_interleave():
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:25")
        await pilot.pause()
        t.stream("before")
        t.append_block(
            Block("tool", "", call_id="c1", tool_name="bash", tool_status="running")
        )
        t.stream("after")
        await t.end_assistant()
        await pilot.pause()
        turn = t.query_one(Turn)
        kinds = [type(ch).__name__ for ch in turn.children]
        assert kinds == ["Static", "Prose", "Collapsible", "Prose"]
