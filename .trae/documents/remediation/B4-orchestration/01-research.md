# B4 编排调研方案

范围：ORC-01 / ORC-02 / ORC-03 / ORC-04 / ORC-05 / MCP-10a。
性质：只读代码 + 方案，不改任何代码。
基线：`/workspace/ai-coding`，证据行号以当前工作树为准（任务给出的少数锚点已漂移，下文逐项注明）。
约定：路径均相对仓库根；`core/` 实际位于 `ai_coding/core/`。

**结论速览**

| ID | 结论 | 改动性质 | 风险 |
|---|---|---|---|
| ORC-01 | 保持不设上限，仅补注释/文档；补一条“无步数上限”的钉扎测试 | 零行为变更 | 低 |
| ORC-02 | 让 `strategy_l1` 被 `_due` 消费（cap 语义）；默认值 1→3 以保持现状 | 行为修复 + 默认值 | 中 |
| ORC-03 | `search_streak_threshold=0` 真正关闭 repeat/计数/陈旧档，保留 L3 硬停 | 行为修复 | 低 |
| ORC-04 | 最后一轮不再注入 POST nudge；转 FINISH + `note` | 行为修复 | 中 |
| ORC-05 | 不改枚举，仅在 reason 追加阶梯来源后缀 | 可观测性 | 低 |
| MCP-10a | 推荐保留配置，改按 MCP catalog 字符数判定 | 行为修复 | 低 |

---

## 1. ORC-01 计划实施回合无上限

### 现状（file:line）

- `ai_coding/core/turn_policy.py:251-266`：
  - `token_gate` 显式排除 `plan_implementing`（251-255），但 `mode == "plan"` 仍受 token 预算约束；
  - `ceiling = -1` 当 `snapshot.plan_implementing or snapshot.mode == "plan"`（262-266），因此 `iteration > ceiling` 永不成立。
- `ai_coding/core/agent_loop.py:1713-1720`：`plan_implementing` 时 `deadline = None`，连墙钟上限也没有。
- `ai_coding/core/agent_loop.py:2751-2763`：`_step_ceiling()` 对 `plan_implementing` / `mode=="plan"` 返回 `-1`。
- `ai_coding/core/plan_codex.py:95-105`：只有该路径把 `session.plan_implementing` 置 True，随后 `process_input(..., synthetic=True)`，finally 复位。
- 任务锚点 `agent_loop.py:1681-1684` 已漂移，当前是 `dispatcher.guards.reset()`；真正证据在 1713-1720 与 2751-2763。

**仍然存在的边界（重要，避免把“无上限”说过头）**

- 上下文窗口与压缩：`request_assembly.py:676-681` 的 `budget_pressure` 对 `plan_implementing` 返回空，但压缩仍会运行。
- PRE 阶梯仍可能 FINISH：`turn_policy.py:304-326`。`plan_implementing` 通常不是 `mode=="plan"`，所以 `allow_search` 为 True；strategy L4 / fatigue L3 / search L3 均可强制收尾。
- 模型自停：`turn_policy.py:361-365` 对 `plan_implementing` 直接 FINISH，不做结构续写。
- 任务轮次上限仍在：`core/task_driver.py:95,111-113`，`cap = profile.max_rounds()`。这里“无上限”只指单个 `process_input` 回合内的模型步数与墙钟，不是整个 plan 运行的轮数。

### 根因

C-2/C-3 的刻意设计：Codex 的 plan 执行回合由模型自停与结构续写约束，不由步数/时钟约束；加回步数上限会把长计划从中间切断。不是缺陷。

### 候选与取舍

| 候选 | 取舍 |
|---|---|
| A. 加 `max_iterations` | 违背 C-3 设计；长计划被腰斩；需要为每个 plan item 设计续跑，超出 P2 |
| B. 只把现有隐式契约写成注释 + 钉扎测试（推荐） | 零行为变更；后来者不会误以为漏了上限；测试锁住“不可悄悄加回” |
| C. 只写文档、不加测试 | 成本最低，但注释会被下一次重构当噪音删掉 |

### 推荐方案（B，零实现代码）

只在三处补注释（英文，与文件风格一致），不改任何表达式：

1. `ai_coding/core/turn_policy.py:256-266` 的 `ceiling` 计算前：
```python
# ORC-01 (decided, not a defect): the plan-implementation turn and the
# planning turn have no step ceiling by design. They are bounded by the
# context window/compaction, the PRE hard-stop ladders and the model
# stopping -- not by max_iterations. Do not "restore" a cap here without a
# per-item resume design.
```
2. `ai_coding/core/agent_loop.py:1713-1720` 的 `deadline = None` 分支：
```python
# ORC-01: no wall clock for plan execution either. A per-turn deadline is
# exactly the mid-item interruption this executor exists to remove.
```
3. `ai_coding/core/agent_loop.py:2751-2763` 的 `_step_ceiling` docstring 已说明，补一句“该返回值同时被 `_has_a_round_left` 消费，`-1` 表示永远还有下一轮”。

