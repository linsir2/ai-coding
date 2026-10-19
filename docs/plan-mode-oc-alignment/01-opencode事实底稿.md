# 01 · opencode plan 模式 / plan agent 机制盘点（事实底稿）

> 修订：本稿已被 `07-重审-opencode事实底稿.md` 取代（2026-10-19 重审），事实以重审版为准。

> 状态：**已落档**（2026-10-19）。源：`/home/linsir365/projects/opencode` v2 @ `0a46301e3`。

> 只读调研。所有结论附 `file:line`；`[verified]` = 代码直接证明，`[inferred]` = 推断并给理由。
> 供与 ai-coding（Python）侧报告逐条对比。文中不含任何改造建议。

## 0. 版本与覆盖范围

- 仓库：`/home/linsir365/projects/opencode`（fork 自 `https://github.com/anomalyco/opencode.git`，分支 `v2`）
- commit：`0a46301e3` "chore(core): refresh bundled models-dev snapshot"（2026-10-04）[verified: `git log -1`]
- 工作区存在他人的未提交改动（`packages/core/src/models-dev/snapshot.txt`、`packages/plugin/src/host.ts` 有修改；另有未跟踪的 `packages/cli/nexus.db`、`research/`）。本次调研未触碰任何仓库文件。
- 重要架构说明：该分支是 opencode **v2 架构**，与传统文档中的 v1 差异很大。plan 不是静态内置 agent 表里的一条数据，而是由 core 内置 Effect 插件 `opencode.plan` 动态定义 [verified: `packages/core/src/plugin/internal.ts:221` 将 `PlanPlugin.Plugin` 列入 pre 插件；`packages/core/src/plugin/plan.ts`]。
- 主要阅读范围（只读）：
  - `packages/core/src/`：`agent.ts`、`permission.ts`、`tool.ts`、`tool/plugin/*`、`config.ts`、`config/plugin/{agent,command,compatibility}.ts`、`plugin/{agent,plan,optimize,internal,command}.ts`、`session*`（`session.ts`、`session/session.ts`、`context.ts`、`model-request.ts`、`runner/*`、`message-updater.ts`、`projector.ts`、`sql.ts`、`revert.ts`）、`file-access.ts`、`util/wildcard.ts`、`v1/config/migrate.ts`
  - `packages/schema/src/`：`agent.ts`、`permission.ts`、`session.ts`、`session-message.ts`、`session-event.ts`、`config.ts`、`config/agent.ts`、`tool.ts`（引用）
  - `packages/tui/src/`：`app.tsx`、`context/local.tsx`、`component/prompt/*`、`component/dialog-agent.tsx`、`routes/session/index.tsx`、`config/keybind.ts`、`feature-plugins/prompt/footer.tsx`
  - `packages/cli/src/`：`commands/commands.ts`、`run/noninteractive.ts`；`packages/server/src/handlers/{agent,session}.ts`；`packages/session-ui/src/timeline/*`、`tools/tool-renderer.tsx`
- 跳过：`node_modules`、dist/build、`*.gen.ts`、测试实现、与 plan 无关的 provider/plugin 细节；未运行测试、未构建。
- 版本差异提示：本分支中 `todowrite`/`todoread` 工具已不存在（v1→v2 迁移删除）[verified: `packages/core/src/database/v1-migration.bun.ts:901` `const REMOVED_TOOLS = ["todowrite"]`；内置工具插件目录 `packages/core/src/tool/plugin/` 无 todo 文件]，但 UI 仍保留对历史 `todowrite` 消息的渲染 [verified: `packages/session-ui/src/tools/tool-renderer.tsx:389`、`1313`]。

## 1. 功能清单

