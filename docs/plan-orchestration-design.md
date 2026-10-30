# 计划编排层重设计（r23 提案）

> 目的：把「plan 出来 → 执行」从**每步重开短会话 + 固定迭代预算 + 受阻即停**，改成
> 一个**有意图的编排层**：计划是活的契约，执行有连续性，验证有证据，失败能重规划。
> 本文只做设计选型，不含实现；四个方案末尾给出对比与推荐路线。

## 1. 现状与实测

当前链路：

```text
/plan <topic>（plan 模式，只读）
  → present_plan（计划卡片）
  → 批准
  → PlanRunner（TaskDriver 的一个 profile）
       每步：全新 ephemeral Session，seed = goal + 未完成项 + 本步 verify
             TurnPolicy 临时换成 max_iterations=8 / natural / 无 C3
       一步最多 1 次 repair；verify 失败 → blocked → 整轮结束
  → 收据 + 父会话两行气泡
```

实测（上传的 trace，90 个回合）：

| 现象 | 数据 |
|---|---|
| 回合被迭代预算切断 | `max_iterations` 7 次；酒店计划里明确 `8/8 次迭代` |
| 计划整体撞回合上限 | `plan turn budget exhausted`（20 回合上限，23 步计划） |
| 单步受阻后整轮终止 | `blocked` → `should_stop` 为真 |
| 每步重新探索 | 每步 seed 只带 goal + 未完成项，不带已读文件、决策、接口 |

根因不是某个数字，而是四个设计选择叠加：

1. **会话边界**：每步都是新会话 → 上下文每步归零，重复探索，跨步一致性靠模型记忆。
2. **预算边界**：预算是「每个 turn 的迭代数」，不是「整个目标的 token/时间」→ 大步骤必然被切断。
3. **计划边界**：计划是静态清单，不能中途重写；`verify` 写错也没有修复通道。
4. **失败边界**：一步失败 = 整轮终止，而不是「记下来、继续独立的步骤、最后重规划」。

对照参考实现（见 §7 来源）：

- Claude Code / Codex / opencode 的默认循环都**没有固定迭代上限**，靠模型停手 + 上下文压缩结束；
- 计划批准后，执行与规划**在同一个会话里继续**（opencode 是用户切回 build，但仍是同一会话）；
- 计划状态（TodoWrite / `update_plan`）是**活的进度账本**，不是一次性批准物；
- Anthropic 明确警告：多智能体只适合「上下文隔离 + 可并行 + 需要专精」三条件同时成立的任务，
  多数编码任务不满足，handoff 会丢上下文，token 常是单 agent 的 3–15 倍。

## 2. 设计约束与不变量

无论选哪个方案，以下不变量建议保留：

1. **证据优先**：一步只有「verify 通过」或「明确标注 on-claim」才算完成；模型说 done 不算。
2. **计划是契约**：每项至少包含目标、涉及文件/接口、验收命令；执行中可以改，但改动要留痕。
3. **失败不静默**：blocked / skipped / stalled 都要有收据，并在下一轮可见。
4. **预算是目标的预算**：以 token / 墙钟 / 步数上限约束整个 episode，而不是每回合一个小数字。
5. **压缩发生在有意义的位置**：优先在步骤边界压缩（保留决策、文件地图、接口），而不是随机截断。
6. **并行只用于可并行的部分**：写操作要么串行，要么 worktree 隔离 + 合并队列；读/研究可以并行。
7. **可观测**：每一步的收据、验证输出、token 花费、变更文件都能在 `/trace` 和 `/resume` 里看到。

## 3. 四个方案

### 方案 A：Plan-as-Goal（单会话连续执行）

**一句话**：批准后的计划变成「带 todo 的目标」，在主会话里一口气跑完，靠压缩续命。

**执行流程**

```text
批准计划
  → task.armed_profile = "goal"（复用 GoalRunner）
  → 主会话进入执行回合：
       模型按 todo 清单工作；每完成一项 → 跑该项 verify → 通过则 todo done
       verify 失败 → 同一回合注入修复提示（带失败输出）→ 再验
       模型停手但还有 open 项 → todo/C3 门禁继续
       上下文接近上限 → L1–L4 压缩（checkpoint 摘要）
  → 全部完成 / 无进展 / 预算耗尽 → 结束，留下收据
```

