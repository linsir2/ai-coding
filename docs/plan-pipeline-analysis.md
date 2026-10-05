# AI-Coding — 计划链路收口分析（R1-5b/R1-6 · M1 · C3 · 审核与修复）

> **为什么是文件名而不是轮次号**：检索轮的 `R11` 已被「记忆轮」占用（见 README 的命名撞车说明），
> `R12` 属于更早的界面轮系列。本轮不占编号，按主题命名。

> 本轮做的是**收口**，不是加功能：把「计划」这条链路从契约、落盘、可见性到停止策略补齐，并把上一轮自己
> 写错的代码与说错的话逐条改回来。全部结论都有代码出处或本轮实测，命令写在正文里。
> 上一轮：`r10-analysis.md`（检索）。本文之后：`r7-backlog.md` 里仍然挂着的结构债。

---

## 0. 一句话结论

`/plan` 从「两命令接力」变成「一条工作流」之后，链路上有 **4 处真缺口**（执行结果不落盘、C3 泄漏到计划回合与
子智能体、步正文不可见、REPL 与 TUI 分叉）和 **9 处小缺口**（收据证明等级、失败步记录、清单替换后的旧收据、
trace 归属、`/plan-run` 无提示、气泡时间戳、契约键名、防御式读取、死代码），外加 **2 个设计缺陷**
（`blocked` 项会买 C3 的额度；M1 的「线性」说法是错的）。

---

## 1. 现状核验（全部本轮实测）

### 1.1 计划的执行结果曾经不落盘（HIGH）

- M1 把步移到独立 session（H1 让 ephemeral 步不落盘），**顺带删掉了唯一会保存父会话的东西**：M1 之前每个步回合
  都是 `process_input(parent)`，回合收尾必然 `TurnCloser.save(parent)`。
- `plan_runner.py` 里没有任何 closer 引用，`textual_app.py` 在 runner 之后也不保存。实测（真实
  `AgentLoop` + 真实 `SessionService`）：

```
after plan run -> disk: stepDeliveries 键不存在, items [pending, pending], messages 2
in memory      ->       1 条收据, 4 条消息
```

- 这个洞能活过审核，因为**内存对象永远是对的**：任何后续回合都会把它存回去。
- 修法：`PlanRunner.run` 结束时一次 `closer.save(session)`（`1467cca`）。

### 1.2 C3 的前提在两个调用点不成立（MEDIUM）

C3（预算耗尽且 todo 仍有 open 项 → 注入 `[continue]` 并重新武装预算）的前提是**模型在干自己的活**。实测两个反例：

| 调用点 | 机制 | 实测 |
|---|---|---|
| 计划回合 | prompt 写着「不要执行工作」，而计划项必然 open | `max_tool_iterations=3` 时 **10 次模型调用 / 3 次续跑**（不开 C3 是 2 次） |
| 子智能体 | `_subagent_settings` 只覆盖 `turn_timeout_seconds` | 有 open 项时 **12 次调用**（预算 3，本该 4 次） |

- 计划步早就在 `plan_runner._run_step` 里显式关掉了 C3，理由与这两条相同。
- 修法：计划回合与子智能体都关（`e54d3b6`），守卫是 `plan_runner.continuations_disabled`，
  **直接读 `loop.policy`**：没有 policy 的对象不是 AgentLoop，静默跳过会让 C3 在正要关掉的回合里继续开着。

### 1.3 `blocked` 项会买 C3 的额度（本轮新发现）

```
item.status=pending      模型调用=6 [continue]=3
item.status=in_progress  模型调用=6 [continue]=3
item.status=blocked      模型调用=6 [continue]=3   ← blocked 也算「未完成」
```

`TERMINAL_STATUSES = {completed, dropped}`，所以 `open_items()` 把 blocked 算 open —— 对 runner 是对的
（验收失败的步下次 `/plan` 要重试），对 C3 是错的（blocked 的语义是「这一步现在做不了」）。
修法：**过滤只发生在 policy 的判定里**（`f60acf3`）。实测 6 次 / 3 续跑 → **2 次 / 0 续跑**。

### 1.4 步正文在执行时不可见

`PlanRunner.run(..., on_delta=None, observer=None)` 一路把 `on_delta` 传给步回合，但两个 UI 调用 runner 时都没传。
用户看到的是一串 `[plan] 步骤 k/N`，模型写了什么、跑了哪些工具全部不可见；而步 transcript 按 M1 的设计
**用完即弃**，所以那个 live 视图是这份工作**唯一**能出现的地方。

