# 计划执行器：与 Codex 的差距盘点（目标相似度 ≥ 90%）

> 依据：`/tmp/codex`（openai/codex，depth=1）逐文件核对 + 本项目全链路逐文件核对。
> 本文只做对照，不写实施代码；复核后的权威设计与分期见第 8–13 节。

## 0. 结论先说

Codex **没有"计划执行器"**。它的 plan 是：

- 一个工具 `update_plan`（只有 `step` + `status`，回灌固定文本 `"Plan updated"`）；
- 一个 collaboration mode（`/plan` 只是切模式，靠 developer 指令约束，不改沙箱/工具）；
- 执行 = **普通回合**，续跑由 `needs_follow_up`（有工具调用 / 有 pending input）决定；
- 批准 = 切回 Default 并提交一句 `"Implement the plan."`；
- 完成度**没有任何门控**：计划没做完也照常结束，剩余项只在 TUI 卡片上展示；
- 压缩会**丢掉** `update_plan` 的历史（只保留 summary + 最近 user 消息），不做计划状态恢复。

我们目前是一套**专用执行器**：`PlanExecutor` 单回合 + `PlanRunner` legacy + `TaskDriver`、
`armed_profile`、workspace 门、条目标记完成时验收、receipt/`StepDelivery`、`PlanReport`、
`<plan-state>` 易失尾块、idle/stall 双刹车、`/plan run` 自动派发。

按 24 个行为维度逐项打分（SAME=1、PARTIAL=0.5、DIFF=0，见第 3 节）：

| 口径 | 得分 |
|---|---|
| 当前实现 vs Codex | **约 23%**（2 SAME / 7 PARTIAL / 15 DIFF） |
| 只对齐"执行循环"（B 方案已做） | 约 45% |
| 本文件第 9 节全部落地后（默认 codex 路径） | **93.75%**（21 SAME / 3 PARTIAL：只读强制、一行收尾、子代理 4/7） |

差距主要不在循环（循环已接近），而在**执行器本身存在、且带了一整套 Codex 没有的机制**。

### 0.1 已确认的实施口径（2026-11-01）

| 决策 | 取值 | 影响 |
|---|---|---|
| 验收/回执/报告 | **默认关闭，保留开关**：`plan_orchestrator="codex"` 时全部不启用；`"session"` 保留旧行为（verify + `StepDelivery` + `PlanReport`） | 默认路径与 Codex 一致；旧能力不丢，但只在显式切换时出现 |
| 旧执行器删除 | **先新增 Codex 路径，验证通过后再删** `PlanExecutor`/`PlanRunner`/`TaskDriver` 计划分支 | 实施分两批，每批可回退 |
| 子智能体 | **本轮纳入设计**：工具名、`fork_turns`、完成回灌方式按 Codex 对齐，复用 `SubAgentRunner` | 见第 9 节 C-5 |
| 本轮代码 | **只出设计，不改代码** | 本文即交付物 |

## 1. Codex 的 plan 全链路（证据）

| 环节 | 事实 | 位置 |
|---|---|---|
| 计划工具 | `update_plan`，参数 `{explanation?, plan:[{step,status}]}`，status ∈ pending/in_progress/completed，**无 id / 无 verify / 无依赖** | `codex-rs/protocol/src/plan_tool.rs:9-28`、`core/src/tools/handlers/plan_spec.rs:7-57` |
| 工具回灌 | 恒返回 `"Plan updated"`，success=true；只发 `EventMsg::PlanUpdate` 事件，不维护会话计划状态 | `core/src/tools/handlers/plan.rs:22,33-41,94-96` |
| Plan 模式内调用 | 在 Plan 模式下调用 `update_plan` **直接返回错误** | `core/src/tools/handlers/plan.rs:87-90` |
| 计划模式 | `/plan` → `apply_plan_slash_command` → `collaboration_modes::plan_mask`；只改 collaboration_mode，**不改 sandbox/approval/工具集** | `tui/src/chatwidget/slash_dispatch.rs:379-381,87-105`、`settings.rs:765-783` |
| 计划模式的硬约束 | developer 指令（`collaboration_mode.rs`）+ `update_plan` 报错 + `request_user_input` 阻塞 + 禁止自动进入/离开 Plan | `context/world_state/collaboration_mode.rs:34-37,157-177`、`request_user_input.rs:73-84`、`session/turn_input.rs:76-91` |
| 批准 | TUI 弹 "Implement this plan?"：Yes → 切 Default 并提交 `"Implement the plan."`；clear context → 新线程 + 计划文本 | `tui/src/chatwidget/plan_implementation.rs:36-64`、`app/event_dispatch.rs:512-531` |
| 持久化 | plan 事件不写 rollout；但 function call/output 与 `TurnItem::Plan` 作为 ResponseItem 落在 `~/.codex/sessions/**/rollout-*.jsonl` | `rollout/src/policy.rs:212,241-251` |
| 执行循环 | `run_turn` 普通循环，**无 plan 专用循环** | `core/src/session/turn.rs:177,490` |
| 续跑信号 | `needs_follow_up` = 任一工具调用（`stream_events_utils.rs:368`）∨ `end_turn==false`（`turn.rs:3371`）∨ RespondToModel 错误 ∨ 流错误/邮箱未空 | `turn.rs:3142,624-636` |
| 判定点 | 工具全部 drain 后：`model_needs_follow_up \|\| has_pending_input` | `turn.rs:636` |
| 预算 | 无迭代上限；token 由自动压缩阈值承担；超时是流 idle timeout（300s） | `turn.rs:1910-1937`、`model-provider-info/src/lib.rs:72-73` |
| 压缩 | 默认 90% context window 触发；写 `SUMMARY_PREFIX + summary` 的 **user 消息**，保留最近 ≤20000 token 的 user 消息，替换历史 | `protocol/src/openai_models.rs:563-574`、`compact.rs:63,407-410,425-439` |
| 压缩与计划 | **计划历史被切断**；不回灌清单，也没有 plan checkpoint 字段 | `compact.rs:84-98` |
| 请求组装 | 每请求重建 instructions/tools（确定性 UUID，不落盘）；world state 每 step 重建并按 diff 追加 | `core/src/client.rs:1052-1085`、`session/world_state.rs:59-446`、`session/mod.rs:4011-4042` |
| 子智能体 | `spawn_agent` / `send_message` / `wait_agent` / `list_agents` / `close_agent` / `resume_agent` / `interrupt_agent`；`fork_turns=none\|all`；共享 cwd；默认并发 4 | `multi_agents_spec.rs:92-365,665-668`、`agent/control/spawn.rs:80`、`config/mod.rs:261` |
| 子智能体回灌 | `InterAgentCompletionMessage` 以 **user 角色**、`trigger_turn=false` 发给父线程 | `agent/control/completion.rs:100-118` |
| 收尾 | `TurnComplete{last_agent_message, error}`；**无完成度门控**；剩余项只在 UI 卡片 / 标题展示 | `tasks/mod.rs:894-903`、`tui/src/history_cell/plans.rs:264-277`、`status_surfaces.rs:1124-1131` |

## 2. 我们的现状（证据）

