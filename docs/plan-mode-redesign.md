# Plan 模式重构：决策记录（P0–P2）

> 状态：已拍板，待实施。本文是实施前的唯一真源；实施完成后随代码更新。
> 取证基础：`.ai-coding/traces/ec89d5ee/`（4 回合 166 事件）＋
> `docs/orchestration-research/` 的 oc/dsh 材料。

## 1. 根因（实测，非推断）

会话 `ec89d5ee` turn-1 跑了 **25 次迭代**，只推进 **1 个 todo 项**，终答是
"当前状态总结 / 无实质性工作"。三个根因叠成一条死链：

### A. `clear` 是自陷陷阱，guard 把它变成硬墙

从 trace 逐次提取的真实参数序列：

```
it1  add id=1   → 成功        it2  clear → 成功（列表清空，goal 一并清）
it3  add id=1   → 成功        it4  clear → 成功
it5  add id=b1  → 成功        it7  clear → duplicate_call 拒绝 ← 死循环
                              it8 / it11 / it15 → 同样被拒
```

三个实测事实：

1. `plan_runner.py:83` 已明写 "never clear the list to rewrite it"，
   模型仍然 clear —— prompt 警告无效。
2. 两次 `clear` 返回**完全相同**（`<task-state>\n(无任务项)\n`），
   模型无法从返回值察觉"这个动作零进展"。
3. 拒绝文案是 `"reuse the earlier result"` —— 对幂等清空毫无意义，
   模型读成"我参数写错了"，于是换措辞重试（it15）。

### B. 8 项计划要 8 次迭代

`todo_write.params_json_schema` 无 `items` 数组（实测输入只有
`action/content/id/status/evidence/depends_on/verify`）；trace 里**每次
iteration 的 `tool_calls` 数量恒为 1**。iter13 的模型 reasoning 原文：

> "The user's budget is running low (13/15 steps). I need to wrap up this turn."

### C. `clear` 后模型失忆

`TaskState.clear()`（`task.py:209-225`）连 `goal` 一起清，模型无法区分
"还没写"与"写了又被清掉"。

### 我上一轮的误判（已撤回）

曾判断 "present_plan 批准后只读没关"。实测推翻：批准 → 前端
`pendingPlanRun`（`session.tsx:627`）→ `turn.finished` 时 `dispatch("/plan run")`
（`session.tsx:494`）→ `commands.py:504` 的 `finally` 里
`session.mode, session.readonly = "normal", False`。trace 实测：turn-2 的
bash 全部成功，链路正确。

## 2. 三方对比（依据本仓库 docs）

| 维度 | opencode | dsh | 我们（现状） |
|---|---|---|---|
| 形态 | 独立 agent 插件（`06:42,34-36`） | 布尔状态 + 提示词（`05:16,18`） | `mode`/`readonly` 字段 + prompt 段 |
| 规划期 todo | **没有 todo 工具**（`06:63-68`） | **prompt 明令禁用**（`08:122`） | **todo 是主干** |
| 产物 | 纯对话文本（`11:114`） | `exit_plan_mode` 携带完整 markdown（`05:23`） | todo 项 + `present_plan` |
| 执行衔接 | 手动切 agent（`18:29`） | 批准后只翻 `plan/mode{active:false}`（`05:19,24`） | 自动 `dispatch("/plan run")` |
| 防重复 | 无（`14:96` "本阶段未核"） | `repeat-tool-reminder` 只提醒不否决（`08:196`） | `duplicate_call` 硬拒 |

dsh 的原文（`08:122`）是本轮设计的第一依据：

> Do not use todo_write to track this planning phase: it tracks implementation
> after an approved plan, while the plan itself belongs in exit_plan_mode.

**关键事实**：todo 在我们代码里**不是全局承重**。实测消费者：

| 消费者 | 依赖 items | 规划期需要 |
|---|---|---|
| `PlanRunner:370` 逐项执行 | 强 | 执行期才有 |
| `acceptance.py:53` 用 `item.verify` | 强 | 执行期才有 |
| `plan_runner:658` 验收契约 | 强 | 执行期才有 |
| `turn_policy:337` stop 门禁 | 强 | 规划期改为门禁 `present_plan` |
| `goal_runner:263` | **只用 goal + 计数器** | — |

## 3. 拍板决策

### P0 — 规划期禁用 todo + 删 `clear`

