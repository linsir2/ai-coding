# deepseek-harness（dsh）plan 模式源码级盘点

> 状态：**已落档**（2026-10-19 重审轮）。源：deepseek-harness @ 57190202。

> 供「以 opencode 为原型、把 dsh 优点融入 Python 项目 ai-coding」使用。
> 本报告只读：未修改仓库、未运行测试。所有结论以源码为准；文档只当地图。

## 0. 版本与覆盖范围

- 仓库：`/home/linsir365/projects/deepseek-harness`
- HEAD：`571902028a75c64c1d767f4ae5fd099485f0ddf0`（`0.2.1-alpha.3` 同步到 master 的合并提交）[verified]
- 工作区：`git status --short` 无改动 [verified]
- 阅读范围：
  - plan 本体：`packages/plan/plan-mode/src/{index,types,client}.ts`、`packages/plan/plan-mode/README.md`、三组 tests
  - todo：`packages/todo/tool-todo/src/*`、README、tests
  - Web UI：`packages/client/ui-plan/**`、`packages/client/ui-user-questions/**` 中的 plan-review 部分、`packages/client/ui-workspace/**` 的 plan-review 状态、`packages/client/ui-conversation/**` 的 plan 座位/placeholder
  - 强制层：`packages/sandbox/sandbox-policy/**`、`packages/sandbox/sandbox/src/escalation.ts`、`packages/interaction/user-approval/**`、`packages/interaction/permission-presets/**`、tool-bash/tool-fs 的 escalation 调用
  - 状态层：`packages/core/session/src/{types,fork}.ts`、`packages/session/session-projection/**`、`packages/core/agent-loop/src/runtime-context.ts`、`packages/core/system-prompt/src/index.ts`
  - 相邻机制：`packages/subagent/subagent/src/child-agent.ts`、`packages/subagent/subagent-spawn-in-process/src/index.ts`、`packages/goal/goal-round-driver/**`、`packages/guard/repeat-tool-reminder/**`
  - 组装：`packages/bundle/base/cordis.patch.yml`、`packages/bundle/web-app/cordis.patch.yml`、`packages/bundle/web-app/presets/{standard,cordis,ptc,minimal}.patch.yml`
  - 文档/快照：`docs/subsystems/{plan,todo,sandbox,approval,permission-presets}.md`、`apps/web/tests/plan-{review,control-row}.e2e.ts`、`snapshots/web/plan-*`
- 跳过：`node_modules`、`dist`、`build`、`lib`；native/Python SDK；未运行测试；未做运行时复现（除阅读 e2e 录像/golden 外）。
- 证据标记：`[verified]` = 源码/测试直接可见；`[inferred]` = 由源码结构推导、未运行验证；`[doc]` = 仅文档陈述。

---

## 1. 功能清单

| F-ID | 功能 | 触发 | 代码位置 | 行为 | 状态 |
|---|---|---|---|---|---|
| F1 | plan 状态事件 | 用户/工具/命令切换 | `packages/plan/plan-mode/src/index.ts:45-54` | `plan/mode: {active:boolean}`，log-only、整值替换、非 surface | [verified] |
| F2 | plan 投影 | 每次 session event | `packages/plan/plan-mode/src/index.ts:136-168` | 折叠 `command/run`、`command/done`、`plan/mode`、`request/header`，输出 `{active,pending}` | [verified] |
| F3 | 服务读写 | `ctx.planMode.get/set` | `packages/plan/plan-mode/src/index.ts:403-445` | `get` 返回当前 + pending；`set` 返回 `committed/queued/cancelled/noop` | [verified] |
| F4 | 运行中排队、空闲即提交 | 用户点击/命令 | `packages/plan/plan-mode/src/index.ts:425-445`、`379-383` | 用 `turnBoundary` 判断 open turn；open 时只记进程内 pending，空闲时直接 append | [verified] |
| F5 | 请求边界落盘 | 下一个被接受的 in-turn pre-step | `packages/plan/plan-mode/src/index.ts:196-213`、`447-460` | 先 `await next()`；只有 step 被接受才 append；append 失败只 warn，不阻塞 turn，pending 保留 | [verified] |
| F6 | 切换提示 | 用户切换且上次 request header 是另一种状态 | `packages/plan/plan-mode/src/index.ts:462-474` | 追加一条 `source.kind='plan-mode'` 的 user message；无 header、同状态、工具批准退出都不提示 | [verified] |
| F7 | `/plan` 命令 | 斜杠命令/命令菜单 | `packages/plan/plan-mode/src/index.ts:229-275` | 裸 `/plan` 进入；`/plan off` 退出；`/plan <msg>` 进入并 `agent.steer` 一条普通 user message；附件顺序保留 | [verified] |
| F8 | plan 提示段 | plan active 或 pending active | `packages/plan/plan-mode/src/index.ts:216-224` | 注册 `plan:policy` system-prompt section，active 才输出配置文本；pending 目标也立即生效 | [verified] |
| F9 | 退出工具 | 模型调用 `exit_plan_mode` | `packages/plan/plan-mode/src/index.ts:277-372` | 工具常驻；inactive 执行报错；active 要求 H1 markdown；通过 user-questions 评审 | [verified] |
| F10 | 计划评审 | `exit_plan_mode` execution | `packages/plan/plan-mode/src/index.ts:311-330` | 一问一答：`Approve` / `Keep planning`；`detail` 是完整 plan；`intent.kind='plan-review'`；带 `callId` | [verified] |
| F11 | 批准 | 用户选 `Approve` | `packages/plan/plan-mode/src/index.ts:347-359` | 只有「恰好一个 Approve 且无 custom 文本」才算同意；记录 silent pending exit，下一 pre-step 才 `plan/mode:false` | [verified] |
| F12 | 继续规划 | 用户选 `Keep planning` 或给反馈 | `packages/plan/plan-mode/src/index.ts:349-353` | 抛 tool error，模型在同 turn 内看到反馈并重做 | [verified] |
| F13 | 关闭评审 | 用户 Request changes / dismiss | `packages/plan/plan-mode/src/index.ts:331-340` | `ASK_CANCELLED` → `concludeTurn()` + `{approved:false}`；plan 保持 active；用户下一轮自己说 | [verified] |
| F14 | 直接退出 | `/plan off` 或 chip | `packages/plan/plan-mode/src/index.ts:240-255`；`packages/client/ui-plan/src/client/index.ts:121-125` | 空闲立即提交，运行中排队；可取消未生效的进入 | [verified] |
| F15 | 计划产物 | 模型调用 `exit_plan_mode` | `packages/client/ui-plan/src/client/plan.ts:32-48` | 从 native `tool/call` 或 PTC dispatch 的 arguments 读 `plan`；不存 plan 文件 | [verified] |
| F16 | 计划卡片 | turn 结束 | `packages/client/ui-plan/src/client/PlanCard.tsx:43-60`；`plan-definition.ts:15-33` | 每个 invocation 一张卡；Chat node `visibility:'hidden'`，只在 turnTail 展示 | [verified] |
| F17 | 计划预览 | 点卡片/评审卡链接 | `packages/client/ui-plan/src/client/plan-resource.ts:37-82` | 从 session history 逐页找 `callId`；找到即 yield 完整 markdown；布局只存 address | [verified] |
| F18 | 评审自动打开 | 评审请求到达 | `packages/client/ui-plan/src/client/PlanCard.tsx:72-83` | 每个 review 在当前浏览器会话自动打开一次；侧栏挂载后再开；手动关闭不重开 | [verified] |
| F19 | 临时评审预览 | 无 `callId` 的 plan-review | `packages/client/ui-plan/src/client/review-preview.ts:18-29`；`PlanPreview.tsx:21-33` | 文本只放在当前 tab navigation 内存；reload 后显示「已失效」，需从评审卡重开 | [verified] |
| F20 | plan chip | plan 有效目标为 active | `packages/client/ui-plan/src/client/PlanModeControl.tsx:19-69` | 显示 Plan 按钮；点击执行 `/plan off`；失败保留 chip + 错误行；`pending ? !active : active` | [verified] |
| F21 | 会话列表状态 | 评审挂起 | `packages/client/ui-workspace/src/client/rows/Rows.tsx:372-377`；`locales.ts:112,238` | 行状态 `plan-review`，warning 点，文案 `Plan awaiting review` / `计划待审` | [verified] |
| F22 | todo 工具 | 模型调用 `todo_write` | `packages/todo/tool-todo/src/index.ts:135-211` | 整表替换；`content` + 三态 status；non-agent 报错；返回计数 | [verified] |
| F23 | todo 投影 | `todo/write` / `turn/start` | `packages/todo/tool-todo/src/index.ts:119-134` | 最新 `todo/write` 整表；`turn/start` 清空；`turn/end` 保留已完成清单 | [verified] |
| F24 | todo UI | session projection | `packages/client/ui-conversation/src/client/skeleton/TodoPanel.tsx:65-109` | composer dock 折叠清单 + 计数；`todos` 不在 prompt 中 | [verified] |
| F25 | sandbox/approval 独立 | 各自开关 | `packages/sandbox/sandbox-policy/src/index.ts:133-180`；`packages/interaction/user-approval/src/index.ts:150-307` | plan 不读不写这两个；组合由部署选择 | [verified] |
| F26 | 权限预设 | `/permission` / UI picker | `packages/interaction/permission-presets/src/index.ts:252-274`、`398-417` | 预设只 bundle sandbox+approval，不包含 plan | [verified] |
| F27 | 子代理继承 | spawn/fork | `packages/subagent/subagent/src/child-agent.ts:238-275`；`packages/core/session/src/fork.ts:20-25` | spawn 新会话 inactive；fork 复制父日志前缀，plan/mode 一并继承；子代理 sandbox 继承父显式 override，approval 固定 `never` | [verified] |
| F28 | 长任务 goal | `/goal` + goal-round-driver | `packages/goal/goal-round-driver/src/prompt.ts:11-27` | 独立机制：idle 时按轮注入 `<goal_round>`，有 max rounds；与 plan 无代码耦合 | [verified] |
| F29 | ACP 不支持 plan | ACP 自动化面 | `packages/acp/acp/README.md:80,175` | 明确省略 modes/commands/plans；plan 本体在 base/CLI 可用，但专用评审 UI 只有 Web（TUI 只覆盖命令入口/直接退出，design note:66） | [verified] |

