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

#### P0b-2d：删旧执行器字段与死统计

**事实前提（已核，P0b-1/2b 之后恒成立）**
- `plan_executing` / `step_item_id` 的唯一写入者是已删的 `plan_runner` / `plan_executor`；
  现在全仓没有任何地方把它置 True，所有分支不可达。
- `step_deliveries` 的唯一写入者已删；只剩序列化、`begin_plan` 重置、一个已删进度签名
  分支里的读取，和两条测试。
- `item_deadline` 的唯一写入者已删（旧执行器的共享时钟）；只剩 `_begin_turn` 读取和一条测试。
- `plan_tokens_used` 只剩序列化；`TodoItem.attempts` 只剩序列化（写入者在已删的 `plan_executor`）。
- `PlanReport.deliveries` 只剩 `render()` 里两处统计，没有写入者（`report.stopped` 是命令层唯一读的字段）。

**删除清单（逐文件）**
1. `domain/session.py`
   - 删字段 `step_item_id`、`plan_executing`、`item_deadline`、`step_deliveries`；删
     `StepDelivery` import。
   - `begin_plan`：删 `self.step_deliveries = []` 与 docstring 里的收据句（goal/items/ladder
     三项仍是它的职责）。
   - `to_dict` 删 `stepDeliveries` 块；`from_dict` 删对应读取。旧键自然被忽略。
   - **保留** `plan_implementing` / `plan_verify_items`（codex 实现轮与条目验收）。
2. `domain/task.py`：删 `TodoItem.attempts`（字段 + to_dict/from_dict 分支）与
   `TaskState.plan_tokens_used`（字段 + `planTokensUsed` 序列化）。旧 `attempts` /
   `planTokensUsed` 键读入即忽略，不需要兼容代码。
3. `domain/plan_delivery.py`：整文件删除（`StepDelivery` 生产者与消费者都已为零）。
4. `core/plan_report.py`：删 `deliveries` 字段与 `render()` 里依赖它的
   `unattested` / `on_claim` 两段；其余字段与 `render()` 保留。
5. `core/agent_loop.py`
   - 删 `_open_items_for`（唯一作用是按 `step_item_id` 收窄）；两处调用改
     `task.open_items()`。
   - 删 `plan_executing` 的进度签名块（`books.note_plan_progress`）、两处
     `plan_executing=` 快照投影、结构续跑块（`PLAN_IDLE_LIMIT`/`PLAN_STALL_ROUNDS`）、
     `verify_item` 的 `plan_executing` 或条件（保留 `plan_verify_items`）、
     `_begin_turn` 的 `item_deadline` / `plan_executing` 分支（保留 `plan_implementing`
     无时钟）、`_has_a_round_left` 的 `plan_executing` 或条件。
   - 删 `PLAN_IDLE_LIMIT` / `PLAN_STALL_ROUNDS` import。
6. `core/turn_policy.py`：删常量 `PLAN_IDLE_LIMIT` / `PLAN_STALL_ROUNDS`、
   `TurnSnapshot.plan_executing` / `TurnSnapshot.stall_rounds`、`decide_next_iteration`
   的 stall 块与 ceiling 里的 `plan_executing`、`allow_search` 里的 `plan_executing`、
   `decide_after_step` 的 plan 分支。**保留** `plan_implementing` 两处（codex 轮无上限、
   不续跑）。
7. `core/turn_books.py`：删 `idle_boundaries` / `stall_rounds` / `progress_calls` /
   `_progress_signature`、`note_idle_boundary` / `note_plan_progress`、`note_outcomes`
   的两处写入、`snapshot()` 的 `stall_rounds=`，以及 `BOOKKEEPING_TOOLS`（唯一消费者是
   `progress_calls`）。
8. `tools/todo_write_tool.py`：删 `executing_plan` 判定与 add/update 冻结块、
   `_frozen_change`、`_verify_on_completion`（update 分支改回直接返回 `_update`）。
   `verify` 参数描述删掉「During a plan run …」一句（该执行点已不存在；`update_plan`
   的 `plan_verify_items` 才是验收入口）。保留只读完成拒绝与 `parallel_safe=False`。
