> 来源：并行调研 agent 产出（只读调查），高级工程师复核；供"长任务编排架构设计"使用。
> 抽检指引：核心论断的行号引用见文末；未确认项保留在文末。
> 姊妹篇：`03-opencode编排数据流.md`（单回合数据流）。

# opencode 长任务（long-horizon）机制专项报告

- 仓库：`/home/linsir365/projects/opencode`
- HEAD：`0a46301e3`（`chore(core): refresh bundled models-dev snapshot`）
- 范围：跨多回合/久跑/崩溃恢复/防漂移的机制。**不重复** `opencode-orchestration-flow.md`（单回合 drain/step/tool 数据流）；引用该报告时只做指针。
- 方法：纯只读代码调查。所有论断带 `文件:行号`（相对仓库根）。无法证实的集中在"未确认"节。
- 术语：本文"回合（turn）"= 从一批输入被 promote 到 drain 返回 `Complete` 的整个 busy period；"段（step）"= drain 内一次模型请求+工具执行。

---

## 1. agent 模式 / mode / plan

**字段定义**（`packages/schema/src/agent.ts:23-35`）：

- `mode: Schema.Literals(["subagent", "primary", "all"])`（:30）；`hidden`（:31）；`steps`（:33）；`permissions`（:34）。
- `Agent.Info.default(id)`（:39-53）：mode 默认 `primary`，hidden=false，权限默认 `{action:"*",effect:"allow"}` + 外部目录 ask + `.env` 读 ask。
- 配置层同名字段在 `packages/schema/src/config/agent.ts:11-22`（含 `disabled`；mode 可选）；legacy `{agent,agents}/*.md` 与 `{mode,modes}/*.md`（后者强制 mode=primary）在 `packages/core/src/config/plugin/agent.ts:21-26, 205-208` 加载，`item.steps` 直通（:120）。

**内置 agent**（`packages/core/src/plugin/agent.ts`）：

| id | mode | 关键行为 | 证据 |
|---|---|---|---|
| `build`（默认） | primary | 默认 agent，权限加 `question allow` | :18（默认 ID）、:87-92 |
| `general` | subagent | 通用多步执行；禁 `question`、禁 `subagent`（不能嵌套） | :94-103 |
| `explore` | subagent | 只读探索：`* deny` 后仅放行 grep/glob/webfetch/websearch/read；system=PROMPT_EXPLORE | :105-131 |
| `compaction` / `title` / `summary` | primary + hidden | 工具全禁（title/summary）；title/summary 带专用 system | :133-153 |

**mode 的运行时语义**：主选（primary 列表）过滤掉 `mode==="subagent"` 与 hidden（`packages/core/src/agent.ts:92-93`）；subagent 工具拒绝调用 `mode==="primary"` 的 agent（`packages/core/src/tool/plugin/subagent.ts:136-137`），可用列表过滤 `mode!=="primary"`（:288-295）。

**plan 模式是插件实现的内置 agent**（`packages/core/src/plugin/plan.ts`，注册于 `packages/core/src/plugin/internal.ts:221`）：

- agent（id=`plan`，name=Plan，mode=primary）：`edit` 全 deny，仅 `~/.opencode/plan/*` 允许（plan.ts:33-43）；`question` 允许。
- 进入/离开方式：`session.agent.selected` / `session.created` durable 事件 → 向会话投递 `<system-reminder>`（resume:false，不唤醒执行）；进入提醒要求"只在 Plan 目录写文件、不要借 subagent 改文件、用户让实施时要求切换 agent"（plan.ts:14-25, 76-97）。
- 漂移防护：每次请求 `session.context` hook 检查 messages 里最后一条 reminder，被 compaction 剥掉则重插（放在用户消息前、尾部，保住 prompt cache 前缀），并持久化为 synthetic（plan.ts:58-74）。
- 被工具层拦截时的错误改写：plan agent 下 edit/write/patch 因 `Permission.BlockedError` 失败时，`execute.after` 把错误换成"不能修改 Plan 目录外文件"（plan.ts:45-54）。
- **注意**：这是提示词+权限层面的只读约束；plan agent 并未在权限上禁止调用 subagent（`* allow` 仍生效），"不要让 subagent 改文件"只是 reminder 文本。