### 测试计划

- `tests/test_turn_policy.py` 新增 `test_plan_implementing_has_no_step_ceiling`：
  - `TurnPolicy(TurnPolicyConfig(max_iterations=3)).decide_next_iteration(TurnSnapshot(iteration=10_000, plan_implementing=True))` → `TurnAction.ITERATE`；
  - 同一 snapshot 非 `plan_implementing` → FINISH，证明测试不是恒真。
- 不新增“无时钟”测试（deadline 由 `turn_timeout_seconds` 驱动，属既有覆盖）。

### 风险

- 注释可能与未来 C-3 调整冲突；风险低，测试只钉“无步数上限”这一条契约。
- 不要把 `mode=="plan"` 与 `plan_implementing` 混为一谈：前者仍有 token 预算（`turn_policy.py:251-255`），后者连 token 预算也没有。

---

## 2. ORC-02 `max_strategy_nudges` 无效

### 现状（file:line）

- 定义：`core/turn_policy.py:72` `max_strategy_nudges: int = 1`；读取：`:106`；传入：`:225` `strategy_l1=self.config.max_strategy_nudges`。
- `core/interventions.py:99` `strategy_l1: int = 1`，但全仓没有任何读取点。`_due` 的 strategy 分支（`interventions.py:387-431`）只按账本 `level` 递增：
  - `entry is None` → L1 nudge（400-401）；
  - `pending` → None（404-413）；
  - `failed` → `level >= L3` 则 L4 finish，否则 `level+1` nudge（414-416）；
  - `unfinished` → 同级重试（420-421）；
  - `helped/exhausted` → L1（431）。
- 因此 `max_strategy_nudges` 无论设 0/1/2，行为都固定为 L1→L2→L3→L4。`TurnSnapshot.strategy_nudges`（`turn_policy.py:169`）由 `turn_books.py:777` 填充，但决策侧从未读取。

### 根因

R6b 原始设计按“本回合已注入次数 `< max`”放行（`docs/r6-analysis.md:315`），13 §2.1 改成账本阶梯后只搬了 `strategy_l1` 这个字段名，没把预算逻辑搬进 `_due`。字段名 `strategy_l1` 也误导：它实际应表示“允许的 nudge 级数上限”。

### 候选与取舍

| 候选 | 取舍 |
|---|---|
| A. 删掉该配置 | 少一个死旋钮，但运维确实需要一个“别一直唠叨”的开关；且要同步文档，收益低 |
| B. 用 `TurnSnapshot.strategy_nudges` 做计数上限 | 语义最贴“每回合次数”，但账本跨回合、计数按回合重置，`unfinished` 重试会对不上；且 `_due` 还需额外判断何时 FINISH |
| C. `_due` 按 `level <= strategy_l1` 放行（推荐） | 与任务方向一致；纯函数、可表测；`strategy_l1=N` 恰好表示允许 L1..LN；超出即 FINISH |
| D. 只改默认值不改逻辑 | 不算修复 |

### 推荐方案（C，含签名）

新增纯函数，`_due` 的 strategy 分支直接返回它：

```python
# ai_coding/core/interventions.py
def _strategy_due(
    entry: Mapping[str, Any] | None,
    snap: TurnSnapshot,
    limits: LadderLimits,
) -> tuple[int, str, str] | None:
    """(level, kind, trigger) constrained by the per-turn nudge budget."""
    cap = int(limits.strategy_l1 or 0)
    if cap <= 0:                       # 0 = the whole strategy gate is off
        return None
    if entry is None:
        return (L1, "nudge", "")
    outcome = str(entry.get("outcome", ""))
    level = int(entry.get("level", 0) or 0)
    if outcome == "pending":
        return None                    # observing window
    if outcome == "failed":
        if level >= cap or level >= L3:
            return (L4, "finish", "")
        return (level + 1, "nudge", "")
    if outcome == "unfinished":
        return (min(max(level, L1), cap), "nudge", "")
    return (L1, "nudge", "")           # helped / exhausted / unknown
```

在 `_due`（`interventions.py:398-401`）里：
```python
if gate == STRATEGY:
    return _strategy_due(entry, snap, limits)
```

**默认值：建议 1 → 3（保持现状，这是本项唯一需要确认的偏离）**

- `core/turn_policy.py:72`、`core/turn_policy.py:106`、`core/interventions.py:99`、`config/models.py:562` 四处同步为 3。
- 理由：当前真实行为就是 L1→L2→L3→L4。若默认保持 1，默认用户会从“三级提示 + 武装”骤降为“一次提示后直接收尾”，并连带让 `interventions.armed()`（`interventions.py:500-507`）对 strategy 永不返回 True，进而改变 `agent_loop.py:2564` 的 `strict_repeats`。现有 `tests/test_loop_escalation.py:107-119` 明确断言默认配置下出现 L1→L2 两条 gate，保持 1 会使它失败。
- `0` = 整个 strategy gate 关闭（不提示、不提前收尾）。注意这与“只是不唠叨、仍保留滞止收尾”不同；若运维只想关提示，仍可用 `failed_streak_threshold` / `stall_iterations` 的现有语义做更细的控制。此点在实现注释中写明。
- `2` = 允许 L1、L2；L2 窗口失败后直接 L4 finish，符合任务方向。

