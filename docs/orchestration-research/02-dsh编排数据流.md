> 来源：并行调研 agent 产出（只读调查），高级工程师复核。
> 抽检通过：`kick:252` / `turn:298` / `step:400` / `buildRequest:614`、`tool-calls.ts:60 executeToolCalls`、`tool-skill/src/index.ts:163` 三分注册点——行号与代码一致。
> 未确认项保留在文末（§12），未做猜测性补全。

# dsh 一次对话回合的端到端数据流（代码级）

> 仓库：`/home/linsir365/projects/deepseek-harness`，HEAD `571902028a75c64c1d767f4ae5fd099485f0ddf0`（0.2.1-alpha.3 同步线）。
> 方法：只读代码；所有行号相对仓库根，行号以当前 HEAD 为准。文档/注释只作定位线索，论断以代码为准。
> 主线口径：**Web 产品路径**（`dsh web`）为主，headless/SDK 作为对照；TUI 未追踪（见"未确认"）。
> 每处不确定均显式标注 **【未确认】**。

---

## 0. 一句话主链

```
UI(packages/client/ui-conversation) 
  → 客户端 session.prompt (packages/api/session-controller/src/client/sessions/session.ts:271)
  → Remote 'session/prompt' (packages/api/session-controller/src/index.ts:461)
  → SessionCommandController.prompt (packages/api/session-controller/src/commands.ts:319)
  → agent.followup (commands.ts:351; agent-loop/src/agent.ts:163→154 send)
  → inbox.splice 落盘 (agent-loop/src/inbox.ts:235) + wakeDriver (agent.ts:214)
  → ReactLoopAgent.kick (agent.ts:252) → while(turn()) (agent.ts:298)
  → 每个 step：preStep(agent/pre-step) → buildRequest(deriveMessages) → llm.stream
  → assistant/message 落日志 → executeToolCalls（tools/pre-execute→guards→execute→post-execute）
  → tool/result 落日志 → 下一步骤或收尾（step/end、turn/end）
  → 事件经 session-controller → follow 流 → 客户端
  → 日志事件经 session-persistence-jsonl 批写入盘
```

---

## 1. 入口：用户输入从哪进（Q1）

产品有两个可确认入口 + 一个对照：

### 1.1 Web（日常对话主路径）

1. 用户发送：`packages/client/ui-conversation/src/client/service.ts:209` `send()`，附件路径为 `sendSession()`（service.ts:229），两者最终都调客户端 Session 对象：
   - service.ts:211 `session.prompt([{ type: 'text', text }], 'queue')`
   - service.ts:286 `session.prompt(content, mode, signal, submission.requestId)`（`mode` = queue/steer）
2. 客户端会话的远端调用：`packages/api/session-controller/src/client/sessions/session.ts:254` `prompt()` → session.ts:271 `this.remote.session.prompt({ requestId, sessionId, mode, content, clientTimeZone }, signal)`；
   - subagent 会话走 `remote.subagents.prompt`（session.ts:291）。
3. 传输：WebSocket 复用流（Typert Remote Mux），宿主侧入口 `packages/api/gateway/src/index.ts:251` `new RemoteStreamMuxServer(...)`，路径常量 `REMOTE_STREAM_MUX_PATH`（gateway/src/index.ts:265）；连接与升级路由挂在 webserver 的 `WebUpgradeRoute`（gateway/src/index.ts:17）。**RPC 方法表与 `@Remote` 装饰器代码生成细节未展开。【未确认】**
4. 宿主方法：`packages/api/session-controller/src/index.ts:461` `@Remote('prompt')` → index.ts:462 `prompt()`：
   - 校验内容非空白（index.ts:464）、时区规范化（index.ts:467-476）；
   - 构造 durable `MessageSource`（index.ts:479-484：`{kind:'user', rpcId, clientTimeZone?, principal?}`）；
   - index.ts:485 → `this.commands.prompt(request, source)`。
5. 命令控制器：`packages/api/session-controller/src/commands.ts:319` `prompt()`：
   - commands.ts:320 `resolveAgent(sessionId)`；冷会话时在 `packages/api/session-controller/src/agent.ts` `createAgent()`（agent.ts:563）/`resume()`（agent.ts:410）内通过 `ctx.agents.create/resume`（agent.ts:569/440，实现见 §1.3）；
   - commands.ts:321 用 `requestId` 幂等去重（`hasPromptRequest`）；
   - commands.ts:341 `createUserMessage({content, source})`；
   - commands.ts:350-351 `agent.steer(message)` 或 `agent.followup(message)`。
6. 进入 Agent：`packages/core/agent-loop/src/agent.ts:163` `followup()` → `send(msg,'next-turn',true)`（agent.ts:154）：
   - agent.ts:157 若处于“已中止的活动中”则强制改为 `next-turn`；
   - agent.ts:159 `inbox.splice(target, Infinity, 0, [message])` → `ReactLoopInbox.mutate`（inbox.ts:198）→ **agent.ts 无关的 durable 事件 `agent/inbox/spliced`**（inbox.ts:227-235）；
   - agent.ts:160 `wakeDriver()` → 若 idle：agent.ts:225-234 建 driver promise、`setPhase(running)`、`loopCtx.agents.withInitiator(this, () => this.kick())`。

### 1.2 CLI headless（一次性任务，对照）

- `packages/bundle/headless/src/index.ts:311` `run()`：读取 task（index.ts:327-332），创建/恢复 agent（index.ts:347-354，`ctx.agents.create` 于 index.ts:348），
- index.ts:375 `agent.followup(createUserMessage({...source:{kind:'user'}}))` → 同一 `send` 链；
- 收尾：index.ts:381-386 `whenIdle` 循环、index.ts:390 `sessions.flush(agent.session)`（持久化屏障）、index.ts:392 `summarize(agent.session, firstSeq)` 输出。

### 1.3 Agent 在哪构造

- 工厂服务 `AgentLoop`：`packages/core/agent-loop/src/index.ts:341`，`static inject = ['agents','sessions','llm','tools','systemPrompt','sessionProjections']`（index.ts:342）；公开 `create`（index.ts:688）/`resume`（index.ts:843）/`createAgent`（index.ts:750）。
- loop 实例化于 `prepare()`：index.ts:604 `machine = new ReactLoopAgent(loopCtx, id, options, session)`；发布时 `agent.ctx.sessions.enter(session)`（index.ts:654）、`loopCtx.agents.enter(agent, parentAgent)`（index.ts:657）、`announce`（index.ts:658-660）。