**上下文工程**：完全复用现有 L1–L4；todo 清单改为 `task_injection="system"`，避免被压缩掉；
文件细节靠 read ledger + memory；接近上限时先清旧工具结果，再摘要。

**记忆 / 技能**：沿用现有静默记忆收割与按需技能注入；不做每步隔离，技能只在需要时加载。

**工具 / 权限**：不变；所有写操作串行，走现有权限门与沙箱。

**验证 / 失败**：每项完成即验；失败在一个回合内修复；连续失败由 acceptance ladder 止损。
受阻项标 blocked、跳过；全部受阻则结束并报告。

**与现有代码的距离**：最小。`GoalRunner` 已有「主会话 + 回合制 + episode 预算 + 无进展保护」；
需要新增的是「按 todo 项跑 verify」这一层，以及把 `/plan run` 从 `PlanRunner` 切到该 profile。

**代价**：单会话上下文会很大，压缩有损；没有并行；一个回合可能很长（但可 Esc）。

**最适合**：中小型、强耦合、需要连续推理的任务；也是「先止损」的最快路径。

---

### 方案 B：DAG 调度 + 共享黑板（结构化编排）

**一句话**：计划编译成依赖图，能并行的并行，所有步骤共享一块结构化黑板。

**执行流程**

```text
计划批准
  → 编译 DAG（depends_on 已有）
  → 取 ready set（最多 K 个）
       write 步骤：worktree 隔离执行 → 产出 branch/patch → 合并队列
       read/研究步骤：直接并行
  → 每步读「上下文包」：goal + 本步契约 + 黑板切片 + 相关文件 + verify
  → 每步回写「步骤报告」：改了哪些文件、做了什么决策、接口、遗留
  → 合并 → 跑集成验证 → 失败进入修复或重规划
```

**黑板（Blackboard）**：`plan/blackboard.json`，结构化字段：

```json
{
  "decisions": [{"id": "d1", "text": "使用 SQLAlchemy 2.0 async", "by": "step-2"}],
  "files": {"app/models/room.py": "step-2 owns; exports Room"},
  "interfaces": [{"name": "RoomRepository", "shape": "...", "by": "step-3"}],
  "commands": {"test": "pytest -q", "lint": "ruff check ."},
  "pitfalls": [{"text": "verify 里的 /dev/null 不可写", "by": "step-4"}]
}
```

每步 seed 只注入与本步相关的切片（按文件/接口/关键词检索），不注入整块黑板。

**上下文工程**：每步上下文小且干净；跨步一致性靠黑板；不需要长会话压缩。
黑板的写入模板由 harness 校验（防止无限膨胀）；超过阈值时做一次「黑板整理」步骤。

**记忆 / 技能**：步骤开始时按标签加载技能（如 `fastapi` / `pytest`）；步骤结束把决策写入
workspace memory（与黑板去重），跨会话可复用。

**工具 / 权限**：write 步骤必须 worktree；合并队列串行写主工作区；读步骤共享只读工具。
权限门按 worktree 路径授权，保持现有红线。

**验证 / 失败**：verify 失败 → 修复 1–2 次；仍失败 → 允许**修正 verify 命令**（todo update）
再验；blocked → 依赖它的步骤标 skipped，独立步骤继续；最后触发一次重规划。

**与现有代码的距离**：中高。已有 `depends_on` / `blockers`、subagent + worktree（但目前
worktree 是临时的，执行完即删，不合并）、每步收据。缺：调度器、黑板存储、上下文包构建、
合并队列、verify 修正通道。

**代价**：并发 token 花费；合并冲突与语义冲突；黑板维护成本；调试复杂度上升。

**最适合**：大型、模块边界清晰、可并行的项目（后端/前端/测试/文档分线）。

---

### 方案 C：Planner + Persistent Executor（双轨 + 边界检查点）

**一句话**：规划仍由 plan 模式负责；执行是一个长会话，但在**每个 todo 边界**做一次
结构化压缩与检查点，必要时重规划。A 的连续 + B 的结构，取中间。

**执行流程**

```text
批准计划
  → 主会话执行（同 A：不重开步骤会话）
  → 每完成一项（verify 通过）：
       1) 写 StepNote：决策 / 改动文件 / 接口 / 下一步 / 证据
       2) 强制一次边界检查点：压缩历史，保留 goal + 计划 + 最近证据 + StepNote
       3) 重读计划：剩余项还成立吗？
            成立 → 继续下一项
            不成立 → 进入一次「重规划」：改写剩余项（需确认或按规则自动）
  → 全部完成 / 无进展 / episode 预算耗尽 → 结束
```

