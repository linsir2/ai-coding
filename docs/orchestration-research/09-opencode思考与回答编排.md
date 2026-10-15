> 来源：并行调研 agent 产出（只读调查），高级工程师复核；供"编排层认知维度"调研使用。
> 主题：用户输入之后，opencode 如何编排模型的"思考"与"回答"（推理管线/塑形提示词/外部化思考/收束/干预/跨压缩）。
> 未确认项保留在文末；姊妹篇：`03`（数据流）、`06`（长任务）。

# opencode 编排层调研：模型"思考与回答"的编排

- 仓库：`/home/linsir365/projects/opencode` @ `0a46301e36d7edd517dd89736acc5daf9c74898e`
- 范围：用户输入之后，opencode 如何编排模型的"思考"（reasoning）与"回答"（final text）
- 方式：只读代码（未修改任何仓库文件）；与既有 `opencode-orchestration-flow.md`（单回合数据流）、`opencode-longtask-research.md`（长任务机制）互不重复
- 关键代码域：`packages/core/src/session/**`（runner / compaction / instructions）、`packages/ai/src/**`（LLM 事件与协议）、`packages/schema/src/**`（会话消息/事件）、`packages/session-ui`、`packages/tui`

---

## 1. 推理/思考内容的管线

### 1.1 LLM 事件层（packages/ai）

是 `ReasoningStart / ReasoningDelta / ReasoningEnd`（不是 "Reasoning.Started"）：

- `packages/ai/src/schema/events.ts:166-188`：`reasoning-start`（id + providerMetadata）、`reasoning-delta`（id + text + providerMetadata）、`reasoning-end`（**带权威全文 text**，可替换累积 delta）。构造器 `events.ts:348-353`。
- 响应组装：`ensureReasoning/reduceReasoningDelta/reduceReasoningEnd`（`events.ts:546-578`），`LLMResponse.reasoning` getter（`events.ts:696-699, 741`）。`joinFragments` 的语义是"end 的完整值替换该 fragment 的 delta 累积"（`events.ts:405-424`）。
- 与 text 一样，reasoning 是**独立内容块**，不是一个全局通道。

### 1.2 会话事件层（core → 持久化）

- `packages/schema/src/session-event.ts:429-461`：`session.reasoning.started`（durable，ordinal+state）、`session.reasoning.delta`（**ephemeral，live-only**）、`session.reasoning.ended`（durable，全文 text+state）。注释明确："Stream fragments are live-only; Reasoning.Ended is the replayable full-value boundary."
- 转发：`packages/core/src/session/runner/publish-llm-event.ts:230-252, 422-437`（fragment 批处理 100ms，`deltaBatchInterval` 见 :79）；`reasoning-start` 同时把 `outputStarted = true`（:422-423）。

### 1.3 存库（message part）

- `packages/schema/src/session-message.ts:183-192`：`AssistantReasoning = { type:"reasoning", text, state?, time:{created, completed?} }`。`state` 是 provider 元数据（按 `providerMetadataKey` 单键存放）。
- 投影：`packages/core/src/session/message-updater.ts:379-402`（started push 空 part；ended 写全文+completed 时间；未收到 ended 的 part 永远没有 `time.completed`）。

### 1.4 展示（TUI）

- 开关：`session.thinking` 配置 `"show"|"hide"`，**默认 hide**（`packages/tui/src/config/index.tsx:155-157`；配置对话框 `packages/tui/src/component/dialog-config.tsx:66-70`）。
- 渲染：reasoning part 渲染为可展开的 "Thinking"/"Thought" BasicTool 条目，流式时 shimmer+标题摘要（`packages/session-ui/src/message/message-content.tsx:637-700`）；标题提取 `reasoningHeading`（`packages/session-ui/src/timeline/projection.ts:712+`）；OpenAI Responses 的 `**标题**` 摘要被识别为独立标题（`packages/tui/src/context/thinking.ts:5-19`）。
- 折叠/分组：`ReasoningMode = "hidden"|"compact"|"full"`（`packages/session-ui/src/timeline/projection.ts:22`）；TUI 隐藏模式把 reasoning 归入分组（`packages/tui/src/routes/session/index.tsx:240`）；timeline detail 有 `thinking.placement: separate|grouped|hidden` 三档（`packages/session-ui/src/timeline/detail.ts:23-67`）。
- 中断态：未完成的 reasoning 显示 "reasoning interrupted"（`packages/tui/src/mini/entry.body.ts:225-234`）。
- 流式客户端侧：`packages/client/src/solid/data.ts:460-466`（`editReasoning`，只找 `!time.completed` 的 part）与 `:986-1004`（消费三个事件）。

