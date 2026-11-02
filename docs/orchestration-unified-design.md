# 统一编排层设计方案 v2（已吸收评审，只读稿）

> v1 评审发现 3 个 P0：执行槽规则自相矛盾、写协调只覆盖 `write/edit` 且 isolated 无 merge、
> mode 改值未列入协议影响面。v2 修正如下，仍不改代码。
>
> 依据：`docs/orchestration-unified-research.md`、两路影响面核对、v1 评审意见。

## 0. 决策与结论

1. **旧路径全删**：`plan_orchestrator` 的 `session/legacy`、`PlanExecutor`、`PlanRunner`、
   `present_plan`、旧状态字段/事件/前端评审卡。共享物先迁出再删文件。
2. **mode 不改 wire**：持久事实仍是 `agent` + `readonly`；内部投影出
   `plan | readonly | build`，并在 `commit_posture` 强制 **plan ⇒ readonly**。
   wire 仍只有 `normal/plan` + `readonly`，前端不新增联合值。
3. **一个执行槽**：`ExecutionSlotPool(max_slots)`，槽单位 = **一次模型步 / 一个并行工具组**；
   编排类工具（subagent/spawn/fanout/控制类）**不占槽**，它们的子步骤才占槽。
   这样同步子代理、fanout 不会与父组互等。
4. **全局串行写**：所有 `writes_shared_workspace=True` 的工具（`write/edit/bash/mcp`）
   取同一 workspace 的写锁；编排/控制类工具不取写锁（否则持锁派子代理会死锁）。
   `isolated` 是 opt-in：子代理在自己的 worktree 写，完成后 **commit 并把 branch/sha 交回**；
   v1 假设的"自动 merge 队列"当前不存在，**v2 不承诺自动合并**。
5. **允许可写子代理**：`spawn_agent(readonly=False)` 在 build 下可用；plan 下只允许
   `parallel_research`（只读 fan-out），不允许任何 spawn。
6. **不新增第三张任务表**：复用 `_handles`（真源）+ `_background`（投影），
   把三个读口（`/tasks`、`task_list`、`list_agents`）收敛到同一 registry。

## 1. 旧路径删除清单（v2：补齐评审遗漏）

### 1.1 先迁出共享物

| 共享物 | 现状引用 | 迁到 |
|---|---|---|
| `PlanReport` | `plan_codex.py:14,90`、`plan_executor.py:30`、`commands.py:637` | `core/plan_report.py` |
| `outside_workspace_paths/message` | `request_assembly.py:39,585`、`acceptance.py:76,84`、`present_plan_tool.py:17`、`test_plan_workspace_gate.py:7` | `core/workspace_guard.py` |
| `TaskDriver`/`RoundProfile` | `goal_runner.py:35,162` | 保留原位（不是旧计划专属） |

### 1.2 整文件删除

`core/plan_executor.py`、`core/plan_runner.py`、`tools/present_plan_tool.py`，以及 11 个旧测试：
`test_plan_executor / test_plan_runner / test_plan_m1 / test_plan_attestation /
test_plan_budget / test_plan_proof / test_plan_continuation / test_present_plan_tool /
test_plan_approved_turn / test_plan_prompt_granularity / test_plan_exploration_budget`。

### 1.3 逐块删除（v2 补齐）

- **acceptance 整簇**（评审新增）：`SessionData.acceptance`、`agent_loop.py:1104,1337,2670`、
  `interventions.py:193,415,510`、`turn_books.py:291,649`、`config.max_acceptance_nudges/
  acceptance_verify_*`、`tests/test_acceptance.py` 口径。
  **注意**：`_verify_plan_item` 通过 `verify_items` 运行单个命令，不依赖 acceptance 契约，
  保留（`agent_loop.py:2741-2753`）。
- **step_deliveries**：`session.py:514`（`begin_plan` 重置）、`:741,809` 序列化、
  `handlers.py:264` 指纹、`agent_loop.py:1056` 进度签名。
- **plan_executing 全链**：`tools/base.py:178-182`、`request_builder.py:158-160`、
  `todo_write_tool.py:115-118,194`、`agent_loop.py:1052,1360,2395,2569`、
  `turn_policy` 多处、`session.item_deadline/step_item_id`。
- **present_plan 协议/前端**：`permission_gate.py:389`、`export_html.py:360`、
  `dispatch.py:170,843`、`turn_books.py:104,533,680`、`prompt.py:161`、`tools/base.py:58`、
  `handlers.py:376,386,728` 的 `"plan"` 默认/白名单、前端
  `util/submission.ts:110`、`util/plan-agent.ts`、`status-bar.tsx:32`、`home.tsx:165`、
  `ask.test.ts`、`reducer.test.ts:1227-1250`、`plan-review-render.test.tsx`。
