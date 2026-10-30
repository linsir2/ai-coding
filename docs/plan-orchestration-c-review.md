# 方案 C 评审：轮内 / 轮外数据流、参考实现复核、复用挂点审计（r24-review）

> 范围：不写代码，只做评审。对象是 [plan-orchestration-c-landing.md](plan-orchestration-c-landing.md)
> 与已落地的 C1（`skipped` + `wave_planner`）。结论先说：**C 可以继续，但现有稿有 6 个 P0
> 必须先修**；否则会把「每步重启」换成「每波漂移」，问题只是换了个位置。

## 0. 评审结论

1. **C 的方向成立**：主执行器仍应是 ReAct 回合，编排层只负责选条目、装配上下文、验收、持久化。
   参考产品没有一个用 DAG 做计划执行（见 §3）。
2. **现有落稿把「波」当成了固定批大小**，但波边界应由**验收/状态更新**触发，尺寸只是上下文预算。
3. **验收位置要改**：原稿写在 `todo_write` 工具内部；应改为「回合边界 + 现有 acceptance 机制」，
   否则要新增工具内 shell、权限、WAL、并行安全一整套问题（见 §4 P0-3）。
4. **`max_iterations=0` 不是一个开关**。代码有 8 处假设它 ≥1；本轮不支持 0，改用有限高上限
   + token/墙钟预算（见 §4 P0-1）。
5. **波会话是新会话，必须继承父会话的权限规则**，否则用户在计划里批的 allow 会全部失效
   （legacy 的 step 会话今天就有这个问题，见 §4 P0-2）。
6. **handoff 会被压缩吞掉**：长波里 seed 只是普通 user 消息，压缩后可能整段消失；需要一条
   每请求都注入的持久注记（见 §4 P0-4）。
7. **崩溃恢复粒度不足**：ephemeral 波会话不落盘、不写 WAL；一项做完但未收据时崩溃，会留下
   「状态 completed、没有收据」的半截（见 §4 P0-5）。
8. **模型不能自己把条目标成 `skipped`**：那等于允许它跳过工作；`skipped` 只能由执行器在依赖失败
   时写（见 §4 P0-6）。
9. acc-layer（并行正确性层）继续维持「串行不引入」的结论；它自己 README 就写明单 agent 不要用。
   只有将来做写并行时借三个机制（见 §5）。
10. **并行结论（已拍板）**：build 主闭环只加一层「显式并行调研工具」`parallel_research`
   （只读 `explore` 子代理，L1.5）；不做写并行、不做多 lane。计划链路 C2 串行跑通后，
   C3 复用同一个 `ResearchFanOut` 做 `kind=research` 波的 fan-out。
11. **默认执行器（已拍板）**：C2 落地后 `wave` 直接作为 `/plan run` 默认，旧 `PlanRunner`
    仅保留配置回退。

## 1. 轮内 / 轮外：控制权与数据流

### 1.1 轮内：一次 `process_input` 到底拥有什么

```text
process_input(session, text)                     agent_loop.py:848  （turn_lock 串行）
 └─ _run_turn                                    agent_loop.py:937
     ├─ _apply_pending_posture / reminders        （wait 到边界才生效）
     ├─ _begin_turn                              agent_loop.py:1484
     │   ├─ append user message（synthetic 可标）
     │   ├─ session.turn_count += 1               :1546
     │   ├─ session.continuations = 0             :1579
     │   ├─ deadline = now + turn_timeout_seconds
     │   └─ task 为空则建 TaskState(goal=输入)     :1548
     └─ while:
         ├─ policy.decide_next_iteration           :1026
         ├─ assembler.assemble                     :1151
         │   └─ 压缩 L3→L1→L2→L4 + memory/task/hook/digest/budget 尾部
         ├─ _model_step                            :1170
         ├─ _run_tools → dispatcher → checkpoint   :2126 / 2259
         └─ 模型不再要工具：
             ├─ _verify_acceptance（有 acceptance 才跑）    :1272
             ├─ policy.decide_after_step                    :1304
             │   └─ acceptance 失败 → 同一回合内 repair 提示（≤ max_acceptance_nudges）
             └─ 预算耗尽且还有未完成项 → C3 续跑（可关）
     └─ _finish_turn                               agent_loop.py:1702
         ├─ TurnCloser.close：handoff/digest/persist（ephemeral 跳过保存）
         ├─ memory harvest（后台）                 :1754
         └─ turn.end hooks
```

**轮内唯一能做的事**：迭代、装配、模型调用、工具分发、压缩、回合末验收、checkpoint。
**轮内不能做的事**：换条目、换会话、聚合跨回合预算、写收据、决定波边界。

关键事实（评审依据）：

- `process_input` 有 `_turn_lock`（:870），**不能并发调用**；外层只能串行驱动。
- 压缩发生在 `assembler.assemble` 内（:1151），只影响本回合的请求视图；outer 不能依赖消息下标。
- 验收只在「模型停止要工具」时跑（:1267-1272），**没有回合内/回合间的区别**；这正是
  「条目边界验收」的天然挂点。
- `last_outcome` 在回合关闭后才稳定（`TurnOutcome.status/reason/usage/text`），outer 读它。
- `ephemeral=True` 的会话：不保存（`turn_close.py:211`）、不写 WAL（`agent_loop.py:2162`），
  但 trace 仍挂到父会话（`_bind_trace` :2020）。
- `session.turn_count` 每回合 +1；PlanRunner 用它记 `StepDelivery.turns`。

### 1.2 轮外：一次 `/plan run` 拥有什么

现状（legacy）：