### 1.5 回喂下一次请求

reasoning **进** `toLLMMessage`，但有降级规则（`packages/core/src/session/runner/to-llm-message.ts:153-180`）：

- 同 provider+同 model 且 assistant 消息无 error：reasoning part 原样带上 `providerMetadata`（"model-scoped proof"：Anthropic signature、OpenAI 加密 reasoning、Gemini thought signature 只有生产它的模型能重放）。
- 跨模型/有 error：`reuseProviderMetadata=false` 时 → 有 error 降级为 `text`，无 error 保留为无 metadata 的 `reasoning`（:178-180）。即"中断回合的部分思考"会以普通文本进入下一请求。
- 空 text 且无 metadata 的 reasoning 被过滤（:208-212）。tool call 侧 metadata 同样模型绑定（:181-206 注释）。
- 例外：compaction 的超预算 flat 路径把 reasoning 整体丢弃（`compaction.ts:808-816`），但 `recent` 保留段用 `[Assistant reasoning]: ...` 形式包含（`compaction.ts:765`）。

### 1.6 provider 差异归一

统一归一为 canonical Reasoning 事件 + `providerMetadata[key]`（key = `route.providerMetadataKey ?? provider`，`llm.ts:79`、`model-request.ts:107,118`）：

- Anthropic：`anthropic-messages.ts:1255-1290`（thinking/redacted_thinking → reasoning 事件，signature 入 metadata）；回程 `:928-953`（有 signature→thinking block；redactedData→redacted_thinking；无 signature 且模型要求签名→降级 text 保持可发送）。
- OpenAI chat：`openai-chat.ts:938-946` 依次尝试 `configured reasoningField / reasoning_content / reasoning / reasoning_text`；:1076-1110、:1236-1253（reasoningDetails 快照）。模型兼容字段声明见 `packages/ai/src/schema/options.ts:193-195`（reasoningField/requireReasoning）。
- OpenAI Responses：`open-responses.ts:68, 968-1027`（`itemId` + `reasoningEncryptedContent`），回程 :477-500。
- Gemini：`gemini.ts:692`、`google-interactions.ts:377-378`；thought signature 附着在 text part 上的情形见 to-llm-message.ts:163-165 注释。
- 其它：`bedrock-converse.ts:682-683`、`mistral-chat.ts:555-556`、`cohere-chat.ts:265-276`。
- reasoning effort 由 variant 系统映射为各 provider 的 settings/body（`packages/core/src/variant.ts:79-473` 大量分支）；会话内换 variant 通过 `effort` part 表达（`to-llm-message.ts:217-247`，`Message.effort` 定义 `messages.ts:217-223, 280-281`）。

---

## 2. 思考独有的失败模式

### 2.1 中断流续写（ContinueAfterIncompleteStream）

- 常量原文（`packages/core/src/session/runner/llm.ts:35-36`）：
  > `The previous response was interrupted. Continue from where you left off without repeating completed content.`
- 触发链：step 失败判定 `isInterruptedStream`（Transport read 或 `invalid-output: incomplete-stream`，`step.ts:281-285`）；unknown finish 被归一成 incomplete-stream（`step.ts:163-171`）；可"续写"条件 = `(isRetryable || (outputStarted && isInterruptedStream))`（`step.ts:181-191`）；当 `retry && record.outputStarted` 时返回 `Outcome.Continue`（`step.ts:253-261`）。**只思考没回答也算 outputStarted**（reasoning-start 置位，`publish-llm-event.ts:422-423`），所以"只思考"的断流同样走续写。
- 续写动作（`llm.ts:278-286`）：等 retry 退避 → 发布 durable `SessionEvent.Synthetic`（该常量文本）→ **新建 assistantMessageID** 作为续写回合。下一次请求里它渲染为 **user 角色**文本（`to-llm-message.ts:284-285`）。
- 测试背书：`packages/core/test/session-runner.test.ts:5535-5584`（有可见文本后继续）；`:5687-5723`（**中断的 reasoning 在下一请求降级为 text**："Partial thought" 出现在 messages[-2]）；`:5726-5767`（断流但已有加密 reasoning state 时，state 原样保留为空 text 的 reasoning part）。
- 无输出时的透明重试：`RecoverFull`（transport `retry-full` 且 `!outputStarted`，`step.ts:174-180`），避免"续写"与"全量重试"叠加（`llm.ts:291-293` 关掉 `recoverContinuation`）。
- 重试节奏：指数 2s→上限 10s、共 10 次、jitter、遵守 retry-after（≤15min），超时类最多 3 次（`retry.ts:32-62`）。