* **删 `todo_write` 的 `clear` action**（`todo_write_tool.py:121`）。
  dsh 无此动作（`05:62-64` 只有整表替换），opencode 无 todo 工具。
  **`TaskState.clear()` 一并删除**：它的唯一驱动就是那个 action（实测
  `/goal` 不调用它），两个领域测试随主体删除；`test_plan_m1` 里用它做
  场景铺垫的一处改为直接构造新 `TaskState`。
* **`clear` 删除后**，`plan_runner.py:83` 的 "never clear" 警告同步删除。
* **prompt 禁用规划期 todo**（对齐 dsh）：`prompt.py` 的 plan-mode 段加一句
  "规划期用 present_plan 交付计划，不要用 todo_write 跟踪规划过程"。
  工具**仍注册**（dsh `05:25`：工具目录跨模式恒定，为 request-cache 稳定）。
* **门禁改判据**（P2 细化）：`present_plan` 成功即视为已提交，
  见 P2。

### P1 — `present_plan` 接受结构化 items

```
present_plan({
  plan:  str,                              # markdown，给人看（现有）
  items: [{content: str, verify: str}]     # 结构化，执行器消费（新增）
})
```

* 复用现有 `no_plan_items` 错误码（`present_plan_tool.py:99`）表达"没给 items"。
* 批准分支**替换**列表（dsh 的整表语义）：`items.clear()` + 逐项
  `TaskState.add()`（复用既有 id 分配）。追加会让违反 prompt 预写 todo 的
  模型留下孤儿步骤被一并执行——真实跑通时复现过（批准 2 项、任务里 3 项）。
* **不给 `todo_write` 加批量**：那是让规划期继续产出结构化列表，正是失败场景。
  放进 `present_plan` 才是"规划期只产出一段 markdown + 一次结构化提交"。

### P2 — 门禁与收尾（实施中修正了原判断）

* **原计划说错了一处**：`turn_policy.py` 的 plan 模式分支本来就按
  `plan_submitted` 判定（`PlanSubmissionGuard.on_stop` 只看
  `snapshot.plan_submitted`，不看 `open_items`），`:337` 的
  `not snapshot.open_items` 是 **normal 模式**的分支。所以「改判据」是
  空操作；实测用例 `test_the_plan_gate_asks_even_with_an_empty_list` 钉住
  了「没有 items 也会追问提交」。
* **真正需要修的是事件**：`agent_loop.py:1506` 的 `TaskUpdated` 触发名单是
  `("todo_write", "start_goal", "update_goal")` —— **不含 `present_plan`**。
  批准写了 items 但前端收不到事件，计划卡永远不更新。已把
  `present_plan` 加入。
* 前端**零改动**：`plan.updated` 已是 `{goal, items[]}`
  （`session.ts:564-579`）。
* `PLAN_NUDGE` 文案同步（不再教 todo_write，改指 items）。

### Q4 — guard 拒绝文案（不回滚 D1-a）

* **不引入"幂等白名单"**。实测反例：`ask_user` 无"已问过"保护，连续 3 次
  真的问了人 3 次 —— 按幂等分类会放行它，是倒退。
* **不回滚 D1-a**（`duplicate_call` 只计成功）：方向正确。
* **只改拒绝文案**：按工具给出正确指引，`read` 说"复用早先结果"，
  `ask_user` 说"你已经问过"，`write/edit` 说"这个文件已写过"。
  修正《导入路径》——`guard.py:DuplicateCallGuard.check` 的 reason 分支。

## 4. 一手源码核实（本轮新增，推翻了两处二手文档）

发现真实源码库在 `/home/linsir365/projects/opencode` 与
`/home/linsir365/projects/deepseek-harness`，据此重核全部关键论断：

| 论断 | 二手文档 | 一手源码 | 结论 |
|---|---|---|---|
| dsh 规划期禁 todo | `08:122` | `bundle/web-app/presets/standard.patch.yml:59` 原文一致 | 确认 |
| dsh `exit_plan_mode` schema | 文档说"markdown" | `plan-mode/src/index.ts:281` **只有 `plan` 一个字段** | 确认且更精确 |
| dsh `todo_write` 批量 | "整表替换" | `todo/tool-todo/src/index.ts:80` `toTodoList(数组)` | 确认 |
| dsh 禁用靠 prompt | `05:26` | `plan-mode/index.ts` 全文 grep 无 `todo` | 确认 |
| oc 对账机制 | `06:38` | `packages/core/src/plugin/plan.ts:57-73` | 确认 |
| oc plan 权限 | `18:43` | `plan.ts:38-41` 四条 push 可见 | 确认 |