### 测试计划

- `tests/test_interventions.py`
  - `test_strategy_budget_zero_disables_the_gate`：`LadderLimits(strategy_l1=0)` + `failed_streak=9` → `select(...) is None`。
  - `test_strategy_budget_two_allows_l1_and_l2_then_finishes`：
    - `entry=None` → `(strategy, 1, nudge)`；
    - `level=1,outcome=failed` → `(strategy, 2, nudge)`；
    - `level=2,outcome=failed` → `(strategy, 4, finish)`。
  - `test_strategy_budget_three_matches_the_existing_ladder`：裸 `LadderLimits()` 仍走 L1→L2→L3→L4，防止默认值回退。
- `tests/test_turn_policy_escalation.py`
  - `test_max_strategy_nudges_two_caps_the_ladder`：`_policy(max_strategy_nudges=2)`，断言第二次失败窗口后 FINISH 而不是 L3。
  - 现有 `test_from_ai_config_reads_the_new_keys` 保持（它显式传 2）。
- `tests/test_loop_escalation.py` 不改；默认 3 时现有断言全部成立。
- `tests/test_interventions_loop.py:104-128` 不改；默认 3 仍会到 L2。

### 风险

- **默认值 1→3 与 `config/models.py:558-562` 的注释（“buys one strategy nudge”）及 `docs/features.md:853` 的“至多 1 次”冲突**；`docs/` 不在 B4 白名单，需另开文档批次修正。
- `max_strategy_nudges=0` 关闭整条 strategy gate，可能让“连续全失败”的回合退回迭代预算收尾；这是“关闭”的应有语义，但要在配置注释里写清。
- `strategy_nudges` 快照字段仍只用于审计，不参与决策；不在本项顺手删除，避免扩大契约。

---

## 3. ORC-03 `search_streak_threshold=0` 未真正关闭

### 现状（file:line）

- `core/interventions.py:164-192` `_triggered(SEARCH)` 的顺序：
  1. `repeat_streak >= 1` → True（167-168）；
  2. plan/readonly → False（172-173）；
  3. `search_streak_l3` → True（179-180）；
  4. `if not search_streak_threshold: return False`（181-182）；
  5. 计数档（188-189）、stale L1（192）。
- `core/interventions.py:343-351` `_search_due` 的顺序：L3 count（334-336）→ stale L3（337-342）→ `repeat_streak >= 1`（343-348）→ `threshold = ...; if not threshold: return None`（349-351）。
- 因此 `search_streak_threshold=0` 时，`repeat_streak>=1` 仍会触发 `repeat_output` nudge；`LadderLimits` 文档（`interventions.py:102-105`）和 `AIConfig` 注释（`config/models.py:380-383`）都声称 0 关闭。
- 既有测试只覆盖了 `search_streak=99`（`tests/test_interventions.py:301-303`），没有覆盖 `repeat_streak`，所以缺陷未被抓住。

### 根因

`repeat_output` 是后加的旁路信号，插在了阈值开关之前；`_search_due` 同样把 repeat 分支放在 threshold guard 之前。硬停 L3 被有意放在开关之前（`tests/test_interventions.py:597-610` 钉扎“L3 独立于计数开关”），这是正确设计，不能一并移到开关之后。

### 候选与取舍

| 候选 | 取舍 |
|---|---|
| A. 把 `if not threshold: return False` 放到 `_triggered` 最顶部 | 会同时关掉 L3 硬停，破坏 `test_the_hard_stop_is_independent_of_the_count_switch` |
| B. 在 L3/stale L3 之后、repeat 之前插入开关（推荐） | 既让 0 真正关闭 repeat/计数/stale 建议档，又保留显式 L3 硬停 |
| C. 只改 `_search_due` 不改 `_triggered` | `select` 仍会在 `_triggered` 被判 True，然后 `_due` 返回 None，不会注入；行为上可行，但两处顺序仍不一致，易回退 |

### 推荐方案（B，`_triggered` 与 `_search_due` 同步）

`_triggered(SEARCH)` 调整为：
```python
# 1) posture is supposed to be look-only
if snap.mode == "plan" or snap.readonly:
    return False
# 2) the explicit hard stop keeps its own switch
if limits.search_streak_l3 and snap.search_streak >= limits.search_streak_l3:
    return True
# 3) 0 disables this gate's advice rungs (count / repeat / stale L1)
if not limits.search_streak_threshold:
    return False
# 4) repeated identical output is no-progress
if snap.repeat_streak >= 1:
    return True
# 5) count rung only when the streak produced no new evidence
if snap.search_streak >= limits.search_streak_threshold and not snap.novel_findings:
    return True
return bool(limits.stale_l1 and snap.stale_streak >= limits.stale_l1)
```

