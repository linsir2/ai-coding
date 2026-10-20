# 15 · plan 模式实测解剖（形同虚设的机制）

> 本文是**实测报告**，不是方案。对象：`feat/orchestration-cognition` 分支当前的 plan 模式。
> 证据两类：① 用户那次真实会话的本地 trace（session `3da6e03f…`）；② 本批用 tmux 起真 TUI + 真模型（qwen3.7-flash）做的复现（session `9a9e2df9…`）。
> 结论一句话：**plan 模式不是「读上下文 → 规划 → 执行」，而是「读上下文 → 规划 → **必须交一份带机械判据的可执行计划** → 执行」。对实现类任务成立，对研究/回答类任务是错的。**

---

## 1. 现场：用户的真实会话发生了什么

**用户输入**（`3da6e03f…json` 的 `messages[0]`）：

```
请你调研我们这个分支都干了些啥
```

**没有 `/plan`**。但会话进了 plan 模式。

### 1.1 完整事件流（trace `turn-1.jsonl`，76 条事件，11 次迭代）

| iter | 事件 | 工具 | 结果 |
|---|---|---|---|
| i1 | MemoryInjected / ContextAssembled | — | |
| i2 | ModelStepFinished → ToolStarted/Finished | `skill{action:list}` | ✅ |
| i3 | | `glob{**/*}` | ✅ |
| i4 | | `glob{.git/logs/refs/heads/…}` + `grep` | ✅ |
| i5 | | `grep{commit }` | ✅ |
| i6 | | `grep{commit:|commit \(amend\):}` | ✅ |
| i7 | | `grep{…}` + `read{.git/COMMIT_EDITMSG}` | ✅ |
| i8 | | `read{.git/logs/refs/heads/…}` | ✅ |
| i9 | | `glob{ai_coding/**/*.py}` + `glob{docs/**/*.md}` | ✅ |
| **i10** | **纯文本回答** | — | **模型产出了完整调研报告**（表格 + 13 个模块详解） |
| **i11** | | `present_plan` | **error=plan_keep**（用户驳回） |
| i12 | | `present_plan` | **dismissed**（用户搁置） |

### 1.2 守卫的那一句（`messages[21]`，`turn_policy.py:52-55` 原文）

```
[plan] 尚未提交计划：把全部可执行步骤放进 present_plan 的 items 数组（每步一条，含 verify 命令）提交评审，不能以纯文本收尾。请立即补交。
```

### 1.3 模型被逼交出的「核验的计划」（`messages[24]` 的工具参数原文）

```markdown
## 分支调研报告交付
将 feat/orchestration-cognition 分支的调研结果落盘为结构化文档，方便后续回溯。
| # | 步骤 | verify |
| 1 | 创建 docs/branch-research/feat-orchestration-cognition.md，写入完整调研报告 | test -f docs/…md && wc -l docs/…md |
| 2 | 验证报告可读性：提取 commit 数量与模块数是否匹配已知数据 | grep -c "^### \|^#### " docs/…md |
```

模型在 `messages[24]` 里自己道歉：

> 抱歉，之前的回复确实只给了纯文本而没有走 `present_plan` 流程。让我补交一个可执行的计划，将调研结果持久化落地。

**用户拿到的是一份「把已经答完的问题落盘」的计划。**

---

## 2. 复现：tmux + 真模型

**环境**：独立工作区 `/tmp/opencode/planprobe/repo`（4 个 commit 的 git 仓库）、`tracing.capture: full`、真 TUI（`ai-coding` 裸启动）、真模型 qwen3.7-flash。

**操作**（复刻用户的真实手势）：

```
tmux send-keys Escape            # 聚焦输入框
tmux send-keys $'\033[Z'         # shift+tab  ← 用户看不见这个动作
tmux send-keys "请你调研一下这个仓库的提交历史都干了些什么" Enter
```

shift+tab 之后状态栏立刻变成 `PLAN · dashscope · qwen3.7-flash`（**没有任何提示告诉用户刚才发生了什么**）。

### 2.1 规划回合（trace `turn-1.jsonl`，84 条事件）

| iter | 工具 | 结果 |
|---|---|---|
| i1 | `bash{git log --oneline}` ×2 | **readonly_denied** ×2 |
| i2 | `glob{.repo/.git/**/*}` + `glob{.git/**/*}` | ✅ |
| i3 | `read{.git/logs/HEAD}` | ✅ |
| **i4** | **纯文本** | **模型已给出完整报告**（提交历史表 + 4 阶段分析） |
| **i5** | **`present_plan`** | ✅ 批准（守卫 NUDGE 之后） |
| i6 | `read{.git/refs/heads/main}` + `read{.git/logs/refs/heads/main}` | ← **批准后规划回合没停** |
| i7 | `glob{**/*}` + `read{.git/COMMIT_EDITMSG}` | ← 同上 |
| i8 | `bash{python3 -c "import zlib…解压 git object"}` | **readonly_denied** |
| i9 | `read{.git/objects/xx/yy…}` ×4 | ← 读二进制，失败 |
| i10 | `todo_write{action:update,status:completed}` ×3 | **readonly_completion** ×3 |
| i11 | 纯文本 | TurnFinished |

