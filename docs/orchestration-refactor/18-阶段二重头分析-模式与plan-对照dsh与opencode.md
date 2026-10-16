# 18 · 阶段二重头分析（模式 + `/plan`）——第一性推导，对照 dsh / opencode

> 状态：**已拍板（2026-10-15）**：Q1=B（两开关拆分）/ Q2=B（PLAN_PROMPT 增补两句）/ Q3=B（queued 到回合末）/ Q4=A（P0/P1 保留+补缺）——修订落于 **§10**，与 §3/§5/§6 冲突处以 §10 为准。
> **实施状态（2026-10-15）**：P0–P5 全部落地并验证——两轴字段与序列化、dispatch 只读闸（失关闭）、`build(mode=)`、快照双轴早退、**验收 shell 旁路封堵**、F5 豁免、`/plan|off|run|readonly` 命令矩阵 + queued 机制（close 搭车落盘）、stats/load 恒显式两键、前端状态栏矩阵标记（bun test 314 + typecheck 绿）；`16` F5 已修；r19 新增 `plan_mode` 真机场景 PASS（进模式→试写被拒可见→写 2 项→run 执行 2 步收据 2 份，证据 `/tmp/opencode/r19-p5/`）。孤儿清理：`DialogueConsole.ask` 删除、前端 7 处过期文案随批修、`/readonly` 入前端目录。
> 方法：三路并行只读调研——dsh `@571902028a75`（`/home/linsir365/projects/deepseek-harness`）、opencode `@0a46301e`（`/home/linsir365/projects/opencode`）、本仓 HEAD+P0/P1 全量 diff；全部事实带 file:line；问卷回执见 §9。
> 范围：模式与 `/plan`；P0/P1 已写代码的预检并入 §7（正式修复清单待拍板后行动）。

---

## 0. 一句话与最终形态

**把"规划期只读 + 计划进执行引擎"做成会话姿态**：`session.mode`（P0 已落）、调用边界 dispatch 闸（P1 已落、可见拒绝）、提示词 `<plan-mode>` 段（P2）、决策面执行类门静默（P3，**含一处 17 漏掉的验收绕过洞**）、命令面 `/plan | off | run`（P4）、载荷与状态栏标记（P5）；计划载体复用本仓独有的 **todo 项 + PlanRunner 收据/证明引擎**——dsh 的"软引导+独立沙箱"与 opencode 的"agent 化+edit 级权限"各取一半，按本仓既有引擎裁剪。
> ※ Q1 回执修订：姿态**拆为两条正交轴**（`mode`=提示姿态、`readonly`=强制开关），组合矩阵、命令矩阵、queued 翻转语义与 P0–P5 增量全部见 **§10**（权威）。

## 1. 第一性原理：三个必须回答的问题（答案有调研出处）

**Q1 只读强制放哪层？**
- 事实：dsh 的 plan 模式**零强制**（`docs/subsystems/plan.md:5`："Plan mode is soft guidance. Sandbox mode and approval policy enforce restrictions independently"）；它的只读是**独立 sandbox 姿态**，在**执行边界**强制（bwrap/fs fence，`packages/fs/fs-sandbox/src/index.ts:112-144`），拒绝以 `[sandbox: file access denied under … mode]` 标记可见（`escalation.ts:64-73`）。
- 事实：opencode 在**调用级** `permission.assert` 强制（`packages/core/src/permission.ts:165-188`），deny 抛 `BlockedError` 并被改写为友好文案给模型（`plan.ts:45-54`），**但 bash 与 subagent 全部放行**（`schema/agent.ts:47` 基础通配 allow；plan 补的 4 条里没有 shell/subagent）——其"只读"实为 edit 工具级，文档与代码已漂移。
- **我们的推导**：本仓没有 OS 级沙箱（只有文件工具的工作区沙箱），也不打算建；提示词在阶段一已被实测证明"不忠实"（PLAN_PROMPT 说 do not execute，模型照做）。→ **调用边界闸（dispatch）是唯一正确层**：与 opencode 同层、比它多一条底线（bash/subAgent 也拒，失关闭）。这与 dsh 的"强制在执行边界"哲学一致，只是本仓的执行边界就是 dispatcher。

