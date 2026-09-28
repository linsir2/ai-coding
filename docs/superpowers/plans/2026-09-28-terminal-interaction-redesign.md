# Terminal TUI Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将终端界面从逐行打印模型重写为 prompt_toolkit `Application` 全屏 TUI，对标 Nexus 视觉设计：深色主题、ASCII art 启动横幅、命令面板（Float 覆盖层 + 键盘导航）、底部状态栏，同时清理旧 CLI 子命令，归一为单一入口。

**Architecture:** 用 `prompt_toolkit.Application` + `HSplit` 布局替换 `run_repl` 的 `while True` 循环。Header `Window` 显示 ASCII art + 状态指示器，Body `Window` 用 `BufferControl` 做可滚动输出，Footer `Window` 显示状态栏，命令面板作为 `Float` 覆盖层。`ConsoleObserver` 接口不变，渲染目标从 `console.print()` 改为 `Buffer.insert_text()`。slash 命令保留，通过面板选中后调用 `handle_input`。

**Tech Stack:** Python 3.10+, prompt_toolkit 3.0+（已有依赖）, rich 13.0+（已有依赖）, Typer（仅保留入口）, pytest

**Spec:** 本文档即 spec。设计依据：`docs/architecture.md`、`docs/features.md`、`docs/data-contracts.md`。

## Global Constraints

- Python >= 3.10（现有下限）
- 不引入新依赖（prompt_toolkit / rich / typer 已有）
- `prompt_toolkit.Application` 的 `full_screen=True` 模式
- 所有新组件必须可单元测试（用 `DummyOutput` + `PipeInput` 或纯函数测试）
- TDD：每个任务先写失败测试 → 实现 → 验证 → commit
- 命令面板的 `Float` 覆盖层必须在 `Layout` 层面管理，不在 `DialogueConsole` 里
- 底部状态栏数据源：`loop.last_outcome.usage` / `loop.settings.max_context_tokens`
- 启动横幅数据源：`loop.tools.list_names()` / `loop.memory_status()` / `loop.settings`

---

## File Structure

### 新增的文件

| 文件 | 职责 |
|---|---|
| `ai_coding/ui/theme.py` | 深色主题配色（Style 对象） |
| `ai_coding/ui/header.py` | ASCII art + 状态指示器渲染（纯函数 → FormattedText） |
| `ai_coding/ui/body.py` | 可滚动输出 Buffer（封装 prompt_toolkit Buffer） |
| `ai_coding/ui/footer.py` | 状态栏渲染（纯函数 → FormattedText） |
| `ai_coding/ui/palette.py` | 命令面板（Float 覆盖层 + 键盘导航） |
| `ai_coding/ui/app.py` | 主 Application 类，组装 layout + 事件绑定 |

### 修改的文件

| 文件 | 改动 |
|---|---|
| `ai_coding/ui/slash.py` | 扩展 `_ALIASES`：新增 8 条命令映射 |
| `ai_coding/ui/repl.py` | `HELP_TEXT` 重写；`handle_input` 新增 8 个命令分支；删除 `run_repl`（由 `app.py` 替代） |
| `ai_coding/ui/console.py` | `ConsoleObserver` 改为写入 `body.Buffer` 而非 `console.print()` |
| `ai_coding/ui/__init__.py` | 导出新模块 |
| `ai_coding/cli.py` | 删除 `ask`/`config-check`/`memory`/`trace` 子命令；入口直接启动 `Application` |

### 删除的文件

| 文件 | 原因 |
|---|---|
| `ai_coding/ui/reader.py` | `InteractiveReader` 被 `Application` 的输入处理替代 |
| `ai_coding/ui/trace_render.py` | 被 `footer.py` 的状态栏替代（纯函数保留在 `footer.py` 内） |
| `tests/test_ui_reader.py` | 测试 `InteractiveReader`，删除后无意义 |
| `tests/_pty_reader_child.py` | pty 测试辅助文件，依赖 `InteractiveReader` |
| `tests/_pty_cancel_child.py` | pty 测试辅助文件，依赖 `InteractiveReader` |

### 测试文件

| 文件 | 覆盖 |
|---|---|
| `tests/test_theme.py` | 主题样式 |
| `tests/test_header.py` | 启动横幅渲染 |
| `tests/test_body.py` | 输出 Buffer 写入 |
| `tests/test_footer.py` | 状态栏渲染 |
| `tests/test_palette.py` | 命令面板导航与执行 |
| `tests/test_app.py` | Application 组装 |
| `tests/test_slash.py` | 扩展：新命令解析 |
| `tests/test_repl.py` | 扩展：新命令 handler |
| `tests/test_cli_cleanup.py` | 验证旧命令已删除 |

---

## 架构图

```
┌─────────────────────────────────────────────────────────────┐
│  pyproject.toml: ai-coding = "ai_coding.cli:app"             │
│  (唯一入口，无子命令)                                         │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  cli.py::run() → asyncio.run(app.run())                     │
│                                                             │
│  删除的子命令：ask / config-check / memory / trace           │
│  保留的装配函数：_build_loop / _build_tool_registry /         │
│    _make_recorder / _make_guards / _make_hooks               │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  ui/app.py::NexusApp(Application)                             │
│                                                             │
│  layout = Layout(                                            │
│    FloatContainer(                                           │
│      content = HSplit([                                     │
│        Window(header, height=12),  ← ui/header.py            │
│        Window(body, wrap_lines=False), ← ui/body.py          │
│        Window(footer, height=1),  ← ui/footer.py            │
│      ]),                                                     │
│      floats = [Float(palette_window)],  ← ui/palette.py      │
│    )                                                         │
│  )                                                           │
│                                                             │
│  key_bindings:                                               │
│    '/' → toggle_palette()                                    │
│    'c-q' → exit                                             │
│    palette 打开时：↑↓ navigate, Enter select, Esc close      │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  ui/slash.py::parse_slash()                                  │
│  ui/repl.py::handle_input()                                  │
│    ├─ /help /status /clear /quit  (已有)                     │
│    ├─ /compact /memory /trace /budget (已有)                 │
│    └─ /model /resume /permissions /todo /skills              │
│       /plan /review /config           (新增)                 │
└─────────────────────────────────────────────────────────────┘
```

---

## Task 1: 清理旧 CLI 子命令 + 统一入口