### 2.2 那份被逼出来的计划（评审卡原文）

```
◆ Plan review
批准这个计划并开始执行？

## 仓库提交历史调研
通过读取 Git 底层文件，系统性地分析该仓库的全部提交记录及其内容变更。
1. 读取 .git/logs/HEAD 获取全部提交序列和消息
     verify：test -f .git/logs/HEAD
2. 读取 .git/refs/heads/main 确认当前分支指向的 commit hash
     verify：test -f .git/refs/heads/main
3. 读取每个 commit 对象（.git/objects/xx/yy...）获取详细提交信息
     verify：for obj in $(grep -oP '[a-f0-9]{40}' .git/logs/HEAD | sort -u); do test -f .git/objects/${obj:0:2}/${obj:2}; done
4. 列出工作区所有文件，对照提交历史理解各阶段变更范围
     verify：glob '**/*'
```

**四个步骤就是模型刚刚做完的事。** verify 全是**为了满足格式而造的判据**：
- `test -f .git/logs/HEAD` → 只测文件存在（读没读过它测不出来）
- `glob '**/*'` → **根本不是 shell 命令**，是工具语法，机械验收必然失败

### 2.3 批准之后（执行回合，trace `turn-2.jsonl`）

| iter | 动作 | 结果 |
|---|---|---|
| i1 | `read{.git/logs/HEAD}` | ← **重复劳动**：规划期已读过 |
| i2 | `todo_write{status:completed}` | ✅（这次不被拒） |
| i3 | `read{.git/refs/heads/main}` | ← **重复劳动** |
| i4 | `todo_write{update}` | ✅ |
| i5 | `bash{for hash in …}` | **ToolApprovalRequested**「bash command requires confirmation」→ **等人点** |

**「批准即自动执行」在这里不成立**：执行期每条 bash 都要人工批准（W4 之后 bash 恒为 `ask` 档）。

---

## 3. 数据流解剖（当前设计）

### 3.1 两条正交的轴

```
mode == "plan"   →  管「出口」：present_plan 是否可用、收尾守卫是否问
readonly == true  →  管「工具面」：bash/write/edit 不出现在 schema 里、执行期结构性拒绝
```

`/plan` 同时置位两者（`app/commands.py:534`），但代码里**完全正交**。plan 模式的所有行为都是这两条轴的组合。

### 3.2 一次 plan 回合的完整时序

```
用户输入
  ↓
[Home 路由 shift+tab] → 合成 /plan（不进 transcript，用户不可见）
  ↓
commands._handle_plan → request_posture(mode="plan", readonly=True)     commands.py:534
  ↓
规划回合：
  ├─ system prompt 追加 <plan-mode> 段                                  prompt.py:127-138
  ├─ 工具面 = 只读面（去掉 mutates_world 的工具）                        agent_loop.py:369-371
  ├─ 模型探索（read/grep/glob 可以，bash 被结构性拒绝）
  ├─ 模型尝试以纯文本收尾
  │     ↓
  │  守卫：decide_after_step → PlanSubmissionGuard.on_stop                turn_policy.py:215-220
  │     ├─ 没提交过 → NUDGE（注入 PLAN_NUDGE）→ 模型必须 present_plan
  │     └─ nudge 过还没提交 → FINISH + PLAN_UNSUBMITTED_NOTE
  └─ present_plan → 评审卡（approve / keep / dismiss）
        ↓ approve
        task.items 整表替换 + armed=True + armed_profile="plan"          present_plan_tool.py:264-281
        ↓
        前端自动派发 /plan run（CLI 不会自动）                             session.tsx:485-508
        ↓
        PlanRunner.run → TaskDriver.run → 每步一个 fresh ephemeral step session
        ├─ step session：mode=normal, readonly=False, policy 8 步
        ├─ 跑 step → verify 命令（subprocess）→ 机械裁决 completed/blocked/stalled
        └─ StepDelivery 收据 + plan.receipt 事件
```

### 3.3 强制模型行为的四处文本（逐字）