---

## 2. 数据流

### 2.1 阶段表

| 阶段 | 触发 | 发生什么 | 证据 |
|---|---|---|---|
| 1. 进入 | `/plan`、`/plan <msg>`、命令菜单 Plan、chip 的 `/plan off` | 命令运行时记录 `command/run`，handler 调 `ctx.planMode.set`；空闲立即 `plan/mode`，运行中只记 pending；带消息则 `agent.steer` | `packages/interaction/commands/src/index.ts:373-381`；`packages/plan/plan-mode/src/index.ts:229-275`；`packages/client/ui-commands/src/client/presentation.ts:20` |
| 2. 状态投影 | 每次 session event | `plan` projection 折叠 command 生命周期 + plan/mode + request/header；客户端得到 `{active,pending}` | `packages/plan/plan-mode/src/index.ts:136-168`；`packages/session/session-projection/src/index.ts:233-339` |
| 3. 请求组装 | 下一 step | system prompt 在 `PLAN_POLICY` 顺序 500 处插入 `plan:policy`；tool catalog 不变；sandbox/approval 走 runtime-context 的 user-message 快照 | `packages/core/system-prompt/src/index.ts:124-179`；`packages/plan/plan-mode/src/index.ts:216-224`；`packages/core/agent-loop/src/runtime-context.ts:152-160` |
| 4. 探索/工具 | 模型 | 所有工具仍在 catalog；plan 层不拦截；sandbox 在 execution 层按文件效果拒绝；approval 只在 escalation 等场景询问 | `packages/plan/plan-mode/tests/plan-mode.spec.ts:581-589`；`packages/sandbox/sandbox/src/escalation.ts:171-207` |
| 5. 提交计划 | 模型调 `exit_plan_mode` | 校验 active + H1；通过 `userQuestions.ask` 展示完整 plan；UI 进入 plan-review 卡片 | `packages/plan/plan-mode/src/index.ts:298-330` |
| 6. 评审 | 用户 | `Approve` / `Keep planning` / dismiss；批准是 pending exit，不是 permission grant | `packages/plan/plan-mode/src/index.ts:331-359` |
| 7. 交接 | 下一个 accepted pre-step | 追加 `plan/mode:false`；下一请求不再有 `plan:policy`；同一 turn 的后续 step 继续执行 | `packages/plan/plan-mode/src/index.ts:196-213`、`355-359`；`tests/plan-mode.spec.ts:963-978` |
| 8. 直接退出 | `/plan off` | 空闲立即提交；运行中排队；可取消 pending entry | `packages/plan/plan-mode/src/index.ts:240-255` |
| 9. 计划留档 | turn 结束 | 从 tool/call 参数派生 plan 卡；侧栏预览从 session history 读；布局只存 address | `packages/client/ui-plan/src/client/plan.ts:32-48`；`plan-resource.ts:37-82` |
| 10. todo（若使用） | 模型调 `todo_write` | append `todo/write` 整表；投影供 UI；下一 `turn/start` 清空 | `packages/todo/tool-todo/src/index.ts:119-134`、`192-209` |

### 2.2 叙事

plan 模式不是「换一个 agent」：它没有创建新 agent、没有新 session、没有 plan 文件。它只是一个可持久化的 boolean collaboration stance：
- 状态落在 session log 的 `plan/mode` 事件；
- 请求组装时决定是否插入一段部署方拥有的 system-prompt section；
- 模型通过一个常驻工具 `exit_plan_mode` 把「完整 markdown 计划」提交给用户评审；
- 用户批准后，状态在下一个 accepted pre-step 翻成 inactive，同一 turn 继续执行。

这条链路的关键是「评审」而不是「执行器」：批准并不启动另一个 agent，也不授予额外权限；它只是把模型从「plan 指导」切回「默认指导」。执行仍由同一个模型、同一段 conversation、同一套工具完成。

同时，dsh 把三件事拆开，互不读写：
1. **collaboration stance**：plan（system section + review tool）；
2. **enforcement**：sandbox mode（文件效果）与 approval policy（escalation 询问）；
3. **task checklist**：todo（UI projection + whole-list tool）。

这解释了为什么 plan 是 soft guidance：它刻意不拥有执行强制力。

---

## 3. 代码地图

| 文件 | 职责 | 关键符号 |
|---|---|---|
| `packages/plan/plan-mode/src/index.ts` | plan 服务、事件、投影、命令、退出工具 | `PlanModeController`、`planProjectionDefinition`、`EXIT_PLAN_MODE`、`resolveConfig`、`loggedActive`、`hasOpenTurn`、`onBoundary`、`narration` |
| `packages/plan/plan-mode/src/types.ts` | `plan` projection 的 host/client 类型唯一来源 | `PlanProjection`、`PlanUnitState` |
| `packages/plan/plan-mode/src/client.ts` | 客户端类型 outlet | re-export |
| `packages/todo/tool-todo/src/index.ts` | `todo_write` 工具 + `todos` 投影 | `toTodoList`、`describe`、`todosProjectionSchema` |
| `packages/todo/tool-todo/src/types.ts` | `TodoItem`、`todo/write` 事件声明 | `TodoItem` |
| `packages/client/ui-plan/src/client/index.ts` | Web plan 插件装配 | chip、plan cards、review actions、sidebar tab、resource |
| `packages/client/ui-plan/src/client/PlanModeControl.tsx` | composer plan chip | `PlanChip` |
| `packages/client/ui-plan/src/client/PlanCard.tsx` | turnTail 计划卡 + 评审自动打开 | `PlanCards`、`PlanReviewOpen` |
| `packages/client/ui-plan/src/client/plan.ts` | 从日志参数派生 plan 文档/资源地址 | `submittedPlan`、`planAddress`、`parsePlanAddress` |
| `packages/client/ui-plan/src/client/plan-resource.ts` | 侧栏计划资源读取 | `planResourceProvider` |
| `packages/client/ui-plan/src/client/plan-definition.ts` | Chat node 定义 | `planDefinition` |
| `packages/client/ui-plan/src/client/review-preview.ts` | 临时评审地址 | `reviewPreviewAddress`、`isReviewPreviewAddress` |
| `packages/client/ui-plan/src/client/review-store.ts` | 当前浏览器已自动打开的 review | `createPlanReviewStore` |
| `packages/client/ui-plan/src/client/PlanPreview.tsx` | markdown 预览与标题 | `PlanPreview`、`PlanTitle` |
| `packages/client/ui-plan/src/client/failure-line.ts` | 资源失败本地化 | `planFailureLine` |
| `packages/client/ui-user-questions/src/client/PlanReviewPanel.tsx` | plan-review 决策卡 | `PlanReviewPanel` |
| `packages/client/ui-user-questions/src/client/contract/slots.ts` | `planReviewOf` 收窄、`PlanReview` 类型 | `PlanReview`、`planReviewOf` |
| `packages/client/ui-user-questions/src/client/index.ts` | 评审卡与通用问题流的优先级 | `plan-review` 优先级 2 |
| `packages/interaction/user-questions/src/index.ts` | 问题服务、live root 校验、intent 校验、answer | `assertLiveRoot`、`ask`、`answer` |
| `packages/interaction/user-questions/src/types.ts` | `AskUserQuestionIntent` | `plan-review` intent |
| `packages/sandbox/sandbox-policy/src/session-mode.ts` | `sandbox/mode` 事件与写路径 | `setSandboxMode` |
| `packages/sandbox/sandbox-policy/src/index.ts` | 每-call 策略解析与 runtime context | `SandboxPolicyService`、`resolve`、`overrideOf` |
| `packages/sandbox/sandbox/src/escalation.ts` | 共享 escalation 词汇/审批 | `approveEscalation`、`sandboxDenialMarker` |
| `packages/interaction/user-approval/src/index.ts` | approval policy、审计、fail-closed | `ApprovalService`、`setApprovalPolicy` |
| `packages/interaction/permission-presets/src/index.ts` | sandbox+approval 预设 | `PermissionPresetService`、`set`、`current` |
| `packages/core/system-prompt/src/index.ts` | section/context 顺序 | `SECTION_ORDERS.PLAN_POLICY:500`、`CONTEXT_ORDERS` |
| `packages/core/agent-loop/src/runtime-context.ts` | system prompt 与 runtime-context 的 surface 投影 | `SystemPromptProjection`、`RuntimeContextProjection` |
| `packages/core/session/src/types.ts` | surface 事件/`surfaceOp` | `SurfaceEventType`、`SurfaceOp` |
| `packages/core/session/src/fork.ts` | fork 前缀复制 | `buildForkSeed` |
| `packages/subagent/subagent/src/child-agent.ts` | 子代理权限 seed | `captureDelegatedPolicyOverrides`、`appendDelegatedPolicyOverrides` |
| `packages/goal/goal-round-driver/src/prompt.ts` | 长任务轮次提示 | `renderGoalRoundPrompt` |
| `packages/bundle/base/cordis.patch.yml` | base 组装（CLI/headless） | plan-mode、sandbox、approval、permission、todo、goal |
| `packages/bundle/web-app/presets/standard.patch.yml` | Web standard preset | plan-mode 在 `planning` group，`isolate.planMode` |
| `packages/bundle/web-app/cordis.patch.yml` | Web host 组装 | host `plan-mode` disabled；`ui-plan` 挂载 |
| `packages/bundle/web-app/presets/minimal.patch.yml` | minimal preset | 没有 plan/todo/goal |

