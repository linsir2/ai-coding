# Textual UI 重写设计（v1：会话闭环）

- 日期：2026-09-28
- 状态：待评审
- 决策来源：brainstorming 对话（渲染栈选 Textual；v1 范围选"会话闭环优先"；弃用原则：与现有冲突或已无用的一律弃用）

## 1. 背景

现有 TUI 是 prompt_toolkit + 纯文本 Buffer 行模型，与目标视觉（四张参考图）存在**结构性**差距，不是组件缺口：

1. 一行一 style（`app.py:142-145` 定义 `_md_prefix`，`app.py:152` 作为 `get_line_prefix` 传入）→ 无法做同行多色（头像/名字/时间戳）、卡片边框+徽章、行内高亮。
2. 纯文本行模型，没有"块"的概念 → 无消息边界、无卡片、无背景块。
3. Markdown 只 strip 不渲染（`body.py:18-25`）→ 代码块/表格/引用丢失。
4. 事件渲染是机器视角（`observer.py:47-54`：`[tool: bash] {json}`）→ 缺"事件→人类可读块"映射。
5. 无宽度感知（`palette.py:10` 写死 72；`header.py:11-17` 写死 40）→ 窄终端破版、不能居中、resize 无处理。
6. UI 测试是源码字符串断言（`test_app.py:49-125`）→ 没有行为级验证手段，这是反复返工的机制性原因。

目标：以 Textual 重建 UI 层，v1 交付图 3/4 的会话闭环。

## 2. 已验证的技术事实

### 2.1 Textual 8.2.8 能力（源码级验证，`pip download` 解包，未安装未改动环境）

| 需求 | API | 位置 |
|---|---|---|
| 复制到系统剪贴板 | `App.copy_to_clipboard` → OSC52 (`\x1b]52`)，且存 `self._clipboard` | `app.py:1770,1786` |
| 复制的键位已内置 | `Screen` 绑定 `ctrl+c,super+c` → `action_copy_text`（无选中时 `SkipAction` 落回退出） | `screen.py:272,985,991` |
| 鼠标拖选 | 全局选择，`Widget.ALLOW_SELECT=True` 默认，跨 widget | `widget.py:328` |
| 命令面板（P1） | 内置 `CommandPalette(SystemModalScreen)` 带搜索 | `command.py:532` |
| 流式 markdown | `Markdown.get_stream()` → `MarkdownStream.write()`（async，含积压合并） | `widgets/_markdown.py:41,78,1155` |
| 流式 markdown（更简路径） | `Markdown.append()` 是**同步可调**的：返回 `AwaitComplete`，内部 `gather()` 立即启动，可 await 也可不 await | `_markdown.py:1445`；`await_complete.py:29-31` |
| 增量解析（性能关键） | `append` 只重解析 `_last_parsed_line` 之后的尾部，不是整篇 | `_markdown.py:1455-1462` |
| 长会话容器 | `RichLog`：`max_lines` 自动裁剪 + `auto_scroll` + `write(renderable)` | `_rich_log.py:61,66,175,264` |
| 主题 | `textual-light` / `textual-dark` 内置 | `theme.py:71,81` |
| 模态输入 | `push_screen_wait`（须在 worker 中调用；内部用 `asyncio.shield`） | `app.py:2961-2965,2981,2996-2998` |
| 行为级 UI 测试 | `App.run_test()` + `Pilot` | `app.py:2133` |
| 截图 | `App.save_screenshot()` / `export_screenshot()` (SVG) | `app.py:1855,1889` |

依赖代价：`textual==8.2.8` + `linkify-it-py` + `mdit-py-plugins` + `platformdirs`（`markdown-it-py` 已在）。

### 2.2 本仓关键约束（决定实现形态）