`_search_due` 在 stale L3 之后插入同一 guard，再进入 `repeat_streak`：
```python
if not limits.search_streak_threshold:
    return None
if snap.repeat_streak >= 1:
    ...
```

**副作用（有意）**：repeat-output 在 plan/readonly 下不再触发，因为它现在排在 posture 判断之后。这与 `interventions.py:169-173` 的既有理由一致（这两种姿态本来就该只读）；补一条测试锁定。

### 测试计划

- `tests/test_interventions.py`
  - `test_search_threshold_zero_disables_the_repeat_rung`：`LadderLimits(search_streak_threshold=0)` + `repeat_streak=5` → `select(...) is None`。
  - `test_search_threshold_zero_keeps_the_hard_stop`：复用现有 597-610 的用例，确认仍返回 `(search, 3, finish)`。
  - `test_search_repeat_is_off_in_plan_and_read_only_postures`：`repeat_streak=5` + `mode="plan"` / `readonly=True` → None。
- `tests/test_interventions_loop.py` 不改（现有用例 threshold 为默认 4）。
- 回归命令：`python -m pytest tests/test_interventions.py tests/test_interventions_loop.py -q`。

### 风险

- 若有用户依赖“threshold=0 但 repeat 仍提醒”，现在会静默；这是修正文档承诺，需在变更说明里点名。
- `search_streak_l3` 仍独立生效；`config.example.yaml:57` 默认 16，意味着用户设 threshold=0 仍可能被 16 次只读硬停。这是设计，不是本项缺陷，但要在配置注释里写清“0 只关建议档，L3 有自己的开关”。

---

## 4. ORC-04 最后一轮仍注入 POST nudge

### 现状（file:line）

- `core/agent_loop.py:1418-1437`：
  - `decision = self.policy.decide_after_step(snapshot)`（1418）；
  - `self._note_intervention(...)`（1419）；
  - FINISH 才收尾（1420-1430）；
  - 否则 `books.note_nudge()` + `session.add_message(... decision.inject ...)` + checkpoint + `continue`（1431-1437）。
- `core/agent_loop.py:2765-2775` 已有 `_has_a_round_left(session, index)`：`ceiling < 0` 为 True，否则 `index < max(1, ceiling)`。
- POST 阶段唯一的 NUDGE 来源是 TASK gate（`turn_policy.py:368-372`，`interventions.select(..., POST)` 只遍历 `(TASK,)`，见 `interventions.py:451`）。
- 最后一轮触发时，注入的消息写过会话，但循环回到顶部 `decide_next_iteration` 会因 `iteration > ceiling` 直接 FINISH（`turn_policy.py:267-295`）；模型永远不会读到这条 nudge，且 outcome 的 note 为空。

### 根因

POST 分支没有复用已有的 `_has_a_round_left`，只看决策类型不看预算余量。`_has_a_round_left` 之前只用于 citation retry（`agent_loop.py:1391`），没有覆盖 ladder 注入。

### 候选与取舍

| 候选 | 取舍 |
|---|---|
| A. 无余量时直接 `continue`，不注入，让下一轮 PRE 正常 FINISH | 状态天然是 MAX_ITERATIONS，但 `decision.note` 无处安放；还需新增局部变量把 note 带到 outcome |
| B. 无余量时把 NUDGE 转成 FINISH，`note=inject`，用一个局部布尔保留状态为 MAX_ITERATIONS（推荐） | 复用现有 FINISH 分支；note 走 handoff/digest；不写无用消息；状态语义正确 |
| C. 仍注入，寄希望下一轮读到 | 原缺陷 |

### 推荐方案（B，含签名/插入点）

在 `decision = self.policy.decide_after_step(snapshot)` 之后、`self._note_intervention(...)` 之前插入（保证账本按“已收尾”记录，而不是记一条永远不会被读的 pending）：

```python
# ORC-04: a POST nudge written on the last allowed iteration is never read.
# Convert it into the turn's terminal note instead of spending a message.
budget_closed_nudge = False
if decision.action is TurnAction.NUDGE and not self._has_a_round_left(session, index):
    budget_closed_nudge = True
    decision = replace(
        decision,
        action=TurnAction.FINISH,
        note=decision.inject or decision.note,
    )
```

FINISH 分支（`agent_loop.py:1420-1430`）的状态改为：
```python
status=(
    TurnStatus.MAX_ITERATIONS if budget_closed_nudge else TurnStatus.COMPLETED
),
```
其余 `text=step.text`、`reason=decision.reason`、`note=decision.note` 不变。这样：
- 不调用 `session.add_message`，不 `books.note_nudge()`；
- `decision.note` 携带 `[task-gate]` 文本，进入 `TurnOutcome.note`，最终由 `render_handoff` 展示（`turn_close.py:284-295`）；
- 账本由 `interventions.record` 记为 `exhausted`（`interventions.py:481-496`），下一回合从 L1 重新开始，不会继承一个“待观察”的假窗口。