9. `tools/base.py`：删 `ToolContext.step_item_id` / `plan_executing`；保留
   `plan_verify_items` / `verify_item`。
10. `core/request_builder.py`：删对应两行透传。

**测试同步（删的是已删行为，不改仍然成立的断言）**
- `test_tool_todo_write.py`：删 6 条（step 冻结两条、`plan_executing` 冻结、
   两条完成即验收、`outside_a_plan_turn_the_proof_does_not_run`）。
- `test_auto_continue.py`：删 `test_open_items_for_narrows_to_the_steps_own_item`。
- `test_agent_loop_react.py`：删 `test_begin_turn_prefers_the_items_shared_deadline`。
- `test_turn_books.py`：删 stall / bookkeeping 两条。
- `test_domain_task.py`：`test_wave_fields_round_trip_only_when_set` 去掉 attempts 断言
  （files/kind 保留）；`test_unknown_wave_fields_degrade` 去掉 attempts 断言；
  删 `test_plan_tokens_round_trip_only_when_set`。
- `test_domain_session.py`：`test_begin_plan_resets_goal_items_and_receipts` 去掉
  `StepDelivery` 构造与重置断言，改名 `test_begin_plan_resets_goal_and_items`。
- `test_plan_command.py`：`test_plan_topic_clears_the_receipt_book_behaviourally` 去掉
  `StepDelivery` import / 构造与 `step_deliveries == []` 断言，保留其余（goal 重置、
  mode/readonly、落盘），改名 `test_plan_topic_starts_a_fresh_book_and_saves`。

**RIPR**
- **R**：新增 `test_a_session_file_with_old_plan_fields_still_loads`
  （`test_domain_session.py`）：`from_dict({"stepDeliveries": [...], "task":
  {"planTokensUsed": 5, "items": [{"id": "1", "content": "x", "attempts": 3}]}})` 成功，
  且 item 可读、无该字段、不抛异常。
- **I**：codex plan 回合仍 `FINISH`（`test_plan_codex_path.py` 不动）。
- **P**：全量 `pytest`；`ruff check ai_coding tests scripts`；`mypy ai_coding`；
  以下 grep 为空：`plan_executing`、`step_item_id`、`item_deadline`、`step_deliveries`、
  `StepDelivery`、`planTokensUsed`、`plan_tokens_used`、`from ai_coding.domain.plan_delivery`；
  `grep -n attempts ai_coding/domain/task.py` 为空。
  （`read_ledger` 的 docstring 仍以已删文件作路径规范化的历史实测样例，那是文本、不是
  依赖，不随删。）
- **R**：真机链路 `/plan` → `<proposed_plan>` → 批准 → `Implement the plan.` →
  `update_plan` 不变（`tests/test_plan_codex_*` 全绿）。

**实施记录（完成）**
- 产品代码按上述清单逐文件删除；`domain/plan_delivery.py` 整文件删除；
  `PlanReport` 本体保留（只去 `deliveries` 与 `render()` 的两段派生文本）。
- 测试按「测试同步」删改；新增旧键兼容用例
  `test_domain_session.py::test_a_session_file_with_old_plan_fields_still_loads`。
- `read_ledger.py` / `test_read_ledger.py` 里的 `plan_delivery.py` 只是路径样例，
  保持原样（早先尝试替换为 `task.py`，因属于无关改动已回退）。
- 校验：`pytest 4664 passed / 29 skipped / 0 failed`；`ruff` 全绿；`mypy` 174 文件全绿。
- **R**：真机链路 `/plan` → `<proposed_plan>` → 批准 → `Implement the plan.` →
  `update_plan` 不变（`tests/test_plan_codex_*` 全绿）。

**不删**：`acceptance`（P0b-2f 整体删契约）；`verify_items` / `_verify_plan_item`；
`plan_implementing` / `plan_verify_items`；`PlanReport` 本体。