### 2.2 max tokens（output length）

- 主循环**没有**对 `finish=length` 的自动续写/追问：step 正常结算，`needsContinuation` 只看"是否调用过本地工具"（`step.ts:271-273`，`publish-llm-event.ts:610-619`），因此纯文本被截断时回合直接结束，截断文本就是最终回答。
- output 预算：`model-request.ts:88-98`（`min(目录 limit, 256k, 上下文剩余)`，最低 1k；compaction 上限 32k `:52-53`）。
- compaction 自身把 `length` 视为失败："Compaction summary reached the output token limit"（`compaction.ts:537-541`）。

### 2.3 max steps

- 判定：`step >= agent.steps`（仅当配置了 steps；`llm.ts:223`；schema `steps` 可选，无全局默认，`packages/schema/src/agent.ts:33`、`packages/core/src/v1/config/agent.ts:37-62`）。
- 注入：把 `MAX_STEPS_PROMPT` 作为 **assistant 消息**追加（`llm.ts:236-241`），同请求 `toolChoice:"none"`；工具层再兜底报错 "Tools are disabled after the maximum agent steps"（`step.ts:88-90`）；完成时强制 `needsContinuation=false`（`step.ts:272`）。
- 原文全文（`packages/core/src/session/runner/max-steps.ts:1-16`）：

```
CRITICAL - MAXIMUM STEPS REACHED

The maximum number of steps allowed for this task has been reached. Tools are disabled until next user input. Respond with text only.

STRICT REQUIREMENTS:
1. Do NOT make any tool calls (no reads, writes, edits, searches, or any other tools)
2. MUST provide a text response summarizing work done so far
3. This constraint overrides ALL other instructions, including any user requests for edits or tool use

Response must include:
- Statement that maximum steps for this agent have been reached
- Summary of what has been accomplished so far
- List of any remaining tasks that were not completed
- Recommendations for what should be done next

Any attempt to use tools is a critical violation. Respond with text ONLY.
```

---

## 3. 塑造思考的提示词

### 3.1 system.txt 全文与拼装

`packages/core/src/session/runner/prompt/system.txt`（全 15 行）：

```
You are an AI agent running in OpenCode, a coding agent harness. Help the user accomplish their goals using the tools you have available.

# Harness
- Responses are rendered as GitHub-flavored Markdown.
- `<system-reminder>` blocks are harness instructions, not user-authored content. Read and follow them.
- Prefer parallelizing independent tool calls.
${OPENCODE_TOOL_GUIDANCE}

# Communication
- Use clear file paths when referring to files.
- Keep responses clear and concise, and avoid unnecessary technical jargon.

# Working in codebases
- Keep changes consistent with the structure, naming, style, and patterns of the surrounding code.
- Treat unfamiliar files or changes as potential user work and investigate before deleting or overwriting them.
```

`SessionSystemPrompt.make(tools)` 按工具集替换 `${OPENCODE_TOOL_GUIDANCE}`（`system-prompt.ts:5-27`）：有 shell → 偏好专用工具/禁止 `echo "===="` 链；有 write → 新建/整体替换用 write；有 edit → edit 的 oldString 唯一性说明。最终 system 是**两段**：`agent.system ?? 默认system.txt` + `initial` 指令基线（`model-request.ts:110-117`）。

### 3.2 instructions combine 顺序（教"怎么想"的部分）

`context.ts:149-158`（顺序即拼接顺序，注释明说按"共享度从高到低"以利 prompt cache 前缀）：