### 1.4 SDK server（对照）

- `packages/sdk/server/src/server.ts:196` `prompt()` → server.ts:211 `rec.handle.agent.followup(message)`。

**【未确认】** `dsh tui` profile 的表单/按键输入到 `session.prompt` 的具体链路：仓库内未找到名为 tui 的输入包（仅有 profile 名与 bundle 名 `web`/`headless`/`acp-app`/`sdk-app` 等），TUI 用户面疑在未开源的部分，未追踪。

---

## 2. 循环结构：ReAct 循环在哪、一次迭代做什么（Q2）

文件：`packages/core/agent-loop/src/agent.ts`（类 `ReactLoopAgent`，agent.ts:98）。

### 2.1 驱动骨架

- `kick()`：agent.ts:252-265
  - `while (await this.turn()) {}`（agent.ts:254）：turn() 返回 true 表示 inbox 还有待处理工作，继续下一个 turn；
  - finally：置回 idle，并处理 `wakeRequested` 的补跑（agent.ts:259-263）。
- `turn()`：agent.ts:298-398
  1. agent.ts:307 `session.append('turn/start', { turn })`（先开边界再 claim）；
  2. 内层 `while(true)`（agent.ts:315）逐个 step：
     - agent.ts:318 `preStep(target, {turn, step})`（见 2.2）；`reject` → `turnEnds={kind:'blocked'}` 结束（agent.ts:319-322）；
     - agent.ts:323 若 `turnEnds` 已定且本步 messages 为空 → break；agent.ts:326-329 首个 step 空批 → `{completed}` 结束；
     - agent.ts:331 `session.append('step/start')`；agent.ts:334-336 挂一个 `session/event` 监听做 `ToolCallRecovery`（崩溃/取消时的补偿结果来源）；
     - agent.ts:340 `await this.step(decision)`；max-tokens 粘性（agent.ts:341-343）；
     - 出错时：把 recovery 的 pending tool results 追补为 `tool/result`（agent.ts:344-355），再抛；
     - finally：agent.ts:358 `session.append('step/end')`；
     - agent.ts:361-364：`turnEnds` 已定且 nextStep 空 → `dispatch.serial('agent/turn-stopping')`（模型不再欠响应时关闭 turn 前的最后一个可插拔点，listener 可再 steer 续跑）；
     - agent.ts:365 `if (turnEnds && nextStep.length===0) break`，否则 agent.ts:366 `target='next-step'` 进入下一 step。
  3. 收尾 reason：取消 → `{aborted, reason}`（agent.ts:370-374）；其他错误 → `{error, errorChain/LlmError.failure}`（agent.ts:377-382）；finally 里 **agent.ts:387 `session.append('turn/end', {turn, reason})`**；
  4. agent.ts:392-397：若 `inbox.hasPending`，换新 AbortController、重置 step=0，返回 true（同一 driver 跑下一个 turn）。

### 2.2 preStep（每个 step 的准入）

- agent.ts:267-288：
  1. agent.ts:271 `inbox.claim(target, turn)`：拿走全部 `next-step` +（target=next-turn 时）**恰好一条** `next-turn`（inbox.ts:109-114），对每条 emit `agent/inbox/claimed`（inbox.ts:112）；
  2. agent.ts:272 `systemPrompt.assemble(assembleContextFor(this, signal))`；
  3. agent.ts:274-284 `agent/pre-step` waterfall（声明：`packages/core/agent/src/runtime-types.ts:320`）；默认链把渲染后的 runtime-context 快照追加进 messages（agent.ts:276-283），返回 `{kind:'enter', messages, assembly}`；listener 可 rewrite/reject（compaction 与 skill catalog 都挂在这里，见 §9/§10）。

### 2.3 step（一次模型调用 + 它触发的工具执行）

- agent.ts:400-561：
  1. agent.ts:410 `prepareRequest`（`agent/request` waterfall + `llm.prepareCall`，见 §3）；
  2. agent.ts:411-413 刷新 runtime-context 快照；agent.ts:416-424 提交/替换 `system/message`（`SystemPromptProjection.project`，runtime-context.ts:88-103）；
  3. agent.ts:426-440 首个 attempt 才把被 claim 的 user 消息落日志（`user/message`）；runtime-context 源的消息用**刷新后的快照**落一次（agent.ts:428-432）；
  4. agent.ts:442 `buildRequest(...)` 组装冻结请求（见 §3）并记 `request/header` 等；
  5. agent.ts:443-450 `new AssistantStreamAttempt(...)`，帧出口 `dispatch.emit('agent/assistant-stream', {frame})`；
  6. agent.ts:453 `preparedCall?.stream(request) ?? this.loopCtx.llm.stream(request)`；agent.ts:457-460 `for await` 每个 chunk：`live.push(chunk)`（累加 durable 流 + BlockAssembler + 发帧）；
  7. 失败/中止：`assistant/message`（有可见内容，`interrupted:true`）或 `assistant/attempt`（无内容/流错误），agent.ts:462-503；
  8. finish=error/aborted → 记 `assistant/attempt`，走 `agent/request-error` waterfall（声明 runtime-types.ts:353），返回 `{kind:'retry'}` 则 **重试当前 step 的请求**（agent.ts:511-526）；
  9. 正常：agent.ts:529-546 `createAssistantMessage(...)` → **agent.ts:537-546 `session.append('assistant/message', {message, usage?, stream}, {surfaceOp:'append'})`**（`stream` 即紧凑 provider 流；由 assistant-stream.ts:112-114 提供）；
  10. agent.ts:547 `finish.kind==='max-tokens'` → 返回 `{max-tokens}`；
  11. agent.ts:549-550 无 tool-call → 返回 `{completed}`；
  12. agent.ts:551-554 有 tool-call → `executeToolCalls(...)`（见 §4），`{concluded}` 为 true → `{completed}`，否则返回 `null`（继续下一 step）。

### 2.4 终止/续跑条件汇总