### 1.5 REPL 的 `/plan` 在生产里不可达，且已与 TUI 分叉

- `cli.py:54-59`：**唯一入口**是 `run_app`（Textual），没有 `--repl` 之类参数。
- `textual_app.run_command:148` 先拦 `/plan` 与 `/plan `，其余 `/x` 才交给 `handle_input`。所以 `repl.py` 不是死代码
  （它是 TUI 的 slash 分发器），但它的 `/plan` 分支永远不执行。
- 于是两份实现漂移：REPL 那份把字面量 `the current task` 当话题，且**不带话题时不重开计划**（不调
  `begin_plan`，旧 goal 与旧收据留着）；planning prompt 在两处各写一份。
- 修法：`plan_runner.plan_and_run` + `PLAN_PROMPT` 单一定义，`ask_topic` 由前端注入（`8a5df6e`）。
  **先否掉了「直接删分支」**：那样 `parse_slash("/plan")` 仍会解析成已知命令却无人处理，变成静默无操作。

### 1.6 吸底策略在采样法下不成立

- 规格来自 `docs/superpowers/plans/2026-09-28-textual-ui.md` §3.3-3（`is_vertical_scroll_end` 用于吸底判断），
  并把验收测试原文写死。
- 采样在 `mount()` **之后**读「用户是否在底部」，而挂载的控件要到下一次 layout 才有高度 —— 读到的是一个
  已经不存在的视图。实测两种失败形态：

```
60 个块突发：target_y=0, max_scroll_y=36     ← scroll_end 从未生效（burst 让视图看起来像被滚走）
30 回合重放：target_y=28/36/40/52, max=96     ← /resume 后停在历史中段
```

- **六个候选的实测结果**（`/tmp` 探针 + 真实 pytest，样本 10–70 次）：

| 候选 | 规格测试 | replay | 结论 |
|---|---|---|---|
| 现状（采样 + 立即滚） | 2/30 失败 | 8–9/10 | 基准 |
| 立即 + `call_after_refresh` | 1/30 | — | refresh 早于布局生效 |
| 立即 + `call_later` 重臂 | **0/70 ✓** | 9/10 | **有语义缺陷**：延后滚动覆盖用户其间的滚动，把 `test_overflow_turns_keep_a_working_scroll` 变成 flaky |
| 有界「跟随到稳定」（用 `is_vertical_scroll_end` 把关） | 1/30 | 8/10 | 链在第一跳中止 —— 守卫本身是错的那个 |
| 粘滞 pin + `watch_scroll_y` 检测上滚 | **30/30 全败** | 8/10 | 程序滚动与用户滚动分不清 |
| 只给 replay 加可等待落定 | 0/30 | 9/10 | 治不了根 |
| **Textual `anchor()` + `_pinned`** | **0/40 ✓** | **30/30 ✓** | 采用（`00d44fc`） |

- 关键发现是**框架早就有这个政策**：`Widget.anchor()` = 「锚定的控件在内容增加时保持吸底，直到用户移动滚动位置」，
  `_compositor.py:609/693` 在每次 arrange 时用 `set_reactive` 把它钉在底部（`set_reactive` **绕过 watcher**）。
  自己写 pin 状态机是重复造轮子 —— 前五轮候选全是在重新实现它。
- `_pinned` 只负责一件 anchor 做不到的事：**用户走了之后不要把他拽回来**。这件事可判定，因为三种滚动来源不同：
  我们的滚动总落在 `max_scroll_y`；compositor 的维持写入绕过 watcher；只有用户滚动会停在底部之上。
- 配套两处**必需**改动：`Turn.flush()` 改为回报是否真的写了东西（否则 50ms 定时器空转也在吸底，
  `test_overflow_turns_keep_a_working_scroll` 当场变红）；`clear()` 重新吸底（`/clear`、`/resume` 之后）。

### 1.7 M1 的「线性」说法是错的