**StepNote（结构化）**：落在 `.ai-coding/plan/steps.jsonl`，同时把「决策/接口」摘要写入
workspace memory。父会话只保留 StepNote 摘要，详细步骤历史留在压缩 checkpoint 里。

**上下文工程**：边界压缩比随机 L4 更可控：每个边界的输入是「goal + 计划 + 上一步证据 +
本步契约」，不会被工具输出的噪声挤爆；历史中真正需要复用的内容（文件地图、接口）被
显式写进 StepNote，而不是指望摘要模型记住。

**记忆 / 技能**：每个边界写记忆；技能按项加载；研究型子项可以派 subagent（结论回父），
但写操作仍在主会话串行，避免合并问题。

**工具 / 权限**：不变，写操作串行；权限门与沙箱不分叉。

**验证 / 失败**：每项 verify + 修复；verify 失败允许改命令并重验（与 B 相同，但只在主会话）；
连续失败 → 边界重规划，把大项拆小或换实现路径；blocked 项跳过，最后报告。

**与现有代码的距离**：中。复用 GoalRunner 的主会话回合、episode 预算、无进展保护；
新增 StepNote 存储、边界压缩调用、重规划钩子、verify 修正；`PlanRunner` 的收据格式可复用。

**代价**：仍是串行；边界压缩的摘要质量决定成败；重规划需要防止「无限重规划」。

**最适合**：中大型、串行为主、需要连续上下文又怕上下文爆炸的项目（酒店系统这类）。

---

### 方案 D：Orchestrator + 专家 Worker（并行 worktree 合并）

**一句话**：lead 只做拆解、集成与验收；worker 在各自 worktree 里实现并自验，产出可合并的
分支；lead 用合并队列集成，失败则重规划或返工。

**执行流程**

```text
规划（lead）：把目标拆成 workstream（文件所有权 + 接口契约 + 验收命令）
  → 并行启动 N 个 worker（task_only 上下文 + 相关技能 + 黑板切片）
        worker：实现 → 自验 → commit 到自己的 branch → 返回 StepNote/证据
  → lead：按依赖合并 branch → 跑集成验证 → 冲突/失败 → 返工或重规划
  → 最终 reviewer（fresh context）审计 diff
```

**上下文工程**：worker 上下文隔离、只回摘要 + patch；lead 维护一份「集成账本」
（文件所有权、接口、已合并分支、未决冲突），不保留 worker 的完整推理。

**记忆 / 技能**：worker 结论写回黑板；lead 在合并后写决策记忆；技能按 workstream 选。

**工具 / 权限**：worker 的 worktree 是一等公民：需要新的「提交 + 返回分支」协议、
合并队列、冲突处理；权限门按 worktree 授权。

**验证 / 失败**：worker 自验 + lead 集成验证 + fresh reviewer；失败返工到原 worker
（保留其分支）；冲突无法自动合并 → lead 介入或重规划。

**与现有代码的距离**：最高。现有 subagent 是「只回结论、worktree 用完即删」，没有 patch
handoff、没有合并队列、没有契约校验。需要新增一套 worker 协议与集成层。

**代价**：token 成本最高（研究显示 3–15×）；合并/语义冲突；handoff 丢上下文；调试最难。
Anthropic 的结论是：编码任务多数不适合多智能体，除非可并行部分足够大且边界清晰。

**最适合**：明确可分解的大型交付（后端 / 前端 / 测试 / 文档分线），且团队接受更高成本。

## 4. 对比

| 维度 | A 单会话 | B DAG+黑板 | C 双轨+检查点 | D 并行 Worker |
|---|---|---|---|---|
| 上下文连续性 | 最高 | 低（每步干净） | 高（边界受控） | 低（worker 隔离） |
| 并行度 | 无 | 高 | 无（研究可并行） | 最高 |
| 跨步一致性 | 靠长上下文 | 靠黑板 | 靠 StepNote + 计划 | 靠契约 + lead |
| 验证强度 | 中 | 高（可修 verify） | 高（可修 verify） | 最高（集成 + reviewer） |
| token 成本 | 低–中 | 中–高 | 中 | 高（3–15×） |
| 实现风险 | 低 | 中高 | 中 | 高 |
| 大项目适配 | 中 | 高 | 高 | 高（可分解时） |
| 失败恢复 | 中（同回合修复） | 强（独立步骤继续） | 强（边界重规划） | 强（返工/重规划） |
| 可观测性 | 中（一长回合） | 高（每步收据） | 高（每步收据 + 检查点） | 中高（分支/合并） |

