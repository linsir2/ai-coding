# 统一编排层：历史改造盘点与现状调研（只读）

> 目的：把"子智能体并行/串行/编辑/只读"的历史改造、以及 build 主闭环与 Codex 计划路径
> 的现状，重新对齐成一个**统一编排层**的设计前提。本轮不改代码。
>
> 依据：`docs/orchestration-explore/*`、`docs/orchestration-refactor/*`、
> `docs/orchestration-research/*`、`docs/b0-parallel-research-plan.md`、
> `docs/subagent-dataflow-v2.md`、`docs/plan-orchestration-*`，以及相关提交
> `8c0b63c / 5d55cd9 / e53b8e9 / 9a7b517 / 78bfd86 / 4810b5d / 76221a1 / 2073f06 /
> a503179 / 941b55b / ff7be24`。

## 1. 时间线（与编排层有关）

| 阶段 | 事 | 结果 |
|---|---|---|
| 2026-10-08 | `orchestration-explore/2026-10-08-redesign-brief.md`：编排+记忆重设计提案（M1–M14） | 提案，未整体实施 |
| 10-15 | `orchestration-refactor/13`：干预阶梯/方法段/环境块/断流续写（**不是**子代理编排） | 已落地 |
| 10-16 | `20-编排对比待办清单`：D-5 并发策略复核、readonly 探索、C-3b 子代理列表 | 已落地 |
| 10-17/18 | D-5 `a503179`：连续 `parallel_safe` 段分组并发；readonly `941b55b`；`subagent_kill` 登记待做 | 部分落地 |
| 10-19 | B0 `8c0b63c`：`parallel_research` 只读 fan-out；`5d55cd9`：规划阶段允许只读并行调研 | 已落地 |
| 之后 | 父子交接契约 `e53b8e9`、子代理信息流 v2 `9a7b517` | 已落地 |
| 计划层 | wave 执行器 `78bfd86`、wave 交接/重规划 `76221a1`/`2073f06` → **退休** `ff7be24` | 已删除 |
| 现在 | Codex 计划路径（`plan_orchestrator=codex` 默认）+ C-5b 可续跑子代理句柄 | 已落地 |

## 2. 历史设计里关于"并行/串行/编辑/只读"的原文结论

1. `plan-orchestration-design.md`：并行只用于可并行部分；**写操作串行或 worktree 隔离 +
   合并队列**；读/研究可并行（DAG 方案 B 的结构化编排，未实施）。
2. `b0-parallel-research-plan.md`：并行只读调研 2–4 个问题，`explore` + `readonly=True`
   三重保证（agent 规则、子会话 readonly、dispatch 结构性拒绝）；**明确不做写并行**。
3. `20-编排对比待办清单` D-5：连续 safe 段分组并发（dsh `runGroup` 语义）；
   WARN 是天然屏障；审批不交错、模型序提交、halt 组边界。
4. `orchestration-refactor/13` 的 methods/environment 是认知提示，不是编排。
5. `subagent-dataflow-v2` / `handoff-contract`：父子用 brief/packet 结构化交接，
   子代理 todo 不自动并入父清单。

## 3. 当前数据流（事实）

### 3.1 build 主闭环的工具批次
- `AgentLoop._run_turn` → `dispatcher.dispatch_batch`（`core/dispatch.py:233-303`）。
- `_concurrent_groups`（`:363-386`）：**连续**且 `parallel_safe=True` 且权限 ALLOW 的调用
  归为一组 `asyncio.gather`；任一 WARN/串行工具成为屏障；结果按模型调用序回写。
- `readonly`：`_surface_tools` 剪掉 `mutates_world` 工具（`agent_loop.py:3465-3482`）；
  dispatch gate 再拒一次（结构 + 规则层）。
- `write`/`edit`：`parallel_safe=False` → 同批串行；`FileStateTracker` 做 stale-read；
  计划模式的 `plan_root` 限制写目录。

### 3.2 子代理
- 同步 `SubAgentRunner.run()`（`core/subagent.py:278-322`）支持 `readonly`，被
  `parallel_research`/旧 `subAgent` 使用。
- 后台 `spawn_background()`（`:401-460`）创建可续跑 handle，但 `_open_handle(..., readonly=False)`
  **硬编码非只读**（C-5b 评审已标注）；`spawn_agent` 没有 readonly 参数。
- `_supervised` 走 `_attach_for_run` → worktree（isolate 时）→ `_run_turn`；句柄可
  send/interrupt/resume/close。
- 并发：runner 自己的 semaphore + `spawn_background` 的 running 行计数；`parallel_research`
  **不加自己的并发**，直接用同一 runner 的槽位。
- 只读 fan-out 与 `spawn_agent` 是两套入口，共用 runner，但**没有统一的执行策略对象**。

### 3.3 计划路径（Codex）
- `/plan <topic>`：`session.agent=plan`、readonly；`<plan-mode>` 易失尾块；
  `update_plan` 报错；计划以 `<proposed_plan>` 文本产生。
- 批准：`commit_posture(build)` + `"Implement the plan."` → 普通 turn；`plan_implementing`
  期间无迭代上限、无 task gate、隐藏 `todo_write`、不注入 `<task-state>`。
- 遗留 `PlanExecutor/PlanRunner` 仍可由 `plan_orchestrator=session|legacy` 选中，
  但默认不可达；`armed_profile`/`step_item_id`/`StepDelivery` 属于它们。

## 4. 已有的"半成品/被遗弃"清单

