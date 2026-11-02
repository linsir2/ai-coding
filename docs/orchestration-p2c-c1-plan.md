# P2-C1 实施方案：统一编排契约、Registry 与执行槽

> 前置：P1、P2-A（`ef81377`）、P2-B（`5a1a61c`）已完成；P2-C 总体方案见
> `docs/orchestration-p2c-plan.md`（已过三轮评审）。
> 本文件只做 C1：`TaskRequest` + `Scheduler` + `TaskRegistry` + `ExecutionSlotPool`
> 与工具迁移。C2 的 journal/租约/恢复、C3 的合并队列不在本轮。

## 0. 一句话目标

把“三套派发入口 / 三套容量 / 三套读口”收成 **一个调度门面 + 一个槽池 + 一个只读
Registry**，且**不改变现有 build/plan/readonly 行为**、不引入第二套执行内核。

## 1. 现状与要解决的问题（事实）

| # | 现状 | 证据 |
|---|---|---|
| 1 | 工具直接持有 runner：`SubAgentTool._runner`、`SpawnAgentTool._runner`、控制类工具 | `tools/subagent_tool.py:132-141,288-382`、`tools/agents_tool.py:58-66,155-229,310-453` |
| 2 | 三套容量：`max_concurrent_tools`（tool 组信号量）、`max_concurrent_subagents`（`_active`+semaphore）、`MAX_RUNNING_JOBS` | `core/dispatch.py:292-300`、`core/subagent.py:250,268,375-378,1153-1178`、`core/jobs.py:295` |
| 3 | 子代理内部 loop 的工具组仍会再申请同一 runner 的槽 → `slots` 小的时候父子互等风险 | `core/subagent.py:375-378`、`tools/builder.py:106-127`、`core/agent_loop.py:501-505` |
| 4 | 三个读口字段不一致：`/tasks`、`list_agents`、`subagent_list` | `app/commands.py:1120-1200`、`tools/agents_tool.py:320-377`、`tools/subagent_tool.py:444-534` |
| 5 | sync/fanout 没有 `_background` 行；完成后无处可查 | `core/subagent.py:1590-1600`、`core/research_fanout.py:154-174` |
| 6 | 控制类工具 `wait/list` 在工具面里是 barrier/并行组，会占工具槽 | `tools/agents_tool.py:255,324`、`tools/subagent_tool.py:449` |
| 7 | P2-B 的 `_active` 只覆盖子代理，工具组另有一套上限，无法统一观测 | `core/subagent.py:1153-1178`、`core/dispatch.py:207,292` |

## 2. 设计决策（C1 版）

### 2.1 `TaskRequest`（只做 child；其余延后）

```
TaskRequest(
  id: str,                 # uuid；Registry 唯一键
  kind: "child",           # C1 只登记 child；fanout 用 question id 单独投影
  task: SubagentTask,      # P2-A 载荷（brief/context/agent/readonly/isolate/schema）
  name: str,
  origin_session: str,
  parent_span: str,
)
```

- **不加** `idempotency_key/timeout_s`：C1 无 journal、无跨重启去重、runner 已有超时；
  加了没有消费者。C2 再加法加入。
- **不加** `parent/observer/emit/worktree_manager`：它们是一次调用的通道，由
  Scheduler 方法参数传入。
- 不复制 `SubagentTask` 的字段；`TaskRequest.render()` 直接委托 `task.render()`。

### 2.2 `Scheduler`（门面，不造第二套执行内核）

```
Scheduler(runner, fanout, worktree_manager, slots, registry)
  run(request, *, parent, observer, emit) -> SubagentResult
  spawn(request, *, parent, observer, emit) -> (request_id, run_id)
  fanout(questions, shared_context, *, parent, observer, parent_span) -> packets
  send(target,text) / interrupt(target) / resume(target) / close(target)
  wait(target,timeout_ms) / close_info(target)
  resolve_target(target) / resolve_run_id(prefix)
  stop(target)                 # /tasks stop：runner.cancel
  task_list() / tasks()        # Registry
```

