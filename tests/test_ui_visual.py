"""渲染层的视觉契约：装饰、宽度、按钮高度、参数呈现（tmux 截图逐条对应）。

每条都对应 `docs/features.md` 描述的呈现方式，对应根因在 commit 里写明。视觉缺陷
容易在重构中被"顺手加回来"，所以它们是断言，不是品味。
"""

from pathlib import Path

import pytest
from textual.app import App, ComposeResult

from ai_coding.ui.transcript import Transcript, Turn


class _Harness(App[None]):
    # The real app's stylesheet: these assertions are about *our* CSS winning over
    # Textual's widget defaults, so the harness has to load it.
    CSS_PATH = str(Path(__file__).resolve().parents[1] / "ai_coding/ui/app.tcss")

    def compose(self) -> ComposeResult:
        yield Transcript()


@pytest.mark.asyncio
async def test_collapsible_has_no_top_border():
    """每行 `▔▔▔▔` 是 Textual `Collapsible` 的默认 `border-top`，不是我们的设计。

    一个 40 行的屏幕里，工具卡与思考折叠各带一条全宽横线，实测一屏就有 3 行纯
    噪音，还会把对话切成一堆碎片。我们只写了 `margin-left`，从没关过它。
    """
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:00")
        turn = t._turn
        turn.start_tool("c1", "已运行命令", "ls -la", "shell", "")
        turn.end_tool("c1", "✓", "ok", "a.py")
        await pilot.pause()

        fold = t.query_one(".tool-ok")
        # ``border: none`` leaves an empty style with the old colour attached,
        # so the style string is what says whether a rule gets drawn.
        top_style = fold.styles.border_top[0]
        assert not top_style, (
            f"折叠体仍有上边框（Textual Collapsible 的默认 border-top: hkey）："
            f"{top_style!r}"
        )
        assert float(fold.styles.padding.bottom) == 0.0, (
            f"折叠体仍有下内边距 {fold.styles.padding.bottom}，"
            f"一个折叠的思考块占 3 行而不是 1 行"
        )


@pytest.mark.asyncio
async def test_user_bubble_spans_the_content_width():
    """用户气泡原本 `width: auto`，实测固定 54 列，右侧空 60 多列。"""
    async with _Harness().run_test(size=(100, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.append_user("读 config.example.yaml 的第 1 行，只回答这一行内容", "16:01")
        await pilot.pause()

        bubble = t.query_one(".user-bubble")
        assert bubble.styles.width is not None and bubble.styles.width.is_fraction, (
            f"气泡宽度应占满内容列，实际 {bubble.styles.width}"
        )
        assert bubble.outer_size.width > 80, (
            f"气泡实际宽 {bubble.outer_size.width}，100 列终端下应接近满宽"
        )


@pytest.mark.asyncio
async def test_turns_have_no_extra_bottom_margin():
    """`.msg-header{margin-top:1}` + `Turn{margin-bottom:1}` 叠加 = 每 Turn 两行空白。"""
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:00")
        await pilot.pause()

        node = t.query_one(Turn)
        bottom = node.styles.margin.bottom
        assert float(bottom) == 0.0, (
            f"Turn 仍有下外边距 {bottom}，与下一条消息的 margin-top 叠加成两行空白"
        )


@pytest.mark.asyncio
async def test_tool_fold_title_has_no_double_marker():
    """工具标题已经自带状态符（▸ 运行中 / ✓ 完成 / ✗ 失败），外面又套一个 ▶。

    实测渲染成 `▶ ✓ 已读取文件 · config.example.yaml` —— 两个符号讲同一件事，
    而思考折叠只有一个 `▶`，于是两者看起来一模一样。
    """
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:00")
        t._turn.start_tool("c1", "读取文件", "config.yaml", "read", "")
        t._turn.end_tool("c1", "✓", "ok", "x")
        await pilot.pause()

        titles = [w.content.plain for w in t.query("CollapsibleTitle")]
        tool_titles = [x for x in titles if "已读取文件" in x]
        assert tool_titles, f"没找到工具折叠标题：{titles}"
        assert not tool_titles[0].startswith("▶"), (
            f"工具标题多了一个折叠符号：{tool_titles[0]!r}"
        )
        assert tool_titles[0].lstrip().startswith("✓"), tool_titles[0]


@pytest.mark.asyncio
async def test_thinking_fold_keeps_its_disclosure_marker():
    """工具让出 `▶` 之后，思考折叠的 `▶` 就是唯一的展开指示，必须留着。"""
    async with _Harness().run_test(size=(80, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:00")
        t._turn.append_reasoning("模型想了一下")
        await pilot.pause()

        titles = [w.content.plain for w in t.query("CollapsibleTitle")]
        assert any(x.startswith("▶") for x in titles), f"思考折叠丢了 ▶：{titles}"