---

## 4. 状态与持久化

| 状态 | 事件/来源 | 存储 | 投影/读取 | 恢复 | 压缩 |
|---|---|---|---|---|---|
| plan active | `plan/mode: {active}` | session log，log-only、非 surface、整值替换 | `plan` unit（`stateVersion:3`），`view` 裁成 `{active,pending}` | 空日志 → inactive；resume/fork/compaction 折叠 | log-only 事件不被 `surfaceOp` 替换，状态保留 |
| plan pending（命令） | `command/run` + `command/done` (`name='plan'`) | session log | `plan` unit 的 `running/wanted` | 可从日志重建；这是唯一「可恢复的 pending」 | 同上 |
| plan pending（直接 set / 批准退出） | `ctx.planMode.set` / `exit_plan_mode` 批准 | **进程内 WeakMap** | `get()` 可见；客户端 projection 不可见（无 command 事件） | 进程退出即丢；README 明确承认 | 不适用 |
| plan 上次告知 | `request/header` | session log | `activeAtLastHeader` | 折叠 | 保留 |
| plan 产物 | `tool/call` / `tool/ptc-dispatch*` 的 `arguments.plan` | session log（log-only tool/call；PTC dispatch） | UI `submittedPlan`；资源 provider 读 history | 从日志重读；布局只存 address | tool/call 是 log-only，不随 surface 压缩删除；模型 transcript 可能看不到 |
| plan review | `user-questions` 请求；工具调用挂起 | 工具调用 + 进程内等待；无单独 plan review 事件 | 客户端 pending question / `plan-review` intent | 浏览器重连可重新收到；进程重启行为未在测试中覆盖 | 评审结束后 tool/result 正常记录 |
| todo | `todo/write: {todos}` | session log，log-only | `todos` unit（`stateVersion:2`） | 折叠；**下一个 `turn/start` 清空** | log-only 保留；投影清空 |
| sandbox mode | `sandbox/mode: {mode}` | session log，log-only | `sandboxMode` unit（`stateVersion:1`） | 折叠；`resolve` 优先级：显式 grant > session override > deployment default | 保留 |
| approval policy | `approval/policy: {policy}` | session log，log-only | `overrideOf` 反向扫描；无独立 projection | 折叠 | 保留 |
| approval 审计 | `approval/asked` / `approval/decided` | session log，log-only | 不进入 transcript | 保留 | 保留 |
| permission preset | `permission/preset: {preset}` | session log，log-only | `permissions` projection（preset + 两 knob） | 折叠；`custom` 派生 | 保留 |
| 权限标签页 | 进程级 catalog | 内存/配置 | `catalog()`；UI join `permissions` | 重启从配置重建 | 不适用 |

### 4.1 关键结论

1. **plan 状态的 durable 真源只有一个**：`plan/mode` 事件。投影、UI、恢复都从它折叠。没有 live mirror，没有 plan 文件。[verified]
2. **pending 有两套**：命令生命周期驱动的 pending 可从日志恢复；直接 `set`/批准退出的 pending 只在 WeakMap，进程退出即丢。这是 dsh 自己承认的限制，但「批准退出」也走同一路径，风险比文档描述的更广。[verified code + inferred impact]
3. **压缩只替换 surface 消息**：`compaction/summary` 用 `surfaceOp: replace` 替换被 shadow 的 surface 节点；log-only 的 `plan/mode`、`todo/write`、`sandbox/mode`、`approval/policy` 不在 surface，所以不会被压缩删除。[verified `packages/core/session/src/types.ts:440-465`；`packages/compaction/compaction-basic/src/region.ts:491-507`]
4. **plan 产物不在 system prompt 里重复**：完整 markdown 只在 tool call 参数/PTC dispatch 中；UI 资源按 `callId` 从 history 读。布局存储只存 `dsh-resource://plan/...`，不存计划文本。[verified `plan.ts:55-60`；`plan-resource.ts:37-82`]
5. **plan review 不是 permission 决策**：它走 user-questions，不走 approval seam；因此 approval policy `never` 不会阻止 plan review。[verified `plan-mode/src/index.ts:307`；design note:59]
6. **todo 是 turn-scoped 的 UI 投影**，不是跨 turn 的持久 checklist：`turn/start` 清空；但 README 摘要又说「survives across turns」。文档内部冲突，代码/test 以清空为准。[verified `tool-todo/src/index.ts:127-130`；`tests/projection.spec.ts:84-96`；README:12 vs :93]
7. **todo 没有自动 nudge/continuation 消费者**：`packages/core/agent-loop`、`packages/goal`、`packages/guard` 中没有读取 `todos` 做下一轮提示的逻辑；todo 只由模型主动调用、UI 展示。循环卫生由独立的 `repeat-tool-reminder` 负责（base `cordis.patch.yml:447-452`）。[verified by grep；`repeat-tool-reminder/src/index.ts:1-20`]

---

## 5. 工具与权限矩阵

### 5.1 plan 层：没有 allow/deny，只有提示

| 工具族 | plan 层最终效果 | 证据 |
|---|---|---|
| `read` / `glob` / `grep` / `lsp` / `read_image` | 允许；plan 提示鼓励用于探索 | `tests/plan-mode.spec.ts:581-589`（read 在 plan 下 untouched） |
| `write` / `edit` / `str_replace_editor` | **plan 层允许**；prompt 说不要写；sandbox 可能拒绝，approval 可能询问 escalation | `tests:581-589`；`packages/fs/tool-fs/src/sandbox.ts:121-145` |
| `bash` / `pwsh` / persistent terminal | **plan 层允许**；prompt 说不要执行变更；sandbox 只限制文件效果，网络/进程不在内 | `tests:581-589`；`docs/subsystems/sandbox.md:11` |
| `todo_write` | 允许；plan prompt 明确说「计划阶段不要用 todo，它用于批准后的实现」 | `standard.patch.yml:59`；`base/cordis.patch.yml:340` |
| `subagent` / `subagent_fork` / `workflow` | 允许；spawn 子代理新会话 inactive，fork 继承 plan；子代理有自己的 sandbox/approval（approval 固定 `never`） | `child-agent.ts:238-275`；`README:186-187` |
| MCP / 其他未来工具 | 允许；dsh 明确拒绝 per-plan allowlist，因为 mutability 是每个工具的属性 | design note:57 |
| `ask_user_question` | 允许；prompt 要求只问 user-owned choice / 材料性歧义 | `standard.patch.yml:61` |
| `exit_plan_mode` | 唯一 plan 专属门：inactive 报错；active 要求 H1；评审 | `plan-mode/src/index.ts:300-310` |

核心事实：plan 层没有额外过滤；测试用同名 fake tool 直接断言 `read`、`write`、`bash` 在 plan 下 `isError=false`。真实工具仍受 sandbox/approval 约束，但 plan 本身不是工具过滤器，也不是执行门。[verified `tests/plan-mode.spec.ts:581-589`；真实执行层见 tool-fs/tool-bash]

### 5.2 plan × sandbox × approval 组合矩阵

sandbox 与 approval 是两条独立轴；plan 是第三条独立轴。下面按代码推导（未运行）：

| 组合 | sandbox 文件效果 | approval | plan 行为 | 结果 |
|---|---|---|---|---|
| plan off + read-only + ask | 写被拒；`/dev/null` 等 sink 例外 | escalation 询问 | 无 | 标准只读会话；写失败 + escalation 提示 |
| plan on + read-only + ask | 同上 | 同上 | 有 plan section；review 可开 | 模型可读/探索；写仍被 sandbox 拒；若模型重试 escalation，用户可批准**一次**写，plan 不拦截 |
| plan on + workspace-write + ask | workspace + temp 可写 | escalation 询问 | 同上 | **模型即使违背 plan 也能写 workspace**；plan 只是提示 |
| plan on + danger-full-access + never | 无文件限制 | 无 approval 提示 | 同上 | **plan 是唯一软约束**；模型可在 review 前自由写；批准后也无额外限制 |
| plan on + read-only + never | 写被拒 | escalation 直接 `rejected` | 同上 | 写不可行；plan review 仍可询问（user-questions 独立） |
| plan on + workspace-write + never | workspace 可写 | 无 escalation | 同上 | 写可执行；plan review 仍可询问；无 approval 门 |
| plan off + danger-full-access + never | 无限制 | 无 | 无 | 普通全权会话 |

