# 方案 B 落地方案：DAG 调度 + 共享黑板（r23）

> 本文是 [plan-orchestration-design.md](plan-orchestration-design.md) 选定方案 B 之后的**实施级**设计：
> 组件、数据流、契约、逐模块影响、迁移、测试与分阶段计划。实现前先对齐这一份。

## 0. 结论摘要

- 用 **PlanOrchestrator**（新）替换 `PlanRunner` 的执行主循环；`TaskDriver` 的 13 钩子协议
  只适合串行单步，不强行复用，但保留 `PlanRunner._settle/_seed/_persist` 中可复用的部分。
- 每步仍是**短会话 + 新 seed**，但 seed 从「goal + 未完成项」升级为 **StepContextPack**：
  目标/验收/拥有文件/接口契约/黑板切片/相关文件行段/上次尝试 StepNote。
- 引入 **PlanBlackboard**（run-scoped、类型化、分角色视图、有容量上限），承担跨步一致性；
  长期知识仍写 workspace memory。
- 预算从「每回合迭代数」改为 **step token/墙钟 + episode token/墙钟**；`max_iterations=0`
  表示不限，无进展保护兜底。
- 失败语义：verify 失败 → repair → **允许一次 verify 修正** → blocked；blocked 只 skip
  依赖它的项，独立项继续；无 ready 项时触发 **一次自动重规划**（整份剩余计划 + explanation）。
- 分三阶段：B1 串行 + 黑板（先落地）、B2 只读并行、B3 写并行 + worktree 合并。

## 1. 目标与非目标

**目标**

1. 20+ 步的计划在无人工输入下连续执行到 done / deadlock / replan，不再「每完成一项就停」。
2. 跨步不重复探索：接口、决策、文件归属、坑通过黑板传递，而不是靠模型记忆。
3. 每步有证据：verify 真跑、输出入库、收据分级（verified / on-claim / fiat）。
4. 预算可解释：step 与 episode 两级，token/墙钟，到限可续跑。
5. 失败可恢复：blocked 不终止整轮、依赖项 skip、可重规划、可修正 verify。
6. 全部接入现有数据流：事件、持久化、`/resume`、boot resume、`/trace`、前端计划卡。

**非目标（本批不做）**

- 不做写并行（B3 才做）；不做跨仓库/多机编排；不引入新的 agent 框架。
- 不改变现有权限门、沙箱、压缩、记忆、技能的基本语义，只增加接入点。

## 2. 架构总览

```text
┌─────────────────────────────── frontend ───────────────────────────────┐
│ routes/session.tsx  turn.finished → /plan run（已批准）                 │
│ state/session.ts    plan.updated / plan.receipt / step.* / replan       │
│ component/message   Todos 卡（[✓][•][!][−][ ] + 新增 [~] skipped）      │
└──────────────────────────────────▲──────────────────────────────────────┘
                                   │ wire events（bridge.to_wire）
┌──────────────────────────────────┴──────────────────────────────────────┐
│ server/handlers.py  _launch_armed_run（profile=plan）→ PlanOrchestrator │
│ server/bridge.py    LoopObserver → notify                               │
└──────────────────────────────────▲──────────────────────────────────────┘
                                   │ on_event / usage
┌──────────────────────────────────┴──────────────────────────────────────┐
│ core/plan_orchestrator.py（新）                                         │
│   PlanScheduler（新）── 编译 DAG / ready queue / skip / replan 判定      │
│   BlackboardStore（新）── 读写 .ai-coding/plan/<sid>/blackboard.json     │
│   StepContextPack（新）── goal+契约+黑板切片+read ledger+技能提示         │
│   StepRunner ── 复用 AgentLoop.process_input + TurnPolicy 副本           │
│   VerifyGate ── 复用 core.acceptance.verify_items + 权限/审批            │
└───────┬──────────────┬──────────────┬──────────────┬───────────────────┘
        │              │              │              │
   AgentLoop       ToolDispatcher   Memory/Skills   Trace/Events
   TurnPolicy      PermissionGate   ReadLedger      RecordingObserver
   ContextCompress ProcessSandbox   TaskState       StepDelivery
```