结论：有内置 plan 模式，但它是**插件补充**（core 卸载到 plugin），核心 schema 只提供 mode 枚举。

---

## 2. step 上限语义（MAX_STEPS_PROMPT）

- 触发判断：`step >= loaded.agent.info.steps`，`steps` 未配置则**永不触发**（`packages/core/src/session/runner/llm.ts:223`）。
- 触发动作（llm.ts:237-241）：
  1. 请求消息尾追加**一条 assistant 角色消息**`MAX_STEPS_PROMPT`（llm.ts:238；`Message.assistant` 确为 role=assistant，`packages/ai/src/schema/messages.ts:270`）；
  2. `toolChoice: "none"`（llm.ts:241）；注释明确"工具定义留在请求里以保住 provider 缓存前缀"（llm.ts:240）。
- 硬约束不止 toolChoice：即使 provider 无视 `none` 仍发出 tool-call，本地执行器直接返回 `Tool.Error "Tools are disabled after the maximum agent steps"`（`packages/core/src/session/runner/step.ts:89-90`）。
- 本步结束后强制结束回合：`needsContinuation = toolChoice!=="none" && record.needsContinuation`（step.ts:272），即"到顶的那步之后 drain 一定退出"（llm.ts:270-271, 295）。
- 提示词内容（`packages/core/src/session/runner/max-steps.ts:1-16`）：要求声明到顶、总结已完成、列出未完成、给出后续建议；"任何工具调用都是 critical violation"。
- **上限默认几？没有默认值**：`steps` 在所有 schema 里都是 optional（`packages/schema/src/config/agent.ts:19`）；内置 agent 均未设置（plugin/agent.ts 全文）；官方文档明确"If this is not set, the agent will continue to iterate until the model chooses to stop or the user interrupts"（`packages/web/src/content/docs/agents.mdx:295`）。
- **定性**：官方定位是成本控制（agents.mdx:291-293 "users who wish to control costs"），即**防烧钱**为主；强制"总结+遗留任务清单"的收尾格式附带了一次防漂移快照，但它不改变"到顶即断、需用户再输入"的事实。v1 的 `maxSteps` 已弃用映射到 `steps`（`packages/core/src/v1/config/agent.ts:34-37,62`）。
- step 计数在**新输入 promote 时重置为 1**（llm.ts:178），跨 Location move 通过 `Continuation{step}` 延续（`packages/core/src/session/runner/index.ts:21`；llm.ts:58）。

---

## 3. todo / 模型可见任务清单

**没有。** 证据：

- 当前工具注册表（`packages/core/src/plugin/internal.ts:233-245` 的内置工具插件挂载 + `packages/core/src/tool/plugin/` 目录列表：edit/file-diff/glob/grep/mcp-resource/opencode/question/read/shell/skill/subagent/webfetch/websearch/write）中不存在 todo/task 工具。
- v1→v2 数据迁移明确把 `todowrite` 列为**已移除工具**并只发一条"该工具已不存在"的历史说明：`packages/core/src/database/v1-migration.bun.ts:901`（`REMOVED_TOOLS = ["todowrite"]`）、:921。
- 唯一相关残留：旧迁移创建过 `todo` 表（`packages/core/src/database/migration/20260127222353_familiar_lady_ursula.ts:76-85,104`），但全仓没有当前代码读写它（grep 无命中）；未发现 drop 迁移。
- 事件层、插件层也无 todo 事件/清单机制。

模型侧的长任务"清单"只能靠：plan 模式的对话式计划（§1）、压缩摘要里的 Work State/Next Move（§8）、或用户自定义 skill/命令。

---

## 4. subagent：子会话生命周期与上下文边界

实现：`packages/core/src/tool/plugin/subagent.ts`（工具名 `subagent`，注册于 internal.ts:243）。

