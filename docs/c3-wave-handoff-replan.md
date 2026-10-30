# C3 收尾方案：波交接（wave_report）与波边界重规划

> 状态：**已实现**（2026-10-30；见 §11 决定与 §8 用例/突变，真机已验）。C2 已落地串行波执行；B0/r25 已完成 build 并行调研与子代理交接。
> 本方案只做 `plan-orchestration-c-landing.md` §9「C3 演化与并行」里**还没做**的部分，并先
> 用代码核对过哪些已经在 C2 里落地、哪些是真的缺口。
>
> 范围待 §11 拍板：`wave_report` / 波边界 replan / 前端波次进度。

## 1. 先理清楚：里程碑还剩什么

C 方案的三个收尾项，对照当前代码：

| 项 | C landing 的设想 | 代码现状 | 结论 |
|---|---|---|---|
| `wave_report` 工具 | 模型提交 interfaces/decisions/pitfalls/next_hint，机械账本覆盖模型说法 | **不存在**；波末只有 `_build_wave_note` 的机械字符串（完成/跳过/改动） | 缺口 |
| 波边界 replan | deadlock / `needs_replan` / blocked≥1/2 → 用 `todo_write` 改剩余项，≤2 次 | **不存在**；`plan_max_replans`、`PlanReport.replanned` 是**没有被读的字段** | 缺口 |
| 前端波次进度 | 新 `plan.wave` 事件 + 计划卡波次提示 | `plan.updated` 已带 `roundsRun`（已完成的波数），卡片没显示 | 缺口（可小改） |
| `kind=research` 波 | 复用 ResearchFanOut | **已放弃**（r25/B 证明执行回合里模型本来就能调 `parallel_research`，见 `docs/c3-research-wave-plan.md` §13） | 不做 |
| `plan_version` | replan 时 +1 | **不存在**；`plan.updated` 每次重画整张列表，版本号没有消费者 | 不做（除非 §11 要求） |

**真实缺口 = `wave_report` + replan + 前端波次显示。**

## 2. 现状数据流（只列与本方案相关的）

```text
WaveRunner._run_wave（core/wave_runner.py:330-395）
  ├─ report.waves += 1
  ├─ wave_session = ephemeral SessionData（seed = goal + task.wave_note + 本波条目）
  ├─ while 有 ready 条目：_run_item（回合 + 验收 + 收据 + blocked→skip）
  ├─ 波末：task.wave_note = _build_wave_note(session, task, wave)   ← 机械账本
  │        task.plan_tokens_used += _wave_tokens
  └─ persist / emit
TaskDriver（core/task_driver.py:78-140）
  ├─ next_item(task, attempted) → 下一个 WaveItem
  ├─ run_round → _run_wave
  └─ no_item → deadlock / stalled 结束
```

相关事实：

- 波会话 `agent="build"`、`readonly=False`（`_make_wave_session`），所以 `todo_write` 可用；
  `present_plan` 是 `plan_only`，在波会话里不可见（`AgentLoop._available_tools`）。
- `_build_wave_note` 只写「已完成/跳过/改动」；没有接口、决定、坑、下一波提示。
- `PlanReport.replanned`（`plan_report`）与 `plan_max_replans`（`config/models.py:494-496`）
  **没有任何读取点**；`TaskState` 没有 `plan_version`。
- `plan.updated` 携带 `roundsRun`（`server/bridge.py:176-188`），前端计划卡只画条目
  （`frontend/src/component/message.tsx:100-160`），没画波次。
- 波内模型可以用 `todo_write` 改列表；但没有「只改剩余项、不许实现」的专用回合。

## 3. 外部调研（怎么设计波交接与重规划）

| 来源 | 结论 | 落点 |
|---|---|---|
| Plan-then-Execute playbook（mosaic/lab）：replan on deviation | 「只重写**剩余步骤**，用 <80 词说明改了什么、为什么」；**停下等批准**再继续 | 交接要短、结构化；replan 需要一个明确的模型回合 |
| Stuck-Loop Recovery（AgentPatterns） | 卡住时先做一次**结构化反思**（重述目标、试过什么、下一版计划），再行动；成本高于提醒但值得 | replan 回合的提示词模板：goal + 已尝试 + 失败原因 |
| Agentic UX / HITL 资料 | 不可逆/高风险动作需要人工确认；普通代码计划可以自动，但改动要可见 | replan 自动改剩余项是否要人工，是产品决策（§11 Q2） |
| 本仓库 C landing §7 | 触发：deadlock / `needs_replan` / blocked≥1/2；动作：`todo_write` 改剩余项，≤2 次；`plan.updated` + notice | 直接沿用其语义，本方案补上落点 |
| 本仓库 v2 子代理结论 | 交接要「模型自己读得到的完整结构化内容」，不要挤进一个 900 字字段；诊断信息别进模型面 | 波交接单独存结构化对象，按需渲染进 seed |

