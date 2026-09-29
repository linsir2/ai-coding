# 命令面板（P1）实现分析

- 日期：2026-09-29
- 状态：待评审（未动代码）
- 触发：Task 8 真实终端验收中，用户输入 `/` 无命令列表——这是 spec §9 已记录的 v1 已知退化，现在补上
- 参考：`docs/superpowers/specs/2026-09-28-textual-ui-design.md` §9（非目标清单）

## 1. 要实现的内容

参考图 2 的命令面板：输入 `/` 后弹出**居中浮层**，背景变暗；顶部搜索框；命令列表（图标 + 名称 + 描述）；选中行高亮；底部键位提示；`↑↓` 导航、`Enter` 执行、`Esc` 关闭。

## 2. 对代码的影响 / 执行范围

| 文件 | 动作 | 内容 |
|---|---|---|
| `ai_coding/ui/commands.py` | **新增** | `SlashCommandProvider(Provider)` + 16 条命令表 |
| `ai_coding/ui/textual_app.py` | 改 | `COMMANDS` 类变量、`on_input_changed` 触发、`on_command_palette_closed` 清理 |
| `ai_coding/ui/app.tcss` | 改 | 面板样式（居中、变暗、高亮、圆角） |
| `tests/test_commands.py` | **新增** | TDD 用例（见 §7） |

**不改动**：`repl.py`（`handle_input` 签名不动）、`console.py`、`modals.py`、`transcript.py`、`blocks.py`、`cli.py`、旧 UI（已删）。

## 3. 原有设计（已被 Task 7 删除的旧 palette）

旧 `ui/palette.py`（prompt_toolkit）：
- 固定 72 宽盒子，在 `HSplit` 里**推挤正文**（不是浮层）
- `❯` 选择标记、`↑↓` 导航、`Enter` 选择、`Esc` 关闭
- **无搜索框**、无背景变暗、无图标
- 触发：`/` 键绑定

旧设计的三个问题（spec §1 已记录）：固定宽度破版、推挤而非浮层、无搜索。

## 4. 目前设计（v1 现状）

- **无面板**（已知退化）：`/` 开头的文本直接进 `run_command` → `handle_input`
- 16 个 slash 命令在 `repl.py`：`quit, clear, help, status, compact, memory, trace, budget, model, resume, permissions, todo, skills, plan, review, config`
- 每条命令有 `console.info(...)` 输出（进 transcript）

## 5. 关键决策（含理由与代价）

| # | 决策 | 选项 | 理由 | 代价 |
|---|---|---|---|---|
| D1 | 面板实现 | **A. Textual 内置 `CommandPalette`** / B. 自写浮层 | A 就是参考图的行为（变暗浮层 + 搜索 + 键盘导航 + 底部提示），零重造；B 要重写搜索/高亮/键盘/变暗 | A 的外观受 Textual 默认样式约束（可用 CSS 调）；B 自由但工作量大 |
| D2 | 命令集 | **A. 全部 16 条** / B. 精选 6 条 | 参考图的 6 条里有 `/code`、`/debug`、`/search`——**本应用没有这些命令**，照抄会列出不存在的命令；16 条是应用真实支持的全集 | 列表较长（16 行），但可滚动/过滤 |
| D3 | 触发方式 | **A. `on_input_changed` 且 `value == "/"`** / B. `/` 键绑定 | A 精确（只在输入恰好是 `/` 时触发，`/plan` 不会误触）；B 会在输入中间打 `/` 时也触发 | A 需要处理"输入框残留 `/`"（见 D5） |
| D4 | 搜索框预填 | **A. 不预填** / B. 预填 `/` | 参考图搜索框显示 `/plan`；不预填时用户输入 `plan` 也能模糊匹配到 `/plan`。预填需要操作 `CommandInput` 的值（时序麻烦） | 搜索框初始为空（参考图是 `/plan`，差一个 `/` 的前缀观感） |
| D5 | 输入框残留 | **A. 关闭时清空** / B. 保留 | `/` 是触发器不是内容；清空更干净 | 用户 Esc 后需重新输入（可接受） |
| D6 | 系统命令 | **A. 保留 `SystemCommandsProvider`** / B. 只留 slash | 系统命令（quit、toggle dark 等）有用；`App.COMMANDS` 是覆盖语义，必须显式加回 | 面板里会多出几条系统命令 |

