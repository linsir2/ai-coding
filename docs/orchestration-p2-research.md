# P2 方案调研：统一父子信息包、子代理写回与并发冲突

> 调研日期 2026-11-02。只做调研与候选方案，未改代码。
> 方法：本地代码/文档逐文件核对；近 7 天提交逐个 `git show`；clone 并逐行阅读
> `linsir2/agent-concurrency-correctness-layer`；clone `opencode` / `openai/codex` /
> `gemini-cli` / `deepseek-harness` / `openhands` 对照；联网核对框架协议与业界做法。

## 0. 一句话结论

现有系统的“派发”已经能用，但**父↔子没有一个统一的信息包契约**，导致同一个结算事实
在 packet / notice / event / ledger / meta / list 六处各写一遍、字段还不一致；写回只有
“临时 worktree + 不合并”这一种，非 git 会静默退回共享工作区，且没有路径争用检测。
下一步应先把**信封（envelope）收敛为唯一契约**，再在其上加**写协调**，而不是先建大调度器。

## 1. 本地现状（代码证据）

### 1.1 父 → 子
- `ParentContext` 只有 `messages` 与 `permissions`（`ai_coding/domain/subagent.py:70-74`），
  系统提示不继承（子按自己的工具面重建，`core/subagent.py:1393-1424`）。
- 真正的指令主通道是无长度上限的自由文本 `task`（`tools/subagent_tool.py:260-266`、
  `tools/agents_tool.py:152-158`）。
- `SubagentBrief`（`[TASK]/[CONTEXT]/[SCOPE]/[CONSTRAINTS]`，上限单一来源）**只有
  fan-out 在用**（`domain/subagent.py:78-135`、`core/research_fanout.py:142-155`）；
  工具派发路径没有 brief，也没有 schema 校验。
- `context_mode` 决定播种：`full` 拷父消息、`task_only` 不拷（`core/subagent.py:1333-1337`）；
  `fork_turns` 是 `spawn_agent` 的映射（`tools/agents_tool.py:183-202`）。

### 1.2 子 → 父（六条通道，字段不一）
| 通道 | 位置 | 字段/限制 |
|---|---|---|
| 同步 packet v2 | `domain/subagent.py:155-227` | 17 字段，最完整 |
| 工具面文本 | `tools/subagent_tool.py:44-124` | 只挑决策必需字段 |
| 后台事件 `BackgroundTaskDone` | `core/events.py:470-492` | 仅 run_id/name/status/summary/truncated/ledger，**无 facts/todos/branch/commit/usage** |
| 模型通知（SteeringQueue） | `core/subagent.py:1781-1809` | 单行：状态+200 字摘要+分支+ledger，无 origin_session |
| ledger | `core/subagent.py:1673-1704` | 完整 packet 落 `<ws>/transcripts/subagents/*.json` |
| meta sidecar | `core/subagent.py:1520-1541` | 供 resume；无 branch/commit/ledger |
| 行表投影 | `domain/subagent.py:449-461`、`tools/subagent_tool.py:473-486` | `list_agents` 全量、`subagent_list` 仅 4 字段 |

重复点：同一结算同时出现在 notice + event + row + ledger + list；丢失点：notice 不含
`facts/schema/todos/commit`，`workdir`/`artifacts` 常指向已被删除的临时 worktree
（`core/subagent.py:1638-1640`）。

### 1.3 写文件
- git 子代理用 worktree + `ai_sub_*` 分支（`core/worktree.py:28,106-116`）；退出前
  `commit_if_dirty`（`:128-166`），成功则把 branch+sha 回填 packet 并刷新 ledger
  （`core/subagent.py:368-393`）。
- **`commit_if_dirty` 失败与“无改动”都返回 `None`**，写成果可能静默丢失。
- 非 git 或 `can_isolate()==False` 时**静默退回主工作区**（`core/subagent.py:363-367`），
  没有降级提示；并发写同目录无锁。
- 后台子代理持有持久 worktree（`core/subagent.py:1032-1053`），**只在 close 时 commit，
  sha 被丢弃**（`:830-837`）。
- 父不知道子改了哪些文件：`artifacts` 来自子 loop 的 `last_artifacts`，没有 git diff /
  changed_paths；没有冲突检测与合并（`docs/orchestration-unified-design.md:21,197`）。
- `FileStateTracker` 每个 `build_tool_registry` 一份（`tools/builder.py:272,301-304`），
  父子不共享；resume 复用 registry 才保住子自己的读账本（`core/subagent.py:1434`）。

