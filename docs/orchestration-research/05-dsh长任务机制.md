> 来源：并行调研 agent 产出（只读调查），高级工程师复核；供"长任务编排架构设计"使用。
> 抽检指引：核心论断的行号引用见文末"关键坐标"；未确认项保留在文末。
> 姊妹篇：`02-dsh编排数据流.md`（单回合数据流）。

# dsh 长任务（long-horizon）机制专项调研

> 仓库：`/home/linsir365/projects/deepseek-harness`，HEAD `57190202`（0.2.1-alpha.3 线）。
> 方法：只读代码；行号相对仓库根。**本文不重复** `/tmp/opencode/dsh-orchestration-flow.md`（单回合数据流）；聚焦"跨很多回合/很久、可能崩溃、需要不漂移"的机制。
> 线索优先级：代码 > 装配 yml > README/注释。装配关系注明 bundle/preset 文件。
> 所有行号以当前 HEAD 为准；不确定处集中在 §11。

---

## 1. plan 子系统（packages/plan/plan-mode）

**plan 是什么**：不是任务列表、不是进度对象、不是子代理——它是 **per-agent 的"协作模式"布尔状态**（active/inactive）+ 一段部署提供的规划期提示词。

- 状态结构：投影态 `PlanUnitState { active, wanted, running, activeAtLastHeader }`（packages/plan/plan-mode/src/types.ts:27-36）；线协议视图 `PlanProjection { active, pending }`（types.ts:21-24）。
- 持久化：session 日志事件 **`plan/mode { active: boolean }`**，声明为 "log-only, non-surface, whole-value replace. The last plan/mode wins"（packages/plan/plan-mode/src/index.ts:45-53）。**不落独立 plan 文件/对象**；plan 文本本身只出现在 `exit_plan_mode` 的 tool/call 参数与 tool/result 中（留在普通对话历史里）。
- 投影 fold：`planProjectionDefinition`（plan-mode/index.ts:136-168）从日志 fold；注释明确 "so resume and fork restore the state"（plan-mode/index.ts:9-13）。
- **谁创建/切换**：
  1. 用户 `/plan [off|message]` 命令（plan-mode/index.ts:229-275）；`/plan <message>` 会把 message 以 `agent.steer` 注入（259-266）。
  2. 模型 `exit_plan_mode` 工具（plan-mode/index.ts:277-372）：要求 plan 是 `#` 开头的 markdown（304-306），经 `userQuestions.ask` 弹出批准/继续规划二选一（313-330）。
- **怎么推进 / 生效时机**：`agent/pre-step` listener（plan-mode/index.ts:196-213）。用户选择若发生在开着 turn 的会话中，先进 `pendingIntents`（WeakMap，186）等待"下一个被接受的 in-turn pre-step"；那时才 append `plan/mode` 并可选注入一条 narration user 消息（onBoundary:448-459；narration:462-474）。无开 turn 时立即提交（set():425-445）。批准 exit 后 `pendingIntents.set({active:false, narrate:false})`（358），下一步生效——工具结果文案即 "carry out the plan starting with your next step"（291-296）。
- **与普通对话的关系**：同 session、同 agent、同工具目录。设计上"exit 工具常驻注册，进入/退出 plan mode 只改提示词 section、不改请求的工具目录"（plan-mode/index.ts:15-17、86-87）。
- **怎么防止执行漂离计划**：**只有提示词层 + 用户审批关卡，没有机械约束**。
  - 规划期规则来自部署配置的 `plan:policy` section（装配文案见 packages/bundle/web-app/presets/standard.patch.yml:54-65，base 亦装配于 packages/bundle/base/cordis.patch.yml:332）：不许写文件/提交、"imperative language means plan the implementation"、"exit_plan_mode 必须是唯一且最后的工具调用"等。
  - 文件明确："Sandbox mode and approval policy enforce restrictions independently and do not read or write plan state"（plan-mode/index.ts:5-7）——即 plan mode 本身**不**封锁 mutation 工具，也不修改沙箱/审批。
  - 批准后（执行期）**没有**"对照 plan 检查模型行为/进度"的任何机制（全包无此逻辑；todo 被文案指定承担"批准后的实现进度跟踪"，但不是强制核对，见 §3）。
  - 装配：`plan-mode` 同时出现在 base bundle（base/cordis.patch.yml:332）与 web-app 三个 preset（standard.patch.yml:51-52 等，带 `isolate.planMode`）。**【未确认】** base 与 preset 同名 id 的层叠覆盖细节（loader patch 合成规则）。