- 单个 step 返回：`completed`（无工具调用或 `concludesTurn`）/`max-tokens`（粘性）/`null`（工具调用未完待续）。
- turn 结束：`turnEnds` 已定且 `nextStep` 清空 → `agent/turn-stopping` → break；或 `pre-step reject`→blocked；或首个空批→completed；或 error/aborted。
- driver 续跑：turn 后 `inbox.hasPending` → 同一个 `kick` 再跑一个 turn；否则回到 idle。
- 中途打断：`cancel`（agent.ts:175-181）清 inbox（除非 keepInbox）并 abort 当前 AbortController；`steer` 在 step 边界并入（agent.ts:167-169 → send 'next-step'）。

---

## 3. 模型请求装配（Q3）

### 3.1 系统提示词

- 注册表 `SystemPrompt`：`packages/core/system-prompt/src/index.ts:429`（service key `systemPrompt`，index.ts:446）。
- 装配：`SystemPrompt.assemble()` 于 index.ts:614：
  - 变量 provider 求值（index.ts:620-629）；
  - section/context 按 `NamedEntries` + 排序合并（index.ts:631-652；排序规则 index.ts:248-250）；
  - 工具 schema 收集（index.ts:634-651）：provider 返回 `{schemas, knownNames}`，schema 深拷贝后按 `toolOrder` 或字典序排序（index.ts:674，`orderTools` index.ts:226）；
  - 递归 waterfall `system-prompt/assemble`（index.ts:677-680，声明 index.ts:31）。
- 内置 section 订单表：index.ts:131-165（例如 TOOL_BASH=1000、TOOL_READ=1100……）；identity section 由构造器注册（index.ts:450-454）。工具包自己注册每个工具的说明 section，例如 bash `packages/shell/tool-bash/src/index.ts:251`、read/edit/write `packages/fs/tool-fs/src/read.ts:69`、edit.ts:77、write.ts:63。
- 渲染：`renderPrompt` 于 index.ts:290；runtime-context 快照另外渲染（`renderContextSections` index.ts:330 + `joinContextSections` index.ts:314）。
- 提交时机：每个 step 在 `step()` 内把渲染结果经 `SystemPromptProjection.project`（runtime-context.ts:65-110）变成 `system/message` append/replace（agent.ts:416-424）。

### 3.2 历史消息怎么选

- **没有独立的“历史数组”**：请求消息 = 从会话日志派生的 surface 投影。
- `buildRequest()`：agent.ts:616-704：
  - agent.ts:626-630 `canonicalHeader({config, adapterDefaults?, tools?})`；
  - agent.ts:635-651 记 `request/header`（仅变化时；reason=initial/resume/change/series）；
  - agent.ts:652-666 工具增减时补 `developer/message`（source `tool-registry`，内容为 tool-addition/removal 块）；
  - agent.ts:671-683 路由元数据变化记 `request/context`；
  - agent.ts:688 `const boundaryMessages = session.deriveMessages()`，逐个 deepFreeze 缓存（agent.ts:689-694）；
  - agent.ts:695-703 冻结请求对象 `{...config, messages, toolHistory: session.toolHistory(), tools?, sessionId, signal}`，`markAgentLoopRequest` 打标。
- `Session.deriveMessages()`：`packages/core/session/src/index.ts:984-1005` —— 按 surface 节点序列投影（缓存增量 O(new nodes)，替换代重建）；空内容 assistant 消息不进 transcript（index.ts:998-1001）。
- `toolHistory()`：session/src/index.ts:953-957（工具定义变更史，供 provider 的 tool-update 投影）。
- **压缩对请求的作用**：surface 上的 `SurfaceOp.replace` 会把被遮蔽节点从派生中删除（session/surface.ts:96-104、247-251 的 `contentGeneration`）；即“裁剪”是通过日志层替换实现，而不是在消息数组上删元素。回合内的裁剪触点见 §9。tool-result pruner 想剪 tool 结果时也只在压缩路径被调用（compaction-basic/src/index.ts:292-297、322-327）。**【未确认】** 除压缩外是否有其它逐请求的消息条数/文本长度裁剪：在 `buildRequest` 未见，**判定为无**（基于代码未见，非文档保证）。

### 3.3 工具 schema 怎么生成

- `ToolRuntime` 在构造时注册 provider：`packages/core/tools/src/index.ts:857` `ctx.systemPrompt.tools(context => this.wireSchemas(context.scope))`。
- `wireSchemas()`：core/tools/src/index.ts:1019-1037：按 scope 可见性取 `view.visible`，逐工具 `schemaOf`（index.ts:1290-1302，剥离/快照 parameters）；native 模式返回全部可见工具，ptc 模式只返回 `run_code`（index.ts:1022-1036）。
- 工具注册：`register(definition)` 于 core/tools/src/index.ts:1071；写工具用 `defineTool`（`packages/core/tools/src/schema.ts:554`；示例 `packages/fs/tool-fs/src/read.ts:77`）。

### 3.4 路由与适配器绑定

- `prepareRequest()`：agent.ts:564-613：
  - 读持久头（agent.ts:573-580），种子 config 冻结（agent.ts:583-592）；
  - agent.ts:593-596 `agent/request` waterfall（可改路由/参数；声明 runtime-types.ts:337）；
  - agent.ts:604 `this.loopCtx.llm.prepareCall(proposedConfig, signal)`（NO_ADAPTER 时降级继续，agent.ts:606-610）。
- LLM 层：`LlmRuntime.prepareCall` 于 `packages/llm/llm/src/index.ts:956-1013`：绑定 adapter registration（index.ts:963-964），解析默认值/校验（index.ts:971-983），返回单次派发的 `stream`（index.ts:995-1012，防二次派发/防配置漂移）。
- `llm.stream`：llm/src/index.ts:1166-1168 → `streamWithRegistration`：**index.ts:1174-1179 递归 `llm/stream` waterfall**（retry 等中间件的截点，例如 `packages/llm/llm-retry`）。
- 终端适配器（DeepSeek）：`packages/llm/llm-deepseek/src/adapter.ts:65-89` `generate()`（idle watchdog）；adapter.ts:91-177 `request()`：图片/文件准备（adapter.ts:95-121）、`serialize`（adapter.ts:119）、**adapter.ts:138 `fetch(\`${baseURL}/messages\`)` POST SSE**、adapter.ts:165 `parseSse` → `translate` 产出 `StreamChunk`。