- `run/spawn` 把 `TaskRequest` 的字段喂给现有 `SubAgentRunner.run/spawn_background`；
  **执行仍由 runner**，Scheduler 只做请求身份、准入、登记与门面。
- `fanout` 仍调用现有 `ResearchFanOut.run(questions, ...)`；Scheduler 只按 question
  顺序把 `ResearchPacket` 登记成 `TaskView(kind="fanout")`，不复制 brief/report 逻辑。
- 控制类全部委托 runner 现成方法；`/tasks stop` 用 `runner.cancel`，与
  `interrupt` 区分。
- 工具迁移后不再持有 runner；`AgentLoop` 暴露 `scheduler`，commands 从 loop 取。

### 2.3 `TaskRegistry`（只读视图，不新增表）

```
TaskView(
  id, run_id, name, kind, status, started, ended,
  usage, ledger_path, branch, commit, changed_paths,
  conflicts, commit_error, degraded, error,
  origin_session,
)
list()/get(target)/to_dict()
```

- 来源：`_handles`（真源）、`_background`（投影）、Scheduler 内存 `request_id→TaskView`
  （sync/fanout 最终态，最多保留 100 条）、`_active`（在途）。
- 字段只放**渲染消费**的：三个读口目前展示的 id/name/status/耗时/usage/ledger/
  branch/commit/改动/冲突/失败/降级/error；`agent/readonly/isolation` 不展示则不存。
- C1 无 journal，故**没有** `needs_reconciliation` 字段（C2 加）。
- 三个读口各自格式化，但数据来自 `TaskRegistry.list()`；camelCase 兼容层保留。
  `/tasks stop` 仍按 run_id（C1 未引入 request_id 停止）。

### 2.4 `ExecutionSlotPool`（一个池，叶子执行单元）

```
ExecutionSlotPool(max_slots)
  async acquire(reason) -> Slot           # run/工具组，排队
  try_reserve(reason) -> Slot|None        # spawn，满则 None
  release(slot)                           # 幂等
  held()/enter_child() contextvar         # 嵌套不再申请
```

- 配置 `ai.max_execution_slots`；若未显式设置（`model_fields_set`），取
  `max_concurrent_subagents`（默认 4）。C4 再做别名/删除。
- 槽单位：**child run / fanout child / 一个并行工具组**；模型步不占槽。
- **child 持槽，嵌套不申请**：`Scheduler.run` 内由 pool 包住 runner 调用，设置
  `slot_held`；子 loop 的工具组看到 held 直接执行。后台 `_supervised`、follow-up
  `_run_followup` 同样在任务内设置；`spawn` 用 `try_reserve` 预占（满则
  `SubAgentPoolFull`），`_supervised` 不重复申请。
- 工具组：dispatcher 对 `occupies_slot=True` 的组 acquire 1 槽；组内并发上限仍是
  `max_execution_slots`（保留一个有界并发，不引入新配置）。
- **编排/控制工具不占槽**：`BaseTool.occupies_slot=False`，覆盖
  `subAgent/spawn_agent/parallel_research/send_message/interrupt_agent/resume_agent/
  close_agent/wait_agent/list_agents/subagent_list`。dispatcher 分组键增加
  `occupies_slot`，编排组不申请槽。
- `_active` + runner semaphore 合并进池：保留 `_admit/try_admit/_release_slot` 的
  调用点语义，内部改为池的 child key；semaphore 删除，避免双重计数。
- 池满：spawn 拒绝（`SubAgentPoolFull`）；sync run 排队；工具组排队。

## 3. 数据流（C1 后的样子）