---

## 2. goal 子系统（packages/goal/*）

**goal 是什么**：同一 session 内的持久化"长任务目标"，带生命周期与自动续跑授权。

- 数据结构：`GoalSnapshot { id, revision, objective, phase: active|paused|blocked|complete, blockedReason?, maxGoalRounds }`（packages/goal/goal/src/types.ts:59-69）；`GoalView` 追加 `roundsStarted/createdAt/updatedAt/activation`（types.ts:90-99）。**activation（armed|disarmed）是进程内状态、从不持久化**（types.ts:71-72、104-105；goal 索引里 `GoalRuntimeState` 同样 process-local，goal/src/index.ts:183-190）。
- 存储：session 事件 `goal/change`（整体快照或 clear 墓碑；domain.ts:44、61-68），CAS 用 revision（index.ts:455-466 `expectCurrent` 拒绝 stale ref）。进程内 `GoalService`（`ctx.goals`，index.ts:240-241）在 `session/event` 上同步维护 activation（index.ts:255-267）。
- 投影：`goalProjectionDefinition`（index.ts:146-169）fold 日志，`goal replay failure` 时冻结状态（index.ts:141-158）。roundsStarted 的推进 = **被 admitted 的 goal 轮 user 消息**（source `kind:'goal', goalId, revision, round`）必须恰为下一轮：fold.ts:321-331。
- **谁创建**：模型 `create_goal`（需 direct-human 授权，tool-goal/src/index.ts:207-232 与 authority.ts:100-103）；人 `/goal <objective>`（command-goal/src/index.ts:143-153、190-197）；Remote `goals.create`（goal/src/index.ts:646-650）。默认 maxGoalRounds=256（index.ts:243-245、253）。
- **"任务没完就继续跑"的闭环在哪**：`packages/goal/goal-round-driver/src/index.ts`（插件在 **base bundle** 装配：packages/bundle/base/cordis.patch.yml:326-327）。链路：
  1. 监听 `agent/status`；agent 变 **idle** 时 `requestDrive`（index.ts:274-301）；
  2. `drive()`：要求 `phase==='active' && activation==='armed'` 且 agent idle、无 competing 排队消息（106-117、172-173）；先等所有子代理 `waitForChildren`（177-180）；
  3. 轮数上限检查：`roundsStarted >= maxGoalRounds` → `goals.block(code:'round-limit')`（182-188）；
  4. 生成 `renderGoalRoundPrompt(goal, round)`（prompt.ts:12-26，含目标、轮次、'Treat the current workspace... as authoritative... Before claiming completion, gather evidence... mark it complete' 等防漂移指令），以 **`agent.followup(message)`** 投递（index.ts:190-208）——即新一轮 = 同一 session 的下一个 turn；
  5. `agent/pre-step` 校验 reservation（`validReservation`：声称的 goalId/revision/round 必须与当前 armed active goal 完全一致，index.ts:364-378、380-445），失败 reject 并恢复其他被 claim 的消息；成功则 `startsRequestSeries: true`（444）。
  6. 每次 goal 变更置 `needsCheckpoint`，驱动前 `sessions.flush` 做持久化屏障（150-162）。
  7. 撤回/暂停语义：reservation 还在 queued 时 agent 变 idle → 撤回保护人工消息（286-298）；host 发起的 pause 会直接 `agent.cancel({kind:'user'})` 中断正在跑的 turn（305-311）。
- **谁判定 goal 完成**：**模型**（`update_goal action=complete|blocked`）或 **人**（/goal 命令路径直接调用域 `complete`/`clear`；`goal` 域 complete 是 Remote-exported 方法 index.ts:390-400）。
  - 模型的完成/阻塞权限经 `completionAuthority`（tool-goal/src/authority.ts:111-118）：必须是 **顶层 agent 本轮有直接人类消息**（authority.ts:80-84、100-103），**或** 当turn恰为当前 goal 的自动轮（`isMatchingGoalRound`，authority.ts:87-93）。
  - `blocked` 有硬阈值：默认需 ≥3 个连续 goal 轮才允许（tool-goal/src/index.ts:39、305-311）。
  - complete/blocked 后追加 wrap-up 指令（要求写面向用户的收尾消息、只报告证据支持的内容：tool-goal/src/wrapup.ts:17-40；注入处 index.ts:319-330）。
