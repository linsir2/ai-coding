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


def _prose_source(transcript: Transcript) -> str:
    """Prose text as the model wrote it (``source``, not the Rich render)."""
    return "\n".join(md.source for md in transcript.query(Prose))


def _visible_children(turn) -> list:
    """Direct children minus the hidden activity fold (a tool-only chrome).

    Every Turn carries an ``activity-fold`` (built at turn start, hidden until
    a tool arrives) so its body is composed before tools mount.  Order tests
    about header/prose should not count it.
    """
    return [
        ch
        for ch in turn.children
        if "activity-fold" not in getattr(ch, "classes", set())
    ]


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
        coll = t.query_one(".tool-running")
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
        coll = t.query_one(".tool-failed")
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
        coll = t.query_one(".tool-ok")
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
        assert t.query_one(".tool-ok").title == "✓ 已读取文件 · a.py"


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
        assert not t.query(".tool-running, .tool-ok, .tool-failed")


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
        tool_folds = [
            c for c in t.query(Collapsible)
            if {"tool-running", "tool-ok", "tool-failed"} & set(c.classes)
        ]
        running, ok, failed = tool_folds
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
        assert [type(ch).__name__ for ch in _visible_children(turn)][:2] == ["Plain", "Prose"]


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
        assert [type(ch).__name__ for ch in _visible_children(turn)] == ["Plain"]
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
        assert [type(ch).__name__ for ch in _visible_children(turn)][:2] == ["Plain", "Prose"]


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
        # Tools no longer interleave with prose as siblings: they group into
        # the activity fold, so the direct children are header, activity,
        # then the two prose runs.
        kinds = [type(ch).__name__ for ch in turn.children]
        assert kinds == ["Plain", "Collapsible", "Prose", "Prose"]
        assert len(turn.query_one(".activity-fold").query(Collapsible)) == 1


@pytest.mark.asyncio
async def test_user_bubble_keeps_brackets_verbatim():
    """User text is literal: markup parsing would eat or crash on any `[`."""
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.append_user("run [INFO] env [k=v] now", "10:25")
        await pilot.pause()
        bubble = t.query_one(".user-bubble", Static)
        visual = bubble.render()
        text = getattr(visual, "plain", None) or str(visual)
        assert "[INFO] env [k=v] now" in text


@pytest.mark.asyncio
async def test_appending_after_the_user_scrolled_up_leaves_the_view_alone():
    """The policy's other half: content must not drag a reader back.

    ``_follow`` re-arms Textual's anchor, and an anchored widget keeps itself
    at the bottom as content arrives — so without a pin check, a user who
    scrolled up to re-read an answer would be pulled to the newest turn by
    every append.  A characterisation test as much as a guard: the old
    one-shot implementation also passed it (it sampled
    ``is_vertical_scroll_end``, which was False), but nothing pinned the
    intent, and the anchor would have broken it silently.
    """
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        for i in range(60):
            t.append_block(_Nth(i))
        await pilot.pause()
        t.scroll_home(animate=False)
        await pilot.pause()
        assert t.scroll_y == 0

        t.append_block(_Nth(60))
        for _ in range(3):
            await pilot.pause()

        assert t.scroll_y == 0, "an append dragged the view back to the newest turn"


@pytest.mark.asyncio
async def test_clearing_the_transcript_starts_following_again():
    """``/clear`` and ``/resume`` rebuild the view: it must follow again.

    A cleared transcript has nothing to stay scrolled up to, so whatever the
    user was doing before is over.  Without this, switching sessions while
    scrolled up leaves the new conversation stranded at its oldest turn.
    """
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        for i in range(60):
            t.append_block(_Nth(i))
        await pilot.pause()
        t.scroll_home(animate=False)
        await pilot.pause()

        t.clear()
        await pilot.pause()
        for i in range(60):
            t.append_block(_Nth(i))
        await pilot.pause()

        assert t.is_vertical_scroll_end