```
AgentLoop._run_turn
  └─ dispatcher.dispatch_batch
        ├─ 普通工具组：slots.acquire → gather（组内 cap=max_slots）→ release
        ├─ 编排组（subAgent/spawn/wait/list...）：不取槽
        └─ 每个 call 仍走 permission/guard/claim/write-ahead（不变）

SubAgentTool.execute
  → TaskRequest(id, task=SubagentTask, name, origin, parent_span)
  → Scheduler.run(request, parent, observer, emit)
      → slots.acquire(child id) → slot_held=True
      → runner.run(str|SubagentTask, ...)（内部不再申请槽）
      → registry.register(TaskView)
      → release
  → ToolResult(data.packet 等，不变)

SpawnAgentTool.execute
  → TaskRequest → Scheduler.spawn
      → slots.try_reserve → 满则 SubAgentPoolFull
      → runner.spawn_background → (request_id, run_id) → registry
  → 后台完成 → runner 现有 notice/event/details（不变）

ParallelResearchTool
  → Scheduler.fanout(questions, ...)
      → ResearchFanOut.run（每子 runner.run，各占 1 槽）
      → registry 按 question id 登记 fanout view

/tasks、list_agents、subagent_list
  → loop.scheduler.tasks().list() → 各自格式化（同源）
```

**成功后的效果**：一个 `max_execution_slots` 同时约束工具组、child、fanout；父等待子
不持槽，`slots=1` 不互等；三读口字段同源；工具只依赖 Scheduler；行为（packet/事件/
通知/写协调）与 P2-A/B 一致。

## 4. 逐文件改动与步骤

### 4.1 新文件

1. `ai_coding/domain/orchestration.py`
   - `TaskRequest`（§2.1）、`to_dict`（Registry/debug 用）、`request_id()` 工厂。
   - 只依赖 `domain/subagent.py` 的 `SubagentTask`。
2. `ai_coding/core/execution_slots.py`
   - `ExecutionSlotPool`、`Slot`、`slot_held` contextvar 与 `enter_child()`。
   - `acquire/try_reserve/release`；`release` 幂等；`max_slots>=1`。
3. `ai_coding/core/task_registry.py`
   - `TaskView`、`TaskRegistry(runner, scheduler)`：`refresh()` 从 handles/background
     + scheduler 内存构造；`list()/get()`；`to_dict` camelCase。
4. `ai_coding/core/scheduler.py`
   - 门面（§2.2）；`run/spawn/fanout/control/task_list`；持有 slots/registry。
   - `spawn` 预占槽；`run` 由 pool 包住；fanout 由 fanout 自己占子槽。

### 4.2 修改文件

| 文件 | 改动 | 作用 |
|---|---|---|
| `tools/base.py` | `occupies_slot: bool = True` | 编排/控制工具声明不占槽 |
| `core/dispatch.py` | `_concurrent_groups` 分组键加 `occupies_slot`；组执行用 `slots.acquire` 替代 `asyncio.Semaphore`；组内 cap=`max_execution_slots` | 工具组统一进池 |
| `core/agent_loop.py` | 构造 dispatcher 传 `slots`（来自 registry）；暴露 `scheduler` 属性；dispatcher 组内 cap 用配置 | 主/子 loop 共享池 |
| `core/subagent.py` | `_active`/semaphore 改为 pooling child key；`run`/`_supervised`/`_run_followup` 设置 `slot_held`；`_admit/try_admit/_release_slot` 委托池 | 单一容量 |
| `tools/subagent_tool.py` | 构造收 scheduler；`execute` 建 TaskRequest；`SubAgentListTool` 用 registry | 去 runner 依赖 |
| `tools/agents_tool.py` | 控制类收 scheduler；`spawn` 建 TaskRequest；`wait/list/close` 委托；`occupies_slot=False` | 去 runner 依赖 |
| `tools/parallel_research_tool.py` | 收 scheduler，调 `scheduler.fanout` | 统一入口 |
| `tools/builder.py` | 创建 slot pool + scheduler；先建 runner/manager/fanout，再建 scheduler；工具注册改传 scheduler；registry 挂 `execution_slots/scheduler` | 组装 |
| `cli.py`、`server/world.py` | 无新增参数（builder 内部组装）；确保每个 world/registry 一套 | 组装一致 |
| `config/models.py` | `max_execution_slots`（默认 0=未设置）；读取回退 `max_concurrent_subagents` | 配置 |
| `config.example.yaml` | 加 `max_execution_slots: 4` 注释 | 文档 |
| `app/commands.py` | `/tasks` 从 `loop.scheduler.tasks()` 取；`_runner_of` 删除 | 读口同源 |
| `core/research_fanout.py` | 不改执行；Scheduler 只消费返回的 packets | 复用 |
| `server/bridge.py`、前端 | C1 不改 wire；`BackgroundTaskDone` 形状不变 | 无回归 |