**Q2 姿态怎么建模？**
- 事实：dsh 用 **log-only 会话事件** `plan/mode {active}`（durable/可 replay、whole-value、不入 transcript；`plan-mode/src/index.ts:45-54`），另有 pending/queued 语义（`set()` 返回 committed|queued…，`src/index.ts:425-445`）。
- 事实：opencode 用 **agent 实体**：`Session.Info.agent` 字段（`schema/session.ts:39`）+ durable 事件 `session.agent.selected`（`session-event.ts:69-78`），plan 是注册表里的一个 primary agent（`plan.ts:12-43`）。
- **我们的推导**：没有多 primary-agent 注册表，也不想要（引入 agent 实体=注册/选择/按消息记录/子代继承一整摊）。**单字符串字段 + 条件持久化**（P0 已落）是与本仓 task/interventionState 同族的既有形态；事件日志语义由文件白名单满足（mode 只是个词）。pending/queued 的复杂度本项目当前买不到（见 Q3 翻转时点）。

**Q3 计划怎么承载与交接？**
- 事实：dsh 计划 = `exit_plan_mode` **工具调用的 markdown 参数**（唯一终态调用），规划期**明令禁 todo**（"Do not use todo_write to track this planning phase"，base bundle `cordis.patch.yml:340`）；批准走 user-questions 评审（Approve/Keep planning/Dismiss），**"A user's conversational agreement approves nothing"**（同段引文）；无自动执行，批准后下一步才开始做。
- 事实：opencode 计划 = **对话文本**（文件 opt-in，`~/.opencode/plan` 仅是可写白名单区），`todowrite` 已从产品移除；交接**纯手动**（切回 build agent），无一键执行。
- **我们的推导**：本仓有现成的**结构化执行引擎**——todo 项（带 verify 证明命令）+ PlanRunner（逐步 bounded turn + 收据 + 机器裁决）。计划载体**必须**是 todo 项，否则整个 `11`/M1 线报废。dsh 的"规划期禁 todo"是其体系专属规则（他们的 todo 是执行期跟踪，载体另有 exit_plan_mode）；本仓 todo 双职（计划载体+执行清单），**不禁**。交接用 **`/plan run`**（用户显式命令）——正是 dsh "conversational agreement approves nothing" 的落地：执行启动必须是非对话的显式信号，且我们复用 PlanRunner，比 dsh 多出逐步验收能力。

## 2. 参照系事实速览（只列决策相关，出处齐全）

**dsh（软引导 + 独立沙箱）**
1. plan 模式=per-agent boolean，唯一配置是部署提示词段（`plan-mode/src/index.ts:68-72`），工具目录跨模式**稳定**（缓存理由，`src/index.ts:15-17`）。
2. 进入/退出：`/plan`、`/plan <msg>`（同回合 steer 一条消息）、`/plan off`（`src/index.ts:229-274`）；切换文案 'Plan mode on. Use /plan off to leave.' 等。
3. 状态变化有叙述去重（仅当上次请求头描述不同状态才注入 `'The user switched this session to plan mode.'`，`src/index.ts:462-474`）。
4. 规划提示词极丰富（六段：stay until approved / explore first / no edits / resolve by inspection / decision-complete / exit_plan_mode 唯一终态；`cordis.patch.yml:335-346`）——因为**强制为零，全靠话术**。
5. guard（repeat-tool-reminder）在 plan 下照常；与 plan 零代码耦合。
6. 已知边界自曝：pending 进程内丢失、子级不可开评审、单一渲染器（`README.md:184-188`）。

**opencode（agent 实体 + edit 级权限）**
1. plan 权限全集：`edit * deny` + `edit ~/.opencode/plan/* allow` + `external_directory` 例外；**shell=allow、subagent=allow**（`plan.ts:12-43` 与基础规则 `schema/agent.ts:46-52`）——存在真实绕过面。
2. deny 在**调用级** assert（`permission.ts:165-188`），edit/write/patch 的 schema **不消失**（因 plan 目录 allow 例外，`findLast` 命中的不是 `* deny`），靠调用拒绝 + 友好改写文案 `Cannot use edit to modify files outside the Plan directory: …`（`plan.ts:50-52`）。
3. 进入=切 agent（shift+tab 等，`tui/src/config/keybind.ts:171-173`）；状态=会话字段+durable 事件；compaction/revert 后有提醒对账重放（`plan.ts:56-74`）。
4. 计划载体=对话（默认），无 todo 工具；**无自动执行**，手动切回 build。

**对照总表**

