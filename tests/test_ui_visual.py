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


@pytest.mark.asyncio
async def test_ten_task_updates_cost_one_row():
    """15 次 TaskUpdated 曾经刷出 8 屏：每次全量 render() 挂 Transcript 顶层。

    改后每 Turn 只有一个折叠体，折叠态只占 1 行 —— 这是真正防霸屏的那条。
    """
    from ai_coding.ui.blocks import Block

    async with _Harness().run_test(size=(100, 40)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:00")
        for i in range(10):
            t.append_block(Block("task", "第一行\n第二行\n第三行", "dim",
                                 title=f"计划 {i}/10"))
        await pilot.pause()
        folds = t.query(".plan-state")
        assert len(folds) == 1
        assert folds[0].outer_size.height == 1, (
            f"计划折叠体占 {folds[0].outer_size.height} 行，折叠态必须只有 1 行"
        )


@pytest.mark.asyncio
async def test_plan_fold_title_is_not_warning_yellow():
    """`Turn Collapsible > CollapsibleTitle` 是 $warning 黄；计划是中性状态，
    标题必须是 muted 配色（和 .block-turn 同色），不能被染成警告色。"""
    from textual.widgets._collapsible import CollapsibleTitle

    from ai_coding.ui.blocks import Block
    from ai_coding.ui.plain import Plain

    async with _Harness().run_test(size=(100, 40)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:00")
        ref = Plain("参照", classes="block-turn")
        t.mount(ref)
        t.append_block(Block("task", "正文", "dim", title="计划 0/2"))
        await pilot.pause()
        title = t.query_one(".plan-state").query_one(CollapsibleTitle)
        assert title.styles.color == ref.styles.color, (
            f"计划标题颜色 {title.styles.color} 不是 muted {ref.styles.color}"
        )


@pytest.mark.asyncio
async def test_user_bubble_has_no_box():
    """用户气泡的四面 `round` 框是阅读噪音（opencode 只有左边一条 `┃`）。

    根因在 `.user-bubble` 一条规则：`border: round` + 灰底。改后只剩左边
    一条重竖线。
    """
    async with _Harness().run_test(size=(100, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.append_user("读 config.example.yaml 的第 1 行", "16:01")
        await pilot.pause()

        bubble = t.query_one(".user-bubble")
        for side in ("top", "right", "bottom"):
            style = getattr(bubble.styles, f"border_{side}")[0]
            assert not style, f"气泡还有 {side} 边框：{style!r}"
        assert bubble.styles.border_left[0] == "heavy", (
            f"气泡左边应是 heavy 竖线，实际 {bubble.styles.border_left!r}"
        )


@pytest.mark.asyncio
async def test_user_bubble_has_no_background_fill():
    """灰底是"气泡感"的一半来源：去框必须连底一起去掉，否则等于没去。"""
    async with _Harness().run_test(size=(100, 24)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.append_user("读 config.example.yaml 的第 1 行", "16:01")
        await pilot.pause()

        bubble = t.query_one(".user-bubble")
        assert bubble.styles.background.a == 0, (
            f"气泡还有背景填充：{bubble.styles.background!r}"
        )


@pytest.mark.asyncio
async def test_send_icon_is_gone():
    """输入区右边那个 `➤` 永远对不齐（`content-align` 是盒内对齐，管不到
    3 行高的父容器），且零功能、无引用、opencode 也没有 —— 直接去掉。"""
    from ai_coding.ui.input_bar import InputBar

    class _InputHarness(App[None]):
        CSS_PATH = str(Path(__file__).resolve().parents[1] / "ai_coding/ui/app.tcss")

        def compose(self) -> ComposeResult:
            yield InputBar()

    async with _InputHarness().run_test(size=(100, 10)) as pilot:
        await pilot.pause()
        assert len(pilot.app.query("#send-icon")) == 0, "输入区还有对不齐的 ➤"


@pytest.mark.asyncio
async def test_accumulated_reasoning_fold_is_one_row():
    """15 条 `▶ 思考过程` 变成 1 条：折叠态必须只有 1 行。"""
    async with _Harness().run_test(size=(100, 40)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.begin_assistant("10:00")
        for i in range(10):
            t._turn.append_reasoning(f"第 {i} 步的思考内容")
        await pilot.pause()
        folds = t.query(".reasoning-fold")
        assert len(folds) == 1
        assert folds[0].outer_size.height == 1, (
            f"聚合后的思考折叠体占 {folds[0].outer_size.height} 行，应为 1"
        )
