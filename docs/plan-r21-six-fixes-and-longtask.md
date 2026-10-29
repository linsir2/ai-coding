# R21 方案：6 个遗留问题的根因与修法 / 长时任务真机评测

> 输入：`docs/r20b-eval-report.md` §3.5 的 5 条未修项 + §1.1 的引用覆盖残余误差。
> 本轮还包含一次长时任务（TaskFlow 全栈）真机跑，带流量录制。
> 状态：**待拍板**。决策点见 §0；评审结论见 §9。

---

## §0 决策点

| # | 问题 | 选项 | 建议 |
|---|---|---|---|
| 1 | plan 卡的 Markdown 怎么处置 | (a) 纯文本美化（表格/标题本地排版） (b) 复用 `<markdown>` 渲染器 (c) 后端只下发结构化条目 | **(a) 先做，(b) 另起 spike** |
| 2 | posture 同步修在哪层 | (a) `agent.switched` 带 `readonly` (b) 前端收到事件后 `refreshPosture()` (c) 新增 `posture.changed` 事件 | **(a)**，并把"只翻 readonly"也发事件 |
| 3 | `update_goal` 误用 | (a) 按 task 状态裁剪工具 (b) 计划回合一律剔除 + 文案 (c) 只改文案与降噪 | **(a)+(c)**（评审：plan 步骤回合无 plan 规则，(b) 落不了地） |
| 4 | 计划计数口径 | (a) 头条改成数全部完成 (b) 显式写"分步验收 1/2 · 分步外 1" (c) 卡上带来源标记 | **(a)** |
| 5 | 中文右缘 `�` | (a) 先做判别实验再修 (b) 直接改为应用层预折行 (c) 不改，只在报告里注明 | **(a)** |
| 6 | 引用内容校验 | (a) 查时回读磁盘比对引文 (b) 读时存内容指纹 (c) 读时存内容草图 | **(a)**；不做读时快照（评审：没有持久化通道） |

---

## §1 plan 卡把 Markdown 原样显示

### §1.1 机制（已核对）