- **入参**：agent/description/prompt/model/sessionID(续聊)/background（subagent.ts:29-48）。
- **深度限制**：沿 parentID 链数祖先，`depth >= limit` 拒绝；`limit = config.experimental.subagent_depth ?? 1`（subagent.ts:117-133；schema 默认 1：`packages/schema/src/config/experimental.ts:11-13`）。
- **权限**：`permission.assert(action:"subagent", resource:<agentID>)`（subagent.ts:138-151）。
- **子会话创建**：`sessions.create({parentID, title, agent, model})`（subagent.ts:185-198）→ `Session.create`（`packages/core/src/session.ts:262-318`）：新 session 行；继承 location/metadata/permissions（:268,286-288）；**不复制父消息历史**（子树从空历史开始）。模型侧提示："New child sessions start with fresh context, so include all relevant context"（subagent.ts:58）。
- **上下文隔离边界**：父只把 `prompt` 文本（新建时前缀 "You are a subagent spawned by another session."，subagent.ts:208-211）交给子；子的 instruction 基线、工具快照按子 agent 权限独立生成（`packages/core/src/session/context.ts:121-161` 对每个 session 重新 select）。后续父↔子之间没有隐式上下文共享，只有下面两条显式回注。
- **前台（默认）**：`jobs.block` 等待子会话运行结束（subagent.ts:234-240；interrupt 时连同子会话一起中断 :235-239）；结果=子会话最后一条 completed assistant 文本（`packages/core/src/session/subagent-job.ts:44-52`；`subagent-completion.ts:10-18`），作为普通工具输出 `<subagent sessionID state="completed">…</subagent>` 进入父历史（subagent.ts:252-265）。
- **后台（background=true）**：立即返回"在后台运行"说明（subagent.ts:19-27,229-232）；完成后由 `SubagentJob.notify`（subagent-job.ts:23-34）→ `SubagentCompletion.deliver`（subagent-completion.ts:20-44）以 **synthetic 消息（delivery 默认 steer）注入父会话**（:36-43），从而唤醒父会话继续。父被明确要求不要轮询（subagent.ts:24）。
- **崩溃恢复钩子**：start 时登记 `Job.Recovery{kind:"subagent"}`（subagent.ts:220-227），重启后由 §6 的 recoverSubagent 续跑并回注（restart.ts:138-190）。
- **失败可见性**：失败/取消返回 ToolFailure 但保留 sessionID，模型可用同一 sessionID 续聊（subagent.ts:245-251; `sessionID` 入参续聊 :153-182，切 agent/model :169-181）。

---

## 5. steering / queue 完整语义

**定义**：`Delivery = "steer" | "queue"`（`packages/schema/src/session-inbox.ts:10`）。

- **核心差异**（语义注释 `packages/core/src/session/inbox.ts:43-48`）：
  - `steer`：可在**步边界**（回合进行中）晋升，模型马上看到；
  - `queue`：只在**空闲/新回合边界**晋升，且一次只晋升一条（"下一回合的活"）。
- 判定实现：
  - `nextPromotable(scope)`：steer 行优先；scope="input" 时才看 queue 行（inbox.ts:407-424）。
  - runner 每步边界调用 `promote(..., entering && !continuing ? promotable : "steer")`（`packages/core/src/session/runner/llm.ts:163-171`）；drain 起步用 `promotable`："steer" scope 的 drain（唤醒 source=steer）遇到只有 queue 项时直接 `Complete`（llm.ts:61-66）。
  - 回合中 `continuing=true` 时只晋升 steer（llm.ts:80-87）。
