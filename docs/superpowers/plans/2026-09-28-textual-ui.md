# Textual UI 重写（v1：会话闭环）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 Textual 8.2 重建 ai_coding 的全屏 TUI，v1 交付会话闭环（参考图 3/4）。

**Architecture:** `AICodingApp(App)` 持有 loop/session；循环跑在 `@work` worker；同步 `on_delta` 攒缓冲、50ms 定时 `Markdown.append`；循环事件经纯函数 `blocks_from_event` 变成 `Block` 写入 `Transcript(VerticalScroll)`；审批/ask_user 用 `push_screen_wait` 弹 `ModalScreen`，`finally` 保证取消后关屏。

**Tech Stack:** Python 3.13 / Textual 8.2.8 / Rich / pytest + pytest-asyncio / ruff

**Spec:** `docs/superpowers/specs/2026-09-28-textual-ui-design.md`（计划中的每个决定都追溯到该文件）

## Global Constraints

- v1 只做会话闭环；欢迎页、命令面板、侧栏、计划卡**不做**（spec §9）。
- `run_app(path, workspace, skills)` 签名不变；`cli.py` 在 Task 7 才切换入口。
- `DeltaSink` 保持同步，**不改** `core/ai/security` 的行为（唯一例外见 Task 6 的 4.1 修复，也只动 UI 层）。
- 循环必须跑在 **async worker**（禁止 `thread=True`），否则 `push_screen_wait` 抛 `NoActiveWorker`。
- 模态 provider 必须 `try/finally` 关屏（spec §3.3-5）。
- 文案固定：`You` / `Nexus` / `Type a message or enter a command...` / `/ for commands` / `● Ready`。
- 每个 task 结束跑：`.venv/bin/python -m pytest -q`、`.venv/bin/ruff check .`、`.venv/bin/python -m mypy ai_coding`，三者干净才 commit（这三个就是 CI 门槛，见 `.github/workflows/ci.yml`；mypy 是 strict，且**必须用 `python -m`**——`.venv/bin/mypy` 的 shebang 指向已不存在的 `.venv-new`）。
- **mypy 基线（Task 1 实测）**：分支起点已有 **7 个既存错误**——6 个在 `ai_coding/ui/app.py`（Task 7 删文件后自动消失）、1 个在 `core/agent_loop.py:243`（`ChatEngine` 未声明 `model_config`，Task 7 以纯注解修复）。因此 Task 1–6 的门槛是「**不新增错误**」（错误集合与基线相同），Task 7 之后才要求归零。
- 异步测试用 `@pytest.mark.asyncio`（仓内约定，无 conftest）。
- 测试取窗口：`App.run_test(headless=True, size=(80, 24))`。

## File Structure

| 文件 | 职责 | Task |
|---|---|---|
| `ai_coding/ui/status_bar.py` | 1 行状态栏（workspace / 模型 / Ready\|Working） | 1 |
| `ai_coding/ui/input_bar.py` | 输入框 + 发送图标 + 提示行 | 1 |
| `ai_coding/ui/transcript.py` | 会话流容器：消息块、流式 markdown、吸底 | 1/2 |
| `ai_coding/ui/textual_app.py` | `AICodingApp` + `run_app` | 1/5 |
| `ai_coding/ui/app.tcss` | 全部样式（圆角、配色、间距） | 1 |
| `ai_coding/ui/blocks.py` | `Block` / `blocks_from_event` / `TranscriptObserver`（纯，无 Textual） | 3 |
| `ai_coding/ui/modals.py` | `ApprovalScreen` / `AskScreen` | 4 |
| `ai_coding/ui/console.py`（改） | `+ TextualConsole(DialogueConsole)` | 6 |
| `scripts/tui_shot.py` | SVG 截图（人眼比对，不进 CI） | 2 |
| `tests/test_textual_app.py` | 外壳与循环接线 | 1/5 |
| `tests/test_transcript.py` | Transcript 行为 | 2 |
| `tests/test_blocks.py` | 纯映射 + 节流（含 R8c 迁移） | 3 |
| `tests/test_modals.py` | 模态返回值与 teardown | 4 |
| `tests/test_pty_textual.py` + `tests/_pty_textual_child.py` | ask_user/审批真实终端 E2E | 6 |
| 删除 | 见 Task 7 清单 | 7 |

---

### Task 0: Spike（throwaway，不提交、不进仓库）

**Files:**
- Create: `/tmp/opencode/spike_textual.py`（仓库外）

**Interfaces:**
- Consumes: 无
- Produces: 三个结论（写进 Task 5 的实现笔记）：① `run_async` 能否复用外部事件循环；② worker 内 `push_screen_wait` + 取消后模态是否残留；③ 同步 sink → `Markdown.append` 是否可用。

- [ ] **Step 1: 写 spike 脚本**

```python
# /tmp/opencode/spike_textual.py
"""Throwaway: prove the three riskiest wirings before touching the real app."""
import asyncio

from textual.app import App, ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Button, Markdown, Static


class Ask(ModalScreen[str]):
    def compose(self) -> ComposeResult:
        yield Button("answer", id="ans")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss("answered")


class Spike(App[None]):
    def compose(self) -> ComposeResult:
        yield Static("spike", id="hello")
        yield Markdown(id="md")

    async def on_mount(self) -> None:
        assert self.screen is not None

        async def ask() -> None:
            try:
                result = await self.push_screen_wait(Ask())
                self.query_one("#hello", Static).update(f"modal={result}")
            finally:
                if self.screen.__class__ is Ask:
                    self.pop_screen()

        async def turn() -> None:
            await ask()
            md = self.query_one("#md", Markdown)
            md.append("## 结论\n\n**ok**\n")
            self.exit()

        self._worker = self.run_worker(turn(), exclusive=True, thread=False)

    def on_unmount(self) -> None:
        if getattr(self, "_worker", None) is not None:
            self._worker.cancel()


def drive(app: Spike) -> None:
    async def main() -> None:
        await app.run_async()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()


if __name__ == "__main__":
    drive(Spike())
    print("SPIKE OK: run_async on external loop + worker modal + Markdown.append")
```

- [ ] **Step 2: 运行并记录结果**

Run: `cd /tmp/opencode && /home/linsir365/projects/ai-coding/.venv/bin/python spike_textual.py`
Expected: 打印 `SPIKE OK: ...`；无异常。

- [ ] **Step 3: 手动确认取消路径**

把 `ask()` 里的 `await self.push_screen_wait(Ask())` 换成 `await asyncio.sleep(30)` 前先弹模态，然后在 worker 外部 `self._worker.cancel()`，观察模态是否残留——记录结论。若残留，说明 Task 5 必须用 `finally: pop_screen()`（spec §3.3-5 已如此设计）。

- [ ] **Step 4: 清理**

Run: `rm /tmp/opencode/spike_textual.py`
不 commit。

---

### Task 1: 依赖 + 应用骨架

**Files:**
- Modify: `pyproject.toml:7-15`（`dependencies` 加 `textual>=8.2`）
- Create: `ai_coding/ui/status_bar.py`
- Create: `ai_coding/ui/input_bar.py`
- Create: `ai_coding/ui/transcript.py`
- Create: `ai_coding/ui/app.tcss`
- Create: `ai_coding/ui/textual_app.py`
- Test: `tests/test_textual_app.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `AICodingApp(*, loop, session, sessions, workspace, model_name="")` → `.query_one(StatusBar)` / `#transcript` / `#prompt`
  - `StatusBar(workspace: str, model_name: str = "")`，方法 `set_model(name)` / `set_busy(bool)`
  - `InputBar`，属性 `.prompt -> Input`，常量文案
  - `Transcript(VerticalScroll)`（空壳，行为在 Task 2）

- [ ] **Step 1: 安装依赖并声明**

Run:
```bash
uv pip install --python .venv/bin/python "textual>=8.2"
```
Expected: `Installed 4 packages`（textual 8.2.8 + linkify-it-py + mdit-py-plugins + platformdirs）

然后编辑 `pyproject.toml` 的 `dependencies`，在 `"prompt-toolkit>=3.0",` **之后**加一行：
```toml
    "textual>=8.2",
```
（`prompt-toolkit` 这一行 Task 7 才删。）

- [ ] **Step 2: 写失败测试**

```python
# tests/test_textual_app.py
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
        assert "● Ready" in app.query_one("#status-right").content.plain
        assert "~/projects/ai-app" in app.query_one("#status-left").content.plain


@pytest.mark.asyncio
async def test_status_bar_switches_to_working():
    app = _app()
    async with app.run_test() as pilot:
        bar = app.query_one(StatusBar)
        bar.set_busy(True)
        await pilot.pause()
        assert "● Working" in app.query_one("#status-right").content.plain
```

