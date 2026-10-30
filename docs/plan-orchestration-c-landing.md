# 方案 C 落地方案：Wave 连续 ReAct（r24）

> **评审补丁（r24-review）**：本稿的部分实现细节已被
> [plan-orchestration-c-review.md](plan-orchestration-c-review.md) §4 修订——
> 验收从工具内改到回合边界、不做 `max_iterations=0`、波会话继承权限、
> handoff 需要常驻载体、波边界按验收/kind 而非纯尺寸。以评审稿为准。

> 用户已选 **C（Wave 混合）**。本文是它的实施级设计：波次怎么切、波内怎么跑、波间怎么交接、
> 验收/预算/失败/重规划怎么落，以及逐模块改动与测试。
>
> 前置：[plan-orchestration-options-v2.md](plan-orchestration-options-v2.md)（四方案对比）、
> [plan-orchestration-b-landing.md](plan-orchestration-b-landing.md)（被取代的 DAG 稿，仅作参考）。

## 0. 一句话

把计划切成 3–6 条目的**波次**；波内是一条连续的 ReAct 会话（同一 transcript、无迭代上限、
条目完成即机械验收），波间换一个**新会话**，用结构化 handoff 接上；波边界做集成验收与重规划。

## 1. 为什么是 C

| 事实 | 后果 |
|---|---|
| 现状每步空会话，跨步知识丢失 | 波内共享 transcript，等于把 N 次重启降到 N/4 |
| 单会话 40 步上下文过大 | 波间换会话，上下文有硬上界 |
| 实测链式短会话比一次性长会话省 ~30% token | 波内连续、波间重置，落在两者之间 |
| 编码依赖运行时才暴露 | 波内用 ReAct 自适应；只按软依赖排序，不编译 DAG |
| 条目需要证据 | 条目完成时跑 verify，波末再跑一次集成验 |

## 2. 数据流

```text
/plan run
  └─ handlers._launch_armed_run(profile=plan)
      └─ PlanExecutor.run(session, task)                    core/plan_executor.py（新）
          ├─ workspace 闸门（复用 plan_runner 的检查）
          ├─ arm + persist + task.updated
          ├─ waves = group_waves(task, size=plan_wave_size)  core/wave_planner.py（新）
          └─ for wave in waves:
              ├─ handoff = previous wave's WaveHandoff（第 1 波为空）
              ├─ wave_session = ephemeral SessionData
              │     seed = goal + handoff + 本波条目 + 各自 verify
              ├─ policy = TurnPolicy(max_iterations=0, stop_policy="natural",
              │                      max_auto_continuations=0)   ← 由执行器驱动回合
              ├─ while 本波还有 ready 条目:
              │   ├─ item = next_ready_in_wave()
              │   ├─ process_input(wave_session, "[wave] 只做 #id：...")
              │   │     loop：压缩照常；todo_write 完成时先 verify（失败不落状态）
              │   ├─ 机械复核该条目（verify 再过一次）
              │   └─ 收据 → 父会话（synthetic 标记 + 摘要 + PlanStepReceipt）
              ├─ 波末集成验收：重跑本波所有 verify；失败项 rerun 一次
              ├─ handoff = wave_report(模型结构化总结) + 机械账本（改过的文件/命令/退出码）
              ├─ replan 判定：deadlock / needs_replan → 改剩余项（≤2 次）
              └─ wave_session 丢弃；父会话持久化
```

**状态归属**：

- 真相 = 父会话的 `TaskState`（条目、状态、证据）+ `step_deliveries`（收据）+ `PlanWave` 记录。
- 波内 transcript 是**工作记忆**，波末丢弃；能跨波的是 handoff（结构化）。
- 计划版本（`plan_version`）在任何改写时 +1，并发 `plan.updated` 事件。

## 3. 波次怎么切

纯函数 `group_waves(items, *, size, path_hint)`，不依赖执行状态，便于单测。

规则（按顺序）：

1. **只在 open 项上切**；已完成/跳过的项不产生波。
2. 按 `depends_on` 求弱连通分量；同一分量内的项优先同波（依赖链不跨波，除非超过 size）。
3. 分量内按顺序装入，**同波上限 `plan_wave_size`（默认 4）**；超过则按拓扑序切开。
4. **路径提示**：`item.files`（present_plan 新字段，C2）或从 content/verify 提取的仓库相对路径；
   两个项共享路径且都写文件时，倾向同波（减少重复读），但**不作为正确性约束**。