注意：
- `danger-full-access` 根本不调用 `ctx.sandbox`；plan 无法改变这一点。[verified `docs/subsystems/sandbox.md`；`sandbox-policy/src/index.ts:164-180`]
- sandbox 只治理**文件效果**，网络与进程可见性不在词汇内；因此 plan + read-only 仍可能 `curl`/spawn 进程。[verified `docs/subsystems/sandbox.md:11`]
- approval 只治理需要 approval 的动作（bash/fs 的 escalation、Auto review 的 ask 等）；**plan review 不走 approval**，所以 `never` 不会阻止 plan review。[verified `plan-mode/src/index.ts:307`；`user-approval/src/index.ts:267-275`]
- 批准 plan 不是权限授予：它只让 `plan/mode` 变 false；sandbox/approval 的当前值不变。[verified `plan-mode/src/index.ts:347-359`；无 sandbox/approval 写入]

### 5.3 拒绝/失败文案（原文）

plan 层：
- `Error: exit_plan_mode requires a calling agent (no session to switch)` [verified `index.ts:300`]
- `Error: exit_plan_mode is only available in plan mode` [verified `index.ts:302`]
- `Error: exit_plan_mode requires a non-empty markdown plan starting with a # heading` [verified `index.ts:305`]
- `Error: no user-questions channel is available to review the plan; ask the user to switch the session mode instead` [verified `index.ts:309`]
- `Error: The user chose to keep planning; revise the plan and present it again.` [verified `index.ts:351-353`]
- `Error: The user chose to keep planning; their feedback: <feedback>` [verified `index.ts:353`]
- `Error: the plan-mode service was reloaded while the plan was under review; present the plan again` [verified `index.ts:345`]
- `Error: human interaction is unavailable while the calling agent is owned by another live agent; include the unresolved question or decision in the child agent's final result` [verified `user-questions/src/index.ts:140-142`]

sandbox 层（不是 plan 文案，但 plan 下会看到）：
- `[sandbox: file access denied under <mode> mode]` [verified `escalation.ts:71-73`]
- `[sandbox: escalation available — retry this exact <subject> once with sandbox_permissions (the narrowest wider mode that suffices) + justification; the approval prompt asks the user]` [verified `escalation.ts:84-86`]
- `the user rejected escalating this <subject> to "<mode>"; it stays denied, so stop and explain instead of working around it` [verified `escalation.ts:203`]
- `approval for escalating to "<mode>" was cancelled` [verified `escalation.ts:204`]
- `sandbox escalation to "<mode>" requires approval, but no approval channel is available` [verified `escalation.ts:205`]

### 5.4 三个真实缺口

1. **workspace-write 下 plan 可被无视**：模型直接 `write` 会成功；plan 只是 prompt。dsh 的设计注释明确说「a model that ignores the guidance can still mutate」。[verified design note:72]
2. **spawn 子代理不继承 plan**：父在 plan mode，spawn 的子会话是 fresh/inactive；子代理可按自己的 sandbox/approval 执行变更。fork 才继承 plan。[verified `subagent-spawn-in-process/src/index.ts:51`；`README:186`]
3. **MCP/未来工具不受 plan 约束**：dsh 拒绝 per-plan allowlist；任何 mutating MCP 工具都可能在 plan 下执行。[verified design note:57]

因此：**不要把 dsh 的 plan 当作安全边界**；要硬约束，必须叠加 sandbox/approval/工具权限。

---

## 6. 关键源码摘录

### 6.1 plan 事件与投影

```ts
// packages/plan/plan-mode/src/index.ts:45-54
declare module '@deepseek-ai/dsh-session/types' {
  interface SessionEventMap {
    /** ... log-only, non-surface, whole-value replace ... */
    'plan/mode': { active: boolean }
  }
}
```

```ts
// packages/plan/plan-mode/src/index.ts:136-168
export const planProjectionDefinition = {
  key: 'plan',
  stateVersion: 3,
  stateSchema: planUnitStateSchema,
  init: () => ({ active: false, wanted: null, running: null, activeAtLastHeader: null }),
  apply: (state, event) => {
    if (event.type === 'command/run' && event.data.name === 'plan') {
      if (event.data.args === undefined) return state
      const wanted = event.data.args.trim() !== 'off'
      return { ...state, running: { commandId: event.data.commandId, wanted } }
    }
    if (event.type === 'command/done' && event.data.commandId === state.running?.commandId) {
      const wanted = event.data.kind === 'success' && state.running.wanted !== state.active
        ? state.running.wanted : null
      return { ...state, wanted, running: null }
    }
    if (event.type === 'plan/mode') return { ...state, active: event.data.active, wanted: null }
    if (event.type === 'request/header') return { ...state, activeAtLastHeader: state.active }
    return state
  },
  wire: {
    viewSchema: planProjectionSchema,
    view: (state) => {
      const wanted = state.running?.wanted ?? state.wanted
      return { active: state.active, pending: wanted !== null && wanted !== state.active }
    },
  },
}
```

要点：`pending` 是「目标状态 ≠ 已记录状态」；`activeAtLastHeader` 只用于判断模型上次是否被告知，避免重复提示。

### 6.2 运行中排队、请求边界落盘

```ts
// packages/plan/plan-mode/src/index.ts:196-213
ctx.on('agent/pre-step', async ({ agent, signal }, next): Promise<PreStepDecision> => {
  const decision = await next()
  const pending = this.pendingIntents.get(agent.session)
  if (decision.kind === 'reject' || signal.aborted || pending === undefined) return decision
  const narration = this.narration(agent.session, pending.active)
  try {
    this.onBoundary(agent.session)
  } catch (error) {
    ctx.logger.warn('dsh-plan-mode: failed to append selected plan mode at step start: %o', error)
    return decision
  }
  return !pending.narrate || narration === undefined
    ? decision
    : { ...decision, messages: [...decision.messages, narration] }
})
```

要点：先 `await next()`，下游接受后才 append；append 失败只 warn，不阻塞 turn；pending 保留以便重试。

```ts
// packages/plan/plan-mode/src/index.ts:425-445
set(agent: Agent, active: boolean): 'committed' | 'queued' | 'cancelled' | 'noop' {
  const session = agent.session
  const pending = this.pendingIntents.get(session)
  const target = pending?.active ?? this.loggedActive(session)
  if (active === target) return 'noop'
  if (this.hasOpenTurn(session)) {
    this.pendingIntents.set(session, { active, narrate: true })
    return this.loggedActive(session) === active ? 'cancelled' : 'queued'
  }
  if (active === this.loggedActive(session)) {
    this.pendingIntents.delete(session)
    return 'cancelled'
  }
  session.append('plan/mode', { active })
  this.pendingIntents.delete(session)
  const narration = this.narration(session, active)
  if (narration !== undefined) agent.inject(narration)
  return 'committed'
}
```

要点：用 `turnBoundary` 的 open turn（而不是 agent status）判断是否空闲；agent status 在 post-turn checkpoint 期间仍是 running。

### 6.3 plan 提示段

```ts
// packages/plan/plan-mode/src/index.ts:216-224
ctx.systemPrompt.section({
  name: 'plan:policy',
  order: ctx.systemPrompt.getSectionOrder('PLAN_POLICY'), // 500
  text: (context) => {
    if (context.agent === undefined) return ''
    const pending = this.pendingIntents.get(context.agent.session)
    return (pending?.active ?? this.loggedActive(context.agent.session)) ? this.section : ''
  },
})
```

要点：pending 目标也立即影响下一请求；section 在 tool sections（1000+）之前；prompt 文本还明确声明「override any later tool description」，两者共同构成优先级。

### 6.4 `/plan` 命令

```ts
// packages/plan/plan-mode/src/index.ts:235-272（节选）
handler: ({ agent, rawInput, attachments }) => {
  const message = rawInput.trim()
  if (message === 'off' && attachments.length > 0) {
    return { kind: 'error', text: 'Attachments cannot accompany /plan off.' }
  }
  if (message === 'off') {
    switch (this.set(agent, false)) {
      case 'committed': return { kind: 'success', text: 'Plan mode off.' }
      case 'queued': return { kind: 'success', text: 'Leaving plan mode (applies from the next step).' }
      case 'cancelled': return { kind: 'success', text: 'Plan mode entry cancelled.' }
      case 'noop':
        return this.loggedActive(agent.session)
          ? { kind: 'success', text: 'Leaving plan mode (applies from the next step).' }
          : { kind: 'success', text: 'Plan mode is already inactive.' }
    }
  }
  const outcome = this.set(agent, true)
  if (message !== '' || attachments.length > 0) {
    agent.steer(createUserMessage({
      content: [...attachments, ...(message === '' ? [] : [{ type: 'text' as const, text: message }])],
      source: { kind: 'user' },
    }))
  }
  return { kind: 'success', text: outcome === 'committed'
    ? 'Plan mode on. Use /plan off to leave.'
    : 'Entering plan mode (applies from the next step). Use /plan off to leave.' }
}
```

