# 02 · opencode plan 模式 / plan agent 源码级深审（v2 @ 0a46301e3）

> 状态：**已落档**（2026-10-19 重审轮）。源：opencode v2 @ 0a46301e3。取代 `01` 中与本稿冲突的事实。

> 只读研究子代理报告。所有结论给 `file:line`；`[verified]` 表示代码直接证明，`[inferred]` 表示由代码路径/时序推导并说明理由。
> 本报告独立成立，不依赖既有底稿；第 8 节再逐条对照 `01-opencode事实底稿.md`。

## 0. 版本与覆盖范围

- 仓库：`/home/linsir365/projects/opencode`，分支 `v2`，HEAD `0a46301e3`（`chore(core): refresh bundled models-dev snapshot`）。`[verified: git log -1]`
- 工作区有他人未提交改动：`packages/core/src/models-dev/snapshot.txt`、`packages/plugin/src/host.ts`；另有未跟踪的 `packages/cli/nexus.db`、`research/`。本次未修改任何仓库文件。`[verified: git status --short]`
- 已先读：`/home/linsir365/projects/ai-coding/docs/plan-mode-oc-alignment/01-opencode事实底稿.md`（467 行），再逐条回源码核对。
- 阅读优先级与实际范围：
  - `packages/core/src`：`plugin/plan.ts`、`plugin/agent.ts`、`plugin/internal.ts`、`plugin/supervisor.ts`、`plugin/optimize.ts`、`agent.ts`、`permission.ts`、`permission/{saved,sql}.ts`、`tool.ts`、`tool/plugin/*`、`file-access.ts`、`config.ts`、`config/discovery.ts`、`config/plugin/{agent,command,compatibility}.ts`、`session.ts`、`session/{session,context,model-request,message-updater,projector,revert,sql,compaction}.ts`、`session/runner/{llm,step,to-llm-message}.ts`、`session/system-prompt.ts`、`session/runner/prompt/system.txt`、`util/wildcard.ts`、`v1/config/{agent,migrate}.ts`、`config/normalize.ts`、`database/v1-migration.bun.ts`
  - `packages/schema/src`：`agent.ts`、`permission.ts`、`session.ts`、`session-message.ts`、`session-event.ts`、`config.ts`、`config/agent.ts`
  - `packages/tui/src`：`app.tsx`、`context/local.tsx`、`component/prompt/index.tsx`、`component/dialog-agent.tsx`、`routes/session/{index,permission}.tsx`、`config/keybind.ts`、`feature-plugins/system/notifications.ts`
  - `packages/cli/src`：`commands/commands.ts`、`run/noninteractive.ts`、`acp/{service,catalog}.ts`
  - `packages/server/src/handlers/{agent,session}.ts`、`packages/client/src/solid/data.ts`、`packages/session-ui/src/{timeline/*,tools/tool-renderer.tsx}`
  - `packages/util/src/{global,global-roots}.ts`
- 跳过：`node_modules`、dist/build、测试实现细节（仅把 `packages/core/test/plugin/plan.test.ts` 当作行为佐证阅读，未运行）、provider/plugin 的无关细节。
- 未运行测试、未构建、未改动文件。`[verified: 本次操作]`

---

## 1. 功能清单

状态列：`实现` / `无` / `部分`（软约束或仅提示词）。