| 约束 | 证据 | 后果 |
|---|---|---|
| `DeltaSink = Callable[[str], None]` 是同步的 | `agent_loop.py:114`；`openai_engine.py:190` 直接调用 | 不能把 `MarkdownStream.write`（async）直接当 sink；UI 侧需"同步攒 + 定时 flush"适配器 |
| `MarkdownStream.write` 是 async；`Markdown.append` 同步可调（返回 `AwaitComplete`） | `_markdown.py:78,1445` | 定时 flush 用 `append` 即可，无需调度协程；`MarkdownStream` 可选 |
| 审批/ask_user provider 是 async | `approval.py:54`；`user_prompt.py:75` | 用 `push_screen_wait`，前提是循环跑在 **async worker** 里 |
| worker 上下文靠 ContextVar 传递 | `worker.py:37,75`（`active_worker`）；`app.py:2961-2965` 非 worker 时抛 `NoActiveWorker`；`dispatch.py:199` 的 `asyncio.gather` 会复制上下文 | 循环必须跑在 `@work`（**非 `thread=True`**）；工具经 `gather` 派生仍继承 worker，故工具内可弹模态 |
| `cli.py` 自建事件循环后调 `run_app` | `cli.py:57-61` | Textual 用 `App.run_async()` 复用该循环，`run_app` 签名不变 |
| `handle_input` 的 console 接口 = `delta/info/error/rule` | `console.py:42-59`；`repl.py` 全文 | 新 `TextualConsole` 继承 `DialogueConsole` 即可让 `repl.py` 零改动 |
| `process_input` 在取消时会持久化部分回合 | `agent_loop.py:365-376`（`CancelledError` → `_close_turn` → 重新抛出） | 退出/中断中途是安全的：取消 worker 后 await 即可 |

## 3. 架构

### 3.1 模块

| 新文件 | 内容 | 替代 |
|---|---|---|
| `ui/textual_app.py` | `AICodingApp(App)`：组装、循环接线、`run_app()` 保持签名 | `app.py` |
| `ui/blocks.py` | 纯数据模型 + `blocks_from_event(event) -> Block \| None`（**无 Textual 依赖，可离线单测**） | `observer.py` 的映射部分 |
| `ui/transcript.py` | `Transcript(VerticalScroll)`：消息块容器、当前流式块、吸底策略 | `body.py` + `scroll.py` |
| `ui/widgets.py` | `MessageHeader` / `UserBubble` / `AssistantBlock` / `ToolBlock` / `CardBlock` | `header.py` |
| `ui/input_bar.py` | 圆角边框 + 占位符 + `➤` + `/ for commands` 提示行 | `app.py` 输入区 |
| `ui/status_bar.py` | 1 行：`>_ {workspace}` + `● Ready` + 模型名 | `footer.py` |
| `ui/modals.py` | `ApprovalScreen` / `AskScreen`（`ModalScreen`） | `_input_provider` hack |
| `ui/console.py` 增量 | `TextualConsole(DialogueConsole)`：4 个方法写入 transcript | 同名文件扩展 |
| `ui/app.tcss` | CSS：圆角、配色、间距；用主题变量 → 亮/暗自动可用 | `theme.py` |

### 3.2 数据流

```
Input.Submitted
  ├─ turn 进行中 → 忽略（输入框 disabled，见 3.3-4）
  ├─ 以 "/" 开头 → handle_input(loop, text, session, sessions, TextualConsole)   [repl.py 不动]
  │                  └─ 其中 /plan /review 的 console.delta 也指向"当前流式块"
  └─ 否则        → @work(exclusive=True): await loop.process_input(session, text, on_delta=sink)

sink(text)  [同步]  → 攒进 _pending_delta
定时 flush (50ms)   → markdown.append(joined)      ← append 同步可调（AwaitComplete 立即启动）
                       每 50ms 一次 widget 更新，不是每 token 一次；且只重解析尾部

LoopObserver.on_event  → blocks_from_event(event) → transcript.append(block)
回合结束               → flush + await 最后一次 append + 时间戳 + 若在底部则 scroll_end()

审批/ask_user: provider → await push_screen_wait(Screen)   ← 在 worker 里，合法
               取消/超时 → finally 关闭仍打开的模态（见 3.3-5）
```

### 3.3 五个关键机制

