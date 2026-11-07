# B4（编排 / 计划）修复实现

- 批次：B4 编排 / 计划（W1）
- 项目根：`/workspace/ai-coding`
- 覆盖：ORC-01（注释 + 钉扎测试）、ORC-02、ORC-03、ORC-04、ORC-05、MCP-10a
- 状态：已实现并验证；未 commit
- 依据：`01-research.md`、`02-design-review.md`（"裁决/采纳"列逐条落实）

---

## 1. 逐项改动

### ORC-02 `max_strategy_nudges` 失效（级数上限）

文件：`ai_coding/core/interventions.py`

- `LadderLimits.strategy_l1` 默认 `1 -> 3`，注释写明它是**级数上限**、`0` 关闭整条 gate。
- 新增纯函数 `_strategy_due(entry, snap, limits)`（约第 398 行）：

```python
cap = min(max(0, int(limits.strategy_l1 or 0)), L3)
```

  - `cap <= 0` → `None`（不提示、不提前收尾）；
  - `entry is None` → L1 nudge；
  - `pending` → `None`（观察窗口）；
  - `failed`：`level >= cap` → 按实际 level finish（评审整改前恒报 L4），否则 `level + 1` nudge；
  - `unfinished` → `min(max(level, L1), cap)` 同级重试；
  - `helped / exhausted / unknown` → L1 nudge。
- `_due` 的 STRATEGY 分支改为直接返回 `_strategy_due(...)`；原通用分支里的 strategy 分支已删除（不可达）。
- 4 处默认值同步为 3：`interventions.py`、`turn_policy.py`（字段 + `from_ai_config` fallback）、`config/models.py`。

文件：`ai_coding/core/turn_policy.py`、`ai_coding/config/models.py`、`config.example.yaml`

- 注释说明"级数上限、`0` 关闭、>3 收敛到 3、显式 1/2 的用户会看到更少 nudge"。
- `config.example.yaml` 新增 `max_strategy_nudges: 3` 及说明。

### ORC-03 `search_streak_threshold=0` 未真正关闭

文件：`ai_coding/core/interventions.py`

- `_triggered(SEARCH)` 顺序改为：posture（plan/readonly）→ `search_streak_l3` 硬停 → `stale_l3` 硬停 → `if not threshold: return False` → repeat → 计数档 → stale L1。
- `_search_due` 在两处硬停（`search_streak_l3`、`stale_l3`）之后、`repeat_streak` 之前插入同一 guard。
- `threshold=0` 仍保留 L3 / stale L3 硬停（各自有独立开关）。评审整改把 `stale_l3` 的触发判定也提到 threshold guard 之前，否则默认 `stale_l3=6` 在 `threshold=0` 下不可达。
- 有意副作用：plan/readonly 下 `repeat_output` 不再提示（posture 判断提前），补测试锁定。

### ORC-04 最后一轮仍注入 POST nudge

文件：`ai_coding/core/agent_loop.py`（POST 分支，约第 1418 行）

- **不使用 `_has_a_round_left`**。在 `_note_intervention` 之前预测下一步：

```python
next_step = self.policy.decide_next_iteration(
    replace(snapshot, iteration=index + 1),
    budget_expired=(frame.deadline is not None and frame.deadline.expired),
)
```

- 只有 `next_step.action is FINISH` 时不注入，转 `FINISH` 并把文本写入
  `decision.note = decision.inject or decision.note or decision.reason`。
- 转换发生在 `_note_intervention` **之前**，账本按 finish 记为 `exhausted`。
- FINISH 分支状态：按 `next_step.reason` 映射（`turn deadline exceeded` / `budget_expired` → `TIMEOUT`，`turn token budget exhausted` → `BUDGET`，其余 → `MAX_ITERATIONS`），与 `terminal_outcome` 共用 `turn_close.terminal_status`；非转换的 FINISH 仍为 `COMPLETED`。