---

## 4. 工具调用解析与分发（Q4）

### 4.1 解析

- 模型侧 tool-call 是 assistant 消息内的 `tool-call` 内容块：agent.ts:549 `message.content.filter(block => block.type==='tool-call')`。
- 调度器：`executeToolCalls` 于 `packages/core/agent-loop/src/tool-calls.ts:60-102`：
  - tool-calls.ts:68 取当前 initiator agent（`ctx.agents.requireInitiator()`）；
  - tool-calls.ts:72-81 组 `PlannedCall{block, exec:{callId, name, arguments, agent, signal}}`；
  - tool-calls.ts:105-111 `parseArguments`：JSON.parse，失败保原文，空串记 `{}`。

### 4.2 注册表

- `ctx.tools` = `ToolRuntime`（`packages/core/tools/src/index.ts:808`），`register()` index.ts:1071；作用域分层 `ScopedLayers`（index.ts:834-837）实现“scoped 遮蔽 global”。
- 执行分类 `executionMode()`：index.ts:1311-1320 —— 只有工具的 `isConcurrencySafe(args)` 返回**严格 true** 才是 `parallel`，否则 `exclusive`（fail-closed）。

### 4.3 调度与并发规则

- 外层（步骤内）：tool-calls.ts:83-101 `while(next<planned.length)`：
  - tool-calls.ts:89 每条开始前重读 `executionMode`（注册表变化能形成 barrier）；
  - mode=parallel 时整段成为候选组，exclusive 单条成组（tool-calls.ts:90）；
- `runGroup()`：tool-calls.ts:122-247：
  - 并发上限 `ctx.agentLoop.config.maxParallelToolCalls`（tool-calls.ts:132），默认 `DEFAULT_MAX_PARALLEL_TOOL_CALLS = 10`（`packages/core/agent-loop/src/constants.ts:6`；schema 在 agent-loop/index.ts:306/346）；
  - `fillPool()`（tool-calls.ts:199-214）：排空“未终止且 in-flight < 上限”的启动；池内后续条目若被重分类为 exclusive 则停在此 barrier；
  - `startCall()`（tool-calls.ts:165-197）：**先** tool-calls.ts:168 `session.append('tool/call', ...)`（拿 seq），再走调度器 `prepare`（tool-calls.ts:170）→ 三种终态：`dispatch`（并发跑）/`post-result`（需要 post 策略）/`final-result`（已定）；
  - **结果按模型顺序提交**：`commitReady()`（tool-calls.ts:147-161）只推进“连续满足的”槽位；结果落 `tool/result` 并携带 `sourceEventSeqs:[callSeq]`（tool-calls.ts:269-290）；`additionalContexts` 通过 `acceptContext` 进入 next-step inbox（tool-calls.ts:157；调用点 agent.ts:553）；
  - 中止：停 replenish、drain 已启动、对未启动者补合成错误结果（tool-calls.ts:238-243、250-260）。

### 4.4 流水线（每一步都可插拔）

从 `ToolRuntime` 的调度器接口（index.ts:817-822）进入：

1. `prepare` → `prepareExecution`：index.ts:1501-1547
   - `createExecution`：index.ts:1399-1489（参数快照/冻结、token、取消状态、ptc 折叠检查）；
   - **index.ts:1513-1516 `tools/pre-execute` waterfall**，默认 `{kind:'allow'}`（声明 index.ts:157 附近；`PreToolDecision` index.ts:608-617）；
   - `ask` → index.ts:1517-1519 `serviceAsk`：**审批环**（见 4.5）；
   - allow 后跑**单调 guards** `guardReason()`（index.ts:1527、768-774；注册 `guard()` index.ts:1144）；
   - 放行 → `{kind:'dispatch'}`。
2. `dispatch` → `dispatchScheduledExecution`：index.ts:1609-1639
   - **index.ts:1613-1616 `tools/execute` waterfall**（timeout/retry/metrics 等围绕派发的策略挂这里）；
   - 本体 `dispatchToolBody`：index.ts:1572-1600 —— 融合 caller signal（index.ts:1577），index.ts:1589 `tool.execute(exec.arguments, exec)`；abort 不弃 body，等其达静默（index.ts:1591-1593）。
3. `finalize` → `finalizeScheduledExecution`：index.ts:1649-1667
   - index.ts:1651-1656 工具 `projectContent`；
   - index.ts:1657→1789-1800 **`tools/post-execute` waterfall**（accept/block/replace/add context；`PostToolDecision` index.ts:618+）。
4. `finish` → `finishScheduledExecution`：index.ts:1677-1692
   - 规范化快照（失败→isError）、`finalizeContent`（index.ts:1695-1700）、**index.ts:1703-1722 `tools/result` 同步通知**（冻结 exec+result 后逐监听器 containment）。
5. 回到 loop：结果写入 `tool/result` 会话事件（tool-calls.ts:282-289），成为下一步模型可见的唯一结果。

### 4.5 审批/权限在哪一环

两条通道，都汇到 `ctx.approval`（`packages/interaction/user-approval`）：

- **策略环（pre-execute ask）**：`tools/pre-execute` 返回 `{kind:'ask'}` → `serviceAsk`（core/tools/src/index.ts:1735-1769）→ index.ts:1752 `approval.request({agent, toolName, callId, reason, displayReason?, signal})`；无服务/无 agent/非 grant 全部 fail-closed（deny）。
- **沙箱升级环（工具体内）**：bash/fs 等工具在执行体内调 `approveEscalation`（`packages/sandbox/sandbox/src/escalation.ts:171-208`），同样落到 `approval.approver.request`（escalation.ts:188-198）。
- 审批服务实现：`UserApprovalService`（user-approval/src/index.ts:156 相关），`request()` 于 index.ts:215-234：先 `session.append('approval/asked')`（index.ts:225）、决定后 `session.append('approval/decided')`（index.ts:232）；决定经 **index.ts:281-284 `approval/request` waterfall**（声明 index.ts:33-40 附近），缺省答案 `'unavailable'`（fail-closed）。
- 线协议与 UI：`approval/request` 被列为远程 waterfall 事件（`packages/api/remotes/src/remote-events.ts:27`）；Web 侧监听在 `packages/client/ui-approval/src/client/index.ts:104` `ctx.remote.$on('approval/request', ...)`。
- 默认 `ask` 策略来自配置（user-approval/src/index.ts:152/244）。**【未确认】** 默认 web profile 里除了 sandbox 升级与 hooks 之外，`tools/pre-execute` 返回 `ask` 的完整监听者清单未逐条枚举（开源包内的 ask 生产者为 `packages/experimental/auto-review/src/index.ts:684`、`packages/experimental/hooks-claude-code/src/index.ts:248` 等实验插件，是否装入默认 profile 未验证）。

