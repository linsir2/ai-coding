# A2：把后端命令层的其余命令接到 TUI（设计）

> 基线：A1（`617296a`）已用**一个** `session.command` 入口对接 `handle_input` 分发，`/plan` `/review` 全流程 E2E 通过。
> A2 的实质不是"往列表加 8 个条目"，而是三件事：**S1 补齐 A1 漏掉的命令后置条件（不变量）**、
> **S2 前端命令面（文本型 vs 对话框型的分流）**、**S3 逐条判定命令归属**。

---

## 1. 命令清单与归属判定（事实来源：`app/commands.py` + 运行时验证）

每条都核过真实实现与数据来源。**归属三态**：`暴露`（TUI 走命令通道）、
`影子`（前端已有更结构化的等价物，命令通道版本保留但列表不指向）、`保留前端`。

| 命令 | 后端实现 | 数据来源 | 输出形状 | 副作用 | TUI 归属 | 判定理由 |
|---|---|---|---|---|---|---|
| `/budget` | `_budget_report` | trace store summary + `render_budget` | 单行 | 无 | **暴露** | A1 E2E 已验证；纯查询 |
| `/trace` | `_trace_report` | `loop.trace.current_path` + `render_turn` | 单行 | 无 | **暴露** | 纯查询 |
| `/todo` | 内联分支 | `session.task.render()` | 多行 | 无 | **暴露** | 与 live `plan` part 冗余，但零成本且能查全量 |
| `/memory` | `_memory_report` | `loop.memory_status()` | 多行 | 无 | **暴露** | 纯查询 |
| `/config` | `_handle_config` | `loop.settings` + `default_model` | 5 行 | 无 | **暴露** | 与 DialogStatus 字段不同（见 §6 D3） |
| `/tasks` | `_handle_tasks` | subagent runner `task_list()` | 多行表格 | 无 | **暴露** | 纯查询 |
| `/compact` | 内联分支 → `loop.compact_session()` | 压缩器 + 会话文件 | 单行 | **改写会话文件并落盘** | **暴露**（UI 决策见 §6 D1） | 用户显式请求的运维动作 |
| `/permissions` | `_handle_permissions` | `dispatcher.approval` / `gate.grants` | 多行 / 单行 | **换审批回调 / 清 grant** | **暴露 + 强制 S1** | 带参变更，撞 I1 不变量 |
| `/status` | 内联分支 | session id + 消息数 + memory | 单行 | 无 | **影子** | DialogStatus 结构化（config.summary） |
| `/skills` | 内联分支 | `loop.skills.catalog_text()` | 多行 | 无 | **影子** | DialogSkills 结构化（skills.list） |
| `/model <name>` | `_handle_model` | `loop.models` + `set_model` | 单行 | **换模型** | **影子**（手打可用） | DialogModels 更强；两者同走 `world.loop.set_model`（`handlers.model_set` 已核实），无分歧 |
| `/resume [id]` | `_handle_resume` | `SessionService.list/resolve/load` | 多行 / 单行 | **切会话（NEW_SESSION）** | **影子 + 带参暴露** | DialogSessions 是主 UI；`/resume <id>` 手打可用，A1 已接 NEW_SESSION |
| `/clear` | 内联分支 | `sessions.create()` | 单行 | **新会话** | **已接**（A1） | NEW_SESSION 动作已落地 |
| `/help` | 内联分支（静态 HELP_TEXT） | — | 多行 | 无 | **影子** | DialogHelp 更好 |
| `/new` | 无（前端 `session.create`） | — | — | 新会话 | **保留前端** | 前端直接建会话，不绕后端 |
| `/quit` | 内联分支 → `REPLAction.QUIT` | — | — | **后端 shutdown** | **暴露 + 强制 S1** | 撞 I5；`app.exit` 仍是主入口 |
| `/plan` `/review` | A1 已完成 | — | — | — | **已接** | — |