| F-ID | 功能 | 触发 | 代码位置 | 行为 | 状态 |
|---|---|---|---|---|---|
| F01 | 内置 plan agent 由 Effect 插件定义 | 进程启动、插件激活 | `packages/core/src/plugin/plan.ts:12,27-43`；`packages/core/src/plugin/internal.ts:221` | `editor.update(plan, ...)`：name=`Plan`、description=`Read-only agent for exploring the codebase and planning work before implementation.`、mode=`primary`，push 4 条权限 | 实现 |
| F02 | 内置 agent 清单 | 插件激活 | `packages/core/src/plugin/agent.ts:86-153`；`packages/core/src/agent.ts:18` | `build`（默认，`defaultID`）、`general`/`explore`（subagent）、`compaction`/`title`/`summary`（hidden primary），加插件定义的 `plan` | 实现 |
| F03 | agent 默认权限模板 | 任意 agent 首次创建 | `packages/schema/src/agent.ts:39-53` | `* * allow`、`external_directory * ask`、`read *.env ask`、`read *.env.* ask`、`read *.env.example allow` | 实现 |
| F04 | 全局 external_directory 允许 | 任意 agent 首次创建 | `packages/core/src/agent.ts:59-64,74-84` | 追加 `<data>/shell/*/*`、`<data>/tool-output/*`、`<tmp>/*`、`<config>/*` 的 external_directory allow | 实现 |
| F05 | 自定义 agent `.md` 发现 | 启动、配置目录/文件变化 | `packages/core/src/plugin/agent.ts:21-24,40-52,164-175`；`packages/core/src/config.ts:185-237`；`packages/core/src/config/discovery.ts:21-68`；`packages/core/src/config/plugin/compatibility.ts:39-42` | 只对 Config 的 `directory` 条目（全局 config 目录、项目 `.opencode` 目录）扫描 `{agent,agents}/**/*.md`（primary:false）与 `{mode,modes}/*.md`（primary:true）；`.claude`/`.agents` 只被 compatibility 插件用于 skills，不扫描 agent | 实现 |
| F06 | `.md` frontmatter/正文解析 | 加载 `.md` | `packages/core/src/plugin/agent.ts:177-211`；`packages/schema/src/config/agent.ts:7-19`；`packages/core/src/v1/config/agent.ts:8-40` | 文件名（去掉 `agent/` 等前缀与 `.md`）= agent id；正文 trim 后作为 `system`/legacy `prompt`；未知 frontmatter key 走 v1 legacy decode；`mode/` 目录强制 `mode:"primary"` | 实现 |
| F07 | 配置内 agent 合并 | 配置/`.md` 变化 | `packages/core/src/plugin/agent.ts:83-127` | 按文档顺序 `editor.update`：标量字段后写覆盖前写；`permissions` push 累积；`disabled` 删除 agent；顶层 `permissions` 先追加到所有已有 agent | 实现 |
| F08 | `default_agent` | 配置变化 | `packages/schema/src/config.ts:35`；`packages/core/src/plugin/agent.ts:88-89`；`packages/core/src/agent.ts:94-103` | 用 `Config.latest(..., "default_agent")` 设 `editor.default(id)`；只有该 agent 可被 selectable 时才生效，否则回退 build/首个 selectable | 实现 |
| F09 | agent 选择/回退 | 每次上下文选择 | `packages/core/src/agent.ts:92-104,116-129` | `selectable = mode !== "subagent" && !hidden`；默认 `default_agent` → `build` → 任意 selectable；`list()` 把 selected 放第一位 | 实现 |
| F10 | TUI shift+tab 循环 | `agent.cycle` | `packages/tui/src/config/keybind.ts:172`；`packages/tui/src/app.tsx:903-908`；`packages/tui/src/context/local.tsx:114-123` | `local.agent.move(1)` 在 primary 且非 hidden 的 agent 间循环；`agent.cycle.reverse` 有命令但默认键位 `none`（`:173,937-943`） | 实现 |
| F11 | TUI `/agents` 对话框 | `/agents` 或命令面板 | `packages/tui/src/app.tsx:885-891`；`packages/tui/src/component/dialog-agent.tsx:8-24` | 列出 `local.agent.list()`（非 subagent、非 hidden），选择写 draft | 实现 |
| F12 | 切换只写 draft，提交时才持久化 | 输入框提交/空回车 | `packages/tui/src/context/local.tsx:63-74,86-113`；`packages/tui/src/component/prompt/index.tsx:1274-1282,1295-1302,1330` | `prepareAgent()` 比较 `session.agent !== agent.id` 后调 `session.switchAgent`；空回车也 `commitSelection()` | 实现 |
| F13 | 会话级 agent 字段 | 每次切换 | `packages/schema/src/session.ts:39`；`packages/core/src/session/sql.ts:53`；`packages/core/src/session/projector.ts:546-556` | `session_v2.agent` 列由 `session.agent.selected` durable 事件投影 | 实现 |
| F14 | `agent-switched` 消息 | 每次切换 | `packages/schema/src/session-message.ts:43-49`；`packages/core/src/session/message-updater.ts:90-103` | 追加消息，含 `agent` 与可选 `previous`；ID 由事件 ID 派生 | 实现 |
| F15 | 每条 assistant 记录 agent | 每个助手回合 | `packages/schema/src/session-message.ts:212-216` | `Assistant.agent: Agent.ID` | 实现 |
| F16 | agent 切换 durable 事件 | `switchAgent` | `packages/schema/src/session-event.ts:69-78`；`packages/core/src/session/session.ts:89-95` | `session.agent.selected`，durable aggregate=`sessionID`；`previous` 可选；不校验 agent 是否存在 | 实现 |
| F17 | plan 进入提醒 | 创建 plan 会话 / 切到 plan | `packages/core/src/plugin/plan.ts:14-21,33-42,76-97,101-113` | 向 `session.synthetic` 递交 `<system-reminder>You are in Plan mode...</system-reminder>`；`resume:false` | 实现 |
| F18 | plan 退出提醒 | plan → 其他 agent | `packages/core/src/plugin/plan.ts:23-25,110-111` | `<system-reminder>You are NO LONGER in Plan mode...</system-reminder>` | 实现 |
| F19 | 每请求提醒对账 | 每次 primary context hook | `packages/core/src/plugin/plan.ts:56-74,101-121` | 扫描最后一条 enter/leave；plan 下缺 enter 补 enter，非 plan 下最后是 enter 补 leave；插入到最后一条 user 消息之前（否则末尾） | 实现 |
| F20 | plan 权限 | agent 创建 | `packages/core/src/plugin/plan.ts:33-42` | `question * allow`、`edit * deny`、`edit <home>/.opencode/plan/* allow`、`external_directory <home>/.opencode/plan/* allow` | 实现 |
| F21 | plan 目录约定 | 插件激活/写文件 | `packages/core/src/plugin/plan.ts:30-31`；`packages/util/src/global.ts:16-18`；`packages/core/test/plugin/plan.test.ts:248-253` | 路径=`path.join(Global.home, ".opencode", "plan")`；插件不 mkdir；`write` 工具会按需创建父目录 | 部分（仅路径+工具行为） |
| F22 | 拒绝文案改写 | plan 下 edit/write/patch 被 deny | `packages/core/src/plugin/plan.ts:45-54`；`packages/core/src/tool.ts:131-138`；`packages/core/src/session/to-session-error.ts:63-68` | 把 `ToolFailure.error instanceof Permission.BlockedError` 替换为 `Cannot use {tool} to modify files outside the Plan directory: {dir}` | 实现 |
| F23 | 权限求值 | 任意 assert | `packages/core/src/permission.ts:87-97`；`packages/core/src/util/wildcard.ts:3-13` | `findLast` 同时匹配 action/resource；无匹配默认 `ask`；`*`→`.*`（s 模式，跨路径段），`?`→`.` | 实现 |
| F24 | 权限来源合并 | 任意 assert | `packages/core/src/permission.ts:158-188` | agent.permissions + session.permissions（session 后）；先对两者做 deny 短路；再拼 saved（仅 allow）；最后 `permission.evaluate` hook 可改 effect | 实现 |
| F25 | ask/reply 流程 | 工具内 assert 返回 ask | `packages/core/src/permission.ts:231-325`；`packages/tui/src/routes/session/permission.tsx:232-257` | once/always/reject；reject 带 message→CorrectedError（模型可见），无 message→DeclinedError（die，整步打断） | 实现 |
| F26 | subagent 权限门 | subagent 工具执行 | `packages/core/src/tool/plugin/subagent.ts:134-151` | 对父会话按 `action:"subagent"`、resource=子 agent.id assert；plan 无该 action 规则→基础 allow | 实现 |
| F27 | 子会话权限继承 | 新建/继续子会话 | `packages/core/src/tool/plugin/subagent.ts:184-193`；`packages/core/src/session.ts:284-288` | 子会话继承父 **session.permissions**（不是父 agent 的权限集）；子会话用自己的 agent 求值 | 实现 |
| F28 | 项目外路径边界 | 读写/编辑/搜索/shell | `packages/core/src/file-access.ts:93-122,125-141,144-160` | 外部路径先 `external_directory` assert，再 `read`/`edit` assert；external 的 resource 是 `dirname/*` | 实现 |
| F29 | 工具目录裁剪 | `context.select` | `packages/core/src/tool.ts:225-262,292-295`；`packages/core/src/session/context.ts:128-134` | 仅当某 action 的最后匹配规则为 `resource:"*" effect:"deny"` 才从 snapshot 移除；`execute` 单独判断。注意：snapshot 之后，PatchTool 的 context hook 还会按模型删减请求级 edit/write/patch（F61） | 实现 |
| F30 | 工具注册/枚举 | 插件激活 | `packages/core/src/plugin/internal.ts:213-246`；`packages/core/src/tool/plugin/*` | 直连工具：patch/edit/write/read/glob/grep/shell/webfetch/websearch/skill/question/subagent；codemode `execute`；`opencode_*` 与 MCP | 实现 |
| F31 | v1 `tools` 配置迁移 | 加载 v1 配置 | `packages/core/src/v1/config/agent.ts:50-59`；`packages/core/src/config/normalize.ts:487-497`；`packages/core/src/v1/config/migrate.ts:27-33` | `write/edit/patch`→`edit`，`task`→`subagent`，`bash`→`shell`；禁用工具转 `deny` 规则 | 实现 |
| F32 | system prompt 组装 | 每次请求 | `packages/core/src/session/model-request.ts:100-120`；`packages/core/src/session/system-prompt.ts:5-27`；`packages/core/src/session/runner/prompt/system.txt:1-15` | `[agent.system ?? 默认 system.txt, input.initial]`；按可用工具名增删 shell/write/edit 指导语 | 实现 |
| F33 | 模型适配 prompt | 每次请求 | `packages/core/src/plugin/optimize.ts:72-90` | 匹配 gpt/claude/kimi/trinity/muse 时替换或追加 system[0]；agent 有自定义 `system` 则跳过 | 实现 |
| F34 | plan 无专属 `agent.system` | — | `packages/core/src/plugin/plan.ts:33-42` | plan 不设置 system；与 build 共用默认 system（再被模型适配 prompt 覆盖/追加） | 实现（无=事实） |
| F35 | plan → 执行的人工交接 | 用户切 agent | `packages/core/src/plugin/plan.ts:20`；`packages/tui/src/component/prompt/index.tsx:1274-1282` | 提醒原文要求模型拒绝实现并让用户切换 agent；实际切换只有用户/客户端动作 | 实现（人工） |
| F36 | 计划批准/自动执行 | — | `packages/schema/src/session-message.ts:295-307`；`packages/schema/src/session-event.ts:652-690`；全仓 `switchAgent` 调用点 | 无 approve/plan 消息类型、无 approve 事件、无 plan→build 自动切换 | 无 |
| F37 | todo/todowrite | — | `packages/core/src/database/v1-migration.bun.ts:901`；`packages/session-ui/src/timeline/projection.ts:608`；`packages/session-ui/src/tools/tool-renderer.tsx:1313` | v2 无 todowrite 工具；迁移把 todowrite 列为 REMOVED；UI 隐藏历史 todowrite（不是渲染） | 无（已移除/隐藏） |
| F38 | plan 文件磁盘产物 | 模型被提示可写 | `packages/core/src/plugin/plan.ts:15-18,31` | 无自动生成；仅提示词约定“用户明确要求时”写 `<home>/.opencode/plan`；权限本身不检查“明确要求” | 部分（提示词） |
| F39 | fork | `Session.fork` | `packages/core/src/session/projector.ts:147-176,183-220` | fork 复制父 `agent`、`model`、`metadata`、`permission` 与边界前消息（含 synthetic/agent-switched）；不复制 pending inbox | 实现 |
| F40 | revert | stage/commit | `packages/core/src/session/revert.ts:32-88`；`packages/core/src/session/projector.ts:739-774` | commit 删除边界及之后的 `session_message` 与 inbox 行，不改 `session_v2.agent`；提醒丢失由 F19 自愈 | 实现 |
| F41 | 命令携带 agent | 执行带 `agent:` 的 command | `packages/core/src/config/plugin/command.ts:73-133` | 非 subagent：先 `switchAgent` 再 prompt；`subagent`/`subtask` 或 agent.mode=subagent：创建子会话 | 实现 |
| F42 | 权限询问/结果事件 | ask/reply | `packages/schema/src/permission.ts:44-53` | `permission.asked`/`permission.replied` 为 ephemeral | 实现 |
| F43 | always 落盘 | 用户选 always | `packages/core/src/permission.ts:299-305,310-321`；`packages/core/src/permission/saved.ts:55-78`；`packages/core/src/permission/sql.ts:8-25` | 把 request.save（不是实际 resource）写项目级 `permission` 表；effect 固定 allow；unique(project,action,resource)；随后尝试自动放行同批 pending | 实现 |
| F44 | CLI 非交互 auto | `opencode run --auto` | `packages/cli/src/commands/commands.ts:28-31`；`packages/cli/src/run/noninteractive.ts:164-183` | 对 ask 自动回 `once`；plan 的 deny 不变；无 auto 时非 v1 兼容模式用带 message 的 reject，让模型继续 | 实现 |
| F45 | 时间线 agent 归属/切换提示 | 渲染 | `packages/tui/src/routes/session/index.tsx:1881-1884,1917-1925,2200`；`packages/session-ui/src/timeline/session-timeline-row.tsx:328-335` | assistant 标签按 `message.agent` 着色；`agent-switched` 显示 `Switched agent ...`；session-ui 显示 `Agent changed` 类 notice | 实现 |
| F46 | agent API | HTTP | `packages/server/src/handlers/agent.ts:8-25` | `agent.list` / `agent.get`；Location 作用域 | 实现 |
| F47 | agent 更新广播 | 配置热更新 | `packages/schema/src/agent.ts:11,57-60`；`packages/core/src/agent.ts:90`；`packages/client/src/solid/data.ts:1216-1219` | `agent.updated` ephemeral → 客户端 invalidate/sync agent 列表 | 实现 |
| F48 | plan 读 `.env` | read 工具 | `packages/schema/src/agent.ts:46-52`；`packages/core/src/file-access.ts:144-160` | plan 无 read 覆盖；`*.env`/`*.env.*` ask，`*.env.example` allow | 实现 |
| F49 | plan 下 shell | shell 工具 | `packages/core/src/tool/plugin/shell.ts:133-142,192`；`packages/core/src/plugin/plan.ts:38-41` | plan 无 shell 规则 → 基础 `* * allow`；按解析出的命令 assert；外部目录先 ask；可修改项目文件 | 实现（软只读） |
| F50 | plan 下 subagent | subagent 工具 | `packages/core/src/tool/plugin/subagent.ts:134-151,288-294`；`packages/core/src/plugin/plan.ts:18` | plan 无 subagent deny → allow；提示词劝阻“不要指示子 agent 改文件”，无硬拒 | 实现（软约束） |
| F51 | `mode` 语义/校验 | agent 创建/选择 | `packages/schema/src/agent.ts:30`；`packages/core/src/agent.ts:92-93`；`packages/core/src/tool/plugin/subagent.ts:136,291`；`packages/tui/src/context/local.tsx:58` | 合法值 `subagent|primary|all`；`all` 同时出现在 primary 选择列表和 subagent 可用列表（由过滤条件推得，代码无专门分支）；非法值使 `.md`/配置文档解码失败并被 `Option` 丢弃（`.md` 直接 return；配置文档整体 decode） | 实现 |
| F52 | 顶层 `permissions` 对 plan 的影响 | 配置加载 | `packages/core/src/config/plugin/agent.ts:84-92`；`packages/core/src/config.ts:230-237` | 所有文档的顶层 permissions 先追加到所有已有 agent（含 plan），再处理 `agents` map/.md；因 last-match-wins 可覆盖内置 plan 规则 | 实现 |
| F53 | synthetic 提醒的持久化形态 | 提醒注入 | `packages/core/src/session/session.ts:273-314`；`packages/core/src/session/inbox.ts:277-333`；`packages/core/src/session/projector.ts:609-638`；`packages/schema/src/session-message.ts:83-89` | 先成为 `session_inbox` 的 synthetic 项；runner promote 后投影为 `session_message.type="synthetic"`；`toLLMMessages` 再映射为 user 消息 | 实现 |
| F54 | TUI 权限 reject 细节 | 权限框 | `packages/tui/src/routes/session/permission.tsx:248-254` | 主会话直接 reject（无 feedback）；子会话（有 parentID）进入 reject 输入框，可带 feedback | 实现 |
| F55 | 其他客户端的 agent 切换 | ACP/desktop/mini | `packages/cli/src/acp/service.ts:109-116`；`packages/app/src/composer/submit.ts:386-399`；`packages/tui/src/mini/stream-v2.transport.ts:1798-1834` | 均通过 `session.switchAgent`/server API；语义与 TUI 相同 | 实现 |
| F56 | plan 的默认系统提示仍含 edit/write 指导 | plan 请求 | `packages/core/src/session/system-prompt.ts:9-27`；`packages/core/src/session/model-request.ts:100-120`；`packages/core/src/tool.ts:257-262`；`packages/core/src/plugin/plan.ts:39-40` | `baseTranscript` 用 snapshot 的 `tools.definitions` 生成默认 system，因此默认 prompt 会加入 write/edit 指导语；模型适配 prompt 可能按 hook 后的工具集重渲染；限制来自 reminder+权限，不是 system | 实现 |
| F57 | plan 目录写权限无条件 | 用户未明确要求时 | `packages/core/src/plugin/plan.ts:39-41`；`packages/core/src/tool/plugin/write.ts:60-61,78-87` | `edit`/`external_directory` 对 plan 目录 allow；工具不检查“用户明确要求”，只要路径在 plan 目录就可写 | 实现 |
| F58 | 历史 todowrite 的 UI | 渲染旧消息 | `packages/session-ui/src/timeline/projection.ts:608`；`packages/session-ui/src/tools/tool-renderer.tsx:1313` | `renderable` 过滤掉 todowrite；`ToolDisplay` 直接 return null；仅 `toolTitle` 保留 case | 实现（隐藏） |
| F59 | 自定义 agent `disabled` | 配置 | `packages/schema/src/config/agent.ts:17`；`packages/core/src/plugin/agent.ts:96-100` | `disabled:true` 删除该 agent（包括内置 plan），后续文档理论上可再创建 | 实现 |
| F60 | `switchAgent` 不校验存在性 | API/CLI/命令 | `packages/core/src/session/session.ts:89-95`；`packages/core/src/session/context.ts:128-129` | 只发布事件；下一次 `context.select` 才以 `AgentNotFoundError` 失败 | 实现 |
| F61 | 请求级 edit/write vs patch 可见性 | 每次 primary/compaction/generate context hook | `packages/core/src/tool/plugin/patch.ts:296-309`；`packages/core/src/session/model-request.ts:236-257,281` | snapshot 里三者都在；PatchTool 的 context hook 再按模型删减：`model.id` 含 `gpt-` 且不含 `oss`/`gpt-4` → 删 `edit`/`write`，保留 `patch`；否则删 `patch`，保留 `edit`/`write`。因此“plan 下 edit/write 是否在请求工具表”依赖模型 | 实现 |