| 面 | dsh | opencode | 我们（重推后） |
|---|---|---|---|
| 姿态建模 | log 事件 `plan/mode{active}`（soft） | `session.agent` 字段（agent 实体） | `session.mode` 字段（P0 已落，fail-safe 词表） |
| 只读强制 | 无（独立 sandbox 在执行边界强制） | 调用级 assert（edit 级；bash/subagent 放行） | **dispatch 调用级闸（mutates_world 失关闭；含 bash/subAgent）** |
| 入口/出口 | `/plan`、`/plan <msg>`、`/plan off` | 切 agent（无命令） | `/plan`、`/plan <topic>`、`/plan off`、`/plan run` |
| 计划载体 | exit_plan_mode 的 markdown；规划期禁 todo | 对话文本；文件 opt-in；无 todo | **todo 项（含 verify）+ PlanRunner** |
| 交接 | 模型提交→用户评审 Approve→下一步执行 | 手动切 agent | **`/plan run` → PlanRunner 逐步执行（收据/证明）** |
| 工具目录 | 跨模式稳定（缓存） | 全 deny 时才隐藏 schema | 稳定（不隐藏，拒绝可见） |
| 拒绝可见性 | sandbox 标记 + escalation | 友好改写文案 | `_mode_denied` 文案（P1 已落） |
| 子代理 | fork 继承/spawn 不继承 | 子会话不继承；但 plan 可 spawn（不拦） | 子会话/步会话默认 normal；plan 下 subAgent 被拒 |

## 3. 方案重推导（逐点取舍；✅=17 成立沿用，✏️=修订）

1. **强制层** ✏️ 表述修订：从"结构性拒绝"升级为显式对照结论——与 dsh"软引导+独立沙箱"**刻意不同**：本项目把"plan 引导"与"只读强制"**融合**为一个姿态（plan 的语义就是只读规划）。理由：本仓没有"只读但非规划"的第二场景；拆分=两个字段两套 UI，买不到东西。若未来出现"带写权限的规划会话"再拆（dsh 的正交性留给未来的我们）。→ **问卷 Q1 复核**。
2. **姿态字段** ✅：`session.mode` + 条件持久化 + fail-safe 词表（P0 已落）。聚焦对照：比 oc 的 agent 实体轻，比 dsh 的事件日志贴合本仓文件白名单。
3. **闸位与拒绝** ✅：`_decide` unknown 后、权限门之前；`error="mode_denied"`、文案自带出路；先于审批（P1 已落）。对照：与 oc 同层且更严（bash/subAgent 不放过）。
4. **工具目录稳定** ✅（显式记录）：不隐藏 schema、不动目录（dsh 缓存理由 + oc 的"被禁工具应可见可学习"）。
5. **白名单** ✅（已在 17 拍板 Q1=B）：read/glob/grep/todo_write/checkpoint_write/ask_user/skill/memory_search。对照发现：oc 连 bash 都放行——**我们比 oc 严**，这是有意的高底线。
6. **规划提示段** ✅：`build(mode=...)` 注入 `<plan-mode>` 段（P2）；文本保持简短（强制已硬，话术不必厚重——与 dsh 相反的原因）。**PLAN_PROMPT 质量增强**列为可选项 → **问卷 Q2**。
7. **决策面** ✏️ **发现 17 的真实漏洞**：`_verify_acceptance`（`agent_loop.py:547/731` 两处调用）→ `verify_items`（`core/acceptance.py:31-62`）**直接 `run_process_async(shell=True)` 跑验收命令，绕过 dispatcher 与模式闸**。plan 模式若携有历史验收契约，规划回合的收尾会执行任意 shell——只读承诺被击穿。**修订：`_verify_acceptance` 首行早退（plan 模式不验证验，契约保留原判直到正常回合）**+ 红测。其余维持：`decide_after_step` plan 早退 FINISH（POST 执行类门静默）、strategy/fatigue 照常（同 dsh：guard 不因 plan 停摆）。
8. **入口/出口** ✅：`/plan`（进模式）/`/plan <topic>`（进+规划轮）；`/plan off`；`/plan run`（退出+执行）。对照：与 dsh 的命令面几乎同形（`/plan <msg>`=我们的 `/plan <topic>`；`/plan off`同名），多出的 `/plan run` 由本仓执行引擎买单（oc/dsh 都没有一键执行，我们是加分项）。
9. **翻转时点** ✏️ **新增语义决策**：dsh 有 pending/queued（`set()` 可 queued 到下一个被接受的 pre-step）；本仓事实：`process_input` 持 `_turn_lock`（`agent_loop.py:324,408`），而**纯命令路径（新裸 `/plan`，不跑回合）不取锁** → 理论可在运行中回合旁路翻转 mode（该回合的下一迭代换 prompt/闸即生效；已构建的 ctx 批次保持旧快照=批次一致）。v1 处置：**即时生效，边界文档化**；pending 复杂度不买。→ **问卷 Q3**。
10. **F5 豁免** ✅：plan 模式不刷新裸 goal（主题归 `begin_plan`）。
11. **载荷/可见性** ✅（Q3=B 已拍）：`session.stats/load` 带 mode + 前端状态栏 `PLAN·只读`；进入/退出/就绪三处 notice（`console.info`）。
12. **subagent/步会话** ✅：新 `SessionData` 默认 normal（子循环闸关）；plan 下 `subAgent` 工具被 dispatch 拒（失关闭兜底）。对照 oc 的缺口（子代理不拦）：我们天然没有。
13. **砍掉项（重申）**：pending/queued；agent 实体；计划文件载体；exit_plan_mode 类评审 seam；提醒对账重放（我们的段每请求恒在，无需对账）；叙述去重（同上）。**全部有对照出处，非拍脑袋。**