| # | 内容 | 现状 | 缺口 |
|---|---|---|---|
| 1 | wave 执行器/波交接/重规划 | **已删除**（`ff7be24`） | 不要复活；统一编排层不应再引入"波"概念 |
| 2 | `PlanExecutor`/`PlanRunner`/`TaskDriver` 的 step 编排 | 仍存在，默认不可达 | 与 build 主闭环是两套编排；应统一或彻底退休 |
| 3 | `parallel_research` vs `spawn_agent` | 两套入口共用 runner | 缺统一任务类型：read fan-out / async child / serial task |
| 4 | 后台子代理只读 | `spawn_background` 硬编码 `readonly=False` | 无法派"只读后台代理"；`spawn_agent` 无 readonly |
| 5 | 写并行 | 主闭环只做"同批写串行"；子代理写靠 worktree | 跨代理写没有全局串行/合并队列；`isolate=false` 时会并发写同一工作区 |
| 6 | `subagent_kill` | 历史 TODO；C-5b 用 `interrupt_agent`/`close_agent` 补上 | 语义已覆盖，但历史文档仍写"待做" |
| 7 | 编排策略对象 | 不存在 | 并行/串行/只读/编辑全是分散的类属性 + 规则层 + 人工约定 |
| 8 | build 模式的"只读子代理" | 只能靠 `subAgent` 的 readonly 参数或 fan-out | `spawn_agent` 不支持；build 主闭环没有统一入口 |
| 9 | 计划执行期的子代理策略 | 计划实现 turn 是普通 build（可 spawn 可写） | 没有"计划实现只允许串行/只读调研"的策略表达 |
| 10 | 主闭环并行度 | 只有 `max_concurrent_tools`；子代理有自己的池 | 两套并发上限，不能统一观测/限流 |

## 5. 统一编排层：建议的抽象（只设计，不实现）

### 5.1 一个调度器，三种执行形态

```
Orchestrator
  ├ inline     : 当前 turn 的工具批次（已有 dispatcher）
  ├ child      : 一次子代理 run（同步/后台/spawn）
  └ fanout     : N 个只读 child（已有 ResearchFanOut）
```

统一输入 `TaskRequest`：

```python
TaskRequest(
  kind="inline"|"child"|"fanout",
  mode="build"|"plan"|"readonly",     # 认知姿态
  write="none"|"serial"|"isolated",  # 编辑权限与写协调
  context="task_only"|"full",        # 子代理上下文
  parallel=bool,                      # 是否允许并发
  timeout=..., parent=..., agent=...
)
```

调度器负责：
- **选形态**：inline 工具批 / 单个 child / fanout。
- **并发**：一个 pool（统一 `max_concurrent_tools` 与 `max_concurrent_subagents` 的语义，
  或至少统一观测），同一批只读任务可并发；写任务串行。
- **写协调**：`write="serial"` 进入全局写队列（同一工作区同一时间一个写者）；
  `write="isolated"` 分配 worktree，结束后 commit + merge 队列。
- **只读传播**：`mode="readonly"` 同时作用于工具面、子会话、dispatch、子代理 spawn，
  不再靠调用点各自传 `readonly`。
- **生命周期**：spawn → send → interrupt → resume → close 与同步 run 一套 API。
- **观测**：统一的 task 表（inline/child/fanout 都有 id、状态、usage、产物指针），
  前端/模型都能查。

### 5.2 两个模式怎么接入

- **build**：默认 `write="serial"`（同批写串行；跨代理写通过全局写队列或 worktree）；
  只读调研用 `kind="fanout", mode="readonly"`；可显式 `spawn_agent(write="serial")`。
- **plan (codex)**：规划 turn 固定 `mode="plan"`（只读、无写）；实现 turn 固定
  `mode="build"` + `write="serial"`；计划进度仍由 `update_plan` 表达，编排层不引入波/step。

### 5.3 复用与拆分

直接复用：`ToolDispatcher`、`SubAgentRunner`（handle/spawn/send/interrupt/resume/close）、
`ResearchFanOut`、`PermissionGate`、`WorktreeManager`、`SessionService`、`FileStateTracker`。
需要抽象：`TaskRequest`/`ExecutionPlan`（策略），一个 `Scheduler`（并发+写队列+worktree），
一个 `TaskTable`（观测）。需要拆开：`spawn_background` 里的"打开 handle"与"执行策略"；
`parallel_research` 与 `spawn_agent` 的入口合并；legacy `PlanExecutor` 的 step 编排。

## 6. 待拍板问题（不是本轮要改的代码）

1. **跨代理写**：默认全局串行写队列，还是默认 worktree 隔离 + 合并队列？两者成本不同
   （前者慢、后者需要合并冲突处理）。
2. **只读后台代理**：`spawn_agent` 是否加 `readonly` 参数，还是只允许 `fanout` 做只读？
3. **并发上限**：`max_concurrent_tools` 与 `max_concurrent_subagents` 是否合成一个
   "执行槽"概念？
4. **计划实现期**：是否允许在 "Implement the plan." 的 turn 里派可写子代理？如果允许，
   写协调必须覆盖父与子。
5. **legacy 处置**：`PlanExecutor/PlanRunner/TaskDriver` 是统一进新编排，还是直接退休？

## 7. 结论

历史改造不是"没做完"，而是**能力长在了不同的层**：并发在 dispatch、只读在 surface/gate、
子代理在 runner、计划在 executor。Codex 路径把计划执行变回普通 turn 之后，这些层之间的
约定（写串行、只读传播、并发上限、任务表）不再有单一 owner。统一编排层要做的不是重写，
而是把四个已有机制收进一个 `TaskRequest → Scheduler → TaskTable` 的策略面。