**偏离（必要）**：`next_step.reason in ("iteration budget exhausted", "search unconverged")` 时**不转换**。原因：G8-2 会在这两个 reason 上再发一次"收尾"模型调用，该调用**会读到**刚注入的 nudge，因此它不是浪费。若不做这个豁免，`tests/test_agent_loop_react.py::test_strict_policy_ignores_the_nudge_cap`（断言 5 次模型调用）会失败，属于弱化既有断言。其余 FINISH 口（token 预算、deadline、PRE 阶梯）照常转换。

### ORC-05 阶梯 FINISH 可观测性

文件：`ai_coding/core/turn_close.py`

- 新增 `_LADDER_SOURCE_GATES = ("strategy", "search")`。
- `terminal_outcome` 对这两类在 `reason` **追加后缀** `[<gate> ladder L<level>]`（level 为 0 时只写 `[<gate> ladder]`）。
- `task` / `fatigue` 不动（golden 与既有契约）；token 预算 reason 不加后缀。
- 后缀不破坏 `domain/turn_digest.py` 的 `startswith("no progress") / startswith("search unconverged")`。

### MCP-10a `mcp_chars_warn` 永不触发

文件：`ai_coding/core/agent_loop.py::_warn_tools_chars_once`

- 统计改为加性：`origin_mcp_chars`（非 local schema）+ `catalog_chars`（`self.catalog_builder()` 长度）。
- `catalog_builder()` 只在"对应阈值 > 0 且尚未告警"时调用，避免告警后的每轮额外构造成本。
- `tools_chars_warn` 的总量口径保持 schema-only（`self._tools_chars` 不变）。
- catalog-only 告警时 hint 不再输出空的 `''` 来源，改为"主要成本是每步重发的 `<mcp-catalog>`"。
- `config.example.yaml` 新增 `mcp_chars_warn: 2000` / `tools_chars_warn: 0` 及注释，说明默认 2000 在大多数 catalog 下会触发；`config/models.py` 同步说明。

### ORC-01 计划实施回合无上限（仅注释 + 护栏测试）

- `turn_policy.py` `ceiling` 计算前：注明"无步数与墙钟上限，仍受窗口/阶梯/模型自停/goal 轮次约束，勿在无 per-item resume 设计时加回 cap"。
- `agent_loop.py` `deadline = None` 分支同样补注释；`_step_ceiling` docstring 补"`-1` 同时被 `_has_a_round_left` 消费"。
- 无任何表达式改动。

---

## 2. 测试改动

| 文件 | 内容 |
|---|---|
| `tests/test_interventions.py` | ORC-02：默认 3 的完整 L1→L3 finish、cap=2 → L2 finish、cap=0、cap=9 收敛；ORC-03：`threshold=0` 关 repeat、plan/readonly 下 repeat 静默；评审整改补 `threshold=0` + stale 超阈值 → L3 finish（含 `stale_l3=0` 关闭与 `repeated_nonempty<2` 不触发） |
| `tests/test_turn_policy_escalation.py` | `max_strategy_nudges=2` 在失败 L2 后 FINISH（finish level 现为 2）；`=0` 静默 |
| `tests/test_turn_close.py` | strategy L4 / search L3 后缀；task 与 token 预算 reason 不变 |
| `tests/test_interventions_loop.py` | 4 处 search outcome reason 改为精确新值 `search unconverged [search ladder L3]`（583 行 `SearchStalled.reason` 不动）；ORC-04 转换用例（token 预算口 → `BUDGET`）+ "iteration budget 口仍注入"反向用例 |
| `tests/test_turn_policy.py` | ORC-01：`plan_implementing` 在 iteration=10000 仍 ITERATE，普通回合同 iteration FINISH |
| `tests/test_tool_surface_warning.py` | `_loop` 支持 `catalog_builder`；catalog-only 告警、schema+catalog 加性、空 catalog 静默 |

