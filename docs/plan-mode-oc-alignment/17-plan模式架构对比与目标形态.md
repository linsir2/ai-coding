# 17 · plan 模式的架构对比（oc / dsh / 我们）与目标形态

> 本文回答两个问题：**我们的 plan 模式为什么「全是歪的」**，以及**目标形态具体长什么样**。
> 证据：opencode v2.0.22（`/home/linsir365/projects/opencode` @ `0a46301e3`）、deepseek-harness（`/home/linsir365/projects/deepseek-harness` @ `57190202`）、本仓 `feat/orchestration-cognition` @ `1fd6de9`。
> 相关：`15-plan模式实测解剖.md`（两次实测）、`16-第六批实施计划（W4b-计划模式归位）.md`（路由修复）。

---

## 1. 一句话结论

**oc 与 dsh 的 plan 都没有「交付物契约」，所以都没有「未交卷」这个失败态，所以都不会产生我们那种「先干完活再补交一份带假 verify 的计划」。**

我们的畸形是**三个东西相乘**：

```
守卫强制提交（turn_policy.py:206-220）
  ×  契约在 prompt 里规定了 items+verify（plan_runner.py:78-98）
  ×  研究类请求的「干活」挡不住（read/grep/glob 的 mutates_world=False）
= 「干完活 → 被追讨 → 补交已做过的事 → verify 造假」
```

**缺的不是更强的措辞，是缺一个「研究出口」和一个「计划先于工作」的结构性约束。**

---

## 2. 逐项对比

### 2.1 形态：plan 是什么

| | **oc** | **dsh** | **我们** |
|---|---|---|---|
| 形态 | 一个 **primary agent**（`plugin/plan.ts:33-43`，共 121 行，全仓唯一的 plan 逻辑） | 一个 **system prompt 段**（`plan:policy`，order 500）+ 一个常驻工具 | 一个 **模式标志位** `session.mode` + 一个 `<plan-mode>` 提示段 |
| 交付物 | **模型的最后一条 assistant 消息**（无格式契约） | **一段 markdown**（`exit_plan_mode({plan})`，只有一个参数） | **`present_plan` 的 `items[]`**（结构化 + verify 命令） |
| 交卷动作 | **不存在** | 调 `exit_plan_mode` | 调 `present_plan` |
| 守卫 | **不存在**（`grep exit_plan_mode` 全仓 0 命中） | **不存在**（README:184「Guidance, not enforcement」） | **`PlanSubmissionGuard`**（nudge 一次后 FINISH） |
| 「未交卷」这个状态 | **不存在** | **不存在** | **存在** ⇒ 畸形可能 |
| 计划能不能被执行 | 不能（模型自己读回散文执行） | 不能（模型自己读回散文执行） | **能**（runner 按 items 执行 + 机械验收 + 收据） |

> **oc 与 dsh 的 plan 全部逻辑不到 200 行，且都没有守卫。** 我们的 plan 有 600+ 行（`plan_runner.py` 1178 行 + `present_plan_tool.py` + 守卫 + 验收 + 收据），因为我们的 plan 是**可执行的**。

### 2.2 「只读」怎么实现

| | oc | dsh | 我们 |
|---|---|---|---|
| 机制 | **声明式权限规则**：`{action:"edit",resource:"*",effect:"deny"}`（`plan.ts:39`） | **什么都不做**（README:184「Guidance, not enforcement」；设计稿 `2026-07-07-plan-mode.md:197` 明说「a model that ignores the section CAN mutate during plan」） | **dispatch 层的特判**：`ctx.readonly and tool.mutates_world`（`dispatch.py:323-324`） |
| 工具表 | **不裁剪**（edit 留在表里，调用被拒 + 错误被改写成可读文案，`plan.ts:45-54`） | **不裁剪**（测试显式断言 plan 下 write/bash 正常执行） | **裁剪**（`agent_loop.py:369-371`） |
| shell | **允许任意命令** | 允许 | 禁（只读闸） |

**oc 的关键取舍**：收窄靠**权限**（执行期），不靠**隐藏**（编排期）。它的一条声明 `edit * deny` 就挡住了所有注册为 `permission:"edit"` 的工具（含第三方），而工具**仍留在表里** —— 模型看得见它、试了、拿到一句「只能在 Plan 目录里改」，当场自纠。