| F-ID | 功能/行为 | 触发方式 | 代码位置(file:line) | 行为细节 | 状态 |
|---|---|---|---|---|---|
| F01 | 内置 agent `plan`（primary，只读定位）由插件定义 | 启动时插件注册 | `packages/core/src/plugin/plan.ts:33-43` | `editor.update(plan, ...)`：name=Plan、description="Read-only agent for exploring the codebase and planning work before implementation."、mode=primary，push 4 条权限 | 实现 |
| F02 | 内置 agent 清单：`build`(默认)/`general`/`explore`/`compaction`/`title`/`summary` | 插件注册 | `packages/core/src/plugin/agent.ts:86-153` | build=Agent.defaultID（primary）；general、explore=subagent；compaction/title/summary=primary+hidden | 实现 |
| F03 | agent 默认权限模板（每个 agent 创建时注入） | 任意 `editor.update` 首次创建时 | `packages/schema/src/agent.ts:39-53` | `* * allow`、`external_directory * ask`、`.env` 读 ask 等 5 条 | 实现 |
| F04 | 全局追加 external_directory allow（shell 输出/tool 输出/tmp/config 目录） | agent 创建时 | `packages/core/src/agent.ts:59-64` | 允许读全局产物目录 | 实现 |
| F05 | 自定义 agent 加载（.md 文件） | 启动/文件变更 | `packages/core/src/config/plugin/agent.ts:21-26,164-212` | 扫描每个配置目录下 `{agent,agents}/**/*.md`（primary:false）与 `{mode,modes}/*.md`（primary:true）；md 正文=system prompt；文件名=agent id | 实现 |
| F06 | 配置内 agent 合并（`agents` map） | 启动/变更 | `packages/core/src/config/plugin/agent.ts:94-127` | 逐文档 `editor.update`：标量字段后写覆盖前写；`permissions` 是 push 累积（last-match-wins） | 实现 |
| F07 | `default_agent` 配置生效 | 配置变更 | `packages/core/src/config/plugin/agent.ts:88-89` | `Config.latest(documents,"default_agent")` → `editor.default(id)` | 实现 |
| F08 | agent 选择解析（无 id 时的 fallback） | 每次上下文选择 | `packages/core/src/agent.ts:92-104,116-123` | `selectable`=mode!=="subagent" && !hidden；优先 default → build → 任意 primary | 实现 |
| F09 | TUI 进入/切换模式：快捷键循环 | `shift+tab`（可配置） | `packages/tui/src/config/keybind.ts:172-173`；`packages/tui/src/app.tsx:903-912,937-945` | `agent.cycle` → `local.agent.move(1)`（在 primary 且非 hidden 的 agent 间循环，含 build/plan） | 实现 |
| F10 | TUI `/agents` 对话框选择 | `/agents` 命令 | `packages/tui/src/app.tsx:885-894`；`packages/tui/src/component/dialog-agent.tsx:1-26` | 列出本地 agent，选择写 draft | 实现 |
| F11 | 切换只做「草稿」，提交消息时才持久化 | 输入框提交 | `packages/tui/src/component/prompt/index.tsx:1274-1282,1330` | `prepareAgent()` 比较 `session.agent !== agent.id` 后调 `session.switchAgent`；空回车也提交（commitSelection） | 实现 |
| F12 | CLI `--agent` 指定 agent | `opencode --agent X` / `opencode run --agent X` | `packages/cli/src/commands/commands.ts:371,403`；`packages/cli/src/run/noninteractive.ts:690-691` | run 模式先 `switchAgent` 再提示；TUI 模式作为初始 draft | 实现 |
| F13 | 会话级 agent 状态（session.agent 字段） | 每次 switch | `packages/schema/src/session.ts:39`；`packages/core/src/session/projector.ts:546-557`；`packages/core/src/session/sql.ts:57` | AgentSelected 事件投影更新 `session_v2.agent` | 实现 |
| F14 | 会话内 agent 切换记录（消息级） | 每次 switch | `packages/schema/src/session-message.ts:43-49`；`packages/core/src/session/message-updater.ts:91-101` | 追加 `type:"agent-switched"` 消息（agent + previous） | 实现 |
| F15 | 每条 assistant 消息记录 agentID | 每个助手回合 | `packages/schema/src/session-message.ts:212-216` | `Assistant.agent: Agent.ID` | 实现 |
| F16 | agent 切换事件（durable，走 session log） | switchAgent | `packages/schema/src/session-event.ts:69-78`；`packages/core/src/session/session.ts:89-95` | 事件 `session.agent.selected`，durable aggregate=sessionID | 实现 |
| F17 | plan 模式进入提醒注入（system-reminder） | 会话创建/切到 plan | `packages/core/src/plugin/plan.ts:14-21,76-97,101-113` | 发 `synthetic` 消息，内容为 `<system-reminder>You are in Plan mode...</system-reminder>`；含「不要实现、要求用户切换 agent」指令 | 实现 |
| F18 | plan 模式退出提醒注入 | 从 plan 切到别的 agent | `packages/core/src/plugin/plan.ts:23-25,110-111` | `<system-reminder>You are NO LONGER in Plan mode...</system-reminder>` | 实现 |
| F19 | 每请求补挂提醒（压缩/revert 后丢失的自愈） | 每次模型请求 context hook | `packages/core/src/plugin/plan.ts:56-74,115-121` | 检查请求消息里是否已有 enter/leave 文本：缺 enter 则补 enter、出现陈旧 enter 则补 leave；插入位置在最后一条 user 消息前 | 实现 |
| F20 | plan 权限：edit 全禁 + plan 目录例外 | 权限求值 | `packages/core/src/plugin/plan.ts:38-41` | `edit * deny`、`edit ~/.opencode/plan/* allow`、`external_directory ~/.opencode/plan/* allow` | 实现 |
| F21 | plan 目录约定 | 提示词约定（无自动创建代码） | `packages/core/src/plugin/plan.ts:31` | `path.join(global.home, ".opencode", "plan")`；仅写文件时按需创建父目录（write 工具行为）| 实现 |
| F22 | plan 下 edit/write/patch 被拒的模型可见文案 | 工具执行时 | `packages/core/src/plugin/plan.ts:45-54` | 把 `Permission.BlockedError` 包出的 ToolFailure 替换为 `Cannot use {tool} to modify files outside the Plan directory: {dir}` | 实现 |
| F23 | 权限求值语义：findLast + 通配 + 默认 ask | 任意 assert | `packages/core/src/permission.ts:87-97`；`packages/core/src/util/wildcard.ts:3-13` | 规则顺序即优先级；`*`匹配任意（含跨路径段）；无匹配默认 `ask` | 实现 |
| F24 | 权限来源：agent.permissions + session.permissions + saved 规则 | 任意 assert | `packages/core/src/permission.ts:148-163,173-189` | session 权限在 agent 之后求值（优先级更高）；deny 短路；saved 规则最后叠加 | 实现 |
| F25 | 权限 ask → 用户问答（once/always/reject） | 工具内部 assert | `packages/core/src/permission.ts:223-229,231-263,265-325` | ask 挂起等待；reject 带 message → CorrectedError（模型可见反馈）；纯 reject → DeclinedError 缺陷隧道，整步打断 | 实现 |
| F26 | 子 agent（subagent 工具）权限门 | subagent 工具执行 | `packages/core/src/tool/plugin/subagent.ts:138-151` | 对父会话按 action=`subagent`、resource=子 agent.id 做 assert；plan 未 deny 该 action → 允许 | 实现 |
| F27 | 子会话权限继承 | 创建/continue 子会话 | `packages/core/src/tool/plugin/subagent.ts:185-198`；`packages/core/src/session.ts:284-288` | 子会话继承父会话的 **session.permissions**（不是父 agent 的权限集）；子会话权限求值用自己的 agent | 实现 |
| F28 | plan 目录外写文件即便绕过 permission 也不可 | FileAccess external 边界 | `packages/core/src/file-access.ts:91-116,122-141` | 项目外路径先走 `external_directory` 检查（plan 默认 ask，plan 目录 allow） | 实现 |
| F29 | 工具目录裁剪（catalog visibility） | 每次 context select | `packages/core/src/tool.ts:225-235,292-295`；`packages/core/src/session/context.ts:128-134` | 仅当某 action 的**最后一条**规则是 `resource:"*" effect:"deny"` 时整个工具从目录移除；plan 的 edit 因存在更后的 allow 规则，edit/write/patch 仍出现在目录里，靠执行期 deny | 实现 |
| F30 | 工具注册/枚举 | 启动插件注册 | `packages/core/src/tool.ts:158-219`；`packages/core/src/plugin/internal.ts:228-245` | 内置直连工具：patch/edit/write/read/glob/grep/shell/webfetch/websearch/skill/question/subagent + codemode `execute` + opencode.* / MCP 工具 | 实现 |
| F31 | v1 `tools` 配置（禁用工具）迁移为权限规则 | 加载 v1 配置时 | `packages/core/src/v1/config/agent.ts:50-59`（agent.tools→permission，write/edit/patch→edit）；`packages/core/src/config/normalize.ts:486-497`（顶层 tools→permission 规则）；`packages/core/src/v1/config/migrate.ts:27-33`（action 改名：write/patch→edit、task→subagent、bash→shell） | v2 agent schema 无 tools 字段；禁用工具=写 `deny` 规则 | 实现 |
| F32 | 系统提示词来源 | 每次请求 | `packages/core/src/session/model-request.ts:100-120`；`packages/core/src/session/runner/prompt/system.txt:1-15`；`packages/core/src/session/system-prompt.ts:5-27` | `agent.system` 优先；否则内置 system.txt + 按可用工具增减指导语；再被 OptimizePlugin 按模型替换/追加 | 实现 |
| F33 | 模型适配提示词覆盖 | 每次请求 | `packages/core/src/plugin/optimize.ts:72-91` | 匹配 gpt/claude/kimi/trinity/muse 时替换 system[0]；agent 有自定义 system 则跳过替换 | 实现 |
| F34 | plan agent 无专属系统提示词 | — | `packages/core/src/plugin/plan.ts`（无 item.system 赋值）| plan 与 build 共用基础 system prompt，区别完全来自 reminder + 权限 | 实现（无=事实） |
| F35 | plan → build 交接到人工 | 用户切 agent | `packages/core/src/plugin/plan.ts:20` | 提醒文本明确：要求用户自行切换 agent；无任何自动切换/批准代码 | 实现（人工交接） |
| F36 | 「计划批准」状态 | — | `packages/schema/src/session-message.ts:295-320`（消息 union 无 plan/approve 类型）；`packages/schema/src/session-event.ts`（事件无 approve） | 不存在批准概念；计划就是普通聊天回答 | 无 |
| F37 | todo/todowrite 工具 | — | `packages/core/src/database/v1-migration.bun.ts:901` | v2 已删除；plan 下自然也没有 | 无（已移除） |
| F38 | plan 文件磁盘产物 | 模型被提醒可写 | `packages/core/src/plugin/plan.ts:15-16` | 无自动生成；仅提示模型「用户明确要求时」写入 `~/.opencode/plan` | 部分（纯提示词约定） |
| F39 | fork 行为 | 会话 fork | `packages/core/src/session/projector.ts:146-215` | fork 复制父会话 agent 字段与边界前消息（含 agent-switched/提醒） | 实现 |
| F40 | revert 行为 | revert stage/commit | `packages/core/src/session/revert.ts:32-88`；`packages/core/src/plugin/plan.ts:56-58`（注释） | revert 不回滚 agent 字段；提醒丢失由 F19 每请求自愈 | 实现 |
| F41 | 斜杠命令携带 `agent:` 可切换模式 | 执行命令 | `packages/core/src/config/plugin/command.ts:124-128` | 命令声明 agent 时先 `switchAgent` 再发 prompt（subagent 模式则改为起子会话） | 实现 |
| F42 | 权限询问/结果事件（非持久化） | permission ask/reply | `packages/schema/src/permission.ts:44-53` | `permission.asked/replied` 为 ephemeral 事件 | 实现 |
| F43 | 「always」保存权限规则（按项目） | 用户选择 always | `packages/core/src/permission.ts:299-305`；`packages/core/src/permission/saved.ts`；`packages/core/src/permission/sql.ts` | 保存到项目级 permission 表，后续叠加在求值末尾 | 实现 |
| F44 | CLI 非交互 --auto 自动批准 | `opencode run --auto` | `packages/cli/src/commands/commands.ts:28-31`；`packages/cli/src/run/noninteractive.ts:164-183` | 对非显式 deny 的 ask 自动回 once；不改变 plan 的 deny | 实现 |
| F45 | agent 切换的按消息归属展示 | TUI/session-ui 渲染 | `packages/session-ui/src/timeline/projection.ts:507`；`packages/tui/src/routes/session/index.tsx:1917-1925` | `Switched agent from X to Y` 旁注；assistant 消息按最近 agent 归属 | 实现 |
| F46 | agent 列表/单个查询 API | HTTP | `packages/server/src/handlers/agent.ts:8-25` | `agent.list` / `agent.get`；agent 服务为 Location 作用域 | 实现 |
| F47 | agent 更新事件广播 | 配置热更新 | `packages/schema/src/agent.ts:11,57-60`；`packages/core/src/agent.ts:90` | `agent.updated` ephemeral 事件 → TUI 刷新 agent 列表 | 实现 |
| F48 | plan 下读敏感文件（.env）仍要 ask | read 工具 | `packages/schema/src/agent.ts:49-51`；`packages/core/src/file-access.ts:143-159` | plan 继承默认 `.env` ask 规则；question 工具在 plan 为 allow | 实现 |
| F49 | plan 下 shell 未被禁止 | shell 工具 | `packages/core/src/tool/plugin/shell.ts:135-140`；plan 权限无 shell deny | shell action 默认 allow；「只读」不是沙箱保证 | 实现（事实） |
| F50 | plan 下可调用 subagent | subagent 工具 | `packages/core/src/tool/plugin/subagent.ts:134-151` | 受提示词劝阻（F17「不要指示子 agent 改文件」）但无权限硬拒 | 实现（软约束） |