- 安全细节：模型不能 resume 被 paused 的 goal（须人恢复，tool-goal/index.ts:279-285）；create/edit/pause/resume 都要求 direct-human（263、270-277）。
- **重启/resume 后不自动跑**（关键）：插件装载时对**所有现存 agent** `disarm`（goal-round-driver/index.ts:447-452，注释明说"never inherits hidden automatic authority from an earlier producer instance"）；且 activation 不落盘。tool-goal 的 guidance 明确告诉模型："After session resume or fork, an active goal is disarmed: when a human asks to continue or resume ... use update_goal action resume to rearm it"（tool-goal/index.ts:115-123）。turn 因 max-tokens 结束也直接 disarm（goal-round-driver/index.ts:349-351）。
- **修正旧报告**：goal **并不监听 `agent/turn-stopping`**。全仓该事件的非测试监听者只有 `deliverables/workspace-changes`（workspace-changes/src/index.ts:155）与 experimental 的 hooks（hooks-claude-code/src/index.ts:276、hooks-codex/src/index.ts:263）。goal 续跑走的是 `agent/status` idle → followup。"turn-stopping 可再 steer 续跑"是事件的设计能力（声明 runtime-types.ts:381；测试 loop.spec.ts:1234），但非 goal 的实现路径。

---

## 3. todo / 清单类工具

有模型可见的任务清单机制：**`todo_write` 工具**（packages/todo/tool-todo/）。

- 注册：`ctx.tools.register(todo_write)`（tool-todo/src/index.ts:135-211）；每个 todo = `{content, status: pending|in_progress|completed}`（types.ts:21-26）。整单替换（last-write-wins），校验唯一、非空；`allowParallelInProgress=false` 时拒绝多个 in_progress（index.ts:80-100）。
- 存储：每次调用 append session 事件 **`todo/write {todos}`**（index.ts:199；声明 types.ts:28-33）。
- **关键限制**：注释明说 "Log-only UI state; **never derived history**"（types.ts:30）——`todo/write` **不进模型消息历史**（deriveMessages 不产出）；模型在后续步骤只看到工具结果里的计数摘要（index.ts:187-190）。UI 通过 `todos` 投影渲染（index.ts:123-134）。
- **跨回合不保留**：投影 fold 在**下一个 `turn/start` 时清空**为 null（index.ts:127-131），turn/end 后短暂保留给 UI 看收尾。全仓未发现任何把 todos 回注模型请求的 pre-step/system 注入（grep `todo/write|'todos'` 无此类消费者）。即：清单是给"人看的进度 + 当前回合内模型自查"，不是跨回合的模型工作记忆。
- 装配：web-app 三个 preset（standard.patch.yml:112-114、cordis.patch.yml:111-112、ptc.patch.yml），`allowParallelInProgress: true`。
- 全仓 `checklist` 无其他实现；唯一的"清单类"近亲是 experimental 的 agent-team 任务板（见 §9）。

---

## 4. steering / queue（inbox delivery 语义与中途纠偏）

inbox 是两个 durable 列表：`next-step` / `next-turn`（投影定义 packages/core/agent-loop/src/inbox.ts:20-65，从 `agent/inbox/spliced` 事件重建）。claim 时机：每个 step 的开头拿全部 `next-step` +（开新 turn 时）恰一条 `next-turn`（inbox.ts:109-114；调用 agent.ts:271）。

Agent 侧 facade（packages/core/agent-loop/src/agent.ts:154-173）：

| API | 落点 | 是否唤醒 |
|---|---|---|
| `followup(msg)` | `next-turn`（新 turn） | 是（163-165） |
| `steer(msg)` | `next-step`（下一个 step 边界注入当前 turn） | 是（167-169） |
| `inject(msg)` | `next-step` | **否**（171-173） |

