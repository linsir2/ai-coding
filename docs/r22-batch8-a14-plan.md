# 批次 8-4 方案（R22·A-14 目标完成后任务清单没有收尾）

> 只出方案，不改代码。依据：`docs/r22-batch8-queue.md` 第 4 项、`docs/r22-feature-matrix.md` §5/§9
> A-14，以及 S4 会话 `r22-s1/.ai-coding/sessions/957ab4e2…json` 的原文。

## 1. 调研：失败链（S4 会话逐条对齐）

会话里的顺序就是问题本身：

1. **清单是旧的**：本轮 goal 是"给 `src/mathx.py` 加模块级 docstring"，但清单里还留着上一段工作
   的 `1. Add type hints… add(a, b)`（`in_progress`）、`2. … sub(a, b)`（completed）、
   `3. Verify final file structure`（completed）。`arm_goal` 明确**保留** item list
   （`domain/task.py:151-159`），所以旧条目会跟着新目标进来。
2. **模型如实申报完成**：`update_goal(status="complete")` → `goal_phase="complete"`
   （`tools/goal_tools.py:204`），但**没有任何人处理清单**。
3. **任务门继续催**：同一回合的下一迭代，`stop_policy: todo_aware` 的 TASK 门看到
   `open_items()` 非空（第 1 项 `in_progress` 不是 terminal），注入

   ```text
   [task-gate] 你还有未完成的任务项：
   - 1. Add type hints (int | float) and docstring to add(a, b)
   请继续推进，或把不再需要的条目用 todo_write 标记为 dropped 并说明原因。
   ```

   模型于是**去补目标之外的类型注解**（`edit` + `bash` 验证各一次），再写一段"当前状态"解释。
4. 这一轮以 `回合未完成 · 迭代预算用尽（10 轮）` 结束；清单里第 1 项仍是 `in_progress`。

即：**目标已申报完成，但清单与门控还按"没做完"行事**，两边都错。

## 2. 方案（一处工具改动）

**FIX-A14-1：`update_goal(complete)` 把仍非终态的条目标成 `dropped`。**

`tools/goal_tools.py::UpdateGoalTool.execute`，在写入 `goal_phase` 之后：

```python
closed = [
    {"id": item.id, "content": item.content, "from": item.status}
    for item in task.items if not item.is_terminal()
]
for entry in closed:
    task.update(entry["id"], status="dropped")
```

- 收据给出**内容和原状态**（`- 1. Add type hints… （原 in_progress）`），只给 id 模型无法判断为什么；
- `data["closedItems"]` 带上同样三项（`id`/`content`/`from`），轨迹与审计能区分"模型自己 dropped"
  与"工具代 dropped"；没有要关的条目时不写这个键；
- 清单很长时只点名前 10 条（`CLOSE_NOTICE_LIMIT`，与任务门列出的上限一致）。

**`blocked` 也关**：目标已申报完成，"这一步现在做不了"就失去意义；留着它（非终态）会继续触发同一个
任务门，修复等于没做。`blocked` 的"下次重试"语义只适用于没申报完成的目标。

### 为什么是 dropped 而不是 completed

- `completed` 会撒谎：这些条目没人做（S4 里第 1 项就是没做），而 `dropped` 的语义正是
  "不再追这件"（`domain/task.py:23`，TERMINAL 集合含 dropped）；
- 清单是模型自己的（`arm_goal` 保留它），`complete` 是模型自己的声明：声明之后的残留项
  属于"上一版工作"或"已放弃"，标 dropped 与声明一致；
- `blocked` 不动清单：同一阻塞下次立案重试是设计（C3 也明确 blocked 不算"模型没干完"，见
  `turn_policy.py:396-398`）。

### 连带效果

- 下一迭代注入的 `<task-state>` 变成 `进度: N/N`、没有 `[>]`、没有 `下一步:`；
- TASK 门（`interventions.select` 的 POST 分支）没有 open items 可催，不再烧迭代；
- 收尾仍是模型自己的文字（工具文案已说"本轮做收尾说明即可"），没有新增回合动作。

### 已知取舍（评审）

- **假报 complete**：模型提前申报、真实工作没做完时，残留项会被静默 dropped。goal 轮内 C3 已关
  （`goal_runner.py:244`），TASK 门是唯一反制；但"只关上一 episode 的陈旧项"需要给 `TodoItem`
  加 episode 归属（S4 的条目 `createdAt` 为空），属于另一个改动。现方案的立场：`complete` 是模型
  自己的声明，声明之后的清单从属于它；假报的代价由 goal 驱动/人工审查承担，而不是让那条清单一直说谎。
- **账本**：`agent_loop.py:2289` 在"有 items 且全部终态"时清空 `intervention_state`。关完条目就满足
  这个条件，所以 TASK 门的历史不会跨回合留下假"已帮助"信号，无需另改。

### 不做

- 不在 `arm_goal` 里清空旧清单（那是模型的工作记忆，且与 `/plan` 共享）；
- 不自动结束回合（`update_goal` 之后模型还要写收尾说明；goal runner 在回合结束后才停）；
- 不改 TASK 门的通用判据（它按清单状态工作是对的，错的是清单没人收尾）。

## 3. 测试（先红后绿）

| 测试 | 断言 |
| --- | --- |
| `tests/test_goal_tools.py::test_a14_complete_closes_the_items_the_goal_supersedes` | `in_progress` + `completed` 两项 → `["dropped","completed"]`；输出含 `dropped`；`render()` 无 `[>]`/`下一步`，`进度: 2/2` |
| 同文件 `test_a14_complete_closes_pending_and_blocked_items_too` | `pending`/`blocked` 也变 `dropped`；`completed` 不动；`closedItems[].from` 如实记录 |
| 同文件 `test_a14_complete_on_a_clean_list_says_nothing_extra` | 空清单：无 `closedItems` 键、输出不含 `dropped` |
| 同文件 `test_a14_a_second_complete_changes_nothing` | 重复申报幂等 |
| 同文件 `test_a14_the_task_gate_stops_asking_once_the_goal_is_complete` | 申报前 `decide_after_step` 是 NUDGE，申报后是 FINISH（"model finished"） |
| 同文件 `test_a14_blocked_keeps_the_items_for_a_later_attempt` | `blocked` 后 `goal_phase=blocked`，`in_progress` 原样保留 |
| 既有 11 例 | 原样通过 |

## 3.1 完成记录（2026-10-29）

`tests/test_goal_tools.py` 17 例全绿（新增 5 例）；`tests/test_goal_runner.py`、`tests/test_turn_policy.py`
全绿；`ruff` / `mypy` 干净。评审并入：收据带内容与原状态、`data.closedItems`、`blocked` 也关、
空清单/重复申报/门控行为补测。

## 4. 验收

1. `pytest tests/test_goal_tools.py tests/test_goal_runner.py -q` 绿；`ruff` / `mypy` 干净；
2. 用一个离线构造：旧清单 + `update_goal(complete)` → `TaskState.render()` 里没有 `[>]`；
3. （真机复跑 S4 成本高）以 S4 会话为证据源，说明修复点落在第 3 步的注入之前——
   清单在 `update_goal` 那一刻就已是终态。
