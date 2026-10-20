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

## 3.5 对照实验：同一件事，两个入口，两个行为（决定性）

> 上面的解剖把「模型为什么不规划」指向了一个可疑处：**裸 `/plan` 不给模型任何规划指令**。
> 用受控实验验证：同一个仓库、同一句话、同一模型、同一只读工具面，**只改入口**。

| | 实验 A · 裸 `/plan`（= shift+tab 走的路） | 实验 B · `/plan <topic>` |
|---|---|---|
| 用户气泡 | `请你调研一下这个仓库的提交历史都干了些什么` | `PLAN_PROMPT.format(topic)`（`Create a detailed implementation plan for: …`） |
| 任务注入 | **无**（`begin_plan` 没跑 → `task.goal` 为空 → 无 `<task-state>` 块） | 有（`目标: 调研这个仓库的提交历史都干了些什么`） |
| **模型第一句 reasoning** | `The user wants me to **investigate** the git commit history of this repository…` | `The user wants me to **create a detailed implementation plan** for investigating the commit history…` |
| 行为 | 探索 9 轮 → **直接输出报告** → 被守卫追讨 → 补交假计划 | 探索 3 轮 → **写计划** → `present_plan` |
| 计划的步骤 | **已经做完的事**（读 .git/logs/HEAD…） | **还没做的事**（获取提交日志 → 逐个调查 4 次提交 → 汇总报告） |
| 计划的 verify | **造假**：`test -f .git/logs/HEAD`、`glob '**/*'`（不是 shell 命令） | **真判据**：`git log --all --oneline --graph --date=short`、`git show --stat <sha>`、`git show <sha> -- '*.py'` |

**结论**：模型「不规划、直接输出」不是因为它读不懂 system prompt（`<plan-mode>` 段占 system prompt 的 44.7%，位置也不埋没），而是因为**裸路径的用户气泡里没有任何东西告诉它「这是规划任务」**。

它照字面执行了用户的话（去调研），把调研做完了，给出报告 —— **它做对了用户的话，但没做 plan 模式该做的事**。

### 3.5.1 裸 `/plan` 为什么没有规划指令（代码）

```python
# app/commands.py:548-553   ——  裸 /plan：只切两个轴，然后直接返回
if not rest:
    if already:
        console.info("[plan] 已在计划模式（只读）。继续对话写计划；评审卡批准后自动执行。")
    return REPLAction.CONTINUE, session      # ← 没有规划回合、没有 PLAN_PROMPT、没有 begin_plan

# app/commands.py:561-563   ——  /plan <topic>：走 plan_only
report = await plan_only(loop, session, topic=rest, ...)
#   └─ plan_runner.py:374-381
#      begin_plan(session, topic)                       ← task.goal = topic（有任务注入了）
#      loop.process_input(session, PLAN_PROMPT.format(topic))   ← 用户气泡 = 规划指令
```

**两条路径的差别不是「有没有 plan 模式」，而是「有没有给模型规划指令」。**

### 3.5.2 附带发现：实验 B 的 present_plan 也失败了

`error=workspace_paths`：

> 目标路径在工作区外：`/tmp/repo_history_analysis.md`。文件工具（read/write/edit/glob）无法访问工作区外的路径；请 cd 到目标目录重新启动 ai-coding，或把计划改为工作区内的路径。

R02 的工作区门正确拦住了「把报告写到 /tmp」。但这是 **D2 的连带伤害**：模型被迫给「写报告」造判据 → 造判据就要指定落盘路径 → 落盘路径又在门外 → 门把它拒了。**判据机制把一个纯输出动作变成了一个路径决策。**


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

### 5.1 一句话（v2 修正）

**plan 模式只切了两个轴（mode / readonly），没有向模型发出「这是一个规划任务」的信号。裸 `/plan`（= shift+tab 唯一的路径）连一句规划指令都不给，于是模型照字面执行了用户的话：把活干完、直接输出答案。等到守卫事后追讨计划时，它手里只剩「已经做过的事」，只能把它们伪装成「带 verify 的步骤」——verify 只能造假。**