## 3. 一次 `/plan run` 的端到端数据流

### 3.1 触发与启动

1. `present_plan` 批准 → `_on_plan_approved`（[agent_loop.py:265](../ai_coding/core/agent_loop.py)）
   标记 `books.plan_approved`，回合以 `completed` 结束。
2. 前端 [session.tsx:671-690](../ai_coding/frontend/src/routes/session.tsx) 发现
   `pendingPlanRun` 且 `shouldDispatchPlanRun(status)` 为真 → 静默 dispatch `/plan run`。
3. 前端命令进入后端 `session_command`；`app/commands.py:593-644` 退出 plan 模式后调用
   **当前**的 `PlanRunner(loop).run(...)`。
4. `/plan run` 的另一个入口是 boot resume：`handlers.py:673-734` 的 `_launch_armed_run`
   按 `task.armed_profile` 选 runner。
5. **改动**：`commands.py` 与 `handlers.py` 都把 `PlanRunner` 换成 `PlanOrchestrator`；
   `armed_profile` 仍为 `"plan"`，不改磁盘兼容。旧 `PlanRunner` 保留为 fallback 开关
   （`ai.plan_orchestrator: "legacy" | "dag"`），灰度期间可切回。

### 3.2 编排循环

```text
orchestrator.run(session):
 1. task = session.task；空则 taskless 返回
 2. compile(task) → DAG；拒绝环/未知依赖/越界路径；写文件冲突 → 插入串行化依赖
 3. store = BlackboardStore.load_or_create(session, task)
 4. task.armed = True；armed_profile="plan"；persist；emit TaskUpdated
 5. loop:
      budget.check()                      # episode token/墙钟
      ready = scheduler.ready(task)       # pending + deps terminal + 未 skip
      if not ready:
          scheduler.mark_skipped_descendants(task)
          if replan_allowed(): await replan(); continue
          break
      item = scheduler.pick(ready)        # 拓扑序 + 声明顺序
      result = await run_step(item)       # 单步，见 3.3
      apply(result)                       # task / blackboard / receipt / events
      if result.needs_replan: await replan()
 6. finally: task.disarm(); persist; emit TaskUpdated; report.render()
```

### 3.3 单步执行（StepRunner）

```text
run_step(item, attempt):
  step_session = 新 ephemeral SessionData（复用 plan_runner._make_step_session 的约定）
  step_session.task = parent.task        # 共享，但请求侧按 scope 过滤（见 6.4）
  pack = StepContextPack.build(parent, task, item, store, attempt_note)
  step_session.messages = [user(pack.render())]
  policy = TurnPolicy(replace(
      loop.policy.config,
      max_iterations=0,                  # 不限
      stop_policy="natural",             # 不吃 todo 门禁
      max_auto_continuations=0,
      max_turn_tokens=step_token_budget,
  ))
  loop.policy = policy；try:
      text = await loop.process_input(step_session, pack.render(), observer=...)
  finally: loop.policy = original
  usage = loop.last_outcome.usage
  note = parse_step_report(step_session, text)
  verify = await verify_gate.run(item, step_session)   # 真跑，带权限/审批
  return StepResult(note, verify, usage, text)
```

- 重试：**复用同一个 step_session**，追加 continuation prompt（带 StepNote + 失败输出），
  不新建会话，避免重探索；最多 `plan_max_step_attempts=2`。
- 无进展：`books.successful_calls == 0 && attempted_calls == 0` → stalled，立即结束该步。
- 收据：`StepDelivery` 扩展 `files_changed / wave / verify_amended / step_note`；
  父会话仍写 synthetic step head + summary，保证 `/resume` 重放。

### 3.4 验证与重试

1. 有 `verify`：`VerifyGate` 调 `core.acceptance.verify_items`（真实 subprocess），
   权限与审批走 `loop.dispatcher.approval` 与 `loop.gate.deny_reason`（与
   `AgentLoop._verify_acceptance` 同一套，见 [agent_loop.py:2449](../ai_coding/core/agent_loop.py)）。
