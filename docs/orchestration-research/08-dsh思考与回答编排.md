> 来源：并行调研 agent 产出（只读调查），高级工程师复核；供"编排层认知维度"调研使用。
> 主题：用户输入之后，dsh 如何编排模型的"思考"与"回答"（推理管线/塑形提示词/外部化思考/收束/干预/跨压缩）。
> 未确认项保留在文末；姊妹篇：`02`（数据流）、`05`（长任务）。

# dsh「思考与回答的编排」专项调研（用户输入之后，模型怎么想、怎么答）

> 仓库：`/home/linsir365/projects/deepseek-harness`，HEAD `571902028a75c64c1d767f4ae5fd099485f0ddf0`。
> 方法：只读代码；行号相对仓库根，以当前 HEAD 为准。
> 不重复：`/tmp/opencode/dsh-orchestration-flow.md`（单回合数据流）与 `/tmp/opencode/dsh-longtask-research.md`（长任务）。本文只答一个维度：**推理/思考的管线、提示词、外部化机制、收束、回合内干预、跨压缩持久化**。
> 每处不确定都标注 **【未确认】**；全表在 §10。

---

## 0. 一句话结论

dsh 对"思考"的态度是**一等公民的透明管道**：reasoning 块从 provider 流进 `ContentBlockMap`，**原样落日志**（含逐 delta 时序），**展示给用户**（折叠的 Think 行 + 运行中组标题），并在同一 provider/model 下**带签名回喂**给下一次请求（无法回喂时降级为无签名文本，而不是丢弃）。"如何想"主要不是靠一段 system prompt 教出来的，而是靠**环境事实**（runtime-context 快照）、**工具提示词 section**、以及**外部化思考的原语**（plan 模式、goal 轮询提示、检查点摘要、重复调用提醒）来塑形。没有"只思考没回答"的专门重试；唯一的空回答防线是 0 block 的 `EMPTY_RESPONSE`。

---

## 1. Q1 推理/思考内容的管线

### 1.1 类型：chunk 与 block

- 思考 block 是核心块类型 `ReasoningBlock { type:'reasoning'; text }`，与 `text/tool-call/image/file/tool-addition/tool-removal` 同属 `ContentBlockMap`：`packages/llm/llm/src/types.ts:61-71`（ReasoningBlock:67-71）、`:137-150`（Map 与 ContentBlock）。
- 流协议 `StreamChunk` 有 `reasoning-delta`（`types.ts:470-482`，见 :473）；`block-start.blockType`/`block-end.block` 也覆盖 `'reasoning'`（:471、:475）。
- 归集器 `BlockAssembler`：`reasoning-delta` 落到 blockType `'reasoning'`，组装成 `{type:'reasoning',text}`（`packages/llm/llm/src/assembler.ts:62-68、108-112`）。
- 会话日志内还保存**紧凑的逐帧流**：`AssistantStreamRecord` 的 `'reasoning-chunks'` 记录（`packages/llm/llm/src/assistant-stream.ts:19-47`，reasoning-chunks:28-34；打包逻辑 :113-128）。

### 1.2 存：落进会话日志（durable）

- 正常完成：step 收尾 `session.append('assistant/message', {message, usage, stream})`——`message` 含全部块（含 reasoning），`stream` 含 reasoning-chunks：`packages/core/agent-loop/src/agent.ts:537-546`；事件定义 `packages/core/session/src/types.ts:342-350`。
- 中断：turn 被取消时，已送达的 **text/reasoning 前缀**被固定为 `assistant/message`（`interrupted:true`），tool-call 一律丢弃：`agent.ts:462-486`；`BlockAssembler.interruptedBlocks()` 只保留非空白 text/reasoning：`assembler.ts:162-179`；语义注释 `session/src/types.ts:332-341`。
- max-tokens 截断时：assembler 丢弃不可安全执行的 tool-call，**保留 reasoning 与 text**（`assembler.ts:130-150`）。
- 失败尝试（无 surface 消息）落 `assistant/attempt`（同样携带完整流）：`session/src/types.ts:351-356`；agent.ts:487-509。
- 空的 assistant 消息（0 block，max-tokens 的纯 usage 宿主）`deriveMessages()` 不产出（`packages/core/session/src/index.ts:984-1005`；`surface.ts:138-144`）。**reasoning-only 消息非空，会照常进入转录**。

### 1.3 展示：给用户（两种形态）

- 完成后的折叠行：`ReasoningRow`（`packages/client/ui-chat/src/client/chat/ReasoningRow.tsx:1-60`，标题文案 `locale.ts:344` `message.think: 'Think'`）= 折叠摘要/展开全文；策略 `presentation-policy.ts:17-19`（组标题显示 reasoning 细节、settled 预览首行）。
- 运行中的"活"用途：UI 读取**当前正在流式输出的 reasoning 文本**生成过程组标题（`conversation-nodes/process-activity.ts:96-113` `liveReasoningDetail`）。
- 最终块投影：`toAssistantBlock` 把 `reasoning` 投影为 Chat 的 reasoning 块（`conversation-nodes/event-projection.ts:117`）；流式 delta 同样可见（:165-173）。
- 用量面板：`message.turnUsage.reasoning`（`locale.ts:185/383`）；但 `TokenUsage.reasoningTokens` 字段（`llm/src/types.ts:188`）两个内置适配器都不填充（pi-ai 把 reasoning 折进 output：`packages/llm/llm-pi-ai/src/stream.ts:24-32`；DeepSeek Messages usage 无该字段：`packages/llm/llm-deepseek/src/translate.ts:34-43`）→ **该 UI 行基本不显示**。
- 实时传输：每 chunk 以 `agent/assistant-stream` 帧发出（`agent-loop/src/assistant-stream.ts:59-71`；agent.ts:449），UI 订阅（旧报告 §6.2；follow 的 assistantStream opt-in）。