- 唤醒语义：`send()` 的 wake 若撞上"已 abort 的活动"会强制降级为 `next-turn`（agent.ts:155-158）；`wakeDriver` 在 maintenance/aborted 期间把 wake 闩存、收敛时重放（214-235）。
- 产品侧 delivery 模式：`SessionPromptRequest.mode: 'queue' | 'steer'`（packages/api/session-controller/src/types.ts:348；契约注释："'queue' appends a turn; 'steer' interrupts the running one" packages/api/session-controller/src/client/contract/session.ts:82）。分发：`mode==='steer' ? agent.steer : agent.followup`（commands.ts:350-351）。
- 子代理侧同构：delivery `'queue' | 'steer'`（packages/subagent/subagent/src/control.ts:20、control-types.ts:91-104）；manager 的 `deliver()`：steer→`agent.steer`，queue→followup（manager.ts:615-627、678-689）。
- **长任务中途纠偏的手段面**（这就是"steer/queue 的完整语义"）：
  1. `steer`：不打断在跑的模型流，但会在**下一个 step 边界**把消息并入请求（严格说不是 mid-flight 插入）；
  2. 对**尚未开跑**的排队消息，可经 Remote `updateQueue` 做 `edit` / `remove` / **`steer`（把 next-turn 的排队项提升为立即 steer）**（packages/api/session-controller/src/commands.ts:418-487；steer 不可用时明确报错 session/steer-unavailable，463-465）；
  3. `cancel`：清空 inbox（除非 keepInbox）并 abort 当前活动（agent.ts:175-181）——goal-round-driver 用它实现"host 暂停立即生效"（见 §2）。
  4. `agent/turn-stopping`（serial await，agent.ts:362）：turn 收尾前最后一个可插拔点，listener 可再 steer 使同 turn 继续（测试示范 loop.spec.ts:1234；产品内建消费者只用于收尾记录/hooks）。
- 客户端还有 submission 生命周期（beginSubmission/handleRunning 等，session-controller 客户端 session.ts:230、256）管理"队列中的投稿"的 UI 状态，不改变上述投递语义。

---

## 5. 子代理隔离（packages/subagent/*）

- **怎么起**：模型调 `subagent` 工具（tool-subagent/src/index.ts:212-219 描述："starts an independently managed subagent and immediately returns its id. The runtime notifies you when it finishes."）→ `ctx.subagents.startActivation({provider, label, request, signal, delivery:'parent'})`（index.ts:330-337）→ manager `startLocal`（subagent/src/manager.ts:379-476）→ **`ctx.agents.create({ sessionId: childId, parentAgent: parent, meta: childSessionMeta(...) })`**（manager.ts:905-922，create 分支 913-922；冷恢复分支 906-912 用 `agents.resume`）。
- **父子边界**（子会话 header 元数据，child-agent.ts:135-154）：
  - `parentSession: parentHeader.id`（146）——血缘记录；
  - `origin: 'subagent'`、`delegationDepth`（150-152，跨持久化的递归预算）；
  - **独立 session/独立上下文**：默认 fresh（"does not share this conversation's context"，tool-subagent/index.ts:127-136）；fork 提供者则 seed 父的 completed turns（wording:113-125；`isSeeded` child-agent.ts:147）。
  - **权限钉死**：approval 固定 `'never'`、sandbox 只继承"父 session 的显式 override"（child-agent.ts:237-255），并以 `source:'delegation'` 事件写入子日志（266-279）供重建；运行时 context 明确告知子："your permission scope was fixed when you were started... do not retry the denied operation"（child-agent.ts:171-176）。
  - 深度上限：`resolveChildDepth`（child-agent.ts:49-58），工具 config `maxDepth` 默认 1（tool-subagent/index.ts:68-77）。
- **结果怎么回注父会话**：子 "settle" 时 `notifySettlement`（manager.ts:1186-1206）→ `createSettlementMessage`（continuation-messages.ts:106-168：一行结局摘要 + 子最后的文本 + structured result）→ `sendWaking(parent, message, parent.status==='idle' ? 'queue' : 'steer')`（manager.ts:1199）——父空闲则作为一个新 turn 的 user 消息唤醒，父忙则 steer 并入。continuable 子另有 `withContinuableReturnGuidance` 要求子主动 `send_message` 给父（continuation-messages.ts:80-96；父→子的继续通道 `deliverToChild` manager.ts:1229-1244）。
- **等待策略**：tool-subagent 的系统提示 section 教模型"派发后继续做有用的事；只剩等待时结束 turn，不要 sleep/poll；下一个结果会开新 turn"（tool-subagent/index.ts:368-381）；headless 收尾 loop 显式 `waitForChildren`（bundle/headless/src/index.ts:379-386）。
- 运行统计：`subagent/start|end` 生命周期事件（lifecycle.ts:96-116），end 携带 stopReason（completed/aborted/max-tokens/refusal/error，lifecycle.ts:134-160）。