### 4.3 明确不做

- 不加 journal/claim/lease/fence（C2）。
- 不加 merge（C3）。
- `TaskRequest` 不加 idempotency/timeout。
- 不改 packet v3/notice/details/claim 键。
- 不把 fanout 改成 TaskRequest 驱动（保留 ResearchFanOut 的 brief/report）。

## 5. RIPR 用例设计（先红后绿）

### R（先写失败用例）

| # | 文件 | 用例 | 期望 |
|---|---|---|---|
| R1 | `tests/test_execution_slots.py`（新） | `slots=1` 父跑 `subAgent`：父不持槽，子内部工具直接跑 | 不超时/不死锁 |
| R2 | 同上 | 工具组占槽、编排调用同时进行不占槽 | 组计数=1，编排=0 |
| R3 | 同上 | `spawn` 满池拒绝、`run` 排队 | `SubAgentPoolFull`/等待后拿到 |
| R4 | 同上 | child 内部再申请（模拟嵌套） | `slot_held` 使其跳过 |
| R5 | `tests/test_task_registry.py`（新） | sync、background、fanout 三种 view | 三读口字段同源 |
| R6 | 同上 | 三读口格式化不丢现有字段（camelCase） | `list_agents.data`/`subagent_list.data`/`/tasks` 行 |
| R7 | `tests/test_scheduler.py`（新） | `run/spawn/fanout/control/wait/stop` 全走门面 | 参数/返回值/事件不变 |
| R8 | 同上 | `spawn` 返回 request_id+run_id；wait 能解析 | 不 unknown |
| R9 | 同上 | emit/parent/observer 透传 | 后台事件/通知不丢 |
| R10 | `tests/test_task_request.py`（新） | `TaskRequest` 只包 SubagentTask；`render` 委托；无多余字段 | 结构/行为 |
| R11 | `tests/test_tool_subagent.py`/`test_agents_tools.py` | 工具构造只收 scheduler，不再有 `_runner` | 属性不存在/仍可用 |
| R12 | `tests/test_builder.py` | 父/子 registry 共享同一 slot pool/scheduler；子栈仍无编排工具 | 同一对象 |
| R13 | `tests/test_mode_gate.py` | readonly/plan 拒绝码不变 | `readonly_denied`/`denied` |
| R14 | `tests/test_background_tasks.py` | 完成事件/notice/details 形状不变 | 回归 |

### I（实现）

按 §4.1→§4.2 顺序：契约 → 槽池 → Registry → Scheduler → 工具/loop/builder 迁移。
每步只让对应用例转绿；旧调用兼容（runner 仍接受 `str|SubagentTask`）。

### P（验证）

- `pytest` 全量 0 failed；`ruff`/`mypy`；前端 `bun test` + `tsc`。
- 突变检查：
  - 去掉 `occupies_slot=False` → R2 红；
  - 去掉 `slot_held` → R1/R4 红；
  - Registry 少一个字段 → R5/R6 红；
  - Scheduler 不传 emit → R9 红。
- `git status` 只含预期文件。

## 6. 架构图（C1 后）

```
                     ┌──────────────── AgentLoop ────────────────┐
                     │ dispatcher (slot for tool groups)         │
                     │   └ permission/guard/claim/write-ahead    │
                     └───────────────┬───────────────────────────┘
                                     │ tools
                     ┌───────────────▼───────────────────────────┐
                     │ Scheduler (run/spawn/fanout/control)       │
                     │  ├ ExecutionSlotPool (one max_execution)   │
                     │  ├ TaskRegistry (read-only view)           │
                     │  └ SubAgentRunner (unchanged executor)     │
                     └───────────────┬───────────────────────────┘
                                     │
               handles / background / active / scheduler views
                                     │
                     /tasks · list_agents · subagent_list
```