```text
/plan run → commands.py:593 → handlers.py:673 _launch_armed_run
  └─ PlanRunner.run                          plan_runner.py:558
      ├─ workspace 闸门 + arm + 落盘 + task.updated
      └─ TaskDriver.run                      task_driver.py:78
          ├─ next_item → 有 ready 项？        :104
          ├─ 轮数上限检查                     :111
          ├─ run_round → PlanRunner._run_step  plan_runner.py:976
          ├─ after_round → persist             :708
          └─ should_stop（r22 起恒 False）      :715
```

GoalRunner 是同一驱动器的另一 profile，**它就是「外层驱动多回合」的现成先例**：

- 每轮在**主会话**上 `process_input`（`goal_runner.py:248`），C3 关掉；
- `task.rounds_run` / `task.tokens_used` 做 episode 账本（:261-269）；
- 无进展 = 本轮没有任何 tool 消息（`_tool_calls` :343）；
- 停止在回合**之后**判定（:287）。

**轮外的职责（C 方案）**：

| 职责 | 落点 | 复用 |
|---|---|---|
| arm / disarm / 落盘 | `run()` 首尾 | PlanRunner.run / GoalRunner.run 模式 |
| 波次选择 | `next_item` 里 `group_waves` | `core/wave_planner.py`（C1 已落地） |
| 会话创建 + seed | `run_round` | `PlanRunner._make_step_session`（需修） |
| 条目提示 | `run_round` | `_step_prompt`（改措辞） |
| 条目验收 | 回合边界 acceptance + 回合后补验 | `AgentLoop._verify_acceptance`（需公开） |
| 结算 / 收据 | `run_round` 尾部 | `PlanRunner._settle` / `_record_on_parent` |
| skip 传播 | 结算失败后 | 新增 `TaskState.skip_dependents` |
| handoff | 波结束 | 新增 `WaveHandoff`（机械账本 + 模型报告） |
| 预算 | `after_round` / `should_stop` | GoalRunner 的 episode 账本 |
| 重规划 | 波边界 | 新增提示 + `replan` 计数 |
| 事件 / 控制台 | 全程 | `TaskUpdated` / `PlanStepReceipt` / `console.info` |

### 1.3 轮内 / 轮外的边界契约

| 事项 | 轮内（AgentLoop） | 轮外（WaveRunner / TaskDriver） |
|---|---|---|
| 迭代数 / token / 墙钟 | 拥有并按回合判定 | 只读 `last_outcome`，聚合 episode |
| 压缩 | 拥有 | 不碰；不依赖消息下标 |
| 工具分发 / 权限 | 拥有 | 不绕过 |
| 验收命令执行 | 拥有（回合末） | 触发：设置 `session.acceptance`；回合后可补验 |
| item 状态 | 工具（todo_write）在回合内改 | 结算/失败/skip 时改，并 emit |
| 持久化 | checkpoint（ephemeral 跳过） | 每个条目后 `save(parent)` |
| 事件 | turn.* / iteration / tool.* | task.updated / plan.receipt / console |

**四不要**：

1. 不要在一个回合运行期间换 `loop.policy`（legacy 是在回合前换、finally 换回，正确）。
2. 不要假设「一个回合只做了一个条目」；模型可能连续做完多个。
3. 不要用消息条数/下标判断进度；用 task 状态 + 收据。
4. 不要让外层和 C3 同时续跑；波内 `max_auto_continuations=0`。

### 1.4 build / legacy plan / wave plan 三路对照

| 维度 | build | legacy plan | wave plan（C） |
|---|---|---|---|
| 会话 | 主会话，持续增长 | 每步一个新 ephemeral | 每波一个新 ephemeral |
| 跨条目知识 | 全保留 | 仅 seed 摘要（丢失） | 波内全保留；波间 handoff |
| 回合数 | 模型自己停 | 每步 1-3 回合 | 每目 1-3 回合；波内连续 |
| 验收 | 回合末（可选） | 每步 acceptance | 每目 acceptance + 波末重验（仅重命令） |
| 预算 | 迭代/token/墙钟 | + 回合数/步数 | + 波 token/墙钟 + episode |
| 状态真相 | transcript | item + 收据 | item + 收据 + 波 handoff |
| 失败 | 模型自处理 | blocked 即整步 | 条目内 repair；blocked→skip；波边界 replan |

## 2. build 与 plan 的差异（复核，结论不变）

1. **状态归属不同**：build 的真相是 transcript；plan 的真相是 item + 收据。计划执行必须把
   「计划」外置成持久状态，模型每次请求都能看到（现有 `task.render()` 已做到）。
2. **上下文策略不同**：build 靠持续 transcript + 压缩；legacy plan 靠每步空会话（知识丢失）；
   wave 靠「波内连续 + 波间 handoff」。
3. **验收时机不同**：build 是可选合同；plan 必须逐条目机械验收。现状 `_verify_acceptance`
   已经是「模型一停就验」，所以条目边界验收不需要新机制。
4. **终止条件不同**：build 是模型停 + 门禁；plan 是「无 open 项」或「无 ready（deadlock）」。
5. **预算单位不同**：build 的迭代上限只是防失控；plan 的预算应是 token/墙钟，迭代数不参与
   「继续/停止」判断。

## 3. 参考实现复核（第二轮，带源码事实）