---

## 6. 记忆 / 跨回合机制盘点

| 机制 | 一句话 | 位置 |
|---|---|---|
| 会话日志即全部历史 | 请求消息每步从日志 surface 投影，无独立 messages 数组；resume/fork 从日志重建 | `packages/core/session/src/index.ts:984-1005`（旧报告 §3.2） |
| session-projection 注册表 | 各域（plan/goal/todos/inbox/...）以纯 fold 维护持久状态，支持 checkpoint 缓存与恢复 | `packages/session/session-projection/src/index.ts:20-34`（registry）、`:48-70`（ProjectionDefinition 契约） |
| session-query | 应用代码可读/过滤/全文搜索历史会话（live 优先）；模型侧工具在 experimental | `packages/session-query/session-query/src/index.ts:98-106`（`SessionQueryEngine`，服务键 `sessionQuery`） |
| agent-instructions | AGENTS.md 类工作区指令：baseline 在第一请求前进 durable context；fs 工具触碰文件后刷新注入（pre-step） | `packages/context/agent-instructions/src/index.ts:334-360`（pre-step 注入）、`:362-378`（工具触碰触发）、`:1-8`（模块意图） |
| session-reference | 用户 @ 其他会话 → 有界只读快照作为"不可信背景"user 消息注入 | `packages/context/session-reference/src/index.ts:1-6`、`:51-60`（固定的不可信警告前缀） |
| time-context | 可选时钟：合格 step 注入 time reading（默认 ≥10 分钟间隔），让模型感知已过时间 | `packages/context/time-context/src/index.ts:1-4`、`:55-67` |
| file-reference | `@` 文件引用的发现 seam（宿主 UI 用） | `packages/context/file-reference/src/index.ts:17`（模型指导文案）、`:26-43` |
| tmux-context / harness-dev-context | 每 turn tmux 位置上下文 / Web GUI 开发用源路径+URL 上下文 | `packages/context/tmux-context/src/index.ts:1-17`；`harness-dev-context/src/index.ts:1-8` |
| 交付物：present | `present` 工具把产物文件登记为交付物（只记路径不复制内容） | `packages/deliverables/tool-present/src/index.ts:40`；README "Move files…" |
| 交付物：workspace-changes | 每 top-level turn 前后 git 快照 + 文件工具编辑 capture，append `workspace/changes` 事件（仅 turn 号），供 UI/验证 | `packages/deliverables/workspace-changes/src/index.ts:1-9`、`recorder.ts:351` |
| compaction（摘要） | 压力触发/overflow 触发的区间摘要替换（旧报告 §9） | `packages/compaction/compaction-basic/src/index.ts:158-235` |

**结论**：dsh 没有自动的"跨回合工作记忆"（不会自动把上回合摘要/清单回灌）；长任务连续性 = 完整日志 + compaction 摘要 + 各域投影状态 + 显式注入类 context（instructions/time/tmux/引用）。模型自己的规划状态就只能靠普通对话历史（plan 文本、goal prompt、todo 摘要）。

---

## 7. 崩溃后续跑（resume / repair / 重启行为）

**resume 修复（一致性，不是续跑）**：
- `AgentLoop.resume` → `resumeWith`（packages/core/agent-loop/src/index.ts:843-926）：拿到写权 → 读全日志 → **`interruptedTurnClosers(persisted)`** 合成缺失的 tool 错误结果/`step/end`/`turn/end`（index.ts:891-892；实现在 packages/core/session/src/repair.ts:209）→ 以 seed 重建 session（893-899）→ `setupAndPublish(..., 'resume')`（908）。
- repair 的目标是"让日志回到可继续的一致终态"（注释 index.ts:884-887），**不重放未完成的 turn**。

