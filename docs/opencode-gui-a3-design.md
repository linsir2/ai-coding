# A3：permissions 对话框 / trace 跨会话总览+导出 / 会话删除（设计）

> 基线：A2（`2045b61`）把命令层整体接通并补齐了不变量（I1 provider 重装、I5 退出收敛、I8 目录契约、D1 reload 重投影）。
> A3 三项的共同形态：**能力已存在于后端，缺的是"入口"与"守卫"**——不新建业务逻辑，只做接线与不变量。

---

## 1. `/permissions` 对话框（D2 决议：对话框形态）

### 事实
- 当前策略 = `loop.dispatcher.approval` 的类型（`CLIApprovalCallback` = confirm-all / `AutoApproveCallback` = auto）。
- 已记住 grant = `gate.grants`（元组，`write:<normpath>` / `edit:<normpath>` 形态；非路径工具"always"不可表述故不记）。
- 撤销 = `gate.revoke_all()`，**内存 + 磁盘（GrantStore.clear）一起清**。
- 文本命令 `/permissions [auto|confirm|clear]` A2 已通；`/permissions confirm` 会换新回调，I1 保证 provider 重装。

### 设计
- **读**（新增只读方法）：`permissions.state` → `{policy: "confirm"|"auto", grants: [...], storePath}`。不新增变更路径。
- **写**：对话框按钮**通过既有命令通道**派发 `/permissions auto|confirm|clear`（`session.command`）。理由（A1 原则）：策略切换只有命令层一处实现，I1 的 provider 重装在 `run_command` 的 finally 里天然生效；新写一个 `permissions.set` handler 会造出第二条变更路径、还得重复那份重装逻辑。
- 忙时行为：按钮点击走既有队列（busy → 排队），对话框关闭；执行结果以 transcript 通知可见。

### 不变量
- **J1**：`permissions.state` 的 policy 判定只认回调类型（`AutoApproveCallback` → auto，其余 → confirm）；不得新增第四种策略。
- **J2**：切换策略必须经命令通道（provider 重装唯一落点在 `run_command.finally`）。
- **J3**：`clear` 后 `permissions.state` 的 grants 为空且磁盘已清（`revoke_all` 的既有语义，测试钉住）。

## 2. trace 跨会话总览 + HTML/OTLP 导出

### 事实
- `build_report(store, since_days=0)` → `AggregateReport(rows, totals)`：每会话 turns/files/steps/prompt/completion/cached/age_days + `mismatch`/`cache_ratio`。
- `read_session_view(store, id)` → `SessionView`；`render_index_html(report, views)` 单页内联全部会话（totals KPI + 每会话页）；`render_session_html(view)` 单会话页；`rows_to_otlp(views)` → OTLP/JSON dict（无网络）。**全是纯函数，20 个测试，生产零调用方。**
- trace 落盘根：`<workspace>/.ai-coding/traces/<session_id>/`（summary.json + 每回合 jsonl）。

### 设计（走命令层，零新前端 UI）
- **`/stats`**：跨会话总览 → `console.info` 渲染表（会话数/回合/步数/token/cache%/最旧）。文本命令，A2 目录加一条即达。
- **`/export [html|otlp] [session-id]`**：落盘到 `<workspace>/.ai-coding/exports/`：
  - `html`（无 id）→ `render_index_html` 单文件；有 id → `render_session_html` 单会话页。
  - `otlp` → `rows_to_otlp` → JSON 文件。
  - 输出 `console.info` 报告路径；无 trace 时如实说"无数据"。
- 不做 TUI 内 trace 浏览器（超出范围；导出文件是可打开的产物）。

### 不变量
- **J4**：导出落盘路径固定在 workspace 内（`<workspace>/.ai-coding/exports/`），不接受任意路径参数（无路径穿越面）。
- **J5**：`/export` 是纯读+写文件，不改会话、不删数据；渲染失败如实报错（不吞异常）。

## 3. 会话删除

### 事实
- `session.delete {id}` → `SessionService.delete` → unlink 会话文件。
- 现 handler **不切 `state.session`**：删掉当前会话后 live session 指向已删文件，下回合持久化会把它重建（脏）。
- `DialogSelect` 选项有 `current` 标记与 `onSelect` 钩子；对话框可在忙时打开（app.tsx:241 不看 busy）。

### 设计
- **后端**：`session.delete` 删的是当前 live session 时，同步 `state.session = sessions.create(); state.session_id = …`，返回 `{ok, sessionId}`（切到新会话，对齐 `/clear` 的 NEW_SESSION 语义）。
- **前端**：会话对话框每行加删除入口（`ctrl+d` 作用于当前选中行）→ **二次确认面板**（复用 PermissionPanel 的视觉：删除/取消），确认后调 `session.delete`；成功则刷新列表，若返回新 `sessionId` 则切 tab；当前会话被删时自动落到新会话。
- **trace 目录**：默认**不删**（trace 是审计/复盘资产，与会话文件分离；`TraceStore` 无删除 API，删除即新增领域能力）。清单里记录为后续可选项。