## 4. 方案设计

### 4.1 `wave_report`：模型提交的结构化交接

**工具 schema（`tools/wave_report_tool.py`，新）**：

```json
{
  "interfaces": ["新增/改变的接口签名，≤200 字/条，≤12 条"],
  "decisions":  ["本波做出、后续要遵守的决定，≤200 字/条，≤12 条"],
  "pitfalls":   ["踩过的坑/不可行的路径，≤200 字/条，≤12 条"],
  "next_hint":  "给下一波的一句话建议，≤400 字，可空",
  "needs_replan": false
}
```

- 只有 `interfaces`/`decisions`/`pitfalls`/`next_hint` 是模型内容；**文件改动与 verify 结果由
  执行器从收据/产物里机械生成并覆盖**，模型说了也不算。
- 容量在工具里校验（超限拒绝，不静默截断）；总渲染上限 `WAVE_HANDOFF_MAX_CHARS = 2_400`。
- 可用性：**只在计划执行波内可见**。
  - `BaseTool` 新增 `wave_only: bool = False`；`AgentLoop._surface_tools` 在
    `armed_profile != "plan"` 或 `task.wave_active != True` 时不广告它（与 `plan_only` 同一
    机制，不新造一套）。
  - 工具执行时再校验一次 `ctx.task.armed_profile == "plan"` 且 `wave_active`，否则返回
    `error="not_in_wave"`（防止模型从记忆里调用）。
  - builder 在主栈注册；普通会话/plan 规划阶段看不到它，schema 成本为 0。
- 强制力：**可选**。模型不调用 → 只有机械账本（C2 行为），不报错、不追问。

**存储与注入**：

- `TaskState` 新增 `wave_handoff: dict[str, Any] | None`（只保留**最近一波**），
  `to_dict()/load` 往返（键 `waveHandoff`，空则不写）。它只有一个消费者：`_seed`。
- `_run_wave` 波末：把机械账本（done/skipped/files）与 `wave_handoff` 组装成
  `WaveHandoff`（`domain/plan_wave.py`，frozen，纯函数 `render(max_chars)`），
  存 `task.wave_handoff`，并继续写 `task.wave_note`（机械短句，给每步的 task-state 用）。
- `_seed`：有 `wave_handoff` → 渲染结构化交接（**只在波首注入一次**，不是每步）；
  没有 → 退回 `wave_note`。这样两段信息的用途分开：
  - `wave_note`：每步 task-state 里的 900 字机械状态；
  - `wave_handoff`：下一波 seed 里的结构化经验（接口/决定/坑/提示），不重复进每步请求。

### 4.2 波边界重规划（改完**停下等人工批准**）

用户拍板：重规划不是自动改，而是「模型改剩余项 → 人批准 → 继续 / 撤回并停止」。

**触发**：

1. 波末 `wave_report.needs_replan == true` → pending；
2. `next_item` 发现 open 项还有、但没有可运行波（deadlock）→ 发起。

（blocked≥1/2 不作为自动触发；它只在波末提示模型自己考虑 `needs_replan`。）

**流程**：

1. **快照**：`saved_items = copy.deepcopy(task.items)`（只快照条目；handoff 不受影响）。
2. **重规划回合**：`_run_replan` 新建 ephemeral 会话（同 `_make_wave_session` 的构造），
   seed = 目标 + 当前 open 项 + 触发原因（deadlock / 模型的 needs_replan / 失败摘要）；
   policy = `plan_step_max_iterations` / `natural` / `max_auto_continuations=0`；
   提示词：**只允许**用 `todo_write` 改剩余项（add / update open 项 / drop open 项），
   不得执行工作、不得改目标；没有要改的就回答 `NO_REPLAN`。
   - 会话带 `replan_mode=True`（瞬态）；`todo_write` 在该上下文里只允许操作**非终态**条目，
     拒绝 `completed`/`skipped`，拒绝把终态改回 pending。