- [ ] **Step 3: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_textual_app.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'ai_coding.ui.textual_app'`

- [ ] **Step 4: 实现 `status_bar.py`**

```python
"""One-row status bar: ``>_ workspace`` left, model + state right."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Static


class StatusBar(Horizontal):
    """Fixed one-row bar at the top of the app."""

    def __init__(self, workspace: str, model_name: str = "") -> None:
        super().__init__(id="status-bar")
        self._workspace = workspace
        self._model = model_name
        self._busy = False

    def compose(self) -> ComposeResult:
        yield Static(id="status-left")
        yield Static(id="status-right")

    def on_mount(self) -> None:
        self._refresh()

    def set_model(self, name: str) -> None:
        self._model = name
        self._refresh()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._refresh()

    def _refresh(self) -> None:
        left = Text()
        left.append(">_ ", style="bold")
        left.append(self._workspace)
        if self._model:
            left.append(f"  ·  {self._model}", style="dim")
        self.query_one("#status-left", Static).update(left)
        self.query_one("#status-right", Static).update(
            Text("● Working" if self._busy else "● Ready",
                 style="yellow" if self._busy else "green")
        )
```

- [ ] **Step 5: 实现 `input_bar.py`**

```python
"""Bottom input: rounded box, placeholder, send glyph, hint line."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, Static

PLACEHOLDER = "Type a message or enter a command..."
HINT = "/ for commands"


class InputBar(Vertical):
    """The input row plus the ``/ for commands`` hint under it."""

    def __init__(self) -> None:
        super().__init__(id="input-bar")

    def compose(self) -> ComposeResult:
        with Horizontal(id="input-row"):
            yield Input(placeholder=PLACEHOLDER, id="prompt")
            yield Static("➤", id="send-icon")
        yield Static(HINT, id="input-hint")

    @property
    def prompt(self) -> Input:
        return self.query_one("#prompt", Input)
```

- [ ] **Step 6: 实现 `transcript.py`（空壳，Task 2 补行为）**

```python
"""Scrolling conversation transcript (behavior lands in Task 2)."""

from __future__ import annotations

from textual.containers import VerticalScroll


class Transcript(VerticalScroll):
    """Append-only conversation area."""
```

- [ ] **Step 7: 实现 `textual_app.py`**

```python
"""The Textual application: shell now, loop wiring in Task 5."""

from __future__ import annotations

from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding

from ai_coding.ui.input_bar import InputBar
from ai_coding.ui.status_bar import StatusBar
from ai_coding.ui.transcript import Transcript


class AICodingApp(App[None]):
    """Full-screen coding assistant UI."""

    CSS_PATH = "app.tcss"
    # Textual binds ctrl+q to quit and ctrl+c to "copy or hint".
    # spec §4 #14: keep the old ctrl+c quit (fires only when nothing is selected,
    # because Screen's copy binding has priority and raises SkipAction otherwise).
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", show=False, priority=True),
        Binding("ctrl+c", "quit", "Quit", show=False),
    ]

    # Declared here (not only assigned in on_mount) so mypy strict can infer them.
    _loop: Any
    _session: Any
    _sessions: Any
    _workspace: str
    _model_name: str

    def __init__(
        self,
        *,
        loop: Any,
        session: Any,
        sessions: Any,
        workspace: str,
        model_name: str = "",
    ) -> None:
        super().__init__()
        self._loop = loop
        self._session = session
        self._sessions = sessions
        self._workspace = workspace
        self._model_name = model_name

    def compose(self) -> ComposeResult:
        yield StatusBar(self._workspace, self._model_name)
        yield Transcript()
        yield InputBar()
```

- [ ] **Step 8: 实现 `app.tcss`**

```css
Screen {
    layout: vertical;
    background: $surface;
}

#status-bar {
    height: 1;
    background: $surface-lighten-1;
    padding: 0 2;
}

#status-left {
    width: 1fr;
}

#status-right {
    width: auto;
}

#transcript {
    height: 1fr;
    padding: 1 2;
    scrollbar-size-vertical: 1;
}

#input-bar {
    height: auto;
    padding: 1 2 0 2;
}

#input-row {
    height: auto;
}

#prompt {
    width: 1fr;
    border: round $primary;
}

#send-icon {
    width: 3;
    content-align: center middle;
    color: $primary;
}

#input-hint {
    height: 1;
    color: $text-muted;
    text-align: right;
}
```

- [ ] **Step 9: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_textual_app.py -q`
Expected: `2 passed`

- [ ] **Step 10: 跑全量回归 + lint**

Run:
```bash
.venv/bin/python -m pytest -q && .venv/bin/ruff check . && .venv/bin/python -m mypy ai_coding
```
Expected: 全绿（旧 TUI 未受影响）；`All checks passed!`

- [ ] **Step 11: Commit**

```bash
git add pyproject.toml ai_coding/ui/status_bar.py ai_coding/ui/input_bar.py \
  ai_coding/ui/transcript.py ai_coding/ui/textual_app.py ai_coding/ui/app.tcss \
  tests/test_textual_app.py
git commit -m "feat(ui): a Textual shell — status bar, transcript area, input bar"
```

---

### Task 2: Transcript 行为

**Files:**
- Modify: `ai_coding/ui/transcript.py`（补行为）
- Create: `scripts/tui_shot.py`
- Test: `tests/test_transcript.py`

**Interfaces:**
- Consumes: `ai_coding.ui.blocks.Block`（Task 3 定义；本任务先用 duck-typed 对象，`Block` 在 Task 3 落地）
- Produces:
  - `Transcript.append_user(text: str, timestamp: str) -> None`
  - `Transcript.begin_assistant(timestamp: str) -> None`
  - `Transcript.stream(text: str) -> None`（同步，攒缓冲）
  - `Transcript.flush() -> None`（同步，`Markdown.append` fire-and-forget）
  - `async Transcript.end_assistant() -> None`（await 最后一次 append）
  - `Transcript.append_block(block) -> None`
  - `Transcript.clear() -> None`
  - `Transcript.is_vertical_scroll_end`（Textual 原生）用于吸底判断

- [ ] **Step 1: 写失败测试**

```python
# tests/test_transcript.py
"""Transcript: message headers, streaming markdown, blocks, auto-scroll."""

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Markdown, Static

from ai_coding.ui.transcript import Transcript


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
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_transcript.py -q`
Expected: FAIL — `AttributeError: 'Transcript' object has no attribute 'append_user'`

- [ ] **Step 3: 实现 `transcript.py`**

```python
"""Scrolling conversation transcript: message blocks + streaming markdown."""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Markdown, Static

_FLUSH_CHARS = 4096


class Transcript(VerticalScroll):
    """Append-only conversation area with a "stay at the bottom" policy.

    Streaming is two-phase because ``on_delta`` is a *sync* callback while
    ``Markdown.append`` is optionally awaitable: ``stream`` only buffers,
    ``flush`` (driven by a 50ms timer in the app) renders, and the async
    ``end_assistant`` awaits the final write (spec §3.3-1).
    """

    def __init__(self) -> None:
        super().__init__(id="transcript")
        self._pending = ""
        self._markdown: Markdown | None = None

    def append_user(self, text: str, timestamp: str) -> None:
        self.mount(Static(_header("◯", "You", timestamp), classes="msg-header"))
        self.mount(Static(text, classes="user-bubble"))
        self._follow()

    def begin_assistant(self, timestamp: str) -> None:
        self.mount(Static(_header("✦", "Nexus", timestamp), classes="msg-header"))
        self._markdown = Markdown()
        self.mount(self._markdown)

    def stream(self, text: str) -> None:
        self._pending += text
        if len(self._pending) >= _FLUSH_CHARS:
            self.flush()

    def flush(self) -> None:
        if self._pending and self._markdown is not None:
            self._markdown.append(self._pending)  # AwaitComplete: starts now
        self._pending = ""
        self._follow()

    async def end_assistant(self) -> None:
        if self._pending and self._markdown is not None:
            await self._markdown.append(self._pending)
        self._pending = ""
        self._markdown = None
        self._follow()

    def append_block(self, block: Any) -> None:
        # ``Any`` until Task 3 lands ``Block``; importing it now would be a
        # cycle (blocks must stay Textual-free, so it cannot import Transcript
        # back).  Task 3 tightens this annotation.
        self.mount(Static(block.render(), classes=f"block-{block.kind}"))
        self._follow()

    def clear(self) -> None:
        for child in list(self.children):
            child.remove()
        self._pending = ""
        self._markdown = None

    def _follow(self) -> None:
        """Scroll to the newest content only if the user is already at the end."""
        if self.is_vertical_scroll_end:
            self.scroll_end(animate=False)


def _header(icon: str, name: str, timestamp: str) -> Text:
    text = Text()
    text.append(f"{icon} ", style="bold blue")
    text.append(name, style="bold")
    text.append(f"  {timestamp}", style="dim")
    return text
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_transcript.py -q`
Expected: `5 passed`

- [ ] **Step 5: 给 `app.tcss` 补消息样式**

在文件末尾追加：

```css
.msg-header {
    margin-top: 1;
}

.user-bubble {
    border: round $surface-lighten-2;
    background: $surface-lighten-1;
    padding: 0 1;
    margin: 0 2;
    width: auto;
}

.block-turn,
.block-tool-chunk {
    color: $text-muted;
}

.block-tool-start {
    color: $warning;
}

.block-tool-end {
    color: $success;
}

.block-turn-end {
    color: $error;
}
```

- [ ] **Step 6: 写截图脚本并运行**