### 1.4 回喂：thinking blocks 回传下一次请求（是）

- **DeepSeek 原生适配器**（`dsh-llm-deepseek`，Messages API）：
  - 收：`thinking` block → reasoning（`translate.ts:49-54`）；`thinking_delta` → reasoning-delta（:73-77）；`signature_delta` 累进 replay block 的 `signature`（:78-81）。
  - 存：`finish` 携带 `replayState`（:161），最终落在 `AssistantMessage.source.replayState`（`agent.ts:529-536` 构造）。
  - 回喂：assistant reasoning → wire `thinking` + 签名（`serialize.ts:26-39`，签名 :31-34）；user/tool 消息里的 reasoning 会被**丢弃**（`serialize.ts:65-67`）。
  - 跨模型/跨 provider：签名不可移植，replay 校验退化为"无签名 thinking"（`llm-deepseek/src/replay.ts:46-63`）。
  - 思考开关与档位在请求体：`thinking:{type:enabled|disabled}` + `output_config.effort`（`serialize.ts:147-156`，档位 off/low/high/max）。
- **pi-ai 适配器**（多 provider：anthropic/openai/google/…，`packages/llm/llm-pi-ai`）：
  - 收：上游 pi-ai 统一事件 `thinking_start/delta/end` → block/reasoning-delta/block（`stream.ts:165-173`）。
  - 存：replay 包只存 provider 元数据——`thinkingSignature`（Anthropic 类）、`redacted`、`thoughtSignature`/`namespace`（工具调用侧）、`textSignature`，以及响应级 `providerThinkingLevel`（`replay.ts:15-35、77-110`）。
  - 回喂：`toPiAssistant` 用 replay 重建 `thinking`（`replay.ts:186-232`，:201-206）；无 replay 的"foreign"路径也把 reasoning 转成无签名 `thinking`（:151-184）；完全不可用时降级而不是失败（:247-259 `onDegrade`）。上下文转换 `context.ts:203-214`。
  - OpenAI 兼容端的怪癖由 catalog profile 开关表达（dsh 自己不解析 `reasoning_content`）：`requiresThinkingAsText`（thinking 必须以 `<thinking>` 文本走）、`requiresReasoningContentOnAssistantMessages`（推理开启时回放的 assistant 消息需要空 `reasoning_content`）、`thinkingFormat`、`supportsReasoningEffort`（`catalog.ts:391-417`；schema `config.ts:261-269`）。
- **归一总结**：DeepSeek 原生走 Messages 的 `thinking/signature`；Claude/OpenAI 等走 pi-ai（`thinking` + 各 provider 签名；OpenAI 系兼容参数由 profile flags 调）；dsh 侧统一物是 `{type:'reasoning',text}` + `ReplayEnvelope`（`llm/src/types.ts:441-460`）。

---

## 2. Q2 思考独有的失败模式：有没有"只思考没回答"的处理

**结论：没有针对 thinking-only 的专门机制。** 找不到任何"reasoning 有、text/tool-call 无"的重试、追问或校验；此类回合按正常 `stop` 收尾（`agent.ts:547-550` 无 tool-call → `{kind:'completed'}`），UI 因 reasoning 不算"回复内容"而不把它当 closing（§5.3），仅此而已。

dsh 实际有的防线是**更粗的空回答防线**：

- `EMPTY_RESPONSE`：0 个内容 block 的正常 `stop` 被适配器判为失败——DeepSeek：`translate.ts:149`（另 adapter.ts:164 无 body）；pi-ai：`stream.ts:96-107`（`message.content.length === 0`）。注意 **thinking block 计入 `content`，所以"只思考"不会命中**。
- 错误码语义与"安全重试"理由：`llm/src/error.ts:33-42`（"an empty message silently ends the turn with nothing for the user or the loop to act on… safe to repeat"）。
- 默认重试集合包含 `EMPTY_RESPONSE`：`llm/src/retry-policy.ts:14-24`。
- 执行者：`llm-retry` 插件（base bundle 装配 `base/cordis.patch.yml:99-100`）在 `agent/request-error` 上先落 `llm/retry`（durable）→ 退避 → `llm/retry-started` → 返回 `{kind:'retry'}`（`llm-retry/src/index.ts:157-191`；`RequestErrorAction` 定义 `core/agent/src/runtime-types.ts:122`）；loop 重试当前 step 的请求（`agent.ts:511-526`）。
- 中断路径的反面：abort 时若"连 text/reasoning 前缀都没有"就不造消息，只落 `assistant/attempt`（`agent.ts:463-486`）——这是"防止把思考残片当回答"的机制，但方向仍是保留而非重试。

---