`_seed` 与三处文档都写「N 步花费 O(N) 上下文，不是 O(N²)」，并说 seed「大小恒定」。实测 seed 总字符：
8 步 2364、16 步 8804、32 步 33836、64 步 132284 —— **每翻一倍约 4 倍**，是对步骤数的二次。
真正被消灭的是「重发父会话全部历史」（4 回合 41k/82k/130k/254k），现在每步只带 goal + 未完成项 + 本步验收。
更糟的是那个 ratio 测试本身：`test_prompt_growth_is_linear_not_quadratic` 在阈值 3.0 上以 **2.963** 通过，
7 步时是 3.80 —— 它钉住的正是它声称要排除的二次增长。换成结构化断言（`24064db` / `a5bfd11`）。

---

## 2. 本轮否掉的设计（写下来，免得下次重提）

1. **删除 REPL 的 `/plan` 分支**（M4-a 的第一版）：会让一个已知命令变成静默无操作。改为抽共享函数。
2. **把 `blocked` 加进 `TERMINAL_STATUSES`**（F1 的一个选项）：等于放弃「验收失败的步下次重试」。
3. **在调用点过滤 `blocked`**（F1 的第一版实现）：会把过滤泄漏到 `_next_ready`、`<task-state>` 与停止策略；
   测试逼着它回到 policy 内部。
4. **延后滚动 / 粘滞 pin / 有界重臂**（(a) 的前五个候选）：见 §1.6 表格，每个都有实测反证。
5. **保留 `getattr` 做「防御」**：六处读的都是 dataclass 字段（`continuations` / `ephemeral`），
   `int(... or 0)` 会把非 int 静默吞成 0 —— 而那是决定 C3 是否继续烧钱的计数器（`b49fe77`）。
   `plan_runner` 里容忍 `object()` stub 的 `getattr` **保留**：那是真实的 duck-typing 边界。

---

## 3. 目标架构（计划链路全貌）

```
用户 "/plan <topic>"
  │
  ├─ textual_app.run_command ─── "/plan" 或 "/plan " 命中 ──┐   （其余 /x → handle_input）
  │                                                          │
  └─ plan_runner.plan_and_run(loop, session, console,        │  ← 唯一工作流实现（8a5df6e）
        topic=…, ask_topic=前端注入（TUI 弹窗 / REPL Prompt.ask）
        │
        ├─ session.begin_plan(topic)     goal + items + receipts 三位一体
        ├─ continuations_disabled(loop)  计划回合关 C3（预算耗尽也不许"继续写计划"）
        ├─ PLAN_PROMPT.format(topic)     唯一 prompt 定义
        ├─ loop.process_input(规划回合)   → todo_write 写清单
        │    └─ TurnCloser → sessions.save(parent)          ← 规划回合的落盘点
        └─ PlanRunner.run(session, console)
             ├─ _drop_foreign_receipts   清单被换掉时清掉不属于它的旧收据
             ├─ 每步：console.step(title) 开折叠块（M3）
             │   step = SessionData("plan-<hex>", ephemeral=True, task 共享)
             │   seed = goal + 未完成项 + 本步验收          ← 不含任何更早步的产出
             │   policy 换副本：max_iterations=步预算, natural, C3=0
             │   process_input(step, prompt, on_delta=step_view.delta)
             │      ├─ TurnCloser.save(step) → ephemeral 跳过（不落盘）
             │      └─ _bind_trace(step)     → 不改 trace 的 session_id（C1）
             │   _settle → task.update + 收据(ac/usage/seedChars)
             │   _record_on_parent → 父会话 += [提示气泡(带时间戳), 摘要气泡]
             │   step_view.end(摘要) → 折叠体标题变成收据那句话
             ├─ 失败步：收据 + 气泡都写（C3 修复），重放里出现 ✗ 受阻
             └─ _persist(parent)          ← 1467cca 补的落盘点
```

---

## 4. 契约变更汇总

| 契约 | 变更 | 出处 |
|---|---|---|
| `StepDelivery.to_dict` | `seed_chars` → **`seedChars`**（统一 camelCase，不做兼容双读；旧文件读成 0，0 本就是「未知」的意思） | `24064db` |
| `StepDelivery.proof` | `verified` 要求每条验收判定 `passed is True`，不再只看 `ac` 非空 | `0b90d85` |
| `SessionData.ephemeral` | ephemeral 会话也**不接管 trace 的 session_id**（trace 的 key 语义改为「当前回合的持久 session」） | `cd2f6b2` |
| `TaskState.open_items()` | **不变**；C3 的判定在 policy 内过滤 `blocked` | `f60acf3` |
| `Turn.flush()` | 返回 `bool`（是否真的写了内容），驱动 50ms 定时器空转时不再吸底 | `00d44fc` |
| `Transcript` | 吸底改为 `anchor()` + `_pinned`；新增 `begin_plan_step()` 与 `StepView` | `00d44fc` / `10b4d9e` |
| `DialogueConsole` | 新增 `ask()`（含 EOF）与 `step()`；`TextualConsole.step()` 返回折叠体视图 | `8a5df6e` / `10b4d9e` |
| `plan_runner` | 新增 `PLAN_PROMPT`、`plan_and_run()`、`continuations_disabled()` | `8a5df6e` |
| `slash` | `_DEPRECATED` 墓碑表：`/plan-run` → `/plan`（两个前端自动生效） | `18b48b8` |