要点：`/plan off` 不产生模型输入；`/plan <msg>` 走普通 steering，保留附件顺序；命令结果不进模型历史。

### 6.5 退出工具与评审

```ts
// packages/plan/plan-mode/src/index.ts:298-359（节选）
execute: async (args, exec) => {
  const agent = exec.agent
  if (agent === undefined) throw new Error(`${EXIT_PLAN_MODE} requires a calling agent (no session to switch)`)
  if (!this.loggedActive(agent.session)) throw new Error(`${EXIT_PLAN_MODE} is only available in plan mode`)
  if (!/^#\s+\S/.test(args.plan.trim())) throw new Error(`${EXIT_PLAN_MODE} requires a non-empty markdown plan starting with a # heading`)
  const interaction = ctx.get('userQuestions')
  if (interaction === undefined) throw new Error('no user-questions channel is available to review the plan; ask the user to switch the session mode instead')
  let answer: AskUserQuestionAnswer
  try {
    answer = await interaction.ask({
      questions: [{
        id: 'plan-review',
        header: 'Plan review',
        question: 'Approve this plan and leave plan mode?',
        detail: args.plan,
        options: [
          { label: 'Approve', description: 'Leave plan mode; the plan is carried out from the next step.' },
          { label: 'Keep planning', description: 'Stay in plan mode; feedback goes back to the model.' },
        ],
        intent: { kind: 'plan-review', approve: 'Approve', callId: exec.callId },
      }],
      agent, signal: exec.signal,
    })
  } catch (cause) {
    if (cause instanceof UserQuestionError && cause.code === 'ASK_CANCELLED') {
      exec.concludeTurn()
      return { approved: false }
    }
    throw cause
  }
  if (disposed) throw new Error('the plan-mode service was reloaded while the plan was under review; present the plan again')
  const reviewItems = answer.answers.filter(entry => entry.id === 'plan-review')
  const item = reviewItems.length === 1 ? reviewItems[0] : undefined
  if (item?.selected.length !== 1 || item.selected[0] !== 'Approve' || item.custom !== undefined) {
    const feedback = item?.custom ?? ''
    throw new Error(feedback === ''
      ? 'The user chose to keep planning; revise the plan and present it again.'
      : `The user chose to keep planning; their feedback: ${feedback}`)
  }
  this.pendingIntents.set(agent.session, { active: false, narrate: false })
  return { approved: true }
}
```

要点：只有「恰好一个 Approve 且无 custom 文本」才同意；dismiss 不是失败，而是结束 turn；批准不直接 append，而是 silent pending exit。

### 6.6 todo 整表替换与投影

```ts
// packages/todo/tool-todo/src/index.ts:119-134
ctx.sessionProjections.register<'todos', TodoItem[] | null>({
  key: 'todos',
  stateSchema: todosProjectionSchema,
  init: () => null,
  apply: (state, event) => {
    if (event.type === 'todo/write') return event.data.todos
    if (event.type === 'turn/start') return null
    return state
  },
  wire: { viewSchema: todosProjectionSchema, view: state => state },
  stateVersion: 2,
})
```

```ts
// packages/todo/tool-todo/src/index.ts:80-100（节选）
function toTodoList(raw: { content: string; status: string }[], allowParallel: boolean): TodoItem[] {
  const todos: TodoItem[] = []
  const seen = new Set<string>()
  let active = 0
  for (const item of raw) {
    const content = item.content.trim()
    if (content.length === 0) throw new Error('invalid todo: `content` must be a non-empty string')
    if (seen.has(content)) throw new Error(`invalid todos: duplicate content ${JSON.stringify(content)}`)
    seen.add(content)
    if (item.status === 'in_progress') active++
    todos.push({ content, status: item.status as TodoItem['status'] })
  }
  if (!allowParallel && active > 1) throw new Error(`invalid todos: at most one task may be in_progress (got ${active})`)
  return todos
}
```

要点：整表替换、无 id、无 partial update；`allowParallelInProgress` 同时改变模型描述和校验；`turn/start` 清空投影。

### 6.7 approval 层

```ts
// packages/interaction/user-approval/src/index.ts:72-75
const NEVER_SENTENCE = 'Approval prompts are disabled in this session: actions that require approval are rejected automatically — do not request sandbox escalation (do not set `sandbox_permissions`).'
const ASK_SENTENCE = 'Approval policy: ask. Operations that require approval may ask through the configured answerers; without an available answerer, the request fails closed.'
```

```ts
// packages/interaction/user-approval/src/index.ts:215-233（节选）
async request(req: ApprovalRequest): Promise<ApprovalOutcome> {
  const session = req.agent.session
  if (!hasOpenTurn(session)) {
    throw new Error('approval.request() outside an open turn: ... Ask from inside the turn that needs the decision.')
  }
  const id = ApprovalRequestId(randomUUID())
  session.append('approval/asked', { id, toolName: req.toolName, ... })
  const outcome = await this.decide(req, session)
  session.append('approval/decided', { id, outcome })
  return outcome
}
```

要点：approval 是 turn-enclosed 的审计对；`never` 在 service 内部、answerer 之前决定；fail-closed。

### 6.8 sandbox 解析

```ts
// packages/sandbox/sandbox-policy/src/index.ts:164-180
resolve(request: SandboxPolicyRequest = {}): SandboxExecutionPolicy {
  const { session } = request
  return {
    mode: request.mode ?? (session === undefined ? undefined : this.overrideOf(session)) ?? this.defaultMode,
    workspaceRoot: resolveWorkspaceRoot(session?.header.cwd ?? this.workspaceRoot),
    ...session === undefined ? {} : { sessionId: session.id },
  }
}
overrideOf(session: Session): SandboxMode | undefined {
  return this.ctx.sessionProjections.stateOf(session, 'sandboxMode') ?? undefined
}
```

要点：显式 grant > session override > deployment default；每-call 解析；plan 不参与。

### 6.9 三种提示层：section vs runtime context vs UI projection

```ts
// packages/core/system-prompt/src/index.ts:124-179（节选）
const SECTION_ORDERS = {
  HARNESS_IDENTITY: -1000,
  DEPLOYMENT_PERSONA_PREFIX: 0,
  PLAN_POLICY: 500,
  TEAM_POLICY: 600,
  PTC_ONLY: 800,
  FILE_REFERENCE: 900,
  TOOL_BASH: 1000,
  ...
}
const CONTEXT_ORDERS = {
  WORKING_DIRECTORY: 100,
  SANDBOX_POLICY: 110,
  APPROVAL_POLICY: 115,
  SUBAGENT_DELEGATION: 120,
  TEAM_IDENTITY: 125,
  ...
}
```

```ts
// packages/core/agent-loop/src/runtime-context.ts:152-160（节选）
project(current: string, sections: readonly ContextSnapshotSection[]): UserMessage | undefined {
  if (this.retained === undefined && current.length === 0) return
  const snapshot = current.length === 0 ? CLEARED : current
  if (this.retained?.text === snapshot) return
  return createUserMessage({
    content: [{ type: 'text', text: snapshot }],
    ...
  })
}
```

要点：
- plan = system section（order 500，改变会重写 prompt 前缀，KV cache 从 500 起失效）；
- sandbox/approval = runtime context（作为 sourced user message 追加在 history 后，cache 友好，压缩后能重新发）；
- todo = UI projection（不进 prompt）。

### 6.10 Web 评审卡

```tsx
// packages/client/ui-user-questions/src/client/PlanReviewPanel.tsx:57-89（节选）
<div className={css.frame} data-plan-review-key={pending.key}>
  <section className={css.card} aria-label={review.question} aria-busy={busy}>
    <div className={css.strip}>
      <StateDot state={busy ? 'ongoing' : 'warning'} />
      {t('plan.header')}
      <div className={css.previewActions}>
        {renderSlot('conversation.plan-review.actions', { review, requestKey: pending.key })}
      </div>
    </div>
    <div className={css.summary}>
      <h3 className={css.title}>{summary.title}</h3>
      {summary.description !== '' && <p className={css.description}>{summary.description}</p>}
    </div>
    <div className={css.footer}>
      <div className={css.feedback} role="status">{error}</div>
      <div className={css.actions}>
        <Button variant="outline" ... onClick={() => { settle(() => pending.dismiss()) }}>
          {t('plan.discuss')}
        </Button>
        <Button variant="primary" {...tooltip(review.approve.description)}
          disabled={busy} onClick={() => { decide(review.approve.label) }}>
          {t('plan.approve')}
        </Button>
      </div>
    </div>
  </section>