**dsh 两次试图给 plan 加闸，两次发布前撤掉**（设计稿 `:166`、`:168`）：
> 「A per-mode tool allowlist ... re-declares a per-TOOL fact (its effects) per MODE ... it rots silently as tools arrive」
> 「ModeDefinition.access clamped the bash seam's per-call sandbox resolution to a mode-declared ceiling ... fusing the collaboration stance with an enforcement level」

**⇒ 它们的结论与我们该取的方向一致**：**「哪些工具在规划期安全」是工具的属性，不是模式的属性** ⇒ 收窄应该用权限规则表达，不是模式的特判。

### 2.3 提醒（模式指令）怎么进上下文

| | oc | dsh | 我们 |
|---|---|---|---|
| 位置 | **user 角色**，但基座 prompt 事先声明「`<system-reminder>` blocks are harness instructions, not user-authored content」（`system.txt:6`） | system prompt 段（order 500） | user 角色，`[plan] ` 前缀标记 |
| 持久性 | **durable**（走 inbox，`session/inbox.ts:169-202`，durable 事件 + 投影 + 幂等 reconcile） | system prompt 段（每请求重渲染） | **一次性 user 消息**（`plan_runner.py:394-399`） |
| 抗压缩 | **每请求对账**（`plugin/plan.ts:56-74` 的 context hook + `lastReminder`）—— compaction 吞了它就补 | 每请求重渲染 ⇒ 天然抗压缩 | **无** —— compaction 后模型可能忘记自己是 plan 模式 |
| 撤销 | **leave 提醒显式撤销**（「Any Plan mode instructions from earlier in this conversation are no longer active」，`plan.ts:23-25`） | pending 优先于 log ⇒ 下一请求就消失 | 无（靠 `session.mode` 标志位与 `present_plan` 的 mode gate 事中拦截） |
| 用户可见 | **不可见**（`rows.ts:383`：无 description 的 synthetic 不渲染） | 不可见（prompt 段） | **可见**（一条 1137 字符的 user 消息） |

### 2.4 模式切换

| | oc | dsh | 我们 |
|---|---|---|---|
| 机制 | **事件**（`session.agent.selected`，`session.ts:89-95`） | **事件**（`plan/mode`，log-only，whole-value-replace，`plan-mode/src/index.ts:45-54`） | **字段**（`session.mode` 写盘） |
| 落盘点 | 事件投影 | **下一个 accepted pre-step**（`onBoundary`，`index.ts:447-460`） | 回合边界（`agent_loop.py:492-507`） |
| 四态返回 | — | `committed/queued/cancelled/noop`（`index.ts:425`） | 无（`request_posture` 返回 bool） |
| 崩溃后 | 事件已落盘 ⇒ 恢复 | pending 是**进程内 WeakMap**（`index.ts:186`）⇒ 丢 | `pending_posture` **不落盘** ⇒ 丢 |

---

## 3. 我们的差距清单（按「畸形贡献度」排序）

| # | 差距 | 我们的形态 | oc/dsh 的形态 | 贡献了什么畸形 |
|---|---|---|---|---|
| **G1** | **没有研究出口** | 守卫强制提交；纯文本收尾 = 违规 | 两者都允许「不交计划就结束」 | **主因**：研究类请求被逼交计划 |
| **G2** | **契约在 prompt 里，强制点在收尾时** | 两处分离 | oc 无契约无强制点；dsh 无契约无强制点 | 「凑结构」的压力 ⇒ 先调查后补交 |
| **G3** | **只读是 dispatch 特判，不是权限规则** | `mutates_world` 布尔 + `ctx.readonly` | oc：一条声明式规则 | 工具表与执行期两套谓词；「只读」与「权限」语义不同源 |
| **G4** | **模式指令是一次性 user 消息** | 一次性 | oc：durable + 每请求对账 | 压缩后模型忘记自己是 plan 模式 |
| **G5** | **模式是字段，不是事件** | `session.mode` 写盘 | oc/dsh：事件 + 边界落盘 | 没有「pending/committed/cancelled/noop」；崩溃丢 |
| **G6** | **计划指令留在历史里且不撤销** | 永久留在转录 | oc 的 leave 提醒显式撤销 | 旧指令持续施压 |
| **G7** | **计划指令对用户可见** | 1137 字符的 user 消息 | 两者的都不可见 | 社交证据（"我交过作业"）+ 注意力被拉走 |
| **G8** | **没有探索预算** | 规划轮 30 轮 | 两者都无 | 探索可以无限长（本仓的 30 是上限，但也够长） |

---

## 4. 目标形态（先定目标，再谈实现）

### 4.1 一句话