**自定义 agent `.md` 的 frontmatter 字段**（`schema/config/agent.ts:7-19`）：

- native：`model`（`{providerID, model, variant?}`）、`request`（headers/body）、`system`、`description`、`mode`（`subagent|primary|all`）、`hidden`、`color`（6 位 hex）、`steps`、`disabled`、`permissions`（Rule 数组）。
- legacy v1（只要出现任一未知 key 就按 v1 decode，`plugin/agent.ts:186-203`）：`model` 字符串、`variant`、`temperature`、`top_p`、`prompt`、`tools`（bool map）、`disable`、`options`、`maxSteps`、`permission`（v1 map）、`color`（hex 或主题名）、`mode`、`hidden`、`description`、`steps`。
- 正文 `markdown.content.trim()` 被当作 native `system` / legacy `prompt`；空正文会得到 `system:""`，`baseTranscript` 因 falsy 回退默认 system（`model-request.ts:110-113`）。`.md` 的 `name` 取自相对路径去前缀/后缀；`mode/` 目录强制 `mode:"primary"`（`plugin/agent.ts:180-208`）。
- 合并：同一 id 先由内置插件创建，再由配置/`.md` 文档按 entries 顺序 `editor.update`；标量后写覆盖，`permissions` push 追加，`disabled` 删除（`plugin/agent.ts:83-127`）。

---

## 2. 数据流

### 2a. 阶段表

| S-ID | 阶段 | 触发 | 输入 | 处理(file:line) | 状态变化 | 持久化 | 用户可见效果 |
|---|---|---|---|---|---|---|---|
| S1 | 注册内置 agent | 进程/位置启动 | 无 | `plugin/internal.ts:213-246` → `plugin/agent.ts:86-153`、`plugin/plan.ts:27-43` | 内存 Agent state | 无（每次启动重建） | 无 |
| S2 | 加载自定义 agent | 启动/配置变化 | 配置文档、`.md` | `config/plugin/agent.ts:40-127,164-211`；`config.ts:185-237` | agent 表重建 + `agent.updated` | 源文件即持久化 | TUI agent 列表刷新 |
| S3 | 选择 draft | shift+tab / `/agents` / `--agent` | 键位/选择/flag | `tui/context/local.tsx:86-123`；`tui/app.tsx:885-908`；`tui/app.tsx:628` | 本地 `draftBySession` / `current` | 无（内存） | 输入框 agent 标签/边框色变化 |
| S4 | 提交/创建会话 | 回车/命令 | 文本 + draft agent | `tui/component/prompt/index.tsx:1230-1240,1274-1282,1295-1302,1330`；`server/handlers/session.ts:126-140,241-247` | 新会话 `session.created` 带 agent；已有会话 `switchAgent` | durable 事件 | 时间线出现切换提示 |
| S5 | 投影 agent 状态 | S4 | `session.agent.selected` | `session/projector.ts:546-556`；`session/message-updater.ts:90-103` | `session_v2.agent` 更新；追加 `agent-switched` | SQLite | `Switched agent ...` |
| S6 | 加入 plan 提醒 | S4/S1 | 事件 | `plugin/plan.ts:76-97,101-113` → `session/session.ts:273-314` | synthetic inbox 项 | `session_inbox` | 提醒在投递后显示 |
| S7 | 请求前选择 | runner drain | sessionID | `session/runner/llm.ts:165-179`；`session/context.ts:121-159` | 解析 agent、合并权限、`registry.snapshot` | 无 | — |
| S8 | 提醒对账 | context hook | 请求消息 | `plugin/plan.ts:58-74` | 内存 splice + 另递 synthetic | inbox | 可能新增 notice |
| S9 | 组装 system prompt | 每次 attempt | agent + tools | `session/model-request.ts:100-120`；`system-prompt.ts:5-27`；`plugin/optimize.ts:72-90` | system 数组 | 无 | — |
| S10 | 工具调用/权限 | provider tool-call | tool name+input | `session/runner/step.ts:88-129` → 叶子工具 `permission.assert` | allow/ask/deny | ask ephemeral | 工具卡片/权限框 |
| S11 | plan 拒绝 edit | S10 deny | BlockedError | `plugin/plan.ts:45-54` → `tool.ts:131-138` → `to-session-error.ts:63-68` → `to-llm-message.ts:141-149` | 工具 error | assistant 消息内 tool state | `Cannot use ... Plan directory ...` |
| S12 | 用户拒绝 ask | 权限框 | once/always/reject | `permission.ts:265-325`；`runner/step.ts:199-217` | CorrectedError（继续）或 DeclinedError（打断） | ask/reply ephemeral；always 写表 | 权限框结果 |
| S13 | 回合结束 | assistant 完成/中断 | — | `session-message.ts:283-293` | `idle` 消息 | SQLite | 时间线 |
| S14 | fork/revert/压缩/resume | 用户/自动 | 会话/消息 | `projector.ts:147-220,739-774`；`revert.ts`；`compaction.ts:217-221,567-575`；`session/session.ts:269-272` | agent 状态延续；提醒可能丢失后由 S8 补 | SQLite/事件 | 历史变化 |

### 2b. 叙事：用户从 plan 会话发一句话到收尾

1. 用户在 TUI 按 `shift+tab`：`agent.cycle` → `local.agent.move(1)`，只在 `mode !== "subagent" && !hidden` 的 agent 间循环（`tui/app.tsx:903-908`、`tui/context/local.tsx:57-58,114-123`）。此时只改本地 draft，不写服务端（`local.tsx:63-113`）。
2. 用户输入并回车：
   - 若是新会话，`data.session.create({ agent: agent.id })` 直接把 plan 写进 `session.created`（`prompt/index.tsx:1230-1240`；`server/handlers/session.ts:126-140`）。
   - 若是已有会话，`prepareAgent()` 发现 `session.agent !== agent.id` 后调 `session.switchAgent`（`prompt/index.tsx:1274-1282`）。
   - 空回车也会走 `commitSelection()`（`prompt/index.tsx:1295-1302`）。
3. `Session.switchAgent` 只做 `get(session)` + 发布 `session.agent.selected`（`session/session.ts:89-95`）。不校验 agent 是否存在；无效 id 会在下一次 `context.select` 以 `AgentNotFoundError` 失败（`session/context.ts:128-129`）。
4. 事件被两路消费：
   - `projector` 把 `session_v2.agent` 更新为新 agent（`projector.ts:546-556`）；
   - `message-updater` 追加 `agent-switched` 消息（`message-updater.ts:90-103`）；
   - `PlanPlugin` 的 `session.agent.selected` 订阅按 `switchReminder` 决定 enter/leave，并 `ctx.session.synthetic(...)`（`plugin/plan.ts:76-97,101-113`）。
5. `Session.synthetic` 生成一个 synthetic **inbox** 项，默认 delivery=`steer`，`resume:false`（`session/session.ts:273-314`）。它不是在切换瞬间直接写成消息；要等 runner 在下一次 drain 的 `SessionInbox.promote` 中投递，再由 projector 写成 `session_message.type="synthetic"`（`runner/llm.ts:165-179`；`projector.ts:609-638`）。
6. runner 的 `advanceToStep` 先 `prepareContext`（`context.select`），再 promote inbox，最后 `context.load` 读取历史（`runner/llm.ts:165-179`）。`context.select` 解析 `session.agent`、合并 `agent.permissions + session.permissions`、用 `registry.snapshot(permissions)` 生成工具快照（`context.ts:121-159`）。
7. 组装请求：`baseTranscript` 取 `agent.system`（plan 没有）否则默认 system.txt，再拼 `input.initial`（`model-request.ts:100-120`）；OptimizePlugin 在 agent 无自定义 system 时按模型替换/追加 system[0]（`optimize.ts:72-90`）。
8. `context` hook 运行 PlanPlugin 对账：如果请求消息里最后一条 enter/leave 不是 enter 且 agent=plan，就在最后一条 user 消息之前插入 `Message.user(enterReminder)`，并再次 `ctx.session.synthetic({resume:false})`（`plugin/plan.ts:58-74`）。这一步同时有“当前请求内存注入”和“异步持久化”两个动作。
9. 模型正常用 read/glob/grep/shell/webfetch/websearch/skill/subagent/MCP 等调研；这些 action 在 plan 下没有 deny，落到基础 `* * allow`。`.env` 读仍是 ask；外部目录除 plan 目录/全局产物目录外仍是 ask。
10. 若模型试图 `edit`/`write`/`patch` 项目文件：叶子工具先做 external_directory（如需），再 `permission.assert({action:"edit"})`（`edit.ts:180-188`；`write.ts:78-86`；`patch.ts:196-208`）。plan 的 `edit * deny` 命中；`edit <plan>/* allow` 只在 plan 目录命中。
11. deny 产生 `BlockedError`；叶子 `mapError` 包成 `ToolFailure`（`edit.ts:219-223`）；PlanPlugin 的 `execute.after` 把它改写成固定文案（`plugin/plan.ts:45-54`）；`toSessionError` 产出 `tool.execution`（`to-session-error.ts:63-68`），作为 tool result error 回给模型（`to-llm-message.ts:141-149`）。模型继续回合。
12. 如果模型按 enter reminder 拒绝实现，它只是按提示词行事；提醒原文要求“Tell them they need to switch agents”（`plugin/plan.ts:20`），代码里没有任何自动切回 build 的路径。
13. 用户再次 `shift+tab` 到 Build 并提交：`switchAgent` → `session.agent.selected` → projector 改列 + `agent-switched`；PlanPlugin 看到 `previous === plan`，递 leave reminder（`plugin/plan.ts:110-111`）。下一轮请求对账时，如果最后是 leave 就不再补；如果历史里最后仍是 enter 就补 leave。
14. 结束：`idle` 消息入库（`session-message.ts:283-293`）。全程没有“批准”或“自动执行”环节。

---

## 3. 代码地图