</div>
```

要点：Web 卡只有「Request changes」（dismiss）和「Approve」；没有单独「Keep planning」按钮。通用 question UI 才会渲染工具给的 `Keep planning` 选项。

### 6.11 计划产物从日志参数派生

```ts
// packages/client/ui-plan/src/client/plan.ts:32-48（节选）
export function submittedPlan(event: { readonly type: string; readonly data: unknown }): SubmittedPlan | undefined {
  if (event.type !== 'tool/call' && event.type !== 'tool/ptc-dispatch-start' && event.type !== 'tool/ptc-dispatch') return undefined
  const data = event.data
  if (!record(data) || data.name !== 'exit_plan_mode') return undefined
  const callId = event.type === 'tool/call' ? data.callId : data.subCallId
  if (typeof callId !== 'string' || callId === '') return undefined
  let args: unknown = data.arguments
  if (event.type === 'tool/call') {
    if (typeof args !== 'string') return undefined
    try { args = JSON.parse(args) as unknown }
    catch (_error) { return undefined }
  }
  if (!record(args) || typeof args.plan !== 'string') return undefined
  const markdown = args.plan
  const title = /^#\s+(\S[^\r\n]*)/.exec(markdown.trim())?.[1]
  return title === undefined ? undefined : { callId: callId as ToolCallId, markdown, title }
}
```

要点：plan 的唯一 durable 副本是 tool call arguments；没有 plan 文件；UI 从日志派生卡和预览。

### 6.12 fork / subagent 继承

```ts
// packages/core/session/src/fork.ts:20-25
export function buildForkSeed(events: readonly SessionEvent[], boundary: SessionSeqType): SessionEvent[] {
  const prefix = events.slice(0, boundary + 1)
  prefix.push({ type: 'session/end-seed', seq: SessionSeq(boundary + 1), ... data: { inherited: true } })
  return prefix.concat(openTurnClosers(prefix, { kind: 'forked' }))
}
```

```ts
// packages/subagent/subagent/src/child-agent.ts:238-275（节选）
export function captureDelegatedPolicyOverrides(parent: Agent): DelegatedPolicyOverrides {
  const preset = parent.ctx.get('permissionPresets')?.current(parent.session)
  return {
    permissionPreset: preset === 'auto' || preset === 'danger-full-access' ? preset : undefined,
    sandboxMode: parent.ctx.get('sandboxPolicy')?.overrideOf(parent.session),
    approvalPolicy: parent.ctx.get('approval') === undefined ? undefined : 'never',
  }
}
export function appendDelegatedPolicyOverrides(childSession: Session, overrides: DelegatedPolicyOverrides): void {
  if (overrides.sandboxMode !== undefined) childSession.append('sandbox/mode', { mode: overrides.sandboxMode, source: 'delegation' })
  if (overrides.approvalPolicy !== undefined) childSession.append('approval/policy', { policy: overrides.approvalPolicy, source: 'delegation' })
  if (overrides.permissionPreset !== undefined) childSession.append('permission/preset', { preset: overrides.permissionPreset })
}
```

要点：fork 复制整个前缀，plan/mode 一并继承；spawn 不 seed，子会话 fresh，所以 plan 默认 inactive。子代理只继承父的**显式** sandbox override，approval 固定 `never`（不能向人求助）。

### 6.13 部署方 plan 提示原文（base / standard 相同）

```text
// packages/bundle/web-app/presets/standard.patch.yml:55-65（base 336-346 基本同义，一处措辞不同）
You are in plan mode. Stay in plan mode until the user approves your plan through exit_plan_mode or switches the session mode. Imperative language to implement changes means plan the implementation, not execute it. A user's conversational agreement — including an answer confirming something you asked — approves nothing and does not end plan mode; fold the confirmed decision into the plan and submit it through exit_plan_mode.

Explore first. Use non-mutating reads, searches, static analysis, and checks to ground the plan in the actual repository. Do not edit or write files, change configuration, run formatters or code generation that rewrites tracked files, commit, or otherwise carry out the plan. Prefer existing functions and patterns over new machinery.

The tool catalog stays the same across modes for request-cache stability. These plan-mode rules override any later tool description or guidance that suggests using mutation tools; those tools remain listed to keep the tool catalog unchanged. Do not use todo_write to track this planning phase: it tracks implementation after an approved plan, while the plan itself belongs in exit_plan_mode.

Resolve discoverable facts by inspection. Use ask_user_question only for user-owned choices or material ambiguity that inspection cannot answer. Do not ask the user where code lives or how current behavior works when you can find out.

Make the plan decision-complete: state the goal and success criteria; group implementation changes by subsystem; identify public API, schema, and data-flow changes; cover edge cases, failure modes, tests, acceptance criteria, and explicit assumptions. Keep it concise enough to review but detailed enough that another engineer can implement it without making design decisions.