### 问题索引（Q1–Q10 → 对应 F/章节）

- Q1 plan 是什么 → F01-F08、§3、§4
- Q2 进入/切换/退出、状态存储 → F09-F16、§2、§4
- Q3 权限模型与强制点 → F20、F23-F29、F43、§5
- Q4 工具面裁剪 → F29-F31、§5.3
- Q5 计划产物与审批 → F17、F21、F36、F38
- Q6 执行交接 / todo / 批准 → F35-F37、§7
- Q7 prompt 结构 → F17-F19、F32-F34、§6
- Q8 持久化 → F13-F16、F39、F40、§4
- Q9 UX → F09-F12、F45-F47、§1 + §4
- Q10 被禁行为运行时形态 → F22、F25、§6.6

## 2. 数据流

### 2a. 阶段表

| S-ID | 阶段 | 触发条件 | 输入 | 处理(file:line) | 状态变化 | 持久化 | 用户可见效果 |
|---|---|---|---|---|---|---|---|
| S1 | 插件注册内置 agent | 进程/位置启动 | 无 | `plugin/internal.ts:213-247` → `plugin/agent.ts:86-153`、`plugin/plan.ts:33-43` | 内存 agent 表（Agent.State） | 无（每次启动重建） | 无 |
| S2 | 加载自定义 agent | 启动 + 配置目录/文件变化 | `.md` 文件 / `opencode.jsonc` | `config/plugin/agent.ts:40-127`；监控 `:64-81` | agent 表更新 + `agent.updated` 事件 | 无（源文件即持久化） | TUI agent 列表刷新 |
| S3 | 用户选 agent（TUI） | shift+tab / `/agents` | 键位/选择 | `tui/app.tsx:903-912`；`context/local.tsx:57-95` | 仅本地 draft（`draftBySession`） | 无 | 输入框 agent 标签变化 |
| S4 | 提交 prompt（带 draft agent） | 回车 | 文本 + draft agent | `tui/component/prompt/index.tsx:1274-1282` → `client.session.switchAgent`；`server/handlers/session.ts:241-244` | 若不同则发 `session.agent.selected` | durable 事件 → session 表 `agent` 列 + `agent-switched` 消息 | 时间线出现“Switched agent …” |
| S5 | 每次请求选择上下文 | runner drain | sessionID | `session/context.ts:121-161` | 解析 agent（`agent.select(session.agent)`）、合并权限、`registry.snapshot(permissions)` 裁剪工具 | 无 | — |
| S6 | 组装 system prompt | 每次尝试 | agent.Info + tools | `model-request.ts:100-120`；`system-prompt.ts:5-27`；`optimize.ts:72-91` | system 数组定稿 | 无 | — |
| S7 | plan 提醒对账 | 每次请求 context hook | 请求消息序列 | `plugin/plan.ts:56-74` | 内存 splice `Message.user(reminder)` + `session.synthetic(...)` | synthetic 消息（inbox→projector） | 时间线出现 notice 行 |
| S8 | 模型流式输出工具调用 | provider 事件 | ToolCall | `runner/step.ts:88-129` | 每个 tool call 独立 fiber 执行 | 进行中状态写入消息 | 工具卡片 |
| S9 | 工具执行权限判定 | 工具内部 | action+resource+agent | `permission.ts:231-263` → 叶子工具各自 `permission.assert` | deny→BlockedError；ask→挂起 | ask 事件 ephemeral | 弹权限框 / 拒绝文案 |
| S10 | plan 下 edit/write/patch 被拒 | S9 deny | BlockedError | `plugin/plan.ts:45-54` 改写 → `tool.ts:131-138` → `step.ts:124-126` → `to-session-error.ts:83-90` | 工具错误入库（ToolStateError） | assistant 消息内工具状态 | 工具卡片报错文案 |
| S11 | 用户拒绝 ask | 权限框 reject | 可选 feedback | `permission.ts:276-297`；`step.ts:199-217` | CorrectedError→ToolFailure（模型继续）或 DeclinedError→整步 aborted | 工具状态 aborted | “The user declined this tool call” |
| S12 | 切换出/入 plan | switchAgent | agent id | S4 + `plugin/plan.ts:101-113` | enter/leave reminder 注入 | synthetic 消息 | notice 行 + 后续行为改变 |