## 3. Q3 塑造思考的提示词

### 3.1 section 体系（静态、进 system prompt）

section 次序常量表：`packages/core/system-prompt/src/index.ts:140-165`（HARNESS_IDENTITY=-1000、PLAN_POLICY=500、各 TOOL_*=1000+、DEPLOYMENT_PERSONA_SUFFIX=10200…）。教"怎么想/怎么工作"的 section：

| section | 文本 | 位置 |
|---|---|---|
| `harness:identity`（永远第一） | `You are an AI agent powered by DeepSeek Harness.` | `system-prompt/src/index.ts:449-455`（文本 :453） |
| `deployment:persona-prefix` | web/headless/acp：`You are a coding agent powered by the {{model}} model.`；base 默认 `''`；可被 agent preset 的 persona 行按 scope 覆盖 | `bundle/web-app/cordis.patch.yml:17-21`；`bundle/headless/cordis.patch.yml:9-12`；`bundle/acp-app/cordis.patch.yml:3-6`；`bundle/base/cordis.patch.yml:497-500`；覆盖机制 `preset/persona/src/index.ts:63-81`；preset 实例 `bundle/web-app/presets/standard.patch.yml:10-14` |
| `plan:policy`（仅 plan 模式激活时非空） | 见 §4.2（全文） | 注册 `plan/plan-mode/src/index.ts:216-224` |
| 工具 section（每个工具一条，教工作方法） | bash：`Check the [exit code: N] marker on every bash result; investigate failures before moving on.`；read：`Use the read tool — not shell commands like cat, sed, head, or tail…`；edit：`Read a file before editing it…`；jobs：`Track every background job id you start… do not busy-poll…`；subagent：`Start independent delegations … continue useful work while they run. When only waiting … end your turn instead of sleeping or polling…`；goal：见 §4.4；workflow：`Use the workflow tool ONLY when the user explicitly asks…` | `shell/tool-bash/src/index.ts:251-255`；`fs/tool-fs/src/read.ts:69-75`；`fs/tool-fs/src/edit.ts:77-83`；`jobs/tool-jobs/src/index.ts:251-255`；`subagent/tool-subagent/src/index.ts:368-381`；`goal/tool-goal/src/index.ts:115-124、189-192`；`workflow/tool-workflow/src/index.ts:325-331` |

注：**普通（非 plan）模式没有一段"总纲式"方法论提示词**——web 会话的 system prompt 里"怎么工作"主要由工具 section + persona 一句话构成；重方法论在 plan:policy、goal guidance、skill 正文这些**按需出现**的文本里。

### 3.2 runtime-context（动态、每步刷新，以 user 消息注入）

- 机制：`RuntimeContextProjection` 只在**快照变化**时提交一条 `runtime-context` user/message（`core/agent-loop/src/runtime-context.ts:146-163`；清空时注入 "Current runtime context: none…" :20）；每个 step 刷新渲染（`agent.ts:411-432`）。
- 快照 section 顺序常量：`system-prompt/src/index.ts:169-178`（WORKING_DIRECTORY=100、SANDBOX_POLICY=110、APPROVAL_POLICY=115、SUBAGENT_DELEGATION=120、HARNESS_SOURCE=200…）。
- 内容贡献者：
  - 工作目录：`session/working-directory/src/index.ts:90-95`；
  - 沙箱策略（三档句子，如 read-only："Do not refuse a required modification from this policy alone: try an available tool normally…"）：`sandbox/sandbox-policy/src/index.ts:42-50、142-150`；
  - 审批策略（`Approval prompts are disabled…do not request sandbox escalation` / `Approval policy: ask…fails closed`）：`interaction/user-approval/src/index.ts:73-75、163-172`；
  - 子代理深度/权限（"your permission scope was fixed…" 由 child-agent 注入）：`subagent/tool-subagent/src/index.ts:382+`；`subagent/subagent/src/child-agent.ts:205+`；
  - dev 环境（harness 源路径/Web 表面）：`context/harness-dev-context/src/index.ts:147、159`；team 身份（experimental）：`experimental/tool-agent-team/src/index.ts:178`。

---

## 4. Q4 外部化思考：把思考写进上下文的机制（含提示词原文）

### 4.1 `todo_write`（工具）

- 定义与行为：整单替换、三态、返回计数摘要（`todo/tool-todo/src/index.ts:135-211`；结果文案 :186-190 `Updated todo list: X pending, …`）。
- 落点：`todo/write` 事件；**"Log-only UI state; never derived history"**（`todo/tool-todo/src/types.ts:28-33`），投影在下一 `turn/start` 清空（`index.ts:127-131`，承旧长任务报告 §3）。即：写给"人看的进度 + 本回合内自我提醒"，不进系统上下文。
- 装配：web presets（`standard.patch.yml:112-114` 等，承旧报告）。

### 4.2 plan 模式（模式 + section + 审批闸）

- 模式状态与生效时机：`plan/mode` 日志 + 投影；pre-step 时把 pending 选择落盘并（仅用户切换时）narration（`plan/plan-mode/src/index.ts:196-213、448-459`；narration 文案 :462-474：`The user switched this session to plan mode.` / `…back to the default mode.`）。
- **注入提示词原文**（base 与 web 三 preset 相同；`bundle/base/cordis.patch.yml:332-346`（正文起 :336）、`bundle/web-app/presets/standard.patch.yml:51-65`（正文起 :55））：