### 1.4 并发与通知
- 两层互不知情的上限：dispatcher 的 `max_parallel`（`core/dispatch.py:207`）与 runner 的
  `max_concurrent_subagents`（`core/subagent.py:318-322`，默认 4）。
- `spawn_agent` 自身串行（`tools/agents_tool.py:60`），但其子任务后台并发；池满只看
  `_background` 的 running 行（`core/subagent.py:447-455`），同步子与 resume 不计数。
- 可写同步子各用临时 worktree；`isolate=False`/非 git 时同工作区并发写，无 path claim。
- 通知在 iteration 边界注入（`core/agent_loop.py:1140-1152`），**下一 step 才可见**；
  idle 会话不唤醒（`core/subagent.py:1137`）；`interrupted/closed/cancelled` 静默
  （`:641-651`）；全局单队列无 origin_session。
- 运行中反馈只有 `send_message`→mailbox（`core/subagent.py:574-593`），且**只有后台
  handle 可寻址**，同步/ephemeral 子返 unknown（`:1372-1378`）。

### 1.5 文档已规划未实现
`docs/orchestration-unified-design.md` 的 `TaskRequest`（:127-139）、`Scheduler`
（:141-151）、`TaskRegistry`（:153-159）、`ExecutionSlotPool`（:163-175）、
`WriteCoordinator`/`writes_shared_workspace`（:185-198）全仓无实现；P3/P4（:250-251）
未开工。`docs/c3-wave-handoff-replan.md` 仍写 wave 已实现，但 wave 已被 `ff7be24` 删除。

## 2. 外部调研

### 2.1 `linsir2/agent-concurrency-correctness-layer`（ACCL）
- 定位：多 agent 并发改同一代码库的“正确性控制面”，RSC = OCC + 租约 fencing +
  短临界区 + Saga；**不是** 2PC/CRDT/Agent OS（`README.md:3-6`）。
- 分区原则：Z3 = per-agent overlay/worktree **无锁**；Z2 = 被共享/被读的资源必须走
  RSC（`docs/multi-agent-harness-design-final.md:227-266`）。
- 提交协议：钉 `base_version` + 读集一起持锁 + 全序锁 + blob 先落盘 + 提交点前复验租约
  + 单帧 WAL + 锁外通知（`harness/kernel/commit.py:1-15,151-189,343-353`）。
- 冲突：`CONFLICT` 带 diff，runtime 有界 rebase（`REJECT_STALE` 重试一次；
  `REJECT_STALE_READ` 对 staged 重跑 merge3；`CONFLICT` 走 merge3/resolver，超预算
  `QUARANTINED`）（`harness/runtime.py:460-500`）。
- 父子：`spawn_child` 只做**准入登记**，不自动调度（`harness/kernel/children.py:338-535`）；
  回包 `ResultEnvelope{status,coverage,artifacts,conflicts,budget_usage}`
  （`harness/types.py:317-340`），有 `DONE_PARTIAL` 与 `NEEDS_RECONCILIATION`，
  **不允许“未知即 DONE”**（`children.py:589-690`）。
- 通知：`EventLog` 单写者单调 seq，`append_batch(notify=False)` 后锁外 notify；
  `SubscriptionRegistry` 读即订阅、带 inflight 背压（`harness/event_log.py:1-9,239-261`、
  `harness/subscription.py:9-45`）。
- 可借鉴：版本钉住+读集校验、单写者/claim/fence、两阶段子任务登记+可重建索引+
  取消级联、`ResultEnvelope` 的三态汇总、锁外通知。
- 不适合照搬：Raft/RSM 与全量 TLA+（单机过重）；把 worktree 当唯冲突方案；Agent OS/
  角色图 DSL。

### 2.2 开源编程助手的子代理数据流
| 对象 | 派发/并发 | 父→子 | 子→父 | 写隔离/只读 |
|---|---|---|---|---|
| OpenCode | `subagent`，无显式上限，depth 1 | fresh + 单 prompt | 文本 + synthetic notice | 无隔离；只读靠权限白名单（shell 仍放行） |
| Codex CLI | `spawn_agent`+send/wait/interrupt/close，6 或 4 线程 | message，`fork_turns=all` 才拷贝历史 | 邮箱通知，文本 status | 共享 cwd，仅提示词 disjoint |
| Gemini CLI | `invoke_agent` 同步，批内并行 | 完整 prompt | 必须 `complete_task`，可 JSON Schema | 共享 workspace；工具白名单只读 |
| DSH | `subagent`，8 并发、depth 1 | spawn 只 prompt；fork 只播种已完成 turn | packet{output,structured,diagnostic,stopReason}+typed notice | 无子级 worktree；writeScopes 仅 advisory |
| Claude Code | `Agent`，20 并发、depth 3 | fresh；fork 例外 | 通知 + SendMessage | worktree 可选 |
| OpenHands | `launch_child_conversation` | task brief | 先 ack，后回 child id/status（不回内容） | local 默认 worktree，shared 显式 |