2. 失败 → repair 回合（失败输出 + StepNote + “只修失败点”）。
3. 再失败 → 允许一次 verify 修正：模型用 `todo_write(update)` 改该项 `verify`，
   orchestrator 重新读取并重跑；收据记 `verify_amended=true`。
4. 仍失败 → `blocked`，证据落盘；依赖项标 `skipped`；独立项继续。
5. 无 `verify`：`step_report` 必须给 `files_changed` + 命令证据，否则收据 `on-claim`。

### 3.5 结束与续跑

- 结束状态：`done | deadlock | budget | no-progress | disarmed`，写 `PlanReport`。
- episode 预算到限 → `budget`；已完成项与黑板保留，`/plan run` 可从剩余项继续。
- boot resume：读取黑板与 task 状态，重建 ready queue；不重跑已完成/已 skip 项。

## 4. 契约与数据模型

### 4.1 计划项（`TodoItem` 扩展）

```python
files: list[str] = []          # 拥有/会改的文件（用于冲突串行化与上下文包）
kind: str = "write"            # write | research | verify
parallel: bool = False         # B3 才消费；B1/B2 只读项可并行
attempts: int = 0              # 已尝试次数（不落盘可接受；落盘更利于 resume）
verify_amended: bool = False
```

`to_dict` 仅在非默认时写新键；`from_dict` 缺键读默认，旧会话兼容。`skipped` 加入
`VALID_STATUSES`，`TERMINAL_STATUSES` **不包含** skipped（它不是完成，但阻断依赖）。

### 4.2 `PlanBlackboard`

```json
{
  "version": 1,
  "goal": "...",
  "decisions": [{"id": "d1", "text": "...", "by": "3", "at": "..."}],
  "files": [{"path": "app/models/room.py", "owner": "4", "interfaces": ["Room"], "status": "written"}],
  "interfaces": [{"name": "RoomRepository", "shape": "async get(id)->Room", "by": "5"}],
  "commands": {"test": "pytest -q", "lint": "ruff check ."},
  "pitfalls": [{"text": "verify 不能用 /dev/null", "by": "2"}],
  "planVersion": 1,
  "budget": {"tokens": 123456, "wallSeconds": 900}
}
```

- 存储：`.ai-coding/plan/<session_id>/blackboard.json`（workspace 内、运行时状态、原子写）。
- 上限：200 条 / 24k 字符；超限触发一次 `blackboard.compact`（模型总结 + 去重），
  或按最近使用淘汰。
- 视图：`for_step(item)` 只返回相关 files/interfaces/decisions/pitfalls（关键词 + 文件匹配），
  不把整块注入。

### 4.3 `StepContextPack`

固定顺序（便于前缀缓存与测试）：

```text
[goal]            目标（不可缩小）
[step]            item.id / content / verify / files / kind
[contracts]       上游接口与文件归属（黑板切片）
[decisions]       与本步相关的决策（黑板切片）
[pitfalls]        已知坑
[repo]            相关文件路径 + 上次读取行段（read ledger，最多 N 段）
[note]            上次尝试的 StepNote（重试时）
[skills]          按 kind/files 推荐的技能（SOPRouter 已有）
[doctrine]        只做这一步；先读后写；完成必须给证据；不要改其它项
```

预算：24k 字符；裁剪优先级 `goal > step > contracts > decisions > repo > skills > pitfalls`。

### 4.4 `StepReport`（新工具，结构化回写）

```json
{
  "status": "completed|partial|blocked",
  "files_changed": ["..."],
  "decisions": [{"text": "...", "scope": "module"}],
  "interfaces": [{"name": "...", "shape": "..."}],
  "evidence": ["pytest -q 通过", "GET /rooms 200"],
  "needs_replan": false,
  "next_hint": "..."
}
```

工具只在 step session 可用；`todo_write` 在 step session 只允许操作**本步 item**（scope 校验），
防止越权改别的步骤。

### 4.5 新事件（wire）