- **任务字段**：`plan_tokens_used`（`task.py:517,546` 序列化）、`TodoItem.attempts`
  （**实际由 `plan_executor.py:310,356,375,388,448` 写入**，随旧路径删除）。
- 三处 `"session"` 回退：`commands.py:817`、`request_assembly.py:412`、`plan_executor.py:65`
  ——必须删，否则 duck-typed settings 仍走旧路径。

### 1.4 保留（共享）

`TaskDriver`、`PlanReport`（迁出后）、workspace_guard、`session.begin_plan`、
`UpdatePlanTool`/`plan_verify_items`/`verify_item`/`_verify_plan_item`、
`TodoItem.verify/depends_on/kind`、`TaskState.armed/armed_profile/rounds_run/resume_count/
tokens_used/resume_fingerprint/no_progress_resumes`（goal/boot 用）、`TaskUpdated`/`plan.updated`、
`FinishResearchTool`、`plan_reminders`、`PLAN_MODE_BLOCK`、`session.continuations`、
`plan_implementing`。

### 1.5 测试迁移（v2 补齐）

删 11 个；改写：`test_plan_mode_turn.py`、`test_plan_workspace_gate.py`（只留
outside_workspace 用例）、`test_repl.py:947-960`、`test_agent_loop_react.py`（plan guard/
plan block）、`test_prompt.py:131,278`、`test_plan_reminders.py:57-63`、`test_m3_boundaries.py`、
`test_finish_research_tool.py:242`、`test_tool_todo_write.py`、`test_domain_session.py`、
`test_domain_task.py`、`test_server_ipc.py:1733,1805-1983`、`test_server_bridge.py:493`、
`test_boot_resume.py`、`test_resume_gate.py:130-147`、`test_plan_guard.py`、
`test_plan_codex_parity.py`、`test_config_loader.py`、`test_tool_context.py`、`test_acceptance.py`。

## 2. plan / build / readonly（v2：不改 wire）

### 2.1 单一真源与不变量

持久事实保持：`session.agent` + `session.readonly`。内部投影：

```python
mode = "plan" if is_plan_agent(agent) else ("readonly" if readonly else "build")
# 不变量：plan 必然 readonly
```

- `request_posture/commit_posture`：`agent="plan"` + `readonly=False` → **强制 readonly=True**
  （拒绝失配，而不是静默接受）。
- `/readonly off` 当 `agent=plan` → 拒绝（"先 /plan off"），避免 plan 规则生效但 dispatch 不拒写。
- wire 不改：仍 `normal/plan` + `readonly`；`mode=build/readonly` 只是内部投影，
  前端不改联合类型。
- `write`：plan/readonly → none；build → serial（默认）。
  `isolated` 只能由 spawn 参数/配置显式开启，不能由 mode 派生。

### 2.2 谁用哪些参数

| 场景 | agent | readonly | mode | write | spawn |
|---|---|---|---|---|---|
| `/plan <topic>` | plan | true（强制） | plan | none | 只允许 `parallel_research` |
| Implement the plan | build | false | build | serial | 可写/只读都允许 |
| `/readonly on` | build | true | readonly | none | 只读 spawn 允许；可写拒绝 |
| `spawn_agent(readonly=True)` | explore（默认） | true | readonly | none | 子栈无 subagent |
| `spawn_agent(readonly=False)` | general/指定 | false | build | serial/isolated | 子栈无 subagent |

### 2.3 readonly spawn 与 gate

`SpawnAgentTool.mutates_world=True` 会被 readonly/plan 结构拒绝。v2 增加
**参数感知**：`BaseTool.mutates_world_for(params) -> bool`（默认返回类属性）。
- `readonly=True` → 返回 False（只读 spawn 不改变父世界）；
- `readonly=False` → 返回 True。
- plan 下仍整体禁止 `spawn_agent`（规则层 deny），只保留 `parallel_research`；
  这样不需要在 plan 里做参数裁决，语义更简单。

## 3. 统一抽象（v2）

### 3.1 TaskRequest

```python
TaskRequest(
  kind="inline" | "child" | "fanout",
  mode="plan" | "build" | "readonly",
  write="none" | "serial" | "isolated",
  context="task_only" | "full",
  turn_kind="normal" | "plan_implement",
  parallel=bool, timeout=..., parent=..., agent=..., readonly=...,
)
```

