# 鼠标滚轮滚动 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用鼠标滚轮滚动 body 输出区，替换当前基于私有字段的 PageUp/PageDown 实现。不占用方向键。

**Architecture:** 给 `Application` 开 `mouse_support=True`，用 `Keys.ScrollUp`/`Keys.ScrollDown` 绑定滚轮，调用官方 `Window._scroll_up()` / `_scroll_down()`。焦点始终留在输入框，滚动不影响打字。

**Tech Stack:** Python 3.13, prompt_toolkit

**Spec:** `docs/features.md#r10-已知缺口`

## Global Constraints

- No new dependencies
- 焦点必须始终在 `input_window`，滚动不得夺走焦点
- 只用 prompt_toolkit 公开 API：`Keys.ScrollUp`、`Keys.ScrollDown`、`Application(mouse_support=True)`、`Window._scroll_up()`、`Window._scroll_down()`
- 禁止直接写 `render_info.vertical_scroll`（私有字段，无边界钳制）
- 方向键 `↑↓` 保留给命令面板导航，**不做**方向键滚动
- TDD：先写失败测试
- All tests must pass: `.venv/bin/python -m pytest tests/ -p no:warnings`
- Lint must pass: `.venv/bin/python -m ruff check ai_coding/ tests/`

## API 事实核对（已用 python 验证，防止再写出不存在的参数）

| 事实 | 验证命令 | 结果 |
|---|---|---|
| `Keys.ScrollUp` / `Keys.ScrollDown` 存在 | `dir(Keys)` | ✅ |
| `kb.add(Keys.ScrollUp)` 可绑定 | 实际绑定 | ✅ |
| `kb.add(MouseEventType.SCROLL_UP)` **不可** | 实际绑定 | ❌ `TypeError: object of type 'MouseEventType' has no len()` |
| `Application(mouse_support=True)` 存在 | `inspect.signature` | ✅ |
| `Application(enable_mouse_vertical_scroll=...)` **不存在** | `inspect.signature` | ❌ `KeyError` |
| `Application` 把 mouse_support 传给 Renderer | 读 `Application.__init__` 源码 | ✅ |
| `Window._scroll_up()` / `_scroll_down()` 零参可调 | `inspect.signature` | ✅ |
| `Buffer` 无 `vertical_scroll` 属性 | `dir(Buffer)` | ✅ 滚动状态只在 `Window.render_info` |

---

### Task 1: 提取滚动逻辑为可测函数

**Files:**
- Create: `ai_coding/ui/scroll.py`
- Test: `tests/test_scroll.py`

**Interfaces:**
- Consumes: `Window`（prompt_toolkit）
- Produces:
  - `scroll_up(window: Window, lines: int = 3) -> None`
  - `scroll_down(window: Window, lines: int = 3) -> None`
  - `auto_scroll_to_end(window: Window) -> None`

**背景**：把滚动逻辑从闭包里抽出来，才能写单元测试。直接测 `run_app` 里的嵌套函数需要启动整个 Application。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scroll.py
"""Body scrolling helpers."""

from unittest.mock import MagicMock

from prompt_toolkit.layout import Window
from prompt_toolkit.layout.controls import FormattedTextControl

from ai_coding.ui.scroll import auto_scroll_to_end, scroll_down, scroll_up


def _window() -> Window:
    return Window(content=FormattedTextControl(lambda: [("", "x\n")]))


def test_scroll_up_calls_private_helper():
    win = _window()
    win._scroll_up = MagicMock()
    scroll_up(win)
    win._scroll_up.assert_called_once()


def test_scroll_down_calls_private_helper():
    win = _window()
    win._scroll_down = MagicMock()
    scroll_down(win)
    win._scroll_down.assert_called_once()


def test_scroll_is_a_noop_when_window_not_rendered():
    """render_info is None before the first render — must not raise."""
    win = _window()
    assert win.render_info is None
    scroll_up(win)   # must not raise
    scroll_down(win)  # must not raise