- **promote 内部次序**（inbox.ts:496-531）：先全部 steer；若队列头是 compaction/move 控件则让 runner 先处理（返回 undefined，:508,521）；queue 场景晋升一条后再补晋升"期间新到的 steer"（:522-528）。compaction 控件会被重排到 steer 前面，保证蒸发前的 steer 文本不被吞进摘要（inbox.ts:533-549 注释+实现）。
- **控件项**：compaction/move 与 prompt 共用 inbox，优先级更高（llm.ts:88-160 处理）。
- **InboxDeliveryChanged**：durable 事件（`packages/schema/src/session-event.ts:232-237`），发布者 `SessionInbox.steer/queue`（inbox.ts:240-260），投影 `projectDeliveryChanged`（:377-384，带 from→to 乐观锁），客户端消费改 pending 展示（`packages/client/src/solid/data.ts:765-766`）。
- **唤醒行为**：把 queue 项改成 steer 会主动 `execution.wake`（`packages/core/src/session/session.ts:134-140`）；改成 queue 不唤醒（:141-144）。
- **用户落点**：
  - TUI：Enter=steer（`packages/tui/src/component/prompt/index.tsx:1092`）；Leader+Enter=queue（:446-453；`packages/tui/src/config/keybind.ts:196`）；队列面板可对排队消息 steer/删除/撤回（`packages/tui/src/routes/session/index.tsx:611-673`，走 PATCH inbox 路由）。
  - HTTP：`session.inbox.update`（`packages/protocol/src/groups/session.ts:632-641`）→ handler（`packages/server/src/handlers/session.ts:560-572`）；cancel 路由 :619-630/ handler :552-558。
- **长任务中途纠偏标准用法**：① 直接发 steer（下个步边界生效，不打断当前执行）；② 先 interrupt 再 resume——`SessionExecution.interrupt(..., {resume:true})` 只按 steer/control 项重新唤醒，queued 输入保持停泊（`packages/core/src/session/execution.ts:153-170` 注释）；③ 排队消息面板把 queue 改 steer 立即插队。
- doorbell/合并语义：执行中到达的输入只 ring doorbell，drain 退出前再 drain 一次；门铃跨 drain 存活并可能启动 successor execution（`packages/core/src/session/run-coordinator.ts:74-85,114-120,136-145`；合并 scope 取最宽值 :140-141）。

---

## 6. 崩溃自动续跑（claim + SessionRestart）

**是自动恢复，不需要人重发 prompt。**

- **claim（写前意图）**：回合开始随 `Execution.Started` 事件在同一提交里写 `session_v2.time_suspended`（`packages/core/src/session/execution.ts:76-83,112-116`；`SessionStore.claim`：`packages/core/src/session/store.ts:205-216`，null 守卫使重复 claim 变 no-op）。
- **释放规则**：成功/失败/用户中断都释放 claim（execution.ts:119-147）；**shutdown 中断故意保留 claim**，作为"下一进程续跑"的持久信号（:128-135 注释）。用户/超时中断会释放（:130,134）。
- **开机清扫**：启动时调用 `resumeSuspendedSessions`：
  - managed server：`packages/server/src/process.ts:232-239`（boot 时 fork）；
  - 可嵌入 server：`packages/server/src/fetch.ts:47`；
  - SDK host：`packages/sdk/src/internal/host.ts:52`。
- **清扫逻辑**（`packages/core/src/session/execution/restart.ts:192-231`）：
  1. 只扫**顶层会话**（`parent_id IS NULL` 的 suspended 行，store.ts:194-204）；
  2. 子会话 claim 先统一释放（`releaseChildClaims`，store.ts:225-238），子的续跑走 subagent job 恢复；
  3. 每个孤儿 claim：`countResume` durable 计数（store.ts:239-251）；`> maxAttempts`（默认 10，restart.ts:30-33）→ 发布 `Execution.Failed`(RESUME_EXHAUSTED)+释放 claim（restart.ts:82-91），防崩溃循环；
  4. 否则投递 synthetic 消息 "The server restarted while you were working. Continue from where you left off without repeating completed work."（restart.ts:15-16,92-98），然后 `execution.resume(sessionID)`（:220-228）。
- **恢复是 at-least-once**：注释明确"local coordination prevents concurrent drains, not repeated external side effects after a crash"（restart.ts:52-54）——工具副作用可能重复执行。
- **后台任务恢复**：
  - shell job：重启后被取消并投递结果通知（resume:false，不唤醒空闲 owner）（restart.ts:101-136）；
  - subagent job：若子会话仍 running 则重新续跑并回注到父（restart.ts:138-190）。