```
You are in plan mode. Stay in plan mode until the user approves your plan through exit_plan_mode or switches the session mode. Imperative language to implement changes means plan the implementation, not execute it. A user's conversational agreement — including an answer confirming something you asked — approves nothing and does not end plan mode; fold the confirmed decision into the plan and submit it through exit_plan_mode.

Explore first. Use non-mutating reads, searches, static analysis, and checks to ground the plan in the actual repository. Do not edit or write files, change configuration, run formatters or code generation that rewrites tracked files, commit, or otherwise carry out the plan. Prefer existing functions and patterns over new machinery.

The tool catalog stays the same across modes for request-cache stability. These plan-mode rules override any later tool description or guidance that suggests using mutation tools; those tools remain listed only to keep the request shape stable. Do not use todo_write to track this planning phase: it tracks implementation after an approved plan, while the plan itself belongs in exit_plan_mode.

Resolve discoverable facts by inspection. Use ask_user_question only for user-owned choices or material ambiguity that inspection cannot answer. Do not ask the user where code lives or how current behavior works when you can find out.

Make the plan decision-complete: state the goal and success criteria; group implementation changes by subsystem; identify public API, schema, and data-flow changes; cover edge cases, failure modes, tests, acceptance criteria, and explicit assumptions. Keep it concise enough to review but detailed enough that another engineer can implement it without making design decisions.

When ready, call exit_plan_mode with the complete plan markdown, starting with a # title. Make exit_plan_mode the only and final tool call in that assistant response: it presents the plan for approval, and implementation begins only in a later step after approval. Do not paste the final plan as a plain reply or ask "should I proceed?" through prose or ask_user_question. If review rejects it, incorporate the feedback and present again. If the review channel is unavailable or aborted, stay in plan mode and ask the user to switch modes manually; do not proceed with implementation.
```

- 退出工具：`exit_plan_mode` 常驻注册（描述 `plan-mode/index.ts:83-86`），结果文案 `Plan approved — plan mode exited; carry out the plan starting with your next step.` / 驳回文案（:291-296）；用户 dismissed 时 `concludeTurn()` 直接收束本 turn（:336-339）。plan 文本要求 `#` 开头（:304-306）。

### 4.3 压缩检查点（compaction 提示词全文）

- 触发：pressure（pre-step，`compaction-basic/src/index.ts:158-176`）/context-overflow（request-error，:190-234）/手动 `/compact`（`compaction/command-compact/src/index.ts:67`）。
- **指令全文**（作为复现会话+工具的辅助调用的最后一条 user 消息，复用前缀缓存）：`compaction/compaction-basic/src/summarizer.ts:32-67`（结构如下，逐字）:

```
You are now acting as a compaction engine for this AI coding assistant. Condense the conversation ABOVE into a structured checkpoint that lets another model resume the work with no loss of essential context.

Output EXACTLY the Markdown structure below: keep every section, in order. Use terse bullets, not prose paragraphs. Write "(none)" for an empty section — never drop a section.

## Primary Request and Intent
- [the user's original and evolving goals; quote verbatim where the exact wording matters]

## Key Technical Concepts
- [technologies, frameworks, patterns, and conventions in play]

## Files and Code
- [exact path: why it matters, key changes or snippets]

## Errors and Fixes
- [error: how it was resolved, plus any related user feedback]

## Pending Jobs
- [explicitly requested work not yet completed]

## Current Work
- [precisely what was in progress at this checkpoint]

## Next Step
- [the single next action, directly in line with the most recent request, or "(none)"]

## Critical Context
- [decisions and their rationale, constraints, user preferences, open questions, data needed to continue]

Rules:
- Write concise English engineering prose. Preserve exact file paths, commands, error strings, identifiers, numeric values, function signatures, and syntax fragments.
- Capture user feedback and explicit instructions faithfully, especially corrections.
- Do NOT mention this summarization request or that the context was compacted.
- Output only the checkpoint text: do not call any tool or take any other action.
- If the conversation already contains a <compacted-summary> block, it is a PRIOR checkpoint. Do not copy it forward verbatim: preserve still-true facts, drop stale ones, and merge newer information into a single consolidated summary under the same structure.
```

- 落地帧（CHECKPOINT_PREAMBLE，`summarizer.ts:69-71`）：`This is an automatically generated checkpoint condensing an earlier span… Treat the captured context as established background and build on it without restating it. Continue the task directly from the messages that follow, without acknowledging this checkpoint.`；包 `<compacted-summary>…</compacted-summary>`（`summarizer.ts:21-23、188-194`）。

### 4.4 goal 轮询与收尾提示词

- **goal 轮提示词全文**（每轮 `agent.followup` 注入；`goal/goal-round-driver/src/prompt.ts:12-26`）：

```
<goal_round>
Objective: <JSON 编码的目标>
Round: n/maxGoalRounds

Continue working toward the objective in this same session. Treat the current workspace, tool results, and durable session state as authoritative; inspect them instead of assuming earlier narration is still current. Make concrete progress and verify the result. Before claiming completion, gather evidence that the whole objective is achieved, read the current goal, and mark it complete. If work remains, leave the goal active for the next round. Follow the configured goal-tool policy before reporting a blocker.
</goal_round>
```