| 环节 | 事实 | 位置 |
|---|---|---|
| 计划工具 | `present_plan`：`{plan, items[{content, verify, depends_on}]}`，最多 6 项 | `tools/present_plan_tool.py:98-149`、`:27` |
| 审批 | 工具内阻塞 `ask(intent="plan_review")`，300s；批准后全量替换 task 并 `armed_profile="plan"`、清零计数 | `present_plan_tool.py:320-326,375-408` |
| 工具回灌 | 返回 `task.render()`，**不是固定文本** | `todo_write_tool.py:374-383` |
| 条目状态 | `TodoItem` 11 字段（id/content/status/depends_on/evidence/verify/files/kind/attempts...），状态 6 种 | `domain/task.py:47-67,18-29` |
| 执行器 | `PlanExecutor`（默认）+ `PlanRunner`（legacy）+ `TaskDriver`；workspace 门、arm/disarm、kickoff prompt、settle、receipt、报告 | `core/plan_executor.py:83-217`、`plan_runner.py:582-665,1029-1130` |
| 单回合 | `process_input(synthetic=True)` 一次；无迭代上限/无 token 预算/无墙钟 | `plan_executor.py:122-133,157-163` |
| 续跑 | 计划回合 no-tool 停步 → **结构性 continue**（不写消息） | `agent_loop.py:1339-1377` |
| 刹车 | `PLAN_IDLE_LIMIT=3`、`PLAN_STALL_ROUNDS=6` | `turn_books.py`、`turn_policy.py:409-417` |
| 计划状态注入 | C-3 follow-up：批准的 `<proposed_plan>` 正文保存在 `SessionData.approved_plan`（落盘 `approvedPlan`），每请求重建为 `<plan-state>` 易失尾块（在 `<plan-mode>` 后、`<task-state>` 前） | `domain/session.py`、`app/commands.py`、`request_assembly.py` |
| 压缩 | 计划回合无专属分支，与普通回合同一闸门；`approved_plan` 是会话字段，`<plan-state>` 按尾块重建，不会被压缩吃掉 | `request_assembly.py`、`context_compression.py` |
| 验收 | `todo_write` 标 completed 时跑 `verify`；失败回退 `in_progress` | `todo_write_tool.py:190-223` |
| 对账/回执 | `_settle_completed`、`_settle_open_leftovers`、`StepDelivery`、`PlanStepReceipt`、`PlanReport` | `plan_executor.py:265-317,319-393,476-483`、`plan_runner.py:549-579` |
| 子智能体 | 模型侧 `subAgent` / `parallel_research` / `subagent_list`；结果走 tool result / `[notice]` | `tools/subagent_tool.py:144,183-230`、`subagent.py:501-521` |
| 子智能体上下文 | `ParentContext` 只带父请求视图 + 权限，不继承系统提示 | `domain/subagent.py:59-74` |
| 结束 | 有 `report.stopped`/`plan incomplete`，前端自动派发 `/plan run` | `plan_executor.py:205-217`、`frontend/src/util/plan-run.ts:26-30` |

## 3. 逐项对照（24 维）

| # | 维度 | Codex | 我们 | 判定 |
|---|---|---|---|---|
| 1 | 计划工具名与参数 | `update_plan`（explanation + step/status） | `present_plan`（plan + content/verify/deps） | DIFF |
| 2 | 条目字段 | step + status | 11 字段含 verify/deps/attempts | DIFF |
| 3 | 工具回灌 | 固定 `"Plan updated"` | `task.render()` | DIFF |
| 4 | 计划状态载体 | transcript（FunctionCall/Output） | `TaskState` 落盘 + `<plan-state>` 尾块 | DIFF |
| 5 | 进入计划模式 | `/plan` 切 collaboration_mode | `/plan <topic>` 切 plan agent + readonly | DIFF |
| 6 | 模式对工具/沙箱 | 不改（纯提示约束） | plan 回合强制 readonly、关搜索梯子 | PARTIAL |
| 7 | 批准/退出 | "Implement this plan?" → 切 Default + `"Implement the plan."` | 工具内阻塞弹窗 + `armed` + 前端自动 `/plan run` | DIFF |
| 8 | 执行入口 | 一句 `"Implement the plan."` | `[plan-run] 开始执行计划：…` kickoff | DIFF |
| 9 | 执行循环 | 普通 `run_turn`，无专用循环 | 普通 loop + plan 专用分支 | PARTIAL |
| 10 | 续跑信号 | `needs_follow_up`（工具调用/pending/end_turn） | 工具调用 ∨ (`plan_executing` ∧ 有未完成项) | PARTIAL |
| 11 | 回合内预算 | 无迭代上限、无 token 预算 | 同左 | SAME |
| 12 | 刹车 | 无（只有流超时） | idle 3 / stall 6 | DIFF |
| 13 | 每轮计划注入 | 无（靠历史） | `<plan-state>` 易失尾块 | DIFF |
| 14 | 压缩后计划状态 | 丢失 | 尾块每轮重建，不丢 | DIFF |
| 15 | 条目验收 | 无 | completed 时跑 verify，失败回退 | DIFF |
| 16 | 回执/交付 | 无 | `StepDelivery` + `PlanStepReceipt` | DIFF |
| 17 | 完成度门控 | 无 | `_settle_open_leftovers` + `plan incomplete` | DIFF |
| 18 | 收尾报告 | 最后一条 assistant 消息 + UI 卡片 | `PlanReport.render()` | DIFF |
| 19 | workspace 门 / armed | 无 | 三层 workspace 门 + arm/disarm | DIFF |
| 20 | 子智能体工具集 | spawn/send/wait/list/close/resume/interrupt | subAgent/parallel_research/subagent_list | PARTIAL |
| 21 | 子智能体上下文 | `fork_turns=none\|all` | 只带父请求视图（近似 none） | PARTIAL |
| 22 | 子智能体回灌 | user 消息、`trigger_turn=false` | tool result / `[notice]` | PARTIAL |
| 23 | 计划完成提示 | "Implement this plan?" 弹窗 | 前端自动执行 | DIFF |
| 24 | 剩余项展示 | TUI 计划卡片 | PlanReport + 前端计划面板 | PARTIAL |

得分 = (2×1 + 7×0.5) / 24 ≈ **23%**。

## 4. 默认路径的取舍

### 4.1 默认路径不启用的（Codex 没有；开关 = `plan_orchestrator="session"`）

1. `present_plan` 工具形态与审批弹窗 → codex 路径改用 `update_plan`（4.2-8）。
2. `verify` 字段与条目边界验收 → 由 `plan_verify_items`（默认 `False`）单独开关。
3. 回执与交付：`StepDelivery`、`PlanStepReceipt`、`_old_completed/_recorded_this_run`。
4. 对账与完成门控：`_settle_completed`、`_settle_open_leftovers`、`_skip_dependents`、
   `plan incomplete`、`PlanReport`。
5. 执行器本体：`PlanExecutor` / `PlanRunner` / `TaskDriver` 计划分支、`armed/armed_profile`、
   workspace 三层门。
6. 计划专用循环分支：`session.plan_executing`、`<plan-state>` 尾块、`PLAN_IDLE_LIMIT`、
   `PLAN_STALL_ROUNDS`（代码保留但 codex 路径不再设置标志）。
7. 计划专用 kickoff/continue prompt（`[plan-run]`）与前端自动 `/plan run`。

### 4.2 必须照抄的（Codex 行为）

8. `update_plan` schema、状态机、固定回灌 `"Plan updated"`、Plan 模式下报错（C-1）。
9. `/plan` = 切模式（提示约束为主）、批准 = 切回 Default + `"Implement the plan."`（C-2）。
10. 执行 = 普通 turn；`needs_follow_up` 语义；无完成度门控；最后一条消息即结果（C-3）。
11. "Implement this plan?" 询问（终端 `ask_user` + 前端卡片）（C-4）。