| # | 文本 | 位置 |
|---|---|---|
| 1 | `<plan-mode>` 段：`You are in plan mode. **Every request is answered with a plan, never with the finished work**` … `do not write the analysis or files as your final answer` | `ai/prompt.py:129-137` |
| 2 | `PLAN_PROMPT`：`Create a detailed implementation plan for: {topic}. Break it down into specific, actionable steps. Submit the plan with present_plan…` | `core/plan_runner.py:71-91` |
| 3 | `PLAN_NUDGE`：`[plan] 尚未提交计划：把全部可执行步骤放进 present_plan 的 items 数组（每步一条，含 verify 命令）提交评审，不能以纯文本收尾。请立即补交。` | `core/turn_policy.py:52-55` |
| 4 | `present_plan` 工具描述：`Use it at the end of planning… 'items' as the executable steps — one entry per step, each with its 'verify' shell command when the outcome is machine-checkable.` | `tools/present_plan_tool.py:69-78` |

**四处都在说同一件事**：计划 = 可执行步骤 + 机械判据。**没有一处提到「有些请求不需要计划」。**

---

## 4. 五个具体缺陷（不是 prompt 措辞问题）

| # | 缺陷 | 证据 | 后果 |
|---|---|---|---|
| **D1** | **plan 模式只有一个出口**：`present_plan` 或「未提交」终止。**没有「这是研究，我答完了」这个出口** | `turn_policy.py:215-220` + `present_plan_tool.py` 的全部返回分支 | 研究型请求被强行改写成可执行计划；模型被迫造假判据 |
| **D2** | **verify 机制把「计划」窄化成「可机械验收的步骤序列」** | `test -f .git/logs/HEAD`、`glob '**/*'`（不是 shell 命令） | ① 模型造假判据 ② 机械验收必然失败 ③ 审批通过后执行的是重复劳动 |
| **D3** | **plan 模式禁 bash** | `bash{git log}` ×2 → `readonly_denied` | 调研类任务被迫读 `.git` 内部文件（姿势怪异、读二进制失败、绕远路） |
| **D4** | **批准后规划回合不停止** | turn-1 的 i6-i10 继续探索，还试图 `todo_write{completed}`（被 `readonly_completion` 拒） | 批准后模型继续在规划上下文里活动，语义混乱 |
| **D5** | **执行期 bash 恒为 `ask`** | turn-2 i5：`ToolApprovalRequested`「bash command requires confirmation」 | 「批准即自动执行」的承诺在含 bash 的真实计划里不成立 |

### 4.1 附带发现（与 plan 模式相邻的真实问题）

| # | 发现 | 位置 |
|---|---|---|
| E1 | **Home 路由的 shift+tab 对用户完全隐形**：合成一条 `/plan` 命令（不进 transcript），只留一句 notice + 状态栏 PLAN。`prompt.tsx:325-326` 把 Tab 绑给补全、Home 把 shift+tab 绑给姿态切换 —— 相邻键位在中文输入下极易误触 | `frontend/src/util/submission.ts:104-116`、`home.tsx:96` |
| E2 | **plan 姿态黏在磁盘上**：`3da6e03f….json` 仍是 `mode:"plan", readonly:true`。再打开这个会话，下一条消息直接进 plan 且**不再有 notice** | `domain/session.py:567-568` |
| E3 | **四个 FINISH 口绕过守卫**：`decide_next_iteration` 的预算/时钟/token/阶梯终止都不查 `_stop_guards` → 模型只要一直在调工具就永远走不到守卫；预算一到回合直接结束，计划未提交、模型收不到任何提示 | `core/turn_policy.py:256-308` vs `:328-331` |
| E4 | **`outcome.note` 在 CLI 是哑的**：`PLAN_UNSUBMITTED_NOTE` / `PLAN_DISMISSED_NOTE` 只有 Server 前端读（`server/bridge.py:189`），CLI 全仓无人读 | `core/turn_policy.py:56-62` |
| E5 | **plan 模式下 `todo_write` 仍在工具面里**（`mutates_world=False`），「不要用 todo 记录计划」是纯 prompt 约束，代码不拦 | `tools/todo_write_tool.py:25` |
| E6 | **CLI 不会自动执行批准后的计划**：`PlanRunner` 全仓只有 `commands.py:503`（`/plan run`）与 `handlers.py:612`（前端/boot）两个调用点 | 同上 |

---

## 5. 根因判定

### 5.1 一句话

**plan 模式把「回合的结束条件」定义成了「提交一份带机械判据的可执行计划」。这个定义对实现类任务成立，对研究/回答类任务是错的——而系统没有为后者提供任何别的出口。**

### 5.2 为什么这不是 prompt 能修的

`PLAN_NUDGE` 是**代码注入**的（`turn_policy.py:215-220` → `agent_loop.py:895-903`），它不依赖模型是否听话：
- 模型听话 → 交一份（可能是假的）计划 → D2
- 模型不听 → 一次 nudge 后 FINISH → 用户拿到「本回合未提交计划」的哑 note（E4）

**两条路都通向同一个坏结果**。所以要么给「研究」一个出口（08 的 T4 `finish_research`），要么把「计划」的窄定义放开（08 的 T12）。**只改 prompt 措辞无效**——prompt 文本已经被那四段话塞满了，没有空隙。