When ready, call exit_plan_mode with the complete plan markdown, starting with a # title. Make exit_plan_mode the only and final tool call in that assistant response: it presents the plan for approval, and implementation begins only in a later step after approval. Do not paste the final plan as a plain reply or ask "should I proceed?" through prose or ask_user_question. If review rejects it, incorporate the feedback and present again. If the review channel is unavailable or aborted, stay in plan mode and ask the user to switch modes manually; do not proceed with implementation.
```

这是 dsh 最值得直接借鉴的 prompt 资产：它把「不要 execute」「用户口头同意不算批准」「exit 是唯一出口」「计划要 decision-complete」「不要用 todo 做 planning」写死在一个高优先级 section 里。

---

## 7. 计划-执行衔接结论

1. **不是独立 agent**。plan 是 per-agent session 的 boolean stance + prompt section + 常驻 exit tool；没有新 agent、没有新 session、没有 plan 文件。[verified `plan-mode/src/index.ts:1-17`；design note:12]
2. **批准后自动继续，不是人工切换**。批准只记录 silent pending exit；下一个 accepted pre-step 追加 `plan/mode:false`；同一 turn 的后续 step 继续执行。工具结果文案就是「carry out the plan starting with your next step」。[verified `index.ts:294`、`355-359`；test `963-978`]
3. **批准不是权限授予**。plan review 走 user-questions，不走 approval；sandbox/approval 的当前值不变。批准后如果 sandbox 是 read-only，写仍然会被拒。[verified `index.ts:307`；无 sandbox/approval 调用]
4. **计划不是文件，是 tool argument**。完整 markdown 存在 `tool/call`（或 PTC dispatch）的 `arguments.plan`；UI 从 session history 派生卡/预览；布局只存 address。[verified `plan.ts:32-48`；`plan-resource.ts:37-82`]
5. **执行依赖 conversation，而不是 plan artifact 再注入**。`exit_plan_mode` 的结果只有 `{approved:true}` + 一句确认，不包含 plan 文本；批准后没有「把 plan 重新注入」的步骤。短任务没问题；长任务/压缩后，模型可能丢失计划细节，只剩 UI 卡可读。[inferred，但代码没有 re-inject 路径；`index.ts:283-296`]
6. **退出语义有三种，不要混**：
   - Approve：成功，pending exit，继续执行；
   - Keep planning：失败调用，带反馈，同 turn 重做；
   - Dismiss（Web「Request changes」）：成功 `{approved:false}` + `concludeTurn`，plan 保持 active，用户下一轮自己说。[verified `index.ts:331-359`；test `1061-1073`]
7. **软层的真实边界**：模型可以无视 plan 继续写（workspace-write/danger-full-access），可以 spawn 子代理绕过（spawn inactive），可以用 MCP 工具绕过。要硬约束必须叠加 sandbox/approval/tool permission。[verified/inferred，见 §5.4]
8. **最适合的定位**：plan 是「评审门」，不是「执行门」；它把「先给我看计划」变成一次结构化工具调用，适合需要人审的交互式编码会话，不适合无人值守的安全隔离。

---

## 8. 可移植优点清单

### 8.1 Top 5（最值得移植）

1. **Log-only stance + projection + request-boundary commit**
   - 证据：`plan-mode/src/index.ts:45-54`、`136-168`、`196-213`、`425-460`
   - 解决：模式状态可恢复、可多端一致；用户随时切换不会撕裂 in-flight 请求；不需要 live mirror。
   - 成本/风险：M；需要 session event + projection + pre-step hook；pending 崩溃会丢。
   - 与 opencode 形态：**叠加**。把 plan 做成 session 级状态，而不是新 agent；用 request 前 checkpoint 落盘。
   - 建议：移植状态机与「先 `next()` 再落盘」的顺序；把 pending 做成 durable 记录，避免 dsh 的 WeakMap 丢失。

2. **Reviewed exit as the only structured handoff**
   - 证据：`plan-mode/src/index.ts:298-359`；`tests/plan-mode.spec.ts:890-978`；design note:34
   - 解决：把「计划完成」变成一个可审计的 tool call + 用户决策，而不是模型说「我计划好了」或用户说「继续」。
   - 成本/风险：S/M；需要 tool + user-question/approval 原语；review 会阻塞。
   - 与 opencode 形态：**叠加/替代人工交接**。把 opencode 的「plan → build」手切换成工具驱动的审批，同一会话继续。
   - 建议：保留「只有恰好一个 Approve 且无 custom 才算同意」；保留 dismiss=结束 turn 的语义。

3. **三轴分离：collaboration stance / enforcement / task checklist**
   - 证据：design note:24、49、57；`plan-mode/src/index.ts` 不引用 sandbox/approval；`sandbox-policy`/`user-approval` 不引用 plan
   - 解决：避免「切 plan 悄悄改权限」；让 plan × sandbox × approval 组合可解释；每层有独立 owner 和生命周期。
   - 成本/风险：S/M（架构约束）；需要团队接受「plan 不是安全边界」。
   - 与 opencode 形态：**叠加**。opencode 的 permission rules 作为 enforcement 轴，plan 作为 stance 轴；不要塞进同一个 agent mode。
   - 建议：在 Python 项目里显式建三个对象/模块，禁止互相 import 状态。

4. **Review intent over generic user-questions，exact artifact + generic fallback**
   - 证据：`user-questions/src/types.ts:24-35`；`ui-user-questions/src/client/contract/slots.ts:81-101`；`PlanReviewPanel.tsx:57-89`；`user-questions/README.md:31`
   - 解决：不为 plan review 新造协议；UI 可专业化，其他 UI 仍能走通用选项；exact plan 作为 `detail`，approve label 显式命名，callId 可回读。
   - 成本/风险：S/M；需要 question 原语 + intent + 可选专用 UI。
   - 与 opencode 形态：**叠加**。映射到现有 question/permission prompt；不要新造 plan decision 协议。
   - 建议：把「Approve / Keep planning / dismiss」三种结果编码清楚；专用卡与通用 UI 的语义要一致。

5. **Deployment-owned, decision-complete plan prompt + anti-sycophancy rules**
   - 证据：`standard.patch.yml:55-65`；`base/cordis.patch.yml:336-346`；`plan-mode/src/index.ts:68-72`、`104-117`
   - 解决：模型容易把「用户说 go」当批准、把 imperative 当 execute、把计划写得太虚；这段 prompt 逐条封堵。
   - 成本/风险：S；但长 section 每请求都付费，切换会 invalidate KV cache。
   - 与 opencode 形态：**叠加**。放进系统提示的工具说明之前；若 opencode 只有 synthetic reminder，可先注入一次性切换提醒 + 每轮短提醒，但不如 section 稳定。
   - 建议：直接复用其结构（不是逐字），保留「只有 exit tool 是出口」「decision-complete」「不要用 todo 做 planning」。

### 8.2 完整可移植清单

| 机制 | 源码证据 | 解决什么问题 | 成本/风险 | 与 opencode 形态 | 建议 |
|---|---|---|---|---|---|
| log-only boolean + projection | `index.ts:45-54`、`136-168` | 模式状态可恢复、多端一致、无 live mirror | M；需 event/projection | 叠加 | 强烈建议 |
| pending + pre-step commit | `index.ts:196-213`、`425-460` | 用户随时切换不撕裂请求；append 失败不阻塞 turn | M；崩溃丢 pending | 叠加 | 建议；改成 durable pending |
| `set` 返回四态 | `index.ts:425-445` | UI 能区分 committed/queued/cancelled/noop | S | 叠加 | 建议 |
| 用 turnBoundary 判 idle | `index.ts:379-383`、`425-433` | 避免 agent.status 在 checkpoint 期间的假 running | S | 叠加 | 建议 |
| transition narration 只在上次 header 不同 | `index.ts:462-474` | 不重复提示；模型只在 context 真变时被告知 | S | 叠加 | 建议 |
| `exit_plan_mode` 常驻 | `index.ts:277`、test `963-978` | 切换不改变 tool catalog，cache 稳定 | S；但有 schema 成本 | 冲突/权衡 | 仅在 cache 敏感时建议；否则过滤工具 |
| 只有 exact Approve 才算同意 | `index.ts:347-354`、tests `1013-1049` | 防止「approve + caveat」被当 consent | S | 叠加 | 强烈建议 |
| dismiss = concludeTurn + approved:false | `index.ts:331-340`、test `1061-1073` | 用户想自己说时不被强行二选一 | S | 叠加 | 建议 |
| Keep planning = tool error + feedback | `index.ts:349-353` | 模型在同 turn 内修正，不新开 turn | S | 叠加 | 建议 |
| plan review 走 user-questions 而非 approval | `index.ts:307`；design note:59 | 评审不是权限决策，语义更清楚 | S | 叠加 | 建议 |
| intent `plan-review` + exact plan + callId | `user-questions/src/types.ts:24-35`；`index.ts:315-326` | 一个协议，多端渲染；可回读 artifact | S/M | 叠加 | 强烈建议 |
| 评审自动打开 + 侧栏资源只存 address | `PlanCard.tsx:72-83`；`plan.ts:55-60`；`plan-resource.ts:37-82` | 不重复存 plan 文本；reload 可恢复；隐私/体积友好 | M（Web UI） | 叠加（若有侧栏） | 建议，CLI 可只保留卡/全文 |
| plan card 从 tool args 派生 | `plan.ts:32-48`；`plan-definition.ts:15-33` | 无 plan 文件、无第二个 durable home | S/M | 叠加 | 建议；长任务另存 artifact |
| todo 整表替换 + 最小 schema | `tool-todo/src/index.ts:80-100`、`135-211` | 无 id/partial update 竞态；模型所见即日志 | S | 叠加 | 建议 |
| todo `allowParallelInProgress` 部署策略 | `index.ts:29-43`、`49-67` | 并发/单活跃由部署决定，且同步改模型描述 | S | 叠加 | 建议 |
| todo 单 owner + non-agent 拒绝 | `index.ts:194-198` | 避免共享列表/静默写空 | S | 叠加 | 建议 |
| sandbox/approval runtime context | `sandbox-policy/src/index.ts:141-152`；`user-approval/src/index.ts:162-174` | 动态策略不重写 system prompt；压缩后可重发 | M | 叠加 | 建议 |
| 权限预设 bundle 两 knob | `permission-presets/src/index.ts:63-73`、`398-417` | 一个 UI 选择器，两个独立 enforcement | M | 叠加 | 建议；opencode 若已有 permission mode 可映射 |
| subagent 权限 seed | `child-agent.ts:238-275` | 子代理不能自行升权/求助；fork 继承 plan | M | 冲突/需设计 | 建议安全部分；plan 传播需明确 |
| goal round driver | `goal-round-driver/src/prompt.ts:11-27`；README | 长任务同 session 续跑、有 round cap、可暂停 | M/L | 叠加 | 若 ai-coding 有长任务需求再移植 |
| repeat-tool-reminder | base `cordis.patch.yml:447-452` | 对完全重复的工具调用做 advisory nudge | S | 叠加 | 可作为相邻 bonus |
| no generic mode registry | design note:47 | 避免为一个模式造抽象 | S | 叠加 | 建议：plan-specific |

### 8.3 不建议移植（避免为融而融）

| 项 | 不移植理由 | 例外 |
|---|---|---|
| **把 plan 当安全边界** | dsh 自己说 plan 是 guidance；workspace-write/danger-full-access 下可写，spawn/MCP 可绕过 | 只用于交互式人审；硬约束必须另加 sandbox/permission |
| **stable tool catalog（永远暴露 exit_plan_mode + 所有 mutator）** | 为 cache 稳定牺牲了工具面清晰度；inactive 的 exit tool 仍占 schema，调用会报错，可能诱导误调 | 如果 prompt 很长、cache 是瓶颈，且你能接受软约束 |
| **generic named-mode registry** | dsh 已经试过并删除；一个 plan 不值得抽象 | 真正出现第二个 collaboration mode 时再提取 |
| **turn/start 清空 todo** | 与「survives across turns」矛盾；多 turn 实现会丢 checklist | 如果你想要「每 turn 重新聚焦」的 checklist，且文档写清楚 |
| **process-local pending / 批准退出** | 崩溃会丢；工具结果说 approved，恢复后 plan 仍 active；projection/service 可能分叉 | 如果你能接受丢一次点击，或在下次请求前一定落盘 |
| **阻塞式 review 无 timeout** | headless/自动化会卡住；无 answerer 直接失败 | 交互式 Web/CLI；否则加 timeout/auto-approve |
| **plan 文本只存在 conversation** | 长执行/压缩后模型可能丢失计划；没有 re-inject 路径 | 短任务；或另存 approved plan 并在执行开始注入精简版 |
| **Web 专属 sidebar/card 全套** | 成本高；CLI/TUI 不需要 | 有 Web/桌面端且要长期回看计划 |
| **把 plan review 塞进 approval seam** | dsh 明确拒绝：评审不是 permission decision，需要 exact artifact + free text + tool-call 语义 | 无；用 question/intent 更合适 |
| **plan 作为独立 agent** | 丢失同会话连续性、review callId、上下文；dsh 刻意选择同一 session | 如果你需要硬隔离/不同模型/不同工作区，才用独立 agent |
| **`firstHeading` 接受 H1-H6 但工具要求 H1** | 小不一致；`##` 计划会被拒，但标题提取器会接受 | 统一成 H1，或放宽校验 |

### 8.4 移植到 Python/ai-coding 的最小蓝图