### 4.3 必须复用的（用户要求保留我们的组件）

12. **压缩栈**：`Compactor` / `context_compression` 的闸门与 checkpoint 不动；
    codex 路径与普通回合同一条压缩路径。
13. **请求组装**：`RequestAssembler` 的易失尾块机制不动；但 codex 计划回合**不注入**
    `<task-state>`（Codex 靠 transcript），`TaskState` 只作为前端计划面板的投影（第 8 节 R3）。
    也就是说：复用组装器，不是复用"把计划状态塞进请求"这个行为。
14. **子智能体**：`SubAgentRunner`、worktree、权限/沙箱、后台表全部复用；只改模型可见的
    工具名与 `fork_turns`/回灌语义（C-5）。

## 5. 初版分期（已被第 8 节取代，保留供对照）

每期完成的条件固定为：`pytest` 全绿 + `ruff` + `mypy` + 代码评审清单逐条核对 + 真机脚本
（真实 `AgentLoop` + 脚本化 `ChatEngine`，不需要 API key）。

### C-0 选择与配置

`config/models.py`：

```python
plan_orchestrator: Literal["codex", "session", "legacy"] = "codex"
plan_verify_items: bool = False   # 仅 codex 路径读取；session 路径始终验收
```

- `plan_runner_for(loop)`（`core/plan_executor.py:52-68`）增加 `codex` 分支，返回
  `CodexPlanRunner`；`session`/`legacy` 保持原状（旧路径原封不动，便于回退）。
- 前端与 server 的两个调用点（`app/commands.py:615-619`、`server/handlers.py:736-740`）不变，
  仍只通过 `plan_runner_for` 选择。

验收：三档配置各跑一次单测，确认选择正确；默认值是 `codex`。

### C-1 `update_plan` 工具（照抄 `plan.rs` / `plan_spec.rs`）

新文件 `ai_coding/tools/update_plan_tool.py`：

```json
{
  "type": "object",
  "properties": {
    "explanation": {"type": "string"},
    "plan": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "object",
        "properties": {
          "step": {"type": "string"},
          "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}
        },
        "required": ["step", "status"]
      }
    }
  },
  "required": ["plan"]
}
```

行为（逐条对齐 Codex）：

- 成功回灌**固定文本** `"Plan updated"`，`success=True`；不回 `task.render()`。
- 全量替换 `TaskState.items`：id 按 1..N 重新分配，`content ← step`，`status` 直接映射；
  除 `depends_on`（空）与 `verify`（仅 `plan_verify_items=True` 时可带）外不写别的字段。
- `session.mode == "plan"`（即 `is_plan_agent(session.agent)`，`domain/session.py:482-491`）
  时返回错误，文本对齐 Codex 的意图：`"update_plan is unavailable in Plan mode; present the plan as text."`。
- **不是** `plan_only`：默认回合也可见（Codex 在 Plan 模式可见但调用报错）；
  `plan_only` 只留给 `present_plan`（session 路径）。
- `parallel_safe = False`（写共享 TaskState）。
- 注册到 `ToolRegistry`；`present_plan` 在 codex 路径**不注册**（计划提交改为纯文本 + C-2 询问）。

验收：schema 键集合恰为 `{explanation, plan}`；回灌文本逐字节 `"Plan updated"`；
plan 模式调用返回 error；默认模式调用后 task 状态与 plan 一致。

### C-2 计划模式 = 模式，不是执行器

- `/plan <topic>`（`app/commands.py:596-649`）保持"切 plan agent + readonly"，不再做别的。
- 请求组装新增 `<plan-mode>` 易失块（`request_assembly.py:491-509` 的 tail 槽位，放在 `task` 前）：
  ```
  <plan-mode>计划模式：只读探索，不修改文件；把计划写成最终回复，不要调用 update_plan。</plan-mode>
  ```
  这是 Codex `collaboration_mode` 的最小等价物；只在 `session.mode=="plan"` 时出现，不落盘。
- 回合收尾（`core/turn_close.py` 之后、`commands.py` 的 `_handle_plan` 返回处）：
  若 mode 是 `plan` 且 `session.task.items` 非空、且本回合干净结束，则询问
  `"Implement this plan?"`（终端用现有 `UserPromptCallback.ask(intent="plan_implement")`，
  前端复用评审卡）。Yes → `commit_posture(agent=<默认 agent>, readonly=False)`，随后提交
  synthetic `"Implement the plan."`；No → 保持只读，计划保留。
- 删除 codex 路径对 `armed/armed_profile` 的依赖：`<task-state>` 只要 `task.items` 非空就注入，
  不再以 armed 为条件；`_armed_profile()` 仅 session/goal 路径使用。

验收：`/plan` 回合只读；结束时出现 "Implement this plan?"；选 Yes 后 agent 回到默认，
且下一条用户消息逐字节是 `"Implement the plan."`；选 No 无任何执行。

### C-3 执行退化为普通回合

新文件 `ai_coding/core/plan_codex.py`（薄，不超过 ~80 行）：

```python
class CodexPlanRunner:
    async def run(self, session, console=None, *, on_delta=None, observer=None):
        if not session.task or not session.task.items:
            return  # nothing to implement
        # Codex parity: no flags, no policy swap, no budget, no verification.
        await self._loop.process_input(session, "Implement the plan.", synthetic=True)
```

- `plan_runner_for` 的 `codex` 分支返回它；`PlanExecutor`/`PlanRunner` 一行不改。
- codex 路径**不设置** `session.plan_executing`，因此 loop 的续跑块、`_has_a_round_left` 的
  无限上限、`<plan-state>`、idle/stall 全部自然不生效（代码留待第二批删除）。
- 结束即普通回合结束：模型有工具调用就继续下一轮，纯文本停下就结束（Codex 语义）；
  没有 `_settle_completed`、没有 `PlanReport`、没有 `plan incomplete`。
- 剩余项由调用方读 `task.open_items()` 输出一行状态（对齐 Codex 的 UI 卡片）：
  ```
  [plan] 3/5 completed; 2 pending
  ```
- 压缩与组装不变：`RequestAssembler` 每次 assemble 先压缩，`<task-state>` 每轮重建。

验收（真机脚本）：3 项计划、脚本模型一次给完全部状态 → **1 个 turn**；
模型中途纯文本停下且仍有 pending → 回合立即结束，不续跑、不注入任何文本；
`/plan run` 只调用一次 `process_input`，prompt 恰为 `"Implement the plan."`。

### C-4 收尾、CLI 与前端

- CLI：`/plan run` 不再拼 `[plan-run] …`，改为走 C-3 的 `"Implement the plan."`；
  输出 `completed/total` 一行。
- 前端：删除 `frontend/src/util/plan-run.ts` 的自动派发；计划面板改读 `task.items`
  （不再读 `stepDeliveries`）；回合结束出现 "Implement this plan?" 卡片。
- 计划模式下 `present_plan` 的评审卡不再出现（工具不注册）；保留旧卡片只在 session 路径。

验收：前端快照/契约测试更新；手动点一遍 "Implement this plan?" 的 Yes/No 两条路径。

### C-5 子智能体对齐（Codex `multi_agents`）

模型可见工具（代替现有 `subAgent` / `parallel_research` / `subagent_list`）：