1. **同步 sink → 定时 flush**：不改引擎（`DeltaSink` 保持同步）。`_pending_delta` 累积，定时器（50ms）合并后调 `markdown.append(joined)`——`append` 是同步可调的（返回 `AwaitComplete`，内部立即启动），无需调度协程；回合结束时 `await` 最后一次 append 再收尾。性能有双重保障：合并写入 + `append` 只重解析尾部（`_markdown.py:1455-1462`）。
2. **循环跑在 `@work(exclusive=True)`（async，非 thread）**：UI 保持可响应（滚动/复制/退出），且 `push_screen_wait` 合法。`exclusive=True` 是兜底：即使有程序化重复提交，也只会取消上一个 worker，而 `process_input` 在取消时会持久化部分回合（`agent_loop.py:365-376`）。
3. **吸底策略**：仅当用户已在底部（`scroll_offset.y >= max_scroll_y - 2`）时自动 `scroll_end()`；用户上滚查看历史时不打断。
4. **回合并发控制（v1 决策：turn 进行中禁用输入）**：`_begin_turn` 无锁无忙标志（`agent_loop.py:404-414`：无条件 `add_message` + `turn_count += 1` + `guards.reset()` + `reporter.begin_turn()`），两个并发 `process_input` 会互相重置回合记账——旧 UI 的 `asyncio.create_task`（`app.py:233`）没有守卫，属**已存在的潜在缺陷**。v1 用 `input.disabled = True` 在回合期间挡住提交（不做队列，队列+steering 属 P2）；模态屏是独立 screen，不受影响；退出走 app 级绑定（Ctrl+Q，或 v1 补的 Ctrl+C，见 §4 #14），仍可用。
5. **模态生命周期**：`push_screen_wait` 内部 `asyncio.shield`（`app.py:2996-2998`），意味着**取消不会关闭已弹出的模态**——回合超时/中断时模态会变成孤儿留在屏幕上。provider 必须写成 `try: return await push_screen_wait(s) finally: if self.screen is s: self.pop_screen()`，并有测试覆盖"超时后模态已关闭"。

## 4. 集成核对表（旧能力 → 新接线 → 验证方式）

| # | 能力 | 旧接线（证据） | 新接线 | 验证 |
|---|---|---|---|---|
| 1 | 循环事件 → 输出区 | `BodyObserver`（`app.py:88,95`） | `blocks_from_event` + `transcript.append` | 离线单测 + `run_test` |
| 2 | 助手流式文本 | `on_delta=body.write`（`app.py:251`） | sink + 50ms flush | `run_test` 断言文本 |
| 3 | 输入提交 | `Enter` 绑定（`app.py:218-233`） | `Input.Submitted` + `@work` + turn 中禁用（4.3） | `run_test`（含 turn 中提交被拒） |
| 4 | 审批通道 | `approval.set_provider`（`app.py:216`） | `push_screen_wait(ApprovalScreen)` | `run_test` 断言 YES/NO/STOP |
| 5 | ask_user 通道 | `user_prompt.set_provider`（`app.py:215`） | `push_screen_wait(AskScreen)`，契约见 4.2 | `run_test` + pty E2E |
| 6 | slash 命令（16 个） | `_run_command` + `DialogueConsole`（`app.py:235-246`） | `TextualConsole(DialogueConsole)` | `test_repl` / `test_compact_command` 保持绿 + 新冒烟 |
| 7 | `/model` 真切换 | `repl.py:245` `loop.set_model` | 不变 | `test_model_switching` 保持绿 |
| 8 | 会话创建/切换 | `sessions.create()`（`app.py:102`） | 不变 | `run_test` |
| 9 | 回合计时 | `turn_start` 重置（`app.py:237,250`） | 状态栏 + 回合时间戳 | `run_test` |
| 10 | 状态属性读取 | `loop.workspace/tools.list_names/memory_status/engine.model_config`（`header.py:15-32`） | 状态栏/欢迎块读同一批属性 | 单元测试（保属性名） |
| 11 | 滚轮滚动 | `scroll.py` + `Keys.ScrollUp/Down`（`app.py:258-266`） | Textual 原生 | 真实终端 |
| 12 | 自动吸底 | `auto_scroll_to_end`（`app.py:210,253`） | 3.3 的策略 | `run_test` 滚动偏移断言 |
| 13 | 复制 | **旧 UI 无此能力** | `copy_to_clipboard`(OSC52) + 原生选择双路 | 真实终端 |
| 14 | 退出 | `Ctrl-C` → `app.exit`（`app.py:254`） | **行为变更**：Textual 默认退出是 `Ctrl+Q`（`app.py:456-461`），`Ctrl+C` 被改成"有选中则复制、无选中则提示按 ctrl+q"（`app.py:463,3990-4001`）→ v1 在 app 级补 `Ctrl+C`=退出，有选中时仍复制。**机制已核实**：绑定链按"焦点→…→screen→app"查找，`run_action` 返回 False 时继续下一个绑定（`app.py:4223-4246`），Screen 的复制绑定先命中、无选中时 `SkipAction` 落回 app 绑定；子类 `BINDINGS` 按 MRO 覆盖同键（`dom.py:671-693`） | `run_test` + 真实终端 |
| 15 | 资源释放 | `finally: loop.aclose + mcp_manager.aclose`（`app.py:285-289`） | `on_unmount` 同样释放 | 单元测试 |
| 16 | MCP 工具注册 | `mcp_manager.open_all`（`app.py:83-85`） | 不变 | 现有测试 |
| 17 | 配置发现 | `ConfigManager().initialize(path)`（`app.py:65-67`） | 不变 | 现有测试 |
| 18 | `/permissions` 切换 | `repl.py:176-198` 替换 `dispatcher.approval` | **需修缺陷，见 4.1** | 新测试 |
| 19 | Esc 取消工具批 / steering | R10 起已断（`docs/features.md:207-209`） | **v1 不接回**（明确记录，非回归） | — |