**新发现的死命令缺陷（随 A2 修）**：`/tasks` 只打印 `run_id[:12]`，`SubAgentRunner.cancel(run_id)` 精确查表且**无前缀解析** → 用户抄列表里的 12 位 id 去 stop 必然 `no background task`。`/resume` 已有 `SessionService.resolve(prefix)` 先例。修法：`SubAgentRunner` 加 `resolve_run_id(prefix)`，`_handle_tasks` 用它；前缀歧义时列出候选（抄 `/resume` 的既有约定）。

## 2. DDD 分层

**Domain（既有，不动）**
- `SlashCommand` / `parse_slash`（`app/slash.py`）— 命令词汇的领域模型（`/plan` 等 19 条 + 废弃墓碑）
- `REPLAction`（`app/commands.py`）— 一行输入的结果语义（`CONTINUE` / `QUIT` / `NEW_SESSION`）
- `Session` / `TaskState` / `BackgroundTask` / `TraceStore` / `PermissionGate`（`domain/*`、`core/*`）
- `ApprovalCallback` / `UserPromptCallback`（`security/*`）— 交互端口

**Application**
- **CommandDispatch = `handle_input`**（`app/commands.py`）— 唯一命令语义来源；TUI 与 REPL 共用
- 查询端口：`AgentLoop.compact_session` / `memory_status` / `set_model` / `skills`、`SubAgentRunner.task_list|cancel`、`SessionService.list|resolve|load`

**Infrastructure（inbound/outbound adapter）**
- 入站：`session_command` handler（JSON-RPC → CommandDispatch）、`EventBridge`（loop 事件 → wire 事件）
- 出站：`IpcConsole`（CommandDispatch 的输出 → `console.line` 通知）、`approval.request` / `user_prompt.ask` 双向请求

**Interface（UI）**
- 发现面：`SLASH_COMMANDS` 目录（slash 键唤起）+ ctrl+P palette（keymap 命令）
- 入口：composer submit（文本型命令）/ keymap command（对话框型与带 seed 型）
- 落点：transcript `notice` part（`console.line` 渲染，端口旧 `Block("info")`）

**依赖方向**：UI → Infrastructure → Application → Domain。UI 不得直接读 Domain（这正是 A2 不给 `/tasks` 做专用 handler、而让命令层统一输出的理由）。

## 3. 数据流

**F1 文本型命令**（`/budget`、`/trace`、`/todo`、`/memory`、`/config`、`/tasks`、`/compact`）
```
composer submit "/budget"
  → dispatch(): text.startsWith("/") → client.request("session.command", {id, text})
  → handler.session_command: 校验会话 → spawn(run_command) → ack {turnId, sessionId}
  → run_command: bridge.command_run=True → handle_input(loop, text, session, sessions, IpcConsole, observer=bridge)
      └─ 命令分支 console.info(rendered) → IpcConsole._line → service.notify("event", console.line)
  → 终局: notify("event", turn.finished{status, action, sessionId}) → bridge.command_run=False
  → 前端: console.line → appendNotice(渲染为 dim part) → turn.finished → 解锁 busy → 派发队列
```

**F2 带参命令**（`/permissions auto`、`/tasks stop <id>`、`/model <name>`、`/resume <id>`）
与 F1 同链路，差别只在输入：slash 自动补全**遇空格即消失**（`slashQuery` 遇空白返回 `null`），所以带参命令的 Enter 走 submit 而非选中。参数由后端 `parse_slash` + `cmd.raw` 解析——**前端不做命令语义解析**（唯一事实在后端）。

**F3 副作用型**
- `/clear`、`/resume <id>` → `REPLAction.NEW_SESSION` → handler 切 `state.session` + `sessions.save`（可列可载）→ 终局事件带 `action="new_session"` + 新 `sessionId` → 前端 `onSelectSession` 切 tab
- `/quit` → `REPLAction.QUIT` → handler `shutdown()` → 前端须自行退出（**I5，A2 补**）
- `/permissions auto|confirm` → 换 `dispatcher.approval` → **必须重装 IPC provider（I1，A2 补）**
- `/compact` → 改写并落盘会话文件 → handler 在终局事件带 `"reload": true` → 前端
  `loadSession()` 重投影（`setState({...loaded, queued: state.queued})`，**I9**）
  + toast 报压缩前后条数（解释不能随重投影一起被抹掉）