**风险**
- dataclass 删字段后任何 `replace(plan_executing=...)` 都会 TypeError —— 已 grep 零处。
- `_open_items_for` 删除后 `task.open_items()` 返回新列表，调用点只读，无副作用。

#### P0b-2e：前端评审卡与 `plan.receipt` 退休

**事实前提（已核）**
- `present_plan` 后端已零注册（P0b-1），前端唯一的消费者是
  `component/plan-review.tsx`：它只在 `session.tsx` 的 `intent === "plan_review"`
  分支里出现，而后端现在只发 `intent="plan_implement"`（`app/commands.py:741-745`），
  由通用 `AskPanel` 渲染计划正文。
- `PlanStepReceipt` 在生产代码里**没有生产者**（`grep "PlanStepReceipt("` 为空），
  只剩 `core/events.py` 定义、`server/bridge.py` 映射、前端 reducer 分支与两条测试。
- `util/plan-agent.ts` **不是**评审卡专属：`isPlanAgentId` / `postureSuffix` 被
  home/session/composer-meta/status-bar/agent 使用，**必须保留**（原文「删
  util/plan-agent.ts」是错的）。
- `util/submission.ts` 的 `planPrelude` 仍被 `session.tsx:748` 使用（`/plan <主题>`
  前置），保留；`plan.updated` 的实时计划面板（`state/session.ts:704` +
  `test/plan-card-render.test.tsx`）保留。

**删除清单**
1. 删文件 `src/component/plan-review.tsx`。
2. `src/routes/session.tsx`：删 `PlanReviewPanel` import；ask 分支（约 `:841-857`）
   改为恒 `<AskPanel request={request()} onAnswer={props.onAskAnswer} />`。
3. `src/util/ask.ts`：删 `PlanItemData`、`planItems`、`planReviewItems`、
   `planReviewAnswer`、`submittedPlan`、`planText`、`planProse` 及表格/行内辅助
   （`FENCE`/`RULE`/`tableCells`/`SEPARATOR_CELL`/`isSeparatorRow`/`isTableLine`/
   `pad`/`renderTable`/`repeatsTheItems`/`inline`）与 `stringWidth` import；
   保留 `AskRequest`（`detail`/`intent`/`data` 仍是通用 ask wire 字段）、`ASK_SKIP`、
   `parseAskRequest`、`answerForSelection`；重写模块 docstring（不再提 plan review）。
4. `src/component/toolcard.tsx`：删 `submittedPlan`/`planText` import、`plan()`、
   `canExpand` 的 plan 项、展开区的 plan/items 渲染块。
5. `src/state/replay.ts`：删 `present_plan` 回填 `params` 的分支（约 `:227-233`）。
6. `src/state/session.ts`：删 tool part 的 `params?` 字段与 `case "plan.receipt"`；
   docstring 去掉 `present_plan`。
7. `src/util/toolmeta.ts`：删 `present_plan: "提交计划"`；删死错误码
   `frozen_plan`/`frozen_verify`/`frozen_item`（P0b-2d 已删产生者）与
   `no_plan_items`/`missing_plan`/`plan_keep`；**保留** `no_user`/`user_prompt_error`/
   `not_in_plan`/`task_busy`/`plan_already_approved`/`implementation_request`
   （`ask_user`/`finish_research` 仍产生）。
8. `src/component/permission.tsx`：两处以 plan-review 作参照的注释改为 AskPanel 说法
   （纯注释，无行为）。
9. `core/events.py`：删 `PlanStepReceipt` 类、`LoopEvent` 联合项、`__all__` 项；
   `server/bridge.py` 删该 import 与 `plan.receipt` 映射。

**测试同步**
- 删 `test/plan-review-render.test.tsx`、`test/plan-replay.test.ts`（整文件只测已删的
  卡片/`submittedPlan`）。
- `test/ask.test.ts`：删 `planReviewAnswer`/`planReviewItems`/`planText`/`planProse`
  的 import 与 describe；`parseAskRequest` 的 detail/intent 用例保留（标题不再提
  评审卡）。
