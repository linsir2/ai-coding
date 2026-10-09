# OpenCode GUI 复刻方案（方案 B：TS+OpenTUI 同款）

> 分支：`feat/opencode-gui-replica`
> 状态：**技术选型 = 方案 B；已按"库优先"原则重新设计（v2）**
> 探索来源：`/home/linsir365/projects/opencode/packages/tui/`（源码）+ tmux 实机截图
> 工具链已验证：`@opentui/core@0.5.17` + `@opentui/solid` + `solid-js@1.9.17` + bun，最小渲染测试通过
> **v2 变更**：IPC/键位/Logo/搜索等全部换成熟第三方库（研究见第三部分，联调已通过）

---

## 第一部分：本项目功能全景（面向 GUI 设计）

### 1.1 对话与回合（Chat / Turn）

| 功能 | 说明 | 数据来源 | 建议 GUI 位置 |
|------|------|---------|--------------|
| 流式对话 | 模型文字流式输出，50ms 缓冲刷新 | `on_delta` 回调 | 转录区 assistant 行 |
| 推理过程 | `reasoning` 独立折叠，每轮一个 | `AssistantMessage.reasoning` | `Thinking/Thought` 折叠行 |
| 回合状态 | completed/max_iterations/timeout/budget/stopped/aborted/error | `TurnOutcome` | 行尾标记 + 红色 turn-end |
| Handoff 报告 | 无模型发言时 harness 写报告（目标/完成/失败/产物） | `render_handoff` | assistant 文案 |
| 输入队列 | turn 运行时输入排队自动执行 | `_enqueue` | prompt 上方 `N queued` |
| Esc 中断 | 杀子进程组 + 标记 ABORTED | cancel event | footer `esc interrupt` |
| 进度重试 | provider 重试/降级链 | `ProviderRetried/Fallback` | 暗淡 `⤷/⤬` 行 |
| 预算警告 | 步数/token 剩余 20% 时注入 | `budget_pressure` | 可显示在 footer |
| C3 自动续跑 | 预算死但任务未完 → 新轮 | `continuations` | 无需显示 |

### 1.2 工具系统（12 个内置 + MCP）

| 工具 | 权限 | 显示名建议 | 状态图标 |
|------|------|-----------|---------|
| bash | WARN+DENY | Shell / 运行命令 | `$` `✓/✗` |
| read | ALLOW | Read / 读取 | `→` |
| glob | ALLOW | Glob / 查找 | `✱` |
| write | WARN | Write / 写入 | `←` |
| edit | WARN | Edit / 编辑 | `←` + diff |
| todo_write | ALLOW | Todo / 计划 | `✓` |
| subAgent | ALLOW | Subagent / 委派 | `↳` |
| skill | ALLOW | Skill / 技能 | `→` |
| memory_search | ALLOW | Memory / 记忆搜索 | `✓` |
| memory_write | ALLOW | Memory / 记忆写入 | `✓` |
| checkpoint_write | ALLOW | Checkpoint | `✓` |
| ask_user | ALLOW | Ask / 提问 | `→` |
| MCP {server}__{tool} | WARN/ALLOW | 动态 | `✓/✗` |

工具卡三态：运行中（spinner+进行时文案）→ 完成（✓+摘要）→ 失败（✗+可展开错误）。
权限：ALLOW 直执 / WARN 弹审批（y/n/s/a）/ DENY 硬拒。

### 1.3 技能系统（Skills）

| 功能 | 说明 | 建议 GUI |
|------|------|---------|
| 技能目录 | `skills/<name>/SKILL.md`（frontmatter: description/version/status/phase/pattern_dna/triggers） | `/skills` 对话框（同 OpenCode DialogSkill） |
| 目录注入 | catalog 进静态 system prompt | 无需显示 |
| SOP 路由 | 四维评分推荐 top3 注入尾块 | 可在 sidebar 显示推荐理由 |
| 相位过滤 | planning/executing/debugging/reviewing | 状态标识 |
| 软/硬干预 | 错误匹配 → pattern_dna / 完整步骤注入 | tool 行样式（类似 notice） |
| 错误恢复 F1 | pattern_dna 回放 | 同上 |
| 动态定价 F1 | 贝叶斯价格 0.05-1.0，展示 `confidence: N%` | 推荐卡片上的置信度 |
| 后台技能审查 | 每 10 轮后台 LLM 提炼新技能 | 可加 toast 通知 |
| 共激活图 F1 | API 已实现未接线 | 待定 |
| 树突索引 F3 | 已实现但 dormant（未接 dendritic_path） | 待定，可接线后进 sidebar |