未改 `tests/golden/**`，未弱化任何既有断言。

---

## 3. 验证结果

```
pytest tests/test_turn_policy.py tests/test_interventions.py tests/test_interventions_loop.py \
       tests/test_loop_escalation.py tests/test_agent_loop_react.py tests/test_turn_close.py \
       tests/test_tool_surface_warning.py tests/test_turn_policy_escalation.py \
       tests/test_plan_codex_path.py -q        -> 全部通过
pytest tests/test_loop_refactor_equivalence.py tests/test_config_loader.py \
       tests/test_turn_digest.py -q            -> 全部通过
pytest <turn_policy/interventions/agent_loop/turn_*/config/request_assembly 全组> -q
                                               -> 全部通过（约 464 项）
ruff check ai_coding <对应测试>                -> All checks passed!
mypy ai_coding                                 -> Success: no issues found (186 files)
```

全量 `pytest tests/ --ignore=tests/integration -x` 在
`tests/test_job_tools.py:144`（job 列表行数）中止。该用例属于其它批次的
`core/jobs.py` 改动，不经过本批任何文件，判定为**既有/并行批次失败**，与本批无关。

评审整改后重跑（2026-11-07）：

```
pytest tests/test_interventions.py tests/test_interventions_loop.py \
       tests/test_loop_escalation.py tests/test_agent_loop_react.py \
       tests/test_turn_close.py tests/test_turn_policy_escalation.py -q
                                              -> 全部通过（175 项）
ruff check ai_coding tests/test_interventions.py \
           tests/test_interventions_loop.py tests/test_turn_policy_escalation.py
                                              -> All checks passed!
mypy ai_coding                                -> Success: no issues found (186 files)
```

> 复跑期间 `ai_coding/core/turn_report.py` 曾被另一批次并发修改（短暂缺
> `from typing import Any`，F821/name-defined）；该文件补齐 import 后，全树
> `ruff check ai_coding` / `mypy ai_coding` 已恢复为 All checks passed / 186 files。
> 该文件不在本批改动范围，未触碰。

---

## 4. 偏离与说明

1. **ORC-04 增加了 closing-call 豁免**（见 §1），否则必须修改 `test_strict_policy_ignores_the_nudge_cap` 的既有断言；这是对 02 裁决的细化，不是放宽。
2. `tests/test_tool_surface_warning.py` 不在任务显式列出的测试白名单中，但 MCP-10a 的验证必须修改该文件（`_loop` 增加 `catalog_builder`）；仅新增/扩展用例，未弱化既有断言。
3. `_strategy_due` 保留设计稿的 `snap` 形参（内部 `del snap`），以便与 `_due` 的其他分支同构。
4. ORC-04 的转换用例用 token 预算触发（`usage` 计入 `tokens_spent`），因为 iteration 预算口会被 G8-2 的 closing 调用读取，不满足"永远读不到"的前提。
5. `docs/`（features / how-to-run / data-contracts）中 `max_strategy_nudges=1` 的滞后描述仍待 W3 统一修正。

## 5. 最需评审的 3 处 diff

1. `ai_coding/core/agent_loop.py` POST 分支的 ORC-04 预测与"closing-call 豁免"：行为边界最微妙（C3 continuation / G8-2 / 阶梯 FINISH 三者的交互）。
2. `ai_coding/core/interventions.py::_strategy_due` + `LadderLimits.strategy_l1` 默认 1→3：改变 `max_strategy_nudges` 的语义（计数 → 级数），并同步三个默认值。
3. `ai_coding/core/interventions.py` 的 SEARCH guard 顺序调整：`threshold=0` 现在也关闭 repeat，且 plan/readonly 下 repeat 静默（有意的行为变化）。

---

## 6. 评审整改（REQUEST-CHANGES）

### P1-1 ORC-04 收尾状态映射