- **drain 起步的脏数据清算**（llm.ts:68-69）：
  - `settleStaleCompactions`：把仍 `status=running` 的 compaction 消息（进程死亡跳过 finalizer）补发 `Compaction.Failed{type:"compaction.interrupted"}`（llm.ts:299-327）。含义：历史边界只看 `completed` 的 compaction（`packages/core/src/session/history.ts:36-41`），所以被中断的压缩本来就不算拼音边界，此处只是让消息状态与事件流一致、UI 不残留假 running；
  - `settleStaleToolCalls`：把 streaming/running 状态的工具调用补发 durable `Tool.Failed "Tool execution interrupted: <name>"`，subagent 嵌带子 sessionID 提示，保留 `executed` 标记（llm.ts:329-352）。含义：崩溃后模型看到的是"该工具被中断"，而不是永远悬空的 running。

---

## 7. 跨回合记忆 / summary / 检索

**唯一进入下一个模型请求的"会话记忆"是 compaction checkpoint（摘要）+ durable synthetic 通知；没有任何 embedding/recall/长期记忆。**

- **compaction 摘要即跨回合记忆**：后续请求开场注入 `<conversation-checkpoint><summary>…</summary><recent-context>…</recent-context></conversation-checkpoint>`（`packages/core/src/session/runner/to-llm-message.ts:303-324`）；历史读取以最近 completed compaction 为边界（history.ts:28-60）。
- **摘要是"滚动更新"而非每轮重写**：存在上一次 checkpoint 时用 update 版提示（"Update the existing checkpoint…新历史优先…IR 遗留任务重排"）（compaction.ts:156-172）；旧版臃肿摘要会被一次性降噪重写（:158-167；LEGACY_HEADING "## Additional Context" :178-179）。
- **title**：独立 hidden agent `title`（plugin/agent.ts:139-145, 26-69 的 PROMPT_TITLE）；生成/再生成读受限历史（8k 字符窗口、首条用户消息 2k，`packages/core/src/session/title.ts:20-21,100-126`）。**title 只入库会话行，不进入模型请求**（未见任何 baseTranscript 引用）。
- **durable synthetic 消息是一种正式记忆**：重启续跑提示、subagent 完成回注、流中断续写提示都持久化为 session 消息并进入后续请求（作为 user 消息渲染，to-llm-message.ts:284-285；事件定义 session-event.ts:279-289）。
- **`summary` agent 已定义但无消费方**：PROMPT_SUMMARY 在 `packages/core/src/plugin/agent.ts:71-81,147-153`，但全仓搜索不到 `make("summary")` 的调用点（只有定义本身）。见"未确认"。
- **无检索类机制**：无 memory/recall/vector 搜索（grep 无命中）；`session.generate`（`packages/core/src/session/generate.ts:26-73`）只是"不带工具循环的一次性生成"，被 TUI `/btw` 用来问侧问题，不写历史（`packages/tui/src/feature-plugins/prompt/btw.tsx:15-18,69`）。
- **session 级可编程指令状态**：API 管理的 per-session instruction entries（`packages/schema/src/instruction-entry.ts:1-22`；表/store `packages/core/src/session/instruction-entry.ts`）——外部集成可给会话挂 JSON 值，参与 instructions 合成（context.ts:150-158 的 entries.load）。这是"外部注入的记忆"接口，不是模型自发记忆。

---

## 8. native 压缩对"长任务不丢信息"意味着什么

判断链：模型级开关 `settings.compaction: summary|native`（`packages/schema/src/model.ts:61`；`packages/schema/src/provider.ts:35-41`）→ 只有 route 声明 `compact` 的 provider 才能 native（openai-responses：`packages/ai/src/protocols/openai-responses.ts:317-321` endpoint=`ResponsesCompaction` + trigger=`ResponsesCheckpoint`；azure/xai 复用其 endpoint：`packages/ai/src/providers/azure.ts:41`、`xai.ts:37`）→ 模型解析时校验，配了 native 但 route 不支持直接报错（`packages/core/src/model-resolver.ts:183-192`）。

**两类 native 机制**（`packages/core/src/session/compaction.ts:309-357`）：