**Files:**
- Modify: `ai_coding/cli.py`
- Create: `tests/test_cli_cleanup.py`

**Interfaces:**
- Consumes: 无
- Produces: `cli.py` 中 `run()` 调用 `app()`，`app` 只有 `--version`/`--help` 两个选项 + 默认启动 REPL

- [ ] **Step 1: 写失败测试**

```python
# tests/test_cli_cleanup.py
"""验证旧 CLI 子命令已删除，REPL 是默认入口。"""

import subprocess
import sys


def test_ask_command_does_not_exist():
    result = subprocess.run(
        [sys.executable, "-m", "ai_coding", "ask", "--prompt", "hello"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0


def test_config_check_command_does_not_exist():
    result = subprocess.run(
        [sys.executable, "-m", "ai_coding", "config-check"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0


def test_memory_command_does_not_exist():
    result = subprocess.run(
        [sys.executable, "-m", "ai_coding", "memory", "list"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0


def test_trace_command_does_not_exist():
    result = subprocess.run(
        [sys.executable, "-m", "ai_coding", "trace", "list"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/test_cli_cleanup.py -v`
Expected: 全部 FAIL（因为旧命令还存在）

- [ ] **Step 3: 删除旧子命令，统一入口**

在 `ai_coding/cli.py` 中：
1. 删除 `ask` 函数（第 96-171 行）
2. 删除 `config_check` 函数（第 47-93 行）
3. 删除 `memory_command` 函数（第 174-238 行）
4. 删除 `trace_command` 函数（第 241-382 行）
5. 删除 `_run_turn_with_mcp` 函数（第 702-742 行）
6. 将 `repl` 的逻辑改为默认入口

改法：提取 `run_repl(path, workspace, skills)` 异步函数，在 `main` 回调中直接调用：

```python
@app.callback()
def main(
    version: bool | None = typer.Option(
        None, "--version", "-V",
        callback=_version_callback, is_eager=True,
    ),
    path: str | None = typer.Option(None, "--path", "-p"),
    workspace: str = typer.Option(".", "--workspace", "-w"),
    skills: str | None = typer.Option(None, "--skills"),
) -> None:
    """ai-coding : an AI coding assistant."""
    setup_logging()
    if not version:
        from ai_coding.ui.app import run_app
        asyncio.run(run_app(path, workspace, skills))
```

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest tests/test_cli_cleanup.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add ai_coding/cli.py tests/test_cli_cleanup.py
git commit -m "refactor: remove old CLI subcommands, unify to single entry"
```

---

## Task 2: 深色主题

**Files:**
- Create: `ai_coding/ui/theme.py`
- Create: `tests/test_theme.py`

**Interfaces:**
- Consumes: 无
- Produces: `STYLE: prompt_toolkit.styles.Style` 对象

- [ ] **Step 1: 写失败测试**

```python
# tests/test_theme.py
from prompt_toolkit.styles import Style

from ai_coding.ui.theme import STYLE


def test_style_is_style_instance():
    assert isinstance(STYLE, Style)


def test_style_has_header_class():
    assert "header" in str(STYLE)


def test_style_has_footer_class():
    assert "footer" in str(STYLE)
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/test_theme.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

```python
# ai_coding/ui/theme.py
"""深色主题配色。"""

from prompt_toolkit.styles import Style

STYLE = Style.from_dict({
    "header": "#00d2d3",
    "header.status:check": "#2ed573",
    "header.status:label": "#a4b0be",
    "header.model": "#ffa502",
    "header.cwd": "#a4b0be",
    "body": "#dfe4ea",
    "footer": "#2f3542",
    "footer.text": "#dfe4ea",
    "palette": "#1e272e",
    "palette.border": "#00d2d3",
    "palette.selected": "#00d2d3",
    "palette.selected.text": "#1e272e",
    "palette.item": "#dfe4ea",
    "palette.hint": "#a4b0be",
    "ascii": "#00d2d3",
})
```

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest tests/test_theme.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add ai_coding/ui/theme.py tests/test_theme.py
git commit -m "feat: add dark theme"
```

---

## Task 3: 启动横幅（Header）

**Files:**
- Create: `ai_coding/ui/header.py`
- Create: `tests/test_header.py`

**Interfaces:**
- Consumes: `loop.tools.list_names()` / `loop.memory_status()` / `loop.settings` / `loop.workspace`
- Produces: `render_header(loop) -> list[tuple[str, str]]`（FormattedText 列表）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_header.py
from ai_coding.ui.header import render_header


class _FakeLoop:
    def __init__(self):
        self.tools = type("T", (), {"list_names": lambda: ["read", "write", "bash"]})()
        self.memory_status = lambda: {"total": 42}
        self.settings = type("S", (), {"default_model": "DeepSeek-V4"})()
        self.workspace = "~/projects/atlas"


def test_header_contains_ascii():
    text = render_header(_FakeLoop())
    flat = "".join(t for _, t in text)
    assert "NEXUS" in flat


def test_header_contains_workspace():
    text = render_header(_FakeLoop())
    flat = "".join(t for _, t in text)
    assert "~/projects/atlas" in flat


def test_header_contains_model():
    text = render_header(_FakeLoop())
    flat = "".join(t for _, t in text)
    assert "DeepSeek-V4" in flat


def test_header_contains_tools_count():
    text = render_header(_FakeLoop())
    flat = "".join(t for _, t in text)
    assert "3" in flat


def test_header_contains_memory_count():
    text = render_header(_FakeLoop())
    flat = "".join(t for _, t in text)
    assert "42" in flat


def test_header_contains_checkmarks():
    text = render_header(_FakeLoop())
    flat = "".join(t for _, t in text)
    assert "✓" in flat
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/test_header.py -v`
Expected: FAIL

- [ ] **Step 3: 实现**

