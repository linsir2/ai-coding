"""Transcript: message headers, streaming markdown, blocks, auto-scroll."""

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Collapsible, Markdown, RichLog, Static

from ai_coding.ui.blocks import Block
from ai_coding.ui.transcript import Transcript, Turn


class _Harness(App[None]):
    def compose(self) -> ComposeResult:
        yield Transcript()


def _texts(transcript: Transcript) -> str:
    out = []
    for widget in transcript.query(Static):
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
        md = t.query_one(Markdown)
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
        assert "x" in t.query_one(Markdown).source


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
        assert "bash" in coll.title
        assert "ls" in coll.title
        assert coll.collapsed


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
        await pilot.pause()
        coll = t.query_one(Collapsible)
        coll.collapsed = False
        await pilot.pause()
        await pilot.pause()
        log = t.query_one(RichLog)
        assert any("hello output" in line.text for line in log.lines)


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
        assert kinds == ["Static", "Markdown", "Collapsible", "Markdown"]