调用点：`goal-round-driver/src/index.ts:190-208`（idle 后 followup；pre-step 校验 reservation :380-445）。
- goal 工具 guidance section（`tool-goal/src/index.ts:115-124`，"Mark complete only when the objective is actually achieved… difficulty, uncertainty, or useful remaining work is not blocked"）。
- **收尾提示词**：complete/blocked 后注入 wrap-up 指令（`tool-goal/src/wrapup.ts:17-40`）：`<goal_complete>…Write the closing message to the user now: state the outcome, summarize what was done and how it was verified…` / `<goal_blocked>…state what has been completed so far, describe the concrete blocking condition…`；统一 grounding 规则 "Report only what earlier rounds and tool results actually establish"（:5-7）。注入路径 `tool-goal/src/index.ts:319-330`（`exec.deferContext(...)`，经工具结果 additionalContexts 进下一步）。

### 4.5 其它"写给模型的元话术"

- **重复调用提醒**（guard）：相同工具+规范化参数连续第 3/5/8 次（可配）时，把提醒挂在 `tools/post-execute` 的 `additionalContexts` 上（下一步可见；被拒调用也计数）；文本：`You are repeating the exact same tool call with identical arguments. Carefully analyze the previous result before calling again…`（温和版 `guard/repeat-tool-reminder/src/index.ts:70-75`）与 detailed 版（工具名/次数/参数预览，"The repeated calls are not making progress…" :77-84）；用户插话即重置（pre-step :236-240）。
- **skill 目录**（user 消息，不进 system）：`<system-reminder> A skill is a reusable set of task-specific instructions… <available_skills> - name: desc…</available_skills> If the user names a skill, or the task clearly matches… call the skill tool…`（`skill/tool-skill/src/index.ts:260-284`；更新版 :286-318）；用户 `/name` 手势把技能正文作为 `skill-invocation` 指令注入（:179-212）。
- **后台作业完成通知**：`tool-jobs` 在 settle 时 wakeup(followup)/inject（`jobs/tool-jobs/src/index.ts:283-308`），配套 section："Track every background job id…do not busy-poll… Before giving a final answer, collect every still-relevant job with job_output…"（:251-255）。
- **审批/沙箱/工作目录**等 runtime-context（§3.2）。

---

## 5. Q5 回答的收束

### 5.1 loop 侧：turn 如何结束

- 模型不再请求工具 = 本 step `{kind:'completed'}`（`agent.ts:547-550`）；工具批存在 `concludesTurn` 标记时也可收束（`agent.ts:551-554`；标记 API `core/tools/src/index.ts:429-431`，使用者如 plan-mode dismissed :337）。
- turn 收尾点序：`step/end` → 若 `turnEnds` 且 next-step 空 → `agent/turn-stopping`（serial，可再 steer 续跑）→ `turn/end {reason}`（`agent.ts:357-390`；事件声明 `core/agent/src/runtime-types.ts:381`）。
- **没有** loop 级的"closing/handoff/总结提示词"。最终文本=转录里最后一条非空 text 的 assistant 消息，由**UI 定义**：`turn-tail.ts:57-69` `closing = finalized.findLast(hasText)`，非 closing 则禁用分支操作（:74-79）；"回复内容"定义排除 reasoning/tool-call（`contract/assistant-content.ts:10-17`）。

### 5.2 `agent/turn-stopping` 的监听者（全仓）

- 声明：`runtime-types.ts:381`。非测试监听者仅三处：
  1. `deliverables/workspace-changes`：回合收尾时抓 git 快照（`web-app/cordis.patch.yml:357-358` 装配；`workspace-changes/src/index.ts:155-157` `recorders.get(...).stopping(turn)`）——**只记录，不注入话**；
  2.、3. experimental `hooks-claude-code`（`src/index.ts:276`）/`hooks-codex`（`src/index.ts:263`）——可以 force continuation（旧长任务报告 §9）。
- 即：**默认监听者不做任何收尾话术**；goal 的"收尾话"是 wrap-up 提示（§4.4）而不是 turn-stopping 监听。

### 5.3 max-tokens 截断的收尾

- step 返回 `{kind:'max-tokens'}`（`agent.ts:547`），turn 级"粘性"（`agent.ts:336-343` 注释与逻辑）；tool-call 块在组装时被丢弃（`assembler.ts:130-150`）；goal 直接 disarm（`goal-round-driver/src/index.ts:349`）。
- 没有自动续写：UI 在 `turn/end {reason:'max-tokens'}` 处插一条提示行（`ui-chat/conversation-nodes/turn-max-tokens.ts:42-72`；文案 `locale.ts:174-175`：`已达到输出 token 上限` / `回答被截断，已有输出保留在对话中。发送"继续"可让模型接着输出。`）。

---

## 6. Q6 回合内的认知干预（harness 主动注入的"话"全清单）

除用户 steer/queue 外，harness 会在这些时点注入内容（按触发分类）：