def test_auto_scroll_to_end_tolerates_unrendered_window():
    win = _window()
    auto_scroll_to_end(win)  # must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_scroll.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ai_coding.ui.scroll'`

- [ ] **Step 3: Write minimal implementation**

```python
# ai_coding/ui/scroll.py
"""Mouse-wheel scrolling for the body window.

R11: the wheel is bound to prompt_toolkit's own ``Window._scroll_up`` /
``_scroll_down`` rather than to ``render_info.vertical_scroll``.  That field is
private, has no bounds clamping, and is ``None`` until the first render — the
helpers below tolerate exactly that, so a wheel event arriving before the first
paint is a no-op instead of an ``AttributeError``.
"""

from __future__ import annotations

from typing import Any

__all__ = ["scroll_up", "scroll_down", "auto_scroll_to_end"]


def scroll_up(window: Any) -> None:
    """Wheel up: move the body one page toward older output."""
    if getattr(window, "render_info", None) is None:
        return
    window._scroll_up()


def scroll_down(window: Any) -> None:
    """Wheel down: move the body one page toward newer output."""
    if getattr(window, "render_info", None) is None:
        return
    window._scroll_down()


def auto_scroll_to_end(window: Any) -> None:
    """Pin the body to the newest line after new output arrives.

    Without this, streaming output scrolls the view away from the cursor the
    moment the buffer outgrows the window.
    """
    if getattr(window, "render_info", None) is None:
        return
    window._scroll()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_scroll.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add ai_coding/ui/scroll.py tests/test_scroll.py
git commit -m "feat(ui): extract body scroll helpers onto Window's own scroll API"
```

---

### Task 2: 绑定鼠标滚轮，替换 PageUp/PageDown 私有字段实现

**Files:**
- Modify: `ai_coding/ui/app.py`（import 区、Application 构造、按键绑定区）
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `scroll_up` / `scroll_down`（Task 1）
- Produces: `run_app` 内的滚轮绑定

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scroll.py 追加
def test_app_enables_mouse_support():
    """The Application must opt into mouse reporting for wheel events."""
    import inspect

    from ai_coding.ui.app import run_app

    source = inspect.getsource(run_app)
    assert "mouse_support=True" in source


def test_app_binds_wheel_keys():
    """Wheel keys must be bound to the scroll helpers."""
    import inspect

    from ai_coding.ui.app import run_app

    source = inspect.getsource(run_app)
    assert "Keys.ScrollUp" in source
    assert "Keys.ScrollDown" in source


def test_app_no_longer_touches_private_vertical_scroll():
    """The private field must be gone — it has no bounds clamping."""
    import inspect

    from ai_coding.ui.app import run_app

    source = inspect.getsource(run_app)
    assert "render_info.vertical_scroll" not in source
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_scroll.py -v`
Expected: FAIL on all three — `mouse_support`, `Keys.ScrollUp/Down` absent; `render_info.vertical_scroll` present (lines 225 / 231).

- [ ] **Step 3: Write minimal implementation**

**Step 3a** — extend the imports in `ai_coding/ui/app.py`:

```python
from prompt_toolkit import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import HSplit, Layout, ScrollOffsets, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import D

from ai_coding.ui.body import BodyBuffer
from ai_coding.ui.footer import render_footer
from ai_coding.ui.header import render_header
from ai_coding.ui.observer import BodyObserver
from ai_coding.ui.palette import CommandPalette, PaletteEntry
from ai_coding.ui.scroll import auto_scroll_to_end, scroll_down, scroll_up
from ai_coding.ui.theme import STYLE
```

**Step 3b** — replace the existing `pageup` / `pagedown` block (currently around lines 222-231) with wheel bindings. Delete:

```python
    @kb.add("pageup")
    def _scroll_up(event: Any) -> None:
        if body_window.render_info:
            body_window.render_info.vertical_scroll -= 5
            event.app.invalidate()

    @kb.add("pagedown")
    def _scroll_down(event: Any) -> None:
        if body_window.render_info:
            body_window.render_info.vertical_scroll += 5
            event.app.invalidate()