| 实现 | 事实 | 对 C 的直接改动 |
|---|---|---|
| **Codex** | `turn.rs` 是 harness 的 `loop`；有 tool call 就 `needs_follow_up=true`，无 tool call 才退；**没有 max_turns**；压缩可在回合中（`MidTurn`）发生；`update_plan` 只发 UI 事件，服务端不校验完成 | 回合由外层驱动、无迭代上限、压缩中途发生，都支持；`todo.completed` 不能当验收信号 → 必须机械验收 |
| **opencode** | 显式 step 机：`advanceToStep → runStep`，每 step 一次模型调用，历史每步从 DB 重载；`agent.steps` 可选，到限注入总结并禁工具；压缩在每个 step 开头可自动发生 | 「多回合同会话」可行；但每 step 重载历史说明「跨步知识靠 compaction + plan 文件」；我们的 wave 用同会话更连续 |
| **Claude Code** | plan 批准后**同一连续 session**；`TodoWrite` 只是 observability，completed 自报；验收靠模型/工具；压缩按 token 阈值（~95%），plan 文件从磁盘重注入 | 「plan 状态必须落盘并重注入」→ 我们的 handoff/task 渲染；没有 item 边界验收 → 我们的机械验收是增强 |
| **dsh** | `agent/pre-step` 只能改写/拒绝 user message；工具调用在 `tools/pre-execute` 才 allow/deny/ask；session 是 append-only log，history 由事件派生 | 「完成前验收」应做成工具 guard 或回合末机械验收；我们选后者（见 P0-3）；状态要外置 |
| **LongHorizon-Harness** | Manage-Execute-Audit：manager 只读外部任务状态 + 审计报告，executor 用 fresh context，auditor 只读独立检查，raw trajectory 丢弃；WeaveBench 51.8→80.7 | 支持「每目独立审计 + 外部状态」；我们的 wave 是连续与隔离的折中，必须保证每个条目都被独立验收 |
| **LongCLI-Bench / AdaPlanner** | 长程任务早失败；静态 plan 随 horizon 退化，需要 in/out-of-plan refinement | 波边界 replan、逐目验收、blocked→skip 都是必要的 |
| **Context rot / Lost-in-the-Middle / Cognition** | 长输入性能下降；中段信息最易丢；摘要交接丢「行动隐含的决定」 | 波不能太大；handoff 必须含 decisions/constraints/精确标识/未答请求/git 状态；不能只给摘要 |
| **Anthropic 多 agent** | 研究类任务 +90%，但 ~15× token，且官方说多数 coding 任务不适合多 agent | 继续串行；只读调研才并行 |

**结论**：C 的「波内连续、波间交接」与参考实现一致；需要补的是**外部状态、逐目审计、
handoff 的固定字段**，不是换调度拓扑。

### 3.5 并行阶梯：build 主闭环要不要并行

把「并行」拆成四层，才能回答用不用得上：

| 层 | 内容 | 现状 | 风险 | 结论 |
|---|---|---|---|---|
| **L0 并行只读工具** | 同一批里连续的 `read/glob/grep/websearch/webfetch` 并发 | ✅ `dispatch.py:347-370`（ALLOW + parallel_safe，`max_parallel` 限流） | 低 | 保留，不动 |
| **L1 并行只读子代理** | 同批多个 `subagent` 调用并发，各自 worktree，结论回父 | ✅ `subagent.py:206`（信号量）、`subagent_tool.py:124`（parallel_safe） | 低 | 保留；计划链路可做确定性 fan-out |
| **L2 并行写子代理** | 各自持久 worktree + 分支 → 合并队列 → 重验 | ❌ 现 worktree 是 `temporary()`，写完即删（`worktree.py:129`） | 高：合并冲突、验证时序、文件所有权 | 推迟；只在计划链路、且文件不相交时考虑 |
| **L3 多会话/多 lane** | 多个会话同时写同一工作区 | ❌ | 很高：审批、undo、checkpoint、压缩都是 per-session | 不做 |

**build 主闭环的答案：已经并行，不需要再加。**

- L0/L1 今天就在：工具批次自动分组并发，子代理有信号量。build 缺的不是并行，而是模型是否
  主动 fan-out；那是提示词问题，不是编排层问题。
- build 的 `_turn_lock`（`agent_loop.py:870`）、单 transcript、审批通道、undo 与 checkpoint 都是
  **per-session** 语义；加多条对话 lane 会同时破坏这四件事，收益只是延迟。
- 写并行的收益是延迟，不是正确性；编码步骤靠编译/测试反馈耦合，静态并行经常白做。
  Anthropic 的多 agent 研究系统 +90% 但约 15× token，且官方明确多数 coding 任务不适合；
  Cognition 直接主张不要为编码建多 agent。
- 现有子代理 worktree 用完即删，**写回本来就被丢弃**——它今天是 L1（研究），不是 worker。
  要升到 L2，必须先做持久 worktree + 文件预留 + 合并队列，不能只在计划层「开个 gather」。

**所以：先改造计划链路；并行只在计划链路里从 L1 起步，L2 明确推迟。**

按你的选择，build 主闭环额外加一层 **L1.5：显式并行调研工具**（仍然只读），见 §3.6；
L2 写并行与 L3 多 lane 仍然不做。

### 3.6 build 并行调研（B0）：`parallel_research`

**要解决什么**：build 里模型常有 2–4 个**互不依赖**的调研问题（"路由在哪注册"/"测试怎么跑"/
"这个接口谁在用"）。现在只能发多次 `subagent` 调用，父上下文被复制 N 份、结果要自己拼。
一个新工具把「fan-out + 结构化输出 + 合并」做成一次调用。

**工具契约**

```json
{
  "questions": [
    {"id": "q1", "question": "路由在哪里注册？", "scope": ["app/", "src/"]},
    {"id": "q2", "question": "现有测试怎么跑？", "scope": ["tests/"]}
  ],
  "context": "可选：共享背景（目标/约束）"
}
```