---

## 5. TDD 用例清单（新增）

| 用例 | 钉住的性质 |
|---|---|
| `test_run_persists_the_parent_session` | 跑完 plan，父会话文件里必须有收据与步气泡 |
| `test_unrun_verdicts_do_not_grade_verified` | 读取端不信任 `ac` 非空（`passed: None` / `False` / 混合都不算 verified） |
| `test_a_failed_step_still_leaves_a_replay_marker` | 失败步通过 `_step_header`（重放真正用的匹配器）能出 header |
| `test_a_replaced_list_does_not_inherit_the_previous_plan_s_receipts` | 换清单后新计划拿到自己的收据，旧收据消失，id 不再一名两步 |
| `test_absorbed_command_points_at_its_replacement` | `/plan-run` 指向 `/plan`，且 `parse_slash` 仍不认它 |
| `test_absorbed_command_is_answered_instead_of_becoming_a_model_turn` | 端到端：不进模型 |
| `test_parent_step_bubbles_carry_real_timestamps` | 通过 `_hhmm` 断言渲染形状，不钉格式串 |
| `test_no_step_reads_an_earlier_step_s_output` | 两条泄漏通道（工具输出 + 已完成步的陈述）都不许出现 |
| `test_step_turns_trace_under_the_parent_session` | ephemeral 步不改 trace 归属（手投事件证明目录由 session_id 决定） |
| `test_plan_turn_runs_with_auto_continuation_disabled` | 计划回合 C3 关、loop 的 policy 事后复原（TUI 与 REPL 各一条） |
| `test_subagent_does_not_auto_continue_its_budget` | 子智能体调用数 ≤ 自身预算，且消息里没有 `[continue]` |
| `test_a_blocked_item_is_not_unfinished_work` | blocked 不触发续跑，但 `open_items()` 仍视其为 open（runner 要重试） |
| `test_a_plan_step_view_grows_live_and_closes_on_the_summary` | 步折叠体实时长、标题最终等于收据摘要 |
| `test_planning_shows_each_step_as_a_live_fold` | 真实 runner + app console：两个步两个折叠体，正文可见 |
| `test_replaying_a_long_session_lands_on_the_newest_turn` | 同进程 6 次重放都停在最新回合（旧的 one-shot 实测 2/6） |
| `test_appending_after_the_user_scrolled_up_leaves_the_view_alone` | 策略另一半：用户上滚后不被拽回（characterization，两版都绿） |

---

## 6. 自我核验与留下的坑

1. **过程失误**：一次 pytest 失败的情况下提交了 `bee4452`（命令用换行串联，退出码没拦住 commit）。提交内容事后
   三次全量验证为绿，但流程是我的错，记在这里。
2. **flaky 的真实归属**：`test_pinned_transcript_follows_the_end` 在审核前的 `0a1a0c9` 上实测 2/25、当前树 1/25，
   同一棵树连续两轮 0/20 与 2/20 —— 是竞态不是回归，靠负载触发。修完后 0/40。
3. **吸底竞态是负载相关的**，所以**没有单进程测试能当确定性护栏**。replay 护栏写成「同进程 6 次重放」
   （旧实现有负载时 2/6，新实现 0/6），另两条新测试是策略 characterization，两版都绿 —— 不冒充回归护栏。
4. **实时视图与重放不对称**：步正文只在 live 可见，`/resume` 只能重建收据那一行。这是 M1「步 transcript 用完即弃」
   的直接后果，写进了 `features.md`，不是遗漏。
5. **没做的事**：R7 backlog 的 P0（`agent_loop.py` 现 828 行 / `process_input` 仍是本轮最大函数）本轮又长大了一点；
   cache 断点仍未测（按当初决定：先测量命中率）。