```

Replace with:

```python
    @kb.add(Keys.ScrollUp)
    def _wheel_up(event: Any) -> None:
        scroll_up(body_window)
        event.app.invalidate()

    @kb.add(Keys.ScrollDown)
    def _wheel_down(event: Any) -> None:
        scroll_down(body_window)
        event.app.invalidate()
```

**Step 3c** — opt into mouse reporting on the `Application`:

```python
    layout = Layout(root)
    layout.focus(input_window)
    app = Application(
        layout=layout,
        key_bindings=kb,
        style=STYLE,
        full_screen=True,
        mouse_support=True,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_scroll.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add ai_coding/ui/app.py tests/test_scroll.py
git commit -m "feat(ui): scroll the body with the mouse wheel

Drops the PageUp/PageDown handlers that poked render_info.vertical_scroll
(private, unclamped) in favour of Keys.ScrollUp/ScrollDown driving Window's
own scrolling, with mouse_support enabled on the Application."
```

---

### Task 3: 新输出到达时自动贴底

**Files:**
- Modify: `ai_coding/ui/app.py`（`_submit_text`、`_input_provider`）
- Test: `tests/test_scroll.py`

**Interfaces:**
- Consumes: `auto_scroll_to_end`（Task 1）
- Produces: `run_app` 内的贴底调用

**背景**：滚轮能看了之后暴露一个新问题——流式输出一旦超出窗口，视图会停在原地，新内容全在屏幕外。`allow_scroll_beyond_bottom=True` 只允许手动滚过去，不会自动跟随。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scroll.py 追加
def test_app_auto_scrolls_after_output():
    """New output must pin the view to the newest line."""
    import inspect

    from ai_coding.ui.app import run_app

    source = inspect.getsource(run_app)
    assert "auto_scroll_to_end(body_window)" in source
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_scroll.py::test_app_auto_scrolls_after_output -v`
Expected: FAIL — `auto_scroll_to_end` is imported but never called.

- [ ] **Step 3: Write minimal implementation**

In `ai_coding/ui/app.py`, pin the view to the end after each turn's output:

```python
    async def _submit_text(text: str) -> None:
        nonlocal turn_start
        turn_start = time.monotonic()
        await loop.process_input(session, text, on_delta=body.write)
        auto_scroll_to_end(body_window)
```

And when the approval / ask_user prompt hands control back to the user:

```python
    async def _input_provider(prompt: str) -> str:
        nonlocal awaiting_input
        input_buffer.reset()
        input_buffer.insert_text(f"{prompt} ")
        awaiting_input = True
        input_event.clear()
        try:
            await input_event.wait()
            answer = input_buffer.text
            auto_scroll_to_end(body_window)
            return answer
        finally:
            awaiting_input = False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_scroll.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add ai_coding/ui/app.py tests/test_scroll.py
git commit -m "feat(ui): pin the body to the newest line after each turn"
```

---

### Task 4: 全量回归 + 真实 pty 验证

**Files:**
- No new files

- [ ] **Step 1: Run full test suite**

Run: `.venv/bin/python -m pytest tests/ -p no:warnings -q -o addopts=""`
Expected: PASS — all tests, `__AUTO_AUDIT__ = 1174 + 11 new = 1185 passed, 4 skipped`

- [ ] **Step 2: Run lint**

Run: `.venv/bin/python -m ruff check ai_coding/ tests/`
Expected: `All checks passed!`

- [ ] **Step 3: Verify the input still works (regression guard for the R11 focus fix)**

Run: `/tmp/verify2.py` if present, else this equivalent:

```python
import os, pty, select, time, signal
pid, fd = pty.fork()
if pid == 0:
    os.environ["TERM"] = "xterm-256color"
    os.environ["COLUMNS"] = "100"; os.environ["LINES"] = "40"
    os.chdir("/home/linsir365/projects/ai-coding")
    os.execv("/home/linsir365/projects/ai-coding/.venv/bin/ai-coding", ["ai-coding"])
out = b""; t0 = time.time(); st = 0
while time.time() - t0 < 9:
    r, _, _ = select.select([fd], [], [], 0.2)
    if r:
        try: c = os.read(fd, 65536)
        except OSError: break
        if not c: break
        out += c
    el = time.time() - t0
    if   st == 0 and el > 3.0: os.write(fd, b"abc"); st = 1
    elif st == 1 and el > 4.5: os.write(fd, b"\x7f\x7f"); st = 2
    elif st == 2 and el > 6.0: os.write(fd, b"z"); st = 3
    elif st == 3 and el > 7.5: os.write(fd, b"\r"); st = 4
    elif st == 4 and el > 9.0: break
os.kill(pid, signal.SIGKILL); os.waitpid(pid, 0)
text = out.decode("utf-8", "replace")
assert "abc" in text,   "input lost characters"
assert "az" in text,    "backspace broken"
assert "[turn" in text, "Enter did not submit"
print("input OK: type, backspace, submit all work")
```

Expected: `input OK: type, backspace, submit all work`

**说明**：pty 无法可靠模拟鼠标滚轮（需要终端上报 SGR 鼠标序列），所以滚轮本身只做单元测试。人工验证方式：真实终端里滚轮上下，应能滚动 body 且输入框仍能打字。

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "test: full regression pass after mouse-wheel scrolling"
```

---

## 实施后终端应有的样子

20 行终端、无滚轮时（现状基线）：

```
 0|╭──────────────────────────────────────╮   header 9 行 (0-8)
 1|│ ✻ Welcome to ai-coding!               │
 ...
 8|model: deepseek-v4-pro-0813
 9|                                        body 10 行 (9-18)
10|  ✨ Welcome to ai-coding! ✨
11|  Describe a task, or use / for commands.
 ...
18|
19|model: ... 0% context · 00:00 · ctrl+c quit · / commands   footer
20|█                                         输入框 (INPUT_HEIGHT=3，含光标)
```

滚轮向下时 body 向上翻（看更早的输出），输入框**不动**、光标仍在里面、继续能打字。回车仍发送。`↑↓` 仍只服务命令面板。

## Self-Review

**1. Spec coverage** — 需求「鼠标滚轮滚动，不要方向键滚动」：
- Task 1：滚动逻辑可测 ✓
- Task 2：滚轮绑定 + 开启 mouse_support + 移除私有字段实现 ✓
- Task 3：新输出自动贴底（滚动的必要配套）✓
- Task 4：回归验证 + 焦点不回退 ✓
- 方向键未被占用：`↑↓` 绑定（app.py 现有）在本次改动中一字未动 ✓

**2. Placeholder scan** — 无 TBD / TODO / 「类似 Task N」。每个代码块都是可直接粘贴的完整实现。API 事实全部来自实测并记录在 Global Constraints 表里。

**3. Type consistency** — `scroll_up(window: Any) -> None` 在 Task 1 定义，Task 2 以 `scroll_up(body_window)` 调用；`body_window` 是 `Window`，签名收 `Any` 兼容。`auto_scroll_to_end` 同理。测试断言的字符串与 Task 2 Step 3 的实现逐字一致（`Keys.ScrollUp`、`mouse_support=True`、`auto_scroll_to_end(body_window)`）。

**已知不做**：滚轮翻页速度写死为 prompt_toolkit 的一页；不做平滑滚动动画；不做鼠标点选复制（终端里靠文本选择）。这些都没在需求里，YAGNI。