## 4. 不变量契约

| # | 不变量 | 违反后果 | 验证方式 |
|---|---|---|---|
| **I1** | 命令执行后（finally），若 `loop.dispatcher.approval` 是 `CLIApprovalCallback`，必须重新 `set_provider(IPC provider)` | provider 丢失 → 审批回落 `asyncio.to_thread(input)` 读 pty stdin → **回合挂死**，审批面板永不出现 | socket 级测试：`session.command "/permissions confirm"` → 触发一次 WARN 审批 → 断言客户端收到 `approval.request` |
| **I2** | 同上，对 `world.user_prompt` | 目前无命令换它，但对称防御（M6.5 同款） | 同款测试（`user_prompt.ask` 往返） |
| **I3** | 一次命令运行**恰好一个**终局 `turn.finished`（内层一律 `turn.step`；取消/异常路径也发） | 前端 busy 永不解除 / 队列不派发 / 反复解锁 | 已有 3 个 IPC 测试覆盖正常路径；**A2 补**取消与异常路径的终局唯一性 |
| **I4** | 命令运行期间排队的输入，在终局事件后才派发 | 命令中途插入新一轮 | E2E：`/review` 跑到一半打字 → 回车 → 断言该行在终局后才出现 |
| **I5** | 后端停止（`/quit`、崩溃、`shutdown`）后前端必须退出或明确报错 | 前端对死 socket 静默挂起（现状：`connection.closed` 无人订阅） | E2E：`/quit` 命令 → 前端干净退出；另测后端 kill → 前端退出并提示 |
| **I6** | `NEW_SESSION` 后 `state.session_id` == 事件 `data.sessionId`，且新会话可 list/load | tab 指向不存在的会话 | 已有 `test_session_command_new_session_switches_the_live_session` |
| **I7** | `console.line` 的 text 原样透传，前端只渲染不解析（无 markup 注入面） | 会话预览/工具输出里的 `[k=v]` 被吞或崩 | 已有 `test_console_text_boundary`；A2 E2E 用含 `[` 的输出复验 |
| **I8** | slash 目录不宣称做不到的动作：每个条目要么指向 keymap command，要么明确是"文本提交" | 选了没反应（Home 静默失败就是这么来的） | 目录类型系统：`SlashCommand` 的 `command` 字段改为可选 + 单测保证每条目二者恰居其一 |
| **I9** | 历史重投影（`/compact` 触发）**不得静默丢弃排队中的提交**：重投影保留 `state.queued` | 用户排队的输入凭空消失 | E2E：`/compact` 时队列非空 → 重投影后队列仍在 |

## 5. 功能模块清单（改动点）

**后端**
1. `server/handlers.py`
   - 抽出 `_install_providers(world, service)`（幂等）：对 `dispatcher.approval` 与 `world.user_prompt` 各自 `set_provider`；`make_handlers` 与每次 `run_command` 的 finally 都调用（**I1/I2**）
   - `run_command` finally 增加 provider 重装；`quit` 分支的通知-退出时序（**I5** 后端侧）
   - `_handle_tasks` 走前缀解析（`/tasks stop` 死命令修复）
2. `core/subagent.py`：`resolve_run_id(prefix)`（前缀唯一 → 返回完整 id；歧义 → `None` + 候选列表接口）
3. 测试：`test_server_ipc.py` 补 I1 / I3(取消/异常终局唯一) / I5(后端侧) 用例；`test_repl.py` 或新文件补 `/tasks stop` 前缀解析单测

**前端**
4. `util/slash.ts`：`SlashCommand.command` 改可选（**I8**）；新增 7 个文本型条目（budget/trace/todo/memory/config/tasks/compact）+ permissions（命令带 seed 变体）
5. `component/prompt.tsx`：`select()` 对"无 keymap command"的条目走 `onSubmit("/"+name)` 文本提交（**F1 入口**）
6. `routes/session.tsx`：终局 `action === "quit"` → 退出（与 `app.exit` 同路径）；`/tasks stop`、`/permissions` 的带 seed 预填（`seed` prop 已具备）
7. `app.tsx`：`connection.closed` 订阅 → 退出并提示（**I5** 前端侧）
8. `util/permission.ts`（或新 `util/command.ts`）：`/permissions` 裸命令的 seed 变体 = 预填 `/permissions `（用户接着打 auto/confirm/clear）