```python
# scripts/tui_shot.py
"""Render the TUI shell to an SVG for eyeballing against the reference (v1).

Run: .venv/bin/python scripts/tui_shot.py
Output: docs/tui-preview.svg
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ai_coding.ui.textual_app import AICodingApp

OUT = Path(__file__).resolve().parent.parent / "docs" / "tui-preview.svg"


async def main() -> None:
    app = AICodingApp(
        loop=None,
        session=object(),
        sessions=object(),
        workspace="~/projects/ai-app",
        model_name="DeepSeek-V3",
    )
    async with app.run_test(size=(100, 30)) as pilot:
        transcript = app.query_one("#transcript")
        transcript.append_user("请帮我分析一下这个项目的代码结构，并给出优化建议。", "10:24")
        transcript.begin_assistant("10:25")
        transcript.stream(
            "好的，我将先分析当前项目的代码结构，然后从可维护性、性能、"
            "可扩展性等方面给出优化建议。\n\n## 项目结构概览\n\n"
            "- backend/  # 后端服务\n- frontend/  # 前端页面\n"
        )
        await transcript.end_assistant()
        transcript.append_block(_block("tool-end", "✓ 已完成代码分析"))
        await pilot.pause()
        app.save_screenshot(str(OUT))


def _block(kind: str, text: str):
    class _B:
        def __init__(self) -> None:
            self.kind, self.text = kind, text

        def render(self):
            from rich.text import Text
            return Text(self.text)

    return _B()


if __name__ == "__main__":
    asyncio.run(main())
    print(f"wrote {OUT}")
```

Run: `.venv/bin/python scripts/tui_shot.py`
Expected: `wrote .../docs/tui-preview.svg`；打开 SVG 肉眼确认状态栏/气泡/标题/输入框都在。

- [ ] **Step 7: 全量回归 + lint + commit**

```bash
.venv/bin/python -m pytest -q && .venv/bin/ruff check . && .venv/bin/python -m mypy ai_coding
git add ai_coding/ui/transcript.py ai_coding/ui/app.tcss scripts/tui_shot.py tests/test_transcript.py
git commit -m "feat(ui): a transcript that streams markdown and keeps the bottom"
```

---

### Task 3: blocks + TranscriptObserver（纯函数，可离线单测）

**Files:**
- Create: `ai_coding/ui/blocks.py`
- Test: `tests/test_blocks.py`
- Modify: `tests/test_tool_chunks.py`、`tests/test_tool_surface_warning.py`（把 `ConsoleObserver` 的用法迁到 `TranscriptObserver`）
- Modify: `tests/test_observer.py`（重写为 blocks 测试；旧 `BodyObserver` 测试删除）

**Interfaces:**
- Consumes: `ai_coding.core.events`（全部 LoopEvent 类型）、`ai_coding.domain.loop.TurnStatus`
- Produces:
  - `Block(kind: str, text: str, style: str = "")`，方法 `render() -> rich.text.Text`
  - `blocks_from_event(event: LoopEvent) -> Block | None`
  - `TranscriptObserver(append: Callable[[Block], None], *, clock=None, chunks=True)`，方法 `on_event(event)`（实现 `LoopObserver` 协议）

- [ ] **Step 1: 写失败测试（含从旧测试迁移的 R8c 语义）**

```python
# tests/test_blocks.py
"""blocks_from_event + TranscriptObserver: pure, offline, no Textual."""

from ai_coding.core.events import (
    IterationStarted,
    TaskUpdated,
    ToolChunk,
    ToolFinished,
    ToolStarted,
    ToolSurfaceWarning,
    TurnFinished,
)
from ai_coding.domain.loop import TurnOutcome, TurnStatus
from ai_coding.ui.blocks import Block, TranscriptObserver, blocks_from_event


def test_iteration_marker():
    block = blocks_from_event(IterationStarted(index=1, max_iterations=8))
    assert block is not None
    assert "1/8" in block.text
    assert block.kind == "turn"


def test_tool_started_names_the_tool_and_previews_params():
    block = blocks_from_event(
        ToolStarted(call_id="c1", tool_name="bash", params={"command": "ls -la"})
    )
    assert block is not None
    assert "bash" in block.text
    assert "ls -la" in block.text


def test_tool_finished_marks_success_and_failure():
    ok = blocks_from_event(
        ToolFinished(call_id="c1", tool_name="bash", success=True, output_preview="done")
    )
    bad = blocks_from_event(
        ToolFinished(call_id="c2", tool_name="bash", success=False, error="boom")
    )
    assert ok is not None and ok.text.startswith("✓")
    assert bad is not None and bad.text.startswith("✗")


def test_completed_turn_is_silent():
    assert blocks_from_event(TurnFinished(outcome=TurnOutcome(status=TurnStatus.COMPLETED))) is None


def test_timed_out_turn_says_so():
    block = blocks_from_event(
        TurnFinished(outcome=TurnOutcome(status=TurnStatus.TIMEOUT, reason="deadline"))
    )
    assert block is not None
    assert "timed out" in block.text


def test_tool_surface_warning_is_shown():
    """R7e-3c parity: the old console rendered this advisory; the TUI keeps it visible."""
    block = blocks_from_event(
        ToolSurfaceWarning(tools_chars=13_301, threshold=12_000, origins=(("mcp:big", 9000),))
    )
    assert block is not None
    assert "13301" in block.text
    assert "mcp:big" in block.text


def test_task_update_is_shown():
    class _Task:
        def render(self, *, max_items: int = 5) -> str:
            return "[ ] 1. 分析代码结构"

    block = blocks_from_event(TaskUpdated(task=_Task()))
    assert block is not None
    assert "分析代码结构" in block.text


def test_r8_chunks_reach_the_sink():
    seen: list[Block] = []
    observer = TranscriptObserver(seen.append)
    observer.on_event(ToolChunk("c1", "bash", "stdout", "step-1\n", 0))
    assert any("step-1" in b.text for b in seen)


def test_r8_chunks_are_throttled_per_call():
    seen: list[Block] = []
    clock = [0.0]
    observer = TranscriptObserver(seen.append, clock=lambda: clock[0])
    observer.on_event(ToolChunk("c1", "bash", "stdout", "one\n", 0))
    observer.on_event(ToolChunk("c1", "bash", "stdout", "two\n", 1))  # same window
    clock[0] += 0.2
    observer.on_event(ToolChunk("c1", "bash", "stdout", "three\n", 2))
    observer.on_event(ToolChunk("c2", "bash", "stdout", "other-call\n", 0))
    text = "\n".join(b.text for b in seen)
    assert "one" in text and "three" in text and "other-call" in text
    assert "two" not in text


def test_chunks_can_be_disabled():
    seen: list[Block] = []
    observer = TranscriptObserver(seen.append, chunks=False)
    observer.on_event(ToolChunk("c1", "bash", "stdout", "hidden\n", 0))
    assert seen == []
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_blocks.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'ai_coding.ui.blocks'`

- [ ] **Step 3: 实现 `blocks.py`**

```python
"""Loop events → human-readable blocks (pure: no Textual, offline-testable).

The console-era renderer (``ui/console.py::ConsoleObserver``) is deleted in
Task 7; its R8c semantics — one throttled line per tool call, quiet on a
completed turn — live here now, with an injectable clock so the throttle can
be tested without sleeping.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass

from rich.text import Text

from ai_coding.core.events import (
    IterationStarted,
    LoopEvent,
    TaskUpdated,
    ToolChunk,
    ToolFinished,
    ToolStarted,
    ToolSurfaceWarning,
    TurnFinished,
)
from ai_coding.domain.loop import TurnStatus

_PARAM_PREVIEW = 100
_OUTPUT_PREVIEW = 120
_CHUNK_THROTTLE_SECONDS = 0.15


@dataclass(frozen=True)
class Block:
    """One rendered unit of transcript content."""

    kind: str
    text: str
    style: str = ""

    def render(self) -> Text:
        return Text(self.text, style=self.style)


def _short(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def blocks_from_event(event: LoopEvent) -> Block | None:
    """Map one loop event to a block, or ``None`` when it is not shown."""
    if isinstance(event, IterationStarted):
        return Block("turn", f"[turn {event.index}/{event.max_iterations}]", "dim")
    if isinstance(event, ToolStarted):
        params = _short(json.dumps(event.params, ensure_ascii=False), _PARAM_PREVIEW)
        return Block("tool-start", f"▸ {event.tool_name}  {params}", "cyan")
    if isinstance(event, ToolFinished):
        mark = "✓" if event.success else "✗"
        preview = _short(event.output_preview or event.error or "", _OUTPUT_PREVIEW)
        tail = f"  {preview}" if preview else ""
        return Block(
            "tool-end",
            f"{mark} {event.tool_name}{tail}",
            "green" if event.success else "red",
        )
    if isinstance(event, ToolChunk):
        return Block("tool-chunk", f"│ {_short(event.text, _OUTPUT_PREVIEW)}", "dim")
    if isinstance(event, ToolSurfaceWarning):
        # R7e-3c parity: the old console showed the number, the threshold and
        # the top origins so the operator knows which server to trim.
        origins = " · ".join(f"{name}={chars}" for name, chars in event.origins[:3])
        suffix = f"  {origins}" if origins else ""
        return Block(
            "notice",
            f"[tools] {event.tools_chars} 字符（阈值 {event.threshold}）{suffix}",
            "dim",
        )
    if isinstance(event, TaskUpdated):
        return Block("task", event.task.render(), "dim")
    if isinstance(event, TurnFinished) and event.outcome.status is TurnStatus.TIMEOUT:
        return Block("turn-end", f"[turn] timed out: {event.outcome.reason}", "red")
    return None


class TranscriptObserver:
    """LoopObserver → blocks.  The chunk throttle lives here (R8c)."""

    def __init__(
        self,
        append: Callable[[Block], None],
        *,
        clock: Callable[[], float] | None = None,
        chunks: bool = True,
    ) -> None:
        self._append = append
        self._clock = clock or time.monotonic
        self._last_chunk: dict[str, float] = {}
        self._chunks = bool(chunks)

    def on_event(self, event: LoopEvent) -> None:
        if isinstance(event, ToolChunk):
            if not self._chunks:
                return
            now = self._clock()
            previous = self._last_chunk.get(event.call_id)
            if previous is not None and now - previous < _CHUNK_THROTTLE_SECONDS:
                return
            self._last_chunk[event.call_id] = now
        block = blocks_from_event(event)
        if block is not None:
            self._append(block)
```