| 后端事件 | wire type | 前端消费 |
|---|---|---|
| `PlanCompiled` | `plan.compiled` | 计划卡头部显示「n 步 · w 波」 |
| `PlanStepStarted` | `step.started` | transcript 的 dim 行：`▸ 3/23 写 models` |
| `PlanStepFinished` | `step.finished` | dim 行：`✓ / ! / ~` + verify 摘要 |
| `PlanReplanned` | `plan.replanned` | `[replan] v2：+2 / -1 / 改写 3 项` |
| 复用 `TaskUpdated` | `plan.updated` | 计划卡状态、skipped 标记 |
| 复用 `PlanStepReceipt` | `plan.receipt` | 一行摘要（父会话气泡同源） |

### 4.6 配置（`AIConfig`）

| 键 | 默认 | 说明 |
|---|---|---|
| `plan_orchestrator` | `"dag"` | `legacy` 可切回旧 PlanRunner |
| `plan_parallelism` | `1` | B1=1；B2 只读并行；B3 才允许 >1 写 |
| `plan_step_token_budget` | `200000` | 单步 token 上限（0=不限） |
| `plan_step_wall_seconds` | `900` | 单步墙钟（0=沿用 turn_timeout） |
| `plan_token_budget` | `0` | 整个 run 的 token 上限（0=不限） |
| `plan_wall_seconds` | `0` | 整个 run 的墙钟（0=不限） |
| `plan_max_step_attempts` | `2` | 单步尝试次数（含初次） |
| `plan_max_replans` | `2` | 每次 run 的重规划次数 |
| `max_tool_iterations` | `0` | 0 = 不限；轮次不再作为步的终止条件 |

## 5. 逐模块影响清单

### 后端 domain

| 文件 | 改动 | 风险 |
|---|---|---|
| `domain/task.py` | 新增 `skipped`、`files/kind/parallel/attempts/verify_amended`；`unmet_dependencies` 把 skipped 视为「已终结但未完成」（实现方式需测试）；渲染 marker `[~]` | 中：状态机被多处读取 |
| `domain/plan_blackboard.py`（新） | 黑板模型、schema 版本、容量、视图、to/from dict | 低 |
| `domain/plan_delivery.py` | `StepDelivery` 增加 `filesChanged/wave/verifyAmended/note`，默认值兼容旧数据 | 低 |
| `domain/session.py` | 新增 `plan_run_id/plan_blackboard_path`（可选，不落盘也可）；`step_deliveries` 已存在 | 低 |
| `domain/acceptance.py` | 复用；`AcceptanceItem` 不新增字段（verify 修正通过重建 item） | 低 |

### 后端 core

| 文件 | 改动 | 风险 |
|---|---|---|
| `core/plan_orchestrator.py`（新） | 编排主循环、预算、重试、收据 | 高（核心） |
| `core/plan_scheduler.py`（新） | DAG 编译/校验、ready queue、skip 传播、deadlock、replan 触发 | 中 |
| `core/plan_context.py`（新） | StepContextPack 构建与裁剪 | 中 |
| `core/plan_blackboard.py`（新） | 读写、原子落盘、容量整理、视图 | 低 |
| `core/plan_runner.py` | 保留旧 profile；抽出 `_make_step_session/_settle/_persist` 供新编排器复用，避免复制 | 中 |
| `core/task_driver.py` | 不改协议；新编排器不实现 RoundProfile（DAG 不是串行协议） | 低 |
| `core/turn_policy.py` | `max_iterations=0` → 不限；`budget_pressure` 跳过；`from_ai_config` 不再 `max(1,…)` | 中：影响所有回合 |
| `core/agent_loop.py` | 允许 `max_iterations=0`；新增 public `verify_contracts(session)`（从 `_verify_acceptance` 拆出）；`_begin_turn` 支持 session 级 `turn_timeout_seconds` 覆盖 | 中 |
| `core/request_assembly.py` | 新增 `blackboard` volatile block（可选）；step session 的 task 按 scope 过滤；`task_injection` 对 step 用 system 或 message 需定 | 中 |
| `core/acceptance.py` | 不改；供 VerifyGate 调用 | 低 |
| `core/goal_runner.py` | 不改；作为 `armed_profile=goal` 的旁路 | 低 |
| `core/subagent.py` / `core/worktree.py` | B2 只读并行复用；B3 需要新协议（见 §8） | 高（B3） |

