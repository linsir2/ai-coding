# 18 · plan 模式的大改造：目标形态与改造顺序（v2）

> 本文是 **plan 模式的架构改造方案**，供拍板。机制取舍参考 opencode v2.0.22 与 deepseek-harness（`/home/linsir365/projects/opencode` @ `0a46301e3`、`/home/linsir365/projects/deepseek-harness` @ `57190202`）。
> 事实依据：§1 的源码级审计；相关文档 `15-实测解剖`、`16-W4b 路由修复`、`17-架构对比`。
> **v2 相对 v1**：按对抗性评审（见 §9）改了 8 条事实错误、12 条设计漏洞、补齐 4 项工程格式（RIPR 用例总表 / 提交切片 / 自核 / 看板）。**评审结论「批次 2/3/4 在形状层面需重写，不能按 v1 开工」已全部处理。**
> 状态：**待拍板**。已原型验证的批次 1 在 `wip/n1-research-exit` 分支，未进主干。

---

## 1. 审计：今天的 plan 模式是什么

### 1.1 一段话

**plan 模式今天不是「一个规划」，是两个正交布尔 + 一条一次性指令 + 一个事后追讨。**

- `session.mode == "plan"` 管**提示与出口**（`<plan-mode>` 段、守卫是否问）
- `session.readonly == True` 管**工具面**（schema 裁剪、执行期拒绝）
- 交付契约**三处分裂**：工具 schema（结构）/ `PLAN_PROMPT`（质量与过程）/ 运行时校验（可达性、序、非空）
- 收束靠**事后追讨**：模型要收尾时守卫问一次「交计划了吗」
- 批准 = 写 task + arm；**执行不由后端触发**，靠前端在 `turn.finished(completed)` 时静默派发 `/plan run`

### 1.2 一次 plan 回合的完整链路（现状）

```
用户 shift+tab / /plan
  ├─ 前端：Home 路由合成一条内部命令 session.command "/plan"（不进转录）
  └─ 后端：_handle_plan 裸分支 → request_posture(mode=plan, readonly=True) → 落盘 → 一句 notice
用户输入
  └─ session.prompt → run_human_turn → (mode==plan) → plan_only(topic=原话)
        ├─ 两道工作区门（topic / items）
        ├─ begin_plan(topic)              ← 换掉整个 task 账本
        ├─ add_message(原话)              ← 非 synthetic ⇒ 被当人话
        ├─ process_input("[plan] " + PLAN_PROMPT)  ← 一次性 user 消息，非 synthetic
        └─ policy: max_iterations=30, 无自动续跑
  ↓ 模型探索（read/grep/glob）
  ↓ 模型要收尾 → PlanSubmissionGuard.on_stop
        ├─ 没交过 → NUDGE（注入 synthetic 消息）→ 继续
        └─ 交过 → FINISH + note（只有前端读得到；CLI 无人读）
  ↓ 批准 → task.items 重写 + armed=True → 前端在 turn.finished 时 dispatch("/plan run")
  ↓ /plan run → PlanRunner → TaskDriver → 每步一个 fresh ephemeral step session
        └─ verify 命令（subprocess，绕过 dispatch gate）→ 机械裁决 → StepDelivery 收据
```

### 1.3 缺陷清单（审计结论；**v1 的 8 处事实错误已更正，标 ⟪改⟫**）

**A 组 · 交付与出口**

| # | 缺陷 | 证据 |
|---|---|---|
| A1 | **没有研究出口**：纯文本答完 = 违规 | `turn_policy.py:215-220`；`present_plan` 的 **13** 个返回分支（`present_plan_tool.py:134,139,155,165,175,181,201,222,229,241,284,289,295`）里没有一个是「研究完成」 ⟪v1 写 12⟫ |
| A2 | **契约三处分裂**（schema 说结构 / PLAN_PROMPT 说质量 / 运行时说可达性） | `plan_runner.py:78-98` vs `present_plan_tool.py:81-124` ⟪v1 说「自相矛盾」不成立：两处都有「machine-checkable」限定词，真差异是措辞强弱⟫ |
| A3 | **没有任何一处说 verify 必须是可跑的 shell** ⇒ 模型给「写报告」造 `test -f` / `glob '**/*'` | `present_plan_tool.py:104-109`（无格式校验）；`acceptance.py:86-89` 明写「proof commands bypass the dispatch gate」 |
| A4 | **守卫只挂一个裁决点**：四个 FINISH 口（时钟 `:260` / 迭代 `:296` / token `:297` / 阶梯 `:303`）绕过它 ⇒ 规划回合耗尽预算时无 nudge、无 note | `turn_policy.py:256-308` vs `:328-331` |
| A5 | 批准后**规划回合不收束**，模型继续探索、还试图 `todo_write{completed}` | `agent_loop.py:938-967` 只有 dismissed 分支 |