同时收紧 Task 2 留下的临时注解：在 `ai_coding/ui/transcript.py` 里把
`def append_block(self, block: Any) -> None` 改为
`def append_block(self, block: Block) -> None`，并加
`from ai_coding.ui.blocks import Block`；`Any` 若不再使用则删除该导入（此时环不存在：`blocks` 不 import `transcript`）。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_blocks.py -q`
Expected: `10 passed`

- [ ] **Step 5: 迁移旧测试的 observer 用法**

**Parity 要求**：新映射必须覆盖旧 `ConsoleObserver` 渲染的全部 **7 类**事件（`IterationStarted` / `ToolStarted` / `ToolFinished` / `ToolChunk` / `ToolSurfaceWarning` / `TaskUpdated` / `TurnFinished(TIMEOUT)`）。任何一类都不能静默消失。

在 `tests/test_tool_chunks.py` 与 `tests/test_tool_surface_warning.py` 中：
- 删除 `from ai_coding.ui.console import ConsoleObserver, DialogueConsole`
- 把 `ConsoleObserver(DialogueConsole(console), clock=...)` 替换为 `TranscriptObserver(seen.append, clock=...)`，断言改为对 `seen` 里 `Block.text` 的断言。
- `tests/test_tool_surface_warning.py::test_the_console_renders_one_readable_line`（约 :167）：改为把 `ToolSurfaceWarning` 喂给 `TranscriptObserver(seen.append)`，断言 `seen[0].text` 里含 `13301`（tools_chars）、`12000`（threshold）与来源名——即 R7e-3c 的用户可见性在新渲染器里保住。把它重命名为 `test_the_transcript_renders_one_readable_line`。

本轮不做 `ConsoleObserver` 删除（Task 7 才删），以免中途破坏旧入口。

- [ ] **Step 6: 删除旧 observer 测试**

`tests/test_observer.py` 测的是 `BodyObserver`（Task 7 才删的旧实现）。它的 R8c 语义已由本任务的 `test_r8_*` 两个用例承接，所以现在删除该文件（旧实现本身留着，直到 Task 7）。

Run: `git rm tests/test_observer.py`
Expected: `rm 'tests/test_observer.py'`

- [ ] **Step 7: 全量回归 + lint + commit**

```bash
.venv/bin/python -m pytest -q && .venv/bin/ruff check . && .venv/bin/python -m mypy ai_coding
git add ai_coding/ui/blocks.py ai_coding/ui/transcript.py tests/test_blocks.py tests/test_tool_chunks.py tests/test_tool_surface_warning.py
git commit -m "feat(ui): loop events become blocks (R8c semantics carried over)"
```

---

### Task 4: 审批 / ask_user 模态屏

**Files:**
- Create: `ai_coding/ui/modals.py`
- Test: `tests/test_modals.py`

**Interfaces:**
- Consumes: `ai_coding.security.approval.ApprovalResult`（仅语义）；Textual `ModalScreen` / `Button` / `Input`
- Produces:
  - `ApprovalScreen(tool_name: str, params: dict, reason: str)` → `dismiss("y" | "n" | "s")`
  - `AskScreen(question: str, options: tuple[str, ...] = ())` → `dismiss(str)`；Esc/取消返回 `""`
  - `async def ask_modal(app, screen)` 由 app 侧实现（Task 5），本任务只交付屏幕本身

- [ ] **Step 1: 写失败测试**

结果用 `push_screen(screen, callback)` 的官方回调收集（`screen.returned` 不是 Textual API）。

```python
# tests/test_modals.py
"""Approval and ask_user screens: return values and Esc semantics."""

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Button, Input

from ai_coding.ui.modals import ApprovalScreen, AskScreen


class _Host(App[str]):
    def compose(self) -> ComposeResult:
        yield Button("open", id="open")

    def __init__(self) -> None:
        super().__init__()
        self.results: list[str] = []


@pytest.mark.asyncio
async def test_approval_returns_the_parser_strings():
    app = _Host()
    async with app.run_test() as pilot:
        for button_id, expected in (("#yes", "y"), ("#no", "n"), ("#stop", "s")):
            app.push_screen(
                ApprovalScreen("bash", {"command": "ls"}, "read-only"),
                app.results.append,
            )
            await pilot.pause()
            await pilot.click(button_id)
            await pilot.pause()
            assert app.results[-1] == expected


@pytest.mark.asyncio
async def test_approval_shows_tool_and_reason():
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(ApprovalScreen("bash", {"command": "ls"}, "read-only"))
        await pilot.pause()
        text = app.screen.query_one("#approval-body").content.plain
        assert "bash" in text
        assert "read-only" in text


@pytest.mark.asyncio
async def test_ask_returns_typed_answer():
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(AskScreen("Which file?", ("a.py", "b.py")), app.results.append)
        await pilot.pause()
        app.screen.query_one("#ask-input", Input).value = "a.py"
        await pilot.click("#ask-send")
        await pilot.pause()
        assert app.results[-1] == "a.py"


@pytest.mark.asyncio
async def test_ask_escape_returns_empty_string():
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(AskScreen("Which file?"), app.results.append)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.results[-1] == ""


@pytest.mark.asyncio
async def test_ask_answer_does_not_leak_to_the_apps_submit_handler():
    """A modal answer must never bubble up as a main-prompt submission."""
    seen: list[str] = []

    class _HostApp(_Host):
        def on_input_submitted(self, event: Input.Submitted) -> None:
            seen.append(event.value)

    app = _HostApp()
    async with app.run_test() as pilot:
        app.push_screen(AskScreen("Which file?"), app.results.append)
        await pilot.pause()
        app.screen.query_one("#ask-input", Input).value = "a.py"
        await pilot.press("enter")
        await pilot.pause()
        assert app.results[-1] == "a.py"
        assert seen == []
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_modals.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'ai_coding.ui.modals'`

- [ ] **Step 3: 实现 `modals.py`**

```python
"""Modal screens for the two human channels: approval and ask_user.

Both return the *strings the existing parsers expect*: approval answers are
parsed by ``security/approval.py`` (``y``/``n``/``s``), and an empty ask answer
becomes ``UserAnswer(reason="empty")`` → the tool tells the model to decide
(spec §4.2).  Escape therefore returns ``""``, never raises.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static


class ApprovalScreen(ModalScreen[str]):
    """Ask whether one tool call may proceed."""

    BINDINGS = [Binding("escape", "no", "No", show=False)]

    def __init__(self, tool_name: str, params: dict[str, Any], reason: str) -> None:
        super().__init__()
        self._tool_name = tool_name
        self._params = params
        self._reason = reason

    def compose(self) -> ComposeResult:
        body = Text()
        body.append(f"{self._tool_name}\n", style="bold")
        if self._reason:
            body.append(f"{self._reason}\n", style="yellow")
        preview = str(self._params)
        body.append(preview if len(preview) <= 200 else preview[:197] + "...", style="dim")
        with Vertical(id="approval-box"):
            yield Static(body, id="approval-body")
            with Horizontal(id="approval-buttons"):
                yield Button("Allow", id="yes", variant="success")
                yield Button("Deny", id="no", variant="error")
                yield Button("Stop", id="stop", variant="warning")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        # ``event.button.id`` is ``str | None``; an unknown button fails closed.
        self.dismiss({"yes": "y", "no": "n", "stop": "s"}.get(event.button.id or "", "n"))

    def action_no(self) -> None:
        self.dismiss("n")


class AskScreen(ModalScreen[str]):
    """Ask the human a free-form question (options are shown, not enforced)."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, question: str, options: tuple[str, ...] = ()) -> None:
        super().__init__()
        self._question = question
        self._options = tuple(options)

    def compose(self) -> ComposeResult:
        with Vertical(id="ask-box"):
            yield Static(self._question, id="ask-question")
            if self._options:
                yield Static("可选：" + " / ".join(self._options), id="ask-options")
            yield Input(id="ask-input")
            yield Button("Send", id="ask-send", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#ask-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self._send()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # The modal owns its input: stop the Submitted message so an App-level
        # handler (the main prompt, Task 5) never sees a modal answer as a turn.
        event.stop()
        self._send()

    def _send(self) -> None:
        self.dismiss(self.query_one("#ask-input", Input).value.strip())

    def action_cancel(self) -> None:
        self.dismiss("")
```

- [ ] **Step 4: 给 `app.tcss` 补模态样式**

在文件末尾追加：

```css
ApprovalScreen,
AskScreen {
    align: center middle;
}

#approval-box,
#ask-box {
    width: 70;
    height: auto;
    padding: 1 2;
    border: round $primary;
    background: $surface-lighten-1;
}