- 模型用 `present_plan` 提交计划，`plan` 是**模型自己写的原始 Markdown**，后端只做 `strip()`：
  [`present_plan_tool.py:192`](file:///workspace/ai-coding/ai_coding/tools/present_plan_tool.py#L192)。
- **结构化条目本来就单独下发**：`review_items`（content/verify/dependsOn）在
  [`present_plan_tool.py:258-261`](file:///workspace/ai-coding/ai_coding/tools/present_plan_tool.py#L258-L261) 构造，
  随 `ask(..., detail=plan, intent="plan_review", data={"items": …})` 一起走。
- 前端把 `detail` 塞进**纯文本** `<text>`：
  [`plan-review.tsx:157`](file:///workspace/ai-coding/ai_coding/frontend/src/component/plan-review.tsx#L157)。
  于是 `#`、`|` 原样出现。
- 前端**已有** Markdown 渲染器（OpenTUI `<markdown>`），转录正文用它：
  [`message.tsx:74-80`](file:///workspace/ai-coding/ai_coding/frontend/src/component/message.tsx#L74-L80)，
  表格受支持。但有先例注释说明"嵌套 markdown renderable 没有可生长的布局"
  ([`ask.ts:154-161`](file:///workspace/ai-coding/ai_coding/frontend/src/util/ask.ts#L154-L161))，
  这正是工具卡历史上退化为纯文本的原因。

### §1.2 为什么 (a) 而不是 (b)

卡片本身已经**结构化列出**了审批会写入的条目（步骤 + verify + 依赖），所以卡里再渲染一遍
Markdown 步骤表是**同一份信息的第二次呈现**——用户说的"重复且廉价"就是这个。
(a) 的目标不是"把 Markdown 渲染得更漂亮"，而是**让 detail 只承担条目表给不出的东西**
（为什么这么做、风险、取舍），并把残留的表格/标题在纯文本里排整齐。

### §1.3 修法

1. `planText`（[`ask.ts:162-174`](file:///workspace/ai-coding/ai_coding/frontend/src/util/ask.ts#L162-L174)）
   扩展**两件事**：把 pipe 表格按列宽对齐成等宽表格（纯字符串处理，不引入新依赖）、
   丢弃 `---` 分隔行。标题 `#` 它**已经**处理过，不重复实现。
2. 卡片里 detail 只显示"条目表之外"的部分：**按内容**判断（不是按行数）——如果 detail 里的
   表格块与 `items` 的 `content` 逐条相同，就剥离该表格块；其余段落（理由、风险、取舍）保留。
3. (b) 作为独立 spike：用 `createTestRenderer` 验证 `<markdown>` 在 scrollbox 里是否有内容高度；
   只有验证通过才替换。**不在本轮做**。

### §1.4 TDD 用例

- `planText` **现状**已去 `#`、列表符、反引号、粗体（[`ask.ts:162-174`](file:///workspace/ai-coding/ai_coding/frontend/src/util/ask.ts#L162-L174)）——
  新增的只有**表格对齐**与**丢弃 `---` 行**，用例只覆盖这两项 + 空输入不抛。
- 表格块内容与 `items` 逐条相同时被剥离；不同时保留。
- **必须同步改**：[`plan-review-render.test.tsx:86`](file:///workspace/ai-coding/ai_coding/frontend/test/plan-review-render.test.tsx#L86)
  现在断言画面含 `# 编排层改造计划`，套 `planText` 后必然失败（评审核实）。

---

## §2 执行期姿态后缀短暂不一致（`Build · 只读`）

### §2.1 机制（已核对）

- 前端把姿态**派生**在渲染时：
  `currentAgent()` = `state.agent ?? stats().agent`，
  `currentReadonly()` = 计划 agent 时恒 true，否则 `stats().readonly`
  ([`session.tsx:109-112`](file:///workspace/ai-coding/ai_coding/frontend/src/routes/session.tsx#L109-L112))。
- `stats()` 只在 `refreshPosture()` 里刷新，调用点：mount、`turn.finished`、
  **`session.command` 的 ack**、reload
  ([`session.tsx:191`](file:///workspace/ai-coding/ai_coding/frontend/src/routes/session.tsx#L191))。
- `/plan run` 的 handler 是 `spawn(...)` 后**立即回 ack**，所以那次 `refreshPosture()` 读到的是
  "还没翻姿态"的旧 stats。之后 [`commands.py:589-596`](file:///workspace/ai-coding/ai_coding/app/commands.py#L589-L596) 调
  `commit_posture`，agent_loop 只在 **agent 变化时**发 `AgentSwitched`
  ([`agent_loop.py:719-729`](file:///workspace/ai-coding/ai_coding/core/agent_loop.py#L719-L729))，
  而事件 payload 只有 `from/to/reason`，**没有 `readonly`**
  ([`bridge.py:121-128`](file:///workspace/ai-coding/ai_coding/server/bridge.py#L121-L128))。
  reducer 只改 agent → readonly 仍是旧的 → 显示 `Build · 只读`，直到该命令自己的
  `turn.finished` 才自愈。
- 附带缺陷：**只翻 readonly 不换 agent**（`/readonly`）时，`commit_posture` 一个事件都不发。

### §2.2 修法（选 a）

1. `AgentSwitched` 增字段 `readonly: bool | None = None`
   （[`events.py:57-69`](file:///workspace/ai-coding/ai_coding/core/events.py#L57-L69)），
   默认 `None`。
2. **桥接必须"None 时不写键"**，否则既有精确断言会红：
   [`test_m1b_agent_history.py:49-56`](file:///workspace/ai-coding/tests/test_m1b_agent_history.py#L49-L56)
   断言 `to_wire(...) == {"type": "agent.switched", "data": {from, to, reason}}`。
3. 发送条件从"agent 变了"改成"**agent 或 readonly 变了**"，payload 带 `readonly`。
   **但 `agent_history` 仍只在 agent 变化时追加**——否则
   [`test_posture_result.py:158-163`](file:///workspace/ai-coding/tests/test_posture_result.py#L158-L163)
   的 `readonly-only 变化 → agent_history == []` 会红，而且前端会把只读开关渲染成"Switched agent"。
4. 前端需要**新增状态字段**：`SessionState` 目前没有 `readonly`
   （`state/session.ts:129-137`，`Action` 只有 `agent-synced`），
   `currentReadonly()` 读的是 `stats()`。要补：字段、`initialState`、`agent-synced`/新 action、
   replay 水合；`from == to` 时不追加 notice。
5. 事件名不变，replay 读 `agentHistory` 时忽略新字段。

### §2.3 TDD 用例

- `/plan run` 后存在 `readonly=false` 的 `agent.switched`（**不要**断言它相对 ack 的到达顺序：
  spawn 后立刻回 ack，事件在子命令里异步发出，顺序是竞态——评审核实）。
- 只 `/readonly` 时发事件，但 `agent_history` 不增长、不产生 `Switched agent` notice。
- `to_wire(AgentSwitched(...))` 在 `readonly is None` 时**不含**该键；带值时含。
- reducer：`agent.switched {to:"build", readonly:false}` → `currentReadonly()` 为 false，不再读 stats。

---

## §3 `update_goal` 误用（`no_goal`）

### §3.1 机制（已核对）

- arming 条件：`task.armed and task.armed_profile == "goal"`
  ([`goal_tools.py:177`](file:///workspace/ai-coding/ai_coding/tools/goal_tools.py#L177))。
- 计划执行**先写入** `armed_profile = "plan"`
  ([`plan_runner.py:602-604`](file:///workspace/ai-coding/ai_coding/core/plan_runner.py#L602-L604))，
  所以计划回合里调 `update_goal` **必中** `no_goal`。
- 工具却一直在工具表里：主会话无条件注册（[`builder.py:357-365`](file:///workspace/ai-coding/ai_coding/tools/builder.py#L357-L365)），
  只有 `_PLAN_STRICT_DENY` 在 strict 计划模式才裁剪，而 `plan_strict_readonly` 默认 False；
  执行时 posture 已回 build，更不会裁。
- **撞词**：计划步骤 seed 直接喂 `[计划目标] {task.goal}`
  ([`plan_runner.py:905`](file:///workspace/ai-coding/ai_coding/core/plan_runner.py#L905))，
  `plan.updated` 也带 `goal` 键 → 模型把"计划目标"当成"running goal"。
- 提示词 methods 表与 plan 段**均不提 goal**，没有提醒。
- TUI 三次放大：`toolmeta` 未登记 `no_goal` → 落到"未知原因（no_goal）"；
  ledger 对 `count == 1` 就给 `level="medium"`，文案还写"**反复失败（1 次）**"
  （[`ledger.py:94-106`](file:///workspace/ai-coding/ai_coding/domain/ledger.py#L94-L106)，语病）。

### §3.2 修法（b + c）

1. **剔除 goal 工具的正确落点**（评审纠正）：计划**步骤回合**用的是新建的
   `SessionData`（[`plan_runner.py:866-885`](file:///workspace/ai-coding/ai_coding/core/plan_runner.py#L866-L885)），
   默认 `agent="build"`、`readonly=False`，**不挂任何 plan 规则**——所以"在 plan 规则里排除"
   等于什么都没做。真正的落点是 `_surface_tools`
   （[`agent_loop.py:3077-3106`](file:///workspace/ai-coding/ai_coding/core/agent_loop.py#L3077-L3106)）
   加一条**task 状态谓词**：`session.task.armed_profile == "plan"` 时不提供
   `start_goal`/`update_goal`。注意该函数的结果参与请求字节，谓词要进 surface 的缓存键，
   否则同一会话两次请求的 tools 段会不一致（破坏缓存命中）。
2. **文案**：`update_goal` 描述结尾**已经**是 "Available only while a goal is running."，
   `NO_GOAL` 也**已经**写"先 start_goal 立案"（[`goal_tools.py:47-50,132-142`](file:///workspace/ai-coding/ai_coding/tools/goal_tools.py#L47-L50)）——
   不必再加。真正该改的是 seed 撞词：`[计划目标]` → `[计划标题]`
   （[`plan_runner.py:905`](file:///workspace/ai-coding/ai_coding/core/plan_runner.py#L905)）。
3. **降噪**：
   - `toolmeta` 新增 `no_goal: {label: "无运行中的目标", tone: "denied"}`，并加前端断言；
     这个 `toolFailure` **同时**喂错误徽标与 ledger，一处改两处生效。
   - ledger **只改文案不改等级**：`count == 1` 时说"调用失败"而不是"反复失败（1 次）"；
     `level` 的 `count >= 2 or failed_streak >= stall_threshold` 规则保持不变，
     否则 [`test_ledger.py:31`](file:///workspace/ai-coding/tests/test_ledger.py#L31) 的
     `count==1 → medium` 会红，且丢掉 `stall` 分支。

### §3.3 TDD 用例

- **计划步骤回合**的 tool schema 不含 `update_goal`（这条是回归点：原来写"plan 规则"测不出来）。
- 缓存键：armed_profile 从 goal 切到 plan 时，tools 段发生变化（同一会话）。
- `toolmeta(no_goal)` → label「无运行中的目标」、tone `denied`。
- ledger：单次失败文案为"调用失败"，`level` 仍为 `medium`（既有断言不变）；两次才是"反复失败（2 次）"。
- seed 含 `[计划标题]`，不含 `[计划目标]`。

---

## §4 计划计数口径

### §4.1 机制（已核对）

`PlanReport.render()`（[`plan_runner.py:514-542`](file:///workspace/ai-coding/ai_coding/core/plan_runner.py#L514-L542)）：

- 头条 `完成 N 步` = `completed` 中 **`attested == True`** 的条数；
- `(含 M 步分步外完成，未经分步验收)` = `attested == False` 的条数；
- `已完成：1, 2` = **全部** `completed`（含 fiat）。
- `Todos n/m` 数的是**任务条目状态**，fiat 完成也算（`message.tsx:96,117`）。

于是 fiat 一步、分步结算一步时，头条说"完成 1 步"、同屏 "Todos 2/2"、`已完成：1, 2`——
三个数字都"对"，但读者无法分辨，读起来就是矛盾。
`R1-5b` 把头条改成只数 attested 是**为了不撒谎**（曾出现"完成 4 步 / 0 回合"），
方向没错，错在**没有把口径写出来**。

### §4.2 修法（选 a）

头条改成数**全部完成**，把"分步验收"作为限定语挂在后面：

```
[plan] 完成 2 步（其中 1 步未经分步验收）· 用了 1 回合 / 32528 tokens
已完成：1, 2
```

理由：`Todos 2/2` 与头条一致；"其中 1 步未经分步验收"仍然把 provenance 说清楚，
不会回到"完成 4 步 / 0 回合"的假象（那种情况现在会写成
`完成 4 步（其中 4 步未经分步验收）· 用了 0 回合`，自曝其短）。

### §4.3 TDD 用例

- fiat 1 + 分步 1 → `完成 2 步（其中 1 步未经分步验收）`。
- 全 fiat 4 → `完成 4 步（其中 4 步未经分步验收）· 用了 0 回合`。
- 全分步 2 → `完成 2 步`（无括号）。
- **必须同步改的既有断言**（评审核实）：
  [`test_plan_attestation.py:75`](file:///workspace/ai-coding/tests/test_plan_attestation.py#L75)（`分步外完成` 文案）、
  `:130`（`完成 0 步`）、`:131`。改文案是有意变更，提交信息要写明这是对 R1-5b 措辞的调整、
  **不是**把 fiat 算成本轮工作。
- 口径细节（评审补充）：代码里 `attested_of.get(step_id, True)` 把"缺收据"当已验证，
  而 `unattested` 数的是 **delivery 条数**而不是 completed 条数。
  文案里"其中 M 步未经分步验收"必须沿用 delivery 口径，否则与 `unattested` 对不上。

---

## §5 长中文行右缘的 `�`

### §5.1 机制（部分确认）

- 正文用 `<markdown>` / `<text>`，`wrapMode` 默认 `"word"`；换行与宽度计算在
  **opentui 的预编译原生库**里（`@opentui/core-linux-x64`），**无源码可查**。
- 应用层嫌疑有限：`get-east-asian-width`/`string-width` 只用于路径截断，且按
  `Intl.Segmenter` 切，安全；只有截断 **astral 字符**（emoji / CJK 扩展 B）才会产生孤代理。
  纯 BMP 中文不会。因此"长中文正文右缘"基本可排除应用层。
- 三个可能：opentui 原生折行在末列切断了宽字符；tmux 网格占位；`TextDecoder` 非 fatal 吞坏字节。

### §5.2 修法：先判别，再决定

实验（不猜）：

1. `tmux pipe-pane -o 'cat > /tmp/raw.bin'` 抓**原始 PTY 字节**，搜 `EF BF BD`（U+FFFD）：
   有 → opentui/原生；无而 `capture-pane` 有 → tmux 或抓取工具。
2. 用 `@opentui/core/testing` 的 `createTestRenderer` + `captureCharFrame()`，
   width=80 渲染 200 汉字一行；再关 `wrapMode="none"` 对照。
3. `OPENTUI_FORCE_WCWIDTH=1` 与默认宽度法对比，并用纯 ASCII / emoji 行区分是否宽字符相关。

结论决定动作：

- 若为应用层/可绕：在渲染前按 `string-width` **预折行**（插 `\n`），把宽字符永远放在行首，
  代价是失去 opentui 的软换行，需要保证与滚动宽一致。
- 若为 opentui 原生：记录最小复现，提 issue；应用层加"行尾留一列"的 padding 作为规避。
- 若为 tmux：**不改代码**，在评测报告里注明截图管线限制（`capture-pane` 对宽字符网格的已知行为）。

> 本轮这一项的**交付物是判别结论 + 最小复现**，不是修复（评审意见：不要让一个尚无结论的实验
> 阻塞其余批次的发布）。真出现应用层可绕的证据，再单开一批修。

### §5.3 本轮判别结论（已做）

最小复现：[`r21-cjk-wrap.probe.tsx`](file:///workspace/ai-coding/ai_coding/frontend/test/r21-cjk-wrap.probe.tsx)
（`bun test/r21-cjk-wrap.probe.tsx`，不进门禁）。`createTestRenderer` width=80、6 行，
同一段文本分别走 `<text>`（默认 word）、`<text wrapMode="char">`、`<text wrapMode="none">`、
`<markdown>`，三种语料：200 汉字 / 纯 ASCII / 120 emoji。

| 语料 | text(word) | text(char) | text(none) | markdown |
|---|---|---|---|---|
| 200 汉字 | 一行，未换行 | 一行，未换行 | 被网格切开 | **按 80 列换行** |
| ASCII | 按空格换行 | 按空格换行 | 不换行 | 按 80 列换行 |
| emoji | 一行，未换行 | 一行，未换行 | 被网格切开 | 一行，未换行 |

- **U+FFFD 计数全为 0**：三种语料 × 四种模式，捕获帧里没有任何替换字符。
  上一轮截图里的 `�` **不是应用层或 opentui 渲染产生的**，而是截图管线（ANSI→PNG 的字形覆盖 /
  末列截断）的产物。`/tmp/shots/*.ansi` 里也搜不到 `EF BF BD`。
- 但发现一个**真实缺陷**（与 `�` 无关）：`<text>` 对没有空格的 CJK 长串**既不按词也不按字符换行**
  （`wrapMode="char"` 在本版本对 CJK 同样不生效，emoji 也是），整行溢出视口；
  `<markdown>` 正常按列换行。转录正文用的正是 `<markdown>`，所以主界面没事；
  受影响的是仍用纯 `<text>` 渲染长中文的卡片（如计划评审卡的 detail、问题行）。
- **动作**：本轮不改（按 §5.2 的约定）；后续批次若修，两条路都可行——
  (a) 这些长中文段落改用 `<markdown>`（与转录同一条路径，已有换行证据）；
  (b) 渲染前用 `string-width` 预折行插 `\n`。优先 (a)，因为它不需要维护第二套宽度计算。

---

## §6 引用覆盖的残余误差：位置 ≠ 内容

### §6.1 问题

证据表记的是"读过哪几行"，不记"那几行当时是什么"。文件在两次读之间被改写 / 被 `mv`，
引用仍然"完整包含"，但引文在那些行上已不存在。

### §6.2 已调研的方案

| 方案 | 成本 | 能证明什么 | 不能证明什么 |
|---|---|---|---|
| **查时回读磁盘**（推荐） | 每答案 1–3 次文件打开，≤几百行；0 token | 引文此刻在该区间内 | 读时到答案时之间的漂移（用 mtime 兜） |
| 读时存指纹（每段 32B SHA-256） | ≈1.3KB/会话 | 文件**未变** | 引文真实性（仍要回读） |
| 读时存内容草图（zlib/每行 8B 哈希） | 160–320KB/会话，JSON 膨胀 10–20× | 纯内存可判子串、抗改名 | 读后漂移 |

外部实践（ALCE / AttrScore / SelfCite / CiteFix / CiteGuard / RAGAS faithfulness）
都是"逐 claim 一次 NLI 或 LLM"或"删片段做消融"，**单条断言一次模型调用**的量级；
本项目的约束（48k 上下文、paging read、本地无 judge 模型）下不可行。
可落地的是 **quote-span containment**（归一化后子串匹配 + 模糊兜底），工业界已在用（Gloo / Quote Verify）。

### §6.3 修法（选 a）

**接入点必须新开一个函数**（评审核实）：现有
`unsupported_claims(marks, seen, claims, *, size_of)`（[`read_ledger.py:356-405`](file:///workspace/ai-coding/ai_coding/domain/read_ledger.py#L356-L405)）
**拿不到答案原文**，`ReadMark` 也只有 `path/start/end/total`；`FileStateTracker` 在
[`builder.py:266`](file:///workspace/ai-coding/ai_coding/tools/builder.py#L266) 建完即被丢弃、不持久化。
所以"复用已有快照"没有通道。改为：

```python
# domain/read_ledger.py
def unsupported_quotes(
    answer: str,
    marks: Sequence[ReadMark],
    reader: Callable[[str], str | None],   # path -> text, None = unreadable
) -> list[tuple[str, int, int, str]]:      # (path, start, end, 归一化后的引文)
```

- 在 `agent_loop._reject_unsupported_claims` 里、**三档判定通过之后**调用；
  第二次（`claim_retries` 已用完）直接放行并 `report_degraded`，不会与档 3 双重退回。
- `reader` 复用 `_line_count` 同样的界：4MB 上限、异常 → `None`（fail-open）。
- **不做**读时快照。文件被改写时，回读到的就是新内容，引文对不上就退一次；
  用户/模型重读后自然收敛。把 `mtime/size` 写进 `ReadMark` 会改 session JSON 契约，
  列为后续（届时 resume 也能判新鲜度）。
- 其余算法（取引文、归一化、`SequenceMatcher` 阈值、退回文案）保持 §6.3 原文。

### §6.4 TDD 用例

- 引文在区间内 → 通过。
- 引文不在区间、ratio 0.9 且连续 10 token → 通过（转述容忍）。
- 引文不在区间、ratio 0.4 → 退回一次；同回合第二次 → 放行 + degraded。
- 文件被改写（回读内容不同）→ 退回一次，提示"文件可能已修改或引文不在该区间"。
- 文件被删 / 超 4MB / 读失败 → fail-open（不阻断）。
- 中文引号 / 全角标点 / 行号前缀 → 归一化后仍命中。
- 档 3 已经退回过的同一回合，档 4 **不再**退回（复用同一个 `claim_retries` 预算）。

---

## §7 长时任务：本轮真机执行与失败根因

### §7.1 本轮怎么跑（已在进行）

- 任务：TaskFlow 全栈（FastAPI+SQLite+pytest / React+Vite / README），与历史失败**同一个任务**。
- 驱动：`scripts/eval_fullstack_task.py`（headless，真 AgentLoop，30 迭代/回合，14 回合）。
- **接管流量**：本地录制代理 `127.0.0.1:8899` 转发到 dashscope，逐条落
  `request(messages/tools/params)` 与 `response(status/elapsed/body_head)` 到
  `/tmp/record/llm.jsonl`；模型 `base_url` 指向它（`config.record.yaml`）。
- **接管会话**：`tracing.enabled=true` + `sessions/*.json` + `eval_task_log.json`。

### §7.2 本轮实测（第一轮：long-r21，未修 harness）

| 指标 | 数值 |
|---|---|
| 第 1 回合成败 | 637.8s / **94 次工具调用** / `be_pytest=1` / 无前端 |
| 模型请求总数 | 111（含 8 次 checkpoint 压缩调用） |
| 上下文曲线 | 17k → 80k → 19k → 44k → 52k → 41k → 50k → 35k → 57k（字符）**反复冲高后被压缩打回** |
| 失败表现 | `sqlite3.OperationalError: attempt to write a readonly database`（测试建表）→ 模型改内存库 → `no such table: tags/tasks` 连锁 |

**这轮的三个真根因（都实测过，不是推测）**：

1. **harness 把项目的 `pip install` 装进了 agent 自己的 venv。**
   `self_check` 用 `sys.executable`，而 `backend/requirements.txt` 钉了旧 pytest：
   实测 venv 里的 **pytest 8.4.2 → 7.4.4**，随后 `pytest_asyncio` 直接
   `ImportError: cannot import name 'FixtureDef' from 'pytest'` ——
   不只评测门塌了，**本仓自己的测试套件也一起塌了**（已修复：pytest 装回 8.4.2；
   harness 改为给项目建 `<ws>/.venv` 并用它跑 install/pytest）。
   这条能解释历史上"每轮 be_pytest 恒为 1"的一大半。
2. **产品默认 `process_sandbox.network = deny`，而任务要求 `pip install` / `npm install`。**
   bash 在 netns 里只有 loopback，模型自己的安装必然失败；真正把依赖装上的只有 harness 的
   gate（在沙箱外跑）。于是模型看到的是"安装失败"的错误，去修一个它无权修的问题。
   评测配置已改为 `network.mode: "off"`（**偏离产品默认，仅评测用**，见 §7.3）。
   顺带发现一个配置坑：YAML 里写 `mode: off` 会被解析成布尔 `false`，
   校验报 `Input should be 'off', 'deny' or 'proxy'`——必须加引号。
3. **"readonly database" 不是沙箱造成的。** 用真实 bwrap 构造（`enforce` + 工作区 bind）
   直接跑 sqlite 写入：**成功**。模型自己判断"工作区只读"并用内存库绕过，
   把一个环境假设错误变成了 `no such table` 的新故障。

### §7.3 评测协议要改的三件事

1. **环境预检 + 解释器隔离**：项目用**自己的 venv**（已改 harness）；跑前 dry-run 验
   `pip install -r` 与 `npm install` 的可行性，否则测的是网络与轮子，不是 agent。
2. **能力与任务对齐**：任务里要求安装依赖，就必须让 bash 有网络（或把任务改成纯 stdlib）。
   默认 `deny` 下这个任务**不可执行**，这是产品姿态与任务规格的冲突，不是模型的错。
3. **判据分级**：`install / test / build / deliverable(README)` 四个独立绿点，
   任一 `False` 都要能指认到层，而不是一个 `ok: false`。

### §7.4 第二轮：修完环境后的真机结果（long-r21b）

| 轮 | 秒 | 工具 | be_pytest | fe_build | 文件 |
|---|---|---|---|---|---|
| 1 | 177.8 | 27 | **0（绿）** | – | 16 |
| 2 | 163.2 | 49 | **0（绿）** | 1 | 28 |
| 3 | 74.8 | 52 | 0 | 1 | 31 |
| 4 | 16.3 | 52 | 0 | 1 | 31 |
| 5 | 16.1 | 52 | 0 | 1 | 31 |
| 6 | 16.1 | 52 | 0 | 1 | 31 |

**结论：一半成功、一半死锁。**

- 历史上"长任务必失败"里，**后端那一半是环境**：修好 venv 与网络后，
  第 1 回合 177.8s / 27 次调用就把 FastAPI+SQLite 后端与 pytest 做到**全绿**
  （对比第一轮 637.8s / 94 次调用仍红）。
- 剩下的失败是**产品缺陷**，不是模型的"能力"：第 4–6 回合每次只有 16s、工具数冻结在 52，
  说明这三轮一个工具都没调，模型每轮都被同一个请求打回。

**新根因（本轮用流量抓到的，之前没人看到）**：

```
HTTP 400  {"error":{"message":"<400> InternalError.Algo.InvalidParameter:
           The \"function.arguments\" parameter of the code model must be in JSON format."}}
```

落到具体消息：历史里第 87 条 assistant 的 `write` 调用，`arguments` 是**被截断的 JSON**
（`Unterminated string starting at: line 1 column 95`）——`max_tokens=4096` 把一次
`write KanbanBoard.tsx` 的输出切断了。

链路：

1. `ai/toolcalls.py::finalize_tool_calls` 只把**空**arguments 兜底成 `{}`，
   被截断的字符串原样通过（`arguments=bucket.get("arguments") or "{}"`）。
2. 这条 assistant 消息带着非法 JSON 进入历史。
3. 之后**每一次**请求都因它被 provider 拒绝（400）。
4. `ai/openai_engine.py:154` 只在错误文本命中 overflow 标记时判为
   `CONTEXT_OVERFLOW`；这条是 `invalid_parameter`，于是判 `FATAL` →
   原样重试 4 次 → 回合死亡。压缩救不了它（内容合法，只是 JSON 坏了）。

**修法（并入 §10）**：

- 入历史前校验 `arguments` 是 JSON；不合法就不执行、不落库，改成一条
  "你的 tool call 被截断，请拆分" 的 system/工具结果。
- `finish_reason == "length"` 且存在 tool_calls 时，**不执行**并明确告知。
- 发请求前做一次**历史自检**（assistant.tool_calls 全部 JSON 可解析）；
  不合法则修复为 `{}` 并配对一条说明，保证"坏历史"不会永久毒化会话。
- 400/422 里出现 `function.arguments` 时，判为**可恢复**（修复后重试一次），
  而不是重试同一个坏请求 4 次。

### §7.5 第三轮：修完 §10 之后，长任务第一次跑通（long-r21c）

同一套 harness、同一份 config、流量仍走录制代理（`/tmp/record/llm.jsonl`）：

```
.venv/bin/python scripts/eval_fullstack_task.py \
    --config config.record.yaml \
    --workspace /workspace/eval-workspace/long-r21c --rounds 8
```

| 轮 | 秒 | 工具 | be_pytest | fe_build | 文件 |
|---|---|---|---|---|---|
| 1 | 126.9 | 23 | 1 | – | 15 |
| 2 | 128.8 | 41 | 1 | – | 16 |
| 3 | 206.4 | 68 | **0（绿）** | 2 | 24 |
| 4 | 266.2 | 103 | 0 | 2 | 33 |
| 5 | 82.6 | 111 | **0（绿）** | **0（绿）** | 39 |

**`TASK COMPLETE at round 5`，total 846.5s。** 交付物已复核：
`backend/` 的 `pytest -q` 现在仍是 **10 passed**，`frontend/dist/` 已产出。

和 long-r21b 的对比就是这次修复的判据：

| | long-r21b（修前） | long-r21c（修后） |
|---|---|---|
| 工具数轨迹 | 27 → 49 → 52 → **52 → 52 → 52** | 23 → 41 → 68 → 103 → 111 |
| 每轮耗时 | … → **16.1s ×3** | 82.6s（最后一轮仍在干活） |
| 结果 | ROUNDS EXHAUSTED，前端没动 | 前后端全绿，任务完成 |

**新守卫在真机流量里被触发了一次**（`transcripts/summary_3483edee71e9.txt:1157`）：

```
[tool:subAgent] {"decision": "deny", "reason": "invalid_arguments", "tool": "subAgent"}
error: 工具调用的参数不是合法 JSON 对象，本次调用未执行。请修正后重发；
       如果是一次写了太多内容导致被截断，请把内容拆小或分段写入（write 建文件、edit 续写）。
```

紧接着的下一步是一次正常的 `bash` 调用——**模型读到了"怎么改"，会话继续**。
这正是修前会毒化整轮、且之后每一轮都重发同一份坏历史的那条路径。
- 400/422 里出现 `function.arguments` 时，判为**可恢复**（修复后重试一次），
  而不是重试同一个坏请求 4 次。

---

## §8 实施顺序

| 批次 | 内容 | 依赖 |
|---|---|---|
| **0** | **§10 坏 tool_call 毒化会话**（长任务实测抓到，优先级最高） | 无 |
| 1 | §4 计数口径（1 行文案 + 3 个既有断言） | 无 |
| 2 | §3 toolmeta/ledger 文案 + seed 改名 | 无 |
| 3 | §3 `_surface_tools` 的 plan-armed 谓词 + 缓存键 | 批次 2 改完更易测 |
| 4 | §1 planText 表格对齐 + 按内容去重 | 需同步改 `plan-review-render.test.tsx:86` |
| 5 | §2 posture 事件（events + bridge + 前端 state 字段 + reducer） | 独立性最差，放后面 |
| 6 | §6 `unsupported_quotes`（新函数 + 接 `claim_retries`） | 需先定 `reader` 的界 |
| 7 | §5 判别实验（只出结论与最小复现） | opentui test renderer |
| — | §7 长任务结果与根因报告 | 本轮跑完后写 |

每批都必须：`pytest` 全绿、`ruff`/`mypy` 干净、前端 `bun test` + `tsc`、相关变异至少覆盖新逻辑。
批次 7 例外（只出结论，不进发布门禁）。

**进度**：批次 0–7 已全部完成。

| 批次 | 状态 | 证据 |
|---|---|---|
| 0 坏 tool_call | 完成 | `tests/test_tool_call_arguments.py`（10 例）、`scripts/mutate_r21.py` L1–L8 |
| 1 计数口径 | 完成 | `test_plan_attestation.py`（含新增混合口径例）、P1/P2 |
| 2 no_goal 降噪 | 完成 | `test_ledger.py`、`test_plan_m1.py`、`toolmeta.test.ts`、P3/P4 |
| 3 surface 谓词 | 完成 | `tests/test_surface_tools_armed.py`（5 例）、S1/S2 |
| 4 planText 表格 | 完成 | `ask.test.ts`（9 例）、`plan-review-render.test.tsx:86` 已改 |
| 5 posture 事件 | 完成 | `test_posture_result.py`、`test_m1b_agent_history.py`、`agent-picker.test.ts`、A1–A5 |
| 6 引文内容校验 | 完成 | `test_read_ledger.py`（9 例）、`test_claim_check.py`（4 例）、Q1–Q6 |
| 7 中文右缘判别 | 完成（只出结论） | 见 §5.3，probe 不进门禁 |

`scripts/mutate_r21.py` 覆盖后端 pytest 与前端 bun 两侧，**25 条变异全部 killed**。

**环境注意**：本仓测试必须用 `.venv/bin/python`（pytest 8.4.2）；系统 pyenv 的 pytest 9.1.1
会让 `tests/test_mcp_oauth.py` 8 例假红（与本轮改动无关，已在 HEAD 上复现）。

---

## §9 评审结论

独立子智能体逐文件核对，报出 13 条（2 blocker / 5 major / 6 minor），**全部已并入上文**：

| # | 发现 | 级别 | 处置 |
|---|---|---|---|
| 1 | §3 的落点写错了：计划步骤回合是新建 `SessionData`（`agent=build`、无 plan 规则），"在 plan 规则里排除"等于没改 | blocker | §3.2 改为 `_surface_tools` 的 `armed_profile == "plan"` 谓词 + 缓存键 |
| 2 | §2 的 `AgentSwitched.readonly` 会打破精确 wire 断言；前端 `SessionState` 根本没有 `readonly` 字段 | blocker | §2.2 补"None 不写键"、`state.readonly` 字段/水合/replay |
| 3 | 放宽发事件条件会把 `/readonly` 变成 `build→build` 噪声，且污染 `agent_history` | major | §2.2 明确历史只按 agent 变化追加；`from == to` 不发 notice |
| 4 | §4 的既有断言没列全，且计数口径说得比代码准 | major | §4.3 列 `:75/:130/:131`，并按 delivery 口径写文案 |
| 5 | §6 的"档 4"接不进现有函数：`unsupported_claims` 没有答案原文，`FileStateTracker` 建完即弃 | major | §6.3 改为新纯函数 `unsupported_quotes(answer, marks, reader)` |
| 6 | §1 会打破 `plan-review-render.test.tsx:86`（断言画面含 `#`）；`planText` 已去 `#` | major | §1.3/§1.4 收窄到"表格对齐 + 丢 `---`"，并列出要改的测试 |
| 7 | §1 的"按行数去重"过弱，会误删理由段 | minor | 改为按 `items.content` 内容比对 |
| 8 | §3 的 ledger 降噪会打破 `test_ledger.py:31`（`count==1 → medium`），且丢掉 stall 分支 | major | §3.2 改为只改文案、不动等级规则 |
| 9 | §3 的文案其实已经在仓库里（description 与 `NO_GOAL` 都写了）；真正该改的是 seed 撞词 | minor | 删除重复文案，只改 `[计划目标]` → `[计划标题]` |
| 10 | `toolmeta` "显示原文"不符合现有短标签约定 | minor | 改为 `no_goal: {label:"无运行中的目标", tone:"denied"}` |
| 11 | 事实小错：`commands.py:591` 是 `request_posture`（排队），落盘/发事件在 `:631-637` 的 `commit_posture`；`FileSnapshot` 在 `file_state_tracker.py:20-23` | minor | 已在 §2/§6 改正引用 |
| 12 | "事件在 ack 之后到达"不可稳定断言（spawn 后立即回 ack） | nit | §2.3 改为"存在带 readonly 的事件"，不断言顺序 |
| 13 | §5 本轮只出实验、不出修复，不该套发布门禁 | nit | §5.2 末尾已注明；§8 批次 7 例外 |

**评审总评**：§1/§4 的文案改动本身安全，但必须同步改两处既有测试；§2 必须先补齐桥接与前端状态字段；
§3 的核心修法必须换落点；§6 必须先改接口。按 §8 的顺序实施即可。

---

## §10 长任务暴露的新缺陷：一个坏 tool_call 毒化整个会话

**不在原 6 项内，但优先级最高**——它是本轮长任务死锁的直接原因，而且任何长任务都会撞上。

### §10.1 现象

一轮 `write` 的文件内容较长，`max_tokens=4096` 把 `function.arguments` 截断；
这条 assistant 消息带着非法 JSON 进入历史后，**之后每一次请求都被 provider 拒绝**：

```
HTTP 400 InternalError.Algo.InvalidParameter:
The "function.arguments" parameter of the code model must be in JSON format.
```

重试 4 次 → 回合死亡；下一轮把同一份历史再发一次 → 再死。实测第 4–6 回合
每轮 16s、工具数冻结在 52。

### §10.2 三处该挡而没挡

| 位置 | 现状 | 后果 |
|---|---|---|
| `ai/toolcalls.py::finalize_tool_calls` | 只把**空** arguments 兜底成 `{}`，截断的字符串原样通过 | 坏 JSON 进入 `ToolCallRef` |
| 工具执行 / 落库 | 没有"arguments 不是 JSON → 不执行"的分支 | 半截参数被当成一次真实调用记录 |
| `ai/openai_engine.py:154` | 只有命中 overflow 标记才判 `CONTEXT_OVERFLOW`，其余 400 = `FATAL` | 同一个坏请求被重试 4 次，而不是修复 |

### §10.3 修法（四层，缺一不可）

1. **入口校验**：`finalize_tool_calls` 之后立刻 `json.loads`；失败则产出
   `ToolCallRef` 的"invalid"标记（或直接不产出），**不执行、不落库**。
2. **截断感知**：`finish_reason == "length"` 且存在 tool_calls → 一律不执行，
   给模型一条"你的调用被输出上限截断，请把文件拆小或分段写"的反馈。
3. **发送前自检**（兜底）：构造请求时扫描 `assistant.tool_calls`，任何不可解析的
   `arguments` 就地替换为 `{}`，并给该调用补一条配对的说明性 tool 结果，
   保证历史永远合法——**旧会话/旧日志里的坏历史也能被救回来**。
4. **错误分类**：400/422 且文本含 `function.arguments` → 判可恢复，
   先跑第 3 步再重试一次；不要原样重试。

### §10.4 TDD 用例

- 截断的 `arguments` 不产生可执行调用；产出中文反馈而不是异常。
- `finish_reason == "length"` + tool_calls → 不执行（即使 JSON 恰好完整）。
- 历史里塞一条坏 `arguments` → 发出的请求里全部 `arguments` 可解析，且配对结果存在。
- 400 `function.arguments` → 判定为可恢复，修复后只重试一次。
- 回归：正常调用不受影响（`toolcalls` 现有用例全绿）。