5. **验收边界**：`verify` 是重命令（含 `pytest`/`make`/`npm test`/`cargo test`）的项，作为波的
   **最后一项**，避免每波都跑全量。
6. 单条目计划 = 单波；全部独立 = 每 size 个一波；未知依赖 = 单独成波（fail-safe）。

`group_waves` 只决定「先跑哪一批」，不改变条目顺序；波内仍需 `next_ready` 过滤依赖。

## 4. 波内执行（A 的核心）

### 4.1 回合驱动，不是迭代驱动

- `max_iterations=0`（不限）。回合何时结束由模型自己停 + 预算决定。
- 执行器在每回合返回后检查：本波 ready 项还在吗？在 → 注入下一回合；不在 → 波结束。
- 每一回合的提示只含**本波 + 当前条目**，不含全部计划（避免复现现状的平方 seed）。
- C3（自动续跑）关闭：续跑由执行器负责，避免双重驱动。

### 4.2 条目验收（关键机制）

`todo_write(update, id, status="completed")` 在工具内部：

1. 若有 `verify`：`await verify_runner(item)`（注入 `ToolContext` 的可选回调；
   复用 `core.acceptance.verify_items` 的权限/审批/超时/工作区根）。
2. 失败 → 返回 `success=False` + 输出尾部，**状态保持 in_progress**，模型在同一会话里修复。
3. 成功 → 落 `completed` + `evidence=verify passed: ...`，再返回列表 + 下一个 ready 提示。
4. 无 `verify` 的条目：允许模型自报完成，但收据标 `on-claim`（现有 `StepDelivery` 语义）。

每条目最多 `plan_max_item_attempts=2` 次修复回合；超过 → `blocked`，其依赖项 `skipped`。

### 4.3 失败与阻塞

- `blocked` 不终止波：跳过依赖它的项，独立项继续。
- 本波无 ready 且仍有 open：波提前结束，进入波边界（可能是 replan）。
- 崩溃/取消：`armed` + `rounds_run` 已落盘；boot resume 从当前波重开（handoff 缺失时用
  `step_deliveries` 重建摘要）。

## 5. 波间交接（handoff）

### 5.1 结构

```python
@dataclass
class WaveHandoff:
    wave_id: str
    items_done: list[str]          # 已终结条目
    files_touched: list[str]       # 机械账本：write/edit 工具实际改过的路径
    interfaces: list[str]          # 模型：新增/改变的接口签名
    decisions: list[str]           # 模型：做出且后续要遵守的决定
    pitfalls: list[str]            # 模型：踩过的坑 / 不可行的路径
    verify: list[dict]             # {item_id, command, passed, tail}
    next_hint: str                 # 下一波建议（可空）
```

- 模型侧：`wave_report` 工具（仅计划执行会话可用），一次调用提交 `interfaces/decisions/pitfalls/next_hint`。
- 机械侧：执行器从 dispatcher 的工具记录里提取 `files_touched` 与 verify 结果，**覆盖**模型的说法。
- 容量：每段最多 12 条、每条 ≤ 200 字符；超出按「最近 + 与下一波相关」裁剪。

### 5.2 注入下一波

`seed = 目标 + handoff（只给最近一波）+ 本波条目 + 依赖产出的 verify 摘要`。
不累计全部历史 handoff（避免线性膨胀）；需要更早的事实由 `memory_search` 提供。

## 6. 预算

| 预算 | 默认 | 作用 |
|---|---|---|
| `plan_wave_token_budget` | 300000 | 单波 token 上限；每回合 `max_turn_tokens` 夹到剩余额度，防止单回合 7× 超支 |
| `plan_wave_wall_seconds` | 1200 | 单波墙钟，超时结束本波 |
| `plan_token_budget` | 0 | episode 上限，0 = 不限 |
| `plan_wall_seconds` | 0 | episode 墙钟，0 = 不限 |
| `max_tool_iterations` | 0 | 回合内不限；仅防失控时手动设 |

到限不是失败：本波已完成的收据保留，`blocked`/`open` 项留给下一波或下次 `/plan run`。

## 7. 重规划

触发（满足其一）：

