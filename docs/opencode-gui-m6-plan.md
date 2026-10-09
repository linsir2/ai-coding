# OpenCode GUI 复刻 — M6 收尾（迁移 + 测试补全 + CLI 简化）

> 状态：实施完成，待全量核验。分支 `feat/opencode-gui-replica`，前序 M5 = `34c716b`。
> 范围依据 `docs/opencode-gui-replica-plan.md` M6 行：「Python/TS 测试补全、删除旧 Textual UI、文档、启动脚本」。
> **偏差（用户指示）：旧 Textual UI 的实际删除延后**，本里程碑只完成逻辑迁移 + 清单固化（见 §4）。

## 1. 逻辑迁移：`ai_coding/ui/` → `ai_coding/app/`

迁移对象是**非 UI 的纯逻辑**（Textual 组件不碰），原则：先改测试 import（测试是唯一消费者），再搬代码，最后全绿。`ui/` 里的原文件保留不动（Textual 壳仍在 import，归入延后删除清单）。

| 源（`ai_coding/ui/`） | 目标（`ai_coding/app/`） | 内容 | 测试 |
|---|---|---|---|
| `slash.py` | `slash.py` | `SlashCommand`、`parse_slash`、`suggest_command`、别名/废弃表、编辑距离建议 | `tests/test_slash.py`（19）改 import → 绿 |
| `trace_render.py` | `trace_text.py` | `summarize_turn` 等行式 trace 渲染（无 Textual） | `tests/test_trace_render.py` → 重命名 `tests/test_trace_text.py`（15）→ 绿 |
| `console.py` | `console.py` | 只搬 `DialogueConsole` + `_LineSink`；`TextualConsole` 是 Textual 专属，**不搬** | 随 repl 测试走 |
| `repl.py` | `commands.py` | `handle_input`、`REPLAction`、帮助/状态/预算/compact 等命令逻辑（460 行，去掉 Textual 分支） | `tests/test_repl.py` 等（39）→ 绿 |

同步归口的 import（**非 Textual 测试**，共 6 个文件）：

- `tests/test_repl.py` — 顶部 1 处 + 局部 `_budget_report` 5 处 → `ai_coding.app.commands`
- `tests/test_plan_command.py` — `DialogueConsole`/`handle_input` → `app.*`（`_plan_topic` 仍指 `ui.textual_app`，见 §4）
- `tests/test_plan_m1.py` — `ui.repl`/`ui.slash`/`ui.console` → `app.*`（`_hhmm` 指 `ui.transcript`，见 §4）
- `tests/test_compact_command.py` — `ui.repl`/`ui.slash` → `app.*`
- `tests/test_m6_contract_coverage.py`、`tests/test_ui_text_boundary.py` — `ui.console` → `app.console`

调用方同步：`ai_coding/core/export_html.py:20` → `from ai_coding.app.trace_text import summarize_turn`。

归口后 `ai_coding/` 生产代码中**零** `from ai_coding.ui...` 引用（仅 `scripts/tui_shot.py` 与 Textual 专属测试仍引用）。

## 2. 前端 IPC 层测试（TDD 补全）

新增 `frontend/test/ipc.test.ts`（6 tests）——M0.5 引入的 `IpcClient` 此前无直接单测：

- `connect` socket 连接 resolve / 未连接 `request` reject / 连接后 `request` 透传 method+params
- `onEvent` 订阅收 notification、退订后不再收到
- `onRequest` 注册审批处理器（`approval.request` 往返）
- `close` 清理

mock 边界：`node:net` + `vscode-jsonrpc/node`（`mock.module`），不启真实 socket。

**全量前端：107 pass / 0 fail（13 文件），`tsc --noEmit` 干净。**

## 3. CLI 简化

`ai_coding/cli.py`：

- 移除 `--legacy-ui` 选项与 `if legacy_ui: run_app else: run_tui` 分支，入口直接 `from ai_coding.server.launcher import run_tui`。`ai-coding --help` 中 legacy 字样 0 处；`tests/test_cli_cleanup.py` 4 pass。
- 残留注释清理：`server/handlers.py`（cancel 两层设计注明「ported from textual_app」）、`server/world.py` docstring（world 成为唯一后端组合根）、`app/commands.py` docstring（指向 OpenTUI over IPC）。

## 4. 旧 Textual UI 删除 —— ✅ 已执行（2026-10-09）

原为延后事项（用户指示「延后删除的别忘了」），用户确认后执行完毕。执行记录如下。

