# 计划执行编排 · 第二轮方案（r24）

> 第一轮选的是「DAG + 共享黑板」。本轮重新从**数据流**出发：先看正常 build 回合与计划执行
> 究竟差在哪，再决定编排层该拥有什么。结论是 **DAG 不是主执行器**，主执行器仍应是 ReAct，
> 编排层负责「选条目 / 装配上下文 / 验收 / 持久化」四件事。下面给四个可选方案。
>
> 前置：[plan-orchestration-design.md](plan-orchestration-design.md)、
> [plan-orchestration-b-landing.md](plan-orchestration-b-landing.md)（DAG 方案的完整落地稿）。

## 0. 结论

1. **计划不是工作流图，是带依赖的清单。** 编码步骤的真实依赖（编译/测试反馈、文件现状）
   大多在运行时才暴露；静态 DAG 编译得越细，越容易在第三步就失真。
2. **执行单元继续是 ReAct 回合。** 编排层不该重写循环，而应决定：下一个做哪条、给它什么上下文、
   什么时候算完成、状态写在哪。
3. 现状 PlanRunner 的核心问题不是「串行」，是**每步都换一个空会话**：跨步知识被丢掉，
   seed 随计划规模平方增长，模型每次都要重新探索。这是「每完成一项就停下来」的体感来源。
4. 四个方案：
   - **A. Plan-as-Goal**：同一会话 + 外部回合驱动（推荐默认）
   - **B. Step-session + 结构化交接**：保留现状骨架，修知识传递与边界
   - **C. Wave 连续 ReAct**：A 的分段版，按波次换会话（规模化路径）
   - **D. DAG 调度**：第一轮方案，只在并行/隔离执行时才有价值
5. 并发正确性层（acc-layer）**不在串行执行里引入**；它自己 README 就写明「单 agent、
   写入不重叠时不要用」。只在 D 的写并行阶段借三个机制（声明写集 + 读集版本校验、
   单写者合并队列、带 verified 的收据），以 ~300 行 asyncio 自研，不 vendoring 25k 行。

## 1. 两条数据流：build 与 plan 到底差在哪

### 1.1 正常 build 回合

```text
前端输入
  └─ server 收到 message
      └─ AgentLoop.process_input(session, text)            agent_loop.py:848
          └─ _run_turn                                     agent_loop.py:937
              ├─ _begin_turn：追加 user 消息、开 turn、turn.start hook   1484
              └─ while true:
                  ├─ policy.decide_next_iteration(snapshot)             1026
                  ├─ assembler.assemble：压缩 + 指令 + 尾部易变块        1151
                  │     （memory / task / hook / digest / budget）
                  ├─ _model_step：一次模型调用                          1170
                  ├─ _run_tools：dispatch → 写回 → checkpoint           2126
                  └─ policy.decide_after_step                           1304
                      ├─ 模型不再要工具：验收（如有）→ finish           2449
                      ├─ 预算断且有未完成项：C3 续跑（新开一轮）
                      └─ 否则触发 ladder（task / strategy / search）
              └─ _finish_turn：handoff + digest + 落盘 + turn.end       1702
```

**状态归属**：会话 transcript 是唯一真相；task 列表是旁路提示；acceptance 是可选合同。
**上下文**：一条持续增长的 history，压缩 L3→L1→L2→L4 兜底；工具结果、模型推理全在里面。
**终止**：模型自己停（无工具调用）+ 门禁；预算断时 C3 自动续跑；没有「步骤」概念。

### 1.2 现状计划执行

```text
前端 turn.finished(completed/max_iterations)               session.tsx:685-689
  └─ dispatch("/plan run")
      └─ commands.py:593 退出 plan 模式
          └─ handlers.py:673 _launch_armed_run（profile=plan）
              └─ PlanRunner.run                              plan_runner.py:558
                  └─ TaskDriver.run                          task_driver.py:78
                      └─ while 有 ready 项:
                          ├─ next_item（跳过本次已试过/被阻塞的）   652, 804
                          ├─ _run_step                                976
                          │   ├─ 新建 ephemeral SessionData           853
                          │   ├─ seed = 标题 + 所有未完成项 + verify + 上次收据  886
                          │   ├─ policy 换成 max_iterations=60 / natural / C3=0   1010
                          │   ├─ process_input(step_session, STEP_HEAD + 本项)     1023
                          │   ├─ verify 失败 → 一次 repair 回合        1032
                          │   ├─ 预算断且未声称完成 → 一次 continue    1043
                          │   └─ settle：verify 过→completed；否则 blocked    1148
                          ├─ 父会话只留 synthetic user + assistant 摘要 + 收据   1081
                          └─ after_round：rounds_run += 1，落盘        708
```