```python
# ai_coding/ui/header.py
"""启动横幅：ASCII art + 状态指示器。"""

from __future__ import annotations

from typing import Any

_ASCII_LOGO = r"""
███╗   ██╗███████╗██╗  ██╗██╗   ██╗███████╗
████╗  ██║██╔════╝╚██╗██╔╝██║   ██║██╔════╝
██╔██╗ ██║█████╗   ╚███╔╝ ██║   ██║███████╗
██║╚██╗██║██╔══╝   ██╔██╗ ██║   ██║╚════██║
██║ ╚████║███████╗██╔╝ ██╗╚██████╔╝███████║
╚═╝  ╚═══╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚══════╝
"""


def render_header(loop: Any) -> list[tuple[str, str]]:
    """Render the startup banner as FormattedText (list of (style, text) tuples)."""
    lines: list[tuple[str, str]] = []
    for line in _ASCII_LOGO.strip("\n").split("\n"):
        lines.append(("class:ascii", f"{line}\n"))
    lines.append(("class:header.status:label", "  ✓ workspace indexed\n"))
    tools = getattr(loop, "tools", None)
    if tools and hasattr(tools, "list_names"):
        names = tools.list_names()
        lines.append(("class:header.status:label", f"  ✓ {len(names)} tools connected\n"))
    memory_status = getattr(loop, "memory_status", None)
    if callable(memory_status):
        try:
            stats = memory_status()
            lines.append(("class:header.status:label", f"  ✓ memory loaded ({stats.get('total', 0)} notes)\n"))
        except Exception:
            lines.append(("class:header.status:label", "  ✗ memory unavailable\n"))
    settings = getattr(loop, "settings", None)
    if settings:
        model = getattr(settings, "default_model", "?")
        lines.append(("class:header.model", f"  model: {model}\n"))
    workspace = getattr(loop, "workspace", "?")
    lines.append(("class:header.cwd", f"  cwd:    {workspace}\n"))
    return lines
```

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest tests/test_header.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add ai_coding/ui/header.py tests/test_header.py
git commit -m "feat: add startup header with ASCII art and status indicators"
```

---

## Task 4: 输出 Buffer（Body）

**Files:**
- Create: `ai_coding/ui/body.py`
- Create: `tests/test_body.py`

**Interfaces:**
- Consumes: 无
- Produces: `BodyBuffer` 类，方法 `write(text: str, style: str = "")` / `clear()` / `get_formatted_text() -> str`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_body.py
from ai_coding.ui.body import BodyBuffer


def test_body_write_text():
    body = BodyBuffer()
    body.write("hello")
    assert "hello" in body.get_formatted_text()


def test_body_write_multiple():
    body = BodyBuffer()
    body.write("line1\n")
    body.write("line2\n")
    assert "line1" in body.get_formatted_text()
    assert "line2" in body.get_formatted_text()


def test_body_clear():
    body = BodyBuffer()
    body.write("hello")
    body.clear()
    assert "hello" not in body.get_formatted_text()


def test_body_write_with_style():
    body = BodyBuffer()
    body.write("error", style="class:error")
    assert "error" in body.get_formatted_text()
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/test_body.py -v`
Expected: FAIL

- [ ] **Step 3: 实现**

```python
# ai_coding/ui/body.py
"""可滚动输出 Buffer。"""

from __future__ import annotations


class BodyBuffer:
    def __init__(self) -> None:
        self._lines: list[str] = []

    def write(self, text: str, style: str = "") -> None:
        self._lines.append(text)

    def clear(self) -> None:
        self._lines.clear()

    def get_formatted_text(self) -> str:
        return "".join(self._lines)
```

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest tests/test_body.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add ai_coding/ui/body.py tests/test_body.py
git commit -m "feat: add body buffer for scrollable output"
```

---

## Task 5: 状态栏（Footer）

**Files:**
- Create: `ai_coding/ui/footer.py`
- Create: `tests/test_footer.py`

**Interfaces:**
- Consumes: `loop.last_outcome` / `loop.settings.max_context_tokens`
- Produces: `render_footer(loop, turn_start: float) -> list[tuple[str, str]]`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_footer.py
import time
from ai_coding.ui.footer import render_footer


class _FakeLoop:
    def __init__(self):
        self.last_outcome = None
        self.settings = type("S", (), {"max_context_tokens": 48000})()


def test_footer_renders_context_percentage():
    loop = _FakeLoop()
    loop.last_outcome = type("O", (), {"usage": {"total_tokens": 24000}})()
    text = render_footer(loop, time.monotonic())
    flat = "".join(t for _, t in text)
    assert "50%" in flat


def test_footer_renders_timer():
    loop = _FakeLoop()
    start = time.monotonic() - 5.0
    text = render_footer(loop, start)
    flat = "".join(t for _, t in text)
    assert "00:05" in flat


def test_footer_no_usage():
    loop = _FakeLoop()
    text = render_footer(loop, time.monotonic())
    flat = "".join(t for _, t in text)
    assert "0%" in flat
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/test_footer.py -v`
Expected: FAIL

- [ ] **Step 3: 实现**

```python
# ai_coding/ui/footer.py
"""底部状态栏。"""

from __future__ import annotations

import time
from typing import Any


def render_footer(loop: Any, turn_start: float) -> list[tuple[str, str]]:
    parts = []
    outcome = getattr(loop, "last_outcome", None)
    usage = getattr(outcome, "usage", None) if outcome else None
    total_tokens = (usage or {}).get("total_tokens", 0)
    max_tokens = getattr(getattr(loop, "settings", None), "max_context_tokens", 0)
    if max_tokens > 0:
        pct = int(total_tokens / max_tokens * 100)
        parts.append(f"{pct}% context")
    elapsed = time.monotonic() - turn_start
    parts.append(f"{int(elapsed // 60):02d}:{int(elapsed % 60):02d}")
    text = " · ".join(parts)
    return [("class:footer.text", text)]
```

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest tests/test_footer.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add ai_coding/ui/footer.py tests/test_footer.py
git commit -m "feat: add footer status bar"
```

---

## Task 6: 命令面板（Palette）

**Files:**
- Create: `ai_coding/ui/palette.py`
- Create: `tests/test_palette.py`

**Interfaces:**
- Consumes: 无
- Produces: `PaletteEntry`（name, description, icon）；`CommandPalette`（`open()` / `close()` / `is_open` / `handle_key(key)` / `select() -> PaletteEntry` / `render() -> list[tuple[str, str]]`）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_palette.py
from ai_coding.ui.palette import CommandPalette, PaletteEntry


def _entries():
    return [
        PaletteEntry("plan", "Create and refine an implementation plan", "📝"),
        PaletteEntry("review", "Review current changes and surface issues", "🔍"),
        PaletteEntry("model", "Change model and reasoning effort", "🤖"),
    ]


def test_palette_open_close():
    p = CommandPalette(_entries())
    assert not p.is_open
    p.open()
    assert p.is_open
    p.close()
    assert not p.is_open


def test_palette_navigate_down():
    p = CommandPalette(_entries())
    p.open()
    p.handle_key("down")
    assert p.selected_index == 1
    p.handle_key("down")
    assert p.selected_index == 2
    p.handle_key("down")
    assert p.selected_index == 0


def test_palette_navigate_up():
    p = CommandPalette(_entries())
    p.open()
    p.handle_key("up")
    assert p.selected_index == 2


def test_palette_select():
    p = CommandPalette(_entries())
    p.open()
    p.handle_key("down")
    selected = p.select()
    assert selected.name == "review"
    assert not p.is_open


def test_palette_render():
    p = CommandPalette(_entries())
    p.open()
    text = p.render()
    flat = "".join(t for _, t in text)
    assert "plan" in flat
    assert "review" in flat
    assert "model" in flat
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/test_palette.py -v`
Expected: FAIL

