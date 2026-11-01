# P0b 实施计划：退休 session/legacy 旧计划路径（分三步）

> 前置：P0a 已把 `PlanReport`/`outside_workspace_*` 迁出（提交 `c7832a7`）。
> 目标：删除 `plan_orchestrator` 的 session/legacy、`PlanExecutor`、`PlanRunner`、
> `present_plan`、旧状态字段/事件/前端评审卡，**不留死代码、不误删共享物**。
>
> 决策（已拍板，默认项）：acceptance 整体删（`_verify_plan_item` 保留）；
> `/readonly off` 在 plan 下拒绝；plan 下只允许 `parallel_research`；
> `subagent_worktree_isolation` 默认改为 false；池满=子代理数≥slots 拒绝；
> `plan_verify_items` 保留；旧会话文件里的旧字段读入时忽略。

---

## P0b-1：删执行器与选型（最小可独立验收）

### 范围
- 删除文件：`ai_coding/core/plan_executor.py`、`ai_coding/core/plan_runner.py`、
  `ai_coding/tools/present_plan_tool.py`。
- 删除测试：`test_plan_executor.py`、`test_plan_runner.py`、`test_plan_m1.py`、
  `test_plan_attestation.py`、`test_plan_budget.py`、`test_plan_proof.py`、
  `test_plan_continuation.py`、`test_present_plan_tool.py`、`test_plan_approved_turn.py`、
  `test_plan_prompt_granularity.py`、`test_plan_exploration_budget.py`。
- `app/commands.py`：删 `plan_only` 两处 import 与调用（`:513,723,802,804`）、
  `_uses_codex`（`:517,778,813-819`）、`plan_runner_for`（`:627-629,859-861`）、
  非 codex 分支（`:600-625`、`:640-662` 的 `if needs_exit and not codex` 等）。
  codex 路径的 `run` 分支改为直接调用新的 `CodexPlanRunner`（从 `core/plan_codex.py`）。
- `server/handlers.py`：删 `plan_runner_for`（`:736-738`），boot 恢复只认 `goal`
  （`:376/386/399-434` 的 `"plan"` 分支、`:728` 的 `profile or "plan"`）。
- `tools/builder.py`：删 `orchestrator` 分叉（`:359-376`），恒注册 `UpdatePlanTool`，
  永不注册 `PresentPlanTool`；`include_subagent` 语义不变。
- `core/request_assembly.py`：删 `plan_orchestrator` 读取（`:412`），codex 行为恒开。
- `config/models.py`：删 `plan_orchestrator`（`:501`）、`plan_step_max_iterations`、
  `plan_item_wall_seconds`、`plan_max_step_turns`、`plan_token_budget`、
  `plan_max_continues`（若存在）；`config.example.yaml` 同步。
- `server/handlers.py`：删 `plan_tokens_used`（`:252-258`）、`step_deliveries` 指纹（`:264`）。

### 验收
- `grep -rn "from ai_coding.core.plan_executor\|from ai_coding.core.plan_runner\|present_plan_tool\|plan_orchestrator" ai_coding tests scripts` 为空。
- 全量 `pytest` 0 failed；`ruff`/`mypy` 干净。
- codex 链路：`/plan`、批准、`Implement the plan.`、`update_plan` 仍工作
  （`tests/test_plan_codex_*` 全绿，只把 `test_plan_codex_parity.py:22` 的 import
  改为 `core.plan_report`）。

### 实施记录（完成，未提交 → 提交前）
- 模型可见文案随删（否则模型会调不存在的工具）：`ai/prompt.py` plan 段、
  `core/plan_reminders.py:45-64`、`tools/finish_research_tool.py`、
  `tools/todo_write_tool.py:157`、`core/dispatch.py:170,843` → `<proposed_plan>`。
- `server/world.py:100` boot 扫描只认 `armedProfile=="goal"`；
  `domain/task.py:from_dict` 把非 goal 的 armed 归一化为未武装（旧 plan 会话
  加载即自愈，`start_goal` 不再永久拒绝）。
- `core/turn_policy.py`：guard 的预算边缘与 `_finish_at` 两处加
  `plan_review_available` 守卫（新用例 `tests/test_plan_codex_path.py`）。
- 脚本：`scripts/plan_lifecycle_live_check.py` 删除；`m2_cases/m3_cases/
  mutate_r21` 里引用已删模块的用例/变异删除。
- 测试同步：`test_boot_resume`（goal + 旧 plan 不参与 boot）、`test_prompt`、
  `test_plan_reminders`、`test_finish_research_tool`、`test_mode_gate`、
  `test_domain_session`、`test_plan_guard`（guard 用例补 review 工具前提）。

### 风险
- `commands.py` 的 `finally` 边界落盘只有旧路径依赖；删除时要确认 codex 的姿态
  已在调用前写穿（C-3/R11）。
- `present_plan` 删除后，`FinishResearchTool` 仍是 plan_only 的唯一出口；
  保留（有用、有人消费），不随删。