构造器校验：mode=plan/readonly ⇒ readonly=True & write=none；build ⇒ readonly=False；
`write="isolated"` 只允许 child。

### 3.2 Scheduler

```
Scheduler.execute(request)
  ├ inline  → ToolDispatcher.dispatch_batch
  ├ child   → SubAgentRunner（同步/后台/控制类）
  └ fanout  → ResearchFanOut
```

职责：mode 投影 → 参数；取槽（见 4）；写锁（见 5）；任务登记（见 3.3）；
子代理生命周期统一从这里走（工具不再直接持有 runner）。

### 3.3 TaskRegistry（不是第三张表）

- `_handles` 是子代理真源，`_background` 是其投影；`TaskRegistry` 只是**读接口**，
  聚合 `_handles` + `_background` + inline 观测事件。
- `/tasks`、`task_list()`、`list_agents`、`subagent_list` 共用同一个 registry；
  不再新增持久表。
- inline 批不持久化，只产生事件（不承诺跨重启）。

## 4. 执行槽（v2：解决死锁）

### 4.1 规则

- 一个配置 `ai.max_execution_slots`（替代 `max_concurrent_tools` +
  `max_concurrent_subagents`；默认 4）。
- **槽单位 = 一次模型步 或 一个并行工具组**。
- **编排类工具不占槽**：`subAgent`、`spawn_agent`、`parallel_research`、
  `send_message`、`interrupt_agent`、`resume_agent`、`close_agent`、`subagent_list`。
  它们只负责启动/等待；真正占槽的是被启动的子步骤。
- 父 turn 在工具阶段不持槽；同步子代理的子步骤占槽；父等待子代理时没有槽，
  因此 `slots=1` 也不会父子互等。
- 并行工具组取 1 槽；组内若含编排工具，该组不取槽（由子步骤取）。
- **池满**：`spawn_agent` 在活跃后台子代理数 ≥ `max_slots` 时拒绝（保留用户可见的
  `agents_exhausted`）；其余取槽**排队等待**，不拒绝。

### 4.2 死锁分析

- 父不持槽等待子 → 无父子环。
- 写锁只在 `writes_shared_workspace` 工具内持有，编排工具不取写锁 → 无"持写锁派子"环。
- isolated 子代理在自己的 workspace 取自己的写锁，不与全局锁互等。

## 5. 全局串行写（v2）

### 5.1 覆盖范围

新增 `BaseTool.writes_shared_workspace: bool = mutates_world`；显式设置：
- `write` / `edit` / `bash` / `mcp_call`：True；
- 编排/控制类：False（即使 `mutates_world=True`）。
`WriteCoordinator`（每个 workspace 一个 `asyncio.Lock`）在工具执行前持有，读工具不取。

### 5.2 isolated 写

- 默认 `write="serial"`（主工作区全局锁）。
- `write="isolated"`：子代理在 worktree 写，取 **worktree 自己的锁**；结束后
  `commit_if_dirty` 并把 `branch + sha` 交回父。
- **没有自动 merge**：父需要显式合并或 cherry-pick；v2 不承诺自动冲突解决。
- 持写锁的工具不得启动子代理（编排工具不取锁，结构上保证）。

## 6. 数据流（v2）

### build
```
用户输入 → AgentLoop → Scheduler(inline, mode=build, write=serial)
  iteration：取槽 → 模型步 → 释放 → 工具批
    - 普通并行组：取 1 槽 → gather → 释放
    - 含编排工具：不取槽；子步骤各自取槽
    - write/edit/bash/mcp：取 workspace 写锁
```

### plan
```
/plan <topic> → agent=plan, readonly=true（强制）→ 只读规则 + 无写
批准 → agent=build, readonly=false, turn_kind=plan_implement
     → Scheduler(child, "Implement the plan.", write=serial)
     → 普通 build turn；update_plan 写任务表；可派可写子代理（写锁串行）
```

### readonly
```
/readonly on → agent=build, readonly=true → 工具面剪 mutates_world
可写 spawn 被 gate 拒；readonly spawn 允许（mutates_world_for=False）
parallel_research → fanout(mode=readonly, isolate=false)
```

## 7. 影响面（v2 补齐，按层）

- **config**：删 `plan_orchestrator`、`plan_step_max_iterations`、`plan_item_wall_seconds`、
  `plan_max_step_turns`、`plan_token_budget`、`max_acceptance_nudges/acceptance_verify_*`；
  新增 `max_execution_slots`；保留 `subagent_max_tool_iterations`。
- **domain**：删 session 5 字段、task `plan_tokens_used`、`attempts`；新增 `TurnMode/
  WritePolicy` 投影（不落盘）。