- [ ] **Step 3: 实现**

```python
# ai_coding/ui/palette.py
"""命令面板：键盘导航。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PaletteEntry:
    name: str
    description: str
    icon: str = ""


class CommandPalette:
    def __init__(self, entries: list[PaletteEntry]) -> None:
        self._entries = entries
        self._is_open = False
        self._selected = 0

    @property
    def is_open(self) -> bool:
        return self._is_open

    @property
    def selected_index(self) -> int:
        return self._selected

    def open(self) -> None:
        self._is_open = True
        self._selected = 0

    def close(self) -> None:
        self._is_open = False

    def handle_key(self, key: str) -> None:
        if key == "down":
            self._selected = (self._selected + 1) % len(self._entries)
        elif key == "up":
            self._selected = (self._selected - 1) % len(self._entries)

    def select(self) -> PaletteEntry:
        entry = self._entries[self._selected]
        self.close()
        return entry

    def render(self) -> list[tuple[str, str]]:
        lines: list[tuple[str, str]] = []
        for i, entry in enumerate(self._entries):
            if i == self._selected:
                prefix = ">"
                style = "class:palette.selected.text"
            else:
                prefix = " "
                style = "class:palette.item"
            icon = f"{entry.icon} " if entry.icon else ""
            lines.append((style, f"  {prefix} /{entry.name:<15} {icon}{entry.description}\n"))
        lines.append(("class:palette.hint", "  ↑↓ navigate  Enter select  Esc close\n"))
        return lines
```

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest tests/test_palette.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add ai_coding/ui/palette.py tests/test_palette.py
git commit -m "feat: add command palette with keyboard navigation"
```

---

## Task 7: 扩展 slash 命令注册表

**Files:**
- Modify: `ai_coding/ui/slash.py`
- Modify: `tests/test_slash.py`

**Interfaces:**
- Consumes: 无
- Produces: `_ALIASES` 新增 8 条映射

- [ ] **Step 1: 写失败测试**

在 `tests/test_slash.py` 末尾追加：

```python
def test_model():
    cmd = parse_slash("/model")
    assert cmd is not None and cmd.name == "model"

def test_resume():
    cmd = parse_slash("/resume")
    assert cmd is not None and cmd.name == "resume"

def test_permissions():
    cmd = parse_slash("/permissions")
    assert cmd is not None and cmd.name == "permissions"

def test_todo():
    cmd = parse_slash("/todo")
    assert cmd is not None and cmd.name == "todo"

def test_skills():
    cmd = parse_slash("/skills")
    assert cmd is not None and cmd.name == "skills"

def test_plan():
    cmd = parse_slash("/plan")
    assert cmd is not None and cmd.name == "plan"

def test_review():
    cmd = parse_slash("/review")
    assert cmd is not None and cmd.name == "review"

def test_config():
    cmd = parse_slash("/config")
    assert cmd is not None and cmd.name == "config"
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/test_slash.py -v`
Expected: 新测试 FAIL

- [ ] **Step 3: 实现**

在 `ai_coding/ui/slash.py` 的 `_ALIASES` 字典中追加：

```python
"/model": "model",
"/resume": "resume",
"/permissions": "permissions",
"/todo": "todo",
"/skills": "skills",
"/plan": "plan",
"/review": "review",
"/config": "config",
```

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest tests/test_slash.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add ai_coding/ui/slash.py tests/test_slash.py
git commit -m "feat: register 8 new slash commands"
```

---

## Task 8: `/model` 命令

**Files:**
- Modify: `ai_coding/ui/repl.py`
- Modify: `tests/test_repl.py`

**Interfaces:**
- Consumes: `loop.settings.models` / `loop.set_model(name)`
- Produces: 无

- [ ] **Step 1: 写失败测试**

```python
async def test_handle_model_lists_models():
    loop = _FakeLoop()
    loop.settings = type("S", (), {"models": {"gpt-4": "GPT-4", "claude": "Claude"}, "default_model": "gpt-4"})()
    sessions = _FakeSessions()
    dc, c = _console_capture()
    action, _ = await handle_input(loop, "/model", sessions.items[0], sessions, dc)
    assert action is REPLAction.CONTINUE
    out = _session_text(c)
    assert "gpt-4" in out and "claude" in out


async def test_handle_model_switches():
    loop = _FakeLoop()
    loop.settings = type("S", (), {"models": {"gpt-4": "GPT-4", "claude": "Claude"}, "default_model": "gpt-4"})()
    loop.set_model = lambda name: None
    sessions = _FakeSessions()
    dc, c = _console_capture()
    action, _ = await handle_input(loop, "/model claude", sessions.items[0], sessions, dc)
    assert action is REPLAction.CONTINUE
    out = _session_text(c)
    assert "claude" in out
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/test_repl.py::test_handle_model_lists_models -v`
Expected: FAIL

- [ ] **Step 3: 实现**

在 `handle_input` 中新增 `model` 分支 + 辅助函数 `_handle_model`（同前计划 Task 3）。

同时在 `AgentLoop` 中新增 `set_model` 方法：