3. **无改动**（模型回答 `NO_REPLAN` 或列表未变）：`report.stopped =
   "needs_replan: 模型没有修改剩余项"`，本轮结束（不需要麻烦人）。
4. **有改动** → 通过注入的 `user_prompt` 问人：
   - 问题：`计划已重规划：批准新的剩余项并继续执行？`
   - 选项：`("继续执行", "停止计划")`
   - `detail` = `task.render_display()`（带 marker 的完整列表，人看到的就是当前列表）；
   - `no_user` / 未回答 / 选「停止计划」→ **恢复快照**、persist、emit `plan.updated`，
     `report.stopped = "replan rejected: 等待人工重新规划"`，本轮结束。
   - 选「继续执行」→ 保留新列表，`report.replanned += 1`，persist、emit，继续 `next_item`。
5. **额度**：`plan_max_replans`（默认 2）按 `report.replanned` 计；被拒即停止，不会继续尝试。
6. **没有 prompt 通道**（测试 / 非交互入口）：视为「不能批准」→ 恢复快照并停止，
   绝不静默自动改计划。

**注入方式**：

- `plan_runner_for(loop, user_prompt=None)` → `WaveRunner(loop, user_prompt=...)`；
- `server/handlers.py` 调 `handle_input(..., user_prompt=world.user_prompt)`；
- `app/commands.py::handle_input` 把 `user_prompt` 传到 `_handle_plan`；
- 没有 prompt 的调用点保持 `None`（保守停止）。

**不做**：`plan_version`；新事件；自动改写。`plan.updated` 在重规划回合里已经由
`todo_write` 驱动刷新，人看到的就是待批准的列表。

### 4.3 前端波次进度

- `TaskState` 增加两个**瞬态、不序列化**字段：`wave_active: bool`、`wave_items: tuple[str,...]`
  （当前波的条目 id）。`_run_wave` 开始/结束设置；`_emit_task` 在开始与结束各发一次
  `plan.updated`。
- `server/bridge.py` 的 `plan.updated` payload 增加：
  ```json
  "wave": {"active": true, "items": ["3","4"]}
  ```
  没有 run 时不写（`None`/省略），旧客户端不受影响。
- 前端 `PlanState`（`state/session.ts`）加可选 `wave`；`message.tsx` 计划卡标题行显示
  `· 第 N 波`（N = `roundsRun` + 1，若 active）或 `· N 波`；条目前加当前波标记可省略。
  **不新增 `plan.wave` 事件**：`plan.updated` 已经承载 task 状态，单独事件会多一条 bridge +
  reducer + replay 路径，收益只有一行字。
- replay：波字段不持久化，重开会话不显示——与「波会话/运行中途不恢复」一致，可接受。

## 5. 数据流（v3）

```text
波开始：set wave_active + wave_items → emit plan.updated（前端显示第 N 波）
  └─ 回合中：模型可调 wave_report（仅波内可见）
        → ToolResult.data = {interfaces, decisions, pitfalls, next_hint, needs_replan}
        → 执行器在波末读取
波末：机械账本(done/skipped/files/verify) + wave_report → WaveHandoff
      → task.wave_handoff（持久化，只留最近一波）
      → task.wave_note（机械短句，每步可见）
      → 若 needs_replan → pending
set wave_active=False → emit plan.updated
next_item：
  ├─ pending / deadlock 且 replanned < cap → WaveItem(replan=reason)
  │    └─ _run_replan：ephemeral 会话 → todo_write 改剩余项 → plan.updated
  └─ 否则 → 正常 WaveItem
下一波 seed：task.wave_handoff.render()（结构化，一次性）或 wave_note
```

## 6. 逐文件改动