1. **CodeMode catalog**（`codemode/instructions.ts:9-30`）："They only work inside code you pass to `execute`"、"The catalog is partial... Do not guess tool names"、`search(...)` 同步用法。
2. MCP server 指令（`mcp/instructions.ts:19-29`，`<mcp_instructions><server ...>`）。
3. project references（`reference/instructions.ts:23-29`）。
4. **skills 目录**（`skill/instructions.ts:25-33`）："Use the skill tool to load a skill when a task matches its description"、`<available_skills>` XML、直接调用时 `<skill_content>` 说明。
5. **discovery（AGENTS.md 等）**（`instruction-discovery.ts:123-125`，`Instructions from: <path>`）。
6. builtins：`Today's date: ...` 与 `<env>` 块（`instructions/builtins.ts:24-53`）。
7. session entries：`<context key="...">`（`instruction-entry.ts:85-103`）。

渲染为一个 `initial` 字符串（`instructions/index.ts:161-169, 229-231`，段间 `\n\n`），作为第二个 system part；**变化走 `session.instructions.updated` → system 角色消息**（message-updater.ts:148-159；schema 注记 text 在 emit 时冻结，`session-event.ts:270-277`）。这就是"基线+增量"的指令编排。

### 3.3 agent 自带系统提示（塑造"怎么想"的其它来源）

- explore agent：`plugin/agent.ts:8-23`（"file search specialist... Return file paths as absolute paths..."）。
- title agent：`plugin/agent.ts:26-68`（"You are a title generator... <=50 characters..."）；summary agent：:71-82（当前核心路径未见调用，见"未确认"）。
- compaction 无自带 system，摘要提示词是拼接在请求尾部的 user 消息（见 Q4）。

### 3.4 plan agent 的 system-reminder 原文

`packages/core/src/plugin/plan.ts:14-25`：

```
<system-reminder>
You are in Plan mode. Discuss the plan with the user directly in the conversation. Do not create or update plan files unless the user explicitly asks you to; when they do, write them only in:
<plan 目录：~/.opencode/plan>

Do not modify any other files or ask a subagent to do so.

You remain in Plan mode until the user switches agents. If the user asks you to implement changes, do not do so. Tell them they need to switch agents.
</system-reminder>
```

离开时（plan.ts:23-25）："You are NO LONGER in Plan mode. The previous Plan restrictions no longer apply..."。注入机制：`session.hook("context")` 在**用户消息之前** splice 一条 `Message.user`（request-time），同时持久化一条 synthetic（`resume:false`），保证 compaction/回退后仍可对账（`plan.ts:56-74`）；agent 切换时经事件补发（:76-97）。plan agent 的权限是 edit 全 deny、只允许写 plan 目录（:39-41）。

---

## 4. 外部化思考的工具/机制（无 todo）

首先确认：**当前没有 todo/plan-checklist 工具**。旧 `todowrite` 在 v1 迁移中被列为移除工具（`packages/core/src/database/v1-migration.bun.ts:901`，`REMOVED_TOOLS = ["todowrite"]`）；现行工具集见 `packages/core/src/tool/plugin/`（edit/read/shell/grep/glob/patch/write/webfetch/websearch/skill/subagent/question/mcp-resource/opencode + codemode `execute`）。无 todo 情况下的替代：

1. **/btw（侧问）**：一次性回答，不写入会话。TUI 插件注入的指令原文（`packages/tui/src/feature-plugins/prompt/btw.tsx:16-20`）：
   > "The user is asking a quick side question about the conversation so far. Answer directly and concisely in markdown from what you already know. Do not call any tools and do not take any actions."
   后端走 `session.generate`（一次 `llm.generate`，不产生工具循环，不落库；`generate.ts:25-73`；btw.tsx:13-15 注释说明"tool call would surface as an empty answer"）。
2. **session.generate（API）**：从当前上下文一次性生成文本、不改变会话（`generate.ts:25`注释 "without mutating the Session"），/btw 即其消费方。
3. **compaction checkpoint**：结构化摘要 + 原文 recent 断——跨压缩的工作状态载体（全文见下）。
4. **skills**：`skill` 工具把 `<skill_content name="...">`（含目录文件清单）载入对话（`skill.ts:36-52`）。
5. **subagent**：起子会话干活，输出回传（描述 `tool/plugin/subagent.ts:55-62`；完成通知见 Q6）。
6. **plan 模式 + plan 目录**：把计划外化为 `~/.opencode/plan` 下的文件，权限系统强制其它文件只读（plan.ts:15-16, 39-41）。
7. **question 工具**：向用户发问（对"要不要继续/选哪条路"的外部化，`tool/plugin/question.ts`）。