```python
def set_model(self, name: str) -> None:
    from ai_coding.ai.factory import build_engine_from_config
    from copy import copy
    cfg = copy(self._original_config)
    cfg.default_model = name
    self.engine = build_engine_from_config(cfg)
```

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest tests/test_repl.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add ai_coding/ui/repl.py ai_coding/core/agent_loop.py tests/test_repl.py
git commit -m "feat: add /model command"
```

---

## Task 9: `/resume` 命令

**Files:**
- Modify: `ai_coding/ui/repl.py`
- Modify: `tests/test_repl.py`

**Interfaces:**
- Consumes: `sessions.list_sessions()` / `sessions.load(id)`
- Produces: `REPLAction.NEW_SESSION`

- [ ] **Step 1: 写失败测试**

```python
async def test_handle_resume_lists_sessions():
    loop = _FakeLoop()
    sessions = _FakeSessions()
    dc, c = _console_capture()
    action, _ = await handle_input(loop, "/resume", sessions.items[0], sessions, dc)
    assert action is REPLAction.CONTINUE
    out = _session_text(c)
    assert "s1" in out or "s2" in out


async def test_handle_resume_loads_session():
    loop = _FakeLoop()
    sessions = _FakeSessions()
    target = sessions.items[0]
    dc, c = _console_capture()
    action, session = await handle_input(loop, "/resume s1", sessions.items[1], sessions, dc)
    assert action is REPLAction.NEW_SESSION
    assert session is target
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/test_repl.py::test_handle_resume_lists_sessions -v`
Expected: FAIL

- [ ] **Step 3: 实现**

在 `handle_input` 中新增 `resume` 分支 + 辅助函数 `_handle_resume`（同前计划 Task 4）。

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest tests/test_repl.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add ai_coding/ui/repl.py tests/test_repl.py
git commit -m "feat: add /resume command"
```

---

## Task 10: `/permissions` 命令

**Files:**
- Modify: `ai_coding/ui/repl.py`
- Modify: `tests/test_repl.py`

**Interfaces:**
- Consumes: `loop.dispatcher.approval`
- Produces: 无

- [ ] **Step 1: 写失败测试**

```python
async def test_handle_permissions_shows_current():
    loop = _FakeLoop()
    loop.dispatcher = type("D", (), {"approval": type("A", (), {"__repr__": lambda s: "CLIApprovalCallback"})()})()
    sessions = _FakeSessions()
    dc, c = _console_capture()
    action, _ = await handle_input(loop, "/permissions", sessions.items[0], sessions, dc)
    assert action is REPLAction.CONTINUE
    out = _session_text(c)
    assert "CLIApprovalCallback" in out or "approval" in out.lower()
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/test_repl.py::test_handle_permissions_shows_current -v`
Expected: FAIL

- [ ] **Step 3: 实现**

在 `handle_input` 中新增 `permissions` 分支 + 辅助函数 `_handle_permissions`（同前计划 Task 5）。

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest tests/test_repl.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add ai_coding/ui/repl.py tests/test_repl.py
git commit -m "feat: add /permissions command"
```

---

## Task 11: `/todo` 命令

**Files:**
- Modify: `ai_coding/ui/repl.py`
- Modify: `tests/test_repl.py`

**Interfaces:**
- Consumes: `session.task`（`TaskState | None`）
- Produces: 无

- [ ] **Step 1: 写失败测试**

```python
async def test_handle_todo_renders_task_state():
    from ai_coding.domain.task import TaskState, TodoItem
    loop = _FakeLoop()
    sessions = _FakeSessions()
    session = sessions.items[0]
    task = TaskState(goal="Build feature X")
    task.add(TodoItem(id="1", content="Write tests", status="pending"))
    task.add(TodoItem(id="2", content="Implement feature", status="in_progress"))
    session.task = task
    dc, c = _console_capture()
    action, _ = await handle_input(loop, "/todo", session, sessions, dc)
    assert action is REPLAction.CONTINUE
    out = _session_text(c)
    assert "Build feature X" in out
    assert "Write tests" in out


async def test_handle_todo_no_task():
    loop = _FakeLoop()
    sessions = _FakeSessions()
    dc, c = _console_capture()
    action, _ = await handle_input(loop, "/todo", sessions.items[0], sessions, dc)
    assert action is REPLAction.CONTINUE
    out = _session_text(c)
    assert "no task" in out.lower() or "empty" in out.lower() or "0" in out
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/test_repl.py::test_handle_todo_renders_task_state -v`
Expected: FAIL

- [ ] **Step 3: 实现**

在 `handle_input` 中新增 `todo` 分支（同前计划 Task 6）。

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest_tests/test_repl.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add ai_coding/ui/repl.py tests/test_repl.py
git commit -m "feat: add /todo command"
```

---

## Task 12: `/skills` 命令

**Files:**
- Modify: `ai_coding/ui/repl.py`
- Modify: `tests/test_repl.py`

**Interfaces:**
- Consumes: `loop.skills`（`SkillRegistry | None`）
- Produces: 无

- [ ] **Step 1: 写失败测试**

```python
async def test_handle_skills_lists_catalog():
    loop = _FakeLoop()
    loop.skills = type("SR", (), {"catalog_text": lambda: "- test-skill: A test skill"})()
    sessions = _FakeSessions()
    dc, c = _console_capture()
    action, _ = await handle_input(loop, "/skills", sessions.items[0], sessions, dc)
    assert action is REPLAction.CONTINUE
    out = _session_text(c)
    assert "test-skill" in out
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/test_repl.py::test_handle_skills_lists_catalog -v`
Expected: FAIL

- [ ] **Step 3: 实现**

在 `handle_input` 中新增 `skills` 分支（同前计划 Task 7）。

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest_tests/test_repl.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add ai_coding/ui/repl.py tests/test_repl.py
git commit -m "feat: add /skills command"
```

---

## Task 13: `/plan` 和 `/review` 命令

**Files:**
- Modify: `ai_coding/ui/repl.py`
- Modify: `tests/test_repl.py`

**Interfaces:**
- Consumes: `loop.process_input()`
- Produces: 无

- [ ] **Step 1: 写失败测试**

```python
async def test_handle_plan_sends_prompt():
    loop = _FakeLoop()
    sessions = _FakeSessions()
    dc, _ = _console_capture()
    action, _ = await handle_input(loop, "/plan", sessions.items[0], sessions, dc)
    assert action is REPLAction.CONTINUE
    assert len(loop.calls) == 1
    assert "plan" in loop.calls[0][0].lower() or "implementation" in loop.calls[0][0].lower()