1. **状态**：`plan_active: bool` + `plan_pending: {target, narrate}`；在 session 事件表追加 `plan/mode`；用 request 前 checkpoint 落盘。
2. **请求组装**：在工具说明之前插入 `plan_policy` system section；`pending` 目标也生效；`active` 时渲染，否则空。
3. **命令/入口**：`/plan`、`/plan off`、`/plan <msg>`；`/plan <msg>` 走普通用户消息队列（steering），不要伪造 system message。
4. **工具**：`exit_plan_mode(plan: str)`；校验 H1；复用现有 question/permission prompt，带 `intent='plan-review'`、`approve` label、`call_id`；approve/keep/dismiss 三态。
5. **强制层（关键差异）**：如果 ai-coding 有 dispatch-time permission rules，在 plan active 时 deny `write/edit/bash` 的变更操作（或 sandbox read-only），allow `read/grep`/`exit_plan_mode`；决定是否 gate `subagent`/MCP/network。不要只抄 dsh 的 soft。
6. **产物**：把 plan 存在 tool call args；如需长执行，另存 approved plan（文件/DB）并在执行第一步注入精简版；UI 只存 reference。
7. **todo**：整表替换、content+status、单 owner、并行 flag；明确 turn-scoped 还是 session-scoped；不要自动 nudge。
8. **持久化**：事件日志 + projection；压缩只替换 surface；pending 用 durable 记录或明确文档化丢失；resume/fork 测试。

---

## 9. 文档与代码的冲突记录

| # | 冲突 | 文档 | 源码/测试 | 结论 |
|---|---|---|---|---|
| C1 | plan prompt 顺序 | `docs/subsystems/plan.md:29`、`plan.zh.md:29` 说 **order 50** | `packages/core/system-prompt/src/index.ts:134` 是 `PLAN_POLICY: 500`；`packages/plan/plan-mode/README.md:133,147` 也说 500 | 以 **500** 为准；50 是文档错误。500 位于 persona(0) 与 tool sections(1000+) 之间；位置在前，且 prompt 文本明确声明覆盖后续工具描述 |
| C2 | todo 是否跨 turn 存活 | `packages/todo/tool-todo/README.md:12` 说「survives across turns」 | 同文件 `:93` 说「cleared when the next turn starts」；`src/index.ts:127-130` 在 `turn/start` 返回 `null`；`tests/projection.spec.ts:84-96` 断言清空 | 以**清空**为准；README 摘要与实现矛盾。todo 是 turn-scoped UI checklist，不是跨 turn 持久清单 |
| C3 | pending 是否可从日志恢复 | `docs/subsystems/plan.md:11` 说 resume/fork/compaction 恢复 `{active,pending}` | `plan-mode/src/index.ts:186` 的 pending 是进程内 `WeakMap`；文档 `:17` 也承认「after final pre-step remains process-local and is lost」 | 只有 command 生命周期驱动的 pending 可从日志重建；直接 `set`/批准退出的 pending 不可。line 11 的「both」是过度概括 |
| C4 | pre-step listener 是否 prepended | `docs/subsystems/plan.md:17` 说「prepended agent/pre-step listener」 | `plan-mode/src/index.ts:196` 用 `ctx.on(...)`，默认追加（`vendor/cordis/src/events.ts:143`：`prepend ? unshift : push`） | 行为上因为它先 `await next()` 再 append，仍满足「下游接受后落盘」；但注册位置不是 prepend。插件加载顺序变化时需复核 |
| C5 | plan unit 是否「optionally registered」 | `docs/subsystems/plan.md:11` 说「optionally registered plan unit」 | `plan-mode/src/index.ts:226` 在构造函数里无条件 `register(planProjectionDefinition)`；只有整个插件可选 | 措辞不精确：包可选，unit 在包内必注册。依赖 registry 时会 fail loud |
| C6 | `get()` 是否要求 turnBoundary | `docs/subsystems/plan.md:11` 说「first dependent access fails if registry, plan key, or turnBoundary absent」 | `get()` 只读 `plan`（`index.ts:403-407`）；`set()` 才读 `turnBoundary`（`379-383`） | 轻微不精确；`get` 不会因 turnBoundary 缺失而失败 |
| C7 | 计划首标题级别 | 工具描述/校验说「starting with a `#` heading」（H1） | `firstHeading` 接受 H1-H6（`index.ts:89-95`）；工具校验 `^#\s+\S` 只接受 H1（`:304`）；UI 标题 regex 也是 H1（`plan.ts:46`） | 以**工具校验 H1** 为准；`firstHeading` 的 H1-H6 是更宽的展示函数，实际到不了非 H1。建议统一 |
| C8 | plan review 的「Keep planning」按钮 | `plan-mode/README.md:61-63` 描述 `Keep planning` | Web 专用卡只有 `Request changes`（dismiss）+ `Approve`；`PlanReviewPanel.tsx:74-85`；`ui-user-questions/README.md:42` 明确「no separate refusal button」 | 不是冲突：Web 选择「dismiss 回编辑器」；通用 question UI 才渲染工具给的 `Keep planning`。两种路径都保持 plan active |
| C9 | plan review 是否受 approval `never` 影响 | 文档没有明说 | `exit_plan_mode` 用 `userQuestions`（`index.ts:307`），完全不走 `ctx.approval`；`never` 只拒绝 approval `request`（`user-approval/src/index.ts:275`） | `never` 不会阻止 plan review。这是设计使然，但产品文案需避免让用户以为「全权模式就没有任何询问」 |
| C10 | plan 模式是否「不限制 agent」 | `plan-mode/README.md:32` 说 every tool stays callable | 测试 `plan-mode.spec.ts:581-589` 证明 `write`/`bash` 在 plan 下执行成功；design note:72 也说可 mutate | 一致：plan 是 guidance，不是 enforcement。不要把它当安全边界 |

---

## 10. 未解问题

1. **进程在 plan review 等待期间重启**：tool call 已记录但无 result；`userQuestions` projection 是否能重新发起/继续？我没有找到覆盖该场景的测试。e2e 只覆盖浏览器重连（`user-questions/README.md:35`）。[unknown]
2. **批准退出在崩溃后的语义**：`exit_plan_mode` 返回 `{approved:true}`，但 `plan/mode:false` 只在下一个 pre-step 落盘；若进程在中间退出，恢复后 plan 仍 active。README 的 pending 限制覆盖这一点，但没有明确说「批准也会丢」。[inferred]
3. **projection pending 与 service pending 分叉**：命令驱动的 pending 在日志里，直接 `set`/批准的在 WeakMap；崩溃后 UI 可能显示 pending，但 service 没有意图。是否应该在恢复时清掉 projection 的 pending？没有代码处理。[inferred]
4. **plan 文本在压缩后的执行可用性**：批准后没有 re-inject；`tool/call` 是 log-only，但模型 derived history 的 assistant message 可能被 compaction shadow。长执行/压缩后模型是否还能看到计划？没有测试。[inferred risk]
5. **plan review 无 timeout**：`interaction.ask` 是阻塞式；如果 UI 不答，turn 一直等。是否应由部署配置 timeout/auto-approve？没有。[verified code；设计未覆盖]
6. **`Request changes` vs `Keep planning` 的反馈差异**：Web 的 dismiss 不把「拒绝原因」作为结构化反馈；模型只收到 `{approved:false}` + 一句「user dismissed」。通用 UI 的 Keep planning 会带 feedback。产品上是否要统一？[verified 行为；未解设计]
7. **plan 模式下 subagent/MCP/network 的绕过**：dsh 明确不解决；如果 ai-coding 要硬 plan，需要额外设计（spawn 传播 plan、MCP mutability metadata、network gate）。[verified gap；移植需决策]
8. **plan 模式是否应限制 network/process**：sandbox 只治理文件效果；read-only 下仍可 curl/spawn。若 plan 的语义是「只探索」，需要额外的网络/进程策略。[verified sandbox 文档；未解]
9. **`firstHeading` H1-H6 vs 校验 H1**：小的内部不一致；是否放宽工具校验到 H2？[verified；未解]
10. **ACP/headless 的 plan**：ACP 明确不支持 plans/commands/modes；headless 没有 user-questions answerer，`exit_plan_mode` 会失败，只剩 `/plan off`。如果 ai-coding 有 headless 场景，需要决定是否提供 auto-approve 或禁用 plan review。[verified docs；未解产品决策]
11. **todo 的 turn-scoped 语义**：清空是代码/test 事实，但 README 摘要说 survives；产品意图到底是「每 turn 重新聚焦」还是「跨 turn 持久」？需要产品决策。[doc conflict；未解]
12. **plan 批准后是否应自动创建 todo**：dsh 没有；plan prompt 说 todo 属于实现阶段，但没有自动映射。是否要在 ai-coding 中把 approved plan 转成 todo items？[verified no code path；未解]

---

## 附：一句话总结

dsh 的 plan 模式是一个**可持久化的 per-session boolean + 高优先级提示段 + 一个常驻的评审工具**；它刻意不做工具过滤、不改 sandbox/approval、不建 plan 文件、不换 agent。真正值得移植的是它的**状态机（pending + request-boundary commit）、评审语义（approve/keep/dismiss）、三轴分离、intent 化评审 UI、以及那段 decision-complete 的 prompt**；不建议照搬的是**soft-only 的安全假设、稳定 tool catalog 的 cache 取舍、turn-start 清空 todo、以及 process-local pending/批准退出**。