---

## 5. 结果回写与下一步衔接（Q5）

- 工具结果落盘结构：`appendToolResult`（tool-calls.ts:269-290）构造 `createToolResultMessage({callId, content, isError})`，append 为会话事件 `tool/result {turn, step, message, error?, meta?}`，surface 语义 `{surfaceOp:'append', sourceEventSeqs:[callSeq]}`。
  - `error` 是内部失败身份（`result.error.info`），`meta` 是工具私有展示载荷（如 diff，供 UI 重放卡片）。
- 因果链接：`tool/call`（先落）与 `tool/result`（后落）用 callId + seq 配对（tool-calls.ts:263-266、288-289）。
- 下一步怎么吃到结果：`executeToolCalls` 返回 `concluded=false` → step 返回 `null` → turn 循环把 target 切到 `next-step`（agent.ts:366）→ 下一次 `preStep` claim inbox（其中含 `additionalContexts` 的 inject 消息）→ `buildRequest` 重新 `deriveMessages()`：新 `tool/result`/`assistant/message`/`user/message` 节点自然进入消息数组（session/src/index.ts:984）。
- 因此 loop 的状态就是日志：无独立“messages 数组”状态需要回写（与 ai-coding 的 `SessionData.messages` 范式不同）。

---

## 6. 事件流（Q6）

事件分三层：Cordis 事件（进程内）、Agent 专属事件（fused dispatch，`packages/core/agent/src/dispatch.ts:107-148`）、线协议事件（Remote/stream）。

### 6.1 回合内进程内事件（发/收）

| 事件 | 发送点 | 消费者 |
|---|---|---|
| `agent/inbox/inserted` / `claimed` / `discarded` | inbox.ts:237-241（inserted/discarded）、inbox.ts:112（claimed） | UI/编排插件（声明 runtime-types.ts:288/299/307） |
| `agent/pre-step`（waterfall） | agent.ts:274 | compaction（basic/index.ts:158）、skill（tool-skill/index.ts:179/215）、plan/usercode 等 |
| `agent/request`（waterfall） | agent.ts:593 | 路由/预设覆盖插件 |
| `agent/request-error`（waterfall） | agent.ts:511 | llm-retry、compaction overflow（basic/index.ts:190） |
| `agent/assistant-stream`（emit） | agent.ts:449（每帧） | session-controller history（history.ts:54、167） |
| `agent/turn-stopping`（serial，await） | agent.ts:362 | goal/编排类插件（可再 steer 续跑） |
| `agent/status`（emit） | agent.ts:145-152（setPhase） | session-controller→线协议 `api-session/status`（index.ts:204-206；remote-events.ts:32） |
| `agent/error`（emit） | agent.ts:248 | headless（bundle/headless/src/index.ts:366）、session-controller（index.ts:207-209） |
| `session/event`（emit，每 append） | session/src/index.ts:876-897（commit 内 `invokeContainedSessionObservers`） | 压缩压力监听（compaction-basic/index.ts:184）、user-approval、projection 缓存（session-projection/src/index.ts:220）、jsonl 持久化（storage.ts:549）、session-controller（index.ts:210-222）、history.follow（history.ts:146） |
| `tools/pre-execute` / `tools/execute` / `tools/post-execute`（waterfall） | core/tools index.ts:1513/1613/1791 | 权限、hooks、timeout/retry 等 |
| `tools/result`（emit，冻结快照） | core/tools index.ts:1711-1721 | UI 卡片（presentResult 通道）、审计 |
| `approval/request`（waterfall） | user-approval index.ts:281-284 | 人机回答者（Web: ui-approval client index.ts:104） |
| `llm/stream`（waterfall） | llm/src/index.ts:1174-1179 | retry、遥测等中间件 |
| `session/flush` / `session/disposed` | 发起方为 `sessions.flush` 等 | jsonl 后端（storage.ts:554/562） |

### 6.2 UI 怎么看到流式输出

1. token 级：`agent/assistant-stream` 帧（`AssistantStreamAttempt.start/push/settle/abandon`，agent-loop/src/assistant-stream.ts:49-109；帧类型 `AssistantStreamFrame`）→ session-controller `SessionAssistantStreamAccumulator.accept`（`packages/api/session-controller/src/assistant-stream.ts:38-78`）维护重连基线；
2. 线协议：`@Remote({mode:'stream'}) follow`（session-controller/src/index.ts:536-539）→ `SessionHistoryController.follow`（history.ts:120-233）：
   - 首发 snapshot（含 projections 与 assistantStream 基线，history.ts:193-203）；
   - 之后逐条推 durable `session/event`（history.ts:146-150、226-232）与 opt-in 的 assistant-stream 帧（history.ts:165-175、220-224）；
3. 客户端：`follow` 请求带 `assistantStream: true`（client/transport.ts:179-183）；帧按 revision 连续性校验（transport.ts:206-215）；客户端会话把 durable 事件与瞬时帧合并（`acceptEventChange`，client/sessions/session.ts:646-665），UI 组件从中渲染流式文本与工具卡片。

### 6.3 持久化回执与状态事件

- `api-session/added|removed|status|error|activity`（session-controller/src/index.ts:191-222）→ 远程 emit 表（packages/api/remotes/src/remote-events.ts:28-32）→ Web 侧列表/状态。

---

## 7. 持久化触点（Q7）

后端：`@deepseek-ai/dsh-session-persistence-jsonl`；store root 由 base bundle 配置 `dshHomePath('sessions')`（`packages/bundle/base/cordis.patch.yml:138-141`）。**【未确认】** `dshHomePath` 的具体 home（如 `~/.dsh`）由 `@deepseek-ai/dsh-home-paths` 解析，未读该包。