### 与 dsh 的一处刻意分歧

dsh 的 `exit_plan_mode` **刻意不做结构化**（`index.ts:281` 只有 markdown）。
我们选 `present_plan({plan, items})` 是两家的折中：

* 多结构化 items → 换来 `PlanRunner` 机械执行 + `verify` 验收（我们的独有能力，
  dsh 明确"无对照 plan 的核对机制"，`11:116`）
* 少 todo 交互 → 消除"8 项烧 8 轮"循环

代价：模型要同时产出 markdown 与 items，认知负担高于 dsh 的纯 markdown。
**这是合理差异而非偏离**——dsh 不需要结构化是因为它没有执行器。

## 5. 删 todo 后的目标数据流（逐帧）

```
用户 /plan "设计酒店系统"
  │
  ├─ commands.py:503  request_posture(mode=plan, readonly=True)
  │
  ├─ 迭代 1..N（规划期）
  │   请求组装 request_assembly.assemble()
  │    ├─ instructions: prompt.py <plan-mode> 段（每请求恒在，18:75）
  │    │   ★ 新增："规划期用 present_plan 交付，不要用 todo_write 跟踪"
  │    ├─ tools: 全量注册（todo_write 也在，保 prefix cache，对齐 05:25）
  │    └─ tail: environment / memory / task(空)
  │
  ├─ 模型调用 present_plan({plan, items})
  │    ├─ present_plan_tool.py:98  items 为空 → no_plan_items（复用错误码）
  │    ├─ user_prompt.ask("批准这个计划并开始执行？", ("开始执行","继续规划"))
  │    │
  │    ├─【批准】ctx.task.armed=True（现有 L141-143）
  │    │   ★ items **替换**列表（items.clear() + 逐项 add()，复用 id 分配）
  │    │   → TaskUpdated → plan.updated → 前端卡片（现有链路，零改动）
  │    │
  │    └─【继续规划】→ _KEEP 文案带 feedback（现有）
  │
  ├─ turn.finished ─→ 前端 pendingPlanRun ─→ dispatch("/plan run")
  │
  └─ commands.py:463 【/plan run】
       ├─ request_posture(mode=normal, readonly=False)（现有 L466）
       ├─ PlanRunner.run()  ← 消费 task.items 逐项执行
       │    plan_runner.py:370  open_items() 过滤（现有）
       │    acceptance.py:53    用 item.verify 验收（现有）
       └─ finally: mode,readonly = "normal",False（现有 L504）
```

**零改动的承重件**：`PlanRunner`、`acceptance`、前端卡片。items 依然落在
`TaskState`，只是来源从"逐项 add"变成"一次提交"。

## 6. 实施结果（已验证）

1. `clear` 已删；`plan_runner.py` 的 "never clear" 警告同步删除。
2. 门禁：本来就按 `plan_submitted`，无需改；新增用例钉住"空列表也会追问"。
3. `PlanRunner` 直接消费批准写入的 items：集成用例
   `test_items_approved_through_present_plan_run_to_completion` 全绿
   （verify 走验收契约、无 verify 走模型自报）。
4. guard 拒绝文案：按工具给指引（read 复用结果 / ask_user 已回答 /
   write、edit 文件已写 / todo_write 列表已反映）。
5. **真实链路跑通**（真实 AgentLoop + 真实工具，脚本在
   `/tmp/opencode/planmode_e2e.py`）：批准后 items 带 verify 落地、
   `armed=True`、`TaskUpdated` 发出、回合 `COMPLETED`。
6. **替换语义**（按 dsh 拍板）：批准时用提交的 items 替换列表。真实跑通
   复现过的孤儿场景（预写 1 项 + 提交 2 项 = 任务里 3 项）在替换后变成
   恰好 2 项。`TaskState.clear()` 已删除，两个领域测试随主体删除。

## 7. 替换语义的边界（已按 dsh 拍板）

批准时**替换**列表，不追加。`TaskState.clear()` 已随 clear action 一并删除，
替换实现为 `ctx.task.items.clear()` + `add()`。

* 目标（goal/armed/计数器）保留：它属于会话/episode，不属于这份清单。
* 旧 step 回执不在此处理：`plan_runner._drop_foreign_receipts` 按
  **statement 内容**丢弃不属于新清单的回执（`:459`），所以 id 从 1 重排
  不会误匹配。