**推荐组合：D1-A + D2-A + D3-A + D4-A + D5-A + D6-A。**

## 6. 每个文件怎么实现（逐步）

### 6.1 `ai_coding/ui/commands.py`（新增）

**API 约束（已核实）**：`CommandPalette` 用 `provider = [provider_class(screen, match_style) for ...]` 实例化 provider（`command.py:828-830`），所以 **不能覆写 `__init__` 加参数**——直接继承 `Provider(screen, match_style)`，命令表走模块级常量。回调类型 `IgnoreReturnCallbackType = Union[Callable[[], Awaitable[Any]], Callable[[], Any]]`（`_types.py:32`），无参、返回被忽略。

```python
"""Slash-command provider for the Textual command palette."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from textual.command import Hit, Provider

if TYPE_CHECKING:
    from ai_coding.ui.textual_app import AICodingApp

#: (name, help) — name without the leading "/", matching repl.py's cmd.name.
SLASH_COMMANDS = [
    ("plan", "Create and refine an implementation plan"),
    ("review", "Review current changes and surface issues"),
    ("status", "Show session and runtime status"),
    ("model", "List or switch models"),
    ("resume", "List or resume saved sessions"),
    ("permissions", "View or change approval policy"),
    ("todo", "Show the current task list"),
    ("skills", "List available skills"),
    ("config", "Show effective configuration"),
    ("compact", "Compact conversation history"),
    ("memory", "Show memory status"),
    ("trace", "Show the last turn's trace"),
    ("budget", "Show session token totals"),
    ("clear", "Start a fresh session"),
    ("help", "Show available commands"),
    ("quit", "Exit the app"),
]


class SlashCommandProvider(Provider):
    """Fuzzy-searches the app's slash commands. No __init__: the palette
    instantiates providers as ``provider_class(screen, match_style)``."""

    async def search(self, query: str):
        matcher = self.matcher(query)
        for name, help_text in SLASH_COMMANDS:
            if (score := matcher.match(name)) > 0:
                yield Hit(score, matcher.highlight(name), self._run(name), help=help_text)

    def _run(self, name: str):
        app = cast("AICodingApp", self.app)

        def callback() -> None:
            app.run_command(f"/{name}")

        return callback
```

`cast` + `TYPE_CHECKING` 导入避免循环依赖（`textual_app` 会 import `commands`），同时满足 mypy strict（`Provider.app` 是 `App[object]`，没有 `run_command`）。

### 6.2 `ai_coding/ui/textual_app.py`（改）

```python
from textual.command import CommandPalette  # 仅用于类型/消息

class AICodingApp(App[None]):
    COMMANDS = {SlashCommandProvider, get_system_commands_provider}  # D6-A

    def on_input_changed(self, event: Input.Changed) -> None:
        # D3-A: 输入恰好是 "/" 时打开面板（"/plan" 不会误触）
        if event.value == "/" and not CommandPalette.is_open(self):
            self.action_command_palette()

    def on_command_palette_closed(self, event: CommandPalette.Closed) -> None:
        # D5-A: "/" 是触发器，关闭后清空输入框
        if self._input.value == "/":
            self._input.value = ""
```

### 6.3 `ai_coding/ui/app.tcss`（改）

```css
CommandPalette {
    align: center middle;
}

CommandPalette > Vertical {
    width: 70;
    height: auto;
    max-height: 20;
    border: round $primary;
    background: $surface;
}

CommandPalette #--input {
    border: none;
    height: 1;
}

CommandPalette OptionList {
    height: auto;
    max-height: 16;
}
```

## 7. 数据流