- **core**：删 plan_executor/plan_runner/present_plan；新增 `scheduler.py`、
  `execution_slots.py`、`write_coordinator.py`、`task_registry.py`；改
  agent_loop/turn_policy/request_assembly/dispatch/subagent。
- **tools**：builder 去 orchestrator 分叉；spawn 加 `readonly/agent`；
  `mutates_world_for`；`writes_shared_workspace`；控制类走 Scheduler。
- **service/server/frontend**：boot 只认 goal；删 plan.receipt/评审卡；
  保留 `plan.updated` 面板；`/tasks` 读 registry。
- **tests**：见 1.5；新增 mode 矩阵、槽/写锁、spawn readonly、registry 同源。

## 8. 分期（v2：先定语义再改并发）

| 期 | 内容 | 验收 |
|---|---|---|
| P0a | 迁出 `PlanReport`/workspace_guard；补全删除清单（acceptance/present_plan/协议/前端） | 全量绿；无旧符号引用 |
| P0b | 删除旧执行器/旧字段/旧分支/旧测试；`plan_orchestrator` 以及三处回退删除 | `pytest` 0 failed；`grep` 无 plan_executor/plan_runner/present_plan |
| P1 | mode 投影 + plan⇒readonly 不变量；`/readonly off` 拒绝；spawn readonly 参数 + `mutates_world_for` | 矩阵测试；失配拒绝；wire 不变 |
| P2 | `ExecutionSlotPool` 合并两个并发配置；编排工具不占槽 | 并发/池满/同步子代理/fanout 无死锁 |
| P3 | `WriteCoordinator` 全局串行写；bash/job/mcp 覆盖；isolated 分支交付（无自动 merge） | 父子并发写测试；锁顺序无死锁 |
| P4 | Scheduler + TaskRequest 接管 inline/child/fanout；TaskRegistry 同源 | build/plan/readonly 真机链路；三读口一致 |
| P5 | 清理重复读口（subagent_list/parallel_research 是否保留为兼容别名） | 文档与工具面收口 |

### 8.1 实施现状（2026-11-02）

- P0a/P0b/P1 已完成（`c7832a7..23b8b32`）。
- **写协调与容量已落地（P2-B，`docs/orchestration-p2b-plan.md`）**：
  - 实际契约是 `BaseTool.write_paths_for` + `(root, 相对路径)` claim（`*` 表示工作区
    级），不是本文 §5 设想的单一 `writes_shared_workspace` 布尔与全局 `asyncio.Lock`；
    bash（含后台 job 生命周期 hold）与 mcp 一律按 `*`。
  - 容量用 `SubAgentRunner._active` 集合统一约束同步/后台/follow-up（`max_concurrent_subagents`），
    `ExecutionSlotPool` 未单独实现；depth 结构性恒为 1（子 registry 无编排工具）。
  - 持久通知 `NoticeStore` + `wake=True` 已落地。
- **仍未实现**：Scheduler / TaskRequest 接管 / TaskRegistry（P4）、合并队列 / 自动
  merge、跨进程租约与恢复；这些是 P2-UNIFIED / P2-C 待办。

## 9. 风险与待拍板（v2）

1. **acceptance 整体删**：`_verify_plan_item` 不依赖 acceptance，保留；其余阶梯随旧路径删。
   需要确认 `interventions/turn_books` 的 acceptance 分支一并删。
2. **`/readonly off` 在 plan 下**：拒绝（推荐）还是自动退出 plan？
3. **readonly spawn 在 plan 下**：只允许 `parallel_research`（推荐，规则更简单），
   还是允许 `spawn_agent(readonly=True)`？
4. **isolated 写默认**：默认 `serial`；`subagent_worktree_isolation` 现有默认 true 是否
   改为 false，或保留"隔离但不自动 merge"？
5. **池满语义**：后台子代理数 ≥ slots 拒绝；其他排队。确认。
6. **`plan_verify_items`**：保留（codex 的可选验收开关）。
7. **旧会话兼容**：旧 session.json 里的 acceptance/stepDeliveries/planTokensUsed
   读入时忽略还是迁移清空。

## 10. 结论

v2 修正了 v1 的三处 P0：执行槽改为"模型步/工具组 + 编排工具不占槽"；写协调覆盖
所有 `writes_shared_workspace` 工具并明确 isolated 无自动 merge；mode 不改 wire。
删除清单按评审补齐了 acceptance、present_plan 协议/前端、序列化与测试。
下一步按 P0a → P0b → P1 先做，P2/P3 在语义和删除稳定后再动。
