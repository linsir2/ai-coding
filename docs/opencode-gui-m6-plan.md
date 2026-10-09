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

## 4. ⚠️ 延后事项：旧 Textual UI 删除（用户指示：延后，勿忘）

以下清单固化了删除 `ai_coding/ui/` 时的**全部**牵连。执行删除时按此走，删完全量核验。

**生产代码（16 文件，3738 行）** — `ai_coding/ui/`：

`transcript.py`(891)、`textual_app.py`(556)、`repl.py`(460)、`blocks.py`(417)、`trace_render.py`(316)、`modals.py`(229)、`app.tcss`(225)、`console.py`(140)、`slash.py`(115)、`resume.py`(83)、`resume_screen.py`(80)、`commands.py`(67)、`plain.py`(66)、`status_bar.py`(59)、`input_bar.py`(28)、`__init__.py`(6)。
（`app/slash.py`、`app/trace_text.py`、`app/console.py`、`app/commands.py` 是前四者的活拷贝，删除后以 app/ 为唯一实现。）

**Textual 专属测试（import `ai_coding.ui` 的）**：

- 完全专属（随删）：`test_textual_app.py`、`test_commands.py`、`test_modals.py`、`test_blocks.py`、`test_transcript*.py`（3 个）、`test_ui_queue.py`、`test_resume_screen.py`、`test_backfill.py`、`test_dispatch_grants.py`、`test_pty_textual.py` + `_pty_textual_child.py`、`test_tool_chunks.py`、`test_structured_handoff.py`、`test_loop_events.py`（部分用例）、`test_thinking.py`（部分用例）、`test_ledger.py`/`test_background_tasks.py`/`test_tool_surface_warning.py`/`test_plan_runner.py`（局部 import 用例）
- **部分依赖**（需局部改造而非整删）：`test_plan_command.py`（`_plan_topic` ← textual_app）、`test_plan_m1.py`（`_hhmm` ← transcript）、`test_ui_text_boundary.py`（`ResumeScreen` 用例）、`test_compact_command.py`（已归口，复核）
- 注意：`test_textual_app.py`/`test_commands.py` 被 `test_backfill.py`/`test_transcript_streaming.py`/`test_transcript_widget_growth.py` 反向 import（`_app`/`_FakeLoop` fixture）

**脚本/文档**：`scripts/tui_shot.py`（import textual_app）、`scripts/trace_cases.py`（注释提及）、`docs/architecture.md`（ui/ 目录描述需改写为 app/ + frontend/）。

**替代覆盖缺口**：旧 Textual 测试覆盖的「事件→块渲染」（blocks/transcript）在新前端由 bun 测试覆盖（`frontend/test/`，107 tests）；删除时逐条对照 `docs/features.md` 的测试映射段，确认新前端有等价用例，没有的先补。

## 5. 核验记录

| 项 | 结果 |
|---|---|
| `pytest`（全量） | ✅ 2 failed（预存 loop_digest）+ 2384 passed + 2 skipped（179s）；另有预存 flaky 挂起，见 §5.1 |
| `ruff check`（本里程碑触碰文件） | ✅ All checks passed（含 11 处 I001 自动修复） |
| `mypy --strict`（同上） | ✅ 8 files, no issues |
| `bun test` | ✅ 107 pass / 0 fail |
| `tsc --noEmit` | ✅ clean |
| `ai-coding --help` | ✅ 无 legacy 字样 |
| tmux 冒烟 | ✅ 见 §5.2 |

### 5.1 全量 pytest

**通过记录**（2026-10-09，`python -u -m pytest -v -p no:warnings`）：

```
2 failed, 2384 passed, 2 skipped in 179.34s (0:02:59)
```

2 个失败是 HEAD 上的预存问题（`test_loop_digest.py` 的
`test_working_memory_is_injected_only_when_history_was_compacted` /
`test_working_memory_is_a_tail_block_never_the_system_prompt`），非本里程碑回归。

**已知 flaky（预存，非 M6 引入）**：全量跑约 1/2 概率挂在
`tests/test_textual_app.py::test_submitting_while_the_palette_is_open_closes_it`
（第 23 个用例，全量第 ~1714 个）的 `await app.workers.wait_for_complete()`
（textual/worker_manager.py:172 `asyncio.gather(*[worker.wait() ...])`）——
某个 turn-group worker 永不 settle 时该 await 无界阻塞。复现与证据：

- 最小组合 `pytest tests/test_repl.py tests/test_textual_app.py` 同状态跑两次：一过一挂；`slash+textual`、`commands+textual` 组合通过 → 与具体前序文件 + 时序相关。
- **HEAD 版 `test_repl.py`（import `ui.*`）同样挂**：`git checkout tests/test_repl.py` 后复现 61 dots 无完成行 → 与 M6 的 import 迁移无关。
- 挂起时 faulthandler 栈：主线程在 `asyncio select`（等 future），无异常。
- 处置：**不修**——该测试文件及 `ai_coding/ui/` 全部在 §4 延后删除清单内，修 throwaway 代码违背最小化原则。删除 ui/ 后此 flaky 随之消失。

### 5.2 tmux 冒烟（M6 后入口链路）

`tmux new-session -d -s tui6 -x 140 -y 42` + `.venv/bin/ai-coding --path config.local.yaml --workspace /tmp/tui-m6-ws --skills ./skills`：

- Home 正常渲染（Logo / prompt / `dashscope · qwen3.8-2.4t-a95b`）；
- 真实回合：输入 `Reply with exactly one word: pong` → 回复 `pong`，`+ Thought` 折叠、Context 侧栏 `3,073 tokens / 6% used`、会话 tabs 正常；
- `--legacy-ui` 移除后入口唯一（`cli.py` 直接 `run_tui`），无回归。

## 6. 踩坑记录

1. **后台 pytest 会被工具超时连坐杀掉**（进程组级 kill）：必须 `setsid sh -c '...' < /dev/null &` 脱离，且外层 shell 的 stdout/stderr 也要断开，否则工具会等管道关闭直至超时。
2. 前端测试 mock `net`/`vscode-jsonrpc` 用 `mock.module`；`onEvent` 退订测试不能靠 mock connection 验证（dispose 是 no-op），改为断言退订调用后 listener 计数不变。