**resume 之后是否自动继续**：**不会**（对用户/编排层而言都需要外部触发一次投递）。
- resume 只把 agent 发布为 idle（`initializeAgent`/`publish`：agent-loop/index.ts:698-701、808-818；loop 构造后无对 `inbox.hasPending` 的自动 kick）。
- 唤醒只能来自：`followup/steer/inject`（agent.ts:160）、maintenance 收敛重放（200）、kick 尾部重放（262）。重放条件都要求本次进程内有 `wakeRequested`。
- 产品上真正"非人工触发的自动投递"只有四处（全仓排查后）：
  1. **goal-round-driver**：armed goal 的 idle 续轮——但 §2 已确认装载/resume 后一律 disarmed，故重启后不跑；
  2. **schedule**：到期提醒（见下）；
  3. **subagent settlement**（manager.ts:1199）；
  4. headless 每次运行显式 `followup(task)`（bundle/headless/src/index.ts:347-354、375）。
- Web 冷会话恢复链：session-controller `resume`（api/session-controller/src/agent.ts:410-445，经 sessionQuery.observeSession 校验后 `ctx.agents.resume`）——也只发布，不投递。

**唯一跨重启自动推进的机制是 schedule（提醒）**：
- 任务持久化在 Host 存储（非 session 日志）；插件启动即 `runtime.requestDrive()`（packages/schedule/schedule/src/index.ts:139-165，尤其 163）。
- `drive()` 扫描 `status==='active' && scheduledAt <= now` 的任务（含**已过期 catch-up**，runtime.ts:90），**冷恢复**目标会话（`sessionController.resolveAgent`，runtime.ts:101）→ `agent.followup(message)`（123）→ `sessions.flush` 确认后写回执（124-141）；再对最近的未来任务设 timer（155-164）。
- 装配在 web-app bundle（packages/bundle/web-app/cordis.patch.yml:140）。

**结论**：dsh 的崩溃语义是"日志修复后等人/等外部事件"；没有"进程重启 → agent 自主接着跑未完成任务"的机制。goal 需要人 rearm（§2），普通 turn 需要新的 followup。

---

## 8. 长会话上下文管理（补全"长"维度）

已有报告 §9 覆盖 compaction 主体；这里补全其余：

1. **tool-result pruner**（packages/compaction/compaction-tool-result-pruner/）：
   - 超阈值（默认 8192 字符）工具结果被替换为 head(4096)+`middle pruned` 标记+tail(1024)（src/index.ts:83-122；config.ts DEFAULTS）；非文本块（图片等）保序保留（93-96）。
   - 每个替换：先 append `compaction/prune` **shadow-price 事件**（用 tokenMeter 估价，160-164），再以 `surfaceOp:'replace'` append 替代 `tool/result`（165-171）——原始结果仍完整留在日志，可精确回放。
   - 触发路径只有压缩：`compaction-basic` 在 pressure 流程先跑 pruner 再复测（compaction-basic/src/index.ts:292-297、322-327）。不会独立周期性裁剪。
2. **request/header 与 series**（请求序列管理）：
   - 每次请求变化时记 `EpochHeader`（配置+adapter defaults+tools），reason ∈ `initial|resume|change|series`（packages/core/session/src/types.ts:270-274；记录点 agent.ts:635-651）。header 未变但"开启了新的消息序列"时记 `reason:'series'`（agent.ts:649-651）。
   - `startsSeries` 的两个来源：pre-step listener 显式声明（goal-round-driver/index.ts:444）与 surface 代次变化（compaction 替换后：agent.ts:632-633）。
   - 消费：(a) system prompt 重发决策——`SystemPromptProjection.project`：route 不支持 in-history、或新 series、或清空时，把 `system/message` 收敛回 head 节点（替换/清空），否则只 append 差异（packages/core/agent-loop/src/runtime-context.ts:88-103）；(b) 工具声明历史序列重建（session/src/tool-history.ts:36-41：series 边界重置 declared/updates）。
   - 意义：长会话里 system prompt 与工具 schema 不会每请求全量重塞；而压缩/编排断点后强制"重锚"，保证模型上下文自洽。
3. **每 step 刷新的 runtime-context 快照**：动态上下文（如时间、tmux、开发上下文等 section）以 `runtime-context` user 消息随 step 更新/替换（runtime-context.ts:114-163；提交点旧报告 §2.3 agent.ts:411-432）。
4. **request/context**：provider/model/contextWindow/systemPromptUpdate 变化单独记录（agent.ts:669-683）。
5. **inbox 状态 durable**：排队消息是日志投影（inbox.ts:20-65），resume 后仍在（虽然不会自动跑）。
6. **spill**：session-reference 的超限溢出旁路（session-reference/src/index.ts 引 `spill.ts`）；attachment/image offload 属压缩旁路（旧报告 §7）。