共性：**派发回执与结果分离**（先 child_id/status，结果另走 notice）；默认 fresh context，
fork 显式；notice 与子代理原话分 kind；写模式枚举化（none/serial/isolated/shared）；
容量与深度统一。

### 2.3 业界协议与并发
- A2A：`Task{id, context_id, status, artifacts, history, metadata}`、8 态状态机、
  SSE 推 `TaskStatusUpdateEvent`/`TaskArtifactUpdateEvent`、`SubscribeToTask` 重连；
  无 `DONE_PARTIAL`（`https://a2a-protocol.org/v1.0.1/definitions/`）。
- MCP Task：`taskId/status/ttl/pollInterval`，`tasks/get|result|cancel`，适合工具级持久句柄。
- CloudEvents 必选 `id/source/specversion/type`，`source+id` 判重。
- 写冲突手段与边界：worktree/分支只隔离工作区、最终仍需 3-way merge；merge queue 适合
  主干 FIFO 校验；OCC（ETag/If-Match）适合记录级；锁必须配单调 fencing token，否则租约
  过期后的旧写者仍能覆盖；CRDT 适合可交换数据，不适合代码语义。
- 通知：事件多为 at-most-once；需要“exactly-once 效果”时用幂等键 + 去重存储 + 重放游标。
- 误区：handoff 当普通工具调用（漏 `ToolMessage` 配对）；以为 fan-out 必有 fan-in；
  以为 worktree 无冲突；以为有锁就安全；以为协议自带 exactly-once。

## 3. 统一信息包（Envelope v1）建议

一个信封，两种方向，六条通道全部由它投影：

```
header:
  v: 1
  message_id        # 全局唯一，幂等/去重用
  correlation_id    # 父 turn + call_id（跨重启可追踪）
  parent_task_id    # 父任务/会话标识
  sender            # {session_id, agent, posture}
  recipient         # {agent, run_id?} 或 broadcast
  type              # task | result | event | steer
  created_at        # ISO 8601
  status            # queued|running|completed|partial|failed|cancelled|interrupted|timeout|needs_input|conflict
  deadline_ms?
```

父→子（`type=task`）payload：`objective/context/scope[]/constraints[]`（复用
`SubagentBrief` 的上限与拒绝清单）、`context_policy(fresh|fork_all|fork_turns)`、
`output_schema?`、`capabilities{readonly, write_mode}`、`limits`。

子→父（`type=result`）payload：`conclusion/facts/schema_valid/schema_errors`、
`artifacts[]`、`changed_paths[]`、`worktree{branch,commit,base}`、`conflicts[]`、
`usage`、`truncated/exit_reason`、`ledger_path`（全文指针）。

`type=event`：`event(started|progress|needs_input|write_conflict|completed)` + 最小 payload，
与 result 同 header；notice 由它渲染，不再另写一套字段。

通道映射（唯一真源 → 投影）：
- 同步工具结果 `ToolResult.data` = `result` 信封；
- 后台完成事件、SteeringQueue 通知 = 同信封（去掉全文，保留指针）；
- ledger = 信封全量；meta sidecar = 信封 header + resume 所需；
- `list_agents/subagent_list` 行 = 信封 header 的只读投影。

版本策略沿用 packet v2 规则：**只允许加可选字段**；消费方先读 `v`。

## 4. 写回与冲突（候选策略）

写模式枚举（默认 `isolated`）：
- `none`：只读，活工作区，无 worktree、无 claim。
- `isolated`：worktree + 分支；结束时 commit（成功才回 branch/sha，失败必须显式
  `commit_failed`，不再与“无改动”混同）；回传 `changed_paths`；**不自动合并**。
- `shared`：显式选择，写活工作区；必须经 `WriteCoordinator`（路径 claim + 租约/fence +
  串行提交），重叠范围拒绝或排队；父收到 `write_conflict` 事件。