一个正常回合的时间线（全部经同一 handle 写入）：

1. **会话创建时**：`persistence.create(header, ...)`（agent-loop/index.ts:718）→（懒物化）`materialize()` 写 header 行 + 首批事件（session-persistence-jsonl/src/index.ts:1263-1279；POSIX 原子发布 = link()+unlink()+目录 fsync，index.ts:1282-1319）。发布时把未存事件后缀补存（agent-loop/index.ts:734-742）。
2. **用户消息入队**：`agent/inbox/spliced` append（inbox.ts:235）→ `session/event` → storage.ts:549-552 `writers.get(session.id).enqueueLive(event)` → `buffered.push` + 200ms 批窗（storage.ts:274-281；常量 `LIVE_WRITE_BATCH_MAX_DELAY_MS=200` storage.ts:36）→ `drainLive`（storage.ts:288-316）→ `persistContiguous`（storage.ts:319+，含连续性与 torn-tail 修复）→ 生成器写文件（generation.ts:740 `handle.writeFile`）。
3. **回合过程中**：`turn/start`、`step/start`、`system/message`、`user/message`、`request/header`、`request/context`、`assistant/message`（带 `stream`+`usage`）、`assistant/attempt`（失败尝试）、`tool/call`、`tool/result`、`step/end`、`turn/end` 等每次 append 都走第 2 步同一条路由（同为 `session/event`）。
4. **显式屏障**：`sessions.flush(session)`（headless 收尾 bundle/headless/src/index.ts:390）→ `session/flush` → `drainLive()+flush()`（storage.ts:554-561）。
5. **销毁/关闭**：`session/disposed` → 异步 close 最终 drain（storage.ts:562-568）；handle.close() 保证 drain（storage.ts:223-260）。
6. **恢复（崩溃尾巴）**：resume 时读物理日志（agent-loop/index.ts:888），`interruptedTurnClosers(persisted)`（`packages/core/session/src/repair.ts:209`）合成缺失的 tool 错误结果/step/end/turn/end 并追加（agent-loop/index.ts:891-892），内存 session 以 seed 重建（index.ts:893-899）。

其它落盘触点（非 session 文件）：attachment 图片字节（compaction-image-offload 等旁路）、telemetry（`packages/session/session-telemetry`，独立 sink）。**【未确认】** 具体 telemetry 写入时机不展开。

---

## 8. skill 层（Q8）

dsh **有** skill 概念（用户/模型可调用的指令包），不是 preset 的别名：

1. 注册表/服务：`ctx.skills`（`packages/skill/skill/src/index.ts:439` `register()`）；发现源：`skill-filesystem` 作为 provider（`packages/skill/skill-filesystem/src/index.ts:135` `ctx.skills.registerProvider(...)`，按工作目录扫描）。
2. 与工具层交互：注册模型工具 `skill`（`packages/skill/tool-skill/src/index.ts:163` `ctx.tools.register(skillTool)`；定义 index.ts:82-162）——模型按目录名调用，工具返回 `{name, provider, resourceBase?, content}`，经正常工具流水线成为 `tool/result`。
3. 注入请求（两条 pre-step 通道，都是 `agent/pre-step` waterfall 监听）：
   - **用户 `/name` 手势**：tool-skill/index.ts:179-206，从被 claim 的 user 消息中识别 `/skill-name`（正则 index.ts:411），把渲染后的技能正文作为 `user/message`（source `skill-invocation`，form `instructions`）**追加到 messages 尾部**（index.ts:198-205）——下一步即模型可见；
   - **技能目录**：tool-skill/index.ts:215-253，把可用技能的 `<available_skills>` 目录渲染为 `user/message`（source `skill-catalog`）插入/替换，内容摘要由 durable digest 决定是否重发（index.ts:230-252、330-337）。
4. 与系统提示词的关系：技能目录不是 system section，而是 user-role 的 durable 上下文（可审计、可连同日志回放）；`skill` 工具说明会经 `systemPrompt.section` 机制进入系统提示（工具包通常自注册 section，示例 tool-bash index.ts:251）。
5. 等价物说明：`preset`（packages/preset）、`goal`（packages/goal）、`plan`（packages/plan）是另外的子系统，走各自的服务/事件（例如 goal 驱动 `agent/turn-stopping` 与 steer；plan 也会在 pre-step 注入/steer，见 `packages/plan/plan-mode/src/index.ts:259`）。**【未确认】** 各 preset/goal/plan 的完整注入细节不在本报告范围，未逐行追踪。

---

## 9. 压缩（compaction）（Q9）

- 引擎抽象：`ctx.compaction`（`packages/compaction/compaction/src/index.ts:119` `CompactionEngine`；`compactIfNeeded` index.ts:136、`compactNow` index.ts:162）。
- 实现：`BasicCompactionEngine`（`packages/compaction/compaction-basic/src/index.ts:113`）；`auto:true` 时注册两个自动监听（index.ts:140-141、148-235）：
  1. **回合内、step 与 step 之间**：`agent/pre-step` listener（index.ts:158-176）→ `compactIfNeeded(agent, 'pressure', signal)`（index.ts:164）；压力判断 = tokenMeter 测量 vs 模型 contextWindow 阈值（index.ts:304-319）；可选先跑 tool-result pruner（index.ts:292-297、322-327）再复测，合格后选区间压缩（index.ts:329+）。
  2. **模型报 context overflow 时**：`agent/request-error` listener（index.ts:190-234）→ 压缩成功后返回 `{kind:'retry'}`（index.ts:233），loop 在 agent.ts:511-526 识别并**重试当前 step 的同一次请求**。
- 数据变换：选中 surface 闭区间后，追加两条事件再替换：`compaction/summary`（region.ts:491-505）与 `user/message`(checkpoint) 但带 `surfaceOp:{op:'replace', startSeq, endSeq}` + `sourceEventSeqs:[start, summary, shadowed...]`（region.ts:506-509）。此后 `deriveMessages()` 自动不再产出被遮蔽节点（session/src/index.ts:984-1005；surface 代次 surface.ts:247-251）。
- 手动路径：`/compact` 命令（`packages/compaction/command-compact/src/index.ts:67` `ctx.compaction.compactNow(...)`；经 `agent.runMaintenance` 的 idle 互斥，agent.ts:183-204）。