---

## 9. experimental 未装/可选包中的长任务相关（按 README description 一句话）

均位于 `packages/experimental/`（README.md:2）：

| 包 | 一句话 |
|---|---|
| `ralph-bundle` | "Run explicitly requested repeated work with **fresh agents**." |
| `tool-ralph` | 模型侧 `ralph` 工具：固定前台 fresh-agent 循环走向一个不可变目标 |
| `agent-team` + `tool-agent-team` + `client-ui-agent-team` + `agent-team-profile` | 具名队友 + 收件箱消息 + **durable 共享任务板**（team 实验组合） |
| `hooks-claude-code` / `hooks-codex` | 跑现存 Claude Code / Codex hooks.json：可 block 提示与工具、附加 context、**force continuation**（`agent/turn-stopping` listener，hooks-claude-code/src/index.ts:276、hooks-codex/src/index.ts:263） |
| `auto-review` | 工具执行前用当前模型做逐调用审查（pre-execute ask 生产者） |
| `tool-session-query` + `session-search` | 模型侧跨会话历史搜索/阅读工具 |
| `terminal-bundle` + `tool-terminal` | 跨 tool call 保活 shell 进程（长任务的会话粘性状态） |
| `tool-worktree` / `worktree` | 建 git worktree 并切换 session 目录（并行工作线隔离） |
| `webhook` + `webhook-github` | 外部事件策略创建 Workspace Sessions（外部触发链） |
| 其余（browser-use-*、computer-use-*、cua*、ptc-runtime-python、voice/speech、inspector、cot-translation、session-title-*、translator、skill-badge 等） | 与长任务主题弱相关，未展开 |

---

## 10. 总评

**核心机制（core bundle 装配，长任务不漂移的主要依赖）**：
1. **会话日志即状态 + 投影 fold**：一切可 resume/fork/审计；请求与工作记忆同源（session-projection + deriveMessages）。
2. **goal 域 + goal-round-driver**：同 session 自动续轮、"没完就继续跑"的闭环；CAS revision、轮数上限、blocked 阈值、完成/阻塞授权校验、wrap-up 收尾——这是唯一内建的"自主长跑"机制（但重启后需人 rearm）。
3. **inbox 双通道（next-step/next-turn）+ steer/queue/updateQueue**：中途纠偏与人机接力。
4. **子代理隔离 + settlement 回注**：把长任务拆给独立上下文的子代理并行跑，结果以消息形态回到父回合。
5. **上下文长寿化**：compaction（摘要+pruner）、request/header series、runtime-context 快照、agent-instructions/time-context 等显式注入。
6. **崩溃修复 closers**：resume 前把日志补回一致终态（修复而非续跑）。

**插件/装配补的**：plan-mode（提示词式规划关卡）、todo_write（UI 进度）、schedule（跨重启定时唤醒）、present/workspace-changes（交付物与回合变更）、experimental 的 ralph / agent-team / hooks-force-continuation / terminal / session-query 工具等。

**明显缺失**：
1. **重启自主续跑没有**：resume 只修日志+发布 idle；goal 被 disarm；没有任何 "pending inbox 自动重放" 逻辑。唯一跨重启自动投递是 schedule 提醒。
2. **执行期对 plan 的机械核对没有**：plan 只在规划期由提示词约束；批准后无对照检查，"漂离计划"检测为零。
3. **跨回合的模型可见任务清单没有**：todo 不进模型历史、下一 turn 清空、无自动回注；模型跨轮目标/进度只有 goal prompt 与对话历史。
4. **崩溃/中断的自动重试没有**：interrupted turn 只被补成终态；未完成工作要等新触发（且 max-tokens 会 disarm goal）。
5. **完成验证是"自报+权限闸"而非机器判定**：goal complete 由模型（在授权条件下）自报，系统只校验授权与最低轮数，不校验目标真的达成；workspace-changes 只记录不评判。

---

## 11. 未确认