冲突检测分三层，按成本从低到高：
1. 声明范围（`write_scope.paths`）重叠 → 派发前拒绝/排队（advisory 变 enforced）。
2. 完成时 `changed_paths` 与其它在途/已完成子的范围比对 → `conflicts[]` 进信封并通知。
3. 提交层（仅 `shared`）：`base_version` + 读集校验 + 3-way merge，冲突带 diff；
   有界重试，超限隔离（`QUARANTINED` 等价），绝不静默覆盖。

通知时效：信封事件进入父会话的**持久 inbox**（带 `correlation_id`/`origin_session`），
在下一个 step 边界注入；紧急中断仍走 steer/mailbox。进程重启后未读通知可重放。

## 5. 三个候选方案

### 方案 A：契约收敛（小步、低风险）
- 做：Envelope v1 落地；`SubagentBrief` 成为所有派发的唯一父→子结构；六条通道全部改为
  信封投影；补 `changed_paths`/`conflicts[]`/`commit_failed`；非 git 降级不再静默。
- 不做：写锁、合并、调度器、统一容量。
- 影响面：`domain/subagent.py`、`core/subagent.py`、`tools/subagent_tool.py`、
  `tools/agents_tool.py`、`core/events.py`、`core/agent_loop.py`（通知注入）、相关测试。
- 效果：信息交流统一、可审计、可观测；并发写冲突仍靠 worktree/人工。
- 风险：低；迁移期需兼容旧 ledger/sidecar（按 `v` 读取）。

### 方案 B：契约 + 写协调（推荐，分 A→B 两步）
- A 全部内容，外加：
  - `agent_loop` 增加 `write_mode` 透传（`none|isolated|shared`），替代裸 `readonly` 语义；
  - `WriteCoordinator`：进程内路径 claim + 租约/fence + `shared` 串行提交队列；
  - 完成时冲突检测（三层的前两层）与 `write_conflict` 事件；
  - 统一容量策略：一个对象管理 `max_concurrent` / depth / 池计数（含同步、后台、resume）；
  - 同步子代理可选“可寻址句柄”，使 `send_message` 覆盖运行中的子；
  - 持久通知 inbox（`origin_session` 过滤 + 重启重放）。
- 不做：跨进程 WAL/收据、Raft、全量恢复、自动 merge。
- 效果：可写子代理可并行且不互相覆盖；冲突在完成时可见并通知；容量不再双池漂移。
- 风险：中；`shared` 必须默认关闭，避免把现有工作区写坏；需迁移测试。

### 方案 C：统一编排内核（对标 ACCL，大改）
- B 全部内容，外加：`TaskRegistry`（状态机 + 父子账本 + 两阶段登记）、`Scheduler`
  （claim/lease/fence + 单一驱动器）、提交层 WAL/收据幂等、取消级联与恢复、
  合并队列（3-way + 校验命令）。
- 效果：跨重启/跨进程的并发正确性与可恢复性；真正“事务式”写。
- 风险：高；涉及 wire/存储/恢复/前端任务面板，周期大；当前单机产品未必需要。

### 对比
| 维度 | A | B | C |
|---|---|---|---|
| 父子信息包统一 | ✅ | ✅ | ✅ |
| 写回可观测（changed_paths/commit 失败） | ✅ | ✅ | ✅ |
| 可写并行不互相覆盖 | ❌（仅 worktree） | ✅（claim+serialized commit） | ✅（+版本/读集/merge） |
| 冲突及时通知 | 部分 | ✅ | ✅ |
| 容量/深度统一 | ❌ | ✅ | ✅ |
| 崩溃恢复/跨进程 | ❌ | 部分（进程内） | ✅ |
| 改动面/风险 | 低 | 中 | 高 |

**建议**：先 A（把契约立住并修掉“写成果静默丢失/降级静默”），评审通过后按 B 的
`WriteCoordinator` 增量实施；C 仅在出现跨进程/多租户需求时再评估。

## 6. 待用户拍板的点

1. 选 A / B / C 哪条路线（或 A→B 分两步）。
2. `shared` 写模式是否保留：保留=允许同工作区受控并行；砍掉=所有可写子强制 worktree。
3. 冲突时默认策略：拒绝派发（fail-closed）/ 排队 / 允许但完成时告警。
4. 通知是否需要“唤醒 idle 父会话”（当前只在下一 step 可见）。
5. Envelope 是否对外稳定（wire/前端可见）还是仅内部契约。