**生产代码 — `ai_coding/ui/` 16 文件整目录删除**（3738 行）：transcript / textual_app / repl /
blocks / trace_render / modals / app.tcss / console / slash / resume / resume_screen / commands /
plain / status_bar / input_bar / `__init__`。产出与渲染逻辑的旧拷贝随删；`app/` 四件套
（slash / trace_text / console / commands）成为唯一实现，`server/` + `frontend/` 承担运行时。

**测试**：14 个整删 + 1 个重写改名 + 9 个外科手术 + 1 个契约同步：

- 整删（Textual 专属）：test_textual_app、test_commands、test_modals、test_blocks、
  test_backfill、test_transcript×3、test_ui_queue、test_resume_screen、test_pty_textual
  （含 `_pty_textual_child.py`）、test_resume（`ui/resume.py` 无生产使用）、test_tool_chunks
  （TranscriptObserver 随删）
- 重写：`test_ui_text_boundary.py` → `test_console_text_boundary.py`（只保留活 guard：
  `app/console.py` 的 print 必须 `markup=False`、RichLog 禁用、bracket 逐字行为；
  Textual AST 规则随 UI 消失）
- 外科手术（删 Textual render 用例、保生产断言）：
  test_plan_command（删 4 个 `_plan_topic`/路由用例）、
  test_plan_m1（时间戳用例改用 `datetime.fromisoformat`）、
  test_plan_runner（删 `_step_header` 匹配段）、test_loop_events（2 个 blocks render 用例）、
  test_thinking（7 个 render/fold 用例 + 无用的模块级 import/helper）、test_ledger、
  test_background_tasks、test_structured_handoff、test_tool_surface_warning（各 1 个用例）
- 契约同步：`test_config_loader.py` 删 ui 块两条断言

**连带清理**：`pyproject.toml` 移除 `textual>=8.2` 依赖；`config/models.py` 删除 `UIConfig`
（`ui.steering` / `ui.tool_chunks` 生产零读取，只被待删测试引用；`AppConfig` `extra="ignore"`
容忍配置里的残留 `ui:` 段）；`scripts/tui_shot.py` 删除；`scripts/trace_cases.py` 注释改写；
`frontend/src/routes/session.tsx` 与 `state/session.ts` 三处对 `ui/textual_app.py` 的悬空引用
改为「retired Textual UI」表述；`docs/architecture.md`、`features.md`、`data-contracts.md`、
`how-to-run.md`、`README.md` 的当前态描述改写。

**替代覆盖缺口核对**：旧「事件→块渲染」由 `frontend/test/`（107 bun tests）覆盖；
socket 级 IPC 由 `tests/test_server_ipc.py` 覆盖——无缺口。

**附带收益**：M6 记录的 flaky 挂起（`app.workers.wait_for_complete()`，全量 ~50% 概率）
随 `test_textual_app.py` 删除而消失，全量 pytest 恢复稳定。

## 5. 核验记录

| 项 | 结果 |
|---|---|
| `pytest`（全量，删除后） | ✅ 2012 passed + 2 failed（预存 loop_digest）+ 2 skipped（total 2016，到 100%，无挂起） |
| `ruff check`（触碰文件） | ✅ All checks passed（迁移 11 处 I001 + test_ledger 1 处 F401 自动修复） |
| `mypy --strict` | ✅ 116 files, no issues |
| `bun test` | ✅ 107 pass / 0 fail |
| `tsc --noEmit` | ✅ clean |
| tmux 冒烟 | ✅ 见 §5.2（删除后重跑，真实回合 pong 往返） |

### 5.1 全量 pytest

**通过记录（Textual UI 删除后，2026-10-09，`python -u -m pytest -q`）**：

```
[100%]  2012 passed, 2 failed, 2 skipped（total 2016，~100s）
```

2 个失败是 HEAD 上的预存问题（`test_loop_digest.py` 的
`test_working_memory_is_injected_only_when_history_was_compacted` /
`test_working_memory_is_a_tail_block_never_the_system_prompt`），非本里程碑回归。

删除前 2388 项、后 2016 项；§5.1 记录的 flaky 挂起（`workers.wait_for_complete()`）随
`test_textual_app.py` 删除消失，连续两次全量均一次到 100%、无挂起。
> 环境备忘：全量跑完的进程在写完全部测试点后、`short test summary` 之后退出，
> 偶见末尾统计行不落盘（本机 bash 工具的 120s 管道超时所致）；百分比与逐字符计数
> （[.Fxs]）与 `--collect-only` 总数一致，判定以两者对齐为准。