@pytest.mark.asyncio
async def test_replaying_a_long_session_lands_on_the_newest_turn():
    """The production path this was broken on: ``/resume`` of a long session.

    Replay mounts dozens of widgets and Textual's Markdown mounts
    fire-and-forget, so the view keeps growing after the last append; the
    one-shot ``_follow`` raced that growth and left the transcript showing the
    middle of a conversation instead of the newest turn.

    Six replays in one process, because that is what makes the race show: a
    single fresh app is not enough.  Measured on the one-shot implementation —
    2 of 6 stranded, every run, against 0 of 6 now.
    """
    from ai_coding.domain.message import ChatMessage

    def long_session(turns: int = 30):
        messages = []
        for i in range(turns):
            messages.append(
                ChatMessage(role="user", content=f"问题 {i}", timestamp="")
            )
            messages.append(
                ChatMessage(
                    role="assistant",
                    content=f"回答 {i}\n" + "细节行\n" * 3,
                    timestamp="",
                )
            )
        return messages

    stranded = []
    for _ in range(6):
        async with _Harness().run_test(size=(80, 24)) as pilot:
            t = pilot.app.query_one(Transcript)
            await t.replay_history(long_session())
            await pilot.pause()
            if not t.is_vertical_scroll_end:
                stranded.append((round(t.scroll_target_y), round(t.max_scroll_y)))

    assert not stranded, f"/resume left the transcript mid-history: {stranded}"


@pytest.mark.asyncio
async def test_a_plan_step_view_grows_live_and_closes_on_the_summary():
    """M3: one step's live output is a fold of its own, closed on its receipt.

    Plan steps run on throwaway sessions whose transcript is discarded by
    design, so the live view is the only place their work can appear.  The
    fold's body must grow the way prose does — remove+mount, never an in-place
    update, because the scroll container's arrangement keys on DOM structure
    (WP4b) — and the title has to end up saying what /resume will say, so the
    live timeline and the replayed one read the same.
    """
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        view = t.begin_plan_step("[plan] 步骤 1/2：改 loader")

        view.delta("先看 loader\n")
        await pilot.pause()
        view.flush()
        await pilot.pause()
        assert "先看 loader" in _prose_source(t)

        view.delta("再改它\n")
        view.flush()
        await pilot.pause()
        view.end("改 loader：已改")
        await pilot.pause()

        fold = t.query_one(".plan-step-fold")
        assert "步骤 1/2" in fold.title
        assert "已改" in fold.title
        # the whole step body, in order — nothing replaced it away
        assert "先看 loader" in _prose_source(t)
        assert "再改它" in _prose_source(t)


def _plan(title="计划 0/2", body="第一行\n第二行"):
    return Block("task", body, "dim", title=title)


@pytest.mark.asyncio
async def test_repeated_task_updates_share_one_plan_fold():
    """15 次 TaskUpdated 曾经刷出 8 屏：每次全量挂 Transcript 顶层。

    改后每 Turn 只有一个计划折叠体，后续更新只改标题。
    """
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:00")
        for i in range(10):
            t.append_block(_plan(title=f"计划 {i}/10", body=f"正文{i}"))
        await pilot.pause()
        folds = t.query(".plan-state")
        assert len(folds) == 1
        assert folds[0].title == "计划 9/10"


@pytest.mark.asyncio
async def test_plan_fold_starts_collapsed():
    """折叠是默认值：1 行标题是全部存在感，展开是用户的主动选择。"""
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:00")
        t.append_block(_plan())
        await pilot.pause()
        assert t.query_one(".plan-state", Collapsible).collapsed is True


@pytest.mark.asyncio
async def test_expanded_plan_fold_refreshes_body_in_place():
    """用户正展开看着，计划变了必须刷新正文 —— 但还是那一个折叠体。"""
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:00")
        t.append_block(_plan(title="计划 0/2", body="旧正文"))
        await pilot.pause()
        t.query_one(".plan-state", Collapsible).collapsed = False
        await pilot.pause()
        t.append_block(_plan(title="计划 1/2", body="新正文"))
        await pilot.pause()
        folds = t.query(".plan-state")
        assert len(folds) == 1
        assert folds[0].collapsed is False
        assert folds[0].title == "计划 1/2"
        assert "新正文" in _texts(t)
        assert "旧正文" not in _texts(t)