- `ai_coding/core/turn_close.py` 新增 `terminal_status(reason, *, budget_expired)`，`terminal_outcome` 改为调用它；映射规则集中一处：
  - `budget_expired` 或 `reason == "turn deadline exceeded"` → `TIMEOUT`；
  - `reason == "turn token budget exhausted"` → `BUDGET`；
  - 其余 → `MAX_ITERATIONS`。
- `ai_coding/core/agent_loop.py` 的 POST→FINISH 转换不再硬编码 `MAX_ITERATIONS`：转换时用 `next_step.reason` 调用 `terminal_status`，结果作为 `TurnOutcome.status`（非转换的 FINISH 仍为 `COMPLETED`）。
- `tests/test_interventions_loop.py::test_the_last_round_turns_a_post_nudge_into_the_terminal_note` 钉的正是这个错误状态：该用例用 `turn_token_budget=1` 关闭回合，下一次 PRE 的 reason 是 `turn token budget exhausted`，断言由 `MAX_ITERATIONS` 改为 `BUDGET`。
- 覆盖范围：`TIMEOUT` 分支由既有 `terminal_outcome` 测试覆盖（`budget_expired=True`），ORC-04 转换路径新增 reason 级映射测试（`BUDGET`）。

### P1-2 ORC-03 stale L3 不可达

- `interventions.py::_triggered(SEARCH)`：把 `stale_l3` 硬停判定（`stale_streak >= stale_l3` 且 `repeated_nonempty >= 2`）移到 `if not limits.search_streak_threshold: return False` **之前**。
- 结果：`threshold=0` 仍关闭 repeat / 计数档 / stale L1-L2 建议档，但 `stale_l3`（默认 6）与 `search_streak_l3` 两个硬停保留（各自有独立开关，`stale_l3=0` 可关闭）。
- `tests/test_interventions.py` 新增：
  - `test_the_stale_hard_stop_is_independent_of_the_count_switch`：`threshold=0`、`stale_streak=6`、`repeated_nonempty=2` → `(search, L3, finish)`；
  - `test_the_stale_hard_stop_keeps_its_own_zero_switch`：`stale_l3=0` → 不触发。

### P2-1 `_strategy_due` 收尾 level

- `failed` 且 `level >= cap` 时返回 `(level, "finish", "")`，不再恒报 `L4`。`cap=1/2` 的收尾后缀现在是 `[strategy ladder L1/L2]`，默认 `cap=3` 为 `L3`。
- 同步断言：`tests/test_interventions.py`（默认 L3、cap=2 的 L2、clamp 后 L3）、`tests/test_turn_policy_escalation.py`（`finish.level == 2`）。`tests/test_turn_close.py` 中显式构造 `level=4` 的后缀用例未改（它验证的是格式化，不是 `_strategy_due`）。

### P2-2 `max_strategy_nudges < 2`

- `config/models.py` 字段注释补充：`armed()` 依据“开放中的 L2 窗口”判定，cap < 2 时 L2 永不出现，`strict_repeats` 整回合不会武装；1 不是“更少 nudge”，而是失去结构化重复防护。

### P2-3 仅记录、不改代码

1. **token 已满时 `_ask_for_closing` 返回空**：`_ask_for_closing` 在 `tokens_spent >= max_turn_tokens` 时直接返回 `""`（请求会被拒绝，且预算正是已经用尽的那个）。因此 token 预算口收尾时没有模型撰写的结束语，只有 harness 报告与 `TurnOutcome.note`。这是既有取舍：不为一句客套再超预算一次。
2. **`mcp_chars` 与 `tools_chars` 口径不一**：`mcp_chars = 非 local schema + <mcp-catalog> 长度`（MCP-10a 加性），`tools_chars = 全部 schema 文本长度`（不含 catalog）。两个阈值刻意回答不同问题（“MCP 花了我多少” vs “每步工具面下限”），本批不合并口径；只在此记录，避免被误读成同一个数字。