**A. 每步/pre-step（在模型调用前落日志）**
| # | 注入 | 触发 | 位置 |
|---|---|---|---|
| 1 | `runtime-context` 快照（工作目录/沙箱/审批/子代理等） | 每个 step，内容变化才替换 | `agent.ts:411-432`；`runtime-context.ts:146-163`；贡献者见 §3.2 |
| 2 | plan 切换 narration（一句 notice） | pending 的 plan 切换在 in-turn 边界生效时 | `plan-mode/index.ts:196-213、448-459` |
| 3 | skill 正文（`skill-invocation`） | 用户消息以 `/name` 开头 | `tool-skill/index.ts:179-212` |
| 4 | skill 目录（`skill-catalog`，首见全量/变更全替） | 目录 digest 变化 | `tool-skill/index.ts:215-253、260-318` |
| 5 | AGENTS.md 类指令（baseline 首请求前；文件被 fs 工具触碰后刷新） | 首次请求 / `step/end` 汇总触碰路径 | `context/agent-instructions/src/index.ts:336-361`（pre-step 折入 claimed 批之后）；触碰收集 :84-96、:361-378 |
| 6 | 时间读数（默认 ≥10 分钟间隔） | 合格 step | `context/time-context/src/index.ts:185+` |
| 7 | tmux 位置上下文（每 turn 首个请求，状态变化才重发） | step==1 | `context/tmux-context/src/index.ts:246+`（模块注释 :1-17） |
| 8 | @ 会话引用（带"不可信快照"前缀的 user 消息） | 用户 @ 提及 | `context/session-reference/src/index.ts:138+`；前缀 :57-61 |
| 9 | goal 轮提示词 | idle + armed active goal 自动续轮 | `goal-round-driver/index.ts:190-208`；文本 §4.4 |
| 10 | 压缩（并非"话"，而是替换历史） | 压力阈值/上下文溢出 | `compaction-basic/index.ts:158-176、190-234` |

**B. 工具执行之后（经 `additionalContexts` 进 next-step inbox，`tool-calls.ts:157`、`agent.ts:553`）**
- 11. 重复调用提醒（`repeat-tool-reminder`，§4.5）；
- 12. goal wrap-up 指令（`tool-goal`，§4.4）；
- 13. job 完成通知（busy→inject；idle→followup，§4.5）；
- 14. 工具自定义附加上下文（如 fs-search 的 `additionalContexts`：`fs/tool-fs-search/src/grep.ts:363`、`glob.ts:365`；spill 恢复路径提示：`spill/spill-policy/src/index.ts:142-148`）；
- 15. experimental hooks 的附加 context（`claude-code-mods/src/host-ops.ts:374`；hooks-claude-code/codex）。

**C. 请求失败时（request-error retry，模型视角=重发）**
- 16. `llm-retry`：`EMPTY_RESPONSE/RATE_LIMIT/SERVER/TIMEOUT/TRANSPORT` 默认最多 5 次（`retry-policy.ts:14-24`），过程落 `llm/retry`/`llm/retry-started`（`llm-retry/src/index.ts:157-191`）；
- 17. 压缩溢出重试（`compaction-basic/index.ts:190-234`）；
- 18. 图片超限 offload 重试（`compaction-image-offload/src/index.ts:27-40`）。

**D. 回合外的唤醒**（承旧长任务报告）：subagent settlement（`subagent/src/manager.ts:1186-1206`）、schedule 提醒（`schedule/schedule/src/runtime.ts:85-164`）。

**"plan reminder" 说明**：没有单独周期提醒；等价物是 plan 激活期间 **plan:policy section 每次请求都在**（text 回调按 active 返回，`plan-mode/index.ts:219-223`），加上模式切换时的一次性 narration（#2）。

---

## 7. Q7 跨压缩的思考持久化

1. **摘要模板**：全文见 §4.3（八段结构 + 规则；含"合并旧 checkpoint"条款 `summarizer.ts:66`）。摘要以**工具/可用前缀缓存**的辅助调用生成（复用会话 system+tools+阴影区消息，末尾追加指令；`summarizer.ts:25-31、144-163`；`purpose:'compaction'` :160）。
2. **落地方式**：`compaction/summary` 事件（记录 summary/rawOutput/usage/shadowedRange/shadowedSeqs）+ 一条**带 preamble 的 checkpoint user 消息**，以 `surfaceOp:{op:'replace',startSeq,endSeq}` 替换被阴影区间（`region.ts:410-416、478-509`）。此后 `deriveMessages()` 不再产出被阴影节点（旧报告 §3.2；`session/src/types.ts:331` 注释、`surface.ts:96-104`）。原始事件仍完整保留在日志（可审计/可回放）。
3. **大 tool 输出**：`compaction-tool-result-pruner`——超阈值（默认 8192 字符）替换为 head(4096)+`middle pruned` 标记+tail(1024)，非文本块保序保留；先落 `compaction/prune` 影子价格事件再 replace（`compaction-tool-result-pruner/src/index.ts:81-122`；触发仅在压缩路径：`compaction-basic/src/index.ts:289-327`）。`spill-policy` 另有一套"head/tail + 完整结果恢复文件路径"的保留策略（`spill-policy/README.md` Summary；`spill-policy/src/index.ts:142-148`），图片走 attachment 旁路 + `image/offload`（`compaction-image-offload/src/index.ts:27-40`）。
4. **被压掉的历史怎么"想起来"**：**核心内没有**面向模型的"回忆"机制——没有 recall 工具、没有按需展开 shadowed 区间。模型能"想起来"的只有：(a) 摘要本身（§4.3）；(b) 上下文里仍在的 runtime-context/指令/引用；(c) 显式注入（session-reference 的 `recall` form 是**跨会话**的，`llm/src/message.ts:66-67`）。experimental 的 `session-search`（"search earlier sessions"，Web/Desktop 默认关闭）与 `tool-session-query`（**排除 caller 会话**，`tool-session-query/src/index.ts:67` 描述；README Summary）都查不了本会话被压掉的历史。**【未确认】** 是否有内测/闭源插件补了本会话 recall。