**B 组 · 姿态与指令**

| # | 缺陷 | 证据 |
|---|---|---|
| B1 | **姿态是字段不是事件**：无四态；`pending_posture` 不落盘 ⇒ 崩溃丢 | `agent_loop.py:454-490`（只返回 bool）；`domain/session.py:333-338` |
| B2 | **规划指令是一次性 user 消息** ⇒ 轮内被压缩挤掉后失忆 | `plan_runner.py:410` 注入一次；`<plan-mode>` 每请求重建但只描述姿态（`prompt.py:127-138`） |
| B3 | **规划指令非 synthetic** ⇒ 被三个后端读者当人话 | `agent_loop.py:1031`（对比 `:568/724/849/902/1136/1158` 全是 `synthetic=True`）⟪v1 写 :563⟫；三个读者：`_extract_user_input`（`request_assembly.py:485-498`，用户在主题里 @ 的文件整轮丢失）、`undo_last_turn`（`agent_loop.py:2056-2067`，把 1129 字指令塞回输入框）、压缩锚点（`agent_loop.py:1038-1046`） |
| B4 | **退出 plan 不撤销历史里的规划指令** ⇒ 旧指令持续施压 | `commands.py:453-469` 只翻两个轴 |
| B5 | **姿态黏盘 + 重启无提示** | `domain/session.py:567-571` 落盘；`handlers.py:349-357` 只在有 armed 任务时复位 |

**C 组 · 只读与执行**

| # | 缺陷 | 证据 |
|---|---|---|
| C1 | **只读与权限规则表不同源**：`mutates_world`+`ctx.readonly`（dispatch 特判）vs `permission_gate` 的三档表 | `dispatch.py:323-324` vs `permission_gate.py:402-468` |
| C2 ⟪v1 事实错误已改⟫ | **子代理今天**继承 `mutates_world=True`（`base.py:122`）⇒ **今天根本不在只读面**（`subagent_tool.py:337` 是 `subagent_list`，不是 `subAgent`）⇒ 「子代理逃逸只读」**今天不成立**，只在 M1a/M1b 的**软档**让 subAgent 可见之后才成立 | `tools/base.py:118-122`；`agent_loop.py:369-371`；`dispatch.py:323-324`；08 稿 D10 行的记载是对的 |
| C3 | **两轴拆开时提示成假话**：`<plan-mode>` 的断言绑 `mode`，工具裁剪绑 `readonly`；`/readonly off` ⇒ 提示说不可用、实际可用 | `prompt.py:129-137` vs `agent_loop.py:1806` / `dispatch.py:323`（**第三个谓词点** `agent_loop.py:2290-2299`） |
| C4 | **执行期 bash 恒 ask** ⇒ 「批准即自动执行」只在 dispatch 层成立 | `permission_gate.py:469,479-490,278` |
| C5 ⟪v1 后果已改⟫ | `todo_write` 在计划模式仍 ALLOW（`mutates_world=False`，add 不拦；completed 在 readonly 下**已拦**）⇒ 预写条目**不会**污染批准时的清单（批准是整表替换，`present_plan_tool.py:264`），但会让实时计划卡显示非计划项 | `todo_write_tool.py:25,97-98,104-115` |

**D 组 · 生命周期与 UX**

| # | 缺陷 | 证据 |
|---|---|---|
| D1 | **`/plan off` 不撤销 arm** ⇒ 文档承诺的「换一份计划」是死路（`plan_only` 与 `present_plan` 双重拒绝） | `commands.py:453-469`；`plan_runner.py:385-392`；`present_plan_tool.py:154-162` |
| D2 | **已批准的计划在非 clean 终态被静默撤销**（后端**明确撤销** arm，前端**主动不派发**；真缺陷是「静默」+ CLI 无人读 `outcome.note`） | `agent_loop.py:1440-1447` + `util/plan-run.ts:17` ⟪v1 说「前后端一致地什么都不做」不准确⟫ |
| D3 | `begin_plan` 不发 `TaskUpdated` ⇒ 整轮规划期间计划卡显示**上一轮**的旧条目 | `domain/session.py:375-389`（`SessionData` 无 emit 通道 ⇒ 落点必须是 `plan_runner._emit_task`） |
| D4 | **评审卡期间输入框被整块卸载**（不是禁用）+ 会话级键位全关 | `session.tsx:656-660,271` |
| D5 | 实时计划卡**不显示 verify / depends_on** | `server/bridge.py:159-163`；`state/session.ts:64-69` |
| D6 | `plan.receipt` 把 harness 写的步骤提示当**用户发言**画进转录 | `state/session.ts:542-563` + `plan_runner.py:1055-1057` |
| D7 | `planPrelude` 的 `/plan` 命令终局事件与 `submit` 的 busy=true 竞态 | 决定性行在 `state/session.ts:715-730`（唯一置 false）vs `:808`（置 true）；命令侧事件源 `handlers.py:499-513` |
| D8 ⟪v1 无落点⟫ | 步骤会话除共享 task + 一条 seed 外不继承任何编排上下文 | `plan_runner.py:826-890` |