### 5.3 与 08 稿的对应关系

| 08 的任务 | 本文的对应发现 | 状态 |
|---|---|---|
| **T4 `finish_research`** | **D1**（唯一出口） | 未实施 —— 这就是本批要修的根因 |
| **T12 PLAN_PROMPT v2** | **D1/D2**（「只有两个出口」「每步有 verify」） | 未实施 |
| **D15/D16**（停止/提交守卫、研究型出口） | **D1 + E3** | 部分（W1 修了「什么算提交」，没修「有几个出口」） |
| **T6 软/硬只读** | **D3**（plan 禁 bash） | 未实施（M2） |
| **T11 评审三态** | **D4**（批准后规划回合不停） | 部分（R08 修了 keep/dismiss 的语义，没修「批准后规划回合要停」） |
| — | **D2 verify 的机械性** | **08 稿没有这一项**。opencode 没有执行器，所以 OC 没有这个问题 —— 这是本仓自有的、必须自己设计的一块 |
| — | **D5 执行期 bash 恒 ask** | 08 稿没有正面处理。它与「批准即自动执行」的承诺直接冲突 |

### 5.4 「计划模式应该是什么」的三个候选形态

| 形态 | 描述 | 与 08 的关系 |
|---|---|---|
| **A · 形态对齐（08 现状）** | plan = 「产出可执行计划」，研究型请求也走这条路 | 08 的 D15/D16 想改，但没改到位 |
| **B · 双出口** | plan = 「规划 → 交计划」**或**「研究 → 交结论」。研究出口由 `finish_research(summary)` 提供，不 arm、不执行 | **08 的 T4**，但 T4 只在 prompt 层说「优先用 finish_research」，没在**守卫层**承认它是出口 |
| **C · 意图分流** | 进 plan 前先判「这是实现类还是研究类」，研究类直接走普通模式 | 需要一个意图判别器（本仓没有；08 稿没有这项） |

**B 是改动最小、与 08 一致、且能解释全部五个缺陷的形态**：有了研究出口，D1 消失 → D2 的造假动机消失（不用交计划就不需要 verify）→ D4 的语义混乱消失（研究出口是终局，不继续探索）。

---

## 6. 待拍板（本批要定的）

1. **plan 模式要不要有研究出口？**（D1 的答案）
   - 若要 → 08 的 T4 升格为本批主线，且守卫必须承认它（不只是 prompt 偏好）。
   - 若不要 → 现状是刻意的，plan = 只做实现类规划，那**产品上就必须让用户不进 plan**（E1 的 shift+tab 隐形 + 普通提问被误触）。
2. **verify 是不是每步必填？**（D2）
   - 若 verify 可选 → 步骤可以是「调研/分析」类不可机械验收的动作，验收改为「模型声明 + 人工看」。
   - 若必填 → 只能接受「模型造假判据」这个副作用，或把机械验收的语义改成「尽力而为」。
3. **plan 模式该不该禁 bash？**（D3）
   - 禁 → 调研类任务只能读 `.git` 内部文件（实测绕远路 + 读二进制失败）。
   - 不禁 → plan 的「只读」不再是硬边界（但 bash 在本仓是 WARN 档，会要人工批准，不是静默写）。
4. **批准后规划回合要不要立刻停？**（D4）
5. **执行期 bash 要不要免审？**（D5）——「批准即自动执行」要么兑现，要么别承诺。

---

## 附：证据清单

| 证据 | 位置 |
|---|---|
| 用户真实会话 | `.ai-coding/sessions/3da6e03f0de24e2baf74a47489396a79.json`（26 条消息）、`.ai-coding/traces/3da6e03f…/turn-1.jsonl`（76 事件） |
| tmux 复现 · 规划回合 | `/tmp/opencode/planprobe/repo/.ai-coding/traces/9a9e2df9…/turn-1.jsonl`（84 事件） |
| tmux 复现 · 执行回合 | 同上 `turn-2.jsonl`（32 事件） |
| tmux 复现 · 全量消息快照 | 同上 `turn-1.full.json`（`capture: full`） |
| 守卫原文 | `ai_coding/core/turn_policy.py:52-62`（NUDGE + 两个 NOTE） |
| prompt 原文 | `ai_coding/ai/prompt.py:127-138`、`ai_coding/core/plan_runner.py:71-91` |
| 出口穷举 | `ai_coding/core/turn_policy.py:215-220`、`ai_coding/tools/present_plan_tool.py:28-45` |
| 绕过守卫的四个 FINISH 口 | `ai_coding/core/turn_policy.py:256-308` vs `:328-331` |
| 进入 plan 的全部路径 | `ai_coding/app/commands.py:534`（唯一写 mode 的地方）、`frontend/src/util/submission.ts:104-116`、`frontend/src/routes/session.tsx:346-363` |