1. **endpoint 机制**：provider 自行返回"replacement window"；本地原文不保留进该 checkpoint（但原消息仍在 DB，见下）。`LLMClient.canCompact` 判定 `route.compact.endpoint`（`packages/ai/src/route/client.ts:236-248`）。
2. **trigger/checkpoint 机制**：`llm.compact(request, {mechanism:"trigger"})` 返回 provider 加密 checkpoint；本地**另把 `keep` 预算内的最新完整用户消息以原文文本拼回**（compaction.ts:339-346；`recentUserMessages` :839-851）——即"用户说过的话"在 native 模式下也有明文保底。

**存储与可重放性**：checkpoint 编码为 `{version, provenance, messages}`（`packages/core/src/session/provider-context.ts:51-55`），provenance=baseURL/path/query 的 sha256（:17-36）；解码只在同一 endpoint（provenance 完全一致）时进行（history.ts:23-26 `replayable`；to-llm-message.ts:305-307）。含义（按代码事实，不吹）：

- **native window 与具体 endpoint 绑定**，换 baseURL/route 后不可重放；此时历史读取会跳过该 checkpoint、回落到原始 transcript（history.ts:16-26 与 :103-107 注释；"local" boundary 下 native 窗口不作为边界）。
- 加密 window 本地**无法度量大小**（estimatePart 视 compaction part 为不可测，compaction.ts:926-928），所以 `due()` 在同 provider 出现"已测量 prompt"前不再触发下一次自动压缩（compaction.ts:227-236）。
- 所以"不丢信息"实际由三层保障：① provider 的窗口替换（质量不可本地审计）；② trigger 模式下 `keep` 内用户原文保留；③ DB 原始消息永远在（只是不再进入请求）。native 压缩**不承诺**本地可验证的保真度。

**summary fallback 的 keep 预算**：

- 默认 `keep = 15_000` tokens（`packages/core/src/session/compaction.ts:189-197` 初值；`Settings.keep` 注释 :42-43）。
- 切分规则：`splitConversation` 取"最新一段 ≤keep tokens 的消息"原文保留在 `<recent-context>`，其余交给模型做结构化摘要；永远至少保留最新一条交换；无更老内容可摘时要留旧摘要而不重复摘要（compaction.ts:687-723）。
- 摘要模板专为长任务交接设计：Objective/Requirements/Decisions/Work State(Completed/Active/Blocked)/Next Move/Relevant Files(≤15)/Important Context（:101-132）；规则要求保留 exact paths/symbols/commands、保留未决问题原话、如实记录改动是否已提交/推送（:134-140）。
- 自动触发阈值：`ceiling = window - max(10%, min(16k unless window<32k))`，即至少留 16k 余量（:890-897）；overflow 恢复按被拒请求的 70%→50%→35% 收缩（:91-92,420-427）。

---

## 9. 命令层：用户手里的长任务工具

**核心命令服务**：`packages/core/src/command.ts`（Command.Service）；`session.command` HTTP → `SessionCommand.execute`（`packages/core/src/session/command.ts:11-35`，默认 delivery=steer :32）。命令定义有三类来源：

- **内置**（`packages/core/src/plugin/command.ts`）：`/init`（AGENTS.md 引导模板，:29-41）、`/review`（review 模板，:42-54）；全部走 `ctx.session.prompt` 注入，是一次普通 steer 输入。
- **MCP prompts**：每个 MCP prompt 注册为 `<server>:<prompt>` 命令（plugin/command.ts:55-81）。
- **用户/项目自定义**：`{command,commands}/**/*.md`（`packages/core/src/config/plugin/command.ts:144-187`），支持 `$1/$ARGUMENTS` 与 `!\`shell\`` 插值（:189-241）；`command.subagent`（或旧名 subtask/ agent 为 subagent 时）会**自动开子会话后台跑**（:98-123，subagents.start+background）。

**TUI 会话级 slash 命令（长任务相关）**（`packages/tui/src/routes/session/index.tsx`）：