### 1.4 记忆系统（Memory）

| 功能 | 说明 | 建议 GUI |
|------|------|---------|
| 笔记 | kind(fact/decision/preference/open_thread), scene, importance, stake | Memory 对话框（列表+筛选） |
| 注入约束块 | pinned/decision/约束类 fact → `<memory-constraints>` | sidebar Context 卡（类似 OpenCode） |
| 检索 | BM25+IDF 门+可选语义，memory_search 工具 | 工具行 + 详情 |
| 收割/合并/淘汰 | 每轮 delta 抽取、相似合并、按价值淘汰 | 可加统计面板 |
| 质押 F1 | stake 0-10，成功×1.2 / 矛盾×0.7 | 笔记列表中的指示 |
| 统计 | total/active/pinned/by_kind/by_evidence/by_scene | `/memory` 对话框 |

### 1.5 子代理（Sub-agents）

| 功能 | 说明 | 建议 GUI |
|------|------|---------|
| 同步子代理 | 同 loop 子实例，全上下文或干净窗口 | tool 行 `↳` + 可跳转 |
| 后台子代理 | run id、跨轮监督、完成通知 | tool 行 + `/tasks` 列表 |
| worktree 隔离 | git worktree 沙箱 | 状态徽标 |
| 结构化输出 | output_schema 校验 | tool 详情 |
| 不确定性协议 F4 | notes/confidence + trace-field.md（保 100 条） | 详情 + trace 记录 |
| 包/账本 | `transcripts/subagents/<id>.json` | 跳转查看 |

### 1.6 任务与规划（Tasks / Planning）

| 功能 | 说明 | 建议 GUI |
|------|------|---------|
| Todo 列表 | pending/in_progress/completed/blocked/dropped + 依赖门 | 转录 plan 折叠（已有） + 可加 sidebar |
| `/plan` 运行器 | 计划→逐步执行→验证→回执 | 步骤折叠 `[plan] k/N` |
| 验收契约 | verify 命令 + fail-closed | 计划报告中 |
| 各门 | task-gate/strategy-gate/acceptance-gate/fatigue-gate F2 | 暗淡通知行 |
| 停滞检测 | 无成功调用 3 轮 → 零进展收尾 | turn-end 红色 |
| 疲劳 F2 | severity² 累积 ≥30 → 一次 nudge | 可显示疲劳值 |

### 1.7 会话（Session）

| 功能 | 说明 | 建议 GUI |
|------|------|---------|
| 持久化 | `sessions/<id>.json` 原子写 | 会话列表对话框 |
| 恢复 | `/resume` 前缀解析、重放 | Sessions 对话框（day 分组） |
| 回合摘要环 | working-memory 12 条 | 无需显示 |
| 用量 | turn_usages 行 | `/budget` |
| 重命名/删除 | 重命名有、删除仅 API | 对话框操作 |
| 临时会话 | plan-*/sub-* 不进列表 | 过滤 |

### 1.8 可观测性（Trace）

| 功能 | 说明 | 建议 GUI |
|------|------|---------|
| Trace JSONL | 每轮事件流 23 种 | `/trace` 对话框 |
| 摘要 | tokens/cache/步骤/耗时/降级 | `/trace` `/budget` |
| 导出 | HTML/OTLP（API 已有无 UI） | 导出对话框 |
| 跨会话统计 | aggregate API（无 CLI） | `/stats` 页（同 OpenCode stats） |

### 1.9 安全（Security）

| 功能 | 说明 | 建议 GUI |
|------|------|---------|
| 权限门 | ALLOW/WARN/DENY 三级 | 审批面板（同 OpenCode Permission） |
| Grant | `~/.config/ai-coding/grants.json` | `/permissions` 对话框 |
| 沙箱 | 路径校验 + 符号链接防逃逸 | 错误行 |
| 陈旧读保护 | mtime+size 快照 | 错误行 |
| 守卫 | duplicate/bash_policy/path_escape/secret | 拒绝行 |
| Hooks | 12 个钩子点 | `/hooks` 只读对话框（可选） |

### 1.10 命令与快捷键