---

## P0b-2：删旧状态字段、事件与前端

### 首批（P0b-1 评审遗留，按最小可验收先做）

#### P0b-2a：退役 plan 提交守卫（present_plan 的最后残迹）

**事实前提（已核，P0b-1 之后恒成立）**：`plan_review_available` 恒 `False`
（`present_plan` 不再注册）；`session.plan_executing` 恒 `False`（旧执行器已删）；
`armed_profile=="plan"` 不再被写入（`from_dict` 也已归一化）。因此下列分支/
字段在生产路径全部不可达。

**删除清单**
- `core/turn_policy.py`
  - `PlanSubmissionGuard`（`:336-376`）、`StopGuard` 协议（`:323`）与
    `stop_guards`/`_ask_guards` 机制（`:386-393,537-548`）——守卫是唯一实现，
    没有第二消费者，按"零抽象"一并删。
  - 常量 `PLAN_NUDGE`/`PLAN_UNSUBMITTED_NOTE`（`:52-59`）、
    `PLAN_DISMISSED_NOTE`/`PLAN_HELD_NOTE`（`:81-90`）、
    `PLAN_WRAPUP_ROUNDS`/`DEAD_BUDGET_PATHS`（`:72-77`，只被守卫与预算边缘用）。
  - `TurnSnapshot.plan_review_available/plan_submitted/plan_reviewed/
    plan_nudged/plan_approved`（`:244-247,292-306`）与预算边缘 nudge 块
    （`:435-455`）；`decide_after_step` 的 plan 分支简化为 `FINISH`；
    `_finish_at` 的守卫询问块（`:576-587`）删除。
  - `plan_state_block`（`:688-710`）及其在 `request_assembly:426` 的消费。
- `core/turn_books.py`：`plan_nudged/plan_reviewed/plan_approved` 字段、
  `note_plan_nudge`/`note_plan_approved`、`snapshot()` 的三项投影、`record`
  里 `present_plan` 分支（`:104,246-258,376-390,533-538,680-685`）。
- `core/agent_loop.py`：`PLAN_DISMISSED_NOTE/PLAN_HELD_NOTE` import、
  `_on_plan_approved`（`:293-303`）、`plan_review_available=` 两处投影、
  `conclusion.exit == "present_plan"` 分支（`:1521-1528`）、dismissed 分支
  （`:1529-1566`）、`books.plan_approved` 撤回收尾（`:2102-2118`）、
  `note_plan_approved=` 传参（`:2367`）、`plan_review_state` 的 ask_user 债务
  （`:2441-2451`）。
- `tools/base.py` 的 `TurnConclusion.plan_review_state`（`:58`）与
  `ToolContext.note_plan_approved`（`:141`）；`core/request_builder.py:96,144`
  的透传；`core/write_ahead.py:5` 注释。
- 测试：删 `tests/test_plan_guard.py`（整文件只测该守卫）；`test_turn_policy.py`
  的 StopGuard 两条用例（`:223-255`）与 plan 守卫用例；清理
  `test_surface_tools_armed`/`test_plan_mode_turn`/`test_server_bridge` 中
  `plan_reviewed/plan_approved/plan unsubmitted` 的构造。

**保留**：`FinishResearchTool`、`<proposed_plan>` 路径、`plan_implementing`
（codex 实现轮）、`plan_executing` 留到 P0b-2d。

**RIPR**
- R：保留 `test_plan_codex_path.py` 的"无 review 工具就 FINISH"用例，断言
  `reason == "model finished"`；新增 `TurnSnapshot(mode="plan",
  iteration=max_iterations)` 下预算只决定 ITERATE/FINISH、`inject is None`。
- I：删掉守卫后，`/plan` 回合结束不再产出任何 present_plan 文案。
- P：全量 `pytest`；`grep -rn "PlanSubmissionGuard|PLAN_NUDGE|plan_review_available|
  plan_submitted|plan_nudged|plan_approved|plan_state_block" ai_coding tests` 为空。
- R：`ruff`/`mypy`。

**风险**：`TurnSnapshot` 是 dataclass，删字段后任何 `replace(...)` 传参处都会
TypeError——实现时先全仓 grep 字段名再删；`turn_books.snapshot()` 与
`request_assembly` 的调用顺序不变。

**验收**：codex 链路 `/plan`→`<proposed_plan>`→"Implement this plan?"→
`Implement the plan.`→`update_plan` 仍通；plan 回合不再出现 present_plan 文案。

#### P0b-2b：旧 live check 与残留白名单（已提交 `d951b0a`）
- 删除 `scripts/n1_live_check.py`、`t12_live_check.py`、`w2_live_check.py`、
  `r19_acceptance.py`（同一套旧 plan 评审 live check，无其它消费者）；
  `m2/m3_cases.py` 里按 `present_plan` 构造的用例同步删除或改写。