- `questions` 2–4 条（1 条请直接用 `subagent`），每条一个只读 `explore` 子代理；
- 返回一个合并报告 + `data.results[]`（每条：`id/status/summary/key_files/findings`）；
- 单条失败不拖垮整次调用：其余结果照常返回，失败条目带 `status/error`；
- 全部失败才 `success=False`。

**实现落点**

```text
core/research_fanout.py（新）
  ResearchQuestion(id, question, scope)
  ResearchPacket(id, status, summary, key_files, facts, usage, error)
  ResearchFanOut(runner).run(questions, parent, observer, parent_span)
     └─ asyncio.gather(*[runner.run(..., agent="explore", isolate=True,
                                    context_mode="task_only",
                                    output_schema=RESEARCH_SCHEMA)])
        # 并发上限由 SubAgentRunner 的信号量负责，这里不再加 semaphore

tools/parallel_research_tool.py（新）
  name = "parallel_research"
  parallel_safe = True
  mutates_world = False
  permission: ALLOW（加进 permission_gate._allow_tools）
  可见性：build 与 plan 都可见（只读）
```

**为什么用 `explore` 而不是 `general`**：`explore` 是内置只读子代理，白名单只放
read/glob/grep 一类（`agents/registry.py:45` 的 `_EXPLORE_ALLOWED`），写工具与 bash 都被拒；
配合临时 worktree，调研不可能改仓库。

**必须有的约束**

1. **每子代理 `task_only` 上下文**：否则 N 个子代理各带一份父历史，token ×N。需要在
   `SubAgentRunner.run` 增加 `context_mode` 参数（默认沿用全局配置），fan-out 强制
   `task_only`，并把目标/约束写进每条 brief。
2. **条数与输出上限**：2–4 条；每条 summary ≤ 600 字、key_files ≤ 8 个；合并结果 ≤
   `max_result_data_bytes`（8k）。
3. **不并发写**：`explore` 规则 + 临时 worktree 双保险。
4. **预算**：每个子代理独立 usage，工具把总 usage 放进 `data`；**只报告，不纳入父回合
   token 预算**（用户拍板）。父账本不变，花费看 `data.usage` / `/budget` / trace。
5. **只读硬开关**：`readonly=True` 落到子会话，工具面剔除 mutating 工具；即使项目覆盖
   `explore`，fan-out 仍然写不了。配合临时 worktree 是三层边界。
6. **重复调用守卫**：同一批问题重复调用会被 `core/guard.py` 的重复守卫挡住（已有机制）。
7. **前端**：`toolmeta.ts` 加标签「并行调研」；toolcard 显示每个子问题的状态。
8. **权限**：加入 ALLOW 清单，否则每次调研都弹审批；子代理内部仍是 AutoApprove + DENY 规则。
9. **计划模式不放开委派**：plan agent 的 `_PLAN_SOFT_DENY` 已禁 `subagent`；`parallel_research`
   同样加入该层，否则等于绕过「plan 模式不委派」的政策。`explore` 的白名单也不含它，
   因此子代理不能递归 fan-out。

**测试**

- 3 问 → 3 个 packet，合并去重；1 个失败 → 其余返回；
- 5 问 → 拒绝；1 问 → 提示改用 `subagent`；
- 子代理只能拿到 task_only 摘要（用假 runner 断言）；
- `explore` 规则下写/编辑/bash 被拒；
- 并发不超过 `max_concurrent_subagents`（假 runner 记录峰值）；
- usage 汇总正确；父回合 token 账包含子代理。

**C3 复用**：计划里的 `kind=research` 波直接调用同一个 `ResearchFanOut`，不再重复实现。


## 4. 方案 C 的问题清单

### P0（必须修，否则不实现）

**P0-1 `max_iterations=0` 不支持。**
`TurnPolicyConfig.from_ai_config` 是 `max(1, …)`（`turn_policy.py:142`）；`iteration > max` 判停
（:381）；plan 收尾用 `max_iterations - iteration`（:371）；预算压力按比例（:587）；静态检查
`index < max_iterations`（`agent_loop.py:1110`）；`_has_a_round_left` 是 `max(1, …)`（:2357）；
收尾调用用 `max + 1`（:2541/2577）。
**修法**：本轮不做 0。波内沿用 `plan_step_max_iterations`（默认 60），把「无上限」交给
token/墙钟与波预算。若将来真要做 0，必须一次改这 6 处并加测试。

**P0-2 波会话不继承父会话权限。**
会话规则在 `session.permissions`（`domain/session.py:411`），`_session_rules` 读它
（`agent_loop.py:2112`）。`PlanRunner._make_step_session`（`plan_runner.py:853`）新建空 session，
**没有复制 permissions**，所以用户在计划执行前批的「本会话允许」在新会话里丢失。
**修法**：波会话创建时 `wave.permissions = list(parent.permissions)`；`agent="build"`、
`readonly=False`、`task=parent.task`、`ephemeral=True`。legacy 也建议同步修。

**P0-3 验收位置：工具内 vs 回合边界。**
原稿让 `todo_write(completed)` 先跑 verify。问题：
- `TodoWriteTool` 声明 `mutates_world=False / parallel_safe=True`（`tools/todo_write_tool.py:24-25`），
  跑 shell 会推翻这两个事实；
- 工具拿不到 `PermissionGate` / `ApprovalCallback`，要在 `ToolContext` 里加回调，并处理
  readonly/规则/工作区外审批；