| 工具 | 参数 | 落到现有实现 |
|---|---|---|
| `spawn_agent` | `{task, name?, fork_turns?: "none"\|"all"}` | `SubAgentRunner.run(..., context_mode=...)` |
| `send_message` | `{agent_id, message}` | `SteeringQueue` 投递到子代理 |
| `wait_agent` | `{agent_id, timeout_ms?}` | 等 `BackgroundTask` 完成 |
| `list_agents` | `{}` | 后台任务表 |
| `close_agent` / `interrupt_agent` | `{agent_id}` | 取消/清理 |
| `resume_agent` | `{agent_id}` | 复用子代理会话续跑 |

- `fork_turns`：`"all"` = 现有 `context_mode="full"`（父请求视图）；`"none"` = `task_only`
  （只给任务，不带父上下文），默认 `"none"`（Codex 默认）。
- 完成回灌：不再走 tool result / `[notice]`，改为 **user 角色消息 + `trigger_turn=false`**
  （我们的 `SteeringQueue` / notices 已具备"不立刻开新回合"的能力，只需改角色与投递点）。
- 并发默认 4（`config/models.py` 的子代理上限从 6 改 4，`SubAgentRunner` 默认 3 同步改 4）。
- 保留 `SubAgentRunner`、worktree、权限、ledger；`parallel_research` 降为内部编排
  （由 `spawn_agent` 多次调用实现），不再进工具面。

验收：工具面快照恰有 7 个新名字；子代理完成后父 transcript 出现一条 user 消息且回合数不变；
并发超过 4 时第 5 个被拒绝或有界排队（与现有信号量行为一致）。

### C-6 相似度门禁（把 90% 变成可测）

新增 `tests/test_plan_codex_parity.py`（纯行为断言，不依赖网络）：

1. `update_plan` 的 schema 键集合 = `{explanation, plan}`，status 枚举 = 3 值；
2. 工具回灌文本逐字节 `"Plan updated"`；
3. plan 模式下 `update_plan` 返回 error，默认模式下成功；
4. `/plan run` 只产生 1 次 `process_input`，prompt = `"Implement the plan."`；
5. 执行期间 `session.plan_executing` 恒为 `False`，transcript 无 `[plan-run]`/`[plan-continue]`；
6. 默认 `plan_verify_items=False` 时不跑任何 verify，不产生 `StepDelivery`/`PlanReport`；
7. 纯文本停步 + 仍有 pending → 回合结束（不再续跑）；
8. 子智能体工具面 = 7 个 Codex 名字，完成回灌是 user 消息；
9. 压缩一次后 `<task-state>` 仍存在（复用组装的唯一偏差，必须成立）。

该测试即"相似度 ≥90%"的机器化证据；第 0 节的分数用它来复算。

## 6. 初版批次与风险（已被第 9、11 节取代）

### 批次

| 批次 | 内容 | 完成后 |
|---|---|---|
| 1 | C-0 + C-1 + C-2 + C-3 + C-4 + C-6 | 默认路径即 Codex 行为，相似度 93.75% |
| 2 | C-5 | 子智能体对齐 |
| 3 | 删除 `PlanExecutor` / `PlanRunner` / `TaskDriver` 计划分支与 `plan_executing` 等死代码 | 只留 `codex` 一档 |

### 风险

| 风险 | 说明 | 处置 |
|---|---|---|
| verify 默认关闭 | 计划条目完成不再自动验收，质量回退 | `plan_verify_items` 开关；session 路径保留旧行为 |
| 压缩后计划丢失 | Codex 就是这么丢的；我们保留 `<task-state>` 会偏离其行为 | 允许偏差（用户要求复用组装/压缩），C-6 第 9 条固定它 |
| 无完成度门控 | 计划没做完也结束，用户需自己再 `/plan run` | 照抄；输出 `completed/total` 一行 |
| 测试面大 | `test_plan_executor.py`、`test_plan_continuation.py`、前端测试都锁旧行为 | 按批次改测试，每批全绿再进下一批 |
| 删除旧执行器 | `session` 路径仍有用户 | 批次 3 之前保留；删除前用配置切换做一次回归 |

## 7. 初版交付说明（保留）

- 初版本文（差距盘点 + 决策 + 初版分期）。
- 第 8 节起为复核后的权威版本。

## 8. 复核（R1）：11 个问题与修正

这一节是**权威版本**，第 5/6 节中与本节冲突的描述作废。复核时逐条回到源码核对，发现初版有 11 处会直接导致数据流走不通或语义分叉。

| # | 复核发现 | 证据（源码） | 修正 |
|---|---|---|---|
| R1 | `update_plan` 在 Plan 模式下**报错**，所以计划不可能由它产生；Codex 的计划是 assistant 消息里的 `<proposed_plan>` 文本块 | `core/src/tools/handlers/plan.rs:87-90`；模板 `collaboration-mode-templates/templates/plan.md:96-126`；解析 `core/src/session/turn.rs:2542-2550`、`utils/stream-parser/src/proposed_plan.rs:7` | 计划阶段**不建 TaskState**；计划以 `<proposed_plan>` 存在于 transcript；执行阶段才由 `update_plan` 建表 |
| R2 | 计划回合停步会进"计划提交守卫"，在 `present_plan` 未注册时无限 nudge | `core/turn_policy.py:578-592` | 新增 `TurnSnapshot.plan_review_available`；为 `False` 时 `_ask_guards` 直接跳过 |
| R3 | 空列表也会注入 `<task-state>（无任务项）`，Codex 没有 | `core/turn_policy.py:640-649`；`domain/task.py:458-460` | codex 路径在 `mode=="plan"` 或 `plan_implementing` 时不注入 task 块 |
| R4 | 执行回合纯文本停步会落进 task gate，产生 nudge | `core/turn_policy.py:615-622` | 新增 `session.plan_implementing`；该标志下 `decide_after_step` 直接 FINISH |
| R5 | `verify_item` 只在 `plan_executing` 时接线，`plan_verify_items` 开关会变成死开关 | `core/agent_loop.py:2344-2352` | `verify_item` 的注入条件改为 `plan_executing or plan_verify_items`；验收逻辑写进 `update_plan` |
| R6 | 调用方读 `report.stopped`，薄执行器不能返回 `None` | `app/commands.py:625-626` | `CodexPlanRunner.run` 仍返回 `PlanReport`（只填 `stopped`，不填 receipt/report） |
| R7 | `update_plan` 与 `todo_write` 同写 `TaskState`（同一 id 空间），会 split-brain | `tools/todo_write_tool.py`、`domain/task.py` | 计划执行回合把 `todo_write` 移出工具面（Codex 没有这个工具） |
| R8 | `SubAgentRunner` 只有 `run/spawn_background/cancel/task_list`，没有 send/resume | `core/subagent.py:193,316,376,401` | C-5a 只注册 4 个工具；`send_message/interrupt_agent/resume_agent` 实现前**不注册**，不造幽灵工具 |
| R9 | 默认切 `codex` 会改变所有 duck-typed 测试的工具注册表 | `tools/builder.py:356-359` | builder 用 `getattr(app_cfg,"plan_orchestrator","codex")`；批次 1 同步更新测试；`session/legacy` 显式值才注册 `present_plan` |
| R10 | 初版"96%"没有把三处偏差算进去 | 第 3 节 24 维 | 修正后 = 21 SAME / 3 PARTIAL（6 只读强制、18 一行收尾、20 子代理 4/7）→ **93.75%** |
| R11 | `/plan run` 把"退出计划模式"的 commit 放在 `finally`；若前半段 `request_posture` 被 queued，**执行回合会以 plan/readonly 起跑** | `app/commands.py:596-649` | codex 路径**先写穿** `commit_posture(build, readonly=False)`，再调用 `CodexPlanRunner.run`；不依赖 finally |