@pytest.mark.asyncio
async def test_plan_fold_survives_backfill_untouched():
    """约束 A：计划折叠体不能进 Turn._folds，否则 backfill 会拿 call_id
    反查会话，给它换上四行工具卡。"""
    from ai_coding.domain.message import ChatMessage, ToolCallRef, ToolOutput
    from ai_coding.domain.run import tool_message

    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:00")
        turn = t._turn
        turn.start_tool("c1", "已运行命令", "ls", "shell", "")
        turn.end_tool("c1", "✓", "ok", "done")
        t.append_block(_plan(title="计划 1/2", body="计划正文"))
        await t.end_assistant([
            ChatMessage(role="assistant", content=None, timestamp="",
                        tool_calls=[ToolCallRef(id="c1", name="bash",
                                                arguments='{"command": "ls"}')]),
            tool_message(ToolOutput(call_id="c1", tool_name="bash",
                                    output="done"), timestamp=""),
        ])
        await pilot.pause()
        plans = t.query(".plan-state")
        assert len(plans) == 1
        assert plans[0].title == "计划 1/2"
        # 工具卡照常生成（backfill 没坏），且只有工具的那一张
        assert len(t.query(".tool-card")) == 1


@pytest.mark.asyncio
async def test_consecutive_reasoning_shares_one_fold():
    """Every model step used to mount its own `▶ 思考过程`; a 15-step turn
    cost 15 lines.  One fold per turn, accumulated."""
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:00")
        t._turn.append_reasoning("先看目录")
        await pilot.pause()
        t._turn.append_reasoning("再看文件")
        await pilot.pause()
        folds = t.query(".reasoning-fold")
        assert len(folds) == 1, f"思考折叠体应合并成一个，实际 {len(folds)} 个"


@pytest.mark.asyncio
async def test_reasoning_fold_accumulates_in_order():
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:00")
        for text in ("第一步", "第二步", "第三步"):
            t._turn.append_reasoning(text)
            await pilot.pause()
        body = t.query_one(".reasoning-body")
        content = body.content
        plain = content.plain if hasattr(content, "plain") else str(content)
        assert plain.index("第一步") < plain.index("第二步") < plain.index("第三步")
        assert plain.count("第一步") == 1, "换体时应替换而不是叠加"


@pytest.mark.asyncio
async def test_reasoning_and_tools_stay_separate_folds():
    """Stage 1 only merges reasoning; tools keep their own folds."""
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:00")
        t._turn.append_reasoning("想一想")
        t._turn.start_tool("c1", "运行命令", "ls", "shell", "")
        await pilot.pause()
        assert len(t.query(".reasoning-fold")) == 1
        assert len(t.query(".tool-running, .tool-ok, .tool-failed")) == 1


@pytest.mark.asyncio
async def test_replay_accumulates_reasoning_into_one_fold():
    from ai_coding.domain.message import ChatMessage

    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        await t.replay_history([
            ChatMessage(role="assistant", content="a", timestamp="t", reasoning="第一段思考"),
            ChatMessage(role="assistant", content="b", timestamp="t", reasoning="第二段思考"),
        ])
        await pilot.pause()
        folds = t.query(".reasoning-fold")
        assert len(folds) == 1
        body = t.query_one(".reasoning-body")
        plain = body.content.plain if hasattr(body.content, "plain") else str(body.content)
        assert "第一段思考" in plain and "第二段思考" in plain


@pytest.mark.asyncio
async def test_expanded_reasoning_fold_stays_expanded_across_appends():
    """Rebuilding the fold must not reset what the user chose."""
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:00")
        t._turn.append_reasoning("第一段")
        await pilot.pause()
        t.query_one(".reasoning-fold").collapsed = False
        await pilot.pause()
        t._turn.append_reasoning("第二段")
        await pilot.pause()
        folds = t.query(".reasoning-fold")
        assert len(folds) == 1
        assert folds[0].collapsed is False, "追加后展开状态被重置了"


@pytest.mark.asyncio
async def test_consecutive_tools_share_one_activity_fold():
    """A batch mounts several tool folds in one tick; they must land in one
    activity fold, not one line each.  Deterministic: the activity body is
    composed before tools arrive (the fold is pre-built at turn start)."""
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:00")
        for i in range(3):
            t._turn.start_tool(f"c{i}", "运行命令", f"cmd{i}", "shell", "")
        await pilot.pause()
        activities = t.query(".activity-fold")
        assert len(activities) == 1, f"应有 1 个活动折叠体，实际 {len(activities)}"
        assert len(activities[0].query(Collapsible)) == 3, "3 个工具折叠体应都在活动内"