## 5. 已选方案与落地顺序

**已选：方案 B（DAG + 共享黑板），预算采用「无迭代上限 + 目标预算」，计划允许自动改写。**

选择 B 的理由：它直接对应这次的失败形态（23 步、可分解、需要跨步一致），并且为将来并行留了
结构；同时，B 不必一上来就做 worktree 写并行——先把「调度 + 黑板 + 每步上下文包 + 验证修正 +
重规划」做对，串行也能立刻解决重复探索、预算切断、受阻即停三个问题。

**落地顺序（按依赖，不涉及时间）**

1. **B1 串行调度 + 黑板**：DAG 就绪检查；每步上下文包；`step_report` 结构化回写；verify 修正；
   blocked 跳过依赖项；预算改为 episode token/墙钟；重规划一次。
2. **B2 只读并行**：计划项标注 `research`/`read` 的步骤并行跑子代理，写步骤仍串行。
3. **B3 写并行 + 合并队列**：worktree 持久化、committer/分支协议、合并队列、冲突处理；
   只对显式 `parallel: true` 的工作流开启。

验收标准（每一阶段都要满足）：

- 一个 20+ 步的计划在无人工输入下连续执行到 done/deadlock/replan，不再出现「每完成一项就停」；
- 任何一步被预算切断，留下可续跑的 StepNote，下一步或下一轮从断点继续，不重做已完成工作；
- 每步的 verify 输出、改动文件、token 花费在 `/trace` 与收据里可查；
- 阻塞一步不会终止整轮；依赖它的项标 skipped，独立项继续；
- 全量测试（后端 + 前端）通过，`ruff` / `mypy` / `tsc` 干净。

## 6. 方案 B 的详细设计

### 6.1 目标形态

```text
plan 模式（只读）→ present_plan（含 files / depends_on / verify）
  → 批准
  → PlanOrchestrator（新的 /plan run 实现）
       1. 编译 DAG，做静态检查（环、未知依赖、越界路径、verify 非空）
       2. 建 Blackboard（run-scoped）与预算账本
       3. while 有 ready 项且预算未耗尽：
            ready = 未完成、依赖已终态、未被 skip 的项
            取最多 K 个（B1: K=1）
            并发执行 LeasedStep：
               build StepContextPack
               run 一个短会话回合（无迭代上限，受 step token/墙钟约束）
               解析 step_report + 跑 verify
               失败：repair（带失败输出）→ 可修正 verify → 再验 → blocked
            回写 Blackboard + 收据 + task 状态
            波次结束：跑静态检查 / 集成验证
            无 ready 且仍有 open → 触发一次 replan
       4. 汇总 PlanReport（done / blocked / skipped / replan / tokens）
```

### 6.2 数据模型

```text
PlanBlackboard（新，run-scoped，落盘 .ai-coding/plan/<session>/blackboard.json）
  goal: str
  decisions: [{id, text, by_step, at}]
  files: [{path, owner_step, interfaces[], status}]
  interfaces: [{name, shape, by_step}]
  commands: {test, lint, run}
  pitfalls: [{text, by_step}]
  budget: {tokens, wall_seconds}
  version: int
  上限：entries 200 / chars 24k；超限触发一次 blackboard 整理（模型总结 + 去重）

StepContextPack（新，纯函数构建，永不落盘）
  goal + 本步 statement/verify/files
  + 黑板切片（按 files / interfaces / keywords 检索，最多 N 条）
  + 相关文件路径与上次读取行段（read ledger）
  + 最近一次尝试的 StepNote（重试时）
  + 技能提示（按文件类型/标签）
  预算：字符上限，例如 24k；超限按优先级裁剪（goal > 本步契约 > 接口 > 文件片段）

StepNote（新，复用 StepDelivery 的字段并扩展）
  status: completed | blocked | stalled | skipped
  files_changed, decisions, interfaces, evidence, verify_output, next_hint
```