- `/compact`（:942-965）→ client（`packages/client/src/solid/data.ts:1503-1530`）→ `POST /api/session/:id/compact`（`packages/protocol/src/groups/session.ts:498-514`，默认 steer：步边界执行，不排在一个 queue 之后）→ `Session.compact` admitCompaction（`packages/core/src/session/session.ts:246-264`）；同一 session 同时只允许一个 compaction inbox 项（inbox.ts:204-228）。
- `/undo` /redo（revert stage/commit，:973-1010 区域）、`/fork`（会话分叉）、`/share`、`/export`/`/copy`、`/rename`、`/timeline`。
- 全局：`/sessions`（别名 resume/continue，`packages/tui/src/app.tsx:704`）、`/btw`（旁路提问）、`/diff`、`/stats`、`/skills`、`/worktrees` 等。
- **Agent 切换（=进入/离开 Plan 模式）**：TUI agent 对话框（`packages/tui/src/component/dialog-agent.tsx:7-30`）→ `switchAgent`（`packages/protocol/src/groups/session.ts:328-339`）→ durable `AgentSelected`（`packages/core/src/session/session.ts:89-95`）。
- **模型可见的会话工具**：`opencode.session_rename` / `session_move` / `models`（`packages/core/src/tool/plugin/opencode.ts:82-198`）。

**用户侧纠偏与恢复面**：interrupt（含 resume 选项）HTTP 路由 `packages/protocol/src/groups/session.ts:760` 相关；inbox cancel/update 路由（:619-641）。

---

## 10. 总评

**"长任务不漂移"的核心机制（core 自带，5 个）**：

1. **Durable inbox + 步边界/空闲边界两档晋升（steer/queue）+ interrupt-resume** —— 让"中途改道"可无损注入且不丢工作（§5）。
2. **消息史全量持久化 + 每步从 DB 重建请求 + compaction checkpoint 滚动摘要协议** —— 模型上下文永远由事件流投影而来，摘要带"遗留任务/文件/未决问题"结构，专为跨压缩接力（§7、§8）。
3. **执行 claim + 开机清扫 + resume 预算** —— 崩溃/优雅重启后自动续跑，含"重复崩溃 10 次即终止"的熔断（§6）。
4. **drain 起步脏数据清算** —— 中断的工具/压缩被正式标记 Failed 再继续，避免模型看到悬空状态（§6）。
5. **subagent 子会话 + 后台 Job + 完成回注** —— 上下文隔离的并行/委派；结果以普通工具输出或 synthetic 注入父会话（§4）。

**插件/配置补的**：plan 模式（plugin/plan.ts）、agent 定义与 steps 上限（config plugin + 配置）、命令体系（init/review/自定义/MCP prompt、/compact 等 TUI slash）、`/btw`。

**明显缺失**：

- **无模型可见的 todo/任务清单**（todowrite 已在 v1→v2 移除，无替代；§3）。
- **无默认 step 预算**：长任务默认无上限（§2）——防烧钱要手动配置。
- **无跨会话长期记忆 / 检索**：唯一"记忆"是会话内 compaction 摘要与 synthetic 通知；没有 memory/recall/embedding 机制（§7）。
- **无内置任务级验证回路**：验证靠模型自律 + snapshot/diff/undo/redo 人工审计；没有 eval/检查清单/闸门（相关能力只有 snapshot+differ/revert，§9/§6 未涉及自动化验证）。
- **queue 语义是整轮排队**：中途 queue 的输入必须等当前回合彻底结束，没有"部分排空"；大批量排队输入会形成长尾回合（§5）。
- `summary` hidden agent 定义悬空（可能为遗留，见未确认）。

---

## 未确认