- `test/toolcard-render.test.tsx`：删「a submitted plan opens into the plan that was
  actually sent」（`present_plan` 参数卡）。
- `test/replay.test.ts`：删两条 `part.params` 断言（`present_plan` 回填）。
- `test/reducer.test.ts`：删 `describe("plan.receipt")`。
- `test_server_bridge.py`：删 `PlanStepReceipt → plan.receipt` 用例。

**RIPR**
- **R**：`test/ask.test.ts` 保留 `parseAskRequest` 的 detail/intent 断言；
  `test/plan-card-render.test.tsx` 证明 `plan.updated` 面板仍活。
- **I**：`AskPanel` 对 `intent="plan_implement"` 仍渲染 `detail` 计划正文
  （`component/ask.tsx:119-123` 不动）。
- **P**：前端 `bun test` + `bunx tsc --noEmit`；后端全量 `pytest` + `ruff` + `mypy`
  （事件类删除后）。
- **R**：真机 `/plan` → `<proposed_plan>` → "Implement this plan?"（AskPanel）→ 是 →
  实现轮。

**验收**
- 生产代码（`ai_coding` 排除 `frontend/node_modules` 与测试目录）grep
  `plan_review|PlanReviewPanel|plan-review|present_plan|PlanStepReceipt|plan.receipt|submittedPlan|planReview|planText|planProse`
  为空；测试夹具里仍有 `present_plan` / `intent="plan_review"` 字符串（历史断言用，
  无生产者），属已知残留。
- 前端 `bun test` 全绿、`tsc --noEmit` 干净；后端全量测试绿。

**实施记录（完成）**
- 前端：删 `plan-review.tsx`、`util/ask.ts` 的计划解析/渲染半边、`toolcard` 计划卡、
  replay/session 的 `present_plan` 分支与 `params`、`toolmeta` 的 `present_plan`/死错误码；
  `AskPanel` 成为唯一问答卡（`plan_implement` 的正文仍走 `detail`）。
- 后端：删 `PlanStepReceipt` 事件与 `plan.receipt` 映射；删无生产者的
  `TurnConclusion.dismissed`；`_NOT_EVIDENCE_ERRORS` 收敛为
  `{CANCELLED_BY_USER, invalid_plan}`（`invalid_plan` 是 `update_plan` 现存的拒绝码）。
- 文案：`/plan` 提示、ask 通道注释、工具错误标签（「无人回答 / 提问通道异常」）随删同步。
- 校验：前端 `544 pass / 0 fail`、`tsc` 0 错误；后端全量 `pytest` 0 failed；ruff/mypy 绿。

**风险**
- `state/session.ts` 的 `params` 删除必须与 `replay.test.ts` 同步，否则类型与断言双红。
- 误删 `util/plan-agent.ts` 或 `planPrelude` 会打断 plan 姿态后缀与 `/plan` 前置；
  已核它们不是评审卡专属。

#### P0b-2f：acceptance 契约退休（保留 proof 执行器）

**事实前提（已核）**
- `session.acceptance` 的生产者（旧 plan_runner/plan_executor 与它们之后的补全）已删；
  生产代码里已无赋值点，它恒为空 —— `acceptance_states` / `pending_items` /
  `verify_acceptance` / acceptance 门 / `note_verify` / `U-ac-*` 账本项全部不可达。
- 唯一仍在工作的验收入口是 `update_plan`（`plan_verify_items`）→ `AgentLoop._verify_plan_item`
  → `core/acceptance.verify_items`。它只需要「一条命令 → (passed, output tail)」。
- `verify_command_problem` 无任何调用者（`present_plan` 已删）。
- `books.verify` 的唯一用途是 `build_ledger(verify=...)`，而它只产出 `U-ac-*` 项；
  `UncertaintyItem.resolves` 也只有 acceptance 会填，前端不读该字段。