**E 组 · 已知但未收进清单的（15 号稿）**

| # | 缺陷 | 证据 |
|---|---|---|
| E1 | Home 路由的 shift+tab **对用户完全隐形**（用户不知道自己进了 plan 模式） | `submission.ts:104-116`；`home.tsx:87-99` |
| E4 | `outcome.note` **在 CLI 是哑的** | 只有 `bridge.py:189` 的前端读 |
| E6 | **CLI 不自动执行**批准后的计划 | `PlanRunner.run` 只有 `commands.py:503` / `handlers.py:612` 两个调用点 |

> **E4/E6 直接使批次 1、3 的验收在 CLI 上失效** ⇒ 验收要么限定在 Server/TUI，要么把 E4/E6 也纳入批次。

---

## 2. 目标形态

### 2.1 一句话

**「计划」是一份契约在工具 schema 里（不在 prompt 里）的可执行结构；规划期只能探索不能工作；研究型请求有一条合法出路 —— 直接给结论，不必交计划；姿态是事件不是字段，指令的形状沿用 08 T3 已定的那一种。**

### 2.2 三个部件（v1 的三处漏洞已修）

```
┌─ PlanStance ─ 姿态：事件 + 投影，不新增第三份权威源 ─────────────┐
│  权威源：M1b 之前 = session.mode；M1b 之后 = session.agent（派生）  │
│  切换：request_posture 返回四态（dsh 的形状）：                  │
│        committed / queued / noop / cancelled                     │
│  pending：复用 session.pending_posture（★不新建字段）             │
│           落盘：改成 durable（★v1 说「另一个 accepted pre-step」不成立，│
│           本仓的边界是回合收尾 + 回合入口，agent_loop.py:492-507/597）│
│  崩溃：pending 落盘 ⇒ 不丢                                       │
│  指令：★形状沿用 08 T3 已定的那一种（durable synthetic + 请求边界  │
│        投递 + save 边界 drain + 每请求对账 + kind/text 去重），      │
│        **不发明第四种**（v1 的「每请求投影」与 08 T3 冲突）        │
│  退出：显式撤销历史里的规划指令（oc 的 leave 提醒）               │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─ PlanDelivery ─ 交付契约：两条合法出路，格式只在 schema 里 ──────┐
│  ① present_plan(items[])     可执行：批准后 runner 跑 + 验收 + 收据 │
│     schema 说清：每步是「还没做的事」、verify 是「可跑的 shell」    │
│  ② finish_research(summary)  不可执行：直接给结论，不 arm、不执行   │
│                                                                  │
│  互斥：**占位 = 成功**（present_plan 的成功 = 人批准；            │
│        finish_research 的成功 = 工具返回）                        │
│        ⇒ 人 dismiss 计划**不**封死 finish_research                │
│        ⇒ finish_research 先成功 ⇒ 同批 present_plan 被拒（不 arm） │
│  prompt 只说「要交一个出口」，不说格式（格式在 schema）             │
│  守卫：approve || research_finished ⇒ 放行                        │
│        ★挂到所有 FINISH 口，但**三条硬约束**（见 §3 批次 1）        │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─ PlanBudget ─ 探索有上界（v1 只画了框，没有批次） ──────────────┐
│  规划回合有独立的**探索预算**（探索轮数），与回合上限分开          │
│  两条互斥的触发点：                                               │
│   · 探索预算将尽 → **提前 nudge**「现在交计划」（在活干完之前）    │
│   · 回合预算耗尽 → **直接 FINISH + note**（不再 nudge）            │
└──────────────────────────────────────────────────────────────────┘
```

### 2.3 目标数据流

```
进入 Plan（shift+tab / /plan）
  → PlanStance.set(plan) ⇒ 四态
      空闲 ⇒ committed（落盘 + 投影更新）
      回合中 ⇒ queued（durable pending，回合边界落盘）
  → 状态栏立刻反映（前端订阅姿态事件，不再等 turn.finished）
  → ★E1：进入 plan 模式要有可见提示（用户现在完全不知道）

每一轮请求装配
  → 规划轮：主题作为人类的话（非 synthetic），plan 指令作为 harness 的话（synthetic）
  → 工具面 = 只读（由权限规则表达，与执行期同源）

规划回合
  → 出口 ① present_plan(items[]) → 评审卡 → 批准 → arm → 排队执行 → 收据
  → 出口 ② finish_research(summary) → 直接给结论 → 回合结束
  → 都不出 → 探索预算将尽时提前 nudge → 仍未出 → 结束（带 note）
  → 批准后规划回合**立刻收束**（不再继续探索）

执行期
  → step session：mode=normal, readonly=False, 全量工具（现状保留）
  → verify 机械验收 + StepDelivery 收据（现状保留）
  → ★C4：只有「由已批准计划启动的 step session」里的 bash 免审（不是把 bash 整体降档）
```