**状态归属**：plan item + 收据；step 会话用完即弃（`ephemeral=True`）。
**上下文**：每步一个空会话，只带 seed；上一步做了什么、读了哪些文件、改了什么接口全部丢失。
**验收**：每步一个 `AcceptanceItem(verify=...)`，在**该步回合结束时**跑一次。
**终止**：全部终结 / deadlock / 回合预算；预算在这里是「跑多少步」的计数。

### 1.3 差异矩阵

| 维度 | build 回合 | 现状 plan 执行 | 设计含义 |
|---|---|---|---|
| 控制器 | loop 自己 | 外层 TaskDriver + 每步换 policy | 编排层必须拥有「下一步」，但不能拥有循环 |
| 状态真相 | transcript | item 状态 + 收据 | 计划状态要持久化，且要能被回合读取 |
| 上下文 | 持续增长 + 压缩 | 每步空会话 + 静态 seed | 知识传递是主要成本项，不是次要优化 |
| 跨步知识 | 自然保留 | **丢失**（只有 seed 摘要） | 要么留在同一会话，要么显式交接 |
| 验收时机 | 回合末（可选） | 步骤末（强制） | 条目边界验收是对的，但要更早、可重试 |
| 失败语义 | 模型自行处理 | blocked 即整步结束 | 应支持「条目内修复」，而不是整步重来 |
| 预算 | token/墙钟 + 无上限迭代 | 迭代数 + 回合数 | 迭代数不该是预算单位 |
| 计划演化 | 无 | 静态（批准后不可改） | 发现新事实要能改写剩余项 |
| 依赖 | 无 | `depends_on` 过滤 ready | 依赖用于「顺序约束」，不是并行分派 |
| 终止 | 模型停 + 门禁 | 无 ready / 预算 | 应统一成「无 ready 且无在飞」 |

### 1.4 由此得到的设计约束

1. **主执行器仍是 ReAct 回合**；编排层只做四件事：选条目、装配上下文、验收、持久化/事件。
2. **依赖只做顺序约束**。除非一项被显式标成调研/只读，否则不按 DAG 并行分派。
3. **验收必须在条目边界、由机械命令判定**，失败允许在**同一条目内**修复，而不是整步重来。
4. **跨步知识有两条合法路径**：同一会话（A/C）或结构化交接（B）。混着来会两头不讨好。
5. **预算单位是 token 与墙钟**；迭代数只作为防失控的兜底，不参与「是否继续」的判断。
6. **计划可演化**：模型可在执行中发现并改写剩余项；改写要留版本与事件。

## 2. 外部实现与研究

### 2.1 四个产品的实际做法

| | opencode | Claude Code | Codex CLI | dsh |
|---|---|---|---|---|
| plan 模式 | 受限 agent（禁 edit）；**无审批事件** | 权限模式 `plan` + ExitPlanMode 审批 | `ModeKind::Plan`，plan 模式下 `update_plan` 被拒 | plan 插件挂在 `agent/pre-step` waterfall |
| 批准后 | 手动切 build，同一会话同一循环 | 同一会话继续，plan 文件落盘 | 同线程发 `Implement the plan.` | 由插件改 pre-step 决策 |
| 计划状态 | 无独立对象；todo 是消息态工具 | **plan 文件** + TodoWrite | `update_plan` → 只发 UI 事件 | durable session log 派生 |
| 循环 | 单一 drain：`advanceToStep → runStep` 直到无输入 | 单一增长 transcript | 单一 history + 压缩摘要 | 单一 turn/step，插件可拦截 |
| 迭代上限 | 可选 `agent.steps`，默认无；到限注入总结提示 | 无固定回合上限 | 未发现 `max_turns` | 无固定上限 |
| 压缩 | 剩余窗口 < max(15%,16k)，摘要保留精确路径/符号/命令 | 接近上限；压缩后 plan 文件重注入 | 默认 65% 后台压缩 | compaction 在 pre-step |
| 子 agent | fresh context，只回最终文本 | 独立窗口，只回摘要 | 独立会话 | scope 隔离的插件 |
| DAG | 无 | 无 | 无 | 无 |