- 工具内 shell 不在 WAL/checkpoint 语义内，失败/超时更难收尾；
- 并行工具批次里跑长命令会阻塞同批。
**修法**：用回合边界。外层在每次 `process_input` 前设置
`session.acceptance = [AcceptanceItem(id=f"plan-{item.id}", statement=..., verify=...)]`，
回合末 `_verify_acceptance` 自动跑；失败会由 acceptance ladder 在同一回合内 nudge repair。
`max_acceptance_nudges=2` 就是条目内修复预算。
**补口**：一个回合可能做完多个条目；外层在回合后用公开的
`AgentLoop.verify_acceptance(session)`（把 `_verify_acceptance` 公开、books 可选）补验其余
已 completed 的条目。

**P0-4 handoff 会被压缩吞掉。**
波 seed 只是一条普通 user 消息；波内压缩 L1/L2/L4 可能把它删掉或摘要掉，后半个波就丢了
decisions/interfaces。
**修法**：handoff 分两层——
- 种子：完整 `WaveHandoff` 放 seed（首回合可读）；
- 常驻：`TaskState.wave_note`（新可选字段，≤800 字），随 `task.render()` 每请求注入。
  内容只放「跨条目必须遵守」的 decisions/接口/文件所有权，不放过程叙述。

**P0-5 崩溃恢复粒度不足。**
ephemeral 波会话不保存、不写 WAL（`turn_close.py:211`、`agent_loop.py:2162`）；父会话在
legacy 里只在 `after_round` 存。崩溃的真实后果有两类：
(a) **重复劳动**：条目已改完、模型已标 completed，但父会话还没存 → 磁盘上仍是 pending，
resume 会重跑（安全，但浪费）；
(b) **假完成**：如果执行器先把 `completed` 存盘、再写收据就崩溃 → 磁盘上「completed 无收据」，
下次 resume 会当它已完成而不再验（危险）。
**修法**：
- 结算顺序固定：`task.update(...)` → `step_deliveries.append(...)` → **一次** `save(parent)`；
  中间不存盘，杜绝 (b)；
- resume 时对「completed 但无收据」的条目做一次 `verify` 补验，通过则补收据，失败则退回
  `in_progress`；
- 波会话不落盘是接受的取舍：重跑从该波第一个未完成条目开始。

**P0-6 模型不能自己写 `skipped`。**
`todo_write` 的 schema 直接取自 `VALID_STATUSES`，加进 `skipped` 后模型可随意跳过条目。
**修法**：`TodoWriteTool._update` 对 `status="skipped"` 直接返回错误（「skipped 由执行器在依赖
失败时设置」）；只有 runner 通过 `TaskState.update` 写。

### P1（应修，影响质量/成本）

**P1-1 波边界规则。**
固定 4 条只是上下文预算，不是逻辑边界。调整为：
1. 重验收命令（`pytest/make/npm test/...`）之后必关；
2. `kind in {research, verify}` 的条目单独成波（调研结果必须先落 handoff）；
3. 尺寸上限（默认 4，可配 3–6）作为上下文预算；
4. 波内仍按列表顺序 + 依赖就绪过滤。
`group_waves` 现在只做了 1/3/4（C1），需要补 2。

**P1-2 波末不要重跑全部 verify。**
逐目已经验过；波末只重跑「重命令」或 `kind="verify"` 的条目。否则 4 个条目 × pytest
会把成本翻倍。

**P1-3 前端 skipped / 进度。**
`message.tsx` 的 `marker()` 没有 `skipped`（落到 `[ ]`）；`done()` 只数 completed，
skipped 会让「Todos x/y」看起来永远差一项。
修法：`[~]` + muted；进度改为 `completed/total`，另起一句「跳过 n（原因：依赖失败）」；
`state/session.ts` 的 `applyPlanUpdate` 不用改（状态原样透传）。

**P1-4 `wave_report` 的可见性与权限。**
新工具默认是 unknown tool → WARN → 弹审批（`permission_gate.py:327` 的 allow 清单）。
需要：加入 allow 清单（它只写会话状态）；新增 `plan_run_only` 可见性并在
`_surface_schemas` 的 key 里加 `session.plan_wave`（`agent_loop.py:3223`），否则缓存会把旧
schema 发给下一轮。**替代方案**：不加工具，handoff 的模型部分用波末最后一个回合的文本 +
机械账本；质量不够再引入工具。

**P1-5 skip 传播与 replan。**
- 规则：任一依赖 `skipped` → 该条目 `skipped`（`Blockers.skipped` 已实现），传递闭包。
- `needs_replan` 只能由模型在 `wave_report` 里声明，或波边界 deadlock；上限 2 次；
  改写后 `plan_version += 1`，发 `plan.updated` + notice。
- replan 的回合仍要过 workspace 闸门（防止新条目写到工作区外）。

**P1-6 报告与收据。**
`PlanReport` 需要 `waves` / `skipped` / `replanned`；`StepDelivery.status` 增加 `skipped`
（`proof` 对它返回 `n/a`，正确）；`render()` 要说明「跳过 n 项（依赖失败）」。

**P1-7 预算账本落点。**
`task.tokens_used` 是 goal episode 的账（`domain/task.py:133`，`arm_goal` 重置）。
波执行不要复用它，避免和 GoalRunner 互相污染。新增可选 `planTokensUsed`（或只在进程内记账，
明确 resume 后从 0 重算）。建议新增字段，`plan_token_budget>0` 时才使用。

**P1-8 波会话的记忆收割。**
`_schedule_harvest` 没有 ephemeral 检查（`agent_loop.py:1754`），波内每个回合都会触发一次
记忆抽取（LLM 调用 + 可能写入内部步骤噪声）。
修法：波会话跳过 harvest（和 `save`/WAL 同一判断），或只在波结束用一个合成摘要收割一次。

### P2（记录，暂不处理）