---

## 10. Mermaid：全链时序（参与者=真实模块名）

```mermaid
sequenceDiagram
    autonumber
    participant UI as "client ui-conversation (service.ts:211)"
    participant SC as "api session-controller (commands.ts:319)"
    participant Loop as "core agent-loop ReactLoopAgent (agent.ts)"
    participant SP as "core system-prompt (index.ts:614)"
    participant TR as "core tools ToolRuntime (index.ts:808)"
    participant LLM as "llm LlmRuntime + llm-deepseek adapter"
    participant Log as "core session Session log (append:798)"
    participant P as "session-persistence-jsonl (storage.ts)"

    UI->>SC: "RPC session/prompt {content, mode, requestId}"
    SC->>Loop: "agent.followup (commands.ts:351)"
    Loop->>Log: "inbox.splice -> append agent/inbox/spliced (inbox.ts:235)"
    Log--)P: "session/event -> enqueueLive (storage.ts:549/274) [200ms batch]"
    Loop->>Loop: "wakeDriver (agent.ts:214) -> kick (252) -> turn() (298)"
    Loop->>Log: "append turn/start (agent.ts:307)"
    Loop->>SP: "preStep: assemble (agent.ts:272; index.ts:614)"
    Loop->>Loop: "agent/pre-step waterfall (agent.ts:274)"
    Note over Loop,Log: "compaction pressure may replace history here (compaction-basic/index.ts:158; region.ts:506)"
    Loop->>Log: "claim -> step/start + system/message + user/message (agent.ts:331-440)"
    Loop->>LLM: "prepareRequest (agent/request + prepareCall, agent.ts:593/604)"
    Loop->>Log: "request/header + developer/message + request/context (agent.ts:637-683)"
    Loop->>LLM: "buildRequest from deriveMessages (agent.ts:688/453) -> stream"
    LLM-->>Loop: "StreamChunks (adapter.ts:138 POST /messages -> 165 translate)"
    Loop--)SC: "agent/assistant-stream frames (agent.ts:449)"
    SC--)UI: "follow stream (history.ts:167) -> client apply (session.ts:663)"
    Loop->>Log: "append assistant/message {stream, usage} (agent.ts:537)"
    alt "assistant requested tool calls"
        Loop->>TR: "executeToolCalls (tool-calls.ts:60) -> tool/call append (tool-calls.ts:168)"
        TR->>TR: "tools/pre-execute (index.ts:1513) [ask -> ctx.approval -> approval/request -> UI]"
        TR->>TR: "guards (index.ts:1527) -> tools/execute (index.ts:1613) -> tool.execute (index.ts:1589)"
        TR->>TR: "tools/post-execute (index.ts:1791) -> finalize -> tools/result (index.ts:1703)"
        Loop->>Log: "append tool/result in model order (tool-calls.ts:156/269)"
        Loop->>Loop: "next step: preStep target=next-step (agent.ts:366)"
    else "no tool calls (or concludesTurn)"
        Loop->>Log: "step/end (agent.ts:358) -> agent/turn-stopping (agent.ts:362) -> turn/end (agent.ts:387)"
    end
    Loop--)SC: "agent/status idle (agent.ts:150 -> index.ts:204)"
    Log--)P: "every session/event batched to JSONL (storage.ts:288/319; generation.ts:740)"
```

---

## 11. 总表：步骤 | 调用链 | 数据结构 | 备注

| # | 步骤 | 调用者(file:line) → 被调用者(file:line) | in → out | 备注 |
|---|---|---|---|---|
| 1 | UI 发送 | ui-conversation/src/client/service.ts:211 → client session.prompt session.ts:254 | `text/mode` → RPC | 附件走 service.ts:229/286 |
| 2 | RPC 到达宿主 | session-controller/src/index.ts:461 `@Remote('prompt')` → commands.ts:319 | `SessionPromptRequest` → `SessionPromptValue{accepted}` | requestId 幂等（commands.ts:321） |
| 3 | 入队 | commands.ts:341 createUserMessage → agent.followup agent.ts:163 → send:154 → inbox.splice inbox.ts:167 | `UserMessage` → `agent/inbox/spliced` 事件 | 唤醒分类见 agent.ts:157 |
| 4 | 唤醒驱动 | agent.ts:160 → wakeDriver:214 → kick:252 | 无 → `withInitiator` 绑定的 driver promise | |
| 5 | 开 turn | agent.ts:307 `session.append('turn/start')` | `{turn}` → `SessionEvent` | turn 内多个 step |
| 6 | step 准入 | agent.ts:318 preStep:267 → inbox.claim inbox.ts:109 | — → `UserMessage[]` | claim = 全部 next-step + 一条 next-turn |
| 7 | 系统提示装配 | agent.ts:272 → system-prompt/src/index.ts:614 assemble | providers → `PromptAssembly{sections,contexts,tools,variables}` | tools 由 core/tools index.ts:1019 wireSchemas 提供 |
| 8 | pre-step 拦截 | agent.ts:274 → `agent/pre-step` waterfall（声明 runtime-types.ts:320） | `{messages,turn,step,signal}` → `PreStepDecision` | compaction/skill 在此注入 |
| 9 | 开 step / 落用户消息 | agent.ts:331/416-440 → `session.append('step/start' / 'user/message' / 'system/message')` | `SystemMessage`/`UserMessage` | context 源单独处理 |
| 10 | 请求配置 | agent.ts:410 prepareRequest:564 → `agent/request` waterfall (593) → `llm.prepareCall` (604; llm/src/index.ts:956) | `LlmCallConfig` → `PreparedLlmCall` | adapter 绑定、默认值解析 |
| 11 | 记请求信封 | agent.ts:637 `request/header`；652 developer/message；682 request/context | `EpochHeader` 等 | 变化才记；reason=initial/resume/change/series |
| 12 | 组装消息 | agent.ts:688 `deriveMessages()`（session/src/index.ts:984）+ toolHistory:953 | 日志 surface → `readonly Message[]`（冻结） | 压缩产生的 replace 已从派生中消失 |
| 13 | 调模型 | agent.ts:453 → llm.stream llm/src/index.ts:1166 → waterfall `llm/stream`:1174 → adapterStream:1064 → adapter.ts:138 fetch | `GenerateOptions` → `AsyncIterable<StreamChunk>` | SSE→translate adapter.ts:165 |
| 14 | 流式帧 | agent.ts:457 live.push → assistant-stream.ts:59 → agent.ts:449 emit `agent/assistant-stream` | `StreamChunk` → `AssistantStreamFrame` | 进程内瞬时，session-controller 侧缓存 |
| 15 | 落助手消息 | agent.ts:537 `session.append('assistant/message', {message, usage, stream})` | Blocks + compact stream → 事件 | 失败走 assistant/attempt（agent.ts:486/509） |
| 16 | 工具调度 | agent.ts:551 → tool-calls.ts:60 executeToolCalls → runGroup:122 | `ToolCallBlock[]` → 有序结果 | 并发上限默认 10；exclusive 即 barrier |
| 17 | 工具执行 | tool-calls.ts:170 prepare → core/tools index.ts:1501（pre-execute 1513、approval 1735、guards 1527）→ dispatch index.ts:1609（execute waterfall 1613 → tool.execute 1589）→ finalize 1649（post-execute 1791）→ finish 1677 | `ToolExecutionInput` → `ToolExecutionResult` | 每段可插拔；ask→ctx.approval |
| 18 | 工具结果落盘 | tool-calls.ts:269 appendToolResult → `tool/result`（sourceEventSeqs=[callSeq]） | `ToolExecutionResult` → 事件（surface append） | `meta` 供 UI 重放卡片 |
| 19 | 续步 | tool-calls.ts:85-101 → agent.ts:555 `return null` → agent.ts:366 target=next-step | `{concluded}` → 下一 step | additionalContexts 经 agent.ts:553 进 next-step |
| 20 | 收尾 | agent.ts:358 step/end；362 agent/turn-stopping；387 turn/end | `TurnEndReason` | 401-397：inbox 有待处理则再开 turn |
| 21 | 状态广播 | agent.ts:150 emit `agent/status` → session-controller index.ts:204 `api-session/status` | `AgentStatus` | remote-events.ts:32 上线协议 |
| 22 | 持久化路由 | session/src/index.ts:884 `session/event` → storage.ts:549 enqueueLive:274 → drainLive:288 → persistContiguous:319 → generation.ts:740 写文件 | `SessionEvent` → JSONL 行 | 200ms 批窗；flush/close 屏障 |
| 23 | 恢复修复 | agent-loop/index.ts:891 `interruptedTurnClosers`（repair.ts:209）→ handle.append | 崩溃尾巴 → 合成 closers | 只在 resume 时 |
| 24 | UI 订阅 | client/transport.ts:179 follow(assistantStream:true) → history.ts:120 follow → 快照+增量 | `SessionFollowFrame` → 客户端窗口 | 流式输出路径见 §6.2 |