**共同点**：没有任何一家把「计划执行」做成 DAG 调度器。计划都是**持久状态 + 单循环 + 运行时纪律**。

### 2.2 研究证据

- LLMCompiler：静态 DAG 在**工具独立、依赖可预知**时有 3.7× 延迟优势；编码步骤不满足这个前提。
- AdaPlanner / DeepAgent：观测偏离计划 → **重写剩余计划**并从当前状态续跑，而不是重跑整图。
- ReWOO / Plan-and-Act：计划与执行分离有价值，但执行器仍是逐步闭环。
- LongCLI-Bench（20 个长程 CLI 任务）：所有 agent pass rate < 20%，失败集中在**早期**；
  plan injection 优于 self-correction。瓶颈是**计划质量与检查点**，不是调度拓扑。
- 我们的实测（`plan_runner.py:10-12`）：同一工作，链式短回合比一次性长回合**省约 30% token**，
  因为作用域化提示探索更少。这条支持 B/C，也提醒 A 的代价。

**结论**：ReAct 是对的；要补的是「外部计划状态 + 机械验收 + 跨步知识」，不是换调度拓扑。

## 3. 并发正确性层（acc-layer）评估

仓库：`github.com/linsir2/agent-concurrency-correctness-layer`，25k 行纯标准库。
核心是 RSC（Reserve-Stage-Commit）= OCC + 租约 fencing + 短临界区 + Saga 补偿；
模块分 L1 事件日志/WAL/工作区/预算，L3 运行时，L4 编排，L5 门面 `RscSession`。

它 README 的「别用它的场景」写得很明确：

> 单 agent、写入不重叠、只要 trace 的场景，用 git worktree 或 Langfuse 更合适。

我们的计划执行正属于这一类（单 agent、串行、同一工作区）。**结论：串行方案里不引入。**

真正值得借的只有三件事，且只服务于「写并行」阶段：

| 机制 | 出处 | 用法 | 我们怎么做 |
|---|---|---|---|
| 声明写集 + 读集版本校验 | `facade.begin/apply`、`workspace._validate_locked` | 提交前发现「我读的文件被改过」 | `core/parallel.py` 里 ~120 行 `FileReservation`：begin 时钉住 (path → hash)，commit 前比对 |
| 单写者合并队列（出队→重验→提交） | `merge_queue.py` | 关掉「各自验证通过、合入后坏了」的 TOCTOU | asyncio.Queue 版 ~80 行；每个写步骤一个 ticket |
| 带 `verified` 的收据 | `facade._verify_gate`、`spec/receipt.py` | 收据区分「跑过验证」与「模型自称」 | 已有 `StepDelivery`，加 `proof=verified/on-claim` 即可 |

**不建议引入**：预算树（我们的预算是 token/墙钟，不需要子树守恒）、WAL/checkpoint/effect journal
（单进程 + 已有 session 持久化）、租约/Raft（单进程）、`RscSession` 门面（需要 Harness/ACB/EventLog
一整套对象图）、`WorktreeManager`（我们的 `core/worktree.py` 已是 async 且更贴合）。
`semantic.py`（AST 静态拦改名后仍被引用）只对 Python 仓库有意义，可作为以后的可选 gate。

## 4. 四个方案

### 方案 A：Plan-as-Goal（连续会话 + 外部回合驱动）★推荐

**核心**：批准的 plan 就是一项目标。`/plan run` 在**父会话**里反复调用 `process_input`，
每一回合做「一个条目或一个条目的一段」，直到计划终结。不新建 step 会话，不换 policy 的次数上限。