### 6.3 调度算法（B1 串行）

```text
compile():
  校验 depends_on：未知 id、环、自依赖 → 计划不可执行，回卡片
  verify 为空且不可机检 → 标记 on-claim（不阻断，但收据标证明等级）
  files 声明冲突（两个未完成项声明同一文件且无依赖）→ 插入一条“串行化”依赖

loop():
  while budget.remaining > 0:
      ready = [i for i in open if i.status == pending and deps_terminal(i)]
      if not ready:
          if any(dep_blocked(i) for i in open): mark_skipped_descendants()
          if still no ready: replan_or_stop()
          continue
      item = next_by_priority(ready)   # 拓扑序 + 声明顺序
      result = await run_step(item)
      apply(result)                    # task 状态 / 黑板 / 收据 / 事件
      maybe_replan(result)
  stop: done | no-progress | budget | disarmed | deadlock
```

依赖传播：某步 `blocked` → 所有传递依赖它的未完成项标 `skipped`（新状态），独立项继续；
若剩余全是 skipped/blocked → `deadlock`，触发一次 replan（若允许）。

### 6.4 预算（替代迭代上限）

- `TurnPolicyConfig.max_iterations = 0` 表示**不限**（需要改 `from_ai_config` 的 `max(1, …)`
  与 `decide_next_iteration` 的比较；`budget_pressure` 在 0 时不告警）。
- 每步执行受 **step 预算**：`plan_step_token_budget`（默认 200k）与
  `plan_step_wall_seconds`（默认 900）。到限不再强行结束整轮：写出 StepNote，标记
  `continuation`，调度器用导出的 StepNote 再起一次尝试（最多 `plan_max_step_attempts=2`）。
- 整个 run 受 **episode 预算**：`plan_token_budget`（默认 0=不限）与
  `plan_wall_seconds`（默认 0=不限），从 provider usage 累计；到限 → `budget`，可 `/plan run` 续跑。
- 无进展保护：一步零工具调用 → 立即结束该步（stalled），不消耗重试额度。

### 6.5 验证与修复

1. 有 `verify`：回合结束后由 orchestrator 亲自跑命令（不接受模型“通过”的声明）。
2. 失败：repair 回合，prompt 带失败输出 + StepNote + “只修失败点”。
3. 再失败：允许一次 **verify 修正**——模型用 `todo_write(update)` 改 `verify`，
   orchestrator 重新读取该项并再跑；修正记录进收据（`verify_amended=true`）。
4. 仍失败：`blocked`，证据落盘；依赖项 skip；不终止整轮。
5. 无 `verify`：模型必须用 `step_report` 给出 `files_changed` + 命令证据；否则标 `on-claim`。

### 6.6 自动重规划

触发：blocked 导致 deadlock、连续两次 stalled、或发现计划假设不成立（模型在 step_report
里报 `needs_replan`）。

规则：

- replan 是一次**只读规划回合**（复用 `PLAN_PROMPT`），输入：原 goal + 当前黑板 + 未完成项 +
  失败证据；输出：剩余项的新列表（可增删改，但不得缩小 goal）。
- 不需要人工批准（已选“自动改写”），但要在父会话留一条 `[replan]` 提示与差异摘要；
  改动的项保留旧收据（按 statement 匹配，不复用 id）。
- 每次 run 最多 `plan_max_replans=2`；超过则停下并报告，避免无限重规划。
- 新项同样过 DAG 静态检查。

### 6.7 黑板与记忆、技能、压缩的关系

- **记忆**：黑板是 run-scoped 工作记忆；步骤结束后把「决策 / 接口 / 坑」写入 workspace memory
  （与黑板去重），跨会话可召回。不要把整块黑板塞进 memory。
- **技能**：按本步声明的文件类型/标签加载（如 `fastapi`、`pytest`、`alembic`），没有标签则按需。
- **压缩**：每步是短会话，天然不需要长上下文压缩；黑板承担跨步一致性，避免“压缩丢细节”。
  若某一步自身很长，仍走现有 L1–L4；StepNote 在压缩前写入，保证关键状态不丢。
- **工具**：`step_report`（新）是唯一的结构化回写通道；`todo_write` 继续维护计划本身；
  `memory_search` 用于黑板切片不足时补细节。
- **Harness**：波次结束跑 `static_check`；集成阶段跑项目声明的 test/lint；`git diff` 记入收据。