### 测试计划

- `tests/test_interventions_loop.py` 新增 `test_the_last_round_does_not_inject_a_post_nudge_it_cannot_read`：
  - `max_tool_iterations=1`，`session.task` 预置一个 pending item；
  - 脚本引擎只返回 `_text("先停一下")`；
  - 断言：
    1. `loop.last_outcome.status is TurnStatus.MAX_ITERATIONS`；
    2. `loop.last_outcome.note` 含 `[task-gate]` 和未完成项文本；
    3. 会话中不存在该轮新增的 `[task-gate]` synthetic 消息（用消息数前后对比或内容断言）；
    4. `session.intervention_state["task"]["outcome"] == "exhausted"`（若该字段存在）。
- 反向用例：`max_tool_iterations=4` 时同一脚本仍注入一次，证明 guard 只作用于最后一轮（可复用 `tests/test_agent_loop_react.py:431-454` 的形状，不修改其既有断言）。
- 回归：`python -m pytest tests/test_agent_loop_react.py tests/test_interventions_loop.py -q`。

### 风险

- 状态从 MAX_ITERATIONS 改为 COMPLETED 的分支必须只对 `budget_closed_nudge` 生效，否则会改变普通 `todo ladder exhausted` 的 COMPLETED 语义并击中 golden（`tests/golden/loop_events/todo_nudge.json:115,126`）。
- `_note_intervention` 必须放在转换之后；否则账本记成 pending，下一回合会被“窗口未结算”抑制一次。
- plan / readonly / plan_implementing 在 `decide_after_step` 已提前 FINISH，不受影响。

---

## 5. ORC-05 阶梯 FINISH 统一记 MAX_ITERATIONS

### 现状（file:line）

- `core/turn_close.py:64-69`：
```python
if budget_expired:
    status = TurnStatus.TIMEOUT
elif decision.reason == "turn token budget exhausted":
    status = TurnStatus.BUDGET
else:
    status = TurnStatus.MAX_ITERATIONS
```
- 阶梯决策携带来源：`core/turn_policy.py:130-142` 的 `TurnDecision.intervention / level / note`；`_as_turn_decision`（374-389）与 `_finish_at`（329-352）都会保留。
- 但 `terminal_outcome` 只写 `reason=decision.reason`（72-80），完全丢掉 `intervention/level`。结果是 strategy 的 `"no progress: ..."` 在 UI/摘要里与“步数用完”无法区分。

### 根因

URN 终态映射只按 `reason` 的两个特例分流，阶梯来源没有进入 `TurnOutcome.reason`；`TurnStatus` 是外部契约（`domain/loop.py:16-24`），不能为每个阶梯加枚举。

### 候选与取舍

| 候选 | 取舍 |
|---|---|
| A. 为每个阶梯加 `TurnStatus` | 破坏外部契约（UI/`/status`/subagent 映射），任务明确禁止 |
| B. 给所有阶梯 reason **加前缀** | 会击穿 `domain/turn_digest.py:355-358` 的 `startswith("no progress")` / `startswith("search unconverged")`，标签退化为“回合步数上限” |
| C. 给非自描述阶梯 reason **追加后缀**（推荐） | 保留 startswith 语义；显示层可直接读到来源；无需改枚举 |
| D. 仅改 note | note 是“留下什么”而不是“为什么结束”，语义错位 |

### 推荐方案（C，strategy + search；task/fatigue 保持不动）

```python
# ai_coding/core/turn_close.py
#: reasons that do not already name their ladder in words
_LADDER_SOURCE_GATES = ("strategy", "search")


def terminal_outcome(decision, *, budget_expired, last_text, iterations):
    ...
    reason = decision.reason
    gate = str(decision.intervention or "")
    if gate in _LADDER_SOURCE_GATES:
        level = int(decision.level or 0)
        source = f"{gate} ladder L{level}" if level else f"{gate} ladder"
        reason = f"{reason} [{source}]" if reason else source
    return TurnOutcome(
        status=status,
        text=last_text,
        iterations=iterations,
        reason=reason,
        note=decision.note,
    )
```

- strategy：`"no progress: no tool call has succeeded in 3 iterations [strategy ladder L4]"`。
- search：`"search unconverged [search ladder L3]"`。
- task / fatigue 不动：`"todo ladder exhausted"`、`"fatigue ladder exhausted"` 已经点名阶梯；且 task 的 reason 被 `tests/golden/loop_events/todo_nudge.json:119,130` 钉死，而 golden 不在 B4 白名单。
- 后缀而非前缀，是为了让 `domain/turn_digest.py:355-358` 的标签逻辑继续命中。这是本项最重要的实现约束。

### 测试计划

- `tests/test_turn_close.py`
  - `test_terminal_outcome_names_the_strategy_ladder`：`TurnDecision(FINISH, "no progress: ...", intervention="strategy", level=4)` → `status is MAX_ITERATIONS`，`reason == "no progress: ... [strategy ladder L4]"`。
  - `test_terminal_outcome_names_the_search_ladder`：`intervention="search", level=3` → `reason == "search unconverged [search ladder L3]"`，且 `reason.startswith("search unconverged")`。
  - `test_terminal_outcome_leaves_the_todo_reason_alone`：`intervention="task", level=2` → `reason == "todo ladder exhausted"`（保护 golden）。