---

## 12. 未确认清单

1. **TUI profile 的输入链路**：未在开源树中找到 tui 输入包；TUI 是否同样经 `session/prompt` Remote 未验证。（web/headless/SDK 三入口已确认。）
2. **`dshHomePath('sessions')` 的物理路径**：base bundle 配置只给出 `dshHomePath('sessions')`（packages/bundle/base/cordis.patch.yml:141），home 解析在 `@deepseek-ai/dsh-home-paths`，未读该包确认（如 `~/.dsh/sessions`）。
3. **默认 web profile 的 `tools/pre-execute` ask 生产者清单**：开源包内 ask 生产者集中在 experimental（auto-review:684、hooks 等）；默认 profile 是否装入、以及是否还有闭源权限插件，未验证。
4. **Typert `@Remote` 装饰器 → HTTP/WS 方法分发的代码生成细节**：只确认装饰器位置（session-controller/src/index.ts:461）、mux server（gateway/src/index.ts:251）与远程事件表（api/remotes/src/remote-events.ts），生成器 (`packages/typert/generator`) 未展开。
5. **headless/SDK 之外（web）冷会话创建与“首条消息前”附着细节**：`resolveObservedAgent`/promote 路径只做了 grep 级确认（api/session-controller/src/agent.ts:410-494、563-575），未逐行通读。
6. **各工具 `meta`/`error.info` 的具体字段含义**：随工具而异，未逐个工具枚举。
7. **`session/event` 观察者失败 containment 的确切语义**（`invokeContainedSessionObservers`）未逐行读，仅确认其存在与调用点（session/src/index.ts:884-889）。

---

## 13. 关键代码坐标速查

- 循环：`core/agent-loop/src/agent.ts`（kick:252、turn:298、preStep:267、step:400、prepareRequest:564、buildRequest:616）
- 循环调度：`core/agent-loop/src/tool-calls.ts`（executeToolCalls:60、runGroup:122、fillPool:199）
- 工具：`core/tools/src/index.ts`（ToolRuntime:808、register:1071、prepare:1501、dispatch:1609、post:1789、approval:1735、tools/result:1703）
- 会话：`core/session/src/index.ts`（append:798、commit/session-event:876、deriveMessages:984、requestHeader:912、toolHistory:953）
- 提示词：`core/system-prompt/src/index.ts`（assemble:614、tools 收集:634、renderPrompt:290）
- LLM：`llm/llm/src/index.ts`（prepareCall:956、stream:1166、llm/stream:1174、adapterStream:1064）；`llm/llm-deepseek/src/adapter.ts`（generate:65、fetch:138、translate:165）
- 入口/API：`api/session-controller/src/index.ts`（prompt:461、事件扇出:191-222）、`commands.ts`（prompt:319）、`agent.ts`（createAgent:563/resume:410）
- 持久化：`session/session-persistence-jsonl/src/storage.ts`（enqueueLive:274、drainLive:288、persistContiguous:319、install:535）；`src/index.ts`（materialize:1263）
- 压缩：`compaction/compaction-basic/src/index.ts`（pre-step:158、request-error:190、compactIfNeeded:269）；`region.ts`（replace 提交:506）
- Skill：`skill/skill/src/index.ts:439`；`skill/tool-skill/src/index.ts:163/179/215`；`skill/skill-filesystem/src/index.ts:135`
- 审批：`interaction/user-approval/src/index.ts`（request:215、decide/waterfall:267-284）；`sandbox/sandbox/src/escalation.ts:171`