### 工具与安全

| 文件 | 改动 |
|---|---|
| `tools/step_report_tool.py`（新） | 结构化验收入参、写 StepNote、拒绝越界字段 |
| `tools/todo_write_tool.py` | 支持 `skipped`；step session 限制只能改本步 item（通过 `ToolContext.planStepScope`） |
| `tools/present_plan_tool.py` | 计划项支持 `files/kind/parallel`；编译期校验（环/未知依赖）在 orchestrator 做 |
| `tools/builder.py` | step session 的工具栈：去掉 `present_plan`；`todo_write` 加 scope；`step_report` 加入 |
| `core/dispatch.py` / `security/permission_gate.py` | 不改；verify 命令仍走 `deny_reason` + approval |
| `security/process_sandbox.py` | B1 主工作区；B3 worktree 作为 bash 根 | 

### server / wire

| 文件 | 改动 |
|---|---|
| `server/handlers.py:673-734` | profile 选择 `PlanOrchestrator`；boot resume 读黑板 |
| `server/handlers.py:655-651` 附近 | 保持 `/plan run` 命令路径；新增 run 终态事件 |
| `server/bridge.py:167-189, 247-251` | `TaskUpdated` 增加新字段；新事件映射；`turn.step` 语义不变 |
| `core/events.py` | 新增事件 dataclass；`RecordingObserver` 自动记录 |

### 前端

| 文件 | 改动 |
|---|---|
| `state/session.ts:892-940` | `applyPlanUpdate` 读 `files/kind/attempts/verifyAmended`；新增 `skipped` 渲染 |
| `state/session.ts:674-695` | 消费 `step.started/step.finished/plan.replanned` |
| `component/message.tsx:107-158` | plan 卡 marker/tone 增加 `[~]` skipped；显示 verify 修正标记 |
| `routes/session.tsx:671-690` | 不改 dispatch；可加 replan 提示条 |
| `util/plan-run.ts` | 不变（已允许 max_iterations） |
| `state/replay.ts` | 步骤 header 已存在；新增 step Note/收据字段读取的兼容 |

### 配置 / 文档 / 测试

| 文件 | 改动 |
|---|---|
| `config/models.py` | 新增 §4.6 配置；`max_tool_iterations` 默认 0 |
| `app/commands.py:593-644` | `/plan run` 调新编排器；保留 `--legacy` 开关 |
| `docs/data-contracts.md` / `docs/features.md` | 新契约、默认值、事件 |
| `tests/` | 新增 scheduler/blackboard/context/replan/verify-amend 测试；改 `test_plan_*` |
| `frontend/test/` | reducer 新事件、skipped 渲染、replan 提示 |

## 6. 与现有子系统的接入

### 6.1 上下文压缩

- 每步是短会话，通常不需要压缩；若单步很长，仍走 `AgentLoop` 的 L1–L4。
- **StepNote 在压缩前写**（编排器在步结束后立即落盘），压缩只影响步内历史，不影响跨步状态。
- `max_context_tokens` 不变；step 的 token 预算用 `TurnPolicyConfig.max_turn_tokens` 实现。

### 6.2 记忆

- 黑板 = run-scoped 工作记忆；步骤结束把 `decisions/interfaces/pitfalls` 以 `memory_write`
  的语义写入 workspace memory（去重、限条数）。
- 步内上下文包不足时，模型仍可用 `memory_search`；不要把整块黑板塞进 memory。

### 6.3 技能

- `SOPRouter`/`SkillRegistry` 已有「按 signal 推荐技能」的尾部块（`request_assembly.py:395-416`）；
  步骤 seed 里加 `kind/files` 标签，复用同一推荐逻辑，或直接 `@skill` 注入。
- 步骤结束时若产生了新的可复用流程，走现有 `skill_review` 机制，不自动写技能。

### 6.4 工具 / 权限 / 沙箱

- step 工具栈：`read/glob/grep/bash/write/edit/memory_search/todo_write(scope)/step_report/subAgent/skill/mcp`，
  禁用 `present_plan`。