- 删除 `security/permission_gate.py` 的 `present_plan` ALLOW 项、
  `core/export_html.py` 的图标、`core/turn_books.py` 的
  `BOOKKEEPING_TOOLS` 项、`core/agent_loop.py` 的 `TaskUpdated` 触发名单项；
  同步更新 `agents/registry.py`、`tools/base.py`、`tools/builder.py`、
  `core/request_builder.py`、`core/dispatch.py`、`domain/read_ledger.py`
  里指向该工具的注释。
- 测试：删 `test_permission_gate.py::test_present_plan_allow` 与
  `test_permission_gate_equivalence.py` 的 `allow_present_plan` 行（工具已
  退休，未知工具 fail-closed 的 WARN 行为不改）。
- 验收：`grep -rn present_plan ai_coding --exclude-dir=frontend` 为空；全量
  测试 + ruff + mypy 绿。

### 范围
- `domain/session.py`：删 `step_item_id`、`plan_executing`、`item_deadline`、
  `acceptance`、`step_deliveries`（含 `begin_plan` 重置 `:514`、序列化 `:741,809`、
  `from_dict` 兼容读取忽略旧键）。
- `domain/task.py`：删 `plan_tokens_used`（`:517,546`）、`TodoItem.attempts`
  （旧执行器唯一写入者）；保留 `armed/armed_profile/rounds_run/resume_count/
  tokens_used/resume_fingerprint/no_progress_resumes`（goal/boot）。
- `core/agent_loop.py`：删 `plan_executing` 全部分支（`:1052,1360,2395,2569` 等）、
  `_verify_plan_item` 的 `boundary_verdict` 复用（保留 `verify_items` 路径）、
  `reset_boundary_verdicts`/`boundary_verdict`、`_ask_guards` 的 plan 分支、
  `plan_review_state`、工具名名单里的 `present_plan`。
- `core/turn_policy.py`：删 `PLAN_IDLE_LIMIT`、`PLAN_STALL_ROUNDS`、结构续跑计划分支、
  `plan_state_block`、`plan_review_available`、`PlanSubmissionGuard`、
  `plan_submitted/plan_nudged/plan_approved`。
- `tools/todo_write_tool.py`：删 `step_item_id`/`plan_executing` 冻结（`:115-139`）、
  `_verify_on_completion`（`:174-223`）；验收由 `update_plan` 的 `plan_verify_items` 承担。
- `tools/base.py`/`request_builder.py`：删 `ToolContext.step_item_id/plan_executing/
  plan_verify_items` 中的旧两项（`plan_verify_items` 保留）。
- `core/events.py`：删 `PlanStepReceipt`；`server/bridge.py` 删 `plan.receipt`。
- 前端：删 `component/plan-review.tsx`、`util/submission.ts` 的 plan review、
  `util/plan-agent.ts`、`status-bar.tsx` 的 plan 分支、`home.tsx` 的 plan 入口、
  `ask.test.ts`/`reducer.test.ts`/`plan-review-render.test.tsx` 的旧用例；
  `state/session.ts` 的 `stepDeliveries`。
- `core/acceptance.py`：保留 `verify_items`（update_plan 用）；删
  `AcceptanceItem/acceptance_states/pending_items` 的消费点
  （`turn_policy:633`、`agent_loop:1102,1337`、`interventions:193,415,510`、
  `turn_books:291,649`、`config.max_acceptance_nudges/acceptance_verify_*`）。
- `ai/prompt.py`：删 plan 模式下教 `present_plan` 的段落（`:155-173`）；
  `plan_reminders.py:56-61` 同步。
- `permission_gate.py:389`、`export_html.py:360`、`dispatch.py:170,843`、
  `turn_books.py:104,533,680`、`tools/base.py:58` 的 `present_plan` 白名单/文案。

### 验收
- `grep -rn "plan_executing\|step_item_id\|step_deliveries\|PlanStepReceipt\|present_plan" ai_coding` 为空。
- 旧 session.json（含 `acceptance`/`stepDeliveries`/`planTokensUsed`）能正常 load，
  旧键被忽略，不报错。
- 全量 `pytest`；前端 `bun test` + `tsc --noEmit`。

### 风险
- `acceptance` 与 `verify_items` 必须分清：只删契约，不删执行命令的函数。
- `plan_implementing` 保留（codex 实现轮）；不要与 `plan_executing` 一起删。

---

## P0b-3：收尾核验

- 全仓 `grep` 旧符号为零；`docs/` 里引用旧路径的文档加"已退休"标注（不改历史文档内容）。
- 真机：`/plan` → 批准 → 实现 → `update_plan` → 完成；`/readonly`；build 主闭环。
- 旧会话加载 + boot resume（只认 goal）。

---

## 自核

- 每个删除项都有真实消费者迁移或确认无消费者（不是"测试在用"）。
- 不删 `TaskDriver`、`PlanReport`、`workspace_guard`、`verify_items`、
  `armed_profile` 字段、`FinishResearchTool`、`update_plan`、`plan_verify_items`。
- 三个 P0b 步骤各自可独立回退。