#approval-buttons {
    height: auto;
    align-horizontal: right;
}

#approval-buttons Button {
    margin-left: 2;
}

#ask-input {
    margin-top: 1;
}

#ask-send {
    margin-top: 1;
}
```

- [ ] **Step 5: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_modals.py -q`
Expected: `5 passed`

- [ ] **Step 6: 全量回归 + lint + commit**

```bash
.venv/bin/python -m pytest -q && .venv/bin/ruff check . && .venv/bin/python -m mypy ai_coding
git add ai_coding/ui/modals.py ai_coding/ui/app.tcss tests/test_modals.py
git commit -m "feat(ui): approval and ask_user modal screens"
```

**落地偏差（review 后修正，已通过 scoped re-review）**：`ApprovalScreen.__init__` 的 `params: dict` → `dict[str, Any]`（strict mypy）；`AskScreen` 的 question/options 用 `rich.text.Text` 包裹（原始字符串会被当 Rich markup 解析，模型给出的 `[/path]` 会 `MarkupError`）；新增 3 个回归测试（审批 Esc→`"n"`、未知 id→`"n"`、带方括号的问题不入 markup）。实现提交：`87b4a47` + `2c9f1e2`。

---

### Task 5: 循环接线（提交、流式、事件、turn 守卫）

**Files:**
- Modify: `ai_coding/ui/textual_app.py`
- Test: `tests/test_textual_app.py`（追加）

**Interfaces:**
- Consumes: `Transcript`（Task 2）、`StatusBar`、`InputBar`、`TranscriptObserver` + `Block`（Task 3）、`ApprovalScreen` / `AskScreen`（Task 4）
- Produces:
  - `AICodingApp.run_turn(text)`（`@work(exclusive=True, group="turn")`）
  - `AICodingApp._on_delta(text: str)`（同步 sink）
  - `AICodingApp.approval_provider(prompt: str) -> str`（async，`try/finally` 关屏）
  - `AICodingApp.ask_provider(prompt: str) -> str`（async，同上）
  - `run_app(path, workspace, skills)`（完整版）

- [ ] **Step 1: 写失败测试（用 FakeLoop，不碰真模型）**

```python
# 追加到 tests/test_textual_app.py
from types import SimpleNamespace

from textual.widgets import Markdown, Static


class _FakeLoop:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.workspace = "/tmp/ws"
        self.model_name = "fake-model"

    async def process_input(self, session, text, *, on_delta=None, observer=None):
        self.calls.append(text)
        if on_delta is not None:
            on_delta("## 回答\n\n")
            on_delta("**好**")
        return "## 回答\n\n**好**"

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_submit_runs_the_loop_and_streams_markdown():
    loop = _FakeLoop()
    app = _app(loop=loop)
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "你好"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        assert loop.calls == ["你好"]
        assert "你好" in _all_static_text(app)
        assert "好" in app.query_one(Markdown).source


@pytest.mark.asyncio
async def test_input_is_disabled_while_a_turn_runs():
    loop = _BlockingLoop()
    app = _app(loop=loop)
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "第一次"
        await pilot.press("enter")
        await pilot.pause()
        assert app.query_one("#prompt").disabled
        loop.release()
        await pilot.pause()
        await pilot.pause()
        assert not app.query_one("#prompt").disabled


class _BlockingLoop(_FakeLoop):
    def __init__(self) -> None:
        super().__init__()
        self._gate = asyncio.Event()

    def release(self) -> None:
        self._gate.set()

    async def process_input(self, session, text, *, on_delta=None, observer=None):
        await self._gate.wait()
        return await super().process_input(session, text, on_delta=on_delta, observer=observer)


def _all_static_text(app) -> str:
    return "\n".join(w.content.plain for w in app.query(Static) if hasattr(w.content, "plain"))
```

并在文件头补 `import asyncio`。

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_textual_app.py -q`
Expected: FAIL — 提交后没有 transcript 内容（或 `#prompt` 不存在行为）

- [ ] **Step 3: 实现接线**

在 `textual_app.py` 中补齐（完整新版）：

```python
"""The Textual application: shell + loop wiring."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Input

from ai_coding.ui.blocks import Block, TranscriptObserver
from ai_coding.ui.input_bar import InputBar
from ai_coding.ui.modals import ApprovalScreen, AskScreen
from ai_coding.ui.status_bar import StatusBar
from ai_coding.ui.transcript import Transcript

_FLUSH_SECONDS = 0.05


class AICodingApp(App[None]):
    """Full-screen coding assistant UI."""

    CSS_PATH = "app.tcss"
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", show=False, priority=True),
        Binding("ctrl+c", "quit", "Quit", show=False),
    ]

    # Declared here (not only assigned in on_mount) so mypy strict can infer them.
    _loop: Any
    _session: Any
    _sessions: Any
    _workspace: str
    _model_name: str
    _status: StatusBar
    _transcript: Transcript
    _input: Input
    _observer: TranscriptObserver
    _turn_started: float

    def __init__(
        self,
        *,
        loop: Any,
        session: Any,
        sessions: Any,
        workspace: str,
        model_name: str = "",
    ) -> None:
        super().__init__()
        self._loop = loop
        self._session = session
        self._sessions = sessions
        self._workspace = workspace
        self._model_name = model_name
        self._observer = TranscriptObserver(self._append_block)
        self._turn_started = time.monotonic()

    # ---- shell -----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield StatusBar(self._workspace, self._model_name)
        yield Transcript()
        yield InputBar()

    def on_mount(self) -> None:
        self._status = self.query_one(StatusBar)
        self._transcript = self.query_one(Transcript)
        self._input = self.query_one("#prompt", Input)
        self.set_interval(_FLUSH_SECONDS, self._flush_stream)

    # ---- streaming (sync sink → buffered flush) ---------------------------

    def _on_delta(self, text: str) -> None:
        self._transcript.stream(text)

    def _flush_stream(self) -> None:
        self._transcript.flush()

    def _append_block(self, block: Block) -> None:
        self._transcript.append_block(block)

    # ---- turn ------------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text or self._input.disabled:
            return
        self._input.value = ""
        self._transcript.append_user(text, _clock())
        self.run_turn(text)

    @work(exclusive=True, group="turn", thread=False)
    async def run_turn(self, text: str) -> None:
        self._input.disabled = True
        self._status.set_busy(True)
        self._turn_started = time.monotonic()
        try:
            self._transcript.begin_assistant(_clock())
            await self._loop.process_input(
                self._session, text, on_delta=self._on_delta, observer=self._observer
            )
        finally:
            await self._transcript.end_assistant()
            self._input.disabled = False
            self._status.set_busy(False)
            self._input.focus()

    # ---- human channels --------------------------------------------------

    async def approval_provider(self, prompt: str) -> str:
        """Ask through a modal; return the string ``approval.py`` parses.

        ``prompt`` is the whole formatted text (spec §4.2: the provider
        interface flattens tool name + reason + params), so it is shown as the
        body and the tool name comes from its first line.
        """
        screen = ApprovalScreen(tool_name=_first_line(prompt), params={}, reason=prompt)
        try:
            # ``str()`` silences mypy's warn_return_any: push_screen_wait is typed
            # as ``ScreenResultType | Any``.
            return str(await self.push_screen_wait(screen))
        finally:
            # push_screen_wait is shielded: a cancelled turn does not close the
            # screen (spec §3.3-5), so close it here.
            if self.screen is screen:
                self.pop_screen()

    async def ask_provider(self, prompt: str) -> str:
        screen = AskScreen(prompt)
        try:
            return str(await self.push_screen_wait(screen))
        finally:
            if self.screen is screen:
                self.pop_screen()


def _first_line(text: str) -> str:
    return text.splitlines()[0] if text else "?"


def _clock() -> str:
    return datetime.now().strftime("%H:%M")
```

- [ ] **Step 4: 完整 `run_app`（本任务交付）**