### 2.4 与「不可动的边界」的关系

| 边界 | 目标形态下 |
|---|---|
| 人批准才执行 | **保留** —— oc/dsh 的「聊天即计划」不抄 |
| 可执行 + 机械验收 + 收据 | **保留** —— 我们比它们强的地方。**verify 保持必填**（用户已拍板），只加格式校验 |
| 只读闸 | 保留，但从 dispatch 特判改为权限规则；**软/硬档沿用 08 D-03**（先硬档 True，评测后翻软档，UI 显示两档） |
| 执行层契约（`08` §8.3） | 一条不动 |

---

## 3. 改造批次

> 每批：治哪些缺陷 → 改哪些文件（**一一映射**）→ 每文件做什么 → RIPR 用例 → 验收 → 依赖。
> **批次间互不依赖**（除标注）。

### 批次 1 · 出口与契约归位（不碰架构）

**治**：A1 A2 A3 A4 A5 C5 D3 E1 E4 E6

| 文件 | 做什么 |
|---|---|
| `ai_coding/tools/finish_research_tool.py`（新） | 第二出口。schema `{summary: str}` 必填；只在 plan 模式可用（与 `present_plan` 同一道 mode 门）；成功 ⇒ 回合终局（不写 task、不 arm、不弹卡）；结论落在 tool result（用户看得到） |
| `ai_coding/tools/base.py` | `TurnConclusion`（回合级持有者）：「这一回合的出口已经定了」——两个出口互斥的**唯一**事实源。★不是挂在 task 上 |
| `ai_coding/core/request_builder.py` | `tool_context(...)` 透传 `conclusion` |
| `ai_coding/tools/present_plan_tool.py` | ① 人**批准**时占位（dismiss **不**占位）② 批准前若 `TurnConclusion` 已定 ⇒ 返回 `already_concluded` ③ schema 补齐格式语义：「每步是还没做的事」「verify 是可跑的 shell 命令」 |
| `ai_coding/tools/builder.py` | 注册 `FinishResearchTool`（与 `present_plan` 同条件）；★build 模式的 schema 剪除要 M1a 的规则语言 ⇒ **本批先只在 plan 模式注册**（见下「已知缺口」） |
| `ai_coding/core/turn_books.py` | ① 记 `research_finished` ② `TurnSnapshot` 增 `plan_approved`（现在 `turn_books.py:406-425` 的 snapshot 里没有它，守卫看不到） |
| `ai_coding/core/turn_policy.py` | ① 守卫判据：`approve \|\| research_finished` ⇒ 放行 ② `PLAN_NUDGE` 改成「给一个出口」（不说格式）③ ★**守卫挂到所有 FINISH 口的三条硬约束**：<br>(i) 该路径**必须一次性记账**（`plan_nudged`）——否则预算条件恒真 ⇒ 每轮 nudge ⇒ **无限自旋**（`plan_nudged` 今天只在 `decide_after_step` 路径记账，`agent_loop.py:895-898`）<br>(ii) **时钟/token 口的 nudge 无效**（时钟已过期）⇒ 直接 FINISH + note<br>(iv) 迭代/阶梯口：nudge 一次（记账）⇒ 下一次 FINISH + note |
| `ai_coding/core/agent_loop.py` | ① 出口 ② 成功 ⇒ 回合在批次末结束（与 dismiss 分支同形）② **A5：present_plan 批准后规划回合立刻收束**（新增 approved 分支，与 dismiss 同形）③ NUDGE 在 `decide_next_iteration` 路径上也记账 |
| `ai_coding/core/plan_runner.py` | `PLAN_PROMPT` 瘦身：删掉 items/verify/pending 的格式描述（搬进 schema），只留「要交一个出口」+ 主题 + decision-complete 的语义 |
| `ai_coding/ai/prompt.py` | `<plan-mode>` 段：加第二出口；删掉与 schema 重复的格式描述；★**断言绑 `mode`、裁剪绑 `readonly` 的不一致**在此显式声明（软/硬档见 §3 批次 3） |
| `ai_coding/security/permission_gate.py` | 白名单加 `finish_research`（不 ask 谁） |
| `ai_coding/core/acceptance.py` + `ai_coding/shell_parse.py` | **A3 的真修法**：提交时校验 verify 是可跑的 shell（用已有的 `shell_parse.parses_cleanly`）——schema description 治不了 |
| `ai_coding/app/commands.py` | **E1**：进入 plan 模式给一句**明确**的可见提示（现在 Home 路由完全隐形） |
| `ai_coding/app/commands.py`（`/plan run`） | **E6**：CLI 路径也自动执行（`commands.py:487-530` 现在只处理用户手打） |
| `ai_coding/core/turn_report.py` / `app/commands.py` | **E4**：CLI 侧把 `outcome.note` 打印出来（现在全哑） |
| `frontend/src/util/toolmeta.ts` | 工具名展示（原型已改） |