- `tests/test_interventions_loop.py`：把 4 处 `loop.last_outcome.reason == "search unconverged"`（407、455、549、584）改为**精确新值** `"search unconverged [search ladder L3]"`；不改成 `in`，避免弱化断言。`SearchStalled.reason == "search unconverged"`（583）**不改**：事件用的是原始 `decision.reason`（`agent_loop.py:2716`），不是 outcome。
- `tests/test_loop_escalation.py:130` 的 `"no progress" in ...` 继续通过（后缀不影响）。
- 回归：`python -m pytest tests/test_turn_close.py tests/test_interventions_loop.py tests/test_loop_escalation.py tests/test_loop_refactor_equivalence.py -q`。

### 风险

- 若后续把 task/fatigue 也纳入，必须同步 `tests/golden/loop_events/todo_nudge.json`；该文件不在白名单，需父任务批准。
- `render_handoff` 对 reason 截断到 160 字符（`turn_digest.py:368-369`）；当前两类 reason 都远小于上限，后缀不会被截掉。
- `subagent.py:2395-2396` 会把 reason 拼进退出原因，UI 也会显示后缀；这是期望的可观测性变化。

---

## 6. MCP-10a `mcp_chars_warn` 永不触发

### 现状（file:line）

- `core/mcp.py:8-13`：M2 之后，MCP 不再“一工具一 schema”，而是 **一个 `mcp_call` 调度器 + 每步重建的 `<mcp-catalog>` 文本**。
- `tools/mcp_call.py:47-52`：`name="mcp_call"`，`origin="mcp"`；其 schema 只有 `server/tool/input` 三个字段（26-34），约 500 字符级。
- `server/world.py:293`：生产只注册 `MCPCallTool` 这一个 MCP 工具。
- `core/agent_loop.py:247-259`：`_tools_by_origin` 按 `short_json(tool_schema(tool))` 累加，**只统计 schema，不统计 catalog**。
- `core/agent_loop.py:1857-1860`：`mcp_chars = sum(chars for origin, chars in self._tools_by_origin.items() if origin != "local")`。
- `config/models.py:481`：`mcp_chars_warn: int = 2_000`。
- 结论：生产环境的 `mcp_chars` 实际只有 `mcp_call` 的 schema（远小于 2000），catalog（默认预算 8000，`core/mcp.py:135`）完全没有被计数，所以阈值永不触发。
- 现有测试为何是绿的：`tests/test_tool_surface_warning.py:118-130` 用一个 `origin="mcp"`、description 3000 字符的假工具；那是 M2 之前的形状，生产已不存在。
- 任务锚点 `agent_loop.py:1824-1842` 已漂移；当前 `_warn_tools_chars_once` 定义在 1833，MCP 判定在 1857-1878。

### 根因

M2 把 MCP 成本从“schema”迁移到“catalog”，但告警仍按 schema 统计；测试用假 origin 工具模拟，掩盖了迁移后的真空。

### 候选与取舍

| 候选 | 取舍 |
|---|---|
| A. 保留 `mcp_chars_warn`，改为 `catalog 字符数 + MCP origin schema`（推荐） | 统计的正是每步真实发送的 MCP 成本；旧测试（假 origin）仍可通过；运维开关保留 |
| B. 删除 `mcp_chars_warn` 并同步 `config.example.yaml` | 少一个坏旋钮；但 catalog 预算 8000 本身很大，运维失去“提前告警”的能力；且要动 `AIConfig` 契约 |
| C. 只把阈值默认值调低 | 治标；仍然测不到 catalog |

### 推荐方案（A，含签名/插入点）

在 `core/agent_loop.py:1857-1860` 附近改为“origin schema + catalog”：

```python
mcp_threshold = int(getattr(self.settings, "mcp_chars_warn", 0) or 0)
total_threshold = int(getattr(self.settings, "tools_chars_warn", 0) or 0)
origin_mcp_chars = sum(
    chars for origin, chars in self._tools_by_origin.items() if origin != "local"
)
catalog_chars = 0
if self.catalog_builder is not None and (
    (mcp_threshold > 0 and not self._tools_warned)
    or (total_threshold > 0 and not self._total_tools_warned)
):
    catalog_chars = len(self.catalog_builder() or "")
mcp_chars = origin_mcp_chars + catalog_chars
```