- `todo_write` 在 step session 只允许 `update` 本步 item 的 `status/evidence/verify`；
  其它 id 直接拒绝并回结构化错误。
- verify 命令绕过 dispatch，因此必须走 `verify_items` 的 `deny_reason` + `approval`；
  orchestrator 通过 `AgentLoop.verify_contracts()` 复用同一套规则。
- B1 用主工作区；B3 worktree 需要把 `workspace` 传给 verify 与 bash 沙箱。

### 6.5 子代理 / worktree

- B2 只读并行：`research` 类步骤用现有 `subAgent`（task_only 上下文、结论回传），
  不产生写冲突。
- B3 写并行：现有 worktree 是 `temporary()` 用完即删、分支不合并（`core/worktree.py:129`），
  需要新增：worker commit → 返回 branch/patch → 合并队列 → 冲突回交 lead。
  **默认关闭**，只在计划项显式 `parallel: true` 且 `plan_parallelism>1` 时启用。

### 6.6 Harness

- 波次结束：`StaticCheckConfig`（ruff 等）跑一次，结果写入黑板 `pitfalls/commands`；
- 集成阶段：项目声明的 test/lint 命令（从计划 `commands` 或项目配置取）；
- `git diff --stat` 写入 StepDelivery；跨步改动文件列表用于冲突检测。

### 6.7 Trace / 导出

- 新事件全部经 `LoopObserver`（`bridge` / `RecordingObserver`），沿用现有 span 命名
  （`turn/step/tool`），新增 `plan.wave`、`plan.replan` 维度。
- `/trace` 汇总新增 `waves/replans/blocked/skipped`；`/export html|otlp` 不改格式，
  只多出这些 span/属性。

### 6.8 预算与超时

- step 内：`max_iterations=0` + `max_turn_tokens=plan_step_token_budget` +
  `session.turn_timeout_seconds=plan_step_wall_seconds`（需 AgentLoop 支持 session 覆盖）。
- run 内：累计 `loop.last_outcome.usage`；`plan_token_budget/plan_wall_seconds`。
- 到限写出 StepNote，允许下一次 `/plan run` 续跑；不把「到限」等同于失败。

### 6.9 持久化 / resume / boot

- 父 session：`task`（状态/attempts/skipped）、`step_deliveries`（收据）、synthetic step 气泡。
- 黑板：`.ai-coding/plan/<sid>/blackboard.json`（原子写，含 `planVersion/runId`）。
- `/resume`：读 task + receipts 重建计划卡与步骤时间线；黑板按需加载。
- boot resume：`_launch_armed_run` 发现 `armed_profile=plan` 且有黑板 → 新编排器接着跑；
  旧会话无黑板 → 旧 PlanRunner fallback，避免升级后打不开。

## 7. 兼容与迁移

1. `skipped` 是新增状态：旧前端不认识会落到默认 `[ ]`；需要前后端同版本发布，
   或先把未知状态映射为 `[!]` 兜底（建议后者，兼容窗口更稳）。
2. 旧计划项没有 `files/kind`：`compile` 默认 `kind=write`、`files=[]`，串行执行，不阻断。
3. 旧收据没有新字段：`from_dict` 读默认（attempts=1、wave=0、verifyAmended=False）。
4. `max_tool_iterations=0` 是行为变更：旧配置写 10/100 的仍按显式值生效；默认配置需要
   在 `/config` 里明示「0=不限」。
5. `plan_orchestrator` 开关：`dag` 默认，`legacy` 可一键回滚；boot resume 也遵守。
6. 磁盘上已有的 `armed` 任务：升级后由 boot scan 选择 profile；无黑板走 legacy。

## 8. 分阶段实施

### B1：串行 DAG + 黑板（先落地）

**范围**：调度器、黑板、上下文包、step_report、verify 修正、skip 传播、episode 预算、一次 replan。

**改动顺序**