## 4. 数据流（目标形态；★=本次新增）

```
[命令层] /plan [topic] | /plan off | /plan run          ← 唯一 mode 写入方（save）
   │  ★mode=plan ──▶ session.mode（条件持久化；崩溃/重启存活）
   │  /plan <topic> ──▶ plan_only：begin_plan(topic) → 规划轮(continuations 关)
   ▼
[回合装配] _begin_turn：★F5豁免（plan 下不夺 goal）
   │  每迭代：system = base+project+methods+★<plan-mode>+skills   (W: 线上可见)
   │          tail：env→memory→task→…（不变）
   ▼
[决策·前] decide_next_iteration：strategy/fatigue 照常（同 dsh：guard 不停摆）
   ▼
[工具批] tool_context(mode=session.mode) → dispatch._decide
   │   ★闸：plan ∧ mutates_world → _mode_denied（可见、先于审批） (W)
   │   todo_write 放行 → TaskUpdated → plan.updated（前端计划面板，零新增事件）
   ▼
[决策·后] decide_after_step：★plan 早退 FINISH（task/acceptance 门静默）
   │   验收：★_verify_acceptance 在 plan 下早退（17 漏洞，本稿修）——证明命令=shell 执行
   ▼
[回合末] settle 账本（与 mode 无关）
   ▼
[载荷] ★session.stats/load +mode → 前端 PLAN·只读                  (W)
   ▼
[执行] /plan run（若在 plan 先退）→ PlanRunner.run ── 步会话 mode=normal（闸关，零改动）
```

## 5. 影响面与逐文件步骤（P2–P5；P0/P1 已完成部分见 §7）

- **P2 提示词**（0 处新增文件）：`ai/prompt.py` 构造参删 `plan_mode`、`build(..., mode="normal")`；`agent_loop._build_instructions`（:1920）传 `session.mode`；迁移 `tests/test_prompt.py`（`plan_mode=` → `build(mode=)`）。normal 输出逐字节不变 → golden 零动。
- **P3 决策面**（+1 字段 +3 早退）：`TurnSnapshot.mode: str = "normal"`（`turn_policy.py` 末字段后）；`decide_after_step` 首行 plan 早退；**`_verify_acceptance`（`agent_loop.py:1334`）首行 plan 早退**（新）；两处 replace（`agent_loop.py:527-532,740-745`）+ `mode=`；F5 条件（`agent_loop.py:898`）加 `mode != "plan"`。`interventions.py` 零改。
- **P4 命令面**：`plan_runner.py` 拆 `plan_only(loop, session, *, topic, on_delta, observer)`（现 :126-145 原样），删 `plan_and_run`（调用点仅 commands.py:247 + 两个测试，全迁移）；`commands.py /plan` 分支按 `/plan|off|run|topic` 重写（进入/退出/就绪/拒因四类 notice），删 `_ask_topic`；`slash.py` 墓碑提示改指 `/plan run`；`handlers.py` IpcConsole 注释迁移。迁移表见 17 §4（test_plan_command/test_plan_workspace_gate/test_repl 4 例/test_plan_m1 2 例/test_slash）。
- **P5 载荷/前端**：`handlers.py` stats/load 各 +1 键；`frontend/src/routes/session.tsx` Stats +1 字段、`status-bar.tsx` 一段 `PLAN·只读`。
- **逐文件影响矩阵**：与 17 §6 相同（13 文件 + 前端 2），P3 行改为 4 处（+verify 早退）。