```python
async def run_app(path: str | None, workspace: str, skills: str | None) -> None:
    """Build the world and run the Textual app (signature kept from cli.py)."""
    from ai_coding.cli import (
        _build_loop,
        _build_tool_registry,
        _make_guards,
        _make_hooks,
        _make_recorder,
    )
    from ai_coding.config.loader import ConfigManager
    from ai_coding.core.mcp import MCPManager
    from ai_coding.core.steering import SteeringQueue
    from ai_coding.security.approval import CLIApprovalCallback
    from ai_coding.security.user_prompt import CLIUserPromptCallback
    from ai_coding.service.session_service import SessionService

    manager = ConfigManager()
    manager.initialize(path)
    app_cfg = manager.app

    from ai_coding.ai.factory import build_engine_from_config
    from ai_coding.ai.prompt import BASE_INSTRUCTIONS

    engine = build_engine_from_config(app_cfg)
    user_prompt = CLIUserPromptCallback()
    registry = _build_tool_registry(
        app_cfg, workspace, skills,
        user_prompt=user_prompt, base_instructions=BASE_INSTRUCTIONS,
    )

    steering = SteeringQueue()
    sessions = SessionService()
    recorder = _make_recorder(app_cfg, workspace, None)
    mcp_manager = MCPManager(app_cfg.mcp, observer=recorder)
    for tool in await mcp_manager.open_all():
        registry.register(tool)

    approval = CLIApprovalCallback()
    loop = _build_loop(
        engine, sessions, registry, app_cfg, workspace, skills,
        approval=approval,
        observer=None,
        trace=recorder,
        guards=_make_guards(app_cfg),
        hooks=_make_hooks(app_cfg, workspace),
        steering=steering,
    )

    model = getattr(getattr(loop, "engine", None), "model_config", None)
    model_name = getattr(model, "name", "") or ""

    app = AICodingApp(
        loop=loop, session=sessions.create(), sessions=sessions,
        workspace=workspace, model_name=model_name,
    )
    approval.set_provider(app.approval_provider)
    user_prompt.set_provider(app.ask_provider)

    try:
        await app.run_async()
    finally:
        await loop.aclose()
        await mcp_manager.aclose()
```

- [ ] **Step 5: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_textual_app.py -q`
Expected: `4 passed`

- [ ] **Step 6: 真实运行一次（本地、有配置时）**

Run: `.venv/bin/python -c "import asyncio; from ai_coding.ui.textual_app import run_app; asyncio.run(run_app(None, '.', None))"`
Expected: 全屏 TUI 打开；输入一句短话，回答流式出现；`ctrl+q` 退出（`/` 命令在 Task 6 才接）。
（若本机没有可用 API key，跳过本步，改由 Task 8 覆盖。）

- [ ] **Step 7: 全量回归 + lint + commit**

```bash
.venv/bin/python -m pytest -q && .venv/bin/ruff check . && .venv/bin/python -m mypy ai_coding
git add ai_coding/ui/textual_app.py tests/test_textual_app.py
git commit -m "feat(ui): the loop is wired — submit, stream, events, turn guard"
```

**落地偏差（实现时发现的硬冲突/必要修正，均已带注释落地）**：
1. **`self._loop` → `self._agent_loop`**：Textual 的 `App` 自己拥有 `_loop`（asyncio 事件循环，`run_async` 会覆写它），本计划原命名会被覆盖成事件循环对象。Task 1 的同类命名一并随之修正，Task 6 的 snippet 已同步更新。
2. **`on_mount` 里补 `self._input.focus()`**：`VerticalScroll` 可聚焦，Textual 的自动聚焦会落在 transcript 上，输入框拿不到键盘。
3. **provider 用 `answer: str = await self.push_screen_wait(screen)` 注解**（而非 `str(...)` 包裹）：注解才是把 `ScreenResultType` 绑到 `str` 的方式，否则 strict mypy 下解析为 `Screen[object]`。
4. 测试 helper `_all_static_text` 需兼容 `str` 型 content（不是所有 `Static.content` 都有 `.plain`）。

---

### Task 6: slash 命令 + 4.1 缺陷修复 + pty E2E

**Files:**
- Modify: `ai_coding/ui/console.py`（加 `TextualConsole`）
- Modify: `ai_coding/ui/textual_app.py`（slash 分支、provider 回填）
- Create: `tests/_pty_textual_child.py`
- Test: `tests/test_pty_textual.py`、`tests/test_textual_app.py`（追加）

**Interfaces:**
- Consumes: `ai_coding.ui.repl.handle_input(loop, text, session, sessions, console)`、`ai_coding.ui.console.DialogueConsole`
- Produces:
  - `TextualConsole(DialogueConsole)`：`delta/info/error/rule` 写入 transcript（`delta` 追加到当前流式块；无流式块时新建一条 assistant 块）
  - `AICodingApp._ensure_approval_provider()`（4.1 修复）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_textual_app.py 追加
from ai_coding.ui.console import TextualConsole

@pytest.mark.asyncio
async def test_slash_command_runs_through_handle_input(monkeypatch):
    loop = _FakeLoop()
    app = _app(loop=loop)
    seen: list[str] = []

    async def fake_handle_input(loop_, text, session, sessions, console):
        seen.append(text)
        console.info("[status] ok")
        from ai_coding.ui.repl import REPLAction
        return REPLAction.CONTINUE, session

    monkeypatch.setattr("ai_coding.ui.textual_app.handle_input", fake_handle_input)
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/status"
        await pilot.press("enter")
        await pilot.pause()
        assert seen == ["/status"]
        assert "[status] ok" in _all_static_text(app)
        assert loop.calls == []  # slash does not go to the model


@pytest.mark.asyncio
async def test_permissions_switch_re_attaches_the_provider():
    from ai_coding.security.approval import CLIApprovalCallback

    app = _app(loop=_FakeLoop())
    async with app.run_test() as pilot:
        fresh = CLIApprovalCallback()
        dispatcher = SimpleNamespace(approval=fresh)
        app._ensure_approval_provider(dispatcher)
        assert fresh._provider == app.approval_provider
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_textual_app.py -q`
Expected: FAIL — `TextualConsole` 不存在 / `_ensure_approval_provider` 不存在

- [ ] **Step 3: 实现 `TextualConsole` 与 4.1 修复**

在 `ai_coding/ui/console.py` 末尾追加：

```python
class TextualConsole(DialogueConsole):
    """DialogueConsole that writes into the Textual transcript instead of stdout.

    Subclassing (rather than duck-typing) keeps ``repl.py``'s annotations true
    without touching that file.
    """

    def __init__(self, transcript: Any = None) -> None:
        # Intentionally does not call super().__init__: no rich Console needed.
        # The default keeps the override Liskov-compatible with DialogueConsole
        # (mypy strict); the app always passes a transcript.
        self._transcript = transcript
        self._streaming = False

    def delta(self, token: str) -> None:
        if not self._streaming:
            from datetime import datetime

            self._transcript.begin_assistant(datetime.now().strftime("%H:%M"))
            self._streaming = True
        self._transcript.stream(token)

    def _end_stream(self) -> None:
        if self._streaming:
            self._transcript.flush()
            self._streaming = False

    def info(self, message: str) -> None:
        self._end_stream()
        from ai_coding.ui.blocks import Block

        self._transcript.append_block(Block("info", message))

    def error(self, message: str) -> None:
        self._end_stream()
        from ai_coding.ui.blocks import Block

        self._transcript.append_block(Block("turn-end", message))

    def rule(self, title: str = "") -> None:
        self._end_stream()
        if title:
            from ai_coding.ui.blocks import Block

            self._transcript.append_block(Block("turn", title))
```

在 `textual_app.py` 中：
- 顶部导入改为模块级 `from ai_coding.ui.repl import REPLAction, handle_input`（便于 monkeypatch），并在 `on_input_submitted` 分流：

```python
    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text or self._input.disabled:
            return
        self._input.value = ""
        self._transcript.append_user(text, _clock())
        if text.startswith("/"):
            self.run_command(text)
        else:
            self.run_turn(text)

    @work(exclusive=True, group="turn", thread=False)
    async def run_command(self, text: str) -> None:
        from ai_coding.ui.console import TextualConsole

        console = TextualConsole(self._transcript)
        try:
            action, new_session = await handle_input(
                self._agent_loop, text, self._session, self._sessions, console
            )
            if action is REPLAction.NEW_SESSION:
                self._session = new_session
            elif action is REPLAction.QUIT:
                self.exit()
        finally:
            self._ensure_approval_provider()

    def _ensure_approval_provider(self, dispatcher: Any | None = None) -> None:
        """4.1: ``/permissions confirm`` builds a fresh callback with no provider.

        Called after every slash command; ``dispatcher`` is injectable so the
        regression test can pass a throwaway one instead of a real loop.
        """
        from ai_coding.security.approval import CLIApprovalCallback

        if dispatcher is None:
            dispatcher = getattr(self._agent_loop, "dispatcher", None)
        callback = getattr(dispatcher, "approval", None) if dispatcher else None
        if isinstance(callback, CLIApprovalCallback):
            callback.set_provider(self.approval_provider)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_textual_app.py -q`
Expected: `6 passed`

- [ ] **Step 5: 写 pty E2E 驱动脚本**