**「计划」是一份有明确交付物契约、且契约在工具 schema 里（不在 prompt 里）的可执行结构；规划期模型只能做探索，不能做工作；而研究型请求有一条合法出路 —— 直接给结论，不必交计划。**

### 4.2 形态图

```
                     ┌──────────────────────────────────────────┐
   进入 Plan ────────►│ PlanStance（事件，不是字段）             │
   （shift+tab /      │  · durable；pending 四态；边界落盘        │
     /plan）          │  · 退出时显式撤销历史里的 plan 指令      │
                     └────────────────┬─────────────────────────┘
                                      ▼
        每请求装配：plan 提醒（durable + 每请求对账，不可见）
                                      ▼
   ┌───────────────────────── 规划回合 ──────────────────────────┐
   │  工具面 = 只读（权限规则表达，不是特判）                    │
   │  合同：只能探索（读/搜），不能工作（写/改/标完成）          │
   │                                                          │
   │  模型有两条合法出路：                                     │
   │   ① present_plan(items + verify)  ← 契约在 schema 里      │
   │   ② finish_research(summary)      ← 研究出口（新增）      │
   │                                                          │
   │  两条都不出 → 守卫 nudge 一次 → 结束（现状，不变）           │
   └──────────────────────────────────────────────────────────┘
                     │                              │
              approve│                              │finish_research
                     ▼                              ▼
        items → task → PlanRunner             直接给结论
        机械验收 + 收据（保留）                不 arm、不执行
```

### 4.3 与三个必守约束的关系

| 约束 | 目标形态下 |
|---|---|
| 「人批准才执行」 | **保留** —— 这是我们的产品边界，oc/dsh 的「聊天即计划」不能抄 |
| 「可执行 + 机械验收 + 收据」 | **保留** —— 我们比它们强的地方 |
| 「不造轮子 / 最小化」 | G1/G2/G3/G4 的修法各自独立成批，**不打包** |

### 4.4 三个候选批次（按依赖排序，各自可独立验收）

| 批次 | 内容 | 治 | 规模 | 依赖 |
|---|---|---|---|---|
| **N1** | **研究出口** `finish_research(summary)`：与 `present_plan` 互斥；成功即终局（不 arm、不执行、不弹卡） | **G1** | M | 无（可独立做） |
| **N2** | **契约归位**：把 items/verify 的格式要求从 `PLAN_PROMPT` 移进 `present_plan` 的 schema description（prompt 只说「要交计划」）；守卫只保留「必须交」的职责 | **G2** | S | 无 |
| **N3** | **只读变成权限规则**：把 `mutates_world` + `ctx.readonly` 的特判，换成 M1a 的规则引擎里的一条声明（`edit * deny` 等），工具表裁剪与执行期判定同源 | **G3** | M | **M1a** |
| N4 | plan 提醒 durable + 每请求对账 + leave 时显式撤销 + 不可见 | **G4/G6/G7** | M | M1b（agent 化） |
| N5 | PlanStance 事件化 + pending 四态 + 边界落盘 | **G5** | M | M1b |

### 4.5 「研究出口」的形态（N1 的具体形式）

```
工具：finish_research({summary: str})
  · 只在 plan 模式可用（与 present_plan 同一道 mode 门）
  · 与 present_plan 互斥：同一回合先成功者生效，另一个返回 already_concluded
  · 成功 ⇒ research_finished = True；守卫放行；回合结束；不 arm、不执行、不弹卡
  · summary 落 tool 结果（用户看得到，模型的历史里有）
  · build/step 模式下不可见（schema 剪除）
守卫：approve || research_finished → 放行；否则 nudge 一次 → 结束
```

**它治的正是你这次的场景**：调研类请求在 plan 模式下，模型探索完直接给结论 → 走 `finish_research` → 回合结束 → **没有假计划、没有 NUDGE 循环、没有工作区门拒、没有策略梯子介入**。

---

## 5. 一句话收口

**oc 与 dsh 的 plan 顺畅，是因为它们没有「交卷」这件事；我们的 plan 有交卷（这是我们的产品价值），但我们把「交卷的格式」写在了 prompt 里、把「交卷的强制」放在了回合末尾 —— 于是模型在交卷前已经把活干完了。**

**目标形态 = 保留交卷（我们的价值）+ 把格式契约归位到 schema + 给研究型请求一个不必交卷的出口。**

三件事（N1/N2/N3）互不依赖，可以按任意顺序做；N3 依赖 M1a。