```
用户输入 "/"
  → Input.Changed(value="/")
  → on_input_changed → value == "/" 且面板未开
  → app.action_command_palette()
  → push_screen(CommandPalette)  [SystemModalScreen：背景变暗 + 居中浮层]
  → AUTO_FOCUS 聚焦搜索框

用户输入 "plan"
  → CommandPalette 内部 → SlashCommandProvider.search("plan")
  → 模糊匹配 16 条 → Hit("/plan", score, callback, help="Create and refine...")
  → 列表显示，匹配部分高亮

用户 ↑/↓
  → CommandPalette BINDINGS → 高亮移动

用户 Enter
  → run_on_select=True → callback()
  → app.run_command("/plan")  [@work worker]
  → handle_input → 输出进 transcript
  → 面板自动关闭

用户 Esc
  → CommandPalette BINDINGS → 关闭
  → Closed(option_selected=False) → on_command_palette_closed → 清空输入框
```

**与现有机制的关系**：
- 面板是 `SystemModalScreen`，**不遮挡输入框**（浮层在中间，输入框在底部）——满足 spec §4 #14 的"不遮输入框"
- 面板打开时输入框**不禁用**（与 turn guard 不同）；但面板的搜索框持有焦点，输入框的 `on_input_changed` 不会触发（按键去了搜索框）
- 面板关闭后 `run_command` 的 worker 与 `run_turn` 同组（`exclusive=True, group="turn"`），不会并发

## 8. TDD 用例（`tests/test_commands.py`）

| # | 用例 | 断言 |
|---|---|---|
| 1 | 输入 `/` 打开面板 | `CommandPalette.is_open(app)` 为 True |
| 2 | 搜索过滤 | `provider.search("plan")` 返回含 `/plan` 的 Hit |
| 3 | Enter 执行命令 | monkeypatch `handle_input`，选中 `/status` → 断言被调用 |
| 4 | Esc 关闭 | 面板关闭，`is_open` 为 False |
| 5 | 关闭后输入框清空 | `app.query_one("#prompt").value == ""` |
| 6 | 直接输入 `/plan` 仍工作（回归） | 不进面板，直接 `run_command` |
| 7 | `/plan` 不误触面板 | 输入 `/plan` 时 `is_open` 为 False |

用例 1/3/4/5 用 `run_test()` + `pilot`；用例 2 纯函数；用例 6/7 回归 Task 6。

## 9. 成功后的效果

- 输入 `/` → 居中浮层弹出，背景变暗，搜索框聚焦
- 输入 `plan` → 列表过滤出 `/plan` 等匹配项，匹配字符高亮
- `↑↓` 移动高亮，`Enter` 执行（输出进 transcript），`Esc` 关闭
- 底部提示 `↑↓ navigate  Tab select  Esc close`（Textual 内置）
- 与参考图 2 结构一致（搜索框 + 列表 + 高亮 + 底部提示 + 变暗背景）

## 10. 架构图

```
┌────────────────────────────────────────────────┐
│ AICodingApp                                    │
│                                                 │
│  COMMANDS = {SlashCommandProvider,              │
│              SystemCommandsProvider}            │
│                                                 │
│  ┌─ StatusBar (1 row) ──────────────────────┐   │
│  ├─ Transcript (VerticalScroll) ────────────┤   │
│  │  ◯ You 11:39                            │   │
│  │  ┌─ UserBubble ─────────────────────┐   │   │
│  │  │ /plan                            │   │   │
│  │  └──────────────────────────────────┘   │   │
│  │  ✦ Nexus 11:39                          │   │
│  │  ┌─ Markdown (streaming) ────────────┐ │   │
│  │  │ ## 计划 ...                       │ │   │
│  │  └──────────────────────────────────┘ │   │
│  │  ▸ bash  {"command": "ls"}           │   │
│  │  ✓ bash                               │   │
│  ├─ InputBar ────────────────────────────┤   │
│  │ ┌─────────────────────────┐ ┌───┐     │   │
│  │ │ Type a message...       │ │ ➤ │     │   │
│  │ └─────────────────────────┘ └───┘     │   │
│  │                        / for commands  │   │
│  └─────────────────────────────────────────┘   │
│                                                 │
│  on_input_changed: value == "/" ────────────┐   │
│      → action_command_palette()             │   │
│                                             ▼   │
│  ┌─ CommandPalette (SystemModalScreen) ──────┐   │
│  │  背景变暗（$background 60%）              │   │
│  │  ┌─ CommandInput ─────────────────────┐  │   │
│  │  │ 🔍 plan|                           │  │   │
│  │  └────────────────────────────────────┘  │   │
│  │  ┌─ OptionList ────────────────────────┐ │   │
│  │  │ /plan   Create and refine...   ◀── │ │   │
│  │  │ /review Review current changes     │ │   │
│  │  │ /status Show session and runtime  │ │   │
│  │  │ ...                                │ │   │
│  │  └────────────────────────────────────┘ │   │
│  │  ↑↓ navigate  Tab select  Esc close     │   │
│  └──────────────────────────────────────────┘   │
│                                                 │
│  Enter → run_on_select → callback              │
│        → app.run_command("/plan")  [@work]      │
│        → handle_input → transcript              │
└────────────────────────────────────────────────┘
```