```python
# tests/_pty_textual_child.py
"""Drive the Textual app in a real pty: approve once, answer one ask_user.

Prints one JSON line at the end: {"approved": bool, "answer": str}.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai_coding.ui.textual_app import AICodingApp  # noqa: E402


class _ScriptedLoop:
    """One turn, no model: an approval, then an ask_user, then exit."""

    def __init__(self) -> None:
        self.workspace = "/tmp/ws"
        self.results: dict[str, object] = {"approved": False, "answer": ""}
        self._app: AICodingApp | None = None

    async def process_input(self, session, text, *, on_delta=None, observer=None):
        from ai_coding.security.approval import CLIApprovalCallback

        callback = CLIApprovalCallback()
        app = self._app
        callback.set_provider(app.approval_provider)
        result = await callback.approve("bash", {"command": "ls"}, "read-only")
        self.results["approved"] = result.value == "yes"

        from ai_coding.security.user_prompt import CLIUserPromptCallback

        ask = CLIUserPromptCallback()
        ask.set_provider(app.ask_provider)
        answer = await ask.ask("Which file?")
        self.results["answer"] = answer.text
        app.exit()
        return "done"

    async def aclose(self):
        return None


async def main() -> None:
    loop = _ScriptedLoop()
    app = AICodingApp(loop=loop, session=object(), sessions=object(), workspace="/tmp/ws")
    loop._app = app
    await app.run_async()
    print(json.dumps(loop.results, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 6: 写 pty 测试**

```python
# tests/test_pty_textual.py
"""Approval and ask_user E2E in a real pty (replaces the deleted run_repl tests).

The child is a real Textual app with a real terminal attached; the test plays a
human: submit a line (which starts the scripted turn), press Enter on the
focused Allow button of the approval modal, then type the ask answer.
A pty has no synchronisation point other than the clock, so the schedule below
is wall-clock; if the first run is flaky, print ``result["output"]`` to see the
screen and adjust the delays — never skip or delete the test.
"""

from __future__ import annotations

import os
import pty
import select
import subprocess
import sys
import time

CHILD = os.path.join(os.path.dirname(__file__), "_pty_textual_child.py")


def _drive(script: list[tuple[float, str]], timeout: float = 25.0) -> dict:
    """Run the child in a pty; send each ``(at_seconds, keys)`` when its time comes."""
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        [sys.executable, CHILD],
        stdin=slave, stdout=slave, stderr=slave, close_fds=True,
        env={**os.environ, "TERM": "xterm-256color"},
    )
    os.close(slave)
    output = b""
    started = time.time()
    step = 0
    try:
        while time.time() - started < timeout:
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    output += os.read(master, 65536)
                except OSError:
                    break
            if step < len(script) and time.time() - started >= script[step][0]:
                os.write(master, script[step][1].encode())
                step += 1
            if proc.poll() is not None:
                break
    finally:
        try:
            os.close(master)
        except OSError:
            pass
    return {"output": output.decode(errors="replace"), "code": proc.wait()}


def test_approval_and_ask_user_reach_the_tools():
    result = _drive([
        (1.5, "go\r"),    # submit a line → the scripted turn starts
        (3.5, "\r"),      # approval modal: Enter activates the focused Allow
        (5.5, "a.py\r"),  # ask modal: type an answer and submit
        (7.5, "\r"),      # nudge in case the answer needed one more Enter
    ])
    assert result["code"] == 0, result["output"]
    assert '"approved": true' in result["output"], result["output"]
    assert '"answer": "a.py"' in result["output"], result["output"]
```

- [ ] **Step 7: 运行 pty 测试并调通**

Run: `.venv/bin/python -m pytest tests/test_pty_textual.py -q`
Expected: `1 passed`（若时序不稳，按实际输出调整等待条件；不得跳过或删除）

- [ ] **Step 8: 全量回归 + lint + commit**

```bash
.venv/bin/python -m pytest -q && .venv/bin/ruff check . && .venv/bin/python -m mypy ai_coding
git add ai_coding/ui/console.py ai_coding/ui/textual_app.py tests/test_textual_app.py \
  tests/_pty_textual_child.py tests/test_pty_textual.py
git commit -m "feat(ui): slash commands, the /permissions provider fix, and a pty E2E"
```

---

### Task 7: 入口切换 + 弃用清理

**Files:**
- Modify: `ai_coding/cli.py:54`（`from ai_coding.ui.app import run_app` → `from ai_coding.ui.textual_app import run_app`）
- Modify: `pyproject.toml`（删除 `"prompt-toolkit>=3.0",`）
- Delete: `ai_coding/ui/app.py`、`body.py`、`header.py`、`footer.py`、`palette.py`、`scroll.py`、`theme.py`、`markdown.py`、`observer.py`
- Delete: `tests/test_app.py`、`test_body.py`、`test_header.py`、`test_footer.py`、`test_palette.py`、`test_scroll.py`、`test_theme.py`、`test_markdown.py`
- Delete: `tests/_pty_ask_user_child.py`、`tests/test_repl_ask_user_pty.py`、`tests/test_repl_ask_user_stdin.py`
- Delete: `ai_coding/ui/repl.py::run_repl`（及 `__all__` 中的 `run_repl`）、`security/user_prompt.py::set_reader`/`set_interactive`/`_interactive`
- Modify: `scripts/r7_cases.py`（删掉调用 pty child 的用例）
- Modify: `ai_coding/ui/console.py`（删 `ConsoleObserver`）
- Modify: `docs/features.md`、`docs/README.md`、`docs/architecture.md`、`docs/how-to-run.md`

**Interfaces:**
- Consumes: Task 6 的 pty 测试必须已绿（删除旧测试的硬性前置）
- Produces: 单一入口 `ai-coding` → Textual app；`prompt-toolkit` 从依赖消失

- [ ] **Step 1: 前置检查（不许跳过）**

Run: `.venv/bin/python -m pytest tests/test_pty_textual.py tests/test_blocks.py tests/test_textual_app.py -q`
Expected: 全绿。否则**停止**，先修 Task 6。

- [ ] **Step 2: 切换入口**

```bash
sed -i 's/from ai_coding.ui.app import run_app/from ai_coding.ui.textual_app import run_app/' ai_coding/cli.py
grep -n "run_app" ai_coding/cli.py
```
Expected: 只剩 `from ai_coding.ui.textual_app import run_app` 与 `loop.run_until_complete(run_app(path, workspace, skills))`

- [ ] **Step 3: 删除旧 UI 与旧测试**

```bash
git rm ai_coding/ui/app.py ai_coding/ui/body.py ai_coding/ui/header.py ai_coding/ui/footer.py \
  ai_coding/ui/palette.py ai_coding/ui/scroll.py ai_coding/ui/theme.py ai_coding/ui/markdown.py \
  ai_coding/ui/observer.py \
  tests/test_app.py tests/test_body.py tests/test_header.py tests/test_footer.py \
  tests/test_palette.py tests/test_scroll.py tests/test_theme.py tests/test_markdown.py \
  tests/_pty_ask_user_child.py tests/test_repl_ask_user_pty.py tests/test_repl_ask_user_stdin.py
```

- [ ] **Step 4: 删除 `ConsoleObserver`**

编辑 `ai_coding/ui/console.py`：删除 `ConsoleObserver` 类（约 62-118 行）与 `__all__` 中的对应项，并删掉只服务它的导入（`IterationStarted`、`ToolChunk`、`ToolFinished`、`ToolStarted`、`ToolSurfaceWarning`、`TurnFinished`、`TurnStatus`、`json`、`time`、`_short` 等，视删除后是否仍被使用而定）。

同时编辑 `tests/test_m6_contract_coverage.py`：删除两个 `ConsoleObserver` 用例（`test_console_observer_reports_a_timed_out_turn`、`test_console_observer_stays_quiet_on_a_completed_turn`）——它们锁定的 R8c 语义已在 `tests/test_blocks.py` 有对应用例；**保留** `test_console_delta_info_error_rule_render`（测的是 `DialogueConsole`，仍在使用）。

Run:
```bash
grep -rn "ConsoleObserver" ai_coding/ tests/ --include=*.py | grep -v __pycache__
```
Expected: **0 行**。

- [ ] **Step 5: 删除 `run_repl` 与连带死代码**

- `ai_coding/ui/repl.py`：删 `run_repl`（298-354 行）、`__all__` 中的 `"run_repl"`，并改写文件头 docstring（去掉 prompt_toolkit 懒加载的描述）。
- `ai_coding/security/user_prompt.py`：删 `set_reader`、`set_interactive`、`_interactive`、`ask()` 里的 interactive 分支，以及类 docstring 中 R7c/R7e-2 段落。
- `scripts/r7_cases.py`：删掉调用 `tests/_pty_ask_user_child.py` 的用例（378 行附近）。
- `tests/test_user_prompt.py`：删 4 个用例（`test_set_reader_replaces_the_input_factory`、其 EOF 变体、两个 `set_interactive` 用例）。

- [ ] **Step 6: 删除依赖**

编辑 `pyproject.toml`，删掉 `"prompt-toolkit>=3.0",`。

Run:
```bash
grep -rn "prompt_toolkit" ai_coding/ tests/ scripts/ --include=*.py | grep -v __pycache__
```
Expected: **0 行**。若有残留，说明漏删，逐一处理。

- [ ] **Step 7: 修掉最后一个既存 mypy 错误**

删掉 `ui/app.py` 后，`mypy ai_coding` 只应剩 `core/agent_loop.py:243`：
`"ChatEngine" has no attribute "model_config"`（`set_model` 给引擎赋了这个属性，但端口没声明）。

修法（**纯注解，零行为变更**，`Any` 已在文件里导入）：在 `ai_coding/ai/engine.py` 的 `ChatEngine` 类体里、`step` 定义之前加：

```python
    #: Set by ``AgentLoop.set_model``; concretely a ``ModelConfig``.  Declared
    #: here (typing only) so the port matches how the loop actually uses it.
    model_config: Any