### 4.1 compaction 摘要模板全文

`packages/core/src/session/compaction.ts:101-132`（`SUMMARY_TEMPLATE`）：

```
You MUST use this format for your response (you may omit sections that aren't applicable). Do not include the <template> tags in your response.
<template>
## Objective
- [one or two brief sentences describing what the user is trying to accomplish]

## Requirements
- [constraints, preferences, requirements, and scope boundaries stated by the user, or "(none)"]

## Decisions
- [decisions already made and why, or "(none)"]

## Work State
Break the objective into smaller goals and report which are completed, which are being worked on, and which are blocked.
### Completed
- [goals that have been completed; otherwise "(none)"]

### Active
- [goals currently being worked on; otherwise "(none)"]

### Blocked
- [anything blocking progress, and why; otherwise "(none)"]

## Next Move
1. [ordered list of next actions, or "(none)"]

## Relevant Files
List the files and directories, other than the current working directory, that another agent would need to open to continue this work. Include at most 15, most important first. Do not list every file that was read or changed. Include paths outside the current working directory when relevant. If none, write "(none)".
- `[file or directory path]`: [brief reason it matters]

## Important Context
- [facts the next agent cannot continue without and cannot easily find on its own; or "(none)"]
</template>
```

配套 `SUMMARY_RULES`（:134-140）：单行要点、保留精确路径/符号/命令/错误串/URL、保留未决用户问题原文、记录 git 状态（uncommitted/committed/pushed/under review/merged）、"Do not mention the summary process or that context was compacted"。构建器 `buildPrompt`（:142-173）：首次摘要 vs "Update the existing checkpoint..." 更新（含 legacy 模板一次性重写逻辑 :156-167）；回复不按模板时追加一次 `NUDGE`（:175-176）："The previous response did not fill in the required summary template. Do not call tools. Return the summary as text using the exact section headings from the template."。摘要请求把 prompt 作为 **user 消息**追加（:276），模板命中校验只看 `##` 标题（:268-269）。

recent 保留段：默认 `keep=15_000` tokens（:191），从最新一条 user 消息边界切起、永远保留最新一轮交换（`recentStart` :701-723）；文本化时含 `[Assistant reasoning]: ...`、工具调用与结果（截断 1250 字符）、`[Synthetic context]` 等（`messageToText` :738-790）。

---

## 5. 回答的收束

- 步进循环：`runStep` 返回 `needsContinuation`（`llm.ts:188-196`）。`Completed` 分支直接取 `outcome.needsContinuation`（`llm.ts:271`），其定义为"本步是否调用过本地工具"（`publish-llm-event.ts:610-619`）；`toolChoice=none`（max steps）时强制 false（`step.ts:272`）。
- `needsContinuation=true` → drain 循环继续（step+1，同一回合）；`false` → 回到 `advanceToStep`，若无待提升输入则 `DrainResult.Complete`（`llm.ts:161-162`）→ 上层发布 `session.execution.succeeded` → 追加 `idle(succeeded)` 标记（message-updater.ts:143, 63-77）。
- **最终文本没有后处理**：就是最后一个 assistant 消息的 text parts（权威值来自 `text-end`，见 events.ts:157-163）。TUI 单独渲染；没有"收束为答案"的二次生成。
- `MAX_STEPS_PROMPT` 原文见 2.3；`llm.ts:236-241` 的注入位置是 messages 尾部、作为 assistant 角色。
- 标题生成：首个输入被 promote 且会话未命名时，在 FiberMap 里异步跑 `SessionTitle.generate`（`llm.ts:174-177`），**不阻塞、不影响回答**；标题请求用 title agent（PROMPT_TITLE），优先选最小 reasoning variant（`context.ts:104-106, 185-186`）；从首条 user 消息或近期对话截 8k 生成、取首行、截 100 字符（`title.ts:20-22, 100-148`）。
- `SessionGenerate`（session.generate / /btw）：独立的一次性请求，复用历史+指令基线，但不改会话、不进回合（generate.ts）。对主回答无影响。
- 压缩摘要（SessionCompaction）对回答的影响仅通过"下一请求的 checkpoint 输入"体现（见 Q7），不直接改当前回答。