## 6. RIPR（红→实现→证明；✏️=本轮新增/修订）

| # | Red | Implement | Prove |
|---|---|---|---|
| R1 | mode 往返/未知值/无键/读回 | P0 ✅已落 | 5 绿 ✅ |
| R2 | 闸：拒绝先于审批/白名单/normal/失关闭/✏️**6 个真实 mutating 工具（write/edit/bash/subAgent/memory_write/mcp_call）逐一的 dispatch 级拒绝**（17 只测了假 "write"） | P1 ✅ + 补用例 | 全绿 |
| R3 | prompt：normal 逐字节旧样；plan 段序在 methods 后 | P2 | 绿 + golden 零动 |
| R4 | policy：plan+open_items→FINISH 无 nudge；strategy 照常；acceptance 门静默；✏️**plan+历史验收契约→`_verify_acceptance` 不跑 shell（红：现有代码会跑）** | P3 | 绿 |
| R5 | plan 下 goal 不刷新；normal F5 原样 | P3 | 绿 |
| R6 | 命令面：裸=进模式零模型调用 / topic=begin_plan+规划轮+continuations 还原 / off / run（先退再执行）/ 重复 topic=重规划 / plan_only 闸#1#2 | P4 | 全绿 |
| R7 | stats/load 带 mode | P5 | 绿 |
| R8 | ✏️子代理不继承：plan 下 subAgent 工具被拒（并入 R2）；子循环 ctx.mode=normal 断言 | P1/P3 | 绿 |
| R9 | 收口：全量+mypy+ruff+r7e/r8/r18+前端构建+r19 真机（进模式→试写被拒→写计划→run 执行） | 全部 | 绿+验收报告 |

## 7. P0/P1 现状核验（独立盘点预检——正式修复清单待拍板后行动）

**符合重推方案的部分**：P0（字段/序列化/词表）与 P1（闸位/文案/失关闭/白名单 8 类 + 6 类保持 True）与 §3 逐点吻合；`request_builder` 单构造点、子会话/步会话默认 normal（零改动即正确）、测试 5+6 绿、全量门禁绿（两次）。

**预检发现（按严重度）**：
1. ⚠️ **运行期半成品态**（预期内，非缺陷）：`session.mode` 尚无生产写入方；`_mode_denied` 文案指引 `/plan off|run` 但命令未实现（P4）；prompt/决策面/载荷未接线（P2/P3/P5）。→ 定稿后按 P2→P5 补齐即闭环。
2. ⚠️ **测试覆盖缺口 vs R2/R8**：6 个真实 mutating 工具没有 dispatch 级逐个用例；子代理断言未加。→ 修补项（§6 已列）。
3. ℹ️ **一处实现与 17 文稿差异**：文稿写 `getattr(session,"mode","normal")`，实落 `session.mode` 直取（类型化契约，鸭子桩已按先例补齐）。判断：保留直取（失败大声化优于静默默认）；已在 2 个测试桩处验证。
4. ℹ️ 字段位置在 `intervention_state` 后（文稿说 "git_branch 一带"）——纯文档漂移，代码合理。
5. ℹ️ `webfetch/websearch` 未列入显式拒绝名单，但因默认 True 在 plan 下被拒——与 Q1=B 一致（sticky note，无需动）。

## 8. 自核与最终模样

- **每新件有消费者**：mode（命令写/闸读/prompt读/快照读/载荷读，五消费）；mutates_world（闸）；`TurnSnapshot.mode`（POST 早退+verify 早退）；`plan_only`（规划半边）；`profile` 无——**没有**任何说不清作用的件；砍项 13 条都有对照出处（§3.13）。
- **推演核对（剧本）**：
  ```
  > /plan 给 dispatch 补文档
    [plan] 已进入计划模式（只读）…（状态栏 PLAN·只读）
    （规划轮：read/grep/memory_search 自由；write/bash 调→被拒，文案指引；todo_write 写 6 项含 verify）
    [plan] 计划就绪：6 项。可继续对话补充；/plan run 执行，/plan off 退出。
  > 把第 3 步拆细            （仅读工具+改计划；plan.updated 刷新）
  > /plan run                 （退出+PlanRunner：逐步执行、收据、证明）
  ```