- 波会话无 WAL：工具副作用若在崩溃时未完成，文件状态可能半写；legacy 同样存在，接受。
- `group_waves` 依赖列表顺序是拓扑序；legacy 手改数据可能乱序，`ready_in_wave` 会兜底但
  波切分不最优。
- 事件没有 `plan.wave`；用 console notice 已能看见，暂不加 wire 类型。
- trace 中波会话挂在父 session id（`_bind_trace`），可接受。
- `plan.updated` 的 `items` 会带 `files/kind/attempts`，前端忽略未知键，安全。

## 5. acc-layer 复审

**它是什么**：多 agent 并发写同一仓库的正确性运行时（RSC = OCC + 租约 fencing + 短临界区 +
Saga 补偿）。25k 行、纯标准库、事件日志 + WAL + 预算树 + 收据链 + 恢复。

**它不是**：单 agent 的计划执行器；也没有「计划/波次/验收」这一层。

README 自己写明（`/tmp/acc-layer/README.md:112-113`）：

> 单 agent、写入不重叠、只要 trace 的场景，用 git worktree 或 Langfuse 更合适。

| 模块 | 我们的对应 | 结论 |
|---|---|---|
| `Harness` / `ACB` / `EventLog` / WAL | 我们的 `AgentLoop` / `SessionData` / `core/events` / `write_ahead` | 不引入：对象图与线程模型完全不同 |
| `facade.begin/apply`（`facade.py:334-459`） | 工具写文件；git 是真相 | 仅概念 |
| `workspace.stage/_validate_locked`（`workspace.py:248-306`） | 无共享 manifest | 写并行时才需要：文件预留 + 读集版本 |
| `commit_batch`（`kernel/commit.py:76-94`） | 无 | 仅概念 |
| `merge_queue`（`merge_queue.py`） | 无 | asyncio 版 ~80 行可自研 |
| `BudgetManager`（`budget.py:1-14`） | token/墙钟单调账本 | 不引入：预算树是并发预留，不是消耗上限 |
| `Receipt`（`spec/receipt.py:45-60`） | `StepDelivery` | 借「verified/on-claim」语义，已有 |
| checkpoint / recovery / effect journal | session 持久化 + WAL | 单进程不需要补偿日志 |
| `Scheduler`（`orchestration.py:801-834`） | `wave_planner` ready 过滤 | 仅概念；我们不需要 claim/lease |
| `WorktreeManager`（`worktree.py`） | `core/worktree.py`（async，更贴合） | 不引入 |
| `semantic.py`（AST 引用检查） | 无 | 可选：Python 仓库的提交前静态门 |
| TLA+ / conformance | 无 | 借不变量清单做测试设计，不借代码 |

**如果将来做写并行**，最小实现（~300 行 asyncio，不 vendor）：
1. `FileReservation`：commit 前钉住 `path → (hash, mtime)`，提交时比对，冲突则 rebase/重跑；
2. `MergeQueue`：`asyncio.Queue`，出队 → 在最新工作树上重跑该步 verify → 提交（关 TOCTOU）；
3. verified receipt：`StepDelivery.proof` 已经是这套语义，扩一个 `merge` 字段即可。

## 6. 复用挂点审计

| 挂点 | 现有实现 | C 方案怎么用 | 状态 |
|---|---|---|---|
| 外层驱动 | `TaskDriver`（`task_driver.py:78`） | 新增 `WaveRunner` 实现 `RoundProfile`，不写新循环 | ✅ 直接复用 |
| arm/disarm/落盘 | `PlanRunner.run`（`plan_runner.py:558`） | 照抄生命周期 | ✅ |
| workspace 闸门 | `outside_workspace_paths`（:222） | 直接调 | ✅ |
| 波分组 | `wave_planner`（C1） | 补 `kind` 规则 | ⚠️ 小改 |
| 会话创建 | `PlanRunner._make_step_session`（:853） | 加 permissions/plan_wave/wave id | ⚠️ 必改（P0-2） |
| seed | `PlanRunner._seed`（:886） | 换成 wave seed + handoff | ⚠️ 重写 |
| 条目提示 | `_step_prompt`（:1219） | 改措辞为「本目完成即停」 | ✅ |
| policy 交换 | `continuations_disabled`（:455） | 复用（60/natural/C3=0） | ✅ |
| 条目验收 | `session.acceptance` + `_verify_acceptance`（`agent_loop.py:2449`） | 公开化 + books 可选 | ⚠️ 必改（P0-3） |
| 回合后补验 | 无 | 新增公开 `verify_acceptance` | ⚠️ 新 |
| 结算/收据 | `_settle`（:1148）/ `StepDelivery` | 加 skipped、波字段 | ⚠️ 小改 |
| 父会话气泡 | `_record_on_parent`（:1081） | 直接复用 | ✅ |
| skip 传播 | 无（`Blockers.skipped` 已有） | 新增 `skip_dependents` | ⚠️ 新 |
| handoff | 无 | `WaveHandoff` + `task.wave_note` | ⚠️ 新（P0-4） |
| episode 预算 | GoalRunner（:261-307） | 复用模式；新增 `planTokensUsed` | ⚠️ 新字段（P1-7） |
| 无进展保护 | GoalRunner `_tool_calls`（:343） | 每目 2 次无进展 → blocked | ✅ 模式复用 |
| 事件 | `TaskUpdated` / `PlanStepReceipt`（`bridge.py:167/247`） | 直接复用；skipped 自动随 `to_dict` 流出 | ✅ |
| 前端计划卡 | `message.tsx:107-130` | 加 `[~]` + 跳过计数 | ⚠️ 必改（P1-3） |
| 前端 reducer | `applyPlanUpdate`（`session.ts:892`） | 不用改（未知键忽略） | ✅ |
| 工具可见性 | `_surface_tools`（`agent_loop.py:3168`） | 新 `plan_run_only` + cache key | ⚠️ 若加 wave_report |
| 权限 allow 清单 | `permission_gate.py:327` | 加 `wave_report` | ⚠️ 若加工具 |
| 命令切换 | `commands.py:593` / `handlers.py:673` | 按配置选 WaveRunner / PlanRunner | ✅ |
| boot resume | `handlers.py:692` | profile=plan → WaveRunner | ✅ |
| 配置 | `config/models.py:446-470` | 新增 6 键 | ⚠️ 小改 |
| 并行只读工具 | `dispatch.py:347-370` | build 与波会话都自动生效 | ✅ 不动 |
| 子代理并发 | `subagent.py:206` 信号量 / `subagent_tool.py:124` | C3 直接复用，不再加 semaphore | ✅ |
| runner 可达性 | runner 藏在 `SubAgentTool` 内 | 需要 `AgentLoop.subagent_runner` 或工具只读属性 | ❌ 新挂点 |
| 调研 fan-out | 无 | C3 的确定性 `gather` | ❌ 新 |
| 持久 worktree / 合并队列 | `worktree.temporary()` 用完即删 | L2 才需要；本轮不做 | ⏸ 推迟 |
| 测试 | `test_plan_*` | 旧全保留 + 新波用例 | ✅ |