## 11. 自检（方案是否有问题）

| 风险 | 分析 | 结论 |
|---|---|---|
| `value == "/"` 误触发 | 输入 `/plan` 时 value 是 `/plan` ≠ `/`；输入中间打 `/`（如路径）时 value 不是恰好 `/` | 安全 |
| 面板打开时输入框残留 `/` | D5-A 关闭时清空 | 已处理 |
| `COMMANDS` 覆盖默认 | D6-A 显式加回 `SystemCommandsProvider` | 已处理 |
| 回调签名 | `IgnoreReturnCallbackType` 接受无参 callable；`lambda: app.run_command(...)` 返回 Worker（被忽略） | 安全 |
| provider 实例化 | `CommandPalette` 以 `provider_class(screen, match_style)` 实例化（`command.py:828`）→ **不能覆写 `__init__` 加参数**；命令表走模块级常量，`cast("AICodingApp", self.app)` 取 `run_command`（避免循环 import + 满足 mypy） | 已处理 |
| 与 turn guard 交互 | 面板是独立 screen，输入框不禁用；但面板搜索框持有焦点，`on_input_changed` 不触发 | 安全 |
| `run_command` 在面板回调里调用 | `@work` 装饰的方法调用即调度 worker，非阻塞 | 安全 |
| 面板关闭后 worker 才跑 | 面板先 dismiss，再 push worker——顺序无冲突 | 安全 |
| mypy strict | `on_input_changed(event: Input.Changed)`、`on_command_palette_closed(event: CommandPalette.Closed)` 均有类型；`Hit` 的 callback 是 `IgnoreReturnCallbackType` | 需验证（TDD 会暴露） |
| 参考图搜索框显示 `/plan` | D4-A 不预填，搜索框初始为空；用户输入 `plan` 即可匹配 | 可接受（差一个 `/` 前缀观感） |

## 12. 最终设想（验证中途细节）

实施后，用户在输入框输入 `/`：
1. 输入框的 `/` 还在，但屏幕中央弹出浮层，背景变暗
2. 搜索框自动聚焦，光标闪烁
3. 用户输入 `plan`，列表实时过滤，`/plan` 高亮，匹配字符加粗下划线
4. 用户按 `Enter`，浮层消失，输入框清空，`/plan` 作为 slash 命令执行（输出进 transcript）
5. 用户按 `Esc`，浮层消失，输入框清空，回到正常输入

若第 3 步列表为空或第 4 步命令没执行 → 说明 provider 或回调接线有错（TDD 用例 2/3 会抓住）。
若第 1 步浮层没弹出 → 说明触发条件或 `action_command_palette` 有错（用例 1 会抓住）。
若输入 `/plan` 也弹面板 → 说明 `value == "/"` 判断有错（用例 7 会抓住）。

## 13. 与现有设计文档的关系

- spec §9 非目标清单：本任务把"命令面板 → P1"这一条**移除**（已实现）
- spec §3.1 模块表：新增 `ui/commands.py`
- spec §4 集成核对表 #6（slash 命令）：补充"面板触发"路径
- plan 的 File Structure：新增 `commands.py` + `test_commands.py`