- **中途细节对照检查**：为什么不改 interventions（抑制是姿态不是阶梯，且 dsh guard 同结论：不因姿态停摆）；为什么不隐藏 schema（dsh 缓存理由 + oc 例外证明隐藏不可靠）；为什么 verify 早退（本稿新洞：证明命令=shell）；为什么不要 pending（dsh 的复杂度对应事件日志体系，本仓文件白名单下即时生效+文档化边界更小）。

## 9. 问卷回执（2026-10-15）

- **Q1 = B**：拆分为两开关（plan 提示姿态 + 独立 readonly 强制轴）。
- **Q2 = B**：PLAN_PROMPT 增补 dsh 式两句（目标+成功标准 / 决策完备）。
- **Q3 = B**：mode 命令在回合运行中发出时 queued 到回合末。
- **Q4 = A**：P0/P1 保留 + 补缺口（具体补丁清单见 §10.4）。

## 10. 回执后设计修订（权威节；覆盖 §3.1/§3.8-3.9/§5 P3-P4/§6 相应行）

### 10.1 两开关架构（Q1=B）

- `session.mode: "normal" | "plan"` = **提示姿态**，唯一消费者 = P2 的 `<plan-mode>` 段（P0 已落，不改）。
- `session.readonly: bool = False` = **强制开关**，唯一消费者 = dispatch 闸。**新增**：字段 + 条件持久化（`"readonly": true` 才写键，同 mode 规则）+ from_dict 布尔兜底。
- 组合矩阵（四格全部定义，无未定义格）：

| mode | readonly | 语义 | 入口 |
|---|---|---|---|
| normal | off | 默认 | — |
| normal | on | **只读会话**（审计/探索，无规划引导）——拆分买到的全新能力 | `/readonly on` |
| plan | on | 安全规划（复合默认，主力路径） | `/plan [topic]` |
| plan | off | 引导式规划但可写（dsh 默认形态；本仓允许，不推荐） | 在 plan 中 `/readonly off` |

- 命令矩阵：
  - `/plan [topic]`：进入 = mode=plan **+ readonly=on**（复合）；带 topic 触发规划轮；已在 plan 再带 topic = 显式重规划（`begin_plan` 既有语义）。
  - `/plan off`：**双关**（normal + readonly off）——plan 会话整体退出；有 open items → notice 提示 `/plan run`。
  - `/plan run`：必要时双关后执行 `PlanRunner`（步会话默认 normal/非只读，零改动保持——执行不受姿态影响，天然正确）。
  - `/readonly on|off`：独立开关；在 normal 下 = 只读会话，在 plan 下 = 单独调强制轴。
  - 文档化边角：`/plan` 的复合进入覆盖 `/readonly` 既有值、退出双关——v1 不做 provenance（归属追踪）。
- **拒绝文案修订**：`_mode_denied` → `_readonly_denied`（`error="readonly_denied"`，`data.reason="read-only session"`），文案："只读已开启——该工具会改变工作区或状态，已拒绝。读/搜/写计划仍可用；关闭：/readonly off 或 /plan off；执行计划：/plan run。"

### 10.2 翻转时点：queued 到回合末（Q3=B）

- 事实（§3.9）：`process_input` 持 `_turn_lock`（`agent_loop.py:324,408`），纯命令路径不持锁。
- 规则：四个姿态命令（`/plan`、`/plan off`、`/plan run` 的模式变更部分、`/readonly`）在**回合运行中**发出 → 不立即写，落入 `session.pending_posture: dict[str, Any] | None`（**runtime 字段，不落盘**，同 dsh 的进程内 pending——限制照抄文档化）；命令层立即回执"（将在当前回合结束后生效）"（dsh 同款文案）。
- 应用点：`_close_turn` 内、`closer.close` **之前**应用（搭 close 的车落盘，零新增保存时机）+ `process_input` 入口兜底 drain（覆盖"排队后进程未崩但无 close 边界"的理论窗）。
- 形状：`{"mode": "plan"|None, "readonly": bool|None}`（None=不改该轴，同键后写覆盖）。
- topic 特例：回合中的 `/plan <topic>` **只排姿态不排 topic**，回执注明"回合结束后请直接描述计划主题（已可聊商划）"；理由：边界自动起新回合=引入 loop 自发回合机制（过大）；steering 语义（给运行中回合补话）不符。
- 边界文档化：pending 进程内丢失（dsh 同款）；崩溃后未应用姿态丢失、已应用姿态随文件存活。