### 4.1 审计发现的集成缺陷（v1 必修）

`repl.py:194` 的 `/permissions confirm` 会**新建** `CLIApprovalCallback()`，新实例没有 provider → 下一次 WARN 级审批走 `approval.py:72` 的 `asyncio.to_thread(input)` 读 stdin，而全屏 TUI 独占 stdin → 挂起。

v1 修法（已定）：app 在每条命令执行后检查 `dispatcher.approval`，若是 `CLIApprovalCallback` 就直接 `set_provider(app 的 provider)`——**无需窥探私有属性**（对同类型实例无条件重设即可）。理由：3 行、零改动 `repl.py`（它同时被 legacy 路径与 4 个测试文件使用），缺陷回归测试放 UI 层。
P2 可选清理：引入 `ApprovalRouter`（持有 policy 与 provider，`/permissions` 只翻转 policy），从根上消除"换对象丢 provider"；需同步改 `repl.py::_handle_permissions` 及其测试，v1 不做。

### 4.2 ask_user 专项核对（v1 活路径，非可选）

| 事实 | 证据 | 对设计的要求 |
|---|---|---|
| **默认启用** | `config/models.py:31`（`ToolConfig.enabled=True` 默认）+ `tools/builder.py:143-153`（条件：`include_subagent and user_prompt is not None and enabled`） | AskScreen 是活路径，必须有行为测试与 pty 覆盖 |
| 子代理拿不到 | 同上：`build_subagent_runner(...)` 不传 `user_prompt` | 不会出现"子代理弹模态打断主回合" |
| 一次只能问一个 | `ask_user_tool.py:44` `parallel_safe = False` | 不会双模态；与审批也不会并发 |
| provider 接口**有损** | `user_prompt.py:52-57` `_format_prompt` 把 question+options 拍平成一个字符串；`set_provider` 签名是 `Callable[[str], Any]`（`user_prompt.py:75`） | v1 模态只能显示整段文本（含「（可选：a / b）」行）；**选项列表化需改 provider 签名**（传 question 与 options 两参）→ P2 |
| 空答案 = 优雅降级 | `user_prompt.py:97-99` 空串 → `reason="empty"`；`ask_user_tool.py:122-127` → `_NO_ANSWER`，指示模型自行决定并写出假设 | Esc/取消必须返回 `""`（而非抛异常），才能走这条路径 |
| 异常 = no_user | `user_prompt.py:100-101` 捕获异常 → `reason="no_user"` | 抛异常也能降级，但返回 `""` 语义更准 |
| 超时 = 取消 | `ask_user_tool.py:30`（`_TIMEOUT_GRACE=15.0`）、`:46`（默认 `timeout_seconds=300.0`）、`:57`（`max_duration_seconds`）；`dispatch.py:116-128` 审批预算取回合余量 | 必须实现 3.3-5 的模态 teardown，否则超时留下孤儿模态 |
| 答案走 tool result | `ask_user_tool.py:132` `data={"answered": True, "answer": ...}` | UI 侧无需持久化 |

### 4.3 回合并发（旧 UI 潜在缺陷，v1 修）