**现有 17 个 slash 命令**：`/help /status /model /resume /permissions /todo /tasks /skills /plan /review /config /compact /memory /trace /budget /clear /quit`

**建议扩展**（对齐 OpenCode 命名习惯）：`/sessions /new /models /agents /mcps /themes /settings /themes /export /undo /redo /diff /stats`

**现有键位**：Ctrl-Q 退出 / Ctrl-C 退出 / Ctrl-Y 复制工具卡 / Esc 中断 / `Shift`拖选

### 1.11 后端已有但无 UI 的功能（GUI 机会清单）

1. Trace 跨会话总览、HTML/OTLP 导出
2. 会话删除、记忆笔记列表/pin/delete
3. 验收契约详情、不确定性账本详情
4. trace-field.md 笔记展示
5. MCP 服务器清单与连接状态（现在连了才有工具）
6. 预算按模型归因（现在只有文字）
7. 配置编辑（现在 /config 只显示 5 个值）
8. Steering 队列（后端就绪，UI 未接线）
9. F3 树突索引（接上 dendritic_path 即可启用）

---

## 第二部分：OpenCode TUI 设计分析（复刻目标）

### 2.1 技术栈

- TypeScript + SolidJS + `@opentui/core` + `@opentui/solid`（自研终端渲染引擎）
- Flexbox 布局、RGBA 颜色（支持 alpha 混合）、自定义 renderable（tab_pulse/shimmer/title_shimmer）
- 主题系统：token 解析（`$text.base`/`$hue.neutral.400`/`transparent`）+ `increase/decrease/tint`

### 2.2 三屏结构

| 屏 | 布局 |
|----|------|
| **Home** | 垂直居中：Logo（块字符画）→ 更新提示 → Prompt（宽≤75）→ footer 提示 |
| **Session** | 左：转录（flexGrow）+ 底部 Prompt；右：侧栏（42 宽）/ 终端 / 面板 |
| **Dialog** | 全屏遮罩 rgba(0,0,0,150/255) + 居中盒子（60/88/116 三档宽） |

### 2.3 核心视觉元素（从实机截图确认）

```
Logo（块字符）:
  █▀▀█ █▀▀█ █▀▀█ █▀▀▄ █▀▀▀ █▀▀█ █▀▀█ █▀▀█
  █  █ █  █ █▀▀▀ █  █ █    █  █ █  █ █▀▀▀
  ▀▀▀▀ █▀▀▀ ▀▀▀▀ ▀▀▀▀ ▀▀▀▀ ▀▀▀▀ ▀▀▀▀ ▀▀▀▀

Prompt（左框线 + 半块底）:
  ┃
  ┃  Ask anything… "Fix a TODO in the codebase"
  ┃
  ┃  Build · DeepSeek V4.1 Flash OpenCode Go · max
  ╹▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀

Dialog（居中 + 标题 + esc）:
                        Commands                             esc
                        Search
                        Suggested
                        Switch model                     ctrl+x m
                        Session
                        Switch session                   ctrl+x l

Footer 状态:
  ~/projects/opencode:v2                              1.18.32
  （上排）                                                    tab agents  ctrl+p commands
```

### 2.4 设计 token（dark 主题核心色）

| Token | 值 | 用途 |
|-------|-----|------|
| text.base | `#eeeeee` | 主文字 |
| text.muted | `#808080` | 次要文字 |
| background.base | `#0a0a0a` | 根背景 |
| background.raised.base | `#141414` | 卡片/对话框 |
| background.raised.high | `#1e1e1e` | 悬停/输入框 |
| action.primary.focused | `#fab283` | 选中/焦点（橙） |
| feedback.error | `#e06c75` | 错误（红） |
| feedback.warning | `#f5a742` | 警告（橙黄） |
| feedback.success | `#7fd88f` | 成功（绿） |
| feedback.info | `#56b6c2` | 信息（青） |
| border.base | `#484848` | 边框 |
| diff added/removed | `#4fd6be` / `#c53b53` | 差异 |

### 2.5 交互模型