**RIPR 用例**（每条：Red → Implement → Prove → Regression）

| # | 用例 | 文件 | 成功后 |
|---|---|---|---|
| 1-1 | 研究型请求在 plan 模式下走 `finish_research`，回合结束、**无 plan、无 arm、无评审卡** | `tests/test_finish_research_turn.py`（原型已有） | 模型给结论即止 |
| 1-2 | 两个出口互斥：先 `finish_research` 成功 ⇒ 同批 `present_plan` 返回 `already_concluded` 且**不 arm** | 同上 | 不会出现「note 说没计划、实际已 arm」 |
| 1-3 | 人 dismiss 计划 **不**封死 `finish_research` | 同上 | dismiss 语义 = 「不算提交」，不是「封死另一个出口」 |
| 1-4 | `present_plan` 批准后规划回合**立刻结束**（A5） | `tests/test_plan_mode_turn.py` | 批准后不再继续探索 |
| 1-5 | 规划回合耗尽迭代预算 ⇒ **有 note**（A4） | `tests/test_plan_guard.py` | 不再静默 |
| 1-6 | 守卫在迭代口 nudge **只发生一次**（记账）⇒ 不会无限自旋 | 同上 | 回合必然终止 |
| 1-7 | 时钟/token 预算耗尽 ⇒ **直接 FINISH + note**（不 nudge） | 同上 | 不自旋 |
| 1-8 | verify 不是可跑 shell（`glob '**/*'`）⇒ 提交被拒并说清原因（A3） | `tests/test_present_plan_tool.py` | 模型不再造假判据 |
| 1-9 | verify 仍是**必填**（不降级 08 §8.3） | 同上 | 机械验收契约不动 |
| 1-10 | `finish_research` 在非 plan 模式不可用 | `tests/test_finish_research_tool.py` | 只在 plan 模式出现 |
| 1-11 | 进入 plan 模式有可见提示（E1） | `tests/test_plan_command.py` | 用户知道自己进了 plan |
| 1-12 | CLI 路径：批准后自动执行（E6）+ note 可见（E4） | `tests/test_plan_command.py` | CLI 与 TUI 行为一致 |

**验收**：研究型请求 → 结论即止（无假计划、无 NUDGE 循环、无工作区门拒、无梯子胡说）；实现型请求 → 仍走 `present_plan`，批准后执行出收据；规划回合耗尽预算有 note；verify 是可跑 shell。

**已知缺口（本批明确不做）**：build 模式下 `finish_research` 的 **schema 剪除**要 M1a 的规则语言 ⇒ 本批只在 plan 模式注册它 ⇒ **08 T4 只完成一半**，记为债。

**原型状态**：`wip/n1-research-exit` 已实现并真机验证（研究型 → `finish_research=1 / present_plan=0 / armed=False`；实现型 → 仍走 present_plan + 1 张收据）。**待并入主干（需评审）**。

### 批次 1b · 探索预算（独立，可与批次 1 合并）

**治**：A4 的一部分 + G8

| 文件 | 做什么 |
|---|---|
| `ai_coding/core/plan_runner.py` | 规划回合加**独立的探索预算**（探索轮数，与回合上限分开） |
| `ai_coding/core/turn_policy.py` | 探索预算将尽 ⇒ **提前 nudge**「现在交计划」（与守卫的 nudge 是**同一套记账**，不重复） |
| `ai_coding/core/turn_books.py` | 记探索预算的消耗 |

**验收**：规划回合的探索轮数有独立上界；真机上模型在预算内交计划（而不是干完活才交）。

### 批次 2 · 姿态与指令归位（依赖批次 1；**M1b 之后**才完整）

**治**：B1 B2 B3 B4 B5 D3