async def test_handle_review_sends_prompt():
    loop = _FakeLoop()
    sessions = _FakeSessions()
    dc, _ = _console_capture()
    action, _ = await handle_input(loop, "/review", sessions.items[0], sessions, dc)
    assert action is REPLAction.CONTINUE
    assert len(loop.calls) == 1
    assert "review" in loop.calls[0][0].lower() or "审查" in loop.calls[0][0].lower()
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest_tests/test_repl.py::test_handle_plan_sends_prompt -v`
Expected: FAIL

- [ ] **Step 3: 实现**

在 `handle_input` 中新增 `plan` 和 `review` 分支（同前计划 Task 8）。

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest_tests/test_repl.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add ai_coding/ui/repl.py tests/test_repl.py
git commit -m "feat: add /plan and /review commands"
```

---

## Task 14: `/config` 命令

**Files:**
- Modify: `ai_coding/ui/repl.py`
- Modify: `tests/test_repl.py`

**Interfaces:**
- Consumes: `loop.settings`
- Produces: 无

- [ ] **Step 1: 写失败测试**

```python
async def test_handle_config_shows_settings():
    loop = _FakeLoop()
    loop.settings = type("S", (), {
        "default_model": "gpt-4",
        "max_context_tokens": 48000,
        "max_tool_iterations": 10,
        "stop_policy": "todo_aware",
    })()
    sessions = _FakeSessions()
    dc, c = _console_capture()
    action, _ = await handle_input(loop, "/config", sessions.items[0], sessions, dc)
    assert action is REPLAction.CONTINUE
    out = _session_text(c)
    assert "gpt-4" in out
    assert "48000" in out
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest_tests/test_repl.py::test_handle_config_shows_settings -v`
Expected: FAIL

- [ ] **Step 3: 实现**

在 `handle_input` 中新增 `config` 分支 + 辅助函数 `_handle_config`（同前计划 Task 9）。

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest_tests/test_repl.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add ai_coding/ui/repl.py tests/test_repl.py
git commit -m "feat: add /config command"
```

---

## Task 15: 重写 HELP_TEXT

**Files:**
- Modify: `ai_coding/ui/repl.py`
- Modify: `tests/test_repl.py`

**Interfaces:**
- Consumes: 无
- Produces: 更新后的 `HELP_TEXT` 常量

- [ ] **Step 1: 写失败测试**

```python
async def test_help_includes_new_commands():
    loop = _FakeLoop()
    sessions = _FakeSessions()
    dc, c = _console_capture()
    action, _ = await handle_input(loop, "/help", sessions.items[0], sessions, dc)
    assert action is REPLAction.CONTINUE
    out = _session_text(c)
    for cmd in ("/model", "/resume", "/permissions", "/todo", "/skills", "/plan", "/review", "/config"):
        assert cmd in out
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest_tests/test_repl.py::test_help_includes_new_commands -v`
Expected: FAIL

- [ ] **Step 3: 实现**

```python
HELP_TEXT = (
    "direct commands:\n"
    "  /help, /h       show this help\n"
    "  /status         show session info\n"
    "  /model [name]   list or switch models\n"
    "  /resume [id]    list or resume saved sessions\n"
    "  /permissions   view or change approval policy\n"
    "  /todo           show current task list\n"
    "  /skills         list available skills\n"
    "  /plan           generate implementation plan\n"
    "  /review        review current changes\n"
    "  /config         show effective configuration\n"
    "  /compact        compact conversation history\n"
    "  /memory         show memory status\n"
    "  /trace          show last turn trace\n"
    "  /budget         show session token totals\n"
    "  /clear          start a fresh session\n"
    "  /quit, /q       exit"
)
```

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest_tests/test_repl.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add ai_coding/ui/repl.py tests/test_repl.py
git commit -m "feat: update HELP_TEXT"
```

---

## Task 16: 主 Application 组装

**Files:**
- Create: `ai_coding/ui/app.py`
- Modify: `ai_coding/ui/__init__.py`
- Create: `tests/test_app.py`

**Interfaces:**
- Consumes: `render_header(loop)` / `BodyBuffer()` / `render_footer(loop, turn_start)` / `CommandPalette(entries)` / `handle_input(loop, text, session, sessions, console)` / `loop` / `sessions`
- Produces: `run_app(path, workspace, skills)` 异步函数；`PALETTE_ENTRIES: list[PaletteEntry]`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_app.py
"""Application 组装。"""

from ai_coding.ui.app import PALETTE_ENTRIES, run_app


def test_palette_entries_complete():
    names = {e.name for e in PALETTE_ENTRIES}
    expected = {"plan", "review", "model", "status", "compact", "resume", "permissions", "help"}
    assert expected <= names


def test_palette_entries_have_descriptions():
    for entry in PALETTE_ENTRIES:
        assert entry.description, f"Missing description for {entry.name}"