- **命令系统**：命令 ID → 键位表；leader 模式（ctrl+x，2s 超时）；模式栈（base/modal/menu）；面板交互性隔离
- **Slash**：命令元数据 + 本地执行（带参）/ 服务端执行
- **对话框**：单栈 replace/clear；DialogSelect 统一列表模式（标题+esc、自动聚焦搜索、fuzzy 分组、● 当前项、footer 动作提示）
- **消息渲染状态机**：streaming→running→completed/error；每工具独立图标和文案；错误点击展开；被拒删除线
- **权限**：`△ Permission required` + 选项（Allow once/Always allow/Reject）+ ←/→ 选择
- **标签**：状态指示（spinner/! /? /•）+ 脉冲动画；横向条/纵向轨两种
- **状态表达**：glyph（! ? ✓ ✗ △ ◇ ● • ▪ ╹）+ 颜色 token + 每个长状态都有文字标签

---

## 第三部分：技术选型 v2（库优先，调研+实测完成）

> 原则：**能用成熟库就不手写**。v1 手写的 IPC / 键位 / Logo 全部替换为生态标准件。

### 3.0 库调研结论（关键项已双端联调）

| 关注点 | 选定库 | 版本 | 验证 | 替代掉的手写物 |
|--------|--------|------|------|----------------|
| TUI 渲染/组件 | `@opentui/core` + `@opentui/solid` | 0.5.17 | ✅ M0 跑通 | — |
| 响应式 | `solid-js` | 1.9.17 | ✅ | — |
| **键位/命令** | `@opentui/keymap`（`/addons` `/opentui` `/solid`） | 0.5.17 | 同生态/**OpenCode 同款**：分层优先级、leader 超时、多键序列、命令目录、focus 隔离 | 手写 `useKeyboard` 分支 |
| **IPC（TS）** | `vscode-jsonrpc` | 9.0.3 | ✅ 联调通过 | 手写 `ipc.ts` |
| **IPC（Python）** | `python-lsp-jsonrpc` | 1.1.2 | ✅ 联调通过 | 手写 `protocol.py`+`server.py`（~300 行） |
| 参数校验 | `zod`(TS) + `pydantic`(Py，已有) | 4.x / 2.x | — | — |
| **Logo** | `figlet`（`ANSI Compact` 字体） | 1.12.0 | ✅ 渲染确认（3 行块字符，风格贴近 OpenCode） | 手写 22 行字形表 |
| Spinner | `opentui-spinner` | 0.0.7 | OpenCode 生产在用 | — |
| 模糊搜索 | `fuzzysort` | 4.0.2 | OpenCode 生产在用 | — |
| CJK 宽度 | `string-width` + `get-east-asian-width` | 8.3.0 / 1.7.0 | 中文对齐刚需 | — |
| Diff 渲染 | `diff`（jsdiff） | 9.0.0 | — | — |

**联调证据**（`vscode-jsonrpc` ↔ `python-lsp-jsonrpc`，Unix socket）：
```
TS→Py 请求:  hello → {"msg":"world","echo":{"name":"ts"}}   ✅
Py→TS 通知:  tick 到达                                       ✅
帧协议:      双方默认 Content-Length 帧（LSP 风格），互认     ✅
```

### 3.1 架构 v2

```
┌──────────────────────────────────────────────────────────────┐
│  TS TUI 前端（frontend/，bun）                                │
│  - @opentui/core + solid 渲染（继承 tty）                      │
│  - @opentui/keymap：命令 ID + 键位 + leader + 模式层           │
│  - vscode-jsonrpc 客户端（Content-Length 帧，双向请求）        │
└───────────────┬──────────────────────────────────────────────┘
                │ Unix socket · JSON-RPC 2.0（Content-Length 帧）
                │ TS→Py: sendRequest / onNotification
                │ Py→TS: endpoint.notify / endpoint.request(审批)
┌───────────────▼──────────────────────────────────────────────┐
│  Python 后端（ai_coding/server/，与 AgentLoop 同进程）         │
│  - python-lsp-jsonrpc：Endpoint(dispatcher, writer.write)     │
│  - 读线程 JsonRpcStreamReader.listen(endpoint.consume)        │
│  - 业务复用 core/domain/memory/skills/tools/security 全部      │
└──────────────────────────────────────────────────────────────┘
```

**进程模型**（不变，`ai-coding` 唯一入口）：

```
$ ai-coding --path config.yaml --workspace . --skills ./skills
      │
      ▼ Python CLI（cli.py）
      1. 构建 world（现有代码不动）
      2. Unix socket 监听：/tmp/ai-coding-<pid>.sock
      3. spawn：bun frontend/src/index.tsx（继承 tty，env 传 socket 路径）
      4. 等待子进程退出，清理 socket
```

**线程模型 v2**（python-lsp-jsonrpc 是线程模型，与 asyncio 世界的桥接方式）：

```
主线程：asyncio loop（AgentLoop/world）
  ├─ 读线程：JsonRpcStreamReader.listen → Endpoint 分发 → ThreadPoolExecutor(max_workers=5)
  │     └─ handler（线程内）→ asyncio.run_coroutine_threadsafe(协程, 主loop) 调度业务
  ├─ 事件推送（主loop内）：endpoint.notify("event", ...)（writer 有 threading.Lock，线程安全）
  └─ 审批（主loop内）：await asyncio.wrap_future(endpoint.request("approval.request", ...))
```

**为什么保留 Unix socket**：TUI 需要真 tty；socket 保持终端干净、可用 `nc -U` 手测、为「服务端模式/重连」留路（对齐 OpenCode 的 client/server）。

### 3.2 IPC 协议 v2（方法/事件表不变，承载换成标准库）

方法/事件清单与 v1 相同（`initialize` / `session.*` / `model.set` / `skills.list` / `config.summary` / `shutdown`；事件 `turn.start` / `text.delta` / `reasoning` / `tool.*` / `task.updated` / `notice` / `turn.finish` / `memory.injected`）。变化只有两点：

1. **帧格式**：换行 JSON → Content-Length 帧（两库默认，互认已实测）
2. **审批往返**：v1 的"通知+回包" → 标准双向请求：
   - Python：`endpoint.request("approval.request", params)` → concurrent Future → `asyncio.wrap_future` 等待
   - TS：`connection.onRequest("approval.request", async (params) => decision)`
   - 超时 → 拒绝（fail-closed）
3. **参数校验**：Python 侧 pydantic 模型解析 params；TS 侧 zod schema（可选强化）

### 3.3 目录结构 v2

```
frontend/
├── package.json           # + vscode-jsonrpc / @opentui/keymap / figlet / fuzzysort
│                          #   / string-width / get-east-asian-width / diff / zod / opentui-spinner
├── tsconfig.json          # jsxImportSource: @opentui/solid
├── bunfig.toml            # preload = @opentui/solid/preload（必须，M0 踩坑记录）
├── src/
│   ├── index.tsx          # 入口：连接 socket → createMessageConnection → render(App)
│   ├── ipc.ts             # vscode-jsonrpc 封装（连接/重连/事件订阅/审批应答）
│   ├── keymap.ts          # @opentui/keymap：命令注册表（app.exit/session.*/input.*）
│   ├── state.tsx          # Solid stores
│   ├── theme/             # tokens.ts（抄 OpenCode 主题数据）+ provider
│   ├── ui/                # dialog / dialog-select / toast / border（基于 @opentui 原语）
│   ├── component/
│   │   ├── logo.tsx       # figlet 'ANSI Compact' 渲染 + 双色分割
│   │   ├── prompt.tsx     # 输入框（keymap 管理 submit 命令）
│   │   ├── message.tsx / tool.tsx / tabs.tsx
│   └── routes/
│       ├── home.tsx
│       └── session.tsx
└── test/                  # bun test + tmux 截图脚本
```

### 3.4 对 v1（M0 已交付）的替换清单

| v1 手写物 | v2 替换 | 动作 |
|-----------|---------|------|
| `frontend/src/ipc.ts`（70 行手写 JSON-RPC） | `vscode-jsonrpc` 封装 | 重写 |
| `ai_coding/server/protocol.py`（90 行） | 删除 | 删除 |
| `ai_coding/server/server.py`（180 行 IpcServer） | `python-lsp-jsonrpc` Endpoint | 删除，重写 service 层 |
| `frontend/src/component/logo.tsx`（字形表） | `figlet` | 重写（保留双色分割逻辑） |
| `app.tsx` 的 `useKeyboard` ctrl+c 分支 | `@opentui/keymap` 命令 | 重写 |
| prompt textarea `keyBindings` hack | keymap `input.submit` 命令（opentui addon 预制） | 替换 |
| 测试 `test_server_protocol.py` | 删除（协议归库） | 删除 |
| 测试 `test_server_ipc.py` | 改为库版 service 测试 | 重写 |

保留：`world.py`、`launcher.py`（骨架）、`handlers.py` 的业务处理、主题 token、Home/Session 布局。

### 附录 A：IPC 方法/事件表（协议参考）

**TS → Python 请求**：
| 方法 | 参数 | 返回 |
|------|------|------|
| `initialize` | `{}` | `{version, workspace, models[], current, session}` |
| `session.create` | `{title?}` | `{id}` |
| `session.list` | `{query?, limit?}` | `[{id,title,createdTime,lastAccessTime,preview,count}]` |
| `session.load` | `{id}` | `{messages:[...]}`（重放） |
| `session.prompt` | `{id, text, delivery}` | `{turnId}`（异步，事件跟进） |
| `session.interrupt` | `{id}` | `{ok}` |
| `session.command` | `{id, name, args}` | `{ok, output?}`（本地 slash） |
| `session.delete` | `{id}` | `{ok}` |
| `permission.reply` | `{requestId, decision, message?}` | `{ok}` |
| `model.set` | `{name}` | `{ok}` |
| `skills.list` | `{}` | `{catalog}` |
| `config.summary` | `{}` | `{model,maxContext,stopPolicy,...}` |
| `shutdown` | `{}` | `{ok}` |

**Python → TS 通知**（`event` 方法，`{type,data}`）：
| 事件 | 数据 | UI 落位 |
|------|------|---------|
| `turn.start` | `{turn}` | footer spinner |
| `text.delta` | `{text}` | 转录流式行 |
| `reasoning` | `{text, done}` | Thinking/Thought 折叠 |
| `tool.start` | `{callId, tool, paramsPreview}` | 工具行（运行中） |
| `tool.chunk` | `{callId, stream, text}` | 工具行实时输出 |
| `tool.finish` | `{callId, tool, success, outputPreview, error, durationMs}` | 工具行（完成/失败） |
| `task.updated` | `{task}` | 计划折叠 |
| `notice` | `{kind, text}` | 暗淡行（重试/降级/后台完成/队列） |
| `turn.finish` | `{status, text?, iterations, usage?}` | turn-end 标记 |
| `memory.injected` | `{count, chars}` | sidebar 更新 |
| `approval.request` | 请求（Py→TS 带 id）| 审批面板 |

**审批往返**：Python 的 `IpcApprovalCallback` 发送 `approval.request`（JSON-RPC 请求），等待 TS 的 `permission.reply` 响应；超时 → 拒绝（fail-closed）。

### 3.6 原「三选一」评估（存档）

| 方案 | 栈 | 保真度 | 工作量 | 结论 |
|------|-----|--------|--------|------|
| A. Textual 重写 | Python + Textual | 静态 90-95% | 8-11 天 | 落选 |
| **B. TS + OpenTUI 同款** | TS+SolidJS + Python IPC | **95%+** | 15-25 天 | **✅ 选中** |
| C. Web GUI | FastAPI + React | 100% | 20-30 天 | 产品形态偏移 |

---

## 第四部分：删除 / 保留 / 新建清单

### 4.1 删除（`git rm -r ai_coding/ui/`）

| 文件 | 行数 | 原因 |
|------|------|------|
| textual_app.py | 556 | 旧壳，重建 |
| transcript.py | 891 | 旧转录，重建 |
| blocks.py | 417 | 旧事件桥，重建 |
| modals.py | 229 | 旧模态，重建为 DialogSelect 体系 |
| status_bar.py | 59 | 重建为 footer |
| input_bar.py | 28 | 重建为 Prompt |
| resume_screen.py | 80 | 重建为 Sessions 对话框 |
| app.tcss | 225 | 重建为主题 TCSS |
| repl.py | 460 | 命令分发核心 → 迁移 |
| commands.py | 67 | 命令元数据 → 迁移 |
| slash.py | 115 | 解析 → 迁移 |
| console.py | 140 | 控制台适配 → 迁移 |
| plain.py | 66 | 文本安全 → 迁移 |
| trace_render.py | 316 | trace 渲染 → 保留逻辑，重建展示 |
| resume.py | 83 | 恢复逻辑 → 迁移 |

### 4.2 保留（后端，一行不动）

`core/` `domain/` `memory/` `skills/` `tools/` `security/` `service/` `ai/` `config/` `infra/`

### 4.3 迁移（UI 无关逻辑 → `ai_coding/app/`）

| 迁移项 | 去向 | 说明 |
|--------|------|------|
| SLASH_COMMANDS 元数据 | `app/commands.py` | 命令表（可扩展） |
| slash 解析/别名 | `app/slash.py` | 前缀解析 + 建议 |
| 命令分发 handle_input | `app/dispatch.py` | 返回动作枚举（NEW_SESSION/QUIT/RESUME/...） |
| trace 文本渲染 | `app/trace_text.py` | 对话框内容 |
| plain 文本安全 | `app/safe_text.py` | markup 安全（若仍用 Rich） |

### 4.4 新建（TS 前端 `frontend/`，方案 B）

| 模块 | 内容 | 库 | 对标 OpenCode |
|------|------|-----|---------------|
| `theme/tokens.ts` | 设计 token（dark/light） | —（数据抄 OpenCode 主题） | packages/theme |
| `app.tsx` | App 壳：路由（home/session）、遮罩层 | `@opentui/solid` | app.tsx |
| `keymap.ts` | 命令注册表（app.exit/session.*/input.*）+ leader | `@opentui/keymap` | config/keybind.ts |
| `ipc.ts` | 连接/事件订阅/审批应答 | `vscode-jsonrpc` | —（客户端） |
| `component/logo.tsx` | 双色 Logo | `figlet`（ANSI Compact） | component/logo.tsx |
| `component/prompt.tsx` | 左框线输入框 + 元数据行 + footer | `@opentui/keymap` 管理 submit | component/prompt/ |
| `component/tabs.tsx` | 会话标签（横向 + 状态指示） | `opentui-spinner` | component/session-tabs.tsx |
| `component/message.tsx` / `tool.tsx` | 用户/助手/推理/工具行状态机 | `diff`（编辑 diff）、`string-width`（CJK） | message-parts.tsx |
| `ui/dialog*.tsx` | Dialog 宿主 + DialogSelect（搜索） | `fuzzysort` | ui/dialog*.tsx |
| `ui/toast.tsx` | 右上角通知 | — | ui/toast.tsx |
| `state.tsx` | Solid stores | `solid-js` | —（服务端推送） |

### 4.5 新建（Python 服务层 `ai_coding/server/`）

| 模块 | 内容 | 状态 |
|------|------|------|
| `world.py` | 世界装配（config→engine→tools→loop） | ✅ M0 已有，保留 |
| `handlers.py` | 业务 handler 表（initialize/session.*/model.set/...） | ✅ M0 已有，改为库 dispatcher 注入 |
| `launcher.py` | 起 socket、spawn bun、清理 | ✅ M0 已有，传输层换库 |
| `service.py` | python-lsp-jsonrpc Endpoint 封装（读线程+线程池+asyncio 桥） | 🔜 M0.5 新建 |
| `protocol.py` / `server.py` | v1 手写 IPC | ❌ M0.5 删除 |

---

## 第五部分：实施阶段（方案 B，约 13-17 个专注日）

| 阶段 | 内容 | 交付物 | 验证 |
|------|------|--------|------|
| **M0** 骨架（IPC 往返）✅ | Python socket server + 启 bun；TS Home 屏（Logo+Prompt）；提交→echo 显示 | 端到端可跑 | ✅ tmux 实测 + 18 个 IPC 单测 |
| **M0.5** 库替换（v2） | IPC 换 `vscode-jsonrpc`↔`python-lsp-jsonrpc`；键位换 `@opentui/keymap`；Logo 换 `figlet`；依赖落地（fuzzysort/string-width/diff/zod/opentui-spinner） | 同样功能、零手写协议 | 联调脚本复跑 + 单测重写全绿 |
| **M1** 主题 + Home 完整 | token 表、placeholder 随机示例、footer（dir:branch + 版本 + 快捷键提示） | 完整首页 | 截图对比 OpenCode 首页 |
| **M2** 真实会话 | 接 AgentLoop 事件桥（经 keymap/ipc 封装）；消息/工具/推理/计划渲染；中断 | 完整对话 | 截图对比 + 事件桥测试 |
| **M3** 对话框系统 | Dialog 宿主 + DialogSelect（fuzzysort）+ palette/models/sessions/skills/status/help | Ctrl+P 全可用 | 截图对比 |
| **M4** 标签 + 侧栏 | session tabs（横向）+ sidebar（Context/MCP）+ 状态 | 多会话 | 截图对比 |
| **M5** 交互细节 | leader 键位、autocomplete（`@opentui/keymap` 层）、审批面板（ipc 双向请求）、toast、队列、两段式 esc | 全功能 | 端到端 + 截图 |
| **M6** 收尾 | Python/TS 测试补全、删除旧 Textual UI、文档、启动脚本 | 合并就绪 | `pytest`+`ruff`+`mypy`+`bun test` 全绿 |

### M0 实测截图（2026-10-09）

```
Home（复刻 OpenCode 首页）:
   █▀▀█ ▀█▀     █▀▀ █▀▀█ █▀▀▄ ▀█▀ █▄ █ █▀▀▀
   █▄▄█  █  ▀▀▀ █   █  █ █  █  █  █ ▀█ █ ▀█
   ▀  ▀ ▀▀▀     ▀▀▀ ▀▀▀▀ ▀▀▀▀ ▀▀▀ █  █ ▀▀▀▀
   ┃
   ┃  Ask anything… "What is the tech stack of this project?"
   ┃
   ┃  Build · qwen3.8-2.4t-a95b
   ╹▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀
                                                    ctrl+p commands
   /tmp/tui-ws                                             v0.1.0