| 文件 | 职责 | 关键符号(file:line) |
|---|---|---|
| `packages/core/src/plugin/plan.ts` | plan agent 定义、enter/leave 文本、拒绝改写、每请求对账 | `plan`(:12)；`enter`(:14-21)；`leave`(:23-25)；`Plugin`(:27-99)；`switchReminder`(:101-113)；`lastReminder`(:115-121) |
| `packages/core/src/plugin/agent.ts` | 内置 build/general/explore/compaction/title/summary | `Plugin`(:83-155) |
| `packages/core/src/agent.ts` | Agent 服务：注册表、默认选择、全局 external 允许 | `defaultID`(:18)；`permissions`(:59-64)；`update`(:74-84)；`selectable/selectedDefault`(:92-104)；`select`(:116-123)；`list`(:124-129) |
| `packages/schema/src/agent.ts` | Agent.Info 字段与默认权限 | `Info`(:23-35)；`Info.default`(:39-53)；`Updated`(:11) |
| `packages/core/src/plugin/internal.ts` | 内置插件 pre/post 顺序 | `pre`(:213-247，AgentPlugin:220、PlanPlugin:221)；`post`(:249-..., ConfigAgentPlugin:252) |
| `packages/core/src/plugin/supervisor.ts` | pre→post 激活顺序 | `activate`(:138-155) |
| `packages/core/src/config/plugin/agent.ts` | `.md` 发现/解析、配置合并、default_agent | `legacySources`(:21-24)；`Plugin`(:34-128)；`discover`(:164-175)；`decode`(:177-211) |
| `packages/core/src/config.ts` | 配置来源优先级/entries | `loadDirectory`(:185-192)；`load`(:194-238)；`entries`(:352-354) |
| `packages/core/src/config/discovery.ts` | 配置目录发现 | `names`(:11)；`discover`(:21-68) |
| `packages/core/src/permission.ts` | 权限求值、assert/ask/reply、saved 合并 | `evaluate`(:87-97)；`configured`(:158-163)；`denied`(:165-167)；`evaluateInput`(:173-189)；`assert`(:231-263)；`reply`(:265-325) |
| `packages/schema/src/permission.ts` | Rule/Ruleset/Reply/事件 | `Request`(:35-40)；`Reply`(:41)；`Asked/Replied`(:44-53)；`Effect`(:55-56)；`Rule`(:58-63) |
| `packages/core/src/tool.ts` | 工具注册、snapshot、execute.after | `executeTool`(:113-156)；`snapshot`(:225-287)；`whollyDisabled`(:292-295) |
| `packages/core/src/session/context.ts` | 请求前选择 agent/权限/工具快照 | `select`(:121-161) |
| `packages/core/src/session/model-request.ts` | 请求组装、context hook、executeTool decline 通道 | `baseTranscript`(:100-120)；`executeTool`(:385-399)；`agentHook`(:403-405) |
| `packages/core/src/session/system-prompt.ts` | 默认 system prompt 渲染工具指导 | `make`(:5-7)；`render`(:9-27) |
| `packages/core/src/plugin/optimize.ts` | 按模型替换/追加 system[0] | `make`(:64-92)；hook(:72-90) |
| `packages/core/src/session/session.ts` | switchAgent/synthetic/prompt/resume | `switchAgent`(:89-95)；`prompt`(:145-178)；`synthetic`(:273-314) |
| `packages/core/src/session/message-updater.ts` | durable 事件→消息 | `session.agent.selected`(:90-103)；`session.synthetic`(:161-172) |
| `packages/core/src/session/projector.ts` | durable 事件→SQL | `Forked`(:147-220)；`AgentSelected`(:546-556)；`InboxDelivered`(:609-638)；`Revert.Committed`(:739-774) |
| `packages/schema/src/session-message.ts` | 消息 union | `AgentSelected`(:43-49)；`Synthetic`(:83-89)；`Assistant.agent`(:212-216)；`Info`(:295-307) |
| `packages/schema/src/session-event.ts` | durable/ephemeral 事件 | `Created.agent`(:59)；`AgentSelected`(:69-78)；`Synthetic`(:279-289)；`Forked`(:187-201) |
| `packages/core/src/session/runner/to-llm-message.ts` | 消息→LLM | agent-switched/idle 丢弃(:250-254)；synthetic→user(:284-285)；tool error(:141-149) |
| `packages/core/src/session/to-session-error.ts` | 错误→会话错误 | ToolFailure(:63-68) |
| `packages/core/src/file-access.ts` | 路径解析/external/read 授权 | `resolve`(:93-122)；`authorizeExternal`(:125-141)；`authorizeRead`(:144-160) |
| `packages/core/src/tool/plugin/edit.ts` | edit 工具 | options permission(:123)；assert(:180-188)；mapError(:219-223) |
| `packages/core/src/tool/plugin/write.ts` | write 工具 | options(:59)；assert(:78-86)；parent dirs 描述(:60-61) |
| `packages/core/src/tool/plugin/patch.ts` | patch 工具 + 按模型删减 edit/write/patch 的 context hook | options(:81)；assert(:196-208)；hook(:296-309) |
| `packages/core/src/tool/plugin/shell.ts` | shell 工具 | external/assert(:133-142)；options(:192) |
| `packages/core/src/tool/plugin/subagent.ts` | subagent 工具/权限门/可用列表 | assert(:138-151)；create(:184-193)；available(:288-294) |
| `packages/core/src/tool/plugin/question.ts` | question 工具 | assert(:63-70)；options(:58) |
| `packages/core/src/tool/mcp.ts` | MCP 工具注册/权限 | action name(:47-60)；assert(:57-63) |
| `packages/core/src/tool/plugin/mcp-resource.ts` | MCP resource 工具 | assert(:36-38,65-67) |
| `packages/core/src/tool/plugin/opencode.ts` | session_rename/session_move/models | 工具注册(:85-140)，无 `permission.assert` |
| `packages/core/src/session/compaction.ts` | 压缩请求/摘要 | 使用当前 context.agent(:567-575)；auto(:217-221) |
| `packages/core/src/session/revert.ts` | revert stage/clear/commit | `stage`(:32-64)；`clear`(:66-80)；`commit`(:82-88) |
| `packages/tui/src/app.tsx` | TUI 命令注册 | `/agents`(:885-891)；cycle(:903-908)；reverse(:937-943) |
| `packages/tui/src/context/local.tsx` | TUI agent draft/列表/颜色 | `createAgent`(:56-134)；draft(:63-74,86-95)；set/move(:97-123)；color(:125-131) |
| `packages/tui/src/component/prompt/index.tsx` | 提交时切换、标签/边框 | create agent(:1230-1240)；prepareAgent(:1274-1282)；空回车(:1295-1302)；label/color(:1547-1551,1565-1570,1585,1661,1852) |
| `packages/tui/src/component/dialog-agent.tsx` | `/agents` 选择框 | options(:8-20)；onSelect(:20-23) |
| `packages/tui/src/routes/session/{index,permission}.tsx` | 时间线/权限框 | assistant label(:1881-1884)；switch notice(:1917-1925)；user color(:2200)；permission choices(:232-257) |
| `packages/cli/src/run/noninteractive.ts` | run --agent/auto/reject | switchAgent(:690-692)；reply auto/reject(:164-183) |
| `packages/cli/src/acp/service.ts` | ACP mode→agent | `selectMode`(:109-116) |
| `packages/server/src/handlers/{agent,session}.ts` | agent API/switchAgent | agent.list/get(:8-25)；session.switchAgent(:241-247) |
| `packages/client/src/solid/data.ts` | agent.updated 刷新 | case(:1216-1219) |
| `packages/core/src/database/v1-migration.bun.ts` | v1→v2 工具迁移 | RENAMED(:899)；REMOVED(:901)；legacy notice(:908-935) |
| `packages/core/src/v1/config/{agent,migrate}.ts` | v1 agent 配置迁移 | tools→permission(:50-59)；action 改名(:27-33) |
| `packages/core/src/config/normalize.ts` | 顶层 tools→permission | `migrateTools`(:487-497) |
| `packages/util/src/global.ts` / `global-roots.ts` | home/XDG 路径 | `home`(:16-18)；`roots`(:8-18) |

---

## 4. 状态与持久化

- **会话级 agent**：`session_v2.agent` 列（`session/sql.ts:53`），由 durable `session.agent.selected` 通过 projector 写入（`session/projector.ts:546-556`）。`Session.Info.agent` 是 optional（`schema/session.ts:39`）。
- **会话级权限**：`session_v2.permission` JSON 列（`session/sql.ts:52`），`Session.setPermissions` 发布 durable `session.permissions`（`session/session.ts:82-88`），projector 写入（`projector.ts` 的 Permissions 处理）。
- **消息级**：
  - `agent-switched`：`{type, agent, previous?}`（`schema/session-message.ts:43-49`），由 message-updater 追加（`message-updater.ts:90-103`），有 id/time/metadata。
  - 每条 assistant：`agent: Agent.ID`（`schema/session-message.ts:212-216`）。
  - synthetic 提醒：消息类型 `synthetic`（`schema/session-message.ts:83-89`），先进入 `session_inbox`，投递后写 `session_message`（`session/session.ts:273-314`；`projector.ts:609-638`）。`toLLMMessages` 把它映射成 **user role** 的文本（`to-llm-message.ts:284-285`）。
  - `agent-switched` 与 `idle` **不进入 LLM 上下文**（`to-llm-message.ts:250-254`）。模型对 agent 切换的感知只来自 plan 的 synthetic 提醒；切到非 plan agent 时没有提醒。
- **权限表**：
  - `permission.asked`/`permission.replied` 是 ephemeral，不入库（`schema/permission.ts:44-53`）。
  - “always”写入项目级 `permission` 表：`id, project_id, action, resource, time_*`，unique(project,action,resource)（`permission/sql.ts:8-25`；`permission/saved.ts:55-78`）。
  - saved 规则在读取时一律映射为 `effect:"allow"`（`permission.ts:148-156`）。
- **agent 表**：不是数据库表，而是 `State.create` 的内存重建结果；每次配置变化 `ctx.agent.reload()` 后从 `initial()` 重放所有 transform（`state.ts:160-180`；`config/plugin/agent.ts:55-58`）。因此 reload 不会把同一份 permissions 永久重复追加。
- **agent.updated**：ephemeral（`schema/agent.ts:11`），客户端收到后 invalidate/sync（`client/src/solid/data.ts:1216-1219`）。
- **fork**：projector 插入新 session 行，复制 `agent`、`model`、`metadata`、`permission`（`projector.ts:147-164`），并复制边界前的 `session_message` 行（`:183-220`）；不复制 `session_inbox` pending 项。
- **revert**：commit 删除边界及之后的 `session_message` 与 `session_inbox`（`projector.ts:739-766`），只清 `revert` 字段（`:767-772`），不修改 `agent`。所以“agent 仍是 plan，但 enter 提醒被 revert 删掉”的情况由每请求对账修复（`plugin/plan.ts:56-58`）。
- **compaction**：使用当前会话选中的 agent 构造压缩请求（`compaction.ts:567-575`）；PlanPlugin 只注册 `context` hook，不注册 `compaction` hook（`plugin/plan.ts:58`）；摘要可能把提醒文本包进 `<conversation-checkpoint>`，对账只认“单独一条、唯一 text part、文本完全等于 enter/leave”的消息，所以引用不算 live（`plugin/plan.ts:115-121`；测试 `plan.test.ts:232-245`）。压缩后下一次 primary 请求会补提醒。
- **resume**：`Session.resume` 只重新唤醒 runner（`session/session.ts:269-272`）；agent 沿用 `session_v2.agent`；提醒对账仍发生在下一次 context hook。
- **share**：`session_v2.share_url` 存在（`session/sql.ts:39`），本次未见与 agent/plan 相关的特殊处理；未深入。
- **TUI draft**：`draftBySession` 是内存 store（`tui/context/local.tsx:63-65`），不落盘；收到匹配的 `session.agent.selected` 后把 draft 值清成 `{agent: undefined}`（`:68-73`）。`--agent` 是启动 draft（`tui/app.tsx:628`）。
- **TUI 展示**：
  - 输入框标签 = `Locale.titlecase(agent.id)`（plan 显示 `Plan`），边框色取 `local.agent.color(id)`（`prompt/index.tsx:1547-1551,1585,1661`）。
  - `Agent.Info.color` 是可选的 6 位 hex（`schema/agent.ts:32`；`schema/config/agent.ts:6`）；plan 没有设置 color，所以 TUI 的 `color()` 按 `visibleAgents()` 的位置从主题 categorical 调色板取色（`local.tsx:60-61,125-131`），不是固定的 plan 专用色。session-ui 另有 `plan` 的 tone 映射用于工具/agent 展示（`session-ui/src/tools/tool-renderer.tsx:187-198`）。
  - assistant 消息底部用 `message.agent` 着色显示 agent 名（`tui/routes/session/index.tsx:1881-1884`）；用户消息边框用当前 session agent 的颜色（`:2200`）。
  - 通知：TUI 的通用 notification 插件只处理 session done / permission asked / question/form，未按 agent 或 plan 分支；subagent 会话会关闭桌面通知（`tui/feature-plugins/system/notifications.ts:5-17,31-62`）。没有“进入 plan”或“离开 plan”的专门通知。

---

## 5. 工具与权限矩阵

### 5.1 规则语言与精确求值