**不改**：Domain 层（`app/slash.py` 的词汇表、`domain/*`）、`handle_input` 的命令语义、IPC 方法表（除新增测试）。

## 6. 里程碑阶段设计

| 阶段 | 内容 | 验收（证据） |
|---|---|---|
| **A2.1 不变量**（S1） | `_install_providers` + finally 重装 + `/tasks stop` 前缀解析 + I1/I3 测试 | socket 级：`/permissions confirm` 后 WARN 审批仍走 `approval.request`；`/tasks stop <12位前缀>` 停得掉；pytest 绿 |
| **A2.2 退出收敛**（S1 尾） | `action=quit` 退出 + `connection.closed` 订阅 | E2E：`/quit` 命令 → 进程干净退出；后端 kill → 前端退出且提示 |
| **A2.3 文本命令面**（S2/S3） | `SlashCommand.command` 可选 + `select()` 文本提交 + 7 条目 | bun 单测（目录完整性 I8）；E2E 逐条：`/budget /trace /todo /memory /config /tasks /compact` 输出落 transcript，多行块可读、含 `[` 字符不崩（I7 复验） |
| **A2.4 带参与副作用**（S3） | `/permissions auto|confirm|clear` 与 `/tasks stop <id>` 的 E2E；`/compact` UI 决策落地 | E2E 三条决策路径 + 中途打断；I4 队列语义断言 |
| **A2.5 收口** | `docs/features.md` 命令表接线状态列、`opencode-gui-m6-plan.md` §9、master plan A2 行 | 文档 diff 自洽 |

**依赖关系**：A2.1 必须先行（`/permissions` 是 I1 的雷）；A2.3 依赖 A2.2 的退出路径（`/quit` 在 A2.3 才有入口）；A2.4 依赖 A2.1 + A2.3。

## 7. 实现成功后是什么样（验收口径）

用户视角：
1. `/` 列表 = 14 条真能跑的命令（现有 9 + 新 5 文本 + permissions），无一条选了没反应
2. `/budget`、`/trace`、`/memory`、`/config`、`/todo`、`/tasks`、`/compact` 的输出以 dim 行落在当前对话里，多行表格可读
3. `/permissions auto|confirm|clear` 生效，且切到 confirm 后**审批面板照常出现**（I1 的用户可见面）
4. `/quit` 从列表和手打两条路都干净退出（I5）
5. `/clear`、`/resume <id>` 切 tab；`/compact` 后 transcript **与服务端一致**（重投影）+ toast 报前后条数 + 队列不丢

系统视角：命令语义仍只有一处（`handle_input`），前端零命令解析；不变量 I1–I8 各有对应测试或 E2E 证据。

## 8. 开放决策（需拍板）

- **D1 `/compact` 后 transcript 怎么处理？——✅ 已定案（2026-10-10，按最新设计）**
  **从服务端重投影**：`session.load` → `messagesToState` → 整段替换 state；不保留压缩前的本地
  transcript。依据（本仓设计的既定契约，非臆造）：
  1. `replay.ts` 的定义就是"transcript = 持久化消息的投影"，hydrate 通道本就整段替换 state；
  2. `compact_now` 的契约是"重写会话"——会话文件即真相（`core/compaction.py:101`）；
  3. OpenCode 参考实现**没有** `/compact`（其 "compact" 命中全是标签宽度断点），无可抄先例；
  4. 旧 Textual 在 `/compact` 后不重渲染（只有 `/resume`/切会话 replay）——那正是随
     `ai_coding/ui/` 退休的**老旧设计**：本地 transcript 与会话文件各说各话。新设计不留这条分叉。
  被重投影抹掉的"[compact] messages X → Y"解释走 **toast**（M5 瞬态通道，扛得住 state 替换），
  不新增机制。配套契约：命令终局事件带 `"reload": true` 时前端必须重投影（§3 F3）。