---

## 6. 回合内的认知干预：synthetic 消息全谱

机制：`Session.synthetic`（`session/session.ts:273-314`）→ 持久化 `SessionMessage.Synthetic`（schema `session-message.ts:83-89`；事件 `session-event.ts:279-289`，带 description/metadata）→ LLM 渲染为 **user 角色**（`to-llm-message.ts:284-285`）。默认 `delivery:"steer"`（:296），`resume:false` 时不唤醒执行（:310）。

清单（原文措辞 + 角色）：

| 来源 | 原文/结构 | 角色 | 位置 |
|---|---|---|---|
| 中断续写 | "The previous response was interrupted. Continue from where you left off without repeating completed content." | user | `llm.ts:35-36, 284` |
| 服务器重启续跑 | "The server restarted while you were working. Continue from where you left off without repeating completed work." | user | `execution/restart.ts:15-16, 92-97` |
| Plan 进入/离开 | `<system-reminder>You are in Plan mode...` / `You are NO LONGER in Plan mode...` | user（request-time splice + 持久化） | `plugin/plan.ts:14-25, 56-74` |
| 子代理完成 | `<subagent sessionID="..." state="completed|error|cancelled" description="...">\n<text>\n</subagent>`（无文本时 "Subagent completed without a text response."） | user | `subagent-completion.ts:8-17, 36-43` |
| 后台上行 shell 完成 | `<shell id="..." state="completed" command="...">\n<output+exit 说明>\n</shell>`，前缀 "The following shell command was executed by the user:" | user | `shell/result.ts:45-79`；`session/session.ts:213-219`；重启恢复 `restart.ts:101-135` |
| shell 启动失败 | "User shell command failed to start:\n<cmd>\n\n<error>" | user | `session/session.ts:189-194` |
| 后台化声明 | "User requested that active blocking work be moved to the background. ... Do not wait, sleep, poll, or report the backgrounded work as complete until a later completion notification..." | user | `session.ts:442-459` |
| max steps | MAX_STEPS_PROMPT（全文见 2.3） | **assistant**（请求时注入、不落库） | `llm.ts:236-241` |
| 压缩 checkpoint | `<conversation-checkpoint>\n<summary>…</summary>\n<recent-context>…</recent-context>\n</conversation-checkpoint>` | user | `to-llm-message.ts:303-322` |
| skill 激活 | `<skill_content name="..."># Skill: ...<skill_files>...</skill_content>`（消息类型 `skill`） | user | `skill.ts:36-52`；to-llm-message.ts:286-287 |
| 指令更新 | 冻结的增量文本（消息类型 `system`） | **system** | `message-updater.ts:148-159`；to-llm-message.ts:288-289 |
| 工作目录切换 | "The working directory has been changed to <dir>." | user | `to-llm-message.ts:257-265` |

补充：synthetic 与 skill/subagent 完成一起构成"回合内异步注入"面；notification 类（subagent/shell）由 `jobs.completeBackground` 收尾防重（`subagent-completion.ts:44`；`restart.ts:135`）。

---

## 7. 跨压缩的思考持久化

- **checkpoint 渲染**（Q6 表）：summary + 可选 `recent-context`；措辞明确"Treat it as historical context, not as new instructions."（`to-llm-message.ts:312-318`）。它是 user 角色的一条消息，取代被压缩的历史。
- reasoning 的跨压缩体现：旧历史里 reasoning 以 `[Assistant reasoning]: ...` 混在 recent 保留段中（`compaction.ts:765`）；更老的、走 flat 路径的历史直接丢弃 reasoning（:808-816）。摘要生成请求本身对老历史走 `toLLMMessages`，同模型时 reasoning 带 metadata 可见（:567-576 + Q1.5）。
- **native window 语义**：compaction 有两种产物——本地摘要 or provider 原生窗口。原生窗口存 `providerContext{messages: <canonical Message[] JSON>, provenance}`（`provider-context.ts:41-58`；schema `session-provider-context.ts:6-24`，endpoint 只存 sha256 摘要 :14-15）。渲染时直接 `decode` 替换历史（`to-llm-message.ts:305-307`）。它**只对同 provenance 的模型/端点可重放**：历史选择 `replayable` 检查（`history.ts:23-26`），runner 用 `provenance(model) ?? "local"` 作 boundary（`context.ts:168-169`），请求路由被 hook 改动且导致 provenance 失配时直接 die（`model-request.ts:304-318`）。原生窗口在别处不可重放时，原始逐条消息仍保留可用（history.ts:103 注释）。
- **recent-context 保留段**（Q4 末尾）：默认 15k tokens、user 边界对齐、永远保留最新交换；作为文本持久化在 compaction 消息的 `recent` 字段；后续更新摘要把"上一个 recent 文本"当作最老 transcript 条目再接（`compaction.ts:474` 注释与 `:466-475`）。
- 压缩触发与预算：`due()`（:227-236，只用当前 provider 的 measured prompt 作锚）、ceiling=窗口-10%（≥16k，:890-897）、overflow 时按 0.7/0.5/0.35 递减重试（:92, 420-426）。