### 2b. 叙事（用户输入一句话 → 回合结束）

1. 用户在 TUI 按 `shift+tab` 把 draft agent 切到 Plan（`local.agent.move`，`tui/app.tsx:908`）。此时未落库。
2. 输入并回车：`prepareAgent()` 发现 `session.agent !== "plan"`，调 `session.switchAgent`（`tui/prompt:1274-1282`）→ core 发布 `session.agent.selected`（`session/session.ts:94`）。同一时刻：
   - projector 把 `session_v2.agent` 更新为 `plan`（`projector.ts:546-557`）；
   - message-updater 追加 `agent-switched` 消息（`message-updater.ts:91-101`）；
   - PlanPlugin 订阅到事件，发 **synthetic** 消息「You are in Plan mode …」（`plugin/plan.ts:76-97`）。
3. 用户文本经 `Session.prompt` 进入 inbox 并唤醒执行（`session/session.ts:148-172`）。
4. runner drain 循环：`prepareContext` → `context.select`（`runner/llm.ts:198-203`、`context.ts:121-161`）解析出 `agent=plan`，合并其权限 6+ 条（模板 5 条 + 全局 4 条 + plan 4 条），用 `registry.snapshot(permissions)` 过滤工具目录。因 plan 的最后一条 `edit` 规则是 `~/.opencode/plan/* allow`（非 `* deny`），edit/write/patch **仍在目录中**（`tool.ts:231,292-295`）。
5. 组装请求（`runner/llm.ts:224-244` → `model-request.ts`）：system=`agent.system`（plan 无）→ 默认 system.txt（或按模型替换，`optimize.ts:78-86`）；随后 `session context` hook 运行，PlanPlugin 检查消息里有没有 enter reminder——如果（压缩/revert 后）没有，就在最后一条 user 消息前补一条 `Message.user(enterReminder)` 并发 synthetic 持久化（`plugin/plan.ts:56-74`）。提醒此刻起对模型可见。
6. 模型按 plan 风格输出：用 read/grep/glob/shell/webfetch 调研（都默认 allow），把方案写在对话里；若用户明确要计划文件，模型被提醒只能写 `~/.opencode/plan`（F20/F21）。
7. 若模型试图 `edit`/`write`/`patch` 项目文件：叶子工具 `permission.assert({action:"edit", resource:...})` → plan 规则 `edit * deny` 命中 → `BlockedError`；叶子 `mapError` 包成 `ToolFailure{message:"Unable to edit …", error:BlockedError}`；Tool execute.after 钩子把错误替换成 `Cannot use edit to modify files outside the Plan directory: ~/.opencode/plan`（`plugin/plan.ts:45-54`）；`toSessionError` 变成 `{type:"tool.execution", message:...}`（`to-session-error.ts:83-90`），作为 tool result error 回给模型（`to-llm-message.ts:141-147`），模型继续回合。
8. 用户要求实现：模型按 enter reminder 拒绝（提示需切换 agent）——这只是提示词约定；若模型仍调 edit→步骤 7。
9. 回合结束（`Idle` 消息，`session-message.ts:283-293`）。用户按 `shift+tab` 切回 Build：`agent-switched` + synthetic「You are NO LONGER in Plan mode」（`plugin/plan.ts:110-111`）。没有任何“批准/自动执行”环节。