| 文件 | 做什么 |
|---|---|
| `ai_coding/core/agent_loop.py` | ① `request_posture` 返回**四态**（committed/queued/noop/cancelled）② 队列落地时机不变（回合收尾 + 回合入口） |
| `ai_coding/domain/session.py` | ① `pending_posture` **落盘**（durable）——★**复用现有字段，不新建 `pending_plan_stance`** ② ★迁移期双写纪律（08 §8.4）：M1b 之前 `mode` 是权威源，M1b 之后 `mode` 派生自 `agent`，**同一 save 内完成** |
| `ai_coding/core/plan_runner.py` | plan 指令改为 **synthetic**（补做 16 号稿设计了但没落地的 `process_input(synthetic=)`）——修 B3 的三个读者 |
| `ai_coding/core/request_assembly.py` | plan 指令的**形状沿用 08 T3**（durable synthetic + 请求边界投递 + save 边界 drain + 每请求对账 + kind/text 去重）⇒ ★**B2/B4 归 08 T3（M2），本批不另发明** |
| `ai_coding/app/commands.py` | `/plan off` ⇒ 发撤销事件（历史里的 plan 指令作废） |
| `ai_coding/server/bridge.py` | 姿态事件上 wire（★转换在 `bridge.py` 的 `to_wire`，不在 `handlers.py`） |
| `ai_coding/core/plan_runner.py` | `begin_plan` 后发 `TaskUpdated`（★落点是 `_emit_task`，`SessionData` 没有 emit 通道） |

**RIPR 用例**：1-2 之外的关键三条

| # | 用例 | 成功后 |
|---|---|---|
| 2-1 | `/undo` 摘掉的是**人类的话**，不是 1129 字指令 | 转录里人话归位 |
| 2-2 | 崩溃后排队中的姿态切换不丢 | pending 落盘生效 |
| 2-3 | 切出 plan 后历史里的 plan 指令被显式撤销 | 旧指令不再施压 |
| 2-4 | ★08 §8.4 的四项迁移测试（旧 `mode=plan,readonly=true` 加载 / 旧 `mode=normal` / 无 `agent` 字段的消息 / 坏 agent id） | 迁移期不炸 |

### 批次 3 · 只读与执行同源（依赖 **M1a** + **M1b**）

**治**：C1 C2 C3 C4

| 文件 | 做什么 |
|---|---|
| `ai_coding/security/permissions.py`（M1a 新建） | 规则语言（`{action,resource,effect}` + findLast + 通配 + 默认 ask）；★**base 规则层是叠加不是替换**（否则 read/glob/grep 无匹配 ⇒ 全落 ask ⇒ plan 不可用） |
| `ai_coding/domain/agents.py`（M1b） | plan 的规则集作为 agent 的一层；★**软/硬档沿用 08 D-03**（先硬档，评测后翻软档；UI 显示两档） |
| `ai_coding/core/agent_loop.py` | ★**四个谓词点统一到一个求值器**：schema 裁剪 `:369-371`、prompt 工具表 `:2290-2299`、dispatch 拒绝 `dispatch.py:323-324`、gate 三档 `permission_gate.py`；★规则 allow **不能降级** gate 的硬拒（bash 红线、配置 deny） |
| `ai_coding/core/dispatch.py` | ★**C4 的正确落点**：不是 verify（verify 是 subprocess，绕过 gate，`acceptance.py:86-89`），而是**步骤会话里模型的 bash 调用** ⇒ 判据 = 「这是一个由已批准计划启动的 step session」⇒ 只在**该上下文**里跳过审批，普通回合/规划回合/子代理的 bash 仍逐条问；★`acceptance.py:97-100` 的「工作区外路径要人批」**不被免掉** |
| `ai_coding/core/request_builder.py` | `ToolContext` 加 `plan_step` 标记（step session 为真）——C4 的信号载体 |
| `ai_coding/core/subagent.py` | ★**C2 改写**：按 08 T14 的形状 —— 子会话继承父的 **session 级规则**并与目标 agent 规则**求交**（不是传 `readonly=True` 布尔，那会让子代理完全没有 write/edit/bash）；★**只在软档让 subAgent 可见后才需要**（今天它不在只读面） |

**RIPR 用例**

| # | 用例 | 成功后 |
|---|---|---|
| 3-1 | 对每个工具，**schema 可见性 == dispatch 会不会直接拒**（同一求值器） | 两处不再分叉 |
| 3-2 | base 规则叠加后 read/glob/grep 仍免审批 | plan 模式可用 |
| 3-3 | 规则 allow 不能降级 bash 红线与配置 deny | 安全边界不后退 |
| 3-4 | 软档下 plan 可用 subAgent；子代理**拿不到超出父规则的工具** | C2 成立且可测 |
| 3-5 | `/readonly off` 时提示与实际一致 | C3 不再是假话 |
| 3-6 | 已批准计划的 step session 里 bash 不再逐条问；**普通回合的 bash 仍然问** | C4 兑现且不开后门 |
| 3-7 | verify 含工作区外路径 ⇒ 仍然要人批 | `acceptance.py:97-100` 不被免掉 |