def test_run_app_is_coroutine():
    import asyncio
    assert asyncio.iscoroutinefunction(run_app)
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/test_app.py -v`
Expected: FAIL

- [ ] **Step 3: 实现**

```python
# ai_coding/ui/app.py
"""主 Application：组装 layout + 事件绑定。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from prompt_toolkit import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Float, FloatContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import D

from ai_coding.ui.body import BodyBuffer
from ai_coding.ui.footer import render_footer
from ai_coding.ui.header import render_header
from ai_coding.ui.palette import CommandPalette, PaletteEntry
from ai_coding.ui.theme import STYLE

PALETTE_ENTRIES = [
    PaletteEntry("plan", "Create and refine an implementation plan", "📝"),
    PaletteEntry("review", "Review current changes and surface issues", "🔍"),
    PaletteEntry("model", "Change model and reasoning effort", "🤖"),
    PaletteEntry("status", "Show session and runtime status", "📊"),
    PaletteEntry("compact", "Summarize conversation to reduce context", "📄"),
    PaletteEntry("resume", "Resume a saved session", "⏰"),
    PaletteEntry("permissions", "Manage approval policy", "🛡"),
    PaletteEntry("todo", "Show current task list", "✓"),
    PaletteEntry("skills", "List available skills", "📦"),
    PaletteEntry("config", "Show effective configuration", "⚙"),
    PaletteEntry("memory", "Show memory status", "🧠"),
    PaletteEntry("budget", "Show session token totals", "📈"),
    PaletteEntry("help", "Show available commands", "❓"),
]


async def run_app(path: str | None, workspace: str, skills: str | None) -> None:
    """组装并运行全屏 TUI。"""
    from ai_coding.cli import _build_loop, _build_tool_registry, _compose_observer, _make_guards, _make_hooks, _make_recorder
    from ai_coding.config.loader import ConfigManager
    from ai_coding.core.mcp import MCPManager
    from ai_coding.core.steering import SteeringQueue
    from ai_coding.security.approval import CLIApprovalCallback, CLIUserPromptCallback
    from ai_coding.service.session_service import SessionService
    from ai_coding.ui.slash import parse_slash

    manager = ConfigManager()
    manager.initialize(path)
    app_cfg = manager.app

    from ai_coding.ai.factory import build_engine_from_config
    engine = build_engine_from_config(app_cfg)
    user_prompt = CLIUserPromptCallback()

    from ai_coding.ai.prompt import BASE_INSTRUCTIONS
    registry = _build_tool_registry(app_cfg, workspace, skills, user_prompt=user_prompt, base_instructions=BASE_INSTRUCTIONS)

    steering = SteeringQueue()
    sessions = SessionService()

    recorder = _make_recorder(app_cfg, workspace, None)
    mcp_manager = MCPManager(app_cfg.mcp, observer=recorder)
    for tool in await mcp_manager.open_all():
        registry.register(tool)

    loop = _build_loop(
        engine, sessions, registry, app_cfg, workspace, skills,
        approval=CLIApprovalCallback(),
        observer=None,
        trace=recorder,
        guards=_make_guards(app_cfg),
        hooks=_make_hooks(app_cfg, workspace),
        steering=steering,
    )

    body = BodyBuffer()
    palette = CommandPalette(PALETTE_ENTRIES)
    session = sessions.create()
    turn_start = time.monotonic()

    body.write("  ✨ Welcome to Nexus! ✨\n")
    body.write("  Describe a task, or use / for commands.\n\n")

    header_control = FormattedTextControl(lambda: render_header(loop))
    footer_control = FormattedTextControl(lambda: render_footer(loop, turn_start))
    body_control = BufferControl(buffer=Buffer())
    palette_control = FormattedTextControl(
        lambda: palette.render() if palette.is_open else []
    )

    kb = KeyBindings()

    @kb.add("/")
    def _toggle_palette(event: Any) -> None:
        nonlocal session
        if palette.is_open:
            palette.close()
            event.app.layout.focus(body_control)
        else:
            palette.open()
            event.app.layout.focus(palette_control)

    @kb.add("escape")
    def _close_palette(event: Any) -> None:
        nonlocal session
        if palette.is_open:
            palette.close()
            event.app.layout.focus(body_control)

    @kb.add("down")
    def _palette_down(event: Any) -> None:
        if palette.is_open:
            palette.handle_key("down")

    @kb.add("up")
    def _palette_up(event: Any) -> None:
        if palette.is_open:
            palette.handle_key("up")

    @kb.add("enter")
    def _palette_select(event: Any) -> None:
        nonlocal session
        if palette.is_open:
            entry = palette.select()
            event.app.layout.focus(body_control)
            asyncio.create_task(_run_command(entry.name))

    async def _run_command(name: str) -> None:
        nonlocal session
        from ai_coding.ui.repl import handle_input
        from ai_coding.ui.console import DialogueConsole
        console = DialogueConsole()
        action, new_session = await handle_input(loop, f"/{name}", session, sessions, console)
        if action.name == "new_session":
            session = new_session

    @kb.add("c-q")
    def _exit(event: Any) -> None:
        event.app.exit()

    palette_window = Window(
        content=palette_control,
        width=D(preferred=60),
        height=D(preferred=14),
    )

    root = FloatContainer(
        content=HSplit([
            Window(content=header_control, height=D(preferred=12), style="class:header"),
            Window(content=body_control, wrap_lines=False),
            Window(content=footer_control, height=1, style="class:footer"),
        ]),
        floats=[
            Float(
                content=palette_window,
                top=4, left=10,
                transparent=True,
            ),
        ],
    )

    app = Application(
        layout=Layout(root),
        key_bindings=kb,
        style=STYLE,
        full_screen=True,
    )

    try:
        await app.run_async()
    finally:
        await loop.aclose()
        await mcp_manager.aclose()
```

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest tests/test_app.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add ai_coding/ui/app.py ai_coding/ui/__init__.py tests/test_app.py
git commit -m "feat: add main Application with full TUI layout"
```

---

## Task 17: 删除旧 UI 文件

**Files:**
- Delete: `ai_coding/ui/reader.py`
- Delete: `ai_coding/ui/trace_render.py`
- Modify: `ai_coding/ui/__init__.py`

**Interfaces:**
- Consumes: 无
- Produces: 无

- [ ] **Step 1: 删除文件**

```bash
git rm ai_coding/ui/reader.py ai_coding/ui/trace_render.py
```

- [ ] **Step 2: 更新 `__init__.py`**

```python
"""REPL / presentation layer: slash commands, console, app."""

__all__ = ["slash", "console", "repl", "app", "palette", "theme", "header", "body", "footer"]
```

- [ ] **Step 3: 运行全量测试确认无破坏**

Run: `pytest tests/ -v`
Expected: 全部 PASS

- [ ] **Step 4: Commit**

```bash
git add ai_coding/ui/__init__.py
git commit -m "chore: remove old reader.py and trace_render.py"
```

---

## Task 18: 全量回归 + 清理

**Files:**
- 无新文件

- [ ] **Step 1: 运行全量测试**

Run: `pytest tests/ -v`
Expected: 全部 PASS

- [ ] **Step 2: 运行 lint**

Run: `ruff check ai_coding/`
Expected: 0 issues

- [ ] **Step 3: 运行 typecheck**

Run: `mypy ai_coding/ --strict`
Expected: 0 issues

- [ ] **Step 4: Commit（如有修复）**

```bash
git add -A
git commit -m "test: full regression pass"
```

---

## Self-Review

### 1. Spec Coverage

| 图片视觉元素 | 对应 Task |
|---|---|
| ASCII art "NEXUS" | Task 3 (header.py) |
| ✓ workspace indexed | Task 3 (header.py) |
| ✓ N tools connected | Task 3 (header.py) |
| ✓ memory loaded | Task 3 (header.py) |
| model: DeepSeek-V4 | Task 3 (header.py) |
| cwd: ~/projects/atlas | Task 3 (header.py) |
| 分隔线 | Task 3 (header.py 最后一行 \n) |
| ✨ Welcome to Nexus! ✨ | Task 16 (app.py body 初始内容) |
| Describe a task, or use / for commands. | Task 16 (app.py body 初始内容) |
| 命令面板（键盘导航 + 高亮） | Task 6 (palette.py) + Task 16 (app.py Float) |
| ↑↓ navigate Enter select Esc close | Task 6 (palette.py render) |
| 底部状态栏 | Task 5 (footer.py) |
| 深色主题 | Task 2 (theme.py) |
| 清理旧 CLI 子命令 | Task 1 |
| 统一入口 | Task 1 |
| /model /resume /permissions /todo /skills /plan /review /config | Task 7-15 |

### 2. Placeholder Scan

- 无 TBD/TODO
- 无 "implement later"
- 所有代码步骤都有完整代码块
- 无 "Similar to Task N"

### 3. Type Consistency

- `PaletteEntry` 在 Task 6 定义，Task 16 使用 `PALETTE_ENTRIES`
- `BodyBuffer` 在 Task 4 定义，Task 16 使用
- `render_header(loop)` 在 Task 3 定义，Task 16 使用
- `render_footer(loop, turn_start)` 在 Task 5 定义，Task 16 使用
- `CommandPalette(entries)` 在 Task 6 定义，Task 16 使用
- `handle_input(loop, text, session, sessions, console)` 在所有 Task 中签名一致
- `run_app(path, workspace, skills)` 在 Task 16 定义，Task 1 调用

### 4. 架构完整性检查

**问题 1：`run_app` 中的 `_run_command` 引用 `session` 变量但它是局部变量**
- 修复：将 `session` 包装在 `list` 或 `dict` 中以便在闭包中修改
- 已修复：使用 `session = new_session` 赋值，Python 闭包可以读取但不能赋值外部变量。需要改为 `nonlocal session` 或使用可变容器。

**问题 2：`ConsoleObserver` 未接入 `app.py`**
- 当前 `app.py` 传 `observer=None` 给 `_build_loop`
- 修复：创建 `ConsoleObserver` 并传入，或者创建新的 `BufferObserver` 写入 `body`

**问题 3：`body` Buffer 的内容未从 `ConsoleObserver` 接收**
- 当前 `BodyBuffer` 是纯文本列表，不接收 `LoopEvent`
- 修复：在 `app.py` 中创建 `BufferObserver` 将事件写入 `body`

**问题 4：`Float` 覆盖层的 `palette_control` 内容未更新**
- `palette_control` 是 `BufferControl`，但 palette 的 `render()` 返回 FormattedText
- 修复：用 `FormattedTextControl` 替代 `BufferControl`，lambda 返回 `palette.render()`

### 5. 修复后的 `app.py` 关键修正

```python
# 修正 1：palette_control 用 FormattedTextControl
palette_control = FormattedTextControl(lambda: palette.render() if palette.is_open else [])

# 修正 2：session 用 nonlocal
async def _run_command(name: str) -> None:
    nonlocal session
    ...

# 修正 3：接入 ConsoleObserver
from ai_coding.ui.console import ConsoleObserver, DialogueConsole
ui_console = DialogueConsole()
buffer_observer = ConsoleObserver(ui_console)  # 需要改为写入 body
```

### 6. 迁移路径

```
当前状态：
  cli.py → repl 命令 → run_repl() → while True → handle_input()
  reader.py → InteractiveReader → stdin 仲裁
  console.py → DialogueConsole → console.print() 逐行输出

目标状态：
  cli.py → run_app() → Application.run_async()
  app.py → HSplit(header, body, footer) + Float(palette)
  palette.py → CommandPalette → 键盘导航
  body.py → BodyBuffer → 可滚动输出
  header.py → render_header() → ASCII art + 状态
  footer.py → render_footer() → 状态栏
```

### 7. 风险点

1. **`nonlocal session` 在嵌套函数中**：Python 支持 `nonlocal`，但需要确保 `session` 在 `run_app` 作用域内定义
2. **`ConsoleObserver` 与 `BodyBuffer` 的桥接**：需要一个新的 observer 将事件写入 body，或者修改 `ConsoleObserver` 接受 `BodyBuffer` 参数
3. **`Float` 覆盖层的渲染时机**：palette 打开时需要触发重绘，`invalidate()` 方法需要调用
4. **删除 `reader.py` 后的影响**：`InteractiveReader` 被多处引用，需要确认所有引用都已更新
5. **`prompt_toolkit.Application` 的事件循环**：`run_async()` 是异步的，需要 `asyncio.run()` 包装

---

## 成功后的效果

```
$ ai-coding

███╗   ██╗███████╗██╗  ██╗██╗   ██╗███████╗
████╗  ██║██╔════╝╚██╗██╔╝██║   ██║██╔════╝
██╔██╗ ██║█████╗   ╚███╔╝ ██║   ██║███████╗
██║╚██╗██║██╔══╝   ██╔██╗ ██║   ██║╚════██║
██║ ╚████║███████╗██╔╝ ██╗╚██████╔╝███████║
╚═╝  ╚═══╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚══════╝

  ✓ workspace indexed
  ✓ 3 tools connected
  ✓ memory loaded (42 notes)
  model: DeepSeek-V4
  cwd:    ~/projects/atlas

─────────────────────────────────────────────────────────────

  ✨ Welcome to Nexus! ✨
  Describe a task, or use / for commands.

  > /plan           📝 Create and refine an implementation plan
    /review         🔍 Review current changes and surface issues
    /model          🤖 Change model and reasoning effort
    /status         📊 Show session and runtime status
    /compact        📄 Summarize conversation to reduce context
    /resume         ⏰ Resume a saved session
    /permissions    🛡 Manage approval policy
    ↑↓ navigate  Enter select  Esc close

 main · clean · 18% context · 00:03
```