### 10.3 PLAN_PROMPT 增补（Q2=B）

在 `plan_runner.py` 的 `PLAN_PROMPT` 追加两句（同文件英文风格）：

> "State each step's goal and its verifiable success criteria, and make the plan decision-complete: another engineer should be able to implement it without making further design decisions."

实施期核对该常量无测试 pin 原文；r19 计划场景不受影响。

### 10.4 对已落 P0/P1 的修订与补缺（Q4=A）

| 件 | 动作 |
|---|---|
| P0 `mode` 字段/词表/序列化 | **不动** |
| P0 **新增** `readonly` | 字段 + 条件写 + from_dict 布尔兜底 + 测试（往返/无键读回 false） |
| P1 闸输入 | `ctx.mode == "plan"` → **`ctx.readonly`**；`ToolContext.mode` 字段 → `ToolContext.readonly: bool = False`（ctx 只需要强制轴；提示轴由 P2 直读 session）；`request_builder` 传 `session.readonly` |
| P1 拒绝件 | `_mode_denied` → `_readonly_denied`（文案/错误码按 §10.1） |
| P1 测试 | `test_mode_gate.py` 改造：`_ctx(readonly=...)`；闸为**单轴**（mode 对闸不可见——`plan`+`readonly off` 放行属命令层组合测试，P4 覆盖）；闸层用例 = readonly 轴全谱 + **真实工具两例**（真 WriteTool 被拒且文件未落盘；真 ReadTool 放行且真读到磁盘） |
| P1 补缺（§7 预检） | R2：6 个真实 mutating 工具（write/edit/bash/subAgent/memory_write/mcp_call）逐一 dispatch 级拒绝；R8：子代理断言 |
| 测试桩 | `test_tool_context.py`/`test_request_builder.py` 双桩字段由 `mode` 改 `readonly`（跟随接口） |
| 运行期半成品态 | `_readonly_denied` 文案指引的 `/plan off|run|readonly` 由 P4 兑现（链路顺序使然，非缺陷） |

### 10.5 P2–P5 增量修订

- **P2**：不变（`build(mode=session.mode)`，mode 唯一消费者）。
- **P3**：`TurnSnapshot` 增**两**字段（`mode` + `readonly`）；`decide_after_step` 早退条件 = `mode == "plan" or readonly`（两轴任一为真时执行类门静默——只读会话里催执行同样荒谬）；**`_verify_acceptance` 早退条件 = `session.readonly`**（承诺只读即证明命令不可跑；plan+off 不承诺则不拦）；F5 豁免条件 = `mode != "plan"`（goal 主题属规划轴）。
- **P4**：命令矩阵按 §10.1；`/readonly` 入驻 `slash.py` 词表 + HELP_TEXT；queued 按 §10.2（pending 字段 + `_close_turn`/`process_input` 两处 drain + 回执文案）。
- **P5**：`session.stats/load` 带**两键**（`mode`、`readonly`）；前端状态栏按矩阵显示（`PLAN`/`只读`/`PLAN·只读`）。

### 10.6 RIPR 增量（在 §6 基础上追加/修订）

- **R1b**：readonly 往返/无键/false 读回。
- **R2′**：闸以 readonly 为输入 + 组合矩阵四格 + 真实工具逐一 + 失关闭 + 先于审批。
- **R4′**：plan 早退 ⇐ 两轴任一；verify 早退 ⇐ readonly（红：现有代码会跑 shell）。
- **R6′**：四命令矩阵；queued（运行中发 `/plan` → 零立即写入 + 回执；回合末应用 + 落盘；topic 特例回执）；`/plan-run` 墓碑指 `/plan run`。
- **R7′**：两键载荷。

### 10.7 自核（两轴穷举）

| 轴 | 消费者（穷举） |
|---|---|
| `mode` | P2 prompt 段；P3 POST 早退（∨）；F5 豁免 |
| `readonly` | P1 闸；P3 verify 早退；P3 POST 早退（∨） |
| 两轴合成 | P5 载荷/状态栏；P4 命令矩阵；queued 合并 |

无第三消费者、无孤儿字段、无未定义组合格；queued 的全部复杂度买在"运行中翻转对账"一处，且与 dsh 同款限制透明声明。