### 8.1 修正后的相似度

| 偏差 | 为什么保留 | 影响面 |
|---|---|---|
| 6：计划模式强制只读 | 我们已有权限门/沙箱，靠提示约束不安全 | 更严，不是更松 |
| 18：收尾只打印 `done/total` | Codex 的卡片在 TUI；我们的卡片在 task 面板 | 同一事实，两个渲染面 |
| 20：子代理 4/7 工具 | `send_message/resume` 需要子会话句柄，现有 runner 没有 | 第二批补齐 |

其余 21 维全部 SAME（含 `<task-state>` 不注入、压缩后计划丢失、无完成度门控）。

## 9. 修正后的设计（权威）

### C-0 配置与选择

```python
# config/models.py
plan_orchestrator: Literal["codex", "session", "legacy"] = "codex"
plan_verify_items: bool = False
```

- `plan_runner_for(loop)`：`codex` → `CodexPlanRunner`；`session` → `PlanExecutor`；`legacy` → `PlanRunner`。
- `build_tool_registry`：`codex` 注册 `UpdatePlanTool`、不注册 `PresentPlanTool`；`session/legacy` 相反。
  `UpdatePlanTool` 只在主会话注册（`include_subagent=True`），子代理没有计划表。
  duck-typed 配置缺失该字段时按 `codex`。
- 运行时标志（**不落盘**）：`session.plan_implementing: bool`、`session.plan_verify_items: bool`。
  崩溃后恢复的回合退回普通策略（Codex 也没有跨进程恢复计划回合）。

### C-1 `update_plan`

- 新文件 `tools/update_plan_tool.py`；`name="update_plan"`；`parallel_safe=False`；`plan_only=False`；`mutates_world=False`（只写会话内的表）。
- schema 与 Codex 逐键一致：`explanation?` + `plan[{step,status}]`，status 三值，`required=["plan"]`。
- `session.mode=="plan"` → `success=False, error="plan_mode"`，输出
  `"update_plan is unavailable in Plan mode; present the plan as text."`。
- 正常路径：`ctx.task.items.clear()` → 逐条 `add(step)` → 按参数置 status → 回灌 **`"Plan updated"`**。
  id 由 `add` 生成（1..N）；`blocked/dropped/skipped` 不接受，只在内部保留。
- `plan_verify_items=True` 时额外接受 `verify`，并对"status=completed 且有 verify"的条目调用
  `ctx.verify_item`；失败 → 该条回 `in_progress`，返回 `success=False, error="verify_failed"`
  （这是开关开启后的已知偏差）。
- 写完后 `ctx.emit(TaskUpdated(task=ctx.task))`，让前端计划面板即时更新。

### C-2 计划模式与 `<proposed_plan>`

- `/plan <topic>`：`commit_posture(agent="plan", readonly=True)` → **进入模式时执行一次**
  `session.begin_plan(topic)`（清空旧表，`domain/session.py:493-507`）→ 交给
  `run_human_turn`。已是 plan 模式时再次输入**不重置**表（只是新的一轮规划）。
- 请求组装：`plan_orchestrator=="codex"` 且 `session.mode=="plan"` 时增加易失尾块 `plan_mode`
  （放在 `task` 槽位之前；assembler 从 `self._settings` 读该配置）；`session`/`legacy`
  路径不注入这个块（它们仍用 `present_plan`）：
  ```
  <plan-mode>Plan mode: read-only research. Do not edit files or run mutating commands.
  Ask the user with ask_user when a decision is required. When the spec is complete,
  output exactly one <proposed_plan> ... </proposed_plan> block and stop. Do not
  execute the plan, and do not ask "should I proceed".</plan-mode>
  ```
- 计划回合 **不注入** `<task-state>`；`plan_review_available=False`，`_ask_guards` 跳过。
- 回合结束：检测与询问放在 `run_human_turn`（CLI 与 server 的唯一前门，覆盖
  `/plan <topic>` 与 plan 模式下的普通文本两条入口），`_handle_plan` 不再自己做询问。
  `run_human_turn` 拿到 `process_input` 返回的最终 assistant 文本 → `extract_proposed_plan(text)`；
  命中则调用 `user_prompt.ask(intent="plan_implement", options=("Yes, implement this plan","No, stay in Plan mode"), detail=plan)`。
  - Yes → `commit_posture(agent="build", readonly=False)` → `CodexPlanRunner.run(...)`。
  - No → 保持 plan 模式，什么都不做。
- codex 路径的 `run_human_turn` **不再调用 `plan_only`**（那条路径会注入 `[plan] …` 合成指令并
  走旧的 workspace 门）；它只做 `process_input(session, text)`。`plan_only` 仅供
  `session`/`legacy` 路径使用。
- `run_human_turn` 新增可选 `user_prompt` 参数：
  - `handle_input` 已有该参数（`app/commands.py:111`），直接透传（`:135`）；
  - `server/handlers.py:809` 传 `world.user_prompt`。
- 计划文本的来源仍是"最后一条 assistant 消息里的 `<proposed_plan>`"；批准后追加保存到
  `SessionData.approved_plan`（落盘 `approvedPlan`），供后续普通回合重建 `<plan-state>`。
  这是对 Codex 的**有意偏差**：Codex 靠历史，本仓明确不把它解析成 `TaskState`，也不改 goal。
  提示词要求模型只在当前请求延续该计划时使用；新话题忽略本块。`/plan off` 与 `begin_plan` 清空。
  对弱模型还有两层兜底：若 plan 模式里模型把计划正文塞进了不存在的 `proposed_plan` 工具调用，
  `run_human_turn` 会在本轮内取出该 `plan` 参数，按正常计划评审路径展示一次，而不是反复报 unknown_tool；
  若模型输出了空的 `<proposed_plan></proposed_plan>` 但正文里有计划内容，则评审正文，而不是静默结束。
- 解析规则（与 `utils/stream-parser/src/proposed_plan.rs` 对齐）：取**第一个**完整块
  `<proposed_plan> … </proposed_plan>`，内容 `strip()`；未闭合 → `None`。
  展示给用户的文本用 `strip_proposed_plan(text)` 去掉该块（Codex `stream_events_utils.rs:30-54`），
  计划正文只进询问卡的 `detail`／前端计划面板；transcript 保留原始消息。

### C-3 执行

`core/plan_codex.py`：

```python
class CodexPlanRunner:
    async def run(self, session, console=None, *, on_delta=None, observer=None) -> PlanReport:
        session.plan_verify_items = bool(self._settings.plan_verify_items)
        session.plan_implementing = True
        try:
            await self._loop.process_input(session, "Implement the plan.", synthetic=True)
        finally:
            session.plan_implementing = False
        done, total = session.task.done_count, session.task.total
        if console and total:
            console.info(f"[plan] {done}/{total} completed")
        return PlanReport()
```

- 不做 workspace 预检、不 `begin_plan`、不验收、不回执、不报告。
- **调用前必须已处于 build/非只读**（R11）：`/plan run` 与"Yes"分支都用
  `loop.commit_posture(agent="build", readonly=False, ...)` 写穿，再调用本 runner；
  不允许把姿态切换留到 `finally`。