### 批次 4 · 生命周期与 UX（独立）

**治**：D1 D2 D4 D5 D6 D7

| 文件 | 做什么 |
|---|---|
| `ai_coding/app/commands.py` | **D1**：`/plan off` 只在「已批准但**未开始执行**」时撤销 arm；★**正在执行时不动**（否则杀掉正在跑的 run）；撤销要有可见说明（`TaskUpdated` + 一句 notice） |
| `ai_coding/core/agent_loop.py` | **D2**：批准后被非 clean 终态撤销时留下**可见痕迹**（note + 事件） |
| `frontend/src/routes/session.tsx` | **D4**：评审卡期间输入框**收起而非整块卸载**，会话级键位不全关；姿态变化立即反映状态栏 |
| `frontend/src/state/session.ts` | **D5**：`plan.updated` 带上 `verify` / `dependsOn`；**D6**：`plan.receipt` 的 harness 提示改到 assistant 侧 |
| `frontend/src/util/submission.ts` | **D7**：消除 `planPrelude` 的 busy 竞态（命令终局事件不再与随后的提交抢 busy） |
| `ai_coding/core/plan_runner.py` | **D8 从本批删掉**（字段不存在 ⇒ 要新增并贯通 task 模型 + schema + 卡片 ⇒ 登记为 M3/M4 的 UX 债，不在本批） |

**验收**：`/plan off` 能换一份计划且不杀正在跑的 run；批准后被撤销有可见说明；评审卡期间能排队输入；计划卡显示 verify；批准后被撤销时用户看得见。

---

## 4. 顺序与依赖

```
批次 1（出口与契约）    ← 零依赖，先做
批次 1b（探索预算）      ← 零依赖，可与 1 合并
M1a（权限引擎）         ← 批次 3 的前置（14 号稿，方案+原型已就绪）
M1b（agent 承载）       ← 批次 2/3 的形态载体
批次 2（姿态与指令）    ← 依赖批次 1 + M1b
批次 3（只读与执行）    ← 依赖 M1a + M1b
批次 4（生命周期与 UX） ← 独立，随时可插
```

★ **v1 说「批次 1/4 可插在 M1a/M1b 之前」不完整**：批次 2 依赖 M1b（agent 才是权威源），批次 3 依赖 M1a+M1b ⇒ **只有批次 1、1b、4 可以前置**。

---

## 5. 与既有设计文档的关系

| 08 稿条目 | 在本方案里 |
|---|---|
| T4 `finish_research` | 批次 1 ①（**已原型，但只完成一半**：schema 剪除要 M1a） |
| T12 PLAN_PROMPT v2 | 批次 1（格式归位）。★**「每步有 verify」保持**（用户拍板：必填 + 格式校验） |
| T3 提醒对账 | 批次 2 的形状依据（**不另发明**）⇒ 主体仍属 M2 |
| T10 durable pending | 批次 2（`pending_posture` 落盘）★**与批次 2 合并，不做两遍** |
| T1 agent 承载 | M1b（批次 2/3 的形态载体） |
| T2 权限规则引擎 | M1a（批次 3 的前置） |
| T5 计划目录 / T9 `.env` ask | M3 |
| T8 隐藏 todo | M3 ★**与批次 1 的 C5 不撞车**：批次 1 只修「不要用 todo 记录计划」的提示与 add 的拦截口径；整剪除留给 T8（规则层） |
| T6 软硬只读 | M2；批次 3 落地时按 08 D-03 先硬档后软档 |
| T11 评审三态 | ★**已由 W1 落地**（R04/R08），不是新工作（v1 的映射有误） |
| T14 子代理权限 seed | 批次 3（C2） |

---

## 6. 非目标

| # | 不做 | 理由 |
|---|---|---|
| 1 | 抄 oc 的「聊天即计划」 | 把执行授权从人手里拿走 |
| 2 | 抄 dsh 的「plan 什么都不管」 | 我们面向无人值守场景，必须有只读闸与预算 |
| 3 | 改执行层的机械验收语义 | 08 §8.3 保留契约；verify 保持必填（用户拍板） |
| 4 | 动 `mode` / `readonly` 两个字段的对外形状 | 批次 2/3 内部消化 |
| 5 | 前端重做 | 只做 D4/D5/D6 三处状态修正 |
| 6 | D8（步骤的完成标准字段） | 字段不存在 ⇒ 要新增并贯通三处 ⇒ 登记为 M3/M4 的 UX 债 |
| 7 | 一次做完四批 | 批次 1 就能消掉你这次遇到的整条失败链 |

---

## 7. 已拍板

| # | 决定 |
|---|---|
| 1 | **verify 保持必填** + 提交时校验它是可跑的 shell（用 `shell_parse.parses_cleanly`） |
| 2 | **出口②防滥用用关键词/意图粗判定**（用户拒绝了「进 plan 时显式标类型」与「不防滥用」） |