@pytest.mark.asyncio
async def test_activity_fold_title_counts_tools():
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:00")
        for i in range(3):
            t._turn.start_tool(f"c{i}", "运行命令", f"cmd{i}", "shell", "")
        await pilot.pause()
        assert "工具 3 个" in str(t.query_one(".activity-fold").title)


@pytest.mark.asyncio
async def test_end_tool_swaps_inside_the_activity_fold():
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:00")
        t._turn.start_tool("c1", "运行命令", "pwd", "shell", "")
        await pilot.pause()
        t._turn.end_tool("c1", "✓", "ok", "output")
        await pilot.pause()
        activity = t.query_one(".activity-fold")
        kids = list(activity.query(Collapsible))
        assert len(kids) == 1
        assert "✓" in str(kids[0].title), f"子折叠体标题没换成 ✓：{kids[0].title}"


@pytest.mark.asyncio
async def test_reasoning_fold_sits_before_the_activity_fold():
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:00")
        t._turn.append_reasoning("想一想")
        t._turn.start_tool("c1", "运行命令", "ls", "shell", "")
        await pilot.pause()
        tops = [ch for ch in t._turn.children if isinstance(ch, Collapsible)]
        assert len(tops) == 2, f"应有思考+活动两个顶层折叠体，实际 {len(tops)}"
        assert "reasoning-fold" in tops[0].classes, "思考折叠体应在活动之前"


@pytest.mark.asyncio
async def test_activity_fold_is_hidden_without_tools():
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:00")
        t._turn.append_reasoning("只想不做")
        await pilot.pause()
        activity = t.query_one(".activity-fold")
        assert activity.display is False, "无工具的回合，活动折叠体应隐藏"


@pytest.mark.asyncio
async def test_replay_groups_tools_into_one_activity_fold():
    from ai_coding.domain.message import ChatMessage, ToolCallRef, ToolOutput
    from ai_coding.domain.run import tool_message

    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        await t.replay_history([
            ChatMessage(
                role="assistant", content=None, timestamp="t",
                tool_calls=[ToolCallRef(id="c1", name="bash", arguments='{"command": "ls"}')],
            ),
            tool_message(ToolOutput(call_id="c1", tool_name="bash", output="done"), timestamp=""),
        ])
        await pilot.pause()
        activities = t.query(".activity-fold")
        assert len(activities) == 1
        kids = list(activities[0].query(Collapsible))
        assert len(kids) == 1 and "✓" in str(kids[0].title)


# ---- collapsed folds carry information, not just a label --------------------


def test_a_fold_title_is_the_first_real_line_flattened():
    """The collapsed fold is all most turns ever show, so its title has to say
    something.  The reference client's thinking fold leads with a summary line
    rather than the constant word "thinking"; a model that writes prose instead
    of a bold lead-in gets its first line."""
    from ai_coding.ui.transcript import one_line_title

    assert one_line_title("\n\n  第一行   有   空白  \n第二行") == "第一行 有 空白"
    assert one_line_title("") == ""
    assert one_line_title("   \n \n") == ""


def test_a_fold_title_is_truncated_rather_than_wrapping():
    from ai_coding.ui.transcript import one_line_title

    title = one_line_title("字" * 200, limit=20)

    assert len(title) == 20
    assert title.endswith("…")


@pytest.mark.asyncio
async def test_the_reasoning_fold_title_shows_what_was_thought():
    async with _Harness().run_test(size=(120, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:00")
        t._turn.append_reasoning("先看 loader，判断它是否可选。\n然后决定改哪里。")
        await pilot.pause()

        title = str(t.query(".reasoning-fold").first().title)

        # Still recognisable as the thinking fold, and now informative.
        assert "思考过程" in title
        assert "先看 loader" in title


@pytest.mark.asyncio
async def test_the_activity_fold_says_which_tools_ran():
    """``工具 3 个`` answered "how many" and nothing else; the operator
    scanning the transcript wants to know what ran without opening the fold."""
    async with _Harness().run_test(size=(120, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:00")
        t._turn.start_tool("c0", "运行命令", "ls", "shell", "")
        t._turn.start_tool("c1", "运行命令", "pwd", "shell", "")
        t._turn.start_tool("c2", "读取文件", "a.py", "read", "")
        await pilot.pause()

        title = str(t.query_one(".activity-fold").title)

        assert "工具 3 个" in title, "the existing contract survives"
        assert "运行命令 ×2" in title
        assert "读取文件" in title