1. `domain/task.py` 新状态/字段 + 测试；`domain/plan_blackboard.py` + 测试。
2. `core/plan_scheduler.py`（纯函数优先：compile/ready/skip/deadlock）+ 单测。
3. `core/plan_context.py`（纯函数 + 容量裁剪）+ 单测。
4. `core/plan_orchestrator.py`：先用假 loop 跑通 3 步/依赖/阻塞/预算；再接真 `AgentLoop`。
5. `tools/step_report_tool.py` + `todo_write` scope + step 工具栈。
6. `config` 新键 + `turn_policy` 0=不限 + `agent_loop` timeout 覆盖。
7. `commands.py` / `handlers.py` 切换 + legacy 开关。
8. 事件与前端 reducer/计划卡；trace 字段。
9. 端到端：3 步小计划、23 步酒店计划（离线录像/假模型）、verify 写错、阻塞依赖、预算切断续跑。

**验收**：§0 的 1–6 条；全量测试绿；旧 `test_plan_*` 语义不变（legacy 路径）。

### B2：只读并行

- 计划项 `kind=research` 的步骤用 `asyncio.gather` 跑多个 subagent（仍走 `max_concurrent_subagents`）。
- 写步骤串行；黑板的 `decisions/interfaces` 由并行研究结果合并（按 step id 去重）。
- 验收：并行研究不改变写顺序；失败隔离；token 预算仍有效。

### B3：写并行 + 合并队列

- worktree 持久化：worker commit 到 `ai_sub_*` 分支，返回 branch + diff stat + 自验结果。
- 合并队列：按 DAG 拓扑顺序合并；冲突 → 交回 lead（新 `resolve_conflict` 步骤）。
- 集成验证：合并后跑全量 test/lint + fresh reviewer（可选 subagent）。
- 只有 `parallel: true` 且文件所有权不冲突的项才并行；默认关闭。

## 9. 测试矩阵

| 层 | 用例 |
|---|---|
| `plan_scheduler` | 环/未知依赖/自依赖拒绝；ready 顺序；skip 传递；deadlock；file 冲突插边 |
| `plan_context` | 切片相关性；24k 裁剪优先级；重试带 StepNote；技能提示 |
| `plan_blackboard` | 原子写/读；版本；容量整理；角色视图不泄漏无关条目 |
| `plan_orchestrator` | 3 步串行；阻塞不终止；预算到限续跑；verify 修正一次；replan 上限 |
| `agent_loop` | `max_iterations=0` 不限；step token/timeout 覆盖；verify_contracts 权限 |
| `tools` | step_report 校验；todo_write scope 拒绝越权；present_plan 在 step 不可用 |
| `server/bridge` | 新事件 wire 形状；plan.updated 新字段；turn.step 不变 |
| 前端 | reducer 新事件；skipped marker；replan 提示；replay 兼容 |
| 端到端 | 23 步假模型录像；真实小项目；`/resume`；boot resume；`/trace` 导出 |

## 10. 风险与回滚

| 风险 | 缓解 / 回滚 |
|---|---|
| 编排器替换导致旧行为回归 | `plan_orchestrator=legacy` 开关；旧 `test_plan_*` 全保留 |
| `max_iterations=0` 失控 | step token/墙钟 + episode 预算 + 无进展保护；默认仍可设上限 |
| 黑板膨胀/污染 | 容量上限 + 整理 + 角色视图；只写结构化字段 |
| verify 修正被滥用 | 每步最多一次；修正必须真跑；收据留痕 |
| 自动 replan 失控 | 最多 2 次；不得缩小 goal；版本化 + 差异事件 |
| 并行写冲突（B3） | 默认关闭；文件所有权 + 合并队列 + 冲突步骤 |
| 前端/后端版本错位 | 未知状态兜底映射；事件字段可选；先发后端再发前端 |

## 11. 外部实现对照（对设计的直接影响）