1. **`summary` agent 的消费方**：`packages/core/src/plugin/agent.ts:147-153` 定义了 hidden summary agent（PROMPT_SUMMARY），但本仓（core/server/sdk/tui/client）搜不到选择该 agent 的代码路径；疑为 v1 遗留或供仓外客户端使用，未确认。
2. **`todo` 表去向**：仅 `packages/core/src/database/migration/20260127222353_familiar_lady_ursula.ts:76-85` 创建；未找到 DROP 迁移，也未找到读写方；其是否仍存在于当前 DB schema 未逐迁移核对。
3. **MAX_STEPS_PROMPT 以 assistant 角色追加在消息尾**（llm.ts:238）并依赖 `toolChoice:"none"` 的行为，在各 provider 协议下的实际效果未跨 provider 验证（代码事实如此，未跑实测）。
4. **native compaction 的 provider 端语义**（OpenAI responses `/compact`、checkpoint 加密 item 的保真度）未核实；本报告只按本地代码事实陈述"可重放性受 provenance 限制、本地不可度量"。
5. **`resume_attempts` 与"同一 turn"的对应**按注释（restart.ts:24-31）理解；只在 `release`（store.ts:217-224）时清零，跨 turn 交叉场景未用测试验证。
6. **桌面/其他客户端**（desktop、console、slack 等包）是否有独立的长任务机制未展开；本报告覆盖 core/server/tui/sdk 启动路径。

---

## 机制总表

| 机制 | 解决长任务哪个问题 | 实现位置（file:line） | 核心 or 插件 |
|---|---|---|---|
| inbox steer/queue 两档晋升 + promote 次序 | 纠偏（中途改道/插队） | `packages/core/src/session/inbox.ts:43-48,240-260,407-424,496-549`；`runner/llm.ts:61-66,80-109,161-181` | 核心 |
| interrupt + resume(steer scope) + doorbell | 纠偏、续航（停止后按 steer 续跑） | `session/execution.ts:153-170`；`run-coordinator.ts:74-85,114-120,136-145` | 核心 |
| compaction summary（滚动更新+15k keep+交接模板） | 遗忘（跨回合记忆/接手） | `session/compaction.ts:101-172,257-295,687-723`；`history.ts:28-60`；`to-llm-message.ts:303-324` | 核心 |
| native provider 压缩 + provenance 重放限制 | 遗忘（provider 侧窗口，绑定 endpoint） | `session/compaction.ts:309-357`；`provider-context.ts:18-58`；`history.ts:16-26` | 核心 |
| 执行 claim + SessionRestart 自动清扫（10 次熔断） | 崩溃（自动续跑、防循环） | `session/execution.ts:76-147`；`execution/restart.ts:15-33,77-99,192-231`；`store.ts:194-251` | 核心 |
| settleStaleCompactions / ToolCalls | 崩溃（脏状态清算） | `runner/llm.ts:68-69,299-352` | 核心 |
| subagent 子会话 + 后台 Job + 完成回注 | 漂移（隔离/并行/委派） | `tool/plugin/subagent.ts:117-133,185-265`；`subagent-job.ts:23-53`；`subagent-completion.ts:20-44` | 核心（以工具插件形态） |
| steps 上限 + MAX_STEPS_PROMPT 强制收尾 | 防烧钱（附带一次总结快照） | `runner/max-steps.ts:1-16`；`runner/llm.ts:223-241`；`runner/step.ts:89-90,272` | 配置（无默认） |
| plan 模式（只读 agent + reminder 重注入） | 漂移（先规划后动手） | `plugin/plan.ts:12-97` | 插件 |
| todo/任务清单 | —— | 无（`v1-migration.bun.ts:901` 已移除 todowrite） | 缺失 |
| title / rename（会话找回，不进请求） | 人类检索（非模型记忆） | `session/title.ts:20-148`；`tool/plugin/opencode.ts:89-108` | 核心 |
| snapshot/diff/revert（undo/redo） | 验证（事后审计/回滚） | `runner/step.ts:73`（每步快照）；`core/session.ts:390-400`；TUI `/diff /undo /redo` | 核心 + TUI |
| 命令层（/compact、/init、/review、自定义命令、MCP prompts、/btw） | 纠偏/编排/维护 | `plugin/command.ts:28-81`；`config/plugin/command.ts:72-139,189-241`；`session/session.ts:246-264`；协议 `groups/session.ts:498-514` | 插件/配置 |
| synthetic durable 通知（重启/subagent 完成/流中断） | 续航、遗忘（进入后续请求的显式上下文） | `session-event.ts:279-289`；`restart.ts:92-98`；`subagent-completion.ts:36-43`；`to-llm-message.ts:284-285` | 核心 |