- **D2 `/permissions` 的 UI 形态？** 纯文本命令（今天就能跑，A2.4 覆盖）vs 新增一个对话框（当前策略 + auto/confirm/clear + 已记住 grant 列表，可点）。文本零成本；对话框是新组件（约 100 行）。**推荐 A2 先文本，对话框记 A3**——`/permissions` 的价值在切换（文本可达），不在浏览。
- **D3 `/status` 与 DialogStatus 的关系？** 列表 `status` 继续指向对话框（结构化，含 provider/model/workspace），后端 `/status`（session id + 消息数 + memory）成影子、手打可达；或让 `/status` 走后端文本、对话框改名。**推荐保持现状**（对话框信息密度更高）。
---

## 9. 实施记录（A2 已落地，2026-10-10）

| 阶段 | 落地内容 | 证据 |
|---|---|---|
| **A2.1** 不变量 | `_wire_*` → 幂等 **`_install_providers`**（建表 + 每条命令 finally 各一次，目标取 `dispatcher.approval`）；`SubAgentRunner.resolve_run_id` + `/tasks stop` 前缀解析（歧义列候选）；假 loop 补落盘 | `test_permissions_confirm_keeps_approvals_on_the_wire`（修复前测试现场打出 `Allow? [y/N/a/s]`，即挂死形态）；`test_resolve_run_id_matches_a_unique_prefix`；`test_handle_tasks_stop_accepts_the_printed_prefix` |
| **A2.2** 退出收敛 | **`util/exit.ts::quitSequence`**（close → destroy → [message] → exit）；`app.exit`、`action="quit"`、`connection.closed` 三处共用；**顺带修掉既有隐患**：`process.exit` 单独用会把终端留在 raw mode（OpenTUI 的 `process.on("exit")` 只销毁 console cache，还原在 `renderer.destroy()` 里） | `test/exit.test.ts`（2 个顺序契约）；E2E：`/permissions confirm` → 审批面板照常 |
| **A2.3** 文本命令面 | `SlashCommand.command` 可选 + **`slashSelectionText`**（无 command → 提交 `/<name>`，prompt.select 分流）；目录 +8 条（budget/trace/todo/memory/config/tasks/compact/permissions）；`session.permissions` seed 预填（session + Shell 两层）；**修 seed 光标 bug**（setText 后光标在 0 位，补参数变成 `auto/permissions`，E2E 抓到） | `test/slash.test.ts` +3（I8 目录完整性、8 条可达、文本行一致）；E2E 逐条：`/budget`、`/trace`、`/todo`、`/memory`、`/config`（5 行多行块）、`/tasks`、`/permissions` seed + `confirm` |
| **A2.4** 带参与副作用 | 终局事件新增 `reload` / `messagesBefore` / `messagesAfter`（**按事实检测**：命令前后会话消息数变短，不嗅探命令文本）；前端 reload → `loadSession` 重投影 + 保留 `queued`（I9）+ toast 报前后条数（D1：解释不能随重投影被抹掉） | `test_compact_asks_the_client_to_re_project`、`test_a_query_command_asks_for_no_re_projection`；E2E：`/compact` 正常路径（历史未达 L4 阈值 → 正确不触发 reload） |
| **A2.5** 收口 | 见 §10 | — |

**已知偏差（诚实记录）**：I9 的"E2E：`/compact` 时队列非空 → 队列仍在"未做真机 E2E——`/compact` 只在历史超过 L4 阈值时缩短，真实会话里难以稳定构造。证据降级为：socket 级契约测试（`reload:true` + 前后条数）+ 前端消费逻辑的类型与代码审查。`reload:true` 时的重投影行为本身未被真机执行过。

## 10. 收口核验

- `pytest` 全量：见提交信息（A2 新增 8 个后端用例）
- `ruff`（触碰文件）✅、`mypy --strict` 116 files ✅、`bun test` 123 pass ✅、`tsc --noEmit` ✅
- E2E（tmux 140×42 真模型）：`/budget` `/trace` `/todo` `/memory` `/config` `/tasks` `/permissions confirm` `/compact` 全部执行并落 transcript；多行块可读；`/permissions confirm` 后审批面板照常（I1）