**删除清单**
1. `domain/acceptance.py` 整文件删除。
2. `core/acceptance.py`：`verify_items` 改为命令级签名
   `verify_items(commands: Sequence[str], workspace, *, timeout_each, timeout_total,
   approval, deny_reason, extra_roots) -> list[tuple[bool, str]]`（每条命令跑一次，
   不再有「已通过跳过」）；删 `pending_items` import、`_approved(item…)`（改成
   `_approved(command…)`）、`verify_command_problem` / `_unresolvable_programs`，以及
   因此不再使用的 `dataclasses.replace` 与顶层 `shell_parse.command_views` import；
   `_needs_a_human` / `_relative_targets_outside` 保留（仍有测试消费）。
3. `domain/session.py`：删 `acceptance` 字段、`AcceptanceItem` import、`to_dict` 的
   `acceptance` 块、`from_dict` 的读取（`begin_plan` docstring 本就没有 contracts 句，
   无需改）。旧 `acceptance` 键读入时自然忽略（`from_dict` 只取已知键）。
4. `core/agent_loop.py`
   - 删 `AcceptanceItem` / `acceptance_states` / `pending_items` import（`verify_items` 保留）。
   - 删 `verify_acceptance`、`_run_verifications`、`_boundary_verdicts` 及其初始化、
     `reset_boundary_verdicts` / `boundary_verdict`。
   - `_verify_plan_item` 直接调命令级 `verify_items([command], …)`（readonly / 空命令短路
     保留；settings 的 `acceptance_verify_each/total_seconds`、session/agent 规则、
     approval、extra_roots 照旧传）。
   - 删两处 `acceptance=acceptance_states(...)`、预算 FINISH 的 `pending_items` + 补验收块、
     `if not step.tool_calls` 开头的 `await self.verify_acceptance(...)`。
   - acceptance nudge 分支改为无条件 `books.note_nudge()`。
5. `core/turn_policy.py`：删 `TurnSnapshot.acceptance` / `acceptance_nudges`、
   `TurnPolicyConfig.max_acceptance_nudges`、`LadderLimits(acceptance_l1=…)`、
   `decide_after_step` 的 `if snapshot.acceptance:` 整块（模型停止后直接进入
   task / stop_policy 判定）、`AcceptanceState` import。
6. `core/interventions.py`：删 `ACCEPTANCE`、`ACCEPTANCE_GATE_LEAD`、
   `LadderLimits.acceptance_l1`、`_triggered` 的 acceptance 尾分支、`_acc_sig`、
   `_helped` 的 acceptance 尾分支、`_due` 里 `gate == TASK or gate == ACCEPTANCE` 的共用逻辑、
   `select` POST 的 `gates = (TASK,)`、`_build` 的 acceptance finish/nudge 分支、
   `_acceptance_text`；同步模块 docstring 与常量注释。
7. `core/turn_books.py`：删 `verify` 字段、`note_verify`、`acceptance_nudges`、
   `note_acceptance_nudge`、`build_ledger(verify=…)` 传参、`snapshot()` 的
   `acceptance=` / `acceptance_nudges=`、`AcceptanceState` import。
8. `domain/ledger.py`：删 `build_ledger` 的 `verify` 参数与 `U-ac-*` 块、
   `UncertaintyItem.resolves` 字段与序列化键（无其它读者）。
9. `core/plan_reminders.py`：模型可见文案 `a real acceptance command exists` →
   `a real verification command exists`。
10. `config`：确认 `max_acceptance_nudges` 只是 `TurnPolicyConfig` 的 duck-typed `getattr`
    默认（随第 5 条消失，`AIConfig` 从未有该字段）；`acceptance_verify_each/total_seconds`
    在 `AIConfig` 里也没有字段，全仓只在 `agent_loop` 用 `getattr` 默认 120/300 读取，
    **保留**（proof 执行器的超时口径；改名属另一决策）。

**测试同步**
- `test_acceptance.py`：保留命令级执行/超时/只读/工作区门/审批用例，改调新签名；删契约
  往返、`pending/failed` helper、already-passed skip、post-budget 验收用例。
- `test_m1a_acceptance_subagent.py`：改为直接断言 `verify_items([...])` 的规则拒绝，
  不再写 `session.acceptance`。