### 6.8 代码落点

| 模块 | 改动 |
|---|---|
| `domain/task.py` | 新增 `skipped` 状态、`attempts`、`verify_amended`；渲染与校验同步 |
| `domain/plan_blackboard.py`（新） | 黑板模型、schema 版本、大小上限、to/from dict |
| `core/plan_scheduler.py`（新） | 编译 DAG、ready set、skip 传播、deadlock 判定 |
| `core/plan_context.py`（新） | StepContextPack 构建与预算裁剪 |
| `core/plan_orchestrator.py`（新） | 新的 RoundProfile（替换 PlanRunner 的 run 路径） |
| `core/turn_policy.py` | `max_iterations=0` = 不限；pressure 跳过 |
| `core/plan_runner.py` | 保留旧 profile 作为 fallback；新编排器逐步接管 |
| `tools/step_report_tool.py`（新） | 结构化回写（status/files/decisions/interfaces/evidence/needs_replan） |
| `tools/present_plan_tool.py` | 计划项增加 `files`、`parallel`、`kind(research/write)`（可选） |
| `config/models.py` | `plan_token_budget`、`plan_wall_seconds`、`plan_step_token_budget`、`plan_step_wall_seconds`、`plan_max_step_attempts`、`plan_max_replans`、`plan_parallelism` |
| `server/handlers.py` + 前端 | 步骤事件带 `wave/attempt/skipped`；计划报告展示 replan 与 skip |
| `docs/data-contracts.md` | 新契约与默认值 |

### 6.9 风险与缓解

| 风险 | 缓解 |
|---|---|
| 黑板膨胀 / 过期 | 条目与字符上限；每波整理；按步检索注入，不整块注入 |
| 上下文包漏掉关键细节 | 计划项声明 `files`；read ledger 补片段；模型可 `memory_search` / `read` 再取 |
| verify 命令写错 | 允许一次修正并留痕；修正后必须真正跑过 |
| 自动改写计划失控 | 不得缩小 goal；最多 2 次 replan；改写留差异与收据 |
| 依赖环 / 未知依赖 | 编译期拒绝，回到计划卡片 |
| 写并行合并冲突 | B3 才开启；worktree 持久化 + 合并队列 + 冲突交回 lead；默认串行 |
| token 成本 | episode 预算；步骤短会话；黑板切片注入；B2 先只读并行 |

## 7. 参考来源

- [Claude Code – permission modes（批准计划后同会话执行）](https://code.claude.com/docs/en/permission-modes#review-and-approve-a-plan)
- [Claude Code – best practices（Explore→Plan→Implement→Commit、fresh verification）](https://code.claude.com/docs/en/best-practices/)
- [Claude Code – todo tracking](https://code.claude.com/docs/en/agent-sdk/todo-tracking)
- [How Claude Code works（压缩与子代理）](https://code.claude.com/docs/en/how-claude-code-works)
- [Codex CLI – approval modes](https://developers.openai.com/codex/cli/features#approval-modes)
- [Codex – update_plan prompt（恰好一个 in_progress）](https://raw.githubusercontent.com/openai/codex/87f7226cca12df04596938f58625de84e976309a/codex-rs/core/gpt_5_2_prompt.md)
- [OpenAI – Unrolling the Codex agent loop](https://openai.com/index/unrolling-the-codex-agent-loop/)
- [opencode – agents（steps 默认不限）](https://opencode.ai/docs/agents/)
- [opencode – compaction](https://opencode.ai/v2/docs/compaction/)
- [Anthropic – multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system)
- [When and how to use multi-agent systems（编码任务不适合的边界）](https://claude.com/blog/building-multi-agent-systems-when-and-how-to-use-them)
- [LangChain – Plan-and-Execute](https://blog.langchain.dev/planning-agents/)
- [LLMCompiler（DAG 并行）](https://arxiv.org/html/2312.04511v2)
- [AdaPlanner（静态计划会过期）](https://papers.neurips.cc/paper_files/paper/2023/file/b5c8c1c117618267944b2617add0a766-Paper-Conference.pdf)
- [DeepAgent（动态子任务 DAG 与重规划）](https://arxiv.org/pdf/2502.07056v1.pdf)
- [Anthropic – compaction threshold](https://platform.claude.com/docs/en/build-with-claude/compaction-threshold)