## 3. 代码地图

| 文件 | 职责 | 关键类/函数(file:line) |
|---|---|---|
| `packages/core/src/plugin/plan.ts` | plan agent 定义 + 提醒注入 + 拒绝文案改写 | `Plugin`(:27-99)；`enter`(:14-21)；`leave`(:23-25)；`switchReminder`(:101-113)；`lastReminder`(:115-121) |
| `packages/core/src/plugin/agent.ts` | 内置 build/general/explore/compaction/title/summary | `AgentPlugin.Plugin`(:83-155) |
| `packages/core/src/agent.ts` | Agent 服务（Location 范围）：解析/选择/默认 | `defaultID="build"`(:18)；`selectable/selectedDefault`(:92-104)；`select`(:116-123) |
| `packages/schema/src/agent.ts` | Agent.Info schema 与默认权限 | `Info.default`(:39-54) |
| `packages/core/src/permission.ts` | 权限求值、ask/assert/reply、saved | `evaluate`(:87-97)；`configured`(:158-163)；`evaluateInput`(:173-189)；`assert`(:231-263)；`reply`(:265-325) |
| `packages/schema/src/permission.ts` | Rule/Ruleset/Effect/事件 schema | `Rule`(:58-63)；`Reply`(:41) |
| `packages/core/src/tool.ts` | 工具注册表：注册/快照裁剪/执行 | `snapshot`(:225-287)；`whollyDisabled`(:292-295)；`executeTool`(:113-156) |
| `packages/core/src/session/context.ts` | 请求前选择：agent/权限/工具快照/prompt 指令 | `select`(:121-161) |
| `packages/core/src/session/model-request.ts` | 请求组装、hook、executeTool 错误通道 | `baseTranscript`(:100-120)；`executeTool`(:385-399)；`agentHook`(:403-405) |
| `packages/core/src/session/runner/step.ts` | 单步执行：工具 fiber、decline 分类 | `attempt`(:72-276)；decline 处理(:199-217) |
| `packages/core/src/session/session.ts` | switchAgent / prompt admit / synthetic | `switchAgent`(:89-95)；`prompt`(:148-172)；`synthetic`(:273+) |
| `packages/core/src/session/message-updater.ts` | 事件→消息投影 | `session.agent.selected`(:91-101) |
| `packages/core/src/session/projector.ts` | 事件→DB 投影 | `AgentSelected`(:546-557)；fork 复制(:146-215) |
| `packages/core/src/config/plugin/agent.ts` | 自定义 agent .md / config 合并 | `Plugin`(:34-128)；`decode`(:177-212) |
| `packages/core/src/config/plugin/command.ts` | 命令可携带 agent 并切换 | 执行逻辑(:73-133) |
| `packages/core/src/file-access.ts` | 文件路径解析 + read/external_directory 权限 | `resolve`(:91-116)；`authorizeExternal`(:122-141)；`authorizeRead`(:143-159) |
| `packages/core/src/tool/plugin/{edit,write,patch}.ts` | 编辑类工具（共享 action=edit） | edit assert(:180-188)、mapError(:219-227)；write assert(:78-86) |
| `packages/core/src/tool/plugin/subagent.ts` | 子 agent 工具与权限门 | assert(:138-151)；available 描述(:286-295) |
| `packages/core/src/plugin/optimize.ts` | 模型适配 system prompt | hook(:72-91) |
| `packages/tui/src/config/keybind.ts` | agent.cycle 键位 | :172-173 |
| `packages/tui/src/app.tsx` | agent 命令注册 | :885-945 |
| `packages/tui/src/context/local.tsx` | TUI agent draft/列表/颜色 | :57-136 |
| `packages/tui/src/component/prompt/index.tsx` | 提交时切换 agent；标签显示 | :1274-1282；:1547-1550,1570；:1835 |
| `packages/cli/src/run/noninteractive.ts` | run --agent 先切后发 | :690-691 |
| `packages/server/src/handlers/session.ts` | switchAgent HTTP 端点 | :241-244 |

## 4. 状态与持久化

- 会话级：`session_v2.agent` 列 [verified: `session/sql.ts:57`]，由 `session.agent.selected` durable 事件投影 [verified: `projector.ts:546-557`]。session 表还有 `permission`（session 级规则集）列 [verified: `session/sql.ts:56`]。
- 消息级：
  - `agent-switched` 消息（含 previous）[verified: `session-message.ts:43-49`，message-updater.ts:91-101]；
  - 每条 assistant 消息记录 `agent` [verified: `session-message.ts:215`]；
  - plan 提醒以 `synthetic` 消息持久化 [verified: `session-message.ts:83-89`；plan.ts:84-89,66-68]；synthetic 在请求中映射为 user 消息文本 [verified: `to-llm-message.ts:284-285`]；
  - `agent-switched`/`idle` 消息**不进** LLM 上下文 [verified: `to-llm-message.ts:252-254`]（即：模型感知 agent 切换只靠 plan 的 synthetic 提醒；切到其他自定义 agent 时无提醒）。
- 权限：ask/reply 事件不持久化 [verified: `schema/permission.ts:44-53` ephemeral]；`always` 结果写入项目级 permission 表 [verified: `permission/sql.ts:19`、`permission.ts:299-305`]。
- fork：复制父会话 `agent`、`model`、`metadata`、`permission` 与边界前消息 [verified: `projector.ts:146-215`]。fork 出的 plan 会话若丢失提醒，由 F19 自愈。
- resume：`Session.resume` 仅重新唤醒 runner [verified: `session/session.ts:269-272`]；agent 沿用 session 表当前值；提醒对账在每次请求发生。
- revert：stage/commit 只处理消息与文件快照，不改 agent 字段 [verified: `session/revert.ts:32-88`]；注释明确「committed reverts can strip reminders」（`plugin/plan.ts:56-57`）。
- share：session 表有 `share_url` 列 [verified: `session/sql.ts:39`]，本次未深入，未见 agent 相关特殊处理。