```text
/plan run
  └─ PlanExecutor.run(session, task)                （新，约 200 行）
      ├─ 计划 workspace 闸门（复用 plan_runner 现有检查）
      ├─ arm + 落盘 + 事件
      └─ while not terminal(task):
          ├─ item = next_ready(task)  → 无则 deadlock/skip/replan
          ├─ 注入本轮提示："本轮只做 #id：<content>；验收：<verify>"
          ├─ process_input(session, 提示)          ← 同一会话，transcript 连续
          │    loop 内部：无迭代上限；token/墙钟；压缩照常
          ├─ 机械验收该条目（verify 命令）：
          │    过 → completed + 收据；不过 → 留在 in_progress，注入了错误，再跑一回合（≤2）
          └─ 事件：task.updated / step.receipt / plan.progress
```

**运行时纪律（比 DAG 更重要）**：

- `todo_write(update, status=completed)` 在**工具内部**先跑该条目的 `verify`：
  失败则返回错误 + 输出，状态不落；模型在同一会话里修复。这是「条目边界验收」的落点。
- `todo_write` 已有依赖校验；补充 `skipped` 状态与「完成 A 后自动提示下一个 ready 条目」。
- 无 ready 且无在飞 → deadlock；有 `needs_replan` → 允许模型改写剩余项（版本 + 事件）。
- 预算：`plan_token_budget`（episode，0=不限）+ 每回合 `max_turn_tokens` + 墙钟；
  回合被 token 切断时 PlanExecutor 直接再开一回合（它才是 C3 的正确位置）。

**改动面**：`core/plan_executor.py`（新）、`TodoWriteTool` 注入 verify 回调（`ToolContext` 加一个
可选 async callable）、`domain/task.py`（skipped + evidence）、`handlers.py`/`commands.py` 切换、
前端计划卡（skipped / 进度）。**不改** AgentLoop 主循环、压缩、记忆、技能、工具栈。

**优点**：连续性最好，跨步知识零丢失；和 Codex/Claude/opencode 一致；实现面最小；
计划可在执行中自然改写；没有「每步重启」的体感。
**缺点**：transcript 持续增长，40 步计划的回合会很长（靠压缩 + 计划摘要重注入）；
单回合 token 成本高于链式短回合（实测约 +30%）；一个错误决定会污染后续条目（靠验收 + 证据收敛）。
**适用**：≤ 20 步、同一子系统、串行为主的计划（酒店 23 步属于偏大但可用）。

### 方案 B：Step-session + 结构化交接（改良现状）

**核心**：保留「每条目一个短会话」的骨架（token 最省），但修三件事：
seed 只给**本条目 + 直接依赖 + 上游产出**（不再重列全部未完成项）；交接用**结构化 StepNote**
（改过的文件、接口、决策、坑）；验收失败在条目内 repair，且允许一次 verify 修正。

```text
TaskDriver-like 调度（无 DAG 编译，soft ready queue）
  └─ 每条目：新会话 + StepNote seed → process_input → verify → 收据 → 黑板/交接
```

**优点**：上下文恒定、token 最省、收据最干净、天然可并行（B2 只读）。
**缺点**：结构化交接做不好就退回现状；「重启感」仍在；交接本身要设计、要测试；
跨条目推理（"上一步的接口改了我这步要跟着改"）依赖交接质量。
**适用**：> 25 步、上下文压力大、或需要并行调研的计划。

### 方案 C：Wave 连续 ReAct（混合）

**核心**：把计划按「共享文件/子系统/依赖链」切成 3–6 条目的波次；**波内用 A**（同一会话、
条目边界验收），**波间换会话**，用结构化 handoff 接上。波边界就是重规划与收据的天然位置。

```text
for wave in group(plan):
    session = fresh or compacted(parent)
    run A-loop over wave items (同一会话、无迭代上限、todo 验收)
    verify wave → receipts → handoff（files/interfaces/decisions/pitfalls）
    replan check（needs_replan / deadlock）
```

**优点**：兼顾连续性与上下文上界；重启次数从 N 降到 N/4；把 A 的 token 代价摊到可接受；
波边界是 replan/并行的挂点。
**缺点**：分组启发式（按文件冲突 + 依赖链）需要实现与调参；两层控制（波 + 条目）比 A 复杂；
波切分错了会丢连续性。
**适用**：20–50 步的大型计划；A 的规模化演进路径。

### 方案 D：DAG 调度（第一轮方案）

**核心**：把计划编译成任务图，ready queue 分派，读写并行，worktree + 合并队列。