- 工具面：`plan_implementing` 时移除 `todo_write`；`update_plan` 始终在。
- 循环：
  - `TurnSnapshot` 增加 `plan_implementing: bool`、`plan_review_available: bool`
    （后者 = `"present_plan" in self.tools.list_names()`，`tools/registry.py:45-48`）；
    两个 snapshot 构造点（`agent_loop.py:1055`、`:1378`）同时填；
  - `_has_a_round_left` 与 `decide_next_iteration` 的 ceiling：`plan_implementing` 时 `-1`；
  - `_begin_turn` 的 deadline：`plan_implementing` 时 `None`；
  - `decide_after_step`：`plan_implementing` → FINISH（在 acceptance/task gate 之前）；
  - `allow_search`：不因 `plan_implementing` 关闭（Codex 不禁搜索）；
  - 续跑仍由"本步有工具调用"驱动，不注入任何文本。
- 失败/中断：不 disarm、不改 task 状态；用户可再次 `/plan run`（Codex 同理）。

### C-4 收尾与前端

- 删除前端 `shouldDispatchPlanRun` 的调用点（`frontend/src/util/plan-run.ts:28-30` 的消费方），
  计划不再自动执行；改为 `plan_implement` 询问卡。
- 计划面板读 `session.task.items`（`TaskUpdated` 事件），不读 `stepDeliveries`。
- `/plan run` 保留为手动入口：等价于"Yes, implement this plan"。

### C-5 子智能体

C-5a（批次 2，可完整实现）：

| 工具 | 参数 | 映射 |
|---|---|---|
| `spawn_agent` | `{task, name?, fork_turns?: "none"\|"all"}` | `SubAgentRunner.spawn_background(...)`；`fork_turns` → `context_mode full/task_only`；默认 `none`；立刻返回 `{agent_id}` |
| `wait_agent` | `{agent_id, timeout_ms?}` | 新增 `SubAgentRunner.wait(run_id, timeout)`：轮询后台行 + `asyncio.wait_for`；返回状态行，不返回正文 |
| `list_agents` | `{}` | `SubAgentRunner.task_list()` |
| `close_agent` | `{agent_id}` | `SubAgentRunner.cancel(run_id)` |

- 完成回灌：沿用现有 notices（`core/subagent.py:501-523` → `agent_loop._take_notices:1731-1752`），
  已是 user+synthetic、且只在下一回合被吸收 = Codex 的 `trigger_turn=false`。
- 并发默认 4（`config/models.py` 与 `SubAgentRunner` 默认值同步）。

C-5b（批次 3，需新增能力）：

| 工具 | 缺的能力 | 落点 |
|---|---|---|
| `send_message` | 子代理需要可寻址的 SteeringQueue | `SubAgentRunner` 保存子会话句柄 + 队列 |
| `interrupt_agent` | 取消"当前一步"而非整个 run | 子代理 loop 的取消令牌 |
| `resume_agent` | 子会话需持久化 | 子会话 `SessionService.save` + 复活 |

C-5b 未完成前这三个名字**不注册**，避免模型调用幽灵工具。

### C-6 相似度门禁

`tests/test_plan_codex_parity.py` 断言：

1. `update_plan` schema 键 = `{explanation, plan}`；status 枚举 3 值；
2. 回灌逐字节 `"Plan updated"`；
3. Plan 模式调用报错，普通模式成功；
4. `/plan run` 只 1 次 `process_input`，prompt 逐字节 `"Implement the plan."`；
5. `plan_implementing` 期间无 `[plan-run]`/`[plan-continue]`，transcript 无计划状态文本；
6. 纯文本停步 → 回合结束（不续跑）；
7. `plan_verify_items=False` 不产生 verify/`StepDelivery`/`PlanReport`；
8. `<proposed_plan>` 解析：有块→询问；无块→不询问；
9. 计划模式下 `<task-state>` 与 `<plan-mode>` 的存在性（前者无、后者有）；
10. 子代理工具面 = 4 个（C-5a 后）= 7 个（C-5b 后）。

## 10. 数据流（逐函数，无省略）

### DF-1 规划回合（`/plan <topic>` 或 plan 模式下的任意输入）

| 步 | 函数 | 状态变化 |
|---|---|---|
| 1 | `handle_input`（`app/commands.py:104-149`） | 解析 slash；`/plan` → `_handle_plan`，普通文本 → `run_human_turn` |
| 2 | `_handle_plan`（`:500-708`） | `commit_posture(agent="plan", readonly=True)`；**仅进入模式时** `session.begin_plan(topic)`；`/plan <topic>` 把 topic 交给 `run_human_turn`（不自己起 turn） |
| 3 | `run_human_turn`（`:711-749`） | `session.mode=="plan"` → `process_input(session, text)`（**人的原话**，`synthetic=False`） |
| 4 | `AgentLoop.process_input`（`agent_loop.py:888-913`） | 取 turn 锁；`_run_turn` → `_begin_turn`：turn_count+1、append user、task=session.task、deadline=普通回合 |
| 5 | `RequestAssembler.assemble` | 压缩 → instructions → tail：`plan_mode` 有、`task` 无、`plan_state` 无 |
| 6 | `_model_step` → `say` | 模型只读探索；最终消息含且仅含一个 `<proposed_plan>` |
| 7 | `decide_after_step`（`turn_policy.py:573-622`） | `mode=="plan"` + `plan_review_available=False` → `FINISH "model finished"`（不 nudge） |
| 8 | `TurnCloser.close` | handoff/digest/persist，与普通回合同一路径 |
| 9 | `run_human_turn` | `extract_proposed_plan(answer)`；命中 → `ask(intent="plan_implement")` |

### DF-2 批准 → 执行

| 步 | 函数 | 状态变化 |
|---|---|---|
| 1 | `run_human_turn`（Yes 分支） | `commit_posture(agent="build", readonly=False)` **写穿**（不排队，R11）；保存 |
| 2 | `CodexPlanRunner.run` | `session.plan_implementing=True`；`plan_verify_items=设置值` |
| 3 | `process_input("Implement the plan.")` | turn_count+1；append user（synthetic）；task 仍为空 |
| 4 | 工具面 | `update_plan` 在；`todo_write` 不在（`plan_implementing`）；`present_plan` 不在 |
| 5 | 第 1 次 `update_plan` | `TaskState.items` 1..N；回灌 `"Plan updated"`；`TaskUpdated` 事件 |
| 6 | 下一轮 assemble | **仍不注入** `<task-state>`（Codex 靠历史）；其余尾块照旧 |
| 7 | 工具调用持续 | `books.note_outcomes` → `continue`（既有结构性续跑） |
| 8 | 无工具停步 | `decide_after_step`：`plan_implementing` → FINISH（无 task gate） |
| 9 | 结束 | `plan_implementing=False`；`[plan] done/total`；保存 |

### DF-3 循环判定顺序（`plan_implementing` 时）

```
for index in ...:
    note_plan_progress(...)              # 既有；plan_implementing 时也记录（仅用于观测）
    snapshot = books.snapshot(..., plan_implementing=..., plan_review_available=...)
    d = policy.decide_next_iteration(snapshot)
        - plan_implementing → ceiling = -1
        - 不检查 idle/stall（不适用）→ FINISH 仅当策略上限/轮次
    if d.action == FINISH: break
    assemble(...); step = model(...)
    if step.tool_calls: dispatch; continue
    d = policy.decide_after_step(snapshot)
        - mode=="plan" and not plan_review_available → FINISH
        - plan_implementing → FINISH
        - (其余原逻辑不动)
    if d.action == FINISH: break
```