证据链：
- `agent_loop.py:404-414`：`_begin_turn` 无条件执行 `session.add_message(user_message(...))`、`session.turn_count += 1`、`dispatcher.guards.reset()`、`reporter.begin_turn(...)`；`agent_loop.py` 全文无 `Lock`、无忙标志。
- `app.py:232-233`：旧 UI Enter 后立即 `input_buffer.reset()` 再 `asyncio.create_task(_submit_text(text))`，无守卫 → 回合进行中可再次提交 → 两个 `process_input` 并发 → 回合计数、guard、reporter 互相覆盖。

v1 处置：turn 进行中 `input.disabled = True`（3.3-4）。验证：`run_test` 断言 turn 中提交被拒、且不产生第二个用户消息块。
P2：提交队列 + 接回 `SteeringQueue`（`app.py:79` 构造后从未接线）。

## 5. 弃用清单（含 blast radius）

### 5.1 直接删除（与 Textual 冲突，被替代）

| 文件 | 生产引用 | 测试引用 | 处置 |
|---|---|---|---|
| `ui/app.py` | `cli.py:54` | `test_app.py` | 替换为 `textual_app.py` |
| `ui/body.py` | `app.py`, `observer.py` | `test_body.py`, `test_observer.py` | 删除 |
| `ui/observer.py` | `app.py:22` | `test_observer.py` | 重写：拆为 `blocks.py`（纯映射）+ 薄适配器 |
| `ui/header.py` | `app.py` | `test_header.py` | 删除 |
| `ui/footer.py` | `app.py` | `test_footer.py` | 删除 |
| `ui/palette.py` | `app.py` | `test_palette.py`, `test_app.py` | 删除（P1 用内置 `CommandPalette` 补回） |
| `ui/scroll.py` | `app.py` | `test_scroll.py` | 删除（Textual 原生） |
| `ui/theme.py` | `app.py` | `test_theme.py`, `test_app.py` | 删除（改 `app.tcss`） |
| `ui/markdown.py` | `body.py` | `test_markdown.py` | 删除（改真渲染） |

对应测试：`test_app.py`/`test_body.py`/`test_header.py`/`test_footer.py`/`test_palette.py`/`test_scroll.py`/`test_theme.py`/`test_markdown.py` 删除或重写；`test_observer.py` 重写为 `blocks_from_event` 的离线单测（更简单、更快）。

### 5.2 删除死代码

| 对象 | 证据 | 处置 |
|---|---|---|
| `ConsoleObserver`（`console.py:62-118`） | 生产无引用；仅 3 个测试文件用（`test_m6_contract_coverage.py` 4 处、`test_tool_chunks.py` 5 处、`test_tool_surface_warning.py` 2 处） | 删除；其 R8c 语义测试迁移到 `blocks_from_event`（纯函数，测试更轻） |

### 5.3 保留（框架无关，且被生产/核心引用）

| 对象 | 证据 | 说明 |
|---|---|---|
| `ui/repl.py::handle_input` + `HELP_TEXT` | `app.py:239` | 16 个 slash 命令的分发核心，v1 直接复用 |
| `ui/slash.py` | `repl.py:15` | 保留 |
| `ui/console.py::DialogueConsole` | `repl.py:14,51` | 作为接口基类，`TextualConsole` 继承它 |
| `ui/trace_render.py` | **`core/export_html.py:20`**（核心反向依赖 ui）+ `repl.py:256,267` | 保留；P2 可搬迁到 `core/`（消除反向依赖） |

### 5.4 `run_repl`：彻底删除（用户已拍板 B）

`run_repl`（`repl.py:298-354`）生产不可达，但它是 ask_user 安全通道唯一的真实终端端到端覆盖，也是 prompt-toolkit 的最后一个使用者。

**删除范围**：
- `repl.py::run_repl` 及 `__all__` 中的导出；`repl.py` 文件头 docstring（第 3-5 行提到懒加载 prompt_toolkit）一并更新
- `tests/_pty_ask_user_child.py`
- `tests/test_repl_ask_user_pty.py`、`tests/test_repl_ask_user_stdin.py`
- `scripts/r7_cases.py` 中调用 pty child 的用例
- `security/user_prompt.py:65` docstring 中提到 `run_repl` 的段落（避免文档撒谎）