**优点**：并行、文件所有权、可恢复、状态精确；acc-layer 的三个机制正好对上。
**缺点**：编码依赖运行时才暴露，图一编译就过时；机制成本高（调度器/黑板/合并队列/隔离）；
参考产品都没这么做；酒店那种计划没有可并行的独立写步骤，收益接近 0。
**适用**：明确的多 agent 写并行（B3）或只读调研并行（B2）。

## 5. 对比

| 维度 | A Plan-as-Goal | B Step-session | C Wave | D DAG |
|---|---|---|---|---|
| 连续性 / 跨步知识 | 最好（同 transcript） | 靠交接，易退化 | 波内最好，波间交接 | 靠黑板 |
| token 成本 | 较高 | **最低** | 中 | 中 + 合并开销 |
| 上下文上界 | 弱（靠压缩） | **强** | 强 | 强 |
| 实现改动 | **最小** | 中 | 中大 | 最大 |
| 与现有循环冲突 | 无 | 无 | 无 | 有（需新执行器） |
| 验收粒度 | 条目（工具内） | 条目（回合末） | 条目 + 波 | 条目 + 图 |
| 失败恢复 | 条目内修复 | 条目内 repair | 条目内 + 波重试 | 图重调度 |
| 并行能力 | 无 | 只读（B2） | 只读（波间） | 读写 |
| 计划演化 | 自然（todo_write） | 需 replan 钩子 | 波边界 | 图重编译 |
| 参考产品一致度 | **高** | 低（只有我们这么做） | 中 | 低 |

## 6. 推荐

**默认 A，规模化走 C，D 只用于并行。**

1. **A1（默认执行器）**：PlanExecutor + 同会话回合驱动 + `todo_write` 条目验收 + skipped +
   计划终结判定。目标：20 步内计划一次跑完，无人工 continue。
2. **A2（计划演化）**：`needs_replan` + deadlock → 改写剩余项（版本 + 事件）；前端提示。
3. **C（规模化）**：当计划 > 25 步或单回合上下文吃紧时，按波次切会话；A 的代码不变，
   只是在外面加一层 wave 循环与 handoff。
4. **B2（并行只读）**：调研类条目用 subagent 并行，结果写回计划证据；不改写步骤的串行性。
5. **B3（写并行，可选）**：如果真要做，才引入 §3 的三个机制（文件预留 + 合并队列 + verified 收据），
   **不引入 acc-layer 的运行时**。

**A 的关键不变量**（实现前先写测试）：

- 同一时刻最多一个 `in_progress`；`todo_write` 在条目完成时先跑 verify，失败不落状态。
- 计划终结 = 无 open 项；有 open 但无 ready = deadlock（不是「完成」）。
- 每个回合结束都把 task + 收据落盘；崩溃后 armed 任务可 resume。
- 预算只按 token/墙钟；迭代数不参与继续/停止判断。
- 更新计划必须留版本号与事件；收据里的 `verify` 必须真跑过。

## 7. 与现有代码的关系

| 现有 | A 方案里 | 说明 |
|---|---|---|
| `PlanRunner` | 保留为 `plan_orchestrator=legacy` | 旧测试与行为不动 |
| `TaskDriver` | A 不用；B 继续用 | 13 钩子协议只适合串行单步 |
| `AgentLoop._verify_acceptance` | 不改；A 走 tool 级 verify | 复用 `core.acceptance.verify_items` 与权限门 |
| `todo_write` | 扩展 verify 回调、skipped、next 提示 | 依赖校验已有 |
| 压缩 | 不改 | A 依赖它控制上下文；C 在波间重置 |
| 记忆 / 技能 | 不改 | 计划条目可携带 `skills` 提示（后续） |
| 前端计划卡 | 加 skipped / 进度 / replan 提示 | 事件沿用 `task.updated` / `plan.receipt` |

## 8. 待选

- 选 A：我按 §6 的 A1 先落地，最小改动、最快验证「不再每步停」。
- 选 C：先做 A1，再加 wave 分组；工期更长，但一开始就按大计划设计。
- 选 B：沿用现状骨架，先修 seed/交接/验收。
- 选 D：维持第一轮方案，需接受 DAG 与并行机制的复杂度。