| # | 文件 | 改动 | 为什么 |
|---|---|---|---|
| 1 | `ai_coding/domain/plan_wave.py`（新） | `WaveHandoff`（frozen）+ 机械账本合并 + `render(max_chars)` | 纯函数、可单测；只服务种子注入 |
| 2 | `ai_coding/domain/task.py` | `wave_handoff`（持久化）、`wave_active`/`wave_items`（瞬态） | 交接载体与前端波次 |
| 3 | `ai_coding/tools/wave_report_tool.py`（新） | schema、校验、wave-only 门禁、写入 `task.wave_handoff` | 模型入口 |
| 4 | `ai_coding/tools/base.py` | `wave_only` 属性 | 工具面裁剪 |
| 5 | `ai_coding/core/agent_loop.py` | `_surface_tools` 按 `wave_only` + armed/wave_active 裁剪；surface cache key 加 `wave_active` | 普通会话零成本 |
| 6 | `ai_coding/tools/builder.py` | 主栈注册 `wave_report` | 波内可见 |
| 6b | `ai_coding/tools/todo_write_tool.py` + `core/request_builder.py` | `ToolContext.replan`；replan 回合的 `todo_write` 只允许改 **open 项**（拒绝 completed/skipped，拒绝把终态改回 pending） | 防止重规划改写历史 |
| 7 | `ai_coding/core/wave_runner.py` | 波首/波末设置瞬态字段；`_build_wave_handoff`; `_seed` 用 handoff；`WaveItem.replan`; `next_item` 发起 replan；`_run_replan` | 编排主体 |
| 8 | `ai_coding/server/bridge.py` | `plan.updated` 加 `wave` 块 | 前端 |
| 8b | `ai_coding/core/wave_runner.py` / `app/commands.py` / `server/handlers.py` | `plan_runner_for`、`handle_input`、`_handle_plan` 注入 `user_prompt`；`_run_replan` 快照/恢复/提问 | 人工批准 |
| 9 | `ai_coding/frontend/src/state/session.ts` + `component/message.tsx` | `PlanState.wave` + 标题行显示波次 | 前端 |
| 10 | 测试 | 见 §8 | RIPR |

**不新增**：事件类型、`plan_version`、第二个波执行器、第二个 `todo_write`。

## 7. 关键契约

### 7.1 `WaveHandoff`（`domain/plan_wave.py`）

```python
@dataclass(frozen=True)
class WaveHandoff:
    done: tuple[str, ...]
    skipped: tuple[str, ...]
    files: tuple[str, ...]
    verify: tuple[dict[str, Any], ...]   # {item_id, command, passed, tail?}
    interfaces: tuple[str, ...]
    decisions: tuple[str, ...]
    pitfalls: tuple[str, ...]
    next_hint: str
    def render(self, max_chars: int = WAVE_HANDOFF_MAX_CHARS) -> str: ...
```

- 机械字段由执行器填；模型字段来自 `wave_report`；**同名字段以机械为准**（模型不提交文件/verify）。
- `render()` 固定分段顺序，超限按「坑 → 决定 → 接口」从旧到新裁剪，并在末尾标 `…`。

### 7.2 `wave_report` 的边界

- 仅 `armed_profile=="plan"` 且 `wave_active` 时可用；否则 `not_in_wave`。
- 超 12 条 / 单条 200 字 / `next_hint` 400 字 → `invalid_report`，不静默截断。
- 不改变任务状态（只写交接），不触发 replan 以外的动作。

### 7.3 replan 的边界

- 只改**剩余** open 项；已完成/跳过/受阻的项不动（模型若试图改，`todo_write` 会拒绝改动
  非 open 项？——需在 replan 提示里声明；现有 `todo_write` 对 completed 的更新没有禁止，
  这是 §10 的一个自检点）。
- 不改 goal；`todo_write` 没有改 goal 的 action。
- 额度：`plan_max_replans`（默认 2），按 `report.replanned` 计数，每次 replay 尝试都计数。
- 失败/无改动：不报错，额度照扣，继续下一波或按原逻辑结束。

## 8. RIPR

| 相 | 动作 | 判据 |
|---|---|---|
| **R** | 先写用例 | 全红 |
| **I** | §6 的 1–9 | 转绿 |
| **P** | 相关 + 全量 | 0 failed |
| **P** | `ruff`/`mypy`/前端 | 干净 |
| **P** | 突变 A：`wave_report` 不校验条数 | 上限用例红 |
| **P** | 突变 B：机械账本被模型字段覆盖 | 合并用例红 |
| **P** | 突变 C：`wave_only` 不裁剪 | 普通会话工具面用例红 |
| **P** | 突变 D：deadlock 不发起 replan | deadlock 用例红 |
| **P** | 突变 E：replan 不 `todo_write` 也能过 | 改写用例红 |
| **P** | 突变 F：`_seed` 用 `wave_note` 不用 handoff | 注入用例红 |
| **R** | `git status --porcelain` | 仅 §6 文件 |