**所以「核验的计划」不是模型笨，是它在一个没有规划指令的回合里被迫补交计划。**（v1 的根因判定「缺研究出口」仍然成立，但它解释的是**研究类请求**为什么该有第二个出口；它解释不了「为什么它不规划」。v2 的判定解释的是后者。两者都要修。）

### 5.1.1 三个层次的因果链

```
① 裸 /plan 不注入规划指令（commands.py:548-553 直接 return）
        ↓
② 模型把用户请求当普通任务执行 → 探索 → 直接输出答案
        ↓
③ 守卫是事后的（turn_policy.py:215-220 在模型「要收尾」时才问）
        ↓
④ 模型手里只有「已做的事」→ 伪装成步骤 → verify 只能造假（test -f / glob '**/*'）
        ↓
⑤ 批准后执行回合把同一批文件重读一遍（重复劳动），且 bash 仍要人批
```

**②③ 之间没有第三个信号**：模型在 9 轮探索里没有收到任何「你该规划了」的提示。

### 5.1.2 为什么 system prompt 修不了

`<plan-mode>` 段占了 system prompt 的 **44.7%**（602 / 1346 字符），位置在 55% 处，既不埋没也不含糊，原文就是 `Every request is answered with a plan, never with the finished work`。

**模型还是没规划。** 所以问题不是「提示不够强」，而是「提示被一条更强的信号覆盖了」——**用户消息**。在实验 A 里用户消息是「请你调研一下这个仓库的提交历史都干了些什么」；在实验 B 里用户消息是「Create a detailed implementation plan for: …」。**后者是规划指令，前者是任务指令。任务指令赢了。**

顺带排除一个可能的解释：不是「模型没看到 plan 段」——bash 被 `readonly_denied`、present_plan 出现在工具表里，都证明 mode 确实传到了。

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

**实测后新增的两条**：

6. **裸 `/plan` 要不要注入规划指令？**（F1）—— 这是「模型不规划」的直接原因，改动最小、零依赖。若不做，**plan 模式对 shift+tab 用户就是「只读模式」而不是「计划模式」**。
7. **verify 是不是每步必填？**（F4）—— 这条动到执行层契约（08 §8.3 的保留项），**必须用户拍板**。

---

## 7. 修法（按根本性排序，含成本）

> 这一节是**建议**，不是已拍板的方案。每一项都标了它治哪个缺陷、代价多大、能不能单独验证。

### F1 · 裸 `/plan` 也注入规划指令（最小、治本）

**改哪**：`app/commands.py:548-553`（裸路径直接 return）—— 让它与 `/plan <topic>` 走同一条「规划回合」的语义。

**怎么改**（二选一，实施时定）：
- (a) **用户气泡加前缀**：plan 模式下用户下一条消息 = 规划指令 + 原始请求（与 `/plan <topic>` 的 `PLAN_PROMPT.format(topic)` 同构）。
- (b) **裸路径也跑 `plan_only`**：topic 为空时用用户的下一条消息作 topic。

**治**：①②④（模型会规划、verify 会是真判据）
**代价**：改 1 处 + 1 处用户气泡拼接；需要决策「裸路径的用户消息是否还算不算用户原话」（影响转录与 replay）
**风险**：低。但要注意 (a) 会让**用户气泡**变形（转录里用户说的话被加了前缀）——需要标记成 synthetic（仓库已有 `synthetic=True` 的用法）。
**可独立验证**：把实验 A 重跑一遍，看模型第一句 reasoning 是否变成「create a detailed implementation plan」。

### F2 · plan 模式 agent 化（08 的 T1/T7，结构修法）

**改哪**：08 §4.2 的 T1 —— 把 plan 从「两个轴」变成**一个角色**（agent 表里的一行，它的 system prompt 就是「你产出计划」）。

**治**：①②（根上的）、以及 D5（agent 规则可以给 plan 不同的 bash 策略）
**代价**：M1b 的一整个批次（agent 表 + 消息级归属 + 规则层）
**收益**：角色化的 system prompt 权重远高于「通用助手 + 追加一段话」。**这也可能改变 M1a/M1b 的排序**（见 §8）

### F3 · 守卫从「事后追讨」前移（治 D4 与「计划不早」）

**改哪**：`core/turn_policy.py:215-220` 的 `on_stop` —— 现在只在模型**要收尾**时才问。