```

Run: `.venv/bin/python -m mypy ai_coding`
Expected: `Success: no issues found in 110 source files`（若此修法引出别的错误，说明不止一行——回退该改动，把错误记进 ledger 并在最终汇报给用户，不要擅自扩大改动）。

- [ ] **Step 8: 全量回归**

Run: `.venv/bin/python -m pytest -q`
Expected: 全绿；总数比 Task 6 结束时减少（旧测试删除 + 新测试加入），失败数为 0。

- [ ] **Step 9: 更新文档**

- `docs/features.md`：重写「R10 已知缺口」节 → 改为「R12：Textual UI（v1 会话闭环）」，列出已接线项（事件→块、提交、审批、ask_user、slash、`/model`）、v1 明确不做项（欢迎页/命令面板/侧栏/计划卡、Esc 取消、steering）、验证方式（`run_test` + pty + 真实终端）。
- `docs/README.md`：把「终端界面重构（R10）」的描述更新为 Textual 版本，删掉「四处数据流尚未接线」的过时段落。
- `docs/architecture.md`：ui 行更新为新模块清单（textual_app / transcript / blocks / widgets→transcript 等）。
- `docs/how-to-run.md`：删掉「输出区尚未接上循环事件」的限制说明；补复制（ctrl+c / Shift+拖选）与退出（ctrl+q）说明。

- [ ] **Step 10: lint + 全量门禁 + commit**

```bash
.venv/bin/python -m pytest -q && .venv/bin/ruff check . && .venv/bin/python -m mypy ai_coding
git add -A
git commit -m "refactor(ui): the Textual app is the only UI; prompt-toolkit is gone"
```

---

### Task 8: 真实终端验收 + 全量回归

**Files:** 无代码改动（除非验收发现问题）

**Interfaces:**
- Consumes: 前面全部任务
- Produces: 验收记录（写进 PR 描述或 commit message）

- [ ] **Step 1: 全量测试 + lint**

Run:
```bash
.venv/bin/python -m pytest -q && .venv/bin/ruff check . && .venv/bin/python -m mypy ai_coding
```
Expected: 全绿、`All checks passed!`；记录实际数字（passed / skipped）。

- [ ] **Step 2: 真实终端（人眼，逐项打勾）**

Run: `.venv/bin/ai-coding`（在真实终端里，不是 pty 脚本）

| 项 | 期望 | 结果 |
|---|---|---|
| 启动 | 状态栏 `>_ {workspace}` + `● Ready`；输入框占位符 `Type a message or enter a command...`；下方 `/ for commands` | ☐ |
| 多轮会话 | 提交两句，两条用户消息各有 `◯ You HH:MM` 头；回答为 `✦ Nexus HH:MM` + 真 markdown（代码块/列表可读） | ☐ |
| 流式 | 回答逐段出现，不是一次性刷出 | ☐ |
| turn 中 | 输入框禁用（灰）；回答完成后恢复并自动聚焦 | ☐ |
| 工具事件 | 出现 `▸ tool` / `✓ tool` 块 | ☐ |
| 滚轮 | 上滚查看历史不被打断；在底部时新内容自动跟随 | ☐ |
| 拖选复制 | 拖选高亮 → `ctrl+c` → 粘贴到别处得到所选文本（OSC52）；不支持时 Shift+拖选用终端原生选择 | ☐ |
| 退出 | `ctrl+c`（无选中）退出并打印 `bye`；有选中时 `ctrl+c` 是复制 | ☐ |
| resize | 拉窄/拉宽终端，布局重排不破版 | ☐ |
| 审批 | 触发一次需要审批的工具（如写文件）→ 模态出现 → Allow/Deny/Stop 各试一次 | ☐ |
| `/permissions confirm` 回归 | 先 `/permissions auto` 再 `/permissions confirm`，随后触发审批 → **模态出现而不是挂起**（4.1） | ☐ |
| ask_user | 让模型调用 `ask_user`（或直接构造）→ 模态输入答案 → 模型拿到答案 | ☐ |
| slash 命令 | `/status`、`/model`、`/clear` 输出进 transcript 而非 stdout | ☐ |
| 截图 | `scripts/tui_shot.py` 的 SVG 与参考图 3/4 对照（结构、层级、配色） | ☐ |

- [ ] **Step 3: 若验收发现问题**

按 TDD 修：先写失败测试（`run_test` 或 `test_blocks.py`），再修，再重跑本 task 全表。

- [ ] **Step 4: 最终 commit（如有修改）**

```bash
git add -A
git commit -m "fix(ui): real-terminal acceptance findings"
```

---

## Self-Review

**Spec coverage：**

| Spec 章节 | 落在哪个 Task |
|---|---|
| §3.1 模块（textual_app/blocks/transcript/widgets→合并进 transcript/input_bar/status_bar/modals/console/tcss） | Task 1、2、3、4、6 |
| §3.2 数据流（slash 分支、@work、50ms flush、事件→块、模态、teardown） | Task 5、6 |
| §3.3-1 同步 sink → 定时 flush | Task 2（`stream`/`flush`/`end_assistant`）+ Task 5（`set_interval`） |
| §3.3-2 async worker（禁 thread） | Task 5（`@work(exclusive=True, group="turn", thread=False)`） |
| §3.3-3 吸底策略 | Task 2（`_follow` + `is_vertical_scroll_end`） |
| §3.3-4 turn 中禁用输入 | Task 5（测试 + `_input.disabled`） |
| §3.3-5 模态 teardown | Task 4（Esc）→ Task 5（provider `try/finally`）；"超时后模态关闭"用 Task 5 的 provider 结构覆盖 |
| §4 #1–#19 集成核对表 | Task 5（#1–#5、#8–#10、#12、#14、#15）、Task 6（#6、#7、#18）、Task 8（#11、#13、#19 明确不接回） |
| §4.1 `/permissions` 缺陷 | Task 6（`_ensure_approval_provider` + 测试 + 验收表） |
| §4.2 ask_user 契约（空答案、超时、单模态） | Task 4（Esc → `""`）+ Task 6（pty E2E） |
| §4.3 回合并发 | Task 5（turn guard 测试） |
| §5.1/5.2 删除 | Task 7 |
| §5.3 保留（repl/slash/trace_render/DialogueConsole） | Task 6（TextualConsole 继承）+ Task 7（不删） |
| §5.4 run_repl 删除 + 死代码 | Task 7 |
| §5.5 依赖变更 | Task 1（加）+ Task 7（删） |
| §6.1 纯函数单测 | Task 3 |
| §6.2 行为层 run_test | Task 1、2、5、6 |
| §6.3 截图 | Task 2（脚本）+ Task 8（比对） |
| §6.4 pty E2E | Task 6 |
| §6.5 真实终端清单 | Task 8 |
| §6.6 全量回归 | 每个 Task 的收尾 + Task 7/8 |
| §7 风险 | Task 0（spike 1/2/4）、Task 2（成本）、Task 6（风险 8）、Task 8（风险 3/5） |
| §9 非目标 | Global Constraints 明示不越界 |

**Placeholder scan：** 无 TBD/TODO；无 `...` 占位实现；每个代码步骤都有可粘贴的实现。Task 6 的 pty 测试给出了完整的墙钟调度，并注明"首次运行若不稳就打印输出再调秒数，不得跳过或删除"——这是 pty 无同步点的固有性质，不是占位符。

**Type consistency：** `Block(kind, text, style)` / `blocks_from_event` / `TranscriptObserver(append, *, clock, chunks)` / `Transcript.{append_user,begin_assistant,stream,flush,end_assistant,append_block,clear}` / `ApprovalScreen(tool_name, params, reason)` / `AskScreen(question, options)` / `TextualConsole(transcript)` / `AICodingApp._ensure_approval_provider(dispatcher=None)` 在各自 Task 的 Interfaces 与代码中一致；`_app(**overrides)` 测试工厂在 Task 1 定义、Task 5/6 复用同一签名。

**已知偏差（有意为之，均在此声明）：**
1. `TranscriptObserver` 落在 `blocks.py`（spec 原文说"observer.py 重写为薄适配器"）。理由：迁移期旧 `observer.py`（BodyObserver）必须活着直到 Task 7，新适配器又是纯函数、与映射同文件最内聚。
2. `run_app` 放在 Task 5（spec §8 把它放在步骤 1）。理由：Task 1 时它还没有可接的循环，提前写会造死代码。
3. 清理（Task 7）放在入口切换之后；spec §8 的顺序一致（步骤 6 清理）。