---

## 8. Q8 总评

**让模型"想得好、答得好"的核心机制（按重要性）**：

1. **思考的透明管道（核心）**：reasoning 块一等公民——落日志（含逐帧时序）、实时展示（含拿思考当过程标题）、带签名回喂；跨模型时"降级不丢"（`serialize.ts:31-34` / `replay.ts:201-206、247-259`）。“想”是可审计、可续接的。
2. **runtime-context 快照（核心）**：把环境的硬事实（目录/沙箱/审批/权限）作为 user 消息**每步刷新**，让思考不被环境误解带偏；"变了才注入"保护缓存。这是 dsh 区别于"全靠 system prompt 说教"的取向（`runtime-context.ts:146-163`）。
3. **外部化思考原语（核心/插件混合）**：plan 模式（规划期强约束 + 唯一终局工具 + 人工审批闸）、goal 轮提示 + wrap-up、检查点摘要、repeat-tool-reminder。它们把"想"变成有文本载体的上下文对象，从而可以跨 step/轮/压缩存活。
4. **失败与空答的工程防线（核心）**：`EMPTY_RESPONSE` + 默认重试集 + durable retry 事件（`error.ts:33-42`、`retry-policy.ts:14-24`）。
5. **收束的诚实性（核心/UI）**：不做自动续写；max-tokens 明示截断并教用户说"继续"；UI 用"最后一条有文本的 assistant"定义 closing，思考不算答复（`turn-tail.ts:57-69`、`assistant-content.ts:10-17`）。

**核心 vs 插件**：核心=日志/块类型/组装器/循环的重试与收束/retry policy；装配（base bundle）=llm-retry、repeat-tool-reminder、compaction、skill、goal、plan-mode；web 专属=workspace-changes（turn-stopping 记录）、session-search（默认关）。plan/goal 与 todo 都是"插件位"的机制，不改变 loop 本体。

**明显缺失**：

1. **thinking-only 无处理**：只思考不回答会被当作正常完成；没有重试/追问/校验（§2）。唯一防线是 0-block。
2. **本会话"回忆"缺失**：压缩后只有摘要；无 recall/展开 shadowed 区间的模型侧工具；session-query 类工具排除本会话（§7.4）。
3. **reasoning-token 计量缺失**：`reasoningTokens` 字段无人填充（§1.3），思考成本对用户不可见。
4. **普通模式无语境化"思考方法论"**：没有 plan 激活时，system prompt 里几乎只有身份一句 + 工具 section；"先规划再动手"之类要求只在 plan 模式/goal/skill 里按需出现（§3.1）。
5. **跨压缩后思考连贯性无机制保障**：摘要是否忠实全靠一次辅助调用 + 模板自律；没有对照校验（旧报告也确认 plan 执行期无机械核对）。

---

## 9. 未确认

1. **pi-ai 上游对 OpenAI/Claude 原生 reasoning 字段的归一细节**：仓库只有 pi-ai 的 tool-JSON patch（`patches/@earendil-works__pi-ai@1.1.0.patch` 全部是参数解析修复），上游 `thinking`/`reasoning_content` 的处理未在本仓库代码内；本文只确认 dsh 侧映射与 replay 元数据（§1.4）。
2. **默认 web profile 实际用哪个适配器跑 Claude/OpenAI 模型**：`llm-pi-ai` 自述为 "design-verification twin"（package.json:3），base bundle 装配的是 `llm-deepseek` 系（base/cordis.patch.yml:518-524）；pi-ai 的装配位置未逐 profile 验证。
3. **`reasoningTokens` 是否被任何外置适配器填充**（闭源/实验）未验证；开源树中两适配器均不填。
4. 旧报告提到的 "plan/usercode" 在本树未找到对应包（本次 grep `usercode|user-code` 无生产代码命中）——疑为旧报告笔误或闭源组件。
5. experimental `hooks-*` 的 force-continuation 具体话术、`agent-preset` skills 的正文内容未读。
6. UI 在"reasoning 流式但从未产出 text"的回合结束后的完整视觉表现（是否只有 Think 行）未截图验证，仅按代码（turn-tail/branchUnavailable）推断。

---

## 10. 总表：机制 | 作用于思考还是回答 | 实现位置 | 提示词全文或结构

