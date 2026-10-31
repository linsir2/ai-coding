# 方案 B：把计划续跑改成结构信号（对齐 codex needs_follow_up）

> 状态：B-1（`7c82f81`）与 B-2（`6e95ef3`）均已落地，全量测试 + 真机脚本通过。
> 运行形态：一个 plan turn 内，工具调用轮次直接续跑；模型停下且仍有条目时
> 也不写消息、只继续；收口只有两个结构刹车（idle 3 / stall 6）。
> 前置：A 方案（单回合、无预算、验收下沉）已落地（commit `c6064cd`）。

## 0. 现状（B 之前的基线，A 方案落地后）

- `PlanExecutor` 进入时替换 `loop.policy`：`max_iterations=-1`、`stop_policy="plan"`。
- 模型无工具调用停下时，`policy.decide_after_step()` → `_plan_after_step()` 返回 NUDGE，
  `_run_turn` 把 `[plan-continue]` 作为 **synthetic user 消息**写进 transcript 再继续。
- 两个刹车在 loop 里：`idle_streak`（连续 3 次无工具停下）、`stall_rounds`（连续 6 轮无进度）。
- 关键事实：`TurnPolicy.inject_task()` 每一轮都把 `<task-state>`（含剩余条目）作为**易失尾块**
  挂到请求上，从不落盘（`core/turn_policy.py:692-701`）。也就是说，剩余清单**本来就在**每个请求里。

## 1. 目标

续跑由"本步是否产生工具调用"这类结构事实决定，不再由策略层注入自然语言；
计划语义不再通过替换全局 `loop.policy` 表达。

## 2. codex 的数据流（对照）

| 环节 | codex | 我们（B 之后） |
|---|---|---|
| 单步产出 | `SamplingRequestResult{needs_follow_up, last_agent_message}`（turn.rs:2077） | `StepResult` + `books` + `outcomes` |
| 续跑信号 | 本步任一次 tool call ⇒ `needs_follow_up=true`（stream_events_utils.rs:368）；或 `end_turn==Some(false)`（turn.rs:3371）；或 `has_pending_input`（turn.rs:624-636） | `bool(step.tool_calls) or 计划仍有可执行项 or 有用户插话` |
| 判定位置 | 工具全部 drain 之后、turn 结束前（turn.rs:3560 → :636） | `_run_turn` 中 `books.note_outcomes()` 之后（agent_loop.py:1401 附近） |
| 停下时 | stop hook 可阻止结束并注入（turn.rs:750-786），否则 break | 计划回合：结构续跑；非计划回合：策略闸门 |
| 预算 | 不作为续跑条件 | 同左：只有 idle/stall 两个刹车 |

## 3. 改动清单（文件:行号）

### B1. `core/agent_loop.py` —— 续跑变成结构判定

新增一个私有判定（放在 `_run_turn` 附近）：

```python
def _needs_follow_up(self, session, task, step, books) -> bool:
    if step.tool_calls:              # codex: any call means mid-work
        return True
    if getattr(session, "plan_executing", False) and task is not None:
        return any(i.status != "blocked" for i in task.open_items())
    return False
```

在 `agent_loop.py:1352` 之前插入：

```python
if self._needs_follow_up(session, task, step, books):
    books.note_idle_boundary()       # only when no tool call (see below)
    continue                          # no message appended
```

细则：
- 有工具调用的路径（:1401 之后）本来就是 `continue`，不改。
- 无工具调用且计划仍有项：`continue`，**不写任何消息**。
- 无工具调用且计划已清空：走现有 `decide_after_step`（它仍负责 acceptance / todo 闸门）。
- `idle_streak` 与 `stall_rounds` 的计数位置不变（`books.note_idle_boundary()` 在续跑分支里调）。

### B2. `core/turn_policy.py` —— 删掉 plan 分支

- 删 `PLAN_CONTINUE`、`PLAN_CONTINUE_LEAD/TAIL`、`_plan_continue_text()`、`_plan_after_step()`（:646-660）。
- 删 `decide_after_step` 里的 `if self.config.stop_policy == PLAN_CONTINUE:`（:615-621）。
- 删 `decide_next_iteration` 里的 plan-stall 分支（:431-442，改由 loop 在续跑判定处读 `stall_rounds`）。
- **必须同时改的一处**：`decide_next_iteration` 的 `allow_search`（:506-510）现在用
  `stop_policy == PLAN_CONTINUE` 关掉搜索梯子；`stop_policy` 取值删除后，计划回合会
  重新开启搜索梯子并被"search unconverged"中途切断。改为读 `snapshot.plan_executing`
  （新增 `TurnSnapshot.plan_executing: bool = False`，由 loop 在 `replace(snapshot, ...)`
  处填入 `session.plan_executing`，两个构造点：`agent_loop.py:1056` 与 `:1344`）。