- `test_acceptance.py` 同时承载原来写在 `test_domain_session` 的契约持久化用例，随契约删除。
- `test_domain_session.py`：`test_begin_plan_keeps_messages_and_acceptance` 收缩为只断言
  messages（改名），旧键兼容用例补上 `acceptance` 键。
- `test_turn_policy.py` / `test_interventions.py` / `test_claim_check.py` / `test_ledger.py`：
  删/改 acceptance 门与 `U-ac` 用例，保留 task/strategy 门覆盖（`test_claim_check` 只删
  验收先于引用检查的排序用例，引用检查本身仍有覆盖）。
- `test_m4_security.py`：只改 `verify_items` 的调用签名，无用例删除。
- `test_turn_books.py` 无 acceptance 引用，本步不改。
- 新增 `test_verify_items_runs_one_command_and_reports_its_verdict`（pass/fail 两条），
  证明保留的能力仍可执行。

**RIPR**
- **R**：先加命令级用例（旧签名不兼容即红），再改实现到绿。
- **I**：`update_plan` 的 `plan_verify_items` 仍能在条目完成时跑命令并回传失败。
- **P**：全量 `pytest` + `ruff` + `mypy`；前端 `bun test` + `tsc`（ledger wire 少 `resolves`
  字段，前端无读者）。
- **R**：契约标识符 `AcceptanceItem|AcceptanceState|acceptance_states|pending_items|failed_items|
  verify_acceptance|note_verify|acceptance_nudges|max_acceptance_nudges|U-ac-|boundary_verdict`
  在 `ai_coding`（排除 `frontend/node_modules`）为零；允许保留 `core.acceptance` 模块路径与
  `acceptance_verify_*` 两个 getattr 名。旧 session.json 含 `acceptance` 键仍能 load
  （未识别键自然忽略）。

**不删**：`verify_items`（命令级执行器）、`_verify_plan_item`、`plan_verify_items` 配置与
`update_plan` 的 verify schema、`acceptance_verify_each/total_seconds`、`_needs_a_human`。

**风险**
- `update_plan` 的验收入口依赖 `_verify_plan_item`，不能随契约一起删。
- `test_m3_boundaries.py` 仍 import `_needs_a_human`，该函数必须保留。

**实施记录（完成）**
- `domain/acceptance.py` 删除；`core/acceptance.verify_items` 收敛为命令级
  `list[tuple[bool, str]]`，总预算耗尽 fail-closed（"not checked: time budget exhausted"）；
  `verify_command_problem` 等无调用者函数一并删。
- `session.acceptance` 与两处快照投影、回合末验收、acceptance 门/账本项
  （`U-ac-*`、`resolves`）、`note_verify`、`max_acceptance_nudges` 全部删除；
  `_verify_plan_item` 直接跑命令，`plan_verify_items`/`update_plan` 契约不变。
- 测试：契约持久化/pending/helper/post-budget 用例删除；命令级 pass/fail 与
  预算 fail-closed 用例新增；`test_m1a` 改为直接断言规则拒绝。改动只发生在
  `test_acceptance/test_m1a_acceptance_subagent/test_domain_session/test_turn_policy/
  test_interventions/test_claim_check/test_ledger/test_m4_security`。
- 校验：后端全量 `pytest` 0 failed（4654 passed / 29 skipped）；`ruff`、`mypy`（173 文件）
  全绿；前端 `bun test` 544 pass、`tsc` 0 错误。

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
- 前端：删 `component/plan-review.tsx` 与只服务它的 `util/ask.ts` 计划解析/渲染
  函数、`toolcard` 的计划卡、`state/replay.ts` 的 `present_plan` 回填、
  `state/session.ts` 的 `plan.receipt` 分支、`toolmeta` 的 `present_plan` 与死错误码，
  以及 `ask.test.ts`/`reducer.test.ts`/`plan-review-render.test.tsx`/`plan-replay.test.ts`
  的旧用例。**保留** `util/plan-agent.ts`（姿态后缀/agent id）、`planPrelude`、
  `plan.updated` 计划面板。详见 P0b-2e。
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