## 5. 工具与权限模型

### 5.1 规则语言与求值

- Rule = `{action, resource, effect: "allow"|"deny"|"ask"}` [verified: `schema/permission.ts:55-63`]。
- 求值：`rules.findLast(match(action, rule.action) && match(resource, rule.resource))`，无匹配默认 `ask` [verified: `permission.ts:87-97`]。规则顺序即优先级；后写覆盖先写。
- 通配：`*`→`.*`（正则 `s` 模式，可跨路径段），`?`→`.` [verified: `util/wildcard.ts:3-13`]。
- 多个 resource 求值：任一 `deny` 先短路（`denied`），否则任一 `ask` → ask，否则 allow [verified: `permission.ts:165-167,173-189`]。
- 额外来源：agent 权限 + session 权限（session 后求值）[verified: `permission.ts:158-163`]；saved 规则叠加最后 [verified: `permission.ts:176`]；插件 hook `permission.evaluate` 可改结果 [verified: `permission.ts:179-188`]。
- agent 无权限时的 fallback 是 `* * deny`（`missingAgentPermissions`）[verified: `permission.ts:19`；注意仅在 agent 解析不到时使用]。

### 5.2 内置 agent 默认权限（合成顺序 = 求值优先级）

所有 agent 公共基底（按顺序）：

1. `* * allow`、`external_directory * ask`、`read *.env ask`、`read *.env.* ask`、`read *.env.example allow` [verified: `schema/agent.ts:46-52`]
2. `external_directory` allow：`<data>/shell/*/*`、`<data>/tool-output/*`、`<tmp>/*`、`<config>/*` [verified: `core/agent.ts:59-64`（路径经 `global.data/tmp/config`）]

各 agent 追加（顺序在基底之后）：

- **build**：`question * allow` [verified: `plugin/agent.ts:87-92`]
- **general**：`question * deny`、`subagent * deny` [verified: `plugin/agent.ts:94-103`]
- **explore**：`* * deny` → grep/glob/webfetch/websearch/read allow、`read .env* ask`、subagent deny、`external_directory * ask`（+ 公共基底里的 allow）[verified: `plugin/agent.ts:105-131`]
- **compaction / title / summary**：title/summary 追加 `* * deny`；compaction 无追加；均 hidden [verified: `plugin/agent.ts:133-153`]
- **plan**：`question * allow`、`edit * deny`、`edit <home>/.opencode/plan/* allow`、`external_directory <home>/.opencode/plan/* allow` [verified: `plugin/plan.ts:38-41`]

合并顺序说明：内置插件（`AgentPlugin`、`PlanPlugin`）在 internal `pre` 组，自定义配置插件 `ConfigAgentPlugin` 在 `post` 组，激活顺序 pre→post [verified: `plugin/supervisor.ts:143-155`、`plugin/internal.ts:221,252`]。因此用户通过 `agents.plan.permissions` / `.md` 追加的规则位于内置规则之后，在 last-match-wins 下**可以覆盖内置默认**（例如重新允许 plan 下 edit）[inferred，基于 `permissions.push` 累积 + `findLast` 求值，`config/plugin/agent.ts:121-123`、`permission.ts:91`]。

### 5.3 权限矩阵：工具（action）× 内置 agent 默认动作

| 工具/操作（action） | action 名 | build | general | explore | plan | 依据 |
|---|---|---|---|---|---|---|
| read（含 .env 例外） | `read` | allow / .env ask | allow / .env ask | allow / .env ask | allow / .env ask | file-access.ts:143-159；schema/agent.ts:49-51 |
| glob | `glob` | allow | allow | allow | allow | glob.ts:68-72 |
| grep | `grep` | allow | allow | allow | allow | grep.ts:87-92 |
| edit / write / patch | `edit` | allow | allow | deny（`*`→目录移除） | deny（plan 目录例外；目录保留） | edit.ts:180；write.ts:78；patch.ts:196；plan.ts:39-40 |
| shell | `shell` | allow | allow | deny | allow | shell.ts:135-140 |
| webfetch | `webfetch` | allow | allow | allow | allow | webfetch.ts:124-129 |
| websearch | `websearch` | allow | allow | allow | allow | websearch.ts:49-54 |
| skill | `skill` | allow | allow | deny | allow | skill.ts:51-55 |
| question | `question` | allow（显式） | deny（显式） | deny | allow（显式） | question.ts:63-70；agent.ts:100；plan.ts:38 |
| subagent（spawn 子 agent） | `subagent` | allow | deny（显式） | deny（显式） | allow（未 deny） | subagent.ts:138-151；plugin/agent.ts:101,126 |
| 读取项目外路径 | `external_directory` | ask（公共目录外） | ask | ask | ask；plan 目录 allow | file-access.ts:122-141；plan.ts:41 |
| codemode 沙箱 | `execute` | allow | allow | deny（整工具从目录移除） | allow | tool.ts:238-239；explore `*` deny 生效 |
| opencode list/read MCP resource | `opencode_list_mcp_resources` / `opencode_read_mcp_resource` | allow | allow | deny（explore 的 `*` deny 命中 findLast） | allow | mcp-resource.ts:36-38,65-68 |
| MCP server 工具 | `name(server, tool)`（命名空间化） | allow | allow | deny（同上；经 execute 不可达） | allow | tool/mcp.ts:47-60 |
| session_rename / session_move / models | 无 assert（执行体不查权限） | 不适用 | 不适用 | 不适用（execute 已移除） | 不适用 | opencode.ts:89-140（无 permission.assert 调用） |

注 1：explore 的工具移除是 `whollyDisabled` 生效的样例——`{action:"*",resource:"*",deny}` 且无更后 allow → 从 catalog 移除 [verified: `tool.ts:292-295`]。
注 2：plan 的 edit 工具**不会**被移除（如上），运行时才拒绝 [verified: 同段代码 + plan.ts:40]。
注 3：plan 未对 shell/subagent 做硬限制；"只读"主要靠提示词提醒（F17/F35/F50）[verified: plan.ts:14-21 无 shell/subagent deny]。