- 保留 `decide_next_iteration` 的 `max_iterations >= 0` 检查与 `idle/stall` 字段。
- `TurnPolicyConfig.stop_policy` 的 `"plan"` 取值消失，只剩 natural/todo_aware/strict。

### B3. `core/turn_policy.py` —— 计划状态进易失尾块

新增 `plan_block(task) -> ChatMessage | None`（或并入 `inject_task`）：

```
<plan-state>计划执行中；仍有 N 项未完成：- 1. …；不要停下询问用户。</plan-state>
```

由 `RequestAssembler` 在 `session.plan_executing` 时挂到尾部（与 `<task-state>` 并列，L3，永不落盘）。
这样"继续"的信号是**每轮刷新的结构化尾块**，不是一次性注入的 user 消息：
- 不会被压缩吃掉（尾块每轮重建）；
- 不进入 transcript，replay 更干净；
- 与 codex 的 stop-hook 注入语义等价，但无需持久化。

### B4. `core/plan_executor.py` —— 不再替换 policy

- 删 `self._loop.policy = TurnPolicy(replace(...))` 与 `finally` 里的还原（:136-145、:203-206）。
- 只保留 `session.plan_executing = True/False`。
- "计划回合无迭代上限"改由 loop 读 `session.plan_executing` 决定：
  `ceiling = -1 if session.plan_executing else policy.config.max_iterations`。
- **必须同时改的一处**：`_has_a_round_left()`（`agent_loop.py:2468`）直接读
  `self.policy.config.max_iterations`；policy 不再被替换后，它会按普通回合的 10 次判断，
  在计划回合第 10 轮起静默关掉 `_reject_unsupported_claims` 的纠正机会。改为读同一个
  `_effective_max_iterations(session)` 辅助函数。
- 好处：消除全局可变状态（`GoalRunner` 也在 :244 替换 policy，两者若共用一个 loop 会互相覆盖）。

### B5. 刹车位置不变

`idle_streak >= 3` 与 `stall_rounds >= 6` 的判定从策略移到 loop 的续跑分支：

```python
if self._needs_follow_up(...):
    if plan and (books.idle_boundaries >= PLAN_IDLE_LIMIT
                 or books.stall_rounds >= PLAN_STALL_ROUNDS):
        break
    books.note_idle_boundary()  # no-tool case only
    continue
```

## 4. 改造后的数据流

```
while True:
    books.note_plan_progress(signature)        # 已有
    snapshot = books.snapshot(...)
    if policy.decide_next_iteration(snapshot) is FINISH:   # 只读上限/梯子
        break
    step = model_step()
    if step.tool_calls:
        outcomes = run_tools(); books.note_outcomes(outcomes)
        if halted: break
        continue                                # 结构续跑
    verify_acceptance()
    if self._needs_follow_up(session, task, step, books):   # 计划还有项
        if idle/stall 到限: break
        books.note_idle_boundary()
        continue                                # 不写消息
    decision = policy.decide_after_step(snapshot)           # 只处理 acceptance/todo
    ...
```

## 5. 风险与取舍

| 风险 | 说明 | 处置 |
|---|---|---|
| 请求字节完全相同 | 不注入文本时，第二轮请求与第一轮只差 assistant 消息；模型可能重复"我完成了" | 尾块每轮重建，且 idle 上限 3；必要时在尾块里带"N 项未完成"的计数变化 |
| 现有测试 | `tests/test_plan_executor.py` 断言 `stop_policies == ["plan"]`、`[plan-continue]` 文本 | 改为断言"一次 process_input + N 次模型步 + 无 synthetic 续跑消息" |
| `session.continuations` | 计划回合不再增长 | boot 指纹已改用 `tokens_used + plan_tokens_used`，不受影响 |
| 策略层职责收窄 | `decide_after_step` 不再管计划 | 计划语义集中到 loop + 尾块，两处而非三处 |
| 与 goal 模式的关系 | `GoalRunner` 仍是"一轮一 turn" | 不在 B 的范围；B 只改 plan |

## 7. 审查结论（逐条核对代码后）

| # | 检查项 | 结论 |
|---|---|---|
| 1 | 插入点（:1352 之前）是否绕过守卫 | 安全。必须在 `:1335`（`_reject_unsupported_claims`）之后插入；`halted`/`finish_research`/`conclusion` 都在工具调用路径（:1418-1466），无工具路径不会经过，不受影响。 |
| 2 | `allow_search`（:506-510） | 确认会受影响，必须改用 `snapshot.plan_executing`。`stop_policy=="strict"`（:409）与 `"natural"`（:637）与计划无关，保留。 |
| 3 | `_has_a_round_left`（:2468） | 确认读 `policy.config.max_iterations`；policy 不再替换后会按普通回合 10 次判断，必须改读 `_effective_max_iterations(session)`。`turn_policy.py:432/446` 在 `max_iterations=-1` 下已被 `>= 0` 守卫挡住，只需删 plan-stall 分支。 |
| 4 | `TurnSnapshot` 是否有 `plan_executing` | 没有（:232-282）。需新增字段，并在 `agent_loop.py:1056`、`:1344` 两个 `replace` 点填入；第三处构造点不存在。 |
| 5 | `<task-state>` 的注入位置 | 确认是易失尾块（`request_assembly.py:487-505`，`task` 槽位），每轮重建、不进 transcript、压缩碰不到。新增 `<plan-state>` 放在 `task` 旁边安全；尾块无总大小上限，只有各自字符预算。 |
| 6 | 方向 | 可以按此实施。更小的替代方案：**只做 B1 + B5**（loop 结构续跑 + 刹车留在 loop），保留 `stop_policy="plan"` 与 `<task-state>` 作为隐式信号，暂不引入 `<plan-state>`、不删 policy 替换——可拿到约 80% 收益，回归面最小。 |