### 不变量
- **J6**：删当前会话必切新会话（handler 保证，不依赖前端）。
- **J7**：删除必二次确认（前端强制；无后端"强删"旁路）。
- **J8**：删除只删会话文件，不动 trace/记忆等派生资产（与现状一致，明确记录）。
- **J9**：`session.delete` 的 `ok` 分三态——删掉了 / 本来就没有文件（`true`，它已经不存在了）/ 未知 id（`false`，让客户端知道该刷新列表）。`SessionService.delete` 只 unlink，所以"live 但还没跑过 turn"的会话没有文件，只能靠 `was_live` 判定。
- **J10**：删掉的若是 live 会话，`sessionId` 回填新的 live id，前端必须跟随切换，否则下一轮会把内容写回已删的文件里。

## 4. 模块清单

**后端**
1. `server/handlers.py`：新增只读 `permissions.state`；`session.delete` 切 live session（返回新 id）。
2. `app/commands.py`：新增 `/stats`、`/export` 两个命令分支（复用既有纯函数 + 一个 `_export_dir` 助手）。
3. 测试：`test_server_ipc.py`（permissions.state 形状、删当前会话返回新 id）、`test_repl.py`（`/stats`、`/export` 输出与落盘路径、不变 J4）。

**前端**
4. `component/dialog-status.tsx`（**改造，不新建**）：权限区块并入既有状态/配置面板——策略、grants、storePath，`p` 切策略、`c` 清 grants，两者都派发 `session.command`。依据：OpenCode 的权限设置住在 `dialog-config.tsx`（`prompt`/`autoaccept` 两个值），本项目的对位就是 `dialog-status.tsx`，另起一个对话框是重复造轮子。
5. `component/dialog-select.tsx`：新增可选 `actions`（对位 OpenCode 的 `actions`/`footerHints`）与 `SelectOption.danger`，供列表行动作复用。
6. `component/dialog-sessions.tsx`：`ctrl+d` 武装式删除（首按把行标题改成 "press ctrl+d again to confirm" 并染红，第二按才真删），成功后原地移除该行。
7. `util/permissions-dialog.ts`（新）：权限纯逻辑（策略文案、grant 行、切换命令），供面板与测试使用。
8. `util/slash.ts`：`/stats`、`/export` 两条文本命令入目录。
9. `app.tsx`：`DialogSessions` 接 `onDeleted`，任何删除都刷新会话列表；删的是 live 会话则切到服务端返回的新 id 并关闭对话框。
10. 测试：`util/permissions-dialog.ts` 的纯逻辑走 bun；删除与切换由 socket 测试兜底。

## 5. 里程碑

| 阶段 | 内容 | 验收 |
|---|---|---|
| **A3.1 permissions 并入状态面板** | `permissions.state` 只读方法 + `dialog-status` 权限区块（读）+ 键派发命令（写） | socket 测试（形状 + J1）；E2E：面板改策略 → transcript 确认 → 审批面板按新策略表现 |
| **A3.2 trace 统计与导出** | `/stats`、`/export html\|otlp [id]` + 导出目录助手 | 单测（落盘路径在 workspace 内、HTML/OTLP 产物可读、无 trace 时的措辞）；E2E 两条命令出文件 |
| **A3.3 会话删除** | handler 切 live session + 前端 `ctrl+d` 二次确认 | socket 测试（删当前会话 → 新 sessionId）；E2E：删除另一会话 / 删除当前会话两条路径 |
| **A3.4 收口** | 文档接线状态 + 全量核验 + 提交 | 全绿 |

依赖：A3.1 与 A3.2 无相互依赖，可并行；A3.3 依赖 A3.1 的对话框模式（确认面板复用其视觉）。

## 6. 待拍板

- **D4 `/export` 的落盘目录**：`<workspace>/.ai-coding/exports/`（推荐，与 traces 同级、随工作区走）还是用户 home？推荐前者：不污染 home、随工作区归档。
- **D5 会话删除是否连带删 trace 目录**：默认不删（推荐，trace 是审计资产；`TraceStore` 也无删除 API，删它等于新增领域能力）。若要"删干净"，需给 TraceStore 加删除并在同一次操作里调用——单独立项。
- **D6 `/stats` 与 `/export` 的入口形态**：文本命令（A2 模式，零新 UI，推荐）还是也做对话框？推荐文本——导出产物是文件，对话框不如命令顺手。