**思路**：给规划回合一个**探索预算**（比普通回合小），预算到就强制 `present_plan`。这样「规划」自然发生在「探索」之后、「干活」之前。

**治**：③（模型会在探索中途被推向规划）
**代价**：要定预算数值；要处理「探索不够怎么办」
**风险**：中。预算太小会把复杂计划逼成半成品。

### F4 · 计划可以没有 verify（治 D2）

**改哪**：`present_plan_tool.py` 的 items schema（`verify` 改成可选）+ `plan_runner._settle` 的机械裁决（没有 verify 时怎么办）。

**治**：D2（模型不必造假判据）、以及 §3.5.2 的 `/tmp` 路径误伤
**代价**：机械验收的语义要重新定义（08 §8.3 的保留契约写着「acceptance.verify_items 的机械验收」——这是**用户边界**，不能随便删）
**风险**：高。**这条要用户拍板**：机械验收是本仓的核心设计（收据、blocked、stalled 判定全靠它），放开 verify 会动到整条执行层契约。

### F5 · 计划执行期不再要人批 bash（治 D5）

**改哪**：M1a 的规则引擎（`permissions.py`）—— plan 的 items 是**已批准**的，它们的 verify 命令是**计划的一部分**，不该再逐条问人。

**治**：D5（「批准即自动执行」兑现）
**代价**：依赖 M1a 的规则语言（给 plan 的 verify 一个「已批准即免问」的身份）
**风险**：中。**安全边界要小心**：verify 命令是模型写的，批准的是「内容」不是「任意 shell」。

---

## 8. 一个被忽视的事实：这条路径用户根本看不见

`frontend/src/util/submission.ts:104-116` 的 `planPrelude()` 会向后端发一条 `session.command {text:"/plan"}` —— **不带 topic**，而且**不进 transcript**。

所以：**用户按 shift+tab 之后，输入框里的话是原样的，但后端已经在 plan 模式了。** 用户看到的是「我明明没输 /plan，怎么在写计划？」

这条与 F1 直接相关：**如果 F1 落地，裸路径的用户气泡会被加上规划指令**，那用户在转录里就更看不到自己的原话了 —— 需要一个可见的标记（类似 `[plan]` 的 notice）。

---

## 9. 对 M1a / M1b 排序的影响

原计划：M1a（权限引擎 T2/T2b/T2c）→ M1b（agent 承载 T1/T1b/T10）。

**实测后的重新评估**：

| | 原判断 | 实测后的判断 |
|---|---|---|
| plan 模式「不规划」是不是权限问题？ | 部分是（D3 禁 bash 逼模型绕远路） | **主要是角色问题**：没有规划指令 → 不是权限语言能解决的 |
| agent 化（T1）能不能单独解决？ | 08 把它排在 M1b | **它可能才是 plan 模式的主修法**（F2） |
| M1a 还要不要先做？ | 要（它是 M2/M3 的地基） | **仍要**，但 F1（裸路径注入规划指令）**不依赖任何东西**，可以插在 M1a 之前单独做 |

**建议的插队**（待拍板）：

```
F1（裸路径注入规划指令）      ← 最小、零依赖、直接治「不规划」
  ↓
M1a（权限引擎）               ← M2/M3 的地基，仍按原计划
  ↓
M1b（agent 承载 T1）          ← F2 的主体；且实测显示它对 plan 模式的价值比原估高
```


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

---

## 8. 第二次实测（用户会话 `135cc71c`，修复后）

> 修复 = `b746bf5`（plan 模式下的输入走规划轮）。这次会话在修复之后跑的。

### 8.1 执行链还原（64 条消息，逐条）