1. 波边界出现 deadlock（有 open 但无 ready）。
2. 模型在 `wave_report` 里标 `needs_replan=true`。
3. 本波 `blocked` 比例 ≥ 1/2 且下游依赖它。

动作：模型用 `todo_write` 改写剩余项（可 add/update/drop；不得缩小 goal），
`plan_version += 1`，发 `plan.updated` + 一条 notice。上限 `plan_max_replans=2`。

## 8. 逐模块改动

| 模块 | 改动 | 性质 |
|---|---|---|
| `domain/task.py` | 新增 `skipped`；`TodoItem.files/kind/attempts`；marker `[~]` | 小 |
| `domain/plan_wave.py`（新） | `Wave`、`WaveHandoff`、`board` 的序列化 | 新 |
| `core/wave_planner.py`（新） | `group_waves` 纯函数 + 路径提示 | 新 |
| `core/plan_executor.py`（新） | 波循环、回合驱动、验收、收据、handoff、预算、replan | 新 |
| `tools/todo_write_tool.py` | verify 回调、skipped、下一个 ready 提示 | 中 |
| `tools/wave_report_tool.py`（新） | 结构化 handoff 提交 | 新 |
| `tools/registry.py` / 工具栈 | 计划执行会话挂 `wave_report`，禁用 `present_plan` | 小 |
| `core/acceptance.py` | 暴露单条目 `verify_one`（复用现有 `verify_items`） | 小 |
| `server/handlers.py` / `app/commands.py` | `plan_orchestrator="wave"` 走新执行器；legacy 保留 | 小 |
| `server/bridge.py` + 前端 | 新事件 `plan.wave`；计划卡 skipped / 波次提示 | 中 |
| `config/models.py` | 4 个预算键 + 开关 + `plan_wave_size` | 小 |
| `docs/features.md` / `data_contracts.md` | 契约更新 | 文档 |

**不改**：AgentLoop 主循环、压缩、记忆、技能、权限门、子代理、WorktreeManager。
波内与波间都复用现有 `process_input` 与 `verify_items`。

## 9. 分阶段

### C1：领域 + 分组（无执行改动）

- `skipped` 状态与字段；`group_waves` 纯函数；测试。
- 不接 handler，不影响任何现有行为。

### C2：执行器主链

- `PlanExecutor` + 回合边界验收（`session.acceptance`）+ handler 切换 + 事件；
  **不加** `wave_report`（handoff 先用机械账本 + 波末回复文本）。
- 端到端：5 条 2 波、verify 失败修复、blocked→skip、死锁、波预算切断后继续。

### C3：演化与并行

- `wave_report` 工具（结构化 handoff / `needs_replan`）；波边界 replan；前端计划卡波次进度；
  `kind=research` 波复用 B0 的 `ResearchFanOut` 做只读 fan-out。

## 10. 测试矩阵

| 层 | 用例 |
|---|---|
| `wave_planner` | 依赖链不跨波；size 上限；共享路径倾向；重验收项置末；单条/全独立/未知依赖 |
| `domain/task` | skipped 不算 open；依赖跳过传播；marker；序列化往返 |
| `todo_write` | 拒绝模型写 skipped；依赖校验；越权 scope |
| `acceptance` | 回合末 verify；失败在同一回合 repair；回合后补验额外完成的条目 |
| `research_fanout` | 2–4 问并发；单条失败隔离；task_only；只读；usage 汇总 |
| `plan_executor` | 波内连续；波间 handoff；blocked 不终止波；deadlock；波预算到限续跑；replan 上限 |
| `wave_report` | 字段校验；机械账本覆盖模型说法；容量裁剪 |
| 前端 | skipped 标记；wave 提示；replay 兼容 |
| 端到端 | 23 步假模型分波跑完；真实验证；`/resume`；boot resume |

## 11. 风险与回滚

| 风险 | 缓解 |
|---|---|
| 波切分不合理，丢连续性 | 分组只是提示；波内仍可访问上一波文件；`plan_wave_size` 可调 |
| 波间 handoff 质量差 | 机械账本兜底 + handoff 结构化 + 下一波可重新读文件 |
| 单波上下文仍过大 | 波预算 + 压缩 + size 上限 6 |
| 执行器与 loop 双重驱动 | 波内 C3 关闭；回合由执行器显式发起 |
| 旧行为回归 | `plan_orchestrator=legacy` 开关，旧 `test_plan_*` 全保留 |