Session（echo 往返）:
   ┃  hello world
      echo: hello world
   ┃
   ┃  Ask anything…
   ┃  Build · qwen3.8-2.4t-a95b
   ╹▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀
```

### M0 关键技术发现（防回退）

1. **`preload = ["@opentui/solid/preload"]` 必须**：bun 把 `solid-js` 解析到 `dist/server.js`（SSR 版），`createEffect/onMount` 全是 no-op；preload 插件把它重写成 `dist/solid.js` 客户端 runtime，否则 UI 渲染但无响应
2. **textarea 默认 `return = newline`**：v2 起由 `@opentui/keymap` 的 `input.submit` 命令接管（M0.5）；在此之前保持 `keyBindings` 覆盖
3. **`await render()` 挂载后立即返回**：不能在它之后关闭 IPC 连接
4. **焦点**：`onMount` 里 `input.focus()`（`queueMicrotask` 太早，渲染器未挂）
5. **v2 IPC 帧协议是 Content-Length（LSP 风格）**，不是行分隔 JSON；联调脚本 `vscode-jsonrpc ↔ python-lsp-jsonrpc` 已实测通过

### 验证方法（贯穿全程）

1. **tmux 截图对比**：同尺寸终端（140×42）跑 OpenCode 和本项目，同场景截图对比
2. **Python 侧**：`pytest` 覆盖协议层（server/handlers/bridge/approval callback）
3. **TS 侧**：`bun test` 覆盖纯逻辑（ipc 解析/token/keymap/选择数学）
4. **不回归**：后端 165+ 测试文件保持全绿
5. **删除时机**：旧 `ai_coding/ui/` 在 M2 完成（新前端可独立跑通会话）后删除，避免中途不可用

---

## 第六部分：功能 → 界面映射表（GUI 设计对照）

| 本项目功能 | OpenCode 对应模式 | 落位 |
|-----------|------------------|------|
| 流式对话 | Session 转录 | assistant 行（markdown） |
| 推理过程 | Thinking/Thought 折叠 | 转录行 |
| 工具调用 | 工具行状态机 | 转录行（图标+标签） |
| 工具审批 | Permission panel | Prompt 位替换 |
| Plan/Todo | plan 折叠 | 转录行 |
| 技能 | DialogSkill | `/skills` |
| 技能推荐/干预 | 无直接等价 | sidebar 卡 + notice 行 |
| 记忆 | Sidebar Context 卡 | sidebar + `/memory` |
| 子代理 | Subagent 行 + 跳转 | 转录行 + `/tasks` |
| Sessions | DialogSessionList | `/sessions` |
| Models | DialogModel | `/models` |
| Trace/Budget | DialogStatus/Stats | `/trace` `/budget` `/stats` |
| Permissions/grants | DialogIntegration 式管理 | `/permissions` |
| MCP | DialogMcp | `/mcps` |
| 命令面板 | Command palette | `Ctrl+P` |
| 中断 | esc interrupt 两段式 | footer |
| 队列 | Queued prompt dock | Prompt 上方 |

---

## 待拍板

**技术选型**：方案 A（Textual 重写，推荐）/ B（TS+OpenTUI）/ C（Web GUI）
请确认后开始 P0。