1. base bundle（base/cordis.patch.yml:332 plan-mode）与 web-app presets 中同名 id（standard.patch.yml:51-52 等）的层叠/覆盖规则未逐行验证（属 loader profile patch 合成，未展开）。
2. `hooks-claude-code`/`hooks-codex` "force continuation" 的具体实现（turn-stopping 里如何继续）未深挖——按任务要求第 9 问只列名。
3. experimental 的 ralph / agent-team 任务板 / webhook 的内部机制未读代码，仅有描述级证据。
4. `jobs/*`（background-job registry）与 bash 后台作业的长任务关联未展开（不在 10 问列表）。
5. plan-review 在 UI 侧的专门呈现（intent: plan-review）只确认协议侧传递（plan-mode/index.ts:326），客户端渲染未追踪。
6. `setupAndPublish` 在 810-829 行之后是否存在其它异步唤醒路径，仅按行区间阅读确认（810-818 为 runMaintenance/initialize/rollback），未见 kick。
7. TUI/桌面入口的 resume 自动行为未追踪（承旧报告"TUI 未确认"）。

---

## 12. 总表：机制 | 解决的问题 | 实现位置 | 核心/插件

| 机制 | 漂移 | 遗忘 | 崩溃 | 纠偏 | 验证 | 实现位置 | 核/插件 |
|---|---|---|---|---|---|---|---|
| 会话日志 + 投影 fold |  | ✔ | ✔ |  | ✔(审计) | `session/src/index.ts:984`；`session-projection/src/index.ts:20` | 核心 |
| plan-mode（模式+exit 审批） | ✔(规划期提示词) |  |  |  | ✔(人审) | `plan/plan-mode/src/index.ts:196,277,425` | 插件(装配) |
| goal 域 + CAS/上限/blocked | ✔(结构化目标) | ✔(日志重建) |  |  | ✔(授权闸+阈值) | `goal/goal/src/index.ts:240,609`；`fold.ts:313` | 核心域 |
| goal-round-driver 自动续轮 | ✔(每轮重述 objective) | ✔(same session) | ✘(重启 disarm) |  | ✔(wrap-up) | `goal/goal-round-driver/src/index.ts:146,208,447` | 核心(装配) |
| todo_write |  | ✘(不跨 turn) |  |  | ✔(进度可见) | `todo/tool-todo/src/index.ts:135,199`；`types.ts:30` | 插件 |
| inbox next-step/next-turn + steer/queue |  | ✔(durable) | ✔(状态重建) | ✔ |  | `agent-loop/src/inbox.ts:20,109`；`agent.ts:154-173` | 核心 |
| updateQueue(edit/remove/steer) |  |  |  | ✔ |  | `api/session-controller/src/commands.ts:418-487` | 核心(API) |
| turn-stopping 钩子 |  |  |  | ✔(可续跑) |  | `agent.ts:362`；消费者 `workspace-changes:155` | 核心(事件) |
| subagent 隔离+settlement 回注 | ✔(独立上下文) | ✘(不继承父 transcript) | ✔(增量settle) | ✔(send_message) |  | `subagent/src/manager.ts:379,913,1187` | 核心 |
| schedule 定时唤醒 |  |  | ✔(跨重启) |  |  | `schedule/src/runtime.ts:85-164`；`index.ts:163` | 插件(web-app) |
| interruptedTurnClosers |  |  | ✔(日志修复) |  |  | `session/src/repair.ts:209`；`agent-loop/index.ts:891` | 核心 |
| compaction + tool-result pruner |  | ✔(摘要) |  |  |  | `compaction-basic/src/index.ts:158,292`；`pruner/src/index.ts:136` | 核心(装配) |
| request/header series |  | ✔(上下文重锚) |  |  |  | `agent.ts:632-651`；`runtime-context.ts:95`；`tool-history.ts:36` | 核心 |
| agent-instructions/time-context 等注入 | ✔(规则常在) | ✔(时间/环境感知) |  |  |  | `context/agent-instructions/src/index.ts:334`；`time-context/src/index.ts:1-4` | 核心(装配) |
| present / workspace-changes |  |  |  |  | ✔(交付物/回合变更) | `tool-present/src/index.ts:40`；`workspace-changes/src/index.ts:155`；`recorder.ts:351` | 插件 |
| ralph / agent-team / hooks 等 | ✔(ralph 目标不可变) |  |  |  |  | experimental 各包（§9） | 实验 |