> ★ 第 2 条的**具体口径待定**：在哪个位置做这个判定（`finish_research` 工具内？还是 prompt 层给模型一个判据？），以及判定词表是什么。**这是实施前必须定的最后一块**，其余已无阻塞。

---

## 8. 提交切片

| # | 提交 | 内容 |
|---|---|---|
| 1 | `feat(plan): the research exit — a plan-mode turn can end with a conclusion` | 原型并入（`wip/n1-research-exit`） |
| 2 | `fix(plan): an approved plan ends the planning round` | A5 收束 |
| 3 | `fix(plan): the guard answers on every way a turn can end` | A4 三条硬约束 + 记账 |
| 4 | `fix(plan): a proof command has to be a shell command` | A3 格式校验 |
| 5 | `feat(plan): the planning budget and the early nudge` | 批次 1b |
| 6 | `feat(plan): the posture has four states and survives a crash` | B1 |
| 7 | `feat(plan): the harness's instruction is not the operator's speech` | B3（含 16 号稿没落地的 `process_input(synthetic=)`） |
| 8 | `feat(security): the rules decide what the catalogue shows` | M1a 落地（14 号稿） |
| 9 | `feat(plan): the only way out is an approved plan` | 批次 3 的 C4 + 只读合并 |

---

## 9. 评审的 12 条如何处理（v1 → v2）

| # | 评审指出 | v2 的处理 |
|---|---|---|
| 1 | C2 事实错误 | ✅ 已改（`subAgent` 继承 `mutates_world=True`，今天不在只读面；C2 改为「软档之后才成立」，改用 08 T14 的形状） |
| 2 | 批次 1 落点不全 | ✅ 补 `tools/builder.py`、`core/request_builder.py`、`turn_books.py` 的 snapshot、A5 落点、C5 落点 |
| 3 | 守卫挂 FINISH 口会无限自旋 | ✅ 三条硬约束写进 §3 批次 1（一次性记账 / 时钟 token 口直接 FINISH+note / nudge 注入什么） |
| 4 | plan 指令形状自相矛盾 | ✅ **不再发明第四种**：沿用 08 T3 已定的形状，B2/B4 明确归 M2 |
| 5 | 出口互斥被人门阻塞打穿 | ✅ **占位 = 成功**（dismiss 不占位）；三条畸形各有归属 |
| 6 | C4 落点错 + 边界丢失 | ✅ 落点改为「step session 的 bash 调用」；★把 15 F5 的边界原样写进去；`acceptance.py:97-100` 不被免掉 |
| 7 | 只读合并后的单一谓词未定义 | ✅ 四个谓词点统一；★base 规则层是叠加不是替换；软硬档沿用 08 D-03 |
| 8 | PlanBudget 无批次 | ✅ 批次 1b |
| 9 | 第三份权威源 / 双份 pending | ✅ 删掉 `pending_plan_stance`，复用 `pending_posture`；M1b 前 mode 是权威源，之后 agent 是，双写同一 save |
| 10 | D1 语义未定 | ✅ 只在「已批准未开始执行」时撤销；正在执行不动；有可见说明 |
| 11 | D8 字段不存在 | ✅ 从本批删掉，登记为债 |
| 12 | 缺工程格式 | ✅ 补 RIPR 用例总表、提交切片、自核、看板；H1–H8（无定义）从「治」里删掉 |

**另修的 8 处事实错误**：A1（13 个分支）、A2（不是自相矛盾）、C5（批准是整表替换）、D2（后端明确撤销 arm）、B3（`:568`）、D7（决定性行在 state/session.ts）、§5 的 T11 映射、批次 1/3 的落点写错文件（bridge.py / plan_runner._emit_task）。

---

## 10. 待拍板（最后一块）

**出口②的防滥用判定放在哪**：

| | 做法 | 代价 |
|---|---|---|
| (a) | **工具内判定**：`finish_research` 执行时检查「用户这条消息里没有改/实现/加的意图词」⇒ 有则拒 | 判定粗糙（同义/反讽会误判）；但判定发生在**执行期**（不可绕） |
| (b) | **prompt 层给模型判据**：`<plan-mode>` 里写「只有当请求是纯探索/评估时才用 finish_research」 | 模型可能不遵守；但零代码、零误杀 |
| (c) | 两者都做：(b) 给模型判据 + (a) 兜底 | 最稳；词表要维护 |

**建议 (c)**：(b) 为主（大部分情况模型自己能判），(a) 兜底（词表小、只挡明显的实现类动词）。词表与判据的落点（`finish_research_tool.py` 内 + `prompt.py` 的 `<plan-mode>` 段）已清楚。