**没有遗漏但需注意的现有事实**：

- `todo_write` 完成后会自动 `TaskUpdated`（`agent_loop.py:2249-2258`），波执行器不用再发。
- `_run_tools` 每批都会 checkpoint（:2259），但 ephemeral 不写 WAL（:2162）。
- `StepDelivery` 的 `proof` 已区分 verified/on-claim/fiat（`plan_delivery.py:49-71`）。
- `present_plan` 在 mode=normal 时不可见（`plan_only`，`agent_loop.py:3171`），执行会话不会误调。
- `@goal` 的工具 `start_goal/update_goal` 在 armed=="plan" 时被隐藏（:3169），波会话安全。

## 7. 修正后的 C 执行流程

```text
WaveRunner.run(parent_session)                    实现 RoundProfile；TaskDriver 驱动
 ├─ workspace 闸门 / arm / persist / task.updated
 ├─ TaskDriver.run(parent, self)
 │   ├─ next_item: waves = group_waves(task)；取第一个未尝试且有 open 项的波
 │   ├─ run_round(wave):
 │   │   ├─ wave_session = new ephemeral SessionData
 │   │   │     task=parent.task, permissions=parent.permissions, agent=build,
 │   │   │     plan_wave=True, messages=[seed(goal, handoff, wave.items)]
 │   │   ├─ for item in wave (ready 优先):
 │   │   │   ├─ before: acceptance=[plan-id]（有 verify 时）; policy=60/natural/C3=0
 │   │   │   ├─ process_input(wave_session, "本目：... 完成即停")
 │   │   │   ├─ after: 读 last_outcome + acceptance
 │   │   │   │    ├─ verify 通过 → completed + evidence + receipt + save(parent)
 │   │   │   │    ├─ verify 失败且 attempts<2 → repair 回合（同一 session）
 │   │   │   │    └─ 仍失败 → blocked + skip_dependents + receipt
 │   │   │   └─ 无 verify：completed 则收据；否则一次「收尾或申报 blocked」回合
 │   │   ├─ 补验：本回合内被模型额外做掉的 completed 条目
 │   │   ├─ 波末：重命令/verify-kind 重验一次；不做全量重跑
 │   │   ├─ handoff = 机械账本（artifacts + verify 结果）+ wave_report（模型）
 │   │   ├─ task.wave_note = handoff 摘要；persist
 │   │   └─ wave_session 丢弃
 │   ├─ after_round: rounds_run += 1; planTokensUsed += usage; persist
 │   ├─ should_stop: 波预算/墙钟/plan_token_budget/无进展/replan 上限
 │   └─ no_item: open 但无 ready → deadlock → 一次 replan（≤2）或报告
 └─ finally: disarm + persist + task.updated
```

**预算**：波 token（每回合 usage 累加）、波墙钟、episode token/墙钟、每目 attempts。
**不做**：0 迭代上限、工具内 shell、DAG 编译、跨波累计 handoff。

### 7.5 计划链路的并行落点（C3，只读 fan-out）

**先 C2 串行跑通，再加 C3。** 并行只给「调研类」波，写条目永远串行。

```text
if wave.items 全部 kind == "research":
    # 不开波会话、不跑模型回合：直接并发子代理
    packets = await asyncio.gather(*[
        runner.run(
            brief(item, handoff),            # 目标 + 已知约束 + 要回答的问题
            isolate=True,                    # 临时 worktree，写回丢弃
            output_schema=RESEARCH_SCHEMA,   # {summary, key_files, findings}
            parent=parent_ctx,               # 权限 + 父上下文
        )
        for item in wave.items
    ])
    for item, packet:
        ├─ packet.status != completed / truncated → blocked（依赖项 skip）
        ├─ usage 累加进波/episode 账
        ├─ task.update(completed, evidence=summary)
        └─ handoff.merge(packet.facts)       # findings/key_files → decisions/files
    continue                                 # 下一波
else:
    串行波会话（§7 主流程）
```

约束：

1. **并发上限不要再加一层**：`SubAgentRunner` 自带 `max_concurrent_subagents` 信号量
   （`subagent.py:206`），执行器只 `gather`，不另设 semaphore。