### 推荐的分期

- **B-1（小）**：B1（结构续跑，不写消息）+ B5（刹车搬进 loop）+ 删 `_plan_after_step` 的文本注入。保留 `stop_policy="plan"` 仅作为"计划回合"标记。
- **B-2（结构）**：B3（`<plan-state>` 尾块）+ B4（不再替换 policy）+ 删 `stop_policy="plan"`，改读 `session.plan_executing`。

每一期都用真机脚本 + 全量测试验证后再进下一期。

## 8. 验收标准

1. 真机脚本（真实 AgentLoop + 脚本化模型）：3 项计划在 **1 个 turn** 内完成，
   模型步数 = 工具批次数 + 1，transcript 中 **没有** `[plan-continue]`。
2. 纯文本停下 + 有剩余项：请求里出现 `<plan-state>` 尾块，且 3 次后收口。
3. `todo_write(list)` 空转：6 轮后收口。
4. 非计划回合（普通 todo_aware）行为逐字节不变（`plan_state_block` 返回 None 时
   `volatile_block` 不产生消息）。
5. `pytest` / `ruff` / `mypy` 全绿。

## 9. 实施记录

| 阶段 | commit | 内容 | 验证 |
|---|---|---|---|
| B-1 | `7c82f81` | loop 结构续跑（不写消息）、两个刹车、删除策略层注入 | 4 个真机测试 + 全量 pytest/ruff/mypy |
| B-2 | `6e95ef3` | `TurnSnapshot.plan_executing`、不再替换 `loop.policy`、`<plan-state>` 尾块、删除 `idle_streak`/`PLAN_CONTINUE` 死代码 | 同上（新增 `<plan-state>` 断言） |

逐条对照（文档第 3 节的五步）：

| 步骤 | 落点 | 状态 |
|---|---|---|
| B1 结构续跑 | `agent_loop.py` 的 `plan_executing` 续跑块；无工具调用时 `continue` 不写消息 | 已实现（`7c82f81`） |
| B2 删 plan 策略分支 | `turn_policy.py` 删 `PLAN_CONTINUE` / `_plan_after_step` / `_plan_continue_text`；`allow_search` 改读 `snapshot.plan_executing` | 已实现（`6e95ef3`） |
| B3 `<plan-state>` 易失尾块 | `TurnPolicy.plan_state_block()` + `request_assembly.py` 的 `plan_state` 槽位（紧跟 `task`） | 已实现（`6e95ef3`） |
| B4 不替换 `loop.policy` | `plan_executor.py` 只设 `session.plan_executing`；`_has_a_round_left` 按 `plan_executing` 取无限上限 | 已实现（`6e95ef3`） |
| B5 刹车位置 | `PLAN_IDLE_LIMIT=3` / `PLAN_STALL_ROUNDS=6` 判定在 loop 续跑分支，`TurnPolicy` 只读 `stall_rounds` | 已实现（`7c82f81`） |

复验（HEAD `33591c1` 上重跑）：`pytest` 4821 passed / 29 skipped（bwrap 缺失导致跳过）、
`ruff` 通过、`mypy` 173 文件无告警、`realrun_plan.py` 三个场景全 PASS、
`realrun_spin.py` 在 6 轮后以 `plan stalled: 6 rounds without progress` 收口。

真机测试在 `tests/test_plan_continuation.py`：真实 `AgentLoop` + 脚本化模型，
覆盖"单回合完成并回合内修复""空闲刹车 3 次""记账工具空转 6 轮""无 verify 条目"。

### 已知残余风险

- 计划回合关闭搜索梯子（`allow_search=False`），一个"每轮读不同文件、从不标记完成"
  的模型不会被 ladder 拦住；只能靠 context 窗口与用户 Esc。重复读同一个文件会被
  `DuplicateCallGuard` 拒绝，因而会落进 `stall_rounds`。
- 结构续跑不写消息时，连续两轮请求只差 assistant 内容与 `<plan-state>`；模型仍可能
  重复"已完成"。这是 idle 上限 3 存在的原因。