**连带死代码（同批清理，证据：唯一调用者是 `run_repl`）**：
- `user_prompt.py:79-81` `set_reader`、`:83-90` `set_interactive`、`:72` `_interactive` 属性，以及 `ask()` 中 `:102-107` 的 interactive 分支
- 对应测试：`tests/test_user_prompt.py:72,81,94,117,130`（4 个用例）
- **保留** `input_fn`/`_read` 的 stdin 兜底（`user_prompt.py:110`）：它是该类的默认行为，被 6+ 个测试依赖，且是 provider 缺失时的 fail-safe
- **保留** `approval.py` 的 `input_fn`/stdin 路径：被 6 个测试文件 + `scripts/r7_cases.py:277` 使用（已在 4.1 用回填 provider 修掉真实缺陷，无需删兜底）

**前置条件（硬性）**：覆盖不能归零。删除动作排在 §8 步骤 6，**必须先**在步骤 4 交付新的 pty 驱动测试（见 §6.4），覆盖 ask_user/审批的真实终端链路；新测试未绿之前不执行本删除。

### 5.5 依赖变更

- `pyproject.toml` 增加 `textual>=8.2`
- 删除 `prompt-toolkit>=3.0`（前置条件：5.4 删除完成，且全仓 `prompt_toolkit` import 清零；`grep -rn "prompt_toolkit" ai_coding/ tests/ scripts/` 只应剩 0 处 import）

## 6. 验证策略（修"验证债"）

1. **纯函数层**：`blocks_from_event` 与块渲染文本的离线单测（不需要 Textual，快且稳）。
2. **行为层**：`App.run_test()` + `Pilot`——提交文本、断言消息块出现、断言流式 markdown 文本、断言审批模态返回值、断言滚动偏移、断言 turn 中提交被拒（4.3）、断言超时后模态已关闭（3.3-5）。
3. **截图**：`scripts/tui_shot.py` 用 `save_screenshot()` 产出 SVG，人工比对参考图；v1 不进 CI（SVG diff 脆）。
4. **pty 端到端**（替代 5.4 删除的 2 个测试）：新驱动脚本在真实 pty 中启动 Textual app，驱动一次审批与一次 ask_user，断言工具侧拿到答案。这是删除旧测试的**前置条件**（见 5.4）。
5. **真实终端清单**（不可省略，必须人眼过）：滚轮滚动、拖选复制（`ctrl+c` 内置复制 + Shift 原生双路）、**Ctrl+C 无选中=退出 / 有选中=复制、Ctrl+Q 退出**（§4 #14）、resize 重排、多轮会话、审批流程、`/permissions confirm` 后再次触发审批（4.1 的缺陷回归）。
6. **全量回归**：基线为 `pytest --collect-only` 实测 **1200 个**（上次全量运行 1196 passed + 4 skipped），除 5.1/5.2/5.4 所列外必须全绿；`ruff` 干净。

## 7. 风险与解决办法（逐条已核实）