| 消息 | 谁 | 内容 |
|---|---|---|
| [0] | user | 「请你探索一下这个分支里面到底都干了啥，相较于原来」 |
| [1] | user | `[plan] Create a detailed implementation plan for: …` ← **规划轮生效** ✓ |
| [2]-[5] | 助手 | glob / grep / read —— 开始探索 |
| [6]-[7] | 工具 | `bash` ×2 → **readonly_denied**（只读闸） |
| [8]-[55] | 助手 | **继续探索 11+ 轮、20 次工具调用**（读完 13 份设计文档 + 6 个源码文件） |
| [56] | 助手 | **产出了完整的研究报告**（表格 + 五大节）← **它把活干完了** |
| [57] | user | `[plan] 尚未提交计划：…不能以纯文本收尾。请立即补交。` ← 守卫 NUDGE |
| [58] | 助手 | 「我来补交一个完整的可执行探索计划。」 |
| [59] | 工具 | `present_plan` **失败**：`workspace_paths` —— 目标路径在工作区外：`/dev/null`, `/tmp/exploration_summary.md` |
| [60]-[61] | 助手/工具 | 又试 `bash` → readonly_denied |
| [62] | user | `[strategy-gate] 最近 2 轮的工具调用全部失败。不要重复同样的做法…` ← **策略梯子介入** |
| [63] | 助手 | **把梯子的注入文本当成「演示素材」开始胡说**：「这是一个实际运行中的演示！我可以看到分支改造后的核心功能正在生效：…`strategy` 门已触发（L1）」 |

**这一回合以模型胡说结束。**

### 8.2 四个缺陷

| # | 缺陷 | 证据 | 归属 |
|---|---|---|---|
| **A** | **plan 模式没有「计划先行」的结构性约束** —— 只有只读 + **事后**追讨 | 守卫只在模型要收尾时才问（`turn_policy.py:206-220` → `agent_loop.py:877`）；规划指令自己写着「use grep/glob to look at code selectively」（`plan_runner.py:78-98`） | 15 号稿 D2/F3 |
| **B** | **NUDGE 后的补交被工作区门拒** | [59] `workspace_paths`（模型老想写 `/tmp/…`、`/dev/null`） | 15 号稿 §3.5.2 |
| **C** | **策略梯子的注入被模型当成内容** → 胡说 | [62]→[63] | **新发现** |
| **D** | **UI 竞态**：裸 `/plan` 命令的 `turn.finished` 与规划轮的 `submitted` 赛跑 → busy 被提前置 false → 输入框显示「Ask anything…」而模型在干活 | 见 §8.3 | **新发现** |

### 8.3 缺陷 D 的机制（UI 竞态）

```
前端：planPrelude() → client.request("session.command", {text:"/plan"})
        .finally(() => submit(prompt.text))        ← 响应到了就提交
后端：那条命令自己也发一个 turn.finished（commands.py:487-530 → handlers.py:860-867）
前端：state/session.ts:725  case "turn.finished" → busy = false
```

⇒ **命令的终局事件与规划轮的 `submitted`（busy=true）赛跑**：命令的 `turn.finished` 若后到，就把 busy 置回 false ⇒ 占位符从「Working…」变回「Ask anything…」，而规划轮正在跑。

⇒ 前端只有 `turn.finished` 一个地方把 busy 置 false（`state/session.ts:725`），**协议里没有 `turn.started`**。

### 8.4 A 的根：plan 模式是「只读 + 事后追讨」，不是「计划先行」

| 任务类型 | 「干活」被只读挡住吗？ | 结果 |
|---|---|---|
| 实现类（写文件） | ✅ 挡住（`mutates_world` 工具被结构性拒绝，`dispatch.py:323-324`） | 流程天然是「读上下文 → 规划 → 批准 → 执行」 |
| **研究类（读 + 答）** | ❌ 没挡住（`read`/`grep`/`glob` 的 `mutates_world=False`，`tools/read_tool.py:64` 等） | **探索就是干活** → 模型把活干完 → 守卫事后追讨 → 补交「已做过的事」当步骤 |

⇒ **你这次会话是研究类**，所以「先探索完再给计划」在这个请求下是**结构必然**，不是模型不听话。

⇒ 要治它，需要**结构性约束**而不是更强的措辞。代码里现成的接缝：
- 规划轮的预算替换（`plan_runner.py:417-451` `continuations_disabled(max_iterations=…)`）→ 可换成更小的「探索预算」，并在快用尽时提前 NUDGE「现在交计划」
- 回合收尾裁决点（`agent_loop.py:855-905`）
- 回合开头裁决点（`agent_loop.py:658-660` → `turn_policy.py:256-308`）