| 参考 | 事实 | 对 B 的影响 |
|---|---|---|
| Codex `update_plan` | `{explanation?, plan:[{step,status}]}`，prompt 要求恰好一个 `in_progress`；服务端不做唯一性校验，TUI 自己排序渲染 | 我们的调度器**拥有**状态机，不把唯一性交给模型；replan 提交整份剩余计划 + explanation |
| Claude Code TodoWrite/Task | 任务清单是执行期进度，不等于计划批准；批准后可选自动/手动执行并对照计划验证 | 计划卡与执行状态分离：`TodoItem` 是契约，收据是证据，UI 由事件派生 |
| opencode | `steps` 默认不限；compaction 保留约 15k 最近上下文；无 DAG 依赖 | 迭代不设限是对的；但需要我们的黑板补跨步一致性 |
| LLMCompiler | Planner 输出 `$id=tool(args)`，变量引用即依赖；ready queue + 并行执行 + 结果按 id 回填 | ready queue 与「依赖满足才入队」直接采用；回填目标是黑板字段，不是聊天 |
| AdaPlanner / DeepAgent | 观测偏离计划 → 重写整份计划并从当前状态续跑；Memory Fold 压缩长历史 | replan 触发条件与「从 checkpoint 续跑」；StepNote 就是 Memory Fold 的落点 |
| bMAS / SE-Blackboard | 黑板分公共/私有区，按角色给 `get_state_for_agent` 视图；cleaner 控膨胀 | `blackboard.for_step(item)` 角色视图 + 容量整理 |
| Aider repo map / Anthropic note-taking | 符号图 + PageRank 裁到 token 预算；结构化笔记按需拉回 | StepContextPack 的 `[repo]` 段只放相关文件/行段，不塞全仓 |

来源：见 [plan-orchestration-design.md](plan-orchestration-design.md#7-参考来源)；
补充：
- [Codex plan_spec.rs](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/handlers/plan_spec.rs)
- [Codex plan prompt](https://github.com/openai/codex/blob/main/codex-rs/core/gpt_5_2_prompt.md)
- [LLMCompiler](https://arxiv.org/html/2312.04511v3)
- [AdaPlanner](https://arxiv.org/abs/2305.16653)
- [DeepAgent](https://arxiv.org/html/2510.21618v2)
- [bMAS blackboard](https://arxiv.org/pdf/2507.01701)
- [Aider repo map](https://aider.chat/docs/repomap.html)
- [Anthropic context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)

## 12. 已确认的设计决策

| # | 问题 | 决策 | 落地含义 |
|---|---|---|---|
| 1 | 上游 blocked 后依赖项的状态 | **新增 `skipped`** | `TodoStatus` 增加 `skipped`，`TERMINAL_STATUSES` 同步；渲染 `[~]`；与 `dropped`（用户删除）语义分开，收据区分 `skipped-by-dependency` |
| 2 | step session 的计划视图 | **只看本步 + 依赖** | 请求里的 task 块由 step scope 过滤：本步 + 直接/传递依赖 + 本轮新完成的收据摘要；完整计划仍在父会话与计划卡 |
| 3 | 黑板放置 | **`.ai-coding/plan/<sid>/blackboard.json`**（推荐即默认） | 随工作区清理；原子写 + `version` 字段；不进 git，不写用户配置目录 |
| 4 | replan 触发 | **deadlock + `needs_replan`** | `StepReport.needs_replan` 也可触发，但受 `plan_max_replans=2` 约束；replan 不得缩小 goal |
| 5 | 旧 PlanRunner | **保留 legacy 开关** | `plan_orchestrator="dag"`（默认）/`"legacy"`；旧 `test_plan_*` 与行为全保留 |
| 6 | verify 写错 | **允许自动修正一次** | 首次 verify 失败进入 repair；repair 后仍失败，允许改一次 `verify`（必须真跑），再失败 → blocked |

决策 1 带来的连带修改：`domain/task.py` 的 `_MARKERS`、`TERMINAL_STATUSES`、`open_items`（skipped
不算 open）、`dependency_invariant`（skipped 视为已终结但保留依赖边）；`todo_write` 的
`VALID_STATUSES` 与前端 `marker()/tone()` 同步；`PlanReport` 统计新增 `skipped` 计数。

决策 2 带来的连带修改：`StepContextPack` 负责生成过滤后的 task 视图；`todo_write` 在 step
会话里只允许改本步相关项（scope 校验），越权返回错误而不是静默接受。