### 5.4 强制点在执行链的哪一层

- 注册表（`Tool.Service`）不做授权 [verified: `tool/AGENTS.md:52`, `tool.ts` 无 Permission 依赖]。
- 每个叶子工具在执行体内部自调 `permission.assert`（edit/write/patch/shell/glob/grep/webfetch/websearch/skill/question/subagent/mcp-resource/read=file-access）[verified: 各 plugin 的 assert 调用点，见 §3/§5.3]。
- `Permission.assert` 是唯一 deny 判定点；`ask` 走挂起问答 [verified: `permission.ts:231-263`]。
- 会话层只在 `context.select` 用权限**裁目录**，不拦执行 [verified: `context.ts:128-134` + `tool/AGENTS.md:54`]。
- step 层处理用户 decline 的分类与打断 [verified: `runner/step.ts:199-217`]。

## 6. 关键源码摘录

### 6.1 plan agent 定义 + enter/leave 提醒（`packages/core/src/plugin/plan.ts:12-43`）

```ts
const plan = Agent.ID.make("plan")

const enter = (directory: string) => `<system-reminder>
You are in Plan mode. Discuss the plan with the user directly in the conversation. Do not create or update plan files unless the user explicitly asks you to; when they do, write them only in:
${directory}

Do not modify any other files or ask a subagent to do so.

You remain in Plan mode until the user switches agents. If the user asks you to implement changes, do not do so. Tell them they need to switch agents.
</system-reminder>`
// leave 略（:23-25）
...
    const global = yield* Global.Service
    const directory = path.join(global.home, ".opencode", "plan")
    const enterReminder = enter(directory)
    yield* ctx.agent.transform((editor) => {
      editor.update(plan, (item) => {
        item.name = Agent.Name.make("Plan")
        item.description = "Read-only agent for exploring the codebase and planning work before implementation."
        item.mode = "primary"
        item.permissions.push({ action: "question", resource: "*", effect: "allow" })
        item.permissions.push({ action: "edit", resource: "*", effect: "deny" })
        item.permissions.push({ action: "edit", resource: path.join(directory, "*"), effect: "allow" })
        item.permissions.push({ action: "external_directory", resource: path.join(directory, "*"), effect: "allow" })
      })
    })
```

### 6.2 拒绝文案改写 + 提醒订阅（`packages/core/src/plugin/plan.ts:45-99`）

```ts
    yield* ctx.tool.hook("execute.after", (event) => {
      if (event.agent !== plan) return Effect.void
      if (event.status !== "error") return Effect.void
      if (event.tool !== "edit" && event.tool !== "write" && event.tool !== "patch") return Effect.void
      if (!(event.error.error instanceof Permission.BlockedError)) return Effect.void
      event.error = new ToolFailure({
        message: `Cannot use ${event.tool} to modify files outside the Plan directory: ${directory}`,
      })
      return Effect.void
    })

    // Compaction and committed reverts can strip reminders while the session's agent stays
    // put. Reconcile per request, appending near the tail so the cached prefix stays warm.
    yield* ctx.session.hook("context", (event) => {
      const reminder = lastReminder(event.messages, enterReminder)
      const missing = event.agent === plan && reminder !== enterReminder
      const stale = event.agent !== plan && reminder === enterReminder
      const text = missing ? enterReminder : stale ? leave : undefined
      if (!text) return Effect.void
      const at = event.messages.at(-1)?.role === "user" ? event.messages.length - 1 : event.messages.length
      event.messages.splice(at, 0, Message.user(text))
      return ctx.session.synthetic({ sessionID: event.sessionID, text, resume: false }).pipe(...)
    })
    // session.created / session.agent.selected 订阅见 :76-97，switchReminder 见 :101-113
```

### 6.3 Agent 默认权限模板与 build/general/explore（`packages/schema/src/agent.ts:39-53`；`packages/core/src/plugin/agent.ts:87-103`）

```ts
// schema/agent.ts
default: (id: ID) => ({
  id, name: Name.make(id),
  request: { settings: {}, headers: {}, body: {} },
  mode: "primary", hidden: false,
  permissions: [
    { action: "*", resource: "*", effect: "allow" },
    { action: "external_directory", resource: "*", effect: "ask" },
    { action: "read", resource: "*.env", effect: "ask" },
    { action: "read", resource: "*.env.*", effect: "ask" },
    { action: "read", resource: "*.env.example", effect: "allow" },
  ],
})

// plugin/agent.ts
editor.update(Agent.defaultID, (item) => {           // 即 "build"
  item.name = Agent.Name.make("Build")
  item.description = "The default agent. Executes tools based on configured permissions."
  item.mode = "primary"
  item.permissions.push({ action: "question", resource: "*", effect: "allow" })
})
editor.update(Agent.ID.make("general"), (item) => {
  item.mode = "subagent"
  item.permissions.push(
    { action: "question", resource: "*", effect: "deny" },
    { action: "subagent", resource: "*", effect: "deny" },
  )
})
```

### 6.4 权限求值与 assert（`packages/core/src/permission.ts:87-97, 158-189, 231-263`）

```ts
export function evaluate(action: string, resource: string, ...rulesets: Permission.Ruleset[]): Permission.Rule {
  return (
    rulesets.flat().findLast((rule) => Wildcard.match(action, rule.action) && Wildcard.match(resource, rule.resource)) ?? {
      action, resource: "*", effect: "ask",
    }
  )
}
...
const configured = Effect.fnUntraced(function* (sessionID, agentID) {
  const session = yield* sessions.get(sessionID)
  const agent = yield* agents.resolve(agentID ?? session.agent)
  return merge(agent?.permissions ?? missingAgentPermissions, session.permissions ?? [])
})
...
if (result.effect === "deny") {
  return yield* new BlockedError({
    rules: relevant(input, result.rules),
    permission: input.action,
    resources: input.resources,
    reason: result.message,
  })
}
if (result.effect === "allow") return
const item = yield* create(request(input, result.message), input.agent)
return yield* restore(Deferred.await(item.deferred)).pipe(
  Effect.catchTag("Permission.DeclinedError", (error) => Effect.die(error)),   // 缺陷隧道
  ...
)
```