- Rule=`{action, resource, effect:"allow"|"deny"|"ask"}`（`schema/permission.ts:58-63`）。
- `evaluate(action, resource, ...rulesets)`：把所有 ruleset flatten 后 `findLast(rule => Wildcard.match(action, rule.action) && Wildcard.match(resource, rule.resource))`；无匹配默认 `{action, resource:"*", effect:"ask"}`（`permission.ts:87-97`）。**规则顺序即优先级，后写覆盖先写。**
- `Wildcard.match`：输入和 pattern 的 `\` 归一为 `/`；转义正则特殊字符；`*`→`.*`，`?`→`.`；正则用 `s` flag（可跨行/跨路径段）；Windows 额外 `i`；pattern 以 `" .*"` 结尾时把该段变成 `"( .*)?"`（`util/wildcard.ts:3-13`）。最后这条使 `git status *` 可匹配不带参数的 `git status`。
- 多 resource 的 deny 短路：`denied()` 对每个 resource 求值，**任一 deny 即整个 assert 为 deny**（`permission.ts:165-167`）。因此 patch 同时触及 plan 目录和项目文件时，整个 patch 会被拒，而不是只允许 plan 目录部分。
- 合并顺序：`configured = merge(agent.permissions ?? missingAgentPermissions, session.permissions ?? [])`（`permission.ts:158-163`）→ 先 `denied(configured)` → `all = configured + savedRules()` → 每 resource `evaluate(..., all)` → 任一 ask 则 ask，否则 allow（`:173-178`）→ 最后触发 `permission.evaluate` hook，hook 可改 effect/message（`:179-188`）。
- saved 规则只可能是 allow（`permission.ts:148-156`）；而且 deny 检查在拼 saved **之前**，所以 **saved allow 不能覆盖 agent/session 的 deny**（测试 `permission.test.ts:263-281`）。
- 找不到 agent 时用 `missingAgentPermissions = [{action:"*",resource:"*",effect:"deny"}]`（`permission.ts:19,161-162`）。
- plugin hook 是第四层：`ctx` 的 `permission.evaluate` 可以在 agent/session/saved 之后改 effect（`permission.ts:179-188`）。

### 5.2 内置 agent 的权限基底（按追加顺序）

所有 agent 首次创建时：

1. `* * allow`
2. `external_directory * ask`
3. `read *.env ask`
4. `read *.env.* ask`
5. `read *.env.example allow`

见 `schema/agent.ts:46-52`。

随后 `Agent` 服务追加 4 条全局 external allow：

6. `<data>/shell/*/*`
7. `<data>/tool-output/*`
8. `<tmp>/*`
9. `<config>/*`

见 `core/agent.ts:59-64,74-84`。

各 agent 再追加：

- **build**：`question * allow`（`plugin/agent.ts:87-92`）。
- **general**：`question * deny`、`subagent * deny`（`:94-103`）。
- **explore**：`* * deny` → grep/glob/webfetch/websearch/read allow、`read .env* ask`、`subagent * deny`、`external_directory * ask` + 原全局 external allow（`:105-131`）。
- **compaction**：无追加，hidden（`:133-137`）。
- **title/summary**：`* * deny`，hidden（`:139-153`）。
- **plan**：`question * allow`、`edit * deny`、`edit <home>/.opencode/plan/* allow`、`external_directory <home>/.opencode/plan/* allow`（`plugin/plan.ts:38-41`）。

所以未加任何用户配置时，plan 的规则数 = 5 + 4 + 4 = **13**。用户配置的顶层 `permissions` 会先追加到所有已有 agent，再追加该 agent 的 `permissions`；因为 last-match-wins，可以覆盖内置 plan 的 default（`config/plugin/agent.ts:84-123`）。`disabled:true` 会直接删除 agent（`:96-100`）。

### 5.3 工具 × plan 的最终 effect

| 工具/操作（action） | plan 最终 effect | 规则/证据 | 备注 |
|---|---|---|---|
| read（项目内普通文件） | allow | 基础 `* * allow`；`file-access.ts:154-159` `read` assert | 先 external 检查；项目内不触发 external |
| read `.env` / `.env.*` | ask | `schema/agent.ts:49-50`；plan 无 read 覆盖 | `.env.example` 最后 allow（`:51`） |
| read 项目外文件 | external ask → read allow/ask | `file-access.ts:149-159`；plan 仅 plan 目录 external allow（`plan.ts:41`） | 外部目录先问，再按 read 规则 |
| glob / grep | allow | `glob.ts:68-72`；`grep.ts:87-92`；基础 `* * allow` | 外部 path 先 external ask |
| edit / write / patch（action=`edit`） | deny；plan 目录内 allow | `plan.ts:39-40`；`edit.ts:180-188`；`write.ts:78-86`；`patch.ts:196-208` | snapshot 里三者都在；请求工具表按模型只保留 edit/write 或 patch 之一，见 §5.4；GPT 系保留 patch 时，拒绝文案为 `Cannot use patch ...` |
| shell | allow；外部目录 ask | `shell.ts:133-142`；plan 无 shell 规则 | 按 parsed commands 断言；可修改项目文件；不是只读沙箱 |
| webfetch | allow | `webfetch.ts:124-132`；基础 allow | resource=URL，save=`["*"]` |
| websearch | allow | `websearch.ts:49-57`；基础 allow | resource=query |
| skill | allow | `skill.ts:51-58`；基础 allow |
| question | allow（显式） | `plan.ts:38`；`question.ts:63-70` | 基础本来就 allow；显式规则把最后的 question 规则固定为 allow |
| subagent | allow | `subagent.ts:138-151`；plan 无 subagent 规则 | 子 agent 自己的权限才决定它能做什么 |
| external_directory（plan 目录） | allow | `plan.ts:41`；`file-access.ts:135-141` | `path.join(directory,"*")`；`*` 跨路径段，子目录也匹配 |
| external_directory（其他） | ask | `schema/agent.ts:48`；`core/agent.ts:60-63` | 全局 data/tmp/config 目录 allow |
| codemode `execute` | allow | `tool.ts:238-245`；基础 `* * allow`；`whollyDisabled("execute")` 为 false | plan 没有 `* * deny` |
| opencode `session_rename`/`session_move`/`models` | 无 assert | `tool/plugin/opencode.ts:85-140`（grep 无 `permission.assert`） | codemode 工具；只受 `execute` 权限约束 |
| MCP resource `opencode_list_mcp_resources`/`opencode_read_mcp_resource` | allow | `mcp-resource.ts:36-38,65-67`；基础 allow | action 名经 effectiveName 加 namespace |
| MCP server 工具（`server_tool`） | allow | `tool/mcp.ts:47-63`；基础 allow | 多数 MCP 工具 codemode 默认 true，经 `execute` 调用；assert 仍按 action 名 |
| todo/todowrite | 不存在 | `v1-migration.bun.ts:901`；`session-ui` 过滤 | v2 无此工具 |
| 写 plan 目录 | external allow + edit allow | `plan.ts:39-41`；`write.ts:60-61,78-87` | 工具不检查“用户明确要求”；权限本身无条件允许 |

### 5.4 工具可见性与拒绝执行

- 注册表本身不做授权（`tool.ts` 没有 Permission 依赖；`tool/AGENTS.md` 也写明）。授权发生在每个叶子工具内部的 `permission.assert`。
- `context.select` 只把 `agent.permissions + session.permissions` 传给 `registry.snapshot`，用于**裁目录**（`context.ts:128-134`），不拦执行。
- `snapshot`：对每个 tool，取 `tool.options?.permission ?? name` 作为 action，调用 `whollyDisabled`；满足“最后一条匹配该 action 的规则 resource===`"*"` 且 effect==="deny"”才从 active 中移除（`tool.ts:225-233,292-295`）。
- `edit`/`write`/`patch` 都声明 `options.permission:"edit"`（`edit.ts:123`；`write.ts:59`；`patch.ts:81`），所以三者共享同一可见性判断。
- plan 的 edit 规则最后一条是 `edit <plan>/* allow`（不是 `edit * deny`），所以 `whollyDisabled("edit", plan.permissions)` 为 **false**；edit/write/patch 都进入 registry snapshot 的 `definitions`。`[verified: tool.ts:292-295 + plan.ts:39-40]`
- **但 snapshot 不是最终请求工具表**：PatchTool 注册了一个 `context/compaction/generate` hook，在请求组装时按模型再删减一次（`patch.ts:296-309`）：
  - `model.id` 含 `gpt-`、且不含 `oss`、且不含 `gpt-4` → `delete event.tools.edit; delete event.tools.write`，保留 `patch`；
  - 其他模型 → `delete event.tools.patch`，保留 `edit`/`write`。
  这个 hook 对所有 agent 生效，不是 plan 专有；`model-request.ts:236-257,281` 会把 hook 后的 `shaped.tools` 作为实际请求工具表。因此“plan 下 edit/write 是否在工具表”必须按模型回答：GPT 系（非 oss/gpt-4）是 `patch` 在、edit/write 不在；其余模型是 edit/write 在、patch 不在。`[verified: patch.ts:296-309 + model-request.ts:252-281]`
- 如果用户配置把 `edit * deny` 追加到 plan 规则之后，则三者会从 snapshot 中消失；如果只追加 `edit src/* deny`，工具仍在，运行时按路径 deny。
- `execute` 单独判断：`codeModeEnabled = !whollyDisabled("execute", rules)`（`tool.ts:238`）。plan 的基础 `* * allow` 使其为 true；explore 的 `* * deny` 使其为 false。
- 直接工具（`options.codemode===false`）进入 `definitions`；其余进入 code mode catalog（`tool.ts:234-262`）。edit/write/patch/read/glob/grep/shell/webfetch/websearch/skill/question/subagent 都是直连；`opencode_*`/MCP resource/MCP 工具是 codemode。
- `execute.after` 改写只针对 `event.agent === plan`、`status==="error"`、`tool in {edit,write,patch}`、`event.error.error instanceof Permission.BlockedError`（`plugin/plan.ts:45-54`）。因此：
  - 由 plan 自定义 deny 规则触发的 BlockedError 也会被改写成“outside the Plan directory”，文案不区分具体原因；
  - 子 agent（如 general）的 BlockedError 不会被改写，因为 `event.agent` 不是 plan。

### 5.5 叶子工具的 assert 示例

- **edit**：`access.resolve({path, kind:"file"})` → `access.authorizeExternal([target], context)` → `permission.assert({action:"edit", resources:[target.resource], save:["*"], ...})` → 写盘（`edit.ts:146-188`）。`write` 同构（`write.ts:71-87`）；`patch` 先解析多个目标，再一次性 assert 所有 target resource（`patch.ts:191-208`）。
- **shell**：解析 cwd、命令和目录 → `access.authorizeExternal([target, ...directories], context)` → 对 `parsed.commands` 逐条 `permission.assert({action:"shell", resources:[command.resource], save:[command.save]})`（`shell.ts:120-142`）。没有解析出命令时不做 shell assert（但 external_directory 仍可能发生）。`[verified: shell.ts:134-142]`
- **read**：`access.authorizeRead(file, context)` 内部先 `authorizeExternal`，再 `permission.assert({action:"read", resources:[target.resource], save:["*"]})`（`file-access.ts:144-160`；`read.ts:55`）。

### 5.6 ask → once/always/reject 的精确流程

1. `permission.assert` → `evaluateInput`；若 effect 是 ask，`create(request, agent)` 在内存 `pending` map 中放一个 `Deferred`，发布 ephemeral `permission.asked`，然后 `Deferred.await`（`permission.ts:204-219,231-260`）。
2. `reply("once")`：`Deferred.succeed`，删除 pending（`permission.ts:306-307`）。
3. `reply("always")`：若 `request.save` 非空，先 `saved.add({projectID, action, resources: request.save})`，再 succeed；随后遍历同批 pending，对每个重新 `evaluateInput`，若现在为 allow 则自动 succeed 并发布 `always`（`permission.ts:299-321`）。注意保存的是工具传入的 `save`（edit/write/patch/read 为 `["*"]`；shell 为解析出的命令前缀；subagent 为 agent id；external 为项目根 `*`），不是实际被询问的 resource。
4. `reply("reject")`：若带 `message` → `CorrectedError(feedback)`；否则 → `DeclinedError`。两者都会把同一 session 的所有 pending ask 一起失败；`CorrectedError` 由叶子工具转成模型可见的 ToolFailure 并继续，`DeclinedError` 走 defect tunnel 到 `SessionModelRequest.executeTool`，随后 step 标记 aborted/中断（`permission.ts:276-297`；`runner/step.ts:199-217`；`model-request.ts:385-399`）。
5. TUI：`request.save?.length` 时显示 once/always/reject，否则只显示 once/reject（`tui/routes/session/permission.tsx:232-257`）。主会话的 reject 直接 `reply("reject")`（无 feedback），子会话（`parentID` 存在）才进入 RejectPrompt 输入 feedback（`:248-254`）。CLI 非交互：`--auto` 回 once；否则非 v1 兼容模式回 reject + `PERMISSION_REJECTED_FEEDBACK` 让模型继续（`noninteractive.ts:164-183`）。

---

## 6. 关键源码摘录

### 6.1 plan agent 定义与 enter/leave 全文（`packages/core/src/plugin/plan.ts:12-54`）

```ts
const plan = Agent.ID.make("plan")

const enter = (directory: string) => `<system-reminder>
You are in Plan mode. Discuss the plan with the user directly in the conversation. Do not create or update plan files unless the user explicitly asks you to; when they do, write them only in:
${directory}

Do not modify any other files or ask a subagent to do so.

You remain in Plan mode until the user switches agents. If the user asks you to implement changes, do not do so. Tell them they need to switch agents.
</system-reminder>`

const leave = `<system-reminder>
You are NO LONGER in Plan mode. The previous Plan restrictions no longer apply. Any Plan mode instructions from earlier in this conversation are no longer active.
</system-reminder>`

export const Plugin = define({
  id: "opencode.plan",
  effect: Effect.fn(function* (ctx) {
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

### 6.2 拒绝文案改写 + 每请求对账（`packages/core/src/plugin/plan.ts:45-74`）

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
      // Before the user's prompt, matching where agent-switch reminders land.
      const at = event.messages.at(-1)?.role === "user" ? event.messages.length - 1 : event.messages.length
      event.messages.splice(at, 0, Message.user(text))
      return ctx.session
        .synthetic({ sessionID: event.sessionID, text, resume: false })
        .pipe(
          Effect.catchCause((cause) =>
            Effect.logWarning("failed to persist Plan mode reminder", { sessionID: event.sessionID, cause }),
          ),
        )
    })
```

### 6.3 switch 提醒与 lastReminder（`packages/core/src/plugin/plan.ts:101-121`）

```ts
function switchReminder(
  event: SessionEvent.Created | SessionEvent.AgentSelected,
  enterReminder: string,
): string | undefined {
  if (event.type === "session.created") {
    if (event.data.agent !== plan) return undefined
    return enterReminder
  }
  if (event.data.agent === event.data.previous) return undefined
  if (event.data.agent === plan) return enterReminder
  if (event.data.previous === plan) return leave
  return undefined
}

function lastReminder(messages: ReadonlyArray<Message>, enterReminder: string) {
  return messages.reduce<string | undefined>((found, message) => {
    const part = message.role === "user" && message.content.length === 1 ? message.content[0] : undefined
    if (part?.type !== "text") return found
    return part.text === enterReminder || part.text === leave ? part.text : found
  }, undefined)
}
```

### 6.4 Agent 默认权限 + 全局 external（`packages/schema/src/agent.ts:39-53`；`packages/core/src/agent.ts:59-84`）

```ts
// schema/agent.ts
default: (id: ID) => ({
  id,
  name: Name.make(id),
  request: { settings: {}, headers: {}, body: {} },
  mode: "primary",
  hidden: false,
  permissions: [
    { action: "*", resource: "*", effect: "allow" },
    { action: "external_directory", resource: "*", effect: "ask" },
    { action: "read", resource: "*.env", effect: "ask" },
    { action: "read", resource: "*.env.*", effect: "ask" },
    { action: "read", resource: "*.env.example", effect: "allow" },
  ],
})

// core/agent.ts
const permissions: Info["permissions"] = [
  { action: "external_directory", resource: SHELL_OUTPUT_GLOB(global.data), effect: "allow" },
  { action: "external_directory", resource: TOOL_OUTPUT_GLOB(global.data), effect: "allow" },
  { action: "external_directory", resource: path.join(global.tmp, "*"), effect: "allow" },
  { action: "external_directory", resource: path.join(global.config, "*"), effect: "allow" },
]
// editor.update(...)
const defaults = Info.default(id)
const current = editor.agents.get(id) ?? {
  ...defaults,
  permissions: [...defaults.permissions, ...permissions],
}
```

### 6.5 权限求值/合并/assert（`packages/core/src/permission.ts:87-97,158-188,231-263`）

```ts
export function evaluate(action: string, resource: string, ...rulesets: Permission.Ruleset[]): Permission.Rule {
  return (
    rulesets.flat().findLast(
      (rule) => Wildcard.match(action, rule.action) && Wildcard.match(resource, rule.resource),
    ) ?? { action, resource: "*", effect: "ask" }
  )
}

const configured = Effect.fnUntraced(function* (sessionID, agentID) {
  const session = yield* sessions.get(sessionID)
  const agent = yield* agents.resolve(agentID ?? session.agent)
  return merge(agent?.permissions ?? missingAgentPermissions, session.permissions ?? [])
})

const evaluateInput = Effect.fnUntraced(function* (input: AssertInput) {
  const rules = yield* configured(input.sessionID, input.agent)
  if (denied(input, rules)) return { effect: "deny" as const, rules }
  const all = [...rules, ...(yield* savedRules())]
  const effects = input.resources.map((resource) => evaluate(input.action, resource, all).effect)
  const effect: Permission.Effect = effects.includes("ask") ? "ask" : "allow"
  const event = yield* hooks.trigger("permission", "evaluate", { ..., effect })
  return { effect: event.effect, message: event.message, rules: all }
})

// assert:
if (result.effect === "deny") return yield* new BlockedError({ ... })
if (result.effect === "allow") return
const item = yield* create(request(input, result.message), input.agent)
return yield* restore(Deferred.await(item.deferred)).pipe(
  Effect.catchTag("Permission.DeclinedError", (error) => Effect.die(error)),
  ...
)
```

### 6.6 工具 snapshot 与 whollyDisabled（`packages/core/src/tool.ts:225-233,292-295`）

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

### 6.7 switchAgent 三件套（`session/session.ts:89-95`；`message-updater.ts:90-103`；`projector.ts:546-556`）

```ts
const switchAgent = Effect.fn("Session.switchAgent")(function* (sessionID, input) {
  const session = yield* get(sessionID)
  yield* bus.publish(SessionEvent.AgentSelected, { sessionID, agent: input.agent, previous: session.agent })
})
```

```ts
"session.agent.selected": (event) =>
  Effect.gen(function* () {
    const previous = event.data.previous ?? (yield* adapter.getAgent())
    yield* adapter.appendMessage(
      SessionMessage.AgentSelected.make({
        id: SessionMessage.ID.fromEvent(event.id),
        type: "agent-switched",
        metadata: event.metadata,
        agent: event.data.agent,
        previous,
        time: { created },
      }),
    )
  }),
```

```ts
yield* bus.project(SessionEvent.AgentSelected, (event) =>
  Effect.gen(function* () {
    yield* run(db, event)
    yield* db.update(SessionTable)
      .set({ agent: event.data.agent, time_updated: event.created })
      .where(eq(SessionTable.id, event.data.sessionID)).run().pipe(Effect.orDie)
  }),
)
```

### 6.8 LLM 消息映射：synthetic→user，agent-switched→丢弃（`to-llm-message.ts:250-285`）

```ts
switch (message.type) {
  case "agent-switched":
  case "idle":
    return []
  ...
  case "user":
    ...
  case "synthetic":
    return [Message.make({ id: message.id, role: "user", content: message.text })]
  case "skill":
    return [Message.make({ id: message.id, role: "user", content: message.text, metadata: message.metadata })]
  case "system":
    return [Message.system(message.text)]
```

### 6.9 自定义 agent 合并（`config/plugin/agent.ts:83-127`）

```ts
yield* ctx.agent.transform((editor) => {
  const permissions = expandPermissions(
    loaded.documents.flatMap((document) => document.info.permissions ?? []),
    global.home,
  )
  const configuredDefault = Config.latest(loaded.documents, "default_agent")
  if (configuredDefault !== undefined) editor.default(Agent.ID.make(configuredDefault))
  for (const current of editor.list()) {
    editor.update(current.id, (agent) => agent.permissions.push(...permissions))
  }

  for (const document of loaded.documents) {
    for (const [id, item] of Object.entries(document.info.agents ?? {})) {
      const agentID = Agent.ID.make(id)
      if (item.disabled) { editor.remove(agentID); continue }
      const exists = editor.get(agentID) !== undefined
      editor.update(agentID, (agent) => {
        if (!exists) agent.permissions.push(...permissions)
        if (item.model !== undefined) agent.model = { ... }
        if (item.system !== undefined) agent.system = item.system
        if (item.description !== undefined) agent.description = item.description
        if (item.mode !== undefined) agent.mode = item.mode
        if (item.hidden !== undefined) agent.hidden = item.hidden
        if (item.color !== undefined) agent.color = item.color
        if (item.steps !== undefined) agent.steps = item.steps
        if (item.permissions !== undefined) agent.permissions.push(...expandPermissions(item.permissions, global.home))
      })
    }
  }
})
```

### 6.10 子 agent 权限门与继承（`subagent.ts:134-151,184-193`；`session.ts:284-288`）

```ts
const agent = yield* agents.resolve(input.agent)
if (agent === undefined) return yield* new ToolFailure({ message: `Unknown agent: ${input.agent}` })
if (agent.mode === "primary")
  return yield* new ToolFailure({ message: `Agent ${input.agent} cannot run as a subagent` })
yield* permission.assert({
  action: name,                    // "subagent"
  resources: [agent.id],
  save: [agent.id],
  sessionID: context.sessionID,
  agent: context.agent,
  source: { type: "tool", messageID: context.messageID, id: context.id },
}).pipe(Effect.mapError((error) => new ToolFailure({ message: `Subagent denied: ${agent.id}`, error })))
...
const child = existing ?? (yield* sessions.create({
  parentID: context.sessionID,
  title: input.description,
  agent: Agent.ID.make(input.agent),
  model,
}))
```

```ts
// Session.create
metadata: input.metadata ?? parent?.metadata,
permissions: input.permissions ?? parent?.permissions,
```

### 6.11 system prompt + optimize（`model-request.ts:100-120`；`optimize.ts:72-90`）

```ts
return {
  providerMetadataKey,
  system: [
    input.agent.system
      ? input.agent.system
      : SessionSystemPrompt.make(input.tools.definitions.map((tool) => tool.name)),
    input.initial,
  ].filter((part) => part.length > 0).map(SystemPart.make),
  messages: toLLMMessages(input.messages, input.model.ref, providerMetadataKey),
}
```

```ts
const hook = (event: SessionHooks["context"]) =>
  Effect.gen(function* () {
    const model = ...
    const template = optimize(model, event.tools)
    if (!template) return
    if ((yield* ctx.agent.get({ agentID: event.agent })).data.system) return
    const system = event.system[0]
    if (!system) return
    const rendered = SessionSystemPrompt.render(template, Object.keys(event.tools))
    const text = mode === "append" ? `${system.text}\n\n${rendered}` : rendered
    event.system[0] = { ...system, text }
  }).pipe(Effect.catch(() => Effect.void))
```

### 6.12 TUI 提交时切换 + draft（`prompt/index.tsx:1274-1282,1295-1302`；`local.tsx:86-113`）

```ts
const prepareAgent = async () => {
  if (!session) {
    await data.session.sync(target)
    session = data.session.get(target)
  }
  if (session?.agent !== agent.id) {
    await client.api.session.switchAgent({ sessionID: target, agent: agent.id })
  }
}
...
if (!trimmed) {
  // Blank Enter in an existing session commits the composer's agent and model selection...
  await attempt("Failed to prepare session", async () => {
    await commitSelection()
    await props.onEmptySubmit?.()
  })
  return true
}
```

```ts
current() {
  const draft = route.data.type === "session" ? agentStore.draftBySession[route.data.sessionID] : undefined
  const selected =
    route.data.type === "session"
      ? (draft?.agent ??
        (draft ? undefined : args.agent) ??
        data.session.get(route.data.sessionID)?.agent ??
        agentStore.current)
      : agentStore.current
  return agents().find((agent) => agent.id === selected) ?? agents().at(0)
},
set(id: string) {
  if (!agents().some((agent) => agent.id === id)) return toast.show({ ... })
  batch(() => {
    const changed = this.current()?.id !== id
    if (changed) model.remember()
    setAgentStore("current", id)
    if (route.data.type === "session") setAgentStore("draftBySession", route.data.sessionID, { agent: id })
    ...
  })
},
```

### 6.13 edit 拒绝链路（`edit.ts:180-188,219-223`；`to-session-error.ts:63-68`）

```ts
yield* permission.assert({
  action: "edit",
  resources: [target.resource],
  save: ["*"],
  metadata: preview ? { files: [preview] } : undefined,
  sessionID: context.sessionID,
  agent: context.agent,
  source: permissionSource,
})
...
Effect.mapError((error) =>
  error instanceof ToolFailure
    ? error
    : new ToolFailure({ message: `Unable to edit ${input.path}`, error }),
)
```

```ts
if (cause instanceof ToolFailure || cause instanceof Tool.Error) {
  if (cause.error === undefined) return { type: "tool.execution", message: cause.message }
  const unwrapped = toSessionError(cause.error)
  return unwrapped.message === "" ? { ...unwrapped, type: "tool.execution", message: cause.message } : unwrapped
}
```

---

## 7. 计划-执行衔接结论

### 7.1 没有 approval / auto-execute

结论：**opencode 本版本没有 plan → approve → auto-execute，也没有任何“计划批准”状态。**

证据：

- 消息 union 只有 `AgentSelected | ModelSelected | LocationSwitched | User | Synthetic | System | Skill | Shell | Assistant | Compaction | Idle`，没有 plan/approve 类型（`schema/session-message.ts:295-307`）。
- durable 事件清单（`session-event.ts:652-690`）没有 approve/plan 事件；只有 `session.agent.selected` 等。
- 生产入口的 `switchAgent` 调用（`grep -rn switchAgent packages`，不含测试/SDK wrapper）只有：TUI 提交（`prompt/index.tsx:1280`）、CLI run（`noninteractive.ts:691`）、server API（`handlers/session.ts:244`）、command plugin（`command.ts:126`）、ACP（`acp/service.ts:115`）、desktop/mini 客户端、subagent continue（`subagent.ts:175`）。没有 plan→build 的自动路径。
- plan 的 enter 提醒明确要求模型“Tell them they need to switch agents”（`plan.ts:20`）；没有任何 UI 按钮/快捷键专门“批准计划”，用户只能用通用的 agent 切换（shift+tab、`/agents`、`--agent`）。
- `grep -rn "approve\|approval" packages/core packages/schema packages/tui packages/cli packages/server packages/session-ui` 只命中权限审批（external/edit approval）和测试，不命中 plan 审批。

### 7.2 替代机制

1. **硬约束**：`edit * deny` + plan 目录例外（`plan.ts:39-40`）。registry snapshot 里 edit/write/patch 都在；实际请求工具表按模型只保留 edit/write 或 patch 之一（§5.4），被保留者执行期拒绝，并被改写成固定文案（`plan.ts:45-54`）。
2. **软约束**：enter/leave `<system-reminder>` 注入 + 每请求对账（`plan.ts:14-25,58-74,101-121`）。
3. **人工交接**：用户切换 agent；切换本身产生 durable 事件、`agent-switched` 消息和 enter/leave 提醒（§6.7）。
4. **计划产物**：没有自动生成/读取/校验；只有 enter 提醒文本允许模型在“用户明确要求”时写 `<home>/.opencode/plan`（`plan.ts:15-16,31`）。权限本身对 plan 目录无条件 allow（`plan.ts:39-41`），所以“是否明确要求”只是提示词约束。

### 7.3 “软只读”结论（再次确认）

plan 的“read-only”不是沙箱，而是 `edit` deny + 提示词：

- **shell 默认 allow**：plan 没有 shell 规则，基础 `* * allow` 命中；shell 工具内部只按解析出的命令做 `shell` assert，外部目录先 `external_directory`（`shell.ts:133-142`）。因此 shell 可以在项目内写文件。
- **subagent 默认 allow**：plan 没有 subagent 规则；`subagent` 工具 assert `subagent` action（`subagent.ts:138-151`）。available 列表用 `Permission.evaluate("subagent", agent.id, selected.permissions)` 过滤（`:288-294`），plan 下 general/explore 都可用。enter 提醒只是说 “Do not ... ask a subagent to do so”（`plan.ts:18`），不是权限。
- **MCP 默认 allow**：MCP 工具/资源没有 plan 规则，基础 `* * allow` 命中；`execute` 也默认 allow（`tool.ts:238`）。`opencode_*` 工具（session_rename/session_move/models）甚至没有 `permission.assert`（`opencode.ts:85-140`）。
- **`.env` 仍是 ask**：基础 `read *.env`/`read *.env.*` ask，plan 无覆盖；`.env.example` allow（`schema/agent.ts:49-51`）。
- **外部目录仍是 ask**：除 plan 目录和全局 data/tmp/config 目录外，`external_directory * ask`（`schema/agent.ts:48`；`core/agent.ts:59-64`；`plan.ts:41`）。
- **plan 目录写**：external allow + edit allow；`write` 工具会创建父目录（`write.ts:60-61`），`patch` add 也走文件写入（`patch.ts:210-250`）；没有“用户明确要求”的代码校验。

### 7.4 subagent 的权限继承怪癖

- `Session.create` 对子会话使用 `permissions: input.permissions ?? parent?.permissions`（`session.ts:284-288`）。subagent 工具创建子会话时**没有传** `permissions`（`subagent.ts:184-193`），所以子会话继承父 **session.permissions**，而不是父 agent 的权限集。
- 子会话求值用自己的 agent（`permission.ts:158-163`）：`agents.resolve(agentID ?? session.agent)`。如果父会话是 plan、子 agent 是 general，则 general 的 `* * allow` + 自己的 `subagent/question deny` 生效，plan 的 `edit * deny` 不会继承。
- 父会话若有 session 级 `edit * deny`，子会话会继承，因而能限制子 agent；但 plan 的 agent 级 deny 不会。`[verified: session.ts:284-288 + permission.ts:158-163]`
- 继续已有子会话时，subagent 工具可 `switchAgent` 到另一个 agent（`subagent.ts:171-181`），模型/agent 语义按新 agent 求值。

### 7.5 plan 的 system prompt 事实

- plan 不设置 `agent.system`（`plan.ts:33-42`），所以 `baseTranscript` 走默认 `system.txt`（`model-request.ts:110-113`）。
- 默认 system.txt 由 `baseTranscript` 用 **snapshot 的 `tools.definitions`** 渲染（`model-request.ts:110-118`；`system-prompt.ts:9-27`）。snapshot 里 edit/write/patch 都在，所以默认 prompt 会加入“Use write/edit”指导；即使后续 PatchTool 在请求 hook 中把 edit/write 删掉（GPT 系），默认 prompt 仍可能保留这段指导。限制完全依赖 reminder + 权限。
- OptimizePlugin 在 agent 无自定义 system 时按模型替换/追加 system[0]（`optimize.ts:81-86`），并使用 hook 事件里的 `event.tools` 重渲染指导语；该 tools 可能已经被 PatchTool 删减（取决于 hook 顺序，`internal.ts:228-230`）。其中 `meta.txt:52` 明确提到 `plan` mode；`kimi.txt:17` 提到 plan mode 的只读限制；gpt/claude 的 prompt 没有 plan 专属文字。`[verified: 文件内容 + optimize.ts；hook 顺序 inferred]`
- 如果用户用 `.md`/config 给 plan 设置了 `system`，OptimizePlugin 会跳过替换（`optimize.ts:81`），但权限/提醒不变。

### 7.6 需要留意的时序/边界

- `ctx.session.synthetic` 先入 inbox，`resume:false`；如果当前没有 run，它保持 pending，直到下一次 drain 被 promote（`session.ts:310`；`runner/llm.ts:165-179`）。UI 上的“提醒消息”不是切换瞬间必然出现。
- 每请求对账的插入点是“最后一条 user 消息之前”，不是“最后一条 reminder 之后”。如果最后一条消息本身就是 leave/enter reminder（例如没有新 user prompt 的 resume），插入顺序可能把新提醒放到旧提醒之前，导致最后一条指令仍是旧提醒。`[inferred: plan.ts:65-66 + lastReminder 语义；未在测试中覆盖]`
- 对账的“去重”只发生在请求消息层（`lastReminder` 精确匹配单 text user 消息）；inbox 层没有按文本去重，每次 `session.synthetic` 都用新 ID。若 switch synthetic 与 prompt 的入队时序发生竞争，可能出现重复提醒。`[inferred: session.ts:288 + inbox.ts admit; 未运行并发测试]`
- `switchAgent` 不校验 agent 存在性（`session.ts:89-95`）；无效 id 直到 `context.select` 才报 `AgentNotFoundError`（`context.ts:128-129`）。

### 7.7 与 plan 相关的 v2/v1 差异（供迁移判断）

- **todowrite 删除**：v2 没有 todo 工具；v1 迁移代码把 `todowrite` 列入 `REMOVED_TOOLS`（`v1-migration.bun.ts:901`），并在历史里给模型发“工具已移除”的 notice（`:908-935`）。v2 时间线隐藏历史 todowrite（`session-ui/src/timeline/projection.ts:608`；`tools/tool-renderer.tsx:1313`）。
- **工具/动作改名**：v1 `bash`→`shell`、`task`→`subagent`、`apply_patch`→`patch`；权限迁移时 `write|edit|patch` 合并到 v2 `edit`，`task`→`subagent`，`bash`→`shell`（`v1/config/migrate.ts:27-33`；`v1-migration.bun.ts:899`）。
- **权限 schema 变化**：v1 用工具名→布尔/字符串/资源 map 的 permission 结构；v2 统一为 `{action,resource,effect}` 规则数组（`schema/permission.ts:58-63`；`v1/config/agent.ts:50-59`；`config/normalize.ts:487-497`）。
- **agent 配置字段变化**：v1 `prompt`→v2 `system`、`disable`→`disabled`、`maxSteps`→`steps`、`tools`→permissions；`mode` 仍允许 `subagent|primary|all`（`v1/config/agent.ts:8-59`；`v1/config/migrate.ts:35-80`；`schema/config/agent.ts:7-19`）。
- **agent 定义方式**：v2 的内置 agent 是运行时 `State` transform/plugin（pre 组的 `AgentPlugin`、`PlanPlugin`），配置再在 post 组追加（`plugin/internal.ts:213-252`；`plugin/supervisor.ts:143-155`）；v1 的静态 agent 定义不在本树内，无法逐项核对。`[unknown: v1 source]`
- **会话 agent 字段**：v1 的 `session` 表已有 `agent` 列，迁移直接复制到 `session_v2.agent`（`v1-migration.bun.ts:611-622`）；v2 另外用 durable `session.agent.selected` + projector 维护（`session-event.ts:69-78`；`projector.ts:546-556`）。这不是 plan 专有差异，但影响迁移后的提醒/切换。
- **plan 本体**：v1 的 plan agent 定义、原始提示词、是否有 todo/批准机制，在本树中无源码；本报告只能确认 v2 的插件定义与“无 approval”结论。`[unknown]`

---

## 8. 对既有 01 底稿的核对表

状态：✅=确认；⚠️=需修正/措辞不精确；➕=补充。

| 原 F-ID | 状态 | 核对结果 |
|---|---|---|
| F01 | ✅ | plan 确由 `plugin/plan.ts` 定义；字段/4 条权限与底稿一致。 |
| F02 | ✅➕ | 内置清单正确；补充 `mode:"all"` 合法值，plan 不在 `plugin/agent.ts` 的静态清单里。 |
| F03 | ✅ | 默认 5 条权限正确。 |
| F04 | ✅ | 4 条全局 external allow 正确；是“外部目录访问”，不只是“读”。 |
| F05 | ⚠️ | `.md` 只在配置目录（全局 config 目录、项目 `.opencode`）扫描；`.claude`/`.agents` 在 v2 只作为 skill 兼容根，不扫描 agent；正文是 system。 |
| F06 | ➕ | 合并规则正确；补充 `disabled` 删除、顶层 `permissions` 先追加到所有 agent、legacy 检测/字段覆盖。 |
| F07 | ✅ | `default_agent` 正确；补充只有 selectable 时才生效。 |
| F08 | ⚠️ | fallback 不是“任意 primary”，而是“任意 `mode !== subagent && !hidden`”（含 `all`）；default_agent 不可选时回退 build。 |
| F09 | ✅ | shift+tab 循环正确；补充 reverse 默认未绑定（`none`）。 |
| F10 | ✅ | `/agents` 对话框正确。 |
| F11 | ✅➕ | draft 语义正确；补充新会话创建直接把 agent 传给 `session.created`。 |
| F12 | ✅ | CLI `--agent` 正确（TUI 初始 draft；run 先 switch）。 |
| F13 | ✅ | `session_v2.agent` + projector 正确。 |
| F14 | ✅ | `agent-switched` 消息正确；`previous` 可选。 |
| F15 | ✅ | assistant.agent 正确。 |
| F16 | ✅ | durable `session.agent.selected` 正确。 |
| F17 | ⚠️ | 提醒文本正确；但 `ctx.session.synthetic` 先入 inbox，`resume:false`，不是切换瞬间直接成消息；要等 promote 后投影。 |
| F18 | ✅ | leave 文本正确。 |
| F19 | ⚠️➕ | 对账逻辑正确；补充 `lastReminder` 只认“单 text part 且文本完全相等”，引用/夹带不算；插入点可能产生旧提醒顺序问题；inbox 无文本去重。 |
| F20 | ✅➕ | edit deny + plan allow 正确；补充 `question * allow`，以及默认 13 条规则（未加配置）。 |
| F21 | ⚠️ | 路径正确（`Global.home` = OS home，不是 XDG config）；插件不自动 mkdir；无自动产物；“写文件时按需建父目录”是 write 工具行为。 |
| F22 | ✅➕ | 改写条件/文案正确；补充自定义 deny 也会被改写成同一句“outside Plan directory”。 |
| F23 | ✅➕ | findLast + 通配 + 默认 ask 正确；补充 `?`、`s` flag、Windows 大小写、pattern 末尾 `" .*"` 可选的 quirk。 |
| F24 | ⚠️ | “saved 最后叠加”不完整：deny 检查发生在 saved 之前，saved allow 不能覆盖 deny；另有 `permission.evaluate` hook 可改 effect。 |
| F25 | ⚠️➕ | 基本正确；补充“reject 一个 ask 会 reject 同 session 所有 pending”；TUI 主会话 reject 无 feedback（直接打断），只有子会话才弹 feedback 输入框；CLI 另有 reject-with-feedback。 |
| F26 | ✅➕ | subagent 权限门正确；补充 available 列表用 `Permission.evaluate` 过滤，plan 下 general/explore 可用。 |
| F27 | ✅➕ | 继承父 **session.permissions** 正确；补充 metadata 也继承、继续子会话可 switchAgent。 |
| F28 | ✅ | external 边界正确；edit deny 在 external allow 之后仍生效。 |
| F29 | ⚠️➕ | whollyDisabled 判据正确；补充 action 来自 `tool.options.permission ?? name`，`execute` 单独判断；session permissions 也参与 snapshot。但“edit/write/patch 仍在工具表”只对 registry snapshot 成立：PatchTool 的 context hook 会按模型删减请求级 edit/write vs patch（`patch.ts:296-309`；F61），所以实际请求工具表不一定是三者同时可见。 |
| F30 | ✅➕ | 工具清单基本正确；补充 `patch`、`opencode_list_mcp_resources`/`opencode_read_mcp_resource`、codemode 与 direct 的区别。 |
| F31 | ✅➕ | 迁移正确；补充 `disable`→`disabled`、`prompt`→`system`、`tools`→permissions 的具体映射。 |
| F32 | ✅➕ | 组装正确；补充第二个 system part `input.initial`；默认 system 会因 edit/write 可见而包含写文件指导。 |
| F33 | ⚠️ | “替换 system[0]”只对 override 模式成立；Anthropic 是 `append`（追加）；有自定义 system 的 agent 跳过。 |
| F34 | ✅➕ | plan 无 `agent.system` 正确；补充 meta/kimi 的模型专属 prompt 确实提到 plan，gpt/claude 没有。 |
| F35 | ✅ | 人工交接正确，无自动切换。 |
| F36 | ✅ | 无批准概念/消息/事件，确认。 |
| F37 | ⚠️ | v2 无 todowrite 正确；但 UI 不是“仍渲染历史 todowrite”，而是 `renderable` 过滤 + `ToolDisplay` 返回 null（隐藏）；只有 title case 保留。 |
| F38 | ⚠️ | 无自动生成正确；但“只提示词约定”要更精确：权限对 plan 目录无条件 allow，写不写只看模型是否遵守提示词。 |
| F39 | ✅➕ | fork 复制 agent/messages 正确；补充不复制 pending inbox。 |
| F40 | ✅➕ | revert 不改 agent 正确；补充 commit 删除边界后消息与 inbox 行。 |
| F41 | ✅➕ | 命令携带 agent 正确；补充 subagent mode 会创建子会话。 |
| F42 | ✅ | ask/replied ephemeral 正确。 |
| F43 | ➕ | 落盘正确；补充表结构/唯一索引、saved 只读为 allow、`save` 而非实际 resource、always 会尝试自动放行同批 pending。 |
| F44 | ✅➕ | `--auto` 回 once、不改 deny 正确；补充 TUI 的 autoaccept 是另一条路径；非 auto 非 v1 用 reject+feedback。 |
| F45 | ✅➕ | 归属/切换提示正确；session-ui 的 notice 文案是 “Agent changed” 类，TUI 是 “Switched agent ...”。 |
| F46 | ✅ | agent.list/get、Location 作用域正确。 |
| F47 | ✅ | agent.updated ephemeral + 客户端刷新正确。 |
| F48 | ✅ | `.env` ask / question allow 正确。 |
| F49 | ✅ | shell 未禁止、可写项目、软只读正确。 |
| F50 | ✅ | subagent 未硬拒、软约束正确。 |

新增核对项（底稿未覆盖）：F51 `mode:"all"`/校验、F52 顶层 `permissions` 对 plan 的覆盖、F53 synthetic 先入 inbox 的持久化时序、F54 TUI 主会话/子会话 reject 差异、F55 ACP/desktop/mini 的切换入口、F56 默认 system 的 edit/write 指导、F57 plan 目录写权限不检查“用户明确要求”、F58 历史 todowrite 是隐藏而非渲染、F59 `disabled` 删除 agent、F60 `switchAgent` 无存在性校验、F61 PatchTool 按模型二选一 edit/write 与 patch。

---

## 9. 未解问题

1. **synthetic 提醒的投递/重复时序**：`ctx.session.synthetic` 先入 inbox，`resume:false`；若 switch 事件与 prompt 入队竞争，inbox 层没有按文本去重，可能产生重复提醒。代码路径可推，未做并发测试确认。`[inferred]`
2. **对账插入顺序**：插入点是“最后一条 user 消息之前”。当最后一条消息本身是 leave/enter reminder（例如无新 user prompt 的 resume），新提醒可能被插到旧提醒之前，最后一条指令仍是旧提醒。`[inferred]`
3. **plan 会话创建但未立即运行**：`session.created` 的 enter synthetic 会 pending 到下一次 drain；UI 是否显示 pending 提醒、是否算“已注入”，取决于客户端对 inbox 的展示。`[inferred]`
4. **`mode:"all"` 的官方语义**：schema 允许，TUI/subagent 过滤推得“既可作为 primary 选择，也可作为 subagent”，但代码中没有专门分支/文档解释。`[inferred]`
5. **v1 的 plan agent 定义/原始 prompt**：本树只有 v1 config/session 迁移代码，没有 v1 静态 agent 源；无法从源码核对 v1 plan 的字段/提示词，只能确认 v2 的插件定义。`[unknown]`
6. **`default_agent` 与 `.md` 同名覆盖的完整优先级**：配置 entries 顺序已确认（wellknown → global → explicit → direct → project `.opencode` → content），但多个 `.md` 同名/嵌套 id 的实际覆盖结果未做运行验证。`[inferred]`
7. **`switchAgent` 无存在性校验**：无效 id 只在 `context.select` 报 `AgentNotFoundError`；不同客户端（TUI/CLI/ACP）对错误的呈现未逐一核对。`[verified code; UX unknown]`
8. **plan 目录写权限的“用户明确要求”**：完全由提示词保证，代码没有“用户消息必须包含明确请求”的检查；模型若忽略提醒，权限仍会 allow。`[verified code; behavior depends on model]`
9. **OptimizePlugin 与 PatchTool hook 的顺序**：`PluginHooks.trigger` 按注册顺序遍历 callback（`plugin/hooks.ts:78-85`），`internal.pre` 列表里 PatchTool（`:228`）在 OptimizePlugin（`:230`）之前；但插件激活本身是否严格按该数组顺序执行未单独核对，因此“模型适配 prompt 是否看到被删减后的 edit/write”仍有一个顺序假设。`[inferred]`

---

## 附：一句压缩结论

opencode v2 的 plan 是“**插件定义的 primary agent + edit 权限硬 deny（plan 目录例外）+ enter/leave synthetic 提醒 + 人工切回 build**”；没有计划批准/自动执行，也没有只读沙箱。shell、subagent、MCP、codemode 默认都还是 allow。registry snapshot 里 edit/write/patch 都在，但请求工具表会被 PatchTool 按模型二选一（GPT 系保留 patch，其余保留 edit/write），被保留的工具执行期被拒并改写成固定文案。子 agent 继承的是父 **session** 权限，不继承父 plan agent 的 `edit * deny`，所以 plan 会话里 spawn 的 general 子 agent 具备写权限（仅受提示词劝阻）。