2. **只读是契约**：临时 worktree 在退出时删除（`worktree.py:129`），调研子代理即使写了也会丢；
   指令里要明说「只读调研，不要改文件」。
3. **机械账本要换路径**：`SubagentResult.artifacts` 可能是 worktree 内路径，合并进 handoff 前
   必须映射回仓库相对路径；调研类建议只用 `facts.key_files`，不采 artifacts。
4. **失败隔离**：一个调研失败不终止波；标 blocked、依赖项 skip，其余结果照常合并。
5. **runner 可达性**：现在 `SubAgentRunner` 藏在 `SubAgentTool` 里，执行器拿不到；需要
   `AgentLoop.subagent_runner` 属性或 `SubAgentTool.runner` 只读属性（审计新增挂点）。
6. **写并行（L2）推迟**：需要持久 worktree + 文件预留 + 合并队列；见 §5 的最小实现。
   在 L2 之前，计划里的写条目永远串行，哪怕它们文件不相交。

## 8. 与先前决策的冲突（需要你确认）

1. **执行形态**：你先选了「DAG + 黑板」，本轮已改为 C；DAG 仅保留在文档 B 里作参考。
2. **step 视图**：你先前定「只显示本步 + 依赖」。波方案里，请求的 task 块仍是**全计划**
   （`task.render()`，≤40 项，成本很小），只有 seed 聚焦本波。要不要把注入的 task 也裁到
   「本波 + 依赖」？我建议**不裁**：全计划是跨条目一致性的来源，且波内连续性正是 C 的价值。
3. **无迭代上限**：你选的是「无迭代上限 + 目标预算」。评审结论是当前代码做不到 0，建议
   波内用 60 的有限上限 + token/墙钟；「无上限」作为后续独立改造（6 处 + 测试）。
4. **验收位置**：原稿 `todo_write` 工具内 → 评为回合边界 acceptance。这是设计修正，不改变
   「逐目机械验收」的目标。

## 9. 决策状态

**已拍板**

| 决策 | 结果 |
|---|---|
| 6 个 P0 修正 | 全部接受 |
| build 主闭环并行 | 加 `parallel_research`（只读 fan-out，L1.5）；不做写并行、不做多 lane |
| 计划链路节奏 | C2 串行跑通 → C3 `kind=research` 波只读 fan-out |
| 默认执行器 | `wave` 直接默认，`legacy` 仅配置回退 |
| L2 写并行 | 本轮不做 |

**采用推荐默认（可随时改）**

- 波默认大小 4（3–6 可调）；边界 = 重命令 / `kind in {research, verify}` / 尺寸。
- handoff 两层：full packet 进 seed，`task.wave_note`（≤900 字）每请求注入。
- `plan_wave_token_budget=300k`（原稿 150k 的修正：酒店 23 项实测约 1.03M tokens，
  单回合可到 ~200k；150k 会在第一波中段切断。每回合的 `max_turn_tokens` 夹到
  「剩余波预算」，所以单回合不会再把 150k 花成 1M）。
- `plan_wave_wall_seconds=1200`、`plan_max_item_attempts=2`、`plan_max_replans=2`、
  `plan_token_budget=0`（不限）。
- 波会话 `ephemeral=True`，继承父 `permissions`，`agent="build"`。
- 波内 `max_iterations=60`（有限上限，不做 0）；C3 关闭，由执行器驱动回合。

**仍需你定 → 已确认**

| 决策 | 结果 |
|---|---|
| `wave_report` 工具 | **C3 再加**；C2 用机械账本 + 波末回复文本 |
| 条目 repair 上下文 | **同一会话**，注入失败输出继续修（复用 acceptance ladder） |

## 10. 实施阶段与验证

**实施状态（C2 已落地）**

- 领域：`skipped`（模型不可写）、`skip_dependents`、`wave_note`、`planTokensUsed`。
- 执行器：`core/wave_runner.py`（`TaskDriver` 的 `RoundProfile`）；波会话继承权限、
  回合边界 acceptance、逐目收据、blocked→skip、机械 handoff、波/回合预算夹紧、
  断点补验收据。
- 切换：`plan_runner_for` 按 `plan_orchestrator` 选型，默认 `wave`。
- 前端：`[~]` skipped 与「跳过 n」。
- 门禁：全部后端测试、ruff、mypy、前端 typecheck 与 576 个前端测试通过。

**阶段（C2 已完成；B0 与 C3 待做）**

1. **C2（串行波执行器）**：✅ 已落地。
2. **B0（build 并行调研）**：`ResearchFanOut` + `parallel_research` + 权限/前端标签 + 测试。
3. **C3（计划并行与演化）**：research 波复用 `ResearchFanOut`；`wave_report`；波边界 replan；
   前端波次进度。

**验证**

- 纯函数：`group_waves` 的 kind/重命令/尺寸/顺序；`skip_dependents` 闭包。
- 轮内：acceptance 失败 → 同一回合 repair；模型一次做多条目 → 外层补验。
- 轮外：2 波 5 目；波预算切断后下次续跑；blocked→skip→独立项继续；deadlock→replan 上限。
- B0：3 问 → 3 packet，1 失败不影响其余；1 问拒绝；5 问拒绝；task_only；只读；
  并发峰值 ≤ `max_concurrent_subagents`；usage 汇总。
- C3：3 个调研条目 fan-out，1 个失败 → blocked、依赖 skip、其余结果合并；写条目仍串行。
- 集成：23 步假模型录像；`/resume`；boot resume；`legacy` 与 `wave` 同计划结果对照。
- 回归：全量 `test_plan_*`、前端 plan 卡片、权限面板。