| 机制 | 思考/回答 | 实现位置 | 提示词全文或结构 |
|---|---|---|---|
| reasoning 块/流协议/组装器 | 思考 | `llm/src/types.ts:67-71,470-482`；`assembler.ts:62-68,108-112` | 无提示词（数据管道） |
| reasoning 落日志+逐帧流 | 思考 | `agent.ts:537-546`；`session/src/types.ts:342-350`；`llm/src/assistant-stream.ts:28-34` | — |
| 中断前缀保留（text/reasoning） | 思考/回答 | `agent.ts:462-486`；`assembler.ts:162-179` | — |
| 展示：Think 行/活组标题/用量 | 思考 | `ui-chat/.../ReasoningRow.tsx`；`process-activity.ts:96-113`；`locale.ts:174-175,344` | UI 文案：`Think`；`已达到输出 token 上限…发送"继续"…` |
| thinking 回喂（DeepSeek） | 思考 | `llm-deepseek/src/serialize.ts:26-39,65-67,147-156`；`translate.ts:49-54,73-81`；`replay.ts:46-63` | wire `thinking`+`signature`；`output_config.effort` |
| thinking 回喂（pi-ai 多 provider） | 思考 | `llm-pi-ai/src/stream.ts:165-173`；`replay.ts:77-110,186-232,247-259`；`catalog.ts:391-417` | replay 元数据：thinkingSignature/redacted/thoughtSignature/providerThinkingLevel |
| EMPTY_RESPONSE 校验+重试 | 回答 | `llm/src/error.ts:33-42`；`retry-policy.ts:14-24`；`llm-retry/src/index.ts:157-191`；`translate.ts:149`；`pi-ai/stream.ts:96-107` | 无提示词；durable `llm/retry`/`llm/retry-started` |
| harness identity section | 思考 | `system-prompt/src/index.ts:449-455` | `You are an AI agent powered by DeepSeek Harness.` |
| deployment persona（web/headless/acp） | 思考 | `web-app/cordis.patch.yml:17-21` 等 | `You are a coding agent powered by the {{model}} model.`（base 为空串） |
| plan:policy（plan 模式） | 思考 | `plan-mode/src/index.ts:216-224`；`base/cordis.patch.yml:332-346`；`standard.patch.yml:51-65` | 全文见 §4.2（探索优先/不落写/唯一终局 exit_plan_mode/decision-complete 计划） |
| plan narration（切换 notice） | 思考 | `plan-mode/index.ts:196-213,448-459,462-474` | `The user switched this session to plan mode.` / `…back to the default mode.` |
| exit_plan_mode 结果文案 | 回答 | `plan-mode/index.ts:291-296` | `Plan approved — plan mode exited; carry out the plan starting with your next step.` |
| runtime-context 快照 | 思考 | `agent.ts:411-432`；`runtime-context.ts:146-163`；贡献者 §3.2 | 结构：按 CONTEXT_ORDERS 渲染的 sections（cwd/沙箱/审批/子代理…） |
| 工具 section（bash/read/edit/jobs/subagent/goal/workflow） | 思考 | §3.1 各行号 | 见 §3.1 表（"investigate failures…"、"read before edit"、"don't busy-poll"…） |
| goal 轮提示词 | 思考 | `goal-round-driver/prompt.ts:12-26`；`index.ts:190-208` | 全文见 §4.4（workspace authoritative / 证据再 complete） |
| goal wrap-up（complete/blocked） | 回答 | `tool-goal/src/wrapup.ts:17-40`；注入 `tool-goal/index.ts:319-330` | "Write the closing message to the user now… Report only what … actually establish" |
| todo_write | 思考（回合内） | `tool-todo/src/index.ts:135-211`；`types.ts:28-33` | 结果 `Updated todo list: …`；不进入历史、下个 turn 清空 |
| compaction 摘要+checkpoint 替换 | 思考/回答 | `compaction-basic/src/summarizer.ts:32-67,69-71,188-194`；`region.ts:410-416,506-509` | 八段模板全文见 §4.3；preamble "Treat the captured context as established background…" |
| tool-result pruner / spill | 思考 | `compaction-tool-result-pruner/src/index.ts:81-122`；`spill-policy/src/index.ts:142-148` | head+`middle pruned`+tail；spill 附恢复文件路径 |
| repeat-tool-reminder | 思考 | `guard/repeat-tool-reminder/src/index.ts:70-84,199-240` | "You are repeating the exact same tool call…" / "Repeated tool call detected…" |
| skill 目录 + /skill 注入 | 思考 | `tool-skill/src/index.ts:179-212,215-253,260-318` | `<system-reminder>…<available_skills>…`；`<skill_content>` 正文 |
| 后台 job 完成通知 + 指导 | 思考/回答 | `tool-jobs/src/index.ts:251-255,283-308` | "You are notified in-session when a job finishes — do not busy-poll…" |
| turn-stopping 监听者（无话术） | 回答 | `agent.ts:361-364`；`workspace-changes/src/index.ts:155-157`；hooks（experimental） | 无提示词；git 快照/可续跑 |
| closing 判定 + max-tokens 提示 | 回答 | `turn-tail.ts:57-69`；`assistant-content.ts:10-17`；`turn-max-tokens.ts:42-72` | UI：closing=最后非空 text；`回答被截断…发送"继续"…` |
| request-error 重试（llm-retry/压缩/offload） | 回答 | `agent.ts:511-526`；`llm-retry/src/index.ts:157-191`；`compaction-basic/index.ts:190-234`；`compaction-image-offload/index.ts:27-40` | 无提示词；durable 重试事件 |