- `mcp_chars` 是“MCP 每步真实成本”的一个上界：`mcp_call` schema + volatile catalog。
- `tools_chars_warn` 的总量 `self._tools_chars` 保持“schema only”（`agent_loop.py:1854`），不把 catalog 算进去；catalog 是尾部块，不是 schema，混在一起会让既有 attribution 语义变化。
- 只在“尚未告警且对应阈值 > 0”时调用 `catalog_builder`，避免告警已发后每轮多建一次字符串。
- 配置保留；在 `config.example.yaml` 的 `ai:` 段补两行注释（当前文件完全没有这两个键）：
```yaml
  # MCP 10: warn once per loop when the MCP surface (mcp_call schema + the
  # per-step <mcp-catalog> block) exceeds this many chars. 0 disables.
  mcp_chars_warn: 2000
  # R2-8: the whole tool-schema surface, built-ins included. 0 disables.
  tools_chars_warn: 0
```

### 测试计划

- `tests/test_tool_surface_warning.py`
  - 修改 `_loop` 帮助函数：新增 `catalog_builder: Any = None` 形参并传给 `AgentLoop(..., catalog_builder=catalog_builder)`；其余 `**overrides` 仍交给 `AIConfig`。
  - 新增 `test_a_large_mcp_catalog_warns_even_when_the_dispatcher_schema_is_small`：`catalog_builder=lambda: "x" * 3_000`，`mcp_chars_warn=2_000`，无 MCP origin 工具 → 发一次 `ToolSurfaceWarning`，`mcp_chars >= 3_000`。修复前该用例失败（`mcp_chars == 0`）。
  - 新增 `test_the_catalog_and_the_dispatcher_schema_are_both_counted`：一个 `origin="mcp"` 的小 schema 工具 + catalog 2500 → `mcp_chars > 2500`。
  - 新增 `test_a_small_catalog_stays_quiet`：catalog `""` + 无 MCP origin → 不告警。
  - 既有 `test_an_mcp_surface_over_its_own_ceiling_warns`（118-130）保持通过（加性统计）。
- `scripts/r7e_cases.py` 的离线用例不需要改（假 origin 仍超阈值）。
- 回归：`python -m pytest tests/test_tool_surface_warning.py -q`。

### 风险

- `catalog_builder` 会在 `_begin_turn` 多跑一次（直到告警发过）；它读 manager 内存状态、无 IO，成本可忽略，但要避免在阈值 0 时调用。
- 若 catalog 被预算截断（`build_mcp_catalog` 返回 ≤8000），告警反映的是“实际发送量”，不是“配置里声明了多少工具”；这是正确的口径。
- **跨批次重复**：`B1-mcp/01-research.md:742-758` 已提出同一修复，并指出 `agent_loop.py` 与 `tests/test_tool_surface_warning.py` 不在 B1 白名单。B4 白名单包含这两处，建议 MCP-10a 只在 B4 落地，B1 删除该条或标注“由 B4 承接”。

---

## 7. 与方向冲突的发现与待确认

1. **ORC-02 默认值**：方向只规定 `0` 关闭、`2` 允许两级，未规定默认值。若保持 `AIConfig.max_strategy_nudges=1`，默认行为会从当前 L1→L2→L3 缩为 L1，并关闭 strategy 的 L2 `armed()`；`tests/test_loop_escalation.py:107-119` 会失败。**推荐默认改为 3（=现状）**。这是唯一需要产品确认的偏离。
2. **ORC-05 不能给所有阶梯加前缀**：`domain/turn_digest.py:355-358` 用 `startswith`，前缀会让“零进展/检索未收敛”标签退化为“回合步数上限”。必须用后缀。
3. **ORC-05 不能改 task 的 reason**：`tests/golden/loop_events/todo_nudge.json:119,130` 钉死 `"todo ladder exhausted"`，而 `tests/golden/` 不在白名单。因此推荐只改 strategy + search；若要求“所有阶梯统一”，需扩白名单并重录 golden。
4. **ORC-03 的 `return False`/`return None` 笔误**：方向写“`if not threshold: return False`”，`_search_due` 实际返回 `None`（`interventions.py:351`）。实现时按各自函数返回类型写，不强行统一。
5. **ORC-01 的“无上限”不是绝对**：仍受上下文窗口、PRE 三类阶梯、模型自停、以及 `TaskDriver` 的 `max_rounds()` 约束（`task_driver.py:95,111-113`）。注释要写“无步数/墙钟上限”，不要写“无任何上限”。
6. **MCP-10a 与 B1 重复**：见上节风险第 3 条，需明确唯一归属。
7. **配置文档滞后**：`docs/features.md:853`、`docs/how-to-run.md:168`、`docs/data-contracts.md:576` 仍写 `max_strategy_nudges=1`；`docs/` 不在 B4 白名单，需另批修正。

---

## 8. 批次内顺序

按“改动面最小 → 冲突最少 → 测试先绿”排序：