### 8.1 用例

| # | 用例 | 期望 |
|---|---|---|
| W1 | `wave_report` 12/13 条、200/201 字、hint 400/401 | 12/200/400 过，超限 `invalid_report` |
| W2 | 模型提交 `interfaces` 但执行器机械账本有 `files` | handoff 的 files 来自机械，模型字段保留 |
| W3 | 普通 build 会话 / plan 规划会话的请求面 | 不含 `wave_report`；波会话含 |
| W4 | 波会话调用 `wave_report` 后波末 | `task.wave_handoff` 有模型字段；`wave_note` 仍有机械句 |
| W5 | 下一波 seed | 含 `[上一波交接]` 的接口/决定/坑/hint，且只出现一次；无 handoff 时退回 note |
| W6 | deadlock（open 项依赖环） | `next_item` 返回 replan；重规划后若仍 deadlock 且额度用完 → `deadlock` 结束 |
| W7 | `needs_replan=true` | 波末后下一轮先 replan，再继续正常波 |
| W8 | replan 回合用 `todo_write` add/update/drop → 人批准 | 列表保留，`plan.updated` 发出，`report.replanned` +1，后续波继续 |
| W9 | 人拒绝 / 无 prompt 通道 | 恢复快照、`plan.updated` 回到旧列表、`report.stopped` 说明，run 结束 |
| W9b | `plan_max_replans=0/2` | 0 不 replan；2 次批准后不再发起 |
| W9c | replan 里 `todo_write` 改 completed 项 | 被拒绝（`replan` 上下文只允许 open 项） |
| W10 | 前端 `plan.updated` 带 `wave` | 计划卡显示「第 N 波」；无 wave 时不显示 |
| W11 | 旧客户端/replay | 没有 `wave` 字段也能正常渲染 |

## 9. 成功后的样子

- 波结束时模型用一次 `wave_report` 交接口/决定/坑/下一波提示；机械账本保证文件与 verify 不撒谎。
- 下一波的 seed 里有结构化交接（而不是一条 900 字的流水句）；每步 task-state 仍只有短机械 note。
- 计划死锁时自动重规划一次（默认上限 2），改的是剩余项；`plan.updated` 一闪，卡片立即更新。
- 计划卡标题显示「第 N 波 · #3,#4 进行中」；重开会话不显示（运行中途不可恢复，已知）。
- 普通 build 会话的请求面**没有** `wave_report`，tool schema 成本为零。

## 10. 自检 / 风险

1. **`todo_write` 目前能改终态项**（`_update` 只拦了 `skipped`，completed 可以被改回
   pending/改 content）。重规划若放任，会改写历史收据对应的条目。决定：replan 会话给
   `ToolContext` 加 `replan=True`（由 `SessionData.replan_mode` 传入），`todo_write` 在该
   上下文里只允许操作**非终态**条目，且拒绝 `completed/skipped` 状态；普通会话行为不变。
2. **deadlock 判定口径**：`next_item` 只在“没有可运行波”时发起 replan；如果 open 项全是
   stalled（模型没做完），不发起，保持 C2 的 stalled 结束。避免把「模型没做完」当成死锁。
3. **重规划无改动**：额度照扣，避免同一原因无限尝试；`attempted` 保证同一次 run 不重复。
4. **前端瞬态字段**：不序列化；重开会话没有波信息，与运行中途不可恢复一致。
5. **预算**：replan 回合计入计划 token 账；episode 预算耗尽时停止，不再发起。
6. **不新增 plan_version**：没有消费者；若 UI 以后要「计划已改版」提示再加。
7. **wave_report 可选**：模型不调用时行为与 C2 一致，不引入新的失败面。

## 11. 决定（2026-10-30）

| 问题 | 决定 |
|---|---|
| 范围 | **三项全做**：wave_report + replan + 前端波次 |
| replan 权限 | **停下等人工批准**：模型改剩余项 → `user_prompt` 问人 → 批准继续 / 拒绝恢复快照并停止 |
| 触发 | **deadlock + 模型 `needs_replan`**；blocked≥1/2 不自动触发 |
| 前端 | **复用 `plan.updated`** 加瞬态 `wave` 块，不新增事件 |