---

## 8. 总评

### 让模型"想得好、答得好"的核心机制（5 个）

1. **思考是一等公民且可重放**：reasoning 独立事件/part/DB 字段，带 provider 加密状态回喂（同模型带 metadata、跨模型/出错降级为 text）。这是 interleaved thinking 与加密推理链能跨步连续的基础（Q1）。
2. **输出感知的中断续写**：不是盲目重试——`outputStarted` 决定"透明重试"还是"发布续写 synthetic + 新 assistant 消息"，并把中断的思考降级为文本保留（Q2）。
3. **指令"缓存友好基线 + 时序增量"**：combine 顺序按共享度排序做稳定 cache 前缀，变更以 system 消息按时间线注入，避免整段重述（Q3）。
4. **结构化 checkpoint（Objective/Work State/Next Move/Relevant Files）+ 原文 recent 窗**：把"思考的工作状态"在压缩时显式外化，且对 native 窗口做 provenance 级安全重放（Q4/Q7）。
5. **分离的认知模式**：plan 模式（提醒+权限+plan 目录）、/btw 一次性侧问、subagent 隔离上下文、max-steps 强制收尾——在不同场景给"思考"换不同的边界（Q3/Q4/Q6）。

### 核心 vs 插件

- 核心（不可替换）：runner 步进/续写、reasoning 事件与持久化、instructions 机制、compaction 模板、synthetic/checkpoint 渲染、max-steps。
- 插件层：plan 模式（core 内置插件 `plugin/plan.ts`）、/btw（TUI 插件）、provider reasoning 归一（`plugin/provider/*` 注入 reasoning 设置）、各协议 adapter 的 reasoning 解析。

### 明显缺失

- **没有 todo/任务状态工具**（`todowrite` 已移除）：Work State 只能靠压缩摘要或 plan 文件承载，压缩之间无轻量外化手段。
- **`finish=length` 无自动续写/提示**：被 output 上限截断的回答就是最终回答；只有 max-steps 有强制收尾语。
- **"只思考、无回答、正常 stop"没有干预**：正常结束且无工具调用时回合直接结束（needsContinuation=false），没有"你还没给出答案"的检查。
- reasoning 默认隐藏（TUI `thinking` 默认 hide），用户对"思考展示"的控制粒度只有 show/hide（无 compact UI 开关暴露给用户；`ReasoningMode` 三档主要被 timeline detail 使用）。
- 原生压缩依赖端点稳定（routing hook 改动直接 die），跨端点只能退回本地摘要。

---

## 未确认

1. `summary` agent（PROMPT_SUMMARY，`plugin/agent.ts:71-82,147-152`）在当前核心路径的实际调用点未找到（可能是遗留/命令侧功能）。
2. `ReasoningPart.encrypted`（`packages/ai/src/schema/messages.ts:181-189`）在核心写路径未见产出方；目前加密状态走 `providerMetadata`（Anthropic lowering 优先读 `part.encrypted`，`anthropic-messages.ts:930`）。
3. `steps` 没有全局默认值；未配置即为无上限（`llm.ts:223` 只在 `steps !== undefined` 时生效），对默认 build agent 的实际默认步数未在本次范围内穷尽（可能与配置层默认合并有关）。
4. `session.generate` 的 `request.generate` hook 对工具的最终处理（generate.ts 传了 tools 定义但无执行循环），仅见 TUI 注释佐证"工具调用会得到空回答"。
5. "只思考没回答"但流正常结束（finish=stop）时，TUI/服务端是否有告警展示，未在本次检索中确认。