1. **ORC-03**：只动 `interventions.py` 的两处顺序 + 表测。语义最纯，先落地可避免后续 ORC-02 改 `_due` 时混淆。
2. **ORC-02**：同一文件的 `_due` + `turn_policy.py` / `config/models.py` 默认值 + 表测。与 ORC-03 同文件，连续做完减少冲突。
3. **ORC-05**：`turn_close.py` + 更新 `test_interventions_loop.py` 的 4 处 search reason。先固定 reason 契约，后续 ORC-04 的新用例可直接断言最终字符串。
4. **ORC-04**：`agent_loop.py` POST 分支 + 新 loop 测试。依赖 ORC-05 的 reason 契约。
5. **MCP-10a**：`agent_loop.py` 告警段 + `config.example.yaml` + `test_tool_surface_warning.py`。与前四项无逻辑依赖，放在 ORC-04 之后避免同一文件并行编辑。
6. **ORC-01**：最后补注释 + 一条钉扎测试。零行为变更，放最后可避免早先的注释在后续重构中被覆盖。

每步之间的最小回归：
```bash
python -m pytest tests/test_interventions.py tests/test_turn_policy_escalation.py -q
python -m pytest tests/test_turn_close.py tests/test_interventions_loop.py -q
python -m pytest tests/test_agent_loop_react.py tests/test_loop_escalation.py tests/test_loop_refactor_equivalence.py -q
python -m pytest tests/test_tool_surface_warning.py -q
```

---

## 9. 明确不做

- **不改 `TurnStatus` 枚举**（`domain/loop.py:16-24`），ORC-05 只改 reason。
- **不改默认模型窗口**：`max_context_tokens` / `context_window` / `model` 配置一律不动。
- **不新增依赖、不联网、不 commit / push**。
- **不弱化断言**：ORC-05 更新 search reason 时写精确新值，不把 `==` 改成 `in`。
- **不删 `strategy_nudges` 快照字段**；它仍作为审计数据保留。
- **不改 `search_streak_l3` 语义**：threshold=0 只关建议档，L3 仍独立。
- **不改 task / fatigue 的 reason 字符串**（golden 与既有契约）。
- **不修改 `tests/golden/`**；因此 ORC-05 不覆盖 task。
- **不改 `docs/`**（`features.md` / `how-to-run.md` / `data-contracts.md` / `r6-*`），默认值文档滞后作为后续文档批次处理。
- **不改 `plan_codex.py` / `request_assembly.py` / `tools/mcp_call.py` / `tools/base.py` / `server/world.py`**；它们只作为证据引用。
- **不改 `core/task_driver.py`**；ORC-01 只引用其轮次上限作为边界说明。

---

## 10. 文件清单

**实现改动**

| 文件 | 项 | 改动 |
|---|---|---|
| `ai_coding/core/interventions.py` | ORC-02、ORC-03 | 新增 `_strategy_due`；`_due` 调用它；`_triggered(SEARCH)` 与 `_search_due` 调整 guard 顺序；`LadderLimits.strategy_l1` 默认 1→3 |
| `ai_coding/core/turn_policy.py` | ORC-02、ORC-01 | `TurnPolicyConfig.max_strategy_nudges` 默认 1→3、`from_ai_config` fallback 同步；`ceiling` 前补 ORC-01 注释 |
| `ai_coding/core/agent_loop.py` | ORC-04、ORC-01、MCP-10a | POST 分支最后一轮转 FINISH+note；`deadline=None` 与 `_step_ceiling` 补注释；`_warn_tools_chars_once` 加 catalog 字符 |
| `ai_coding/core/turn_close.py` | ORC-05 | `terminal_outcome` 为 strategy/search 追加 `[<gate> ladder L<n>]` 后缀 |
| `ai_coding/config/models.py` | ORC-02 | `max_strategy_nudges` 默认 1→3；注释同步 |
| `config.example.yaml` | MCP-10a | 增加 `mcp_chars_warn` / `tools_chars_warn` 注释（可选但推荐） |

**测试改动**

| 文件 | 项 | 改动 |
|---|---|---|
| `tests/test_interventions.py` | ORC-02、ORC-03 | 新增 strategy cap 0/2/3 用例；新增 repeat + threshold=0、plan/readonly 用例 |
| `tests/test_turn_policy_escalation.py` | ORC-02 | 新增 `max_strategy_nudges=2` 的 FINISH 断言 |
| `tests/test_turn_close.py` | ORC-05 | 新增 strategy/search/task 三类 terminal_outcome 用例 |
| `tests/test_interventions_loop.py` | ORC-04、ORC-05 | 4 处 search outcome reason 改精确新值；新增最后一轮 POST nudge 用例 |
| `tests/test_tool_surface_warning.py` | MCP-10a | `_loop` 支持 `catalog_builder`；新增 catalog 告警/静默用例 |
| `tests/test_turn_policy.py` | ORC-01 | 新增 `plan_implementing` 无步数上限的钉扎测试 |

**只读、不改**

`ai_coding/core/task_driver.py`、`ai_coding/core/plan_codex.py`、`ai_coding/core/request_assembly.py`、`ai_coding/tools/mcp_call.py`、`ai_coding/tools/base.py`、`ai_coding/server/world.py`、`ai_coding/domain/turn_digest.py`、`tests/golden/loop_events/todo_nudge.json`、`docs/`。

**不创建**：除本文件外不新增任何文档或脚本。