**flaky 记录（已消解）**：删除前 `test_submitting_while_the_palette_is_open_closes_it` 的
`await app.workers.wait_for_complete()`（`textual/worker_manager.py:172`
`asyncio.gather(*[worker.wait() ...])`）存在顺序依赖的无界等待，最小复现为
`pytest tests/test_repl.py tests/test_textual_app.py`（同状态跑两次一过一挂；
HEAD 版 `test_repl.py` 同样挂 → 预存、非 M6 迁移引入）。该文件及 `ai_coding/ui/` 已随
本次删除移除，flaky 随之消失。

### 5.2 tmux 冒烟（M6 后入口链路；Textual 删除后又跑了一次）

`tmux new-session -d -s tuiN -x 140 -y 42` + `.venv/bin/ai-coding --path config.local.yaml --workspace /tmp/tui-m6del-ws --skills ./skills`：

- Home 正常渲染（Logo / prompt / `dashscope · qwen3.8-2.4t-a95b`）；
- 真实回合：输入 `Reply with exactly one word: pong` → 回复 `pong`，`+ Thought` 折叠、Context 侧栏 `3,073 tokens / 6% used`、footer `3,073 (6%) · ctrl+p commands · 0.1.0` 均正常；
- `--legacy-ui` 移除后入口唯一（`cli.py` 直接 `run_tui`），删除 `ai_coding/ui/` 后无回归（`ui:` 配置段被 `extra="ignore"` 容忍）。

## 6. 踩坑记录

1. **后台 pytest 会被工具超时连坐杀掉**（进程组级 kill）：必须 `setsid sh -c '...' < /dev/null &` 脱离，且外层 shell 的 stdout/stderr 也要断开，否则工具会等管道关闭直至超时。
2. 前端测试 mock `net`/`vscode-jsonrpc` 用 `mock.module`；`onEvent` 退订测试不能靠 mock connection 验证（dispose 是 no-op），改为断言退订调用后 listener 计数不变。
3. **用正则删测试函数要吃装饰器**：`(?=\ndef |\nasync def )` 这类前瞻会越过 `@pytest.mark.asyncio`，
   把装饰器吞掉（`test_loop_events.py` 就留下了游离 `o` 与裸 async def）。安全做法：前瞻
   `\n\n\n` 或 `\n\n@`，或直接读文件确认边界。
4. **全量 pytest 末尾统计行偶发不落盘**（进程写完 short summary 就退出）：判定以 `[100%]` +
   进度区 `[.Fxs]` 字符计数与 `--collect-only` 总数对齐为准。

## 7. ask_user 通道对接（M6 后增量，2026-10-09）

唯一一根「后端已实现、前端没接」的线（P6c/R6c），M6 删除收尾后补上：

- **后端**：`world.build_world` 由 `UnavailableUserPromptCallback` 改为
  `CLIUserPromptCallback`（无 provider 时退回 stdin，非 TUI 入口不变）；
  `handlers._wire_user_prompt` 复制 `_wire_approval` 的范式，把 provider 装成
  对前端的 `user_prompt.ask` 服务端请求（`{question, options}` → 纯文本答案）。
- **前端**：`util/ask.ts`（纯逻辑：参数整形 + 选择→答案）、`component/ask.tsx`
  （`◆ Question` 面板，占 prompt 槽位，与 PermissionPanel 同一视觉语言；
  ↑/↓/j/k + 1-9 + enter + esc）、`app.tsx`（onRequest 处理器 + pending 状态 +
  turn.finished 兜底 settle + prompt/Shell 层门控）、`session.tsx`（props + 槽位）。
- **测试**：`tests/test_server_ipc.py` 新增 2 个 socket 级往返
  （`test_user_prompt_round_trip_over_the_wire`、
  `test_user_prompt_an_empty_answer_is_not_an_answer`）；`frontend/test/ask.test.ts`
  7 个纯逻辑用例（含 options 非字符串丢弃、越界选择=跳过）。
- **E2E**（tmux 140×42 真模型）：提问面板出现（`◆ Question 要不要继续重构？` +
  `▸ 1 继续 / 2 停止` + 工具卡 `⠏ 正在询问用户`）→ 按 `2` → `✓ 已询问用户 /
  用户回答：停止` → 模型「不再继续重构」；第二次 esc → `✗ 已询问用户 / no_answer`
  → 模型「我将自行决定：选择 A」。