---

## 机制总表

| 机制 | 作用于 | 实现位置 | 提示词全文/结构 |
|---|---|---|---|
| reasoning 事件归一（Start/Delta/End + providerMetadata） | 思考 | `packages/ai/src/schema/events.ts:166-188,348-353,546-578`；协议 adapater 见 Q1.6 | 结构化事件；end 带权威全文 |
| reasoning 持久化（part + durable started/ended） | 思考 | `schema/src/session-message.ts:183-192`；`message-updater.ts:379-402`；`session-event.ts:429-461` | `AssistantReasoning{text,state,time}` |
| reasoning 回喂/降级 | 思考 | `runner/to-llm-message.ts:153-180,208-212` | 同模型带 metadata；跨模型/出错降级 text |
| TUI 思考展示 | 思考(展示) | `tui/src/config/index.tsx:155-157`；`session-ui/src/message/message-content.tsx:637-700`；`tui/src/context/thinking.ts` | "Thinking"/"Thought" 可展开；默认 hide |
| 中断流续写 synthetic | 思考→回答 | `llm.ts:35-36,278-286`；`step.ts:163-191,253-261` | "The previous response was interrupted. Continue from where you left off without repeating completed content." |
| 服务器重启续跑 | 思考→回答 | `execution/restart.ts:15-16,92-97` | "The server restarted while you were working. Continue from where you left off without repeating completed work." |
| max-tokens 处理 | 回答(截断即终) | `model-request.ts:88-98`；`step.ts:271-273` | 无提示词；仅 compaction 将 length 判失败（`compaction.ts:537-541`） |
| max-steps 强制收尾 | 回答 | `llm.ts:223,236-241`；`max-steps.ts:1-16`；`step.ts:88-90` | MAX_STEPS_PROMPT 全文（assistant 角色；toolChoice:none） |
| system.txt + 工具指引 | 思考+回答 | `runner/prompt/system.txt:1-15`；`system-prompt.ts:5-27` | 全文见 3.1；`${OPENCODE_TOOL_GUIDANCE}` 按工具替换 |
| 指令 combine 基线 | 思考 | `context.ts:149-158`；`instructions/index.ts:161-169` | CodeMode→MCP→refs→skills→discovery→builtins→entries，\n\n 连接为一个 system part |
| 指令增量（system 消息） | 思考 | `message-updater.ts:148-159`；`session-event.ts:270-277` | 冻结的增量文本（变更 diff/替换全文） |
| plan 模式提醒 | 思考 | `plugin/plan.ts:14-25,56-74` | `<system-reminder>You are in Plan mode...`（user 角色，前端 splice+持久化） |
| /btw 侧问 | 思考(外化) | `tui/src/feature-plugins/prompt/btw.tsx:16-20`；`generate.ts:25-73` | "Answer directly and concisely... Do not call any tools..." |
| compaction checkpoint | 思考(外化/持久化) | `compaction.ts:101-173,687-723`；`to-llm-message.ts:303-322` | SUMMARY_TEMPLATE 全文见 4.1；`<conversation-checkpoint><summary/><recent-context/>` |
| native 窗口重放 | 思考(持久化) | `provider-context.ts:41-58`；`history.ts:23-26`；`model-request.ts:304-318` | canonical Message[] JSON + provenance（端点哈希） |
| synthetic 注入全谱 | 思考+回答 | 见 Q6 表 | user 角色（除 instructions 为 system）；默认 steer |
| subagent 通知 | 回答/协调 | `subagent-completion.ts:36-43` | `<subagent sessionID state description>` |
| 标题生成 | 回答(旁路) | `llm.ts:174-177`；`title.ts:100-148`；`plugin/agent.ts:26-68` | PROMPT_TITLE（≤50 字符、单行、无工具） |
| effort/variant 切换语义 | 思考(强度) | `to-llm-message.ts:217-247`；`variant.ts:79-473` | `effort` part（同模型换档时注入） |

*报告完毕。全部论断基于 HEAD `0a46301e3` 的只读检查。*