### 6.5 工具目录裁剪（`packages/core/src/tool.ts:225-235, 292-295`）

```ts
snapshot: Effect.fn("Tool.snapshot")((permissions) =>
  Effect.sync(() => {
    const data = state.get()
    const active = new Map<string, Tool.Info>()
    const rules = permissions ?? []
    for (const [name, tool] of data.tools) {
      if (whollyDisabled(tool.options?.permission ?? name, rules)) continue
      active.set(name, tool)
    }
    ...
```
```ts
const whollyDisabled = (action: string, rules: Permission.Ruleset) => {
  const rule = rules.findLast((rule) => Wildcard.match(action, rule.action))
  return rule?.resource === "*" && rule.effect === "deny"
}
```

### 6.6 deny 的模型可见形态（链路）

- 叶子映射（edit；write/patch 同构）：

```ts
// packages/core/src/tool/plugin/edit.ts:219-227
Effect.mapError((error) =>
  error instanceof ToolFailure ? error : new ToolFailure({ message: `Unable to edit ${input.path}`, error }),
)
```

- plan 改写（§6.2）→ `toSessionError`：

```ts
// packages/core/src/session/to-session-error.ts:85-90
if (cause instanceof ToolFailure || cause instanceof Tool.Error) {
  if (cause.error === undefined) return { type: "tool.execution", message: cause.message }
  const unwrapped = toSessionError(cause.error)
  return unwrapped.message === "" ? { ...unwrapped, type: "tool.execution", message: cause.message } : unwrapped
}
```

- 回填给模型：tool result error

```ts
// packages/core/src/session/runner/to-llm-message.ts:141-147
if (tool.state.status === "error") {
  return { ..., result: { error: tool.state.error, content: tool.state.content ?? [] } }
}
```

- 最终模型看到两类文本：plan 改写后 `Cannot use edit to modify files outside the Plan directory: <home>/.opencode/plan`；用户拒绝 ask 时（`step.ts:200-207`）工具被标 aborted `"The user declined this tool call"`。

### 6.7 agent 切换持久化三件套

```ts
// packages/core/src/session/session.ts:89-95
const switchAgent = Effect.fn("Session.switchAgent")(function* (sessionID, input) {
  const session = yield* get(sessionID)
  yield* bus.publish(SessionEvent.AgentSelected, { sessionID, agent: input.agent, previous: session.agent })
})
```
```ts
// projector: session 列
db.update(SessionTable).set({ agent: event.data.agent, ... }).where(...)   // :548-556
// message-updater: 消息
SessionMessage.AgentSelected.make({ type: "agent-switched", agent, previous, ... })  // :93-100
```

### 6.8 system prompt 组装（`packages/core/src/session/model-request.ts:100-120`）

```ts
system: [
  input.agent.system
    ? input.agent.system
    : SessionSystemPrompt.make(input.tools.definitions.map((tool) => tool.name)),
  input.initial,
]
```

（plan 不设 `system`，因此走默认模板；`optimize.ts:78-86` 会在 agent 无自定义 system 时按模型替换 system[0]。）

### 6.9 子 agent 权限门与继承（`subagent.ts:138-151`；`session.ts:284-288`）

```ts
yield* permission.assert({
  action: name,                    // "subagent"
  resources: [agent.id], save: [agent.id],
  sessionID: context.sessionID, agent: context.agent, source: ...
}).pipe(Effect.mapError((error) => new ToolFailure({ message: `Subagent denied: ${agent.id}`, error })))
...
const child = ... sessions.create({ parentID: context.sessionID, agent: Agent.ID.make(input.agent), model })
// Session.create:
// permissions: input.permissions ?? parent?.permissions,
```

## 7. 计划-执行衔接结论

**opencode（本版本）没有 plan → approve → auto-execute 机制。**

- 没有批准状态、批准消息类型、批准事件 [verified: message union `session-message.ts:295-320` 与事件清单中无 approve/plan 类型；对 `approve|approval|plan mode` 的全仓库 grep 只命中权限自动批准与无关内容]。
- 没有自动从 plan 切回 build 的代码：plan 的 enter 提醒要求模型拒绝实现并让用户切换 agent [verified: `plan.ts:20`]；切换只发生于用户动作（TUI shift+tab / `/agents`、CLI `--agent`、HTTP `session.switchAgent`、命令携带 `agent:`）[verified: §1 F09-F12、F41]。
- 替代机制 = 提示词约定 + 权限硬约束 + 人工切换：
  1. 硬约束：`edit * deny`（plan 目录例外）[verified: `plan.ts:39-40`]；
  2. 软约束：enter/leave system-reminder 注入与每请求自愈 [verified: `plan.ts:14-25,56-74,101-113`]；
  3. 交接：用户在 UI 切换，切换本身产生 durable 事件与提示 [verified: §6.7、`plan.ts:110-111`]。
- 计划产物：默认不存在；只有提醒文本允许模型在用户明确要求时写 `~/.opencode/plan`（无审批、无读回机制）[verified: `plan.ts:15-16,31`]。
- todo/计划清单工具：v2 已删除 `todowrite` [verified: `v1-migration.bun.ts:901`]。
- 需注意的“文档 vs 实际”差异（供对比阶段引用）：
  1. plan 的 “read-only” 不是沙箱：shell 默认 allow、subagent 默认 allow、MCP 工具默认 allow；只读由 `edit` deny + 提示词共同构成 [verified: §5.3 注 3]；
  2. plan 下 edit/write/patch 工具**仍出现在模型工具目录**（因为有 plan 目录 allow 规则破坏 whollyDisabled），拒绝发生在执行期 [verified: `tool.ts:292-295` + `plan.ts:40`]；
  3. 子 agent 不继承父 agent 的权限（继承的是父 **session** 权限），所以 plan 会话里 spawn 的 general 子 agent 拥有 general 的写权限，仅受提示词劝阻 [verified: §6.9 + `permission.ts:158-163`]；
  4. agent 切换消息 `agent-switched` 不进 LLM 上下文；只有 plan 有 synthetic 提醒注入，其它 agent 切换对模型不可见 [verified: `to-llm-message.ts:252-254`]。