| # | 风险 | 已核实的解决办法 |
|---|---|---|
| 1 | 模态接线（worker 上下文） | 上下文链已核实：`@work` 设置 `active_worker` ContextVar（`worker.py:37,75`），`dispatch.py:199` 的 `gather` 复制上下文 → 工具内 `push_screen_wait` 合法；非 worker 会抛 `NoActiveWorker`（`app.py:2961-2965`）→ **硬约束：循环必须跑在 async worker，禁止 `thread=True`**。第 0 步 spike 验证 |
| 2 | 孤儿模态（取消/超时后留在屏幕） | `push_screen_wait` 内部 `asyncio.shield`（`app.py:2996-2998`），取消**不会**关屏 → provider 用 `try/finally` + `if self.screen is s: pop_screen()`（3.3-5），并有专项测试 |
| 3 | 长会话 widget 膨胀 | 混合容器：已完成回合写入 `RichLog`（`max_lines` 自动裁剪，`_rich_log.py:61,264`），仅"当前流式回合"用 `Markdown` widget；回合结束转写进 RichLog 并移除 widget。v1 先测量再启用 |
| 4 | 流式渲染成本 | 双重缓解且已核实：50ms 合并（每 token 一次 → 每秒 ≤20 次）+ `append` 只重解析尾部（`_markdown.py:1455-1462`）→ 成本与**新增**文本成正比，不随回答长度增长 |
| 5 | 复制与退出的键位冲突 | 已核实 Textual 内置：`ctrl+c,super+c` → `action_copy_text`（`screen.py:272,985`）→ OSC52，无选中时 `SkipAction` 落回；**但默认退出是 `Ctrl+Q`，`Ctrl+C` 只弹提示**（`app.py:456-463,3990-4001`）→ v1 在 app 级补 `Ctrl+C`=退出（无选中时生效），保留"有选中=复制"。限制：OSC52 依赖终端（Textual 自述 macOS Terminal 不支持）→ 兜底 Shift+拖选走终端原生选择，文档写明 |
| 6 | 回合并发（旧缺陷） | v1 决策：turn 中禁用输入（4.3），`exclusive=True` 兜底；`process_input` 取消时持久化部分回合（`agent_loop.py:365-376`） |
| 7 | 退出/中断中途丢数据 | 已核实 `process_input` 的 `CancelledError` 分支会 `_close_turn` 持久化（`agent_loop.py:365-376`）→ `on_unmount` 取消 worker 并 await 即可 |
| 8 | 删除旧 pty 测试后的覆盖空窗 | 硬性前置：新 pty 测试（§6.4）先绿，再执行删除 |
| 9 | ask_user 选项被拍平（无法渲染选项列表） | 已知限制，v1 接受（显示整段文本）；P2 改 provider 签名（4.2） |
| 10 | 新依赖引入 | 4 个纯 Python 包；`textual` 是活跃维护的主流框架 |

## 8. 实施顺序（writing-plans 的输入）

0. **Spike**（throwaway）：最小 Textual app 跑在 `cli.py` 的既有事件循环上；worker 内 `push_screen_wait`（含**取消后模态 teardown**，验证 3.3-5）；同步 sink → 50ms → `Markdown.append`；并验证 `thread=True` 的反例会抛 `NoActiveWorker`。
1. 依赖 + `textual_app.py` 骨架（状态栏 + transcript + 输入框，不接循环）；`run_test` 行为测试；截图脚本。
2. 循环接线：提交 → `process_input` → 流式 markdown 块；回合结束收尾；turn 中禁用输入（4.3）。
3. 事件 → 块：`blocks_from_event` + `ToolBlock`（TDD，先写 5.2 迁移过来的 R8c 语义测试）。
4. 审批/ask_user 模态屏（TDD，覆盖 YES/NO/STOP、空答案 `""`、超时 teardown、4.1 缺陷）+ 新的 pty 端到端驱动测试（§6.4，删除旧测试的前置）。
5. slash 命令接线：`TextualConsole` + `/permissions` provider 回填。
6. 弃用清理（前置：步骤 4 的 pty 测试已绿）：删 5.1/5.2/5.4 所列文件、测试与连带死代码（含 `user_prompt.py` 的 `set_reader`/`set_interactive`）；改 `pyproject.toml`；更新 `docs/features.md`（R10 已知缺口节已过时）、`docs/README.md`、`docs/architecture.md`、`docs/how-to-run.md`。
7. 真实终端验收（6.5 清单）+ 全量回归。

## 9. 非目标（v1 明确不做）

- 欢迎页（logo/清单/命令表）→ P1
- 命令面板（`/` 弹出）→ P1（v1 中 `/plan` 等键入回车仍可用，但无选择面板，属**已知退化**）
- 侧栏（Home/Projects/Agents/Settings）→ P2（那四屏后端无数据模型）
- 计划卡、结构化工具结果卡 → P2
- Esc 取消工具批、steering 接回 → 单独议题（R10 起已断）
- 主题切换 UI → 默认亮色，CSS 变量留好
- **模型驱动的 slash 命令不渲染工具事件块**（`/plan`、`/review`、未知 `/foo`）：`handle_input` 不接受 observer（`repl.py` 签名不动），所以这些命令期间 `ToolStarted`/`ToolFinished` 不进 transcript、状态栏不置忙（流式回答本身仍进 transcript）。旧 UI 因 loop 级 observer 会渲染——属 **v1 已知退化**，与命令面板退化并列记录（Task 6 review 发现，Task 7 写进 `docs/features.md`）。