## 7. 风险与核验

1. **槽嵌套**：`slot_held` 只在 child run 执行 task 内设置；子内部工具组继承；
   工具组不设置。R1/R4 钉住。
2. **双重计数**：`_active`/semaphore 必须全部迁到池；R3/R13 钉住。
3. **编排不占槽**：`occupies_slot=False` + 分组键；subAgent 仍并行（同键组不取槽），
   但不再与读组混在一起。
4. **读口兼容**：camelCase 与事件/前端不动；R6/R14 钉住。
5. **fanout 不复刻**：保留 ResearchFanOut；Registry 只登记 view。
6. **配置回退**：未显式设置 slots 时取 `max_concurrent_subagents`；R3/R12 钉住。
7. **循环依赖**：Scheduler 持有 runner/fanout/registry；registry 只读 runner；
   构建顺序固定，不在 import 期互相引用。

## 8. 实施记录（完成）

- **契约**：`domain/orchestration.py::TaskRequest`（id/task/name/kind/origin_session/
  parent_span；`render` 委托 `SubagentTask`；`for_child` 生成 id）。
- **槽池**：`core/execution_slots.py`（`acquire/try_reserve/release`、`slot_held`
  contextvar、`enter_child/holding_slot`）。
- **Registry**：`core/task_registry.py`（`TaskView` + `TaskRegistry`，合并
  `_background` 投影与 scheduler 内存视图，camelCase `to_dict`，settled 上限 100）。
- **Scheduler**：`core/scheduler.py`（run/spawn/fanout/控制类/wait/stop/close_info/
  resolve/tasks；spawn 返回 `(request_id, run_id)`）。
- **dispatcher**：分组键加 `occupies_slot`；普通组/单调用取 1 槽，编排组不取；
  `slots=None` 时退回旧 Semaphore。
- **runner**：`_active`/semaphore 迁到共享池；`run`/`_supervised`/`_run_followup`
  用 `enter_child()`，嵌套不再申请；所有终态释放点保留。
- **工具**：`subAgent/spawn_agent/parallel_research/控制类/list` 改走 Scheduler；
  `occupies_slot=False`。
- **配置**：`ai.max_execution_slots`（0=未设置，回退 `max_concurrent_subagents`）。
- **读口**：`/tasks`、`list_agents`、`subagent_list` 从 `TaskRegistry.list()` 格式化；
  `/tasks` 标题改为“sub-agent runs（后台与本进程子代理）”，如实反映范围。
- **校验**：全量 `pytest` exit 0（28 skipped 为 bwrap 不可用）；`ruff` 通过；
  `mypy` 179 文件。
- **tmux 真机（qwen3.7-flash，`max_execution_slots=1`）**：
  | 场景 | 结果 |
  |---|---|
  | 同步 `subAgent`（读 + 写文件） | COMPLETED，10 步，结束时槽 0；子分支记录 `c1_child.txt`，无父子互等 |
  | 后台 `spawn_agent` + `wait_agent` | COMPLETED，8 步，结束时槽 0；registry 视图 completed |
- **已知边界**：`/tasks` 现在也会列出同步/fanout 子代理（同源读口的直接结果）；
  `stop` 只对后台 run 生效，对同步视图提示 “not running”。
- **未做（按 C1 范围）**：journal/租约/fence（C2）、合并队列（C3）。

## 9. 成功后的最终模样

- 一个 `ai.max_execution_slots` 控制工具组/子代理/fanout；`slots=1` 下父执行子代理
  不互等，子内部工具照常跑。
- `subAgent`/`spawn_agent`/`parallel_research`/控制类工具只依赖 Scheduler；
  不再各自持有 runner。
- `/tasks`、`list_agents`、`subagent_list` 数据同源，现字段/格式不丢。
- P2-A packet、P2-B claim/hold、notice/details、plan/readonly 行为全部不变。
- C2 可在 Scheduler 上直接加 journal/claim/lease/fence，无需再动工具。