### DF-4 压缩

| 触发 | 行为 | 与 Codex 的对照 |
|---|---|---|
| 每次 `assemble` 前的 `Compactor.compress`（`request_assembly.py:328-335`） | 阈值/检查点原样 | 同一语义（阈值不同是我们已有配置） |
| 压缩后 | `<proposed_plan>` 与早期 `update_plan` 调用可能被丢弃 | Codex 也丢 |
| `TaskState` | 仍持久化在 session，前端面板可见 | Codex 的 plan 卡片来自事件，同样不受压缩影响 |
| 请求中的计划状态 | 无（codex 路径不注入 task/plan_state） | 与 Codex 一致 |

### DF-5 子智能体（C-5a）

```
模型: spawn_agent{task, fork_turns:"none"}
  → SubAgentTool.execute
  → SubAgentRunner.spawn_background(...) → run_id；返回 {"agent_id": run_id}
子代理在后台 loop 里跑完
  → _finish → _announce_to_model → notices.put(...)
父循环在下一次 iteration 边界 _take_notices
  → session.add_message(user "[notice] ...", synthetic=True)
  （若父回合已结束，notice 等下一个用户回合；等价 trigger_turn=false）
模型: wait_agent{agent_id} → 新增 SubAgentRunner.wait → 状态行（complete/failed/running）
模型: list_agents{} → task_list()
模型: close_agent{agent_id} → cancel(run_id)
```

## 11. 测试用例推演（文档级）

每个用例都给出"初始状态 → 逐步状态 → 断言"。断言编号对应第 9 节 C-6。

### T1 计划生成（happy path）

初始：`session.agent="build"`，`session.task.items=[]`，transcript 为空。

| 步 | 输入/事件 | 期望状态 |
|---|---|---|
| 1 | `/plan 加一个健康检查端点` | agent=plan、readonly=True、task.items=[]、transcript 无新增 |
| 2 | 模型读 3 个文件（只读工具） | 每次工具调用 → 续跑；无 nudge |
| 3 | 模型输出含 `<proposed_plan>` 的最终文本 | `decide_after_step` = FINISH（R2） |
| 4 | `extract_proposed_plan` | 返回 markdown（非空） |
| 5 | `ask(intent="plan_implement")` 答 Yes | `commit_posture(build, readonly=False)` |
| 6 | `CodexPlanRunner.run` | `plan_implementing=True`，prompt=`"Implement the plan."` |

断言：第 2、3 步无 `[plan-continue]`；第 5 步只剩 1 次 `process_input`；无 `present_plan` 调用。

### T2 Plan 模式下调用 `update_plan`

| 步 | 事件 | 期望 |
|---|---|---|
| 1 | 模型 `update_plan{plan:[…]}` | `ToolResult{success:False, error:"plan_mode"}`；task 不变 |
| 2 | 模型继续，输出 `<proposed_plan>` | 正常询问 |

断言：C-6-3；task 始终为空，证明计划不来自工具（R1）。

### T3 执行中途纯文本停步

初始：已批准计划，3 项。

| 步 | 事件 | 期望 |
|---|---|---|
| 1 | `update_plan` 3 pending | items=3；`"Plan updated"` |
| 2 | 2 次 bash 调用 | 续跑 |
| 3 | `update_plan` 第 2 项 completed | 状态=completed |
| 4 | 模型只回文本"已完成" | `decide_after_step` → FINISH；**不** nudge |
| 5 | 收尾 | `[plan] 1/3 completed`（或按实际）；保存 |

断言：C-6-5/6（R4）。这是与 A/B 方案最可见的行为差异（不再自动续跑）。

### T4 执行中发生压缩

| 步 | 事件 | 期望 |
|---|---|---|
| 1 | 第 6 次 assemble 触发压缩 | 历史被 summary 替换 |
| 2 | 下一次请求 | 无 `<task-state>`/`<plan-state>`；`<proposed_plan>` 可能已不在历史里（与 Codex 一致） |
| 3 | 前端 | 计划面板仍显示 3 项（来自 `TaskState`/`TaskUpdated`） |

断言：C-6-5；并断言压缩没有删掉 `TaskState`（session 持久化）。

### T5 `plan_verify_items=True`

| 步 | 事件 | 期望 |
|---|---|---|
| 1 | 配置开启，执行 `update_plan` 带 `verify` 且 completed | `verify_item` 被调用一次（R5） |
| 2 | 命令失败 | 该项回 `in_progress`；`success=False, error="verify_failed"` |
| 3 | 命令成功 | 状态保留 `completed`；`TaskUpdated` |

断言：默认 `False` 时 `verify_item` 调用次数 = 0（C-6-7）。

### T6 `/plan run` 无计划

| 步 | 事件 | 期望 |
|---|---|---|
| 1 | 用户直接 `/plan run` | 仍提交一次 `"Implement the plan."`；模型按历史回答 |
| 2 | 结束 | `[plan] 0/0 completed`（total=0 时不打印） |

断言：C-6-4；没有 workspace 门/`no items` 拒绝（与 Codex 一致）。

### T7 子代理（C-5a）

| 步 | 事件 | 期望 |
|---|---|---|
| 1 | `spawn_agent{task, fork_turns:"none"}` | 返回 `agent_id`；父回合照常继续 |
| 2 | 子代理完成 | 父 transcript 在下一 iteration 出现一条 `[notice]` user 消息；turn_count 不增 |
| 3 | `wait_agent{agent_id}` | 返回状态行；不重复正文 |
| 4 | `list_agents` / `close_agent` | 对应 `task_list`/`cancel` |

断言：C-6-10；并发第 5 个被 `spawn_background` 拒绝（现有 `RuntimeError`）。

## 12. 代码级复用清单（无模糊点）

| 组件 | 复用方式 | 精确接口 | 是否修改 |
|---|---|---|---|
| `AgentLoop.process_input` | 直接调用 | `async def process_input(session, user_input, on_delta=None, observer=None, images=(), *, synthetic=False) -> str`（`agent_loop.py:888-913`） | 不改 |
| `AgentLoop._run_turn/_begin_turn` | 沿用 | 仅新增 `plan_implementing` 的 deadline/ceiling 两个判断 | 改 2 处 |
| `TurnBooks` | 沿用 | 无新计数 | 不改 |
| `TurnPolicy.decide_after_step` | 沿用 | 加 `plan_implementing` 与 `plan_review_available` 两个早退分支 | 改 1 个函数 |
| `TurnPolicy.decide_next_iteration` | 沿用 | ceiling 用 `plan_implementing` | 改 1 行 |
| `RequestAssembler` | 沿用 | 新增 `plan_mode` 槽位；`task` 槽位按条件跳过 | 改 2 处 |
| `Compactor` / `context_compression` | 完全不动 | `compress(...)` 原样 | 不改 |
| `TurnCloser` | 完全不动 | `close(...)` 原样 | 不改 |
| `TaskState`/`TodoItem` | 沿用 | `add(content, verify=..., depends_on=...)`；status 三值映射 | 不改结构 |
| `BaseTool`/`ToolContext` | 沿用 | `execute(params, ctx)`；`ctx.task/mode/emit/verify_item` | 新工具用现有字段 |
| `ToolRegistry` | 沿用 | `register(...)`；`plan_only` 不动 | 新工具注册 |
| `SessionData` | 沿用 | 新增 2 个运行时字段（不序列化） | 加字段 |
| `PlanReport` | 沿用为返回类型 | `stopped: str` | 不改 |
| `UserPromptCallback` | 沿用 | `ask(question, options, detail=, intent=, data=)`（`security/user_prompt.py:46-57`） | 新 intent |
| `SubAgentRunner` | 沿用 | `run`、`spawn_background`、`cancel`、`task_list`（`subagent.py:193,316,376,401`） | 新增 `wait` |
| notices | 沿用 | `notices.put` → `_take_notices` → user+synthetic | 不改 |
| `handle_input`/`run_human_turn` | 沿用 | 透传 `user_prompt` | 改 2 行 |
| server turn 入口 | 沿用 | `handlers.py:809` 传 `world.user_prompt` | 改 1 行 |
| 前端 ask 面板 | 沿用 | `user_prompt.ask` → `intent` | 加样式/文案 |
| 前端计划面板 | 沿用 | 读 `TaskUpdated` 的 `items` | 去掉 `stepDeliveries` |

**明确不改**：压缩算法与阈值、checkpoint 三字段、memory、permission gate、dispatch、MCP、skills、session 序列化格式（除两个运行时不落盘字段）、`PlanExecutor`/`PlanRunner`（批次 3 之前原样保留）。

## 13. 批次与验收

| 批次 | 内容 | 验收 |
|---|---|---|
| 1 | C-0 + C-1 + C-2 + C-3 + C-4 + C-6 | T1–T4、T6 全过；`pytest`/`ruff`/`mypy` 全绿；相似度 93.75% |
| 2 | C-5a（4 个工具） | T7 全过；工具面 4 个 |
| 3 | C-5b（3 个工具）+ 删除旧执行器 | 工具面 7 个；`session/legacy` 删除前做一次配置回归 |

每批的评审清单固定：数据流与第 10 节逐行对齐、无新增计划状态文本、无隐藏预算、压缩路径未被改动。

## 14. 实施记录

### 批次 1（已落地）

| 项 | 落点 | 状态 |
|---|---|---|
| C-0 配置档位 | `config/models.py`：`plan_orchestrator` 支持 `codex/session/legacy`；新增 `plan_verify_items=False`。默认值已在本批末尾翻转为 **`codex`**（builder 的 duck-typed 回退同步为 `codex`）。 | 完成 |
| C-1 `update_plan` | `tools/update_plan_tool.py`：schema 与 Codex 逐键一致；固定回灌 `"Plan updated"`；Plan 模式返回 `plan_mode`；全量替换 1..N；`plan_verify_items=True` 时接受/执行 `verify`。 | 完成 |
| 工具注册 | `tools/builder.py`：`codex` 注册 `update_plan`、不注册 `present_plan`；子代理两者都没有。 | 完成 |
| C-2 计划模式 | `<plan-mode>` 易失尾块（仅 codex + `mode=="plan"`）；`<proposed_plan>` 解析与展示剥离；`run_human_turn` 增加 `user_prompt`，回合结束询问 `intent="plan_implement"`。 | 完成 |
| C-3 执行 | `core/plan_codex.py`：`CodexPlanRunner` 只提交一次 `"Implement the plan."`；`session.plan_implementing/plan_verify_items`（不落盘）；`decide_after_step`/ceiling/deadline/`todo_write` 隐藏；verify 接线。 | 完成 |
| C-4 前端 | 删除自动 `/plan run`（`frontend/src/util/plan-run.ts` 及其测试）；`ask.tsx` 渲染 `detail`（计划正文，15 行截断）。 | 完成 |
| C-6 门禁 | `tests/test_plan_codex_parity.py`（9 例）+ `tests/test_plan_codex_path.py`（11 例）+ `tests/test_plan_codex_realrun.py`（3 例真机脚本）。 | 完成 |

批次 1 的验证：

- `pytest`：**4861 passed / 29 skipped**（仅 bwrap 等既有跳过），0 failed；
- `ruff check ai_coding tests`：通过；`mypy ai_coding`：175 文件无告警；
- 前端 `bun test`：577 pass，`bun run typecheck` 通过；
- 真机脚本 `tests/test_plan_codex_realrun.py`：3 个场景（两回合完成、纯文本停步不续跑、无计划 `/plan run`）全过。

已知与文档的差异（均为批次 1 的刻意取舍）：

1. 默认值已在批次 1 末尾翻转为 `codex`；`session`/`legacy` 仍保留为显式回退项（删除放在真实 provider 验证之后）。
2. 前端用通用 `AskPanel` 渲染 `plan_implement`（而不是 Codex 的专用 TUI 弹窗），计划正文通过 `detail` 展示；
3. 子代理工具面在批次 2 补齐（见下）。

### 批次 2（已落地）

| 项 | 落点 | 状态 |
|---|---|---|
| `spawn_agent` | `tools/agents_tool.py`：后台启动并立刻返回 `agent_id`；`fork_turns=none`（默认，`task_only`/无父上下文）或 `all`（`full`/父消息+规则） | 完成 |
| `wait_agent` | `SubAgentRunner.wait(run_id, timeout_ms)`：`asyncio.shield` 等待，超时/结束都不取消子任务；只返回状态行 | 完成 |
| `list_agents` / `close_agent` | `task_list()` / `cancel(run_id)` 的薄封装 | 完成 |
| 并发 | `AIConfig.max_concurrent_subagents=4`、builder/runner 默认 4 | 完成 |
| 兼容 | 旧的 `subAgent`/`parallel_research`/`subagent_list` 仍注册但保持 deferred；默认表面只有 4 个 Codex 名字 | 完成 |

验证：`tests/test_agents_tools.py` 19 例；全量 `pytest` 退出码 0；`ruff`/`mypy` 通过。
C-5b 的 `send_message`/`interrupt_agent`/`resume_agent` 仍未注册（能力缺口，不造幽灵工具）。

**C-5a 二轮评审修复**（`fix(agents)` 提交）：`fork_turns="none"` 保留父权限规则；
`wait` 改用 `asyncio.wait`（子任务取消/异常不再砸父回合）；池满判定看 running 行且使用
`SubAgentPoolFull` 区分"池满/启动失败"；`create_task` 失败时落 `error` 并关闭协程；
`_finish`/`_reap` 先回灌模型再判 emit，崩溃不再伪装成 cancelled；工具面按
`permission_name` 判断（`subagent deny` 能隐藏 `spawn_agent`/`close_agent`）；
`wait_agent`/`list_agents` 进入自动放行表；旧 `subAgent` 标记 deferred。

## 15. 本轮交付

- 第 1–4 节：对照与差距；第 8–13 节：复核后的权威设计、数据流、推演、复用清单。
- 第 14 节：批次 1、批次 2 的落地记录；`plan_orchestrator` 默认值已翻转为 `codex`。
- 下一步：C-5b（`send_message`/`interrupt_agent`/`resume_agent`）——方案与评审见
  [subagent-c5b-design.md](subagent-c5b-design.md)（v2，已吸收两轮评审）；旧执行器
  （`PlanExecutor`/`PlanRunner`/`TaskDriver` 计划分支）的删除放在**真实 provider
  端到端验证之后**（scripted 模型不能替代一次真实 `/plan run`）。
