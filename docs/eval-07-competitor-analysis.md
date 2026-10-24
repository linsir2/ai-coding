# eval-07 · 竞品对标：opencode / Codex / Claude Code / Copilot

> 方法：① 联网调研（架构、工具循环、错误处理、数据流）；② **克隆源码逐行分析**
> （`opencode` MIT、`codex` Apache-2.0，位于 `/workspace/_vendors/`）。
> 所有源码结论给出 `文件:行号`。

## 0. 一句话对照

| | ai-coding（本项目） | opencode | Codex CLI | Claude Code | Copilot coding agent |
|---|---|---|---|---|---|
| 形态 | 终端 CLI + Bun TUI | 终端 CLI + 桌面/IDE/Slack | 终端 CLI / IDE / App | 终端 CLI / SDK | 云端异步代理 |
| 主语言 | Python | TypeScript + Effect | Rust | TypeScript | 闭源 |
| 许可 | 未声明（自有） | MIT | Apache-2.0 | 源码可得，**非开源** | 闭源 |
| 引擎范式 | **自持 ReAct 循环**（进程内 while） | **事件溯源 + 准入/执行分离** | 任务/会话/轮次/采样四层 | ReAct 状态机（7 态） | 云 VM + PR 工作流 |
| 循环 | `AgentLoop._run_turn` | `SessionRunner`（每 step 一次 `llm.stream`） | `run_turn` → `run_sampling_request` | `query.ts` AsyncGenerator | 非本地循环 |
| 存储 | 会话 JSON + trace jsonl + `.memory` | SQLite（Drizzle）+ 事件表 | rollout `.jsonl` + SQLite 索引 | 事件 + checkpoint | 云 VM + git |
| 模型绑定 | **任意 OpenAI 兼容端点** | 任意（models.dev） | GPT 系（可配） | Claude 系 | 多模型 |

## 1. opencode（`/workspace/_vendors/opencode`）

### 1.1 架构：事件溯源 + 准入/执行分离

不是「内存 while 循环」，而是**持久化事件溯源 + 进程内协调器**：

- 用户输入先**准入**为 durable inbox 行（`InboxEnqueued`），再**调度**执行
  （`SessionExecution.wake`），二者分离 —— [session.ts:L155-188](file:///workspace/_vendors/opencode/packages/core/src/session/session.ts#L155-L188)
- **每个 step 只做一次 `llm.stream` 调用**，从重新加载的历史继续，**没有内存中的 tool loop**
  —— [runner/llm.ts:L202](file:///workspace/_vendors/opencode/packages/core/src/session/runner/llm.ts#L202)
- 协调器是 **doorbell 模型**：`wake` 合并 scope、`drain` 循环、`interrupt` 立即返回
  —— [run-coordinator.ts:L27-54](file:///workspace/_vendors/opencode/packages/core/src/session/run-coordinator.ts#L27-L54)

```
用户输入
  └─ Session.prompt (session.ts:155)
       ├─ SessionPrompt.prepare       → 物化附件/skill
       ├─ SessionInbox.admit          → publish InboxEnqueued
       │     └─ Projector             → INSERT session_inbox
       └─ SessionExecution.wake
             └─ Coordinator.wake → drain (runner/llm.ts:56)
                   ├─ promote         → InboxDelivered → INSERT message
                   └─ runStep (llm.ts:203)
                         └─ SessionStep.attempt (step.ts:73)
                               ├─ llm.stream → publish Text/Reasoning/Tool.*
                               ├─ executeTool (model-request.ts:381)
                               └─ Step.Ended/Failed → projector 落库
```

### 1.2 工具执行

- 统一入口 `Tool.Snapshot.execute` —— [tool.ts:L264-286](file:///workspace/_vendors/opencode/packages/core/src/tool.ts#L264-L286)
- **decline 必须逃逸**：`Permission.assert` 与 question 工具把「拒绝」作为 defect 抛出，
  `executeTool` 用 `catchCauseFilter` 把它恢复为失败，**防止工具把「用户拒绝」吞成模型可见输出**
  —— [model-request.ts:L371-396](file:///workspace/_vendors/opencode/packages/core/src/session/model-request.ts#L371-L396)
- 子代理：创建/复用 child session + `permission.assert` + 前台 `jobs.block` / 后台立即返回
  —— [tool/plugin/subagent.ts:L100-267](file:///workspace/_vendors/opencode/packages/core/src/tool/plugin/subagent.ts#L100-L267)

### 1.3 错误处理（三层）

1. **统一转换** `toSessionError`：`AIError` 按 reason 分 `provider.rate-limit/auth/transport`；
   `Permission.BlockedError` → `permission.rejected`；工具失败保留 curated message
   —— [to-session-error.ts:L31-93](file:///workspace/_vendors/opencode/packages/core/src/session/to-session-error.ts#L31-L93)
2. **Step 层 Outcome 联合**：`Completed | Retry | Continue | RecoverFull | Compacted | OutputLimit`
   —— [step.ts:L32-43](file:///workspace/_vendors/opencode/packages/core/src/session/runner/step.ts#L32-L43)
   - **透明重试仅当 `!recorded.outputStarted`**（用户还没看到东西才重试）
   - `OutputLimit` 连续两次 → `StepFailedError`，否则注入 `CONTINUE_AFTER_OUTPUT_LIMIT`
3. **终止分类** `terminal`：`succeeded / interrupted / failed`
   —— [execution.ts:L47-51](file:///workspace/_vendors/opencode/packages/core/src/session/execution.ts#L47-L51)

### 1.4 上下文压缩

[compaction.ts](file:///workspace/_vendors/opencode/packages/core/src/session/compaction.ts)（~1190 行）：

- `due()`：`estimateContext(context) >= ceiling`，且末尾不是已完成 compaction
  —— [L409-418](file:///workspace/_vendors/opencode/packages/core/src/session/compaction.ts#L409-L418)
- **三条路径**：native（provider 原生）/ 预计算摘要 / `summarize` —— [L284-319](file:///workspace/_vendors/opencode/packages/core/src/session/compaction.ts#L284-L319)
- **尺寸收缩阶梯**：`SHRINK_STEPS = [0.85, 0.7, 0.5, 0.35]`，provider 拒「too long」时逐级收缩
  —— [L119-128](file:///workspace/_vendors/opencode/packages/core/src/session/compaction.ts#L119-L128)
- **四级裁剪**：`OUTPUTS_CLIPPED → OUTPUTS_REMOVED → USER_ONLY → OMITTED`
- **后台预计算**：上下文到 85% 时后台生成摘要并 hold，**为复用 prompt cache**
  —— [L328-376](file:///workspace/_vendors/opencode/packages/core/src/session/compaction.ts#L328-L376)

### 1.5 数据流

- **Bus**：durable 事件在 `db.transaction({behavior:"immediate"})` 写入，
  **projector 在同一事务内执行** —— [bus.ts:L320-452](file:///workspace/_vendors/opencode/packages/core/src/bus.ts#L320-L452)
- **Projector**：每个事件一个 projector（`Step.*`/`Text.*`/`InboxEnqueued`/`Execution.*`）
  —— [projector.ts:L449-829](file:///workspace/_vendors/opencode/packages/core/src/session/projector.ts#L449-L829)
- **Inbox**：`admit` 幂等 reconcile；`promote` 顺序 **steer > control > input**
  —— [inbox.ts:L505-540](file:///workspace/_vendors/opencode/packages/core/src/session/inbox.ts#L505-L540)
- **崩溃恢复**：**write-ahead claim** —— 启动时在 started 事件同一事务记录 durable intent；
  终止释放，**shutdown 中断除外**（保留 claim 供下次恢复）
  —— [store.ts:L208-227](file:///workspace/_vendors/opencode/packages/core/src/session/store.ts#L208-L227)

## 2. Codex CLI（`/workspace/_vendors/codex/codex-rs`）

### 2.1 架构：四层 + 策略/机制分离

```
SessionTask（生命周期）→ run_turn（轮次/压缩/hooks）
  → run_sampling_request（请求/重试）→ try_run_sampling_request（流/工具分发）
```

- `SessionTask` trait + `start_task`（tokio::spawn）+ `on_task_finished`
  —— [tasks/mod.rs:L181-221](file:///workspace/_vendors/codex/codex-rs/core/src/tasks/mod.rs#L181-L221)
- 中断：`cancel()` 后等 `GRACEFULL_INTERRUPTION_TIMEOUT_MS = 100ms`，超时 `abort()`
  —— [tasks/mod.rs:L72](file:///workspace/_vendors/codex/codex-rs/core/src/tasks/mod.rs#L72)
- `run_turn` 主循环 —— [turn.rs:L473-922](file:///workspace/_vendors/codex/codex-rs/core/src/session/turn.rs#L473-L922)
- `run_sampling_request` 带重试 —— [turn.rs:L1730-1870](file:///workspace/_vendors/codex/codex-rs/core/src/session/turn.rs#L1730-L1870)

### 2.2 apply-patch（标志性设计）

- 语法 `*** Begin Patch / Add File / Update File / Move to / End Patch`
  —— [parser.rs:L1-25](file:///workspace/_vendors/codex/codex-rs/apply-patch/src/parser.rs#L1-L25)
- **tree-sitter Bash 严格 query 拦截 heredoc**：要求 apply_patch 是唯一顶层语句，
  且 `cd` 与 `apply_patch` 之间必须是 `&&`；并拒绝「裸补丁体」隐式调用
  —— [invocation.rs:L274-404](file:///workspace/_vendors/codex/codex-rs/apply-patch/src/invocation.rs#L274-L404)
- **真实读文件算 unified diff** → `ApplyPatchAction`；写失败标 `delta.exact = false`
  —— [invocation.rs:L180-252](file:///workspace/_vendors/codex/codex-rs/apply-patch/src/invocation.rs#L180-L252)

### 2.3 沙箱与 execpolicy：策略/机制分离

- **execpolicy 是纯策略引擎**：`Decision { Allow, Prompt, Forbidden }`，
  `Evaluation::from_matches` 取**最严**者 —— [policy.rs:L402-411](file:///workspace/_vendors/codex/codex-rs/execpolicy/src/policy.rs#L402-L411)
- **sandboxing 是纯机制**：`should_sandbox` / `select_initial` / `transform`
  —— [manager.rs:L271-604](file:///workspace/_vendors/codex/codex-rs/sandboxing/src/manager.rs#L271-L604)
- **唯一耦合点** `bypass_sandbox`：要求**每个命令段**都被显式 allow
  —— [core/src/exec_policy.rs:L448-459](file:///workspace/_vendors/codex/codex-rs/core/src/exec_policy.rs#L448-L459)

### 2.4 错误分类与重试（三层）

1. **分类** `CodexErrorDetails`（strum 生成 `CodexErrKind`）
   —— [protocol/src/error.rs:L101-231](file:///workspace/_vendors/codex/codex-rs/protocol/src/error.rs#L101-L231)
2. **决策** `retry_delay` → `Option<Duration>`：
   - **不重试**：`TurnAborted`/`ContextWindowExceeded`/`InvalidRequest`/`Sandbox`/`QuotaExceeded`
   - **仅服务端建议**：`ServerOverloaded`/`RetryLimit`
   - **指数退避**：`Stream`/`ContentFilter`/`RateLimitExceeded`/`Timeout`/`ConnectionFailed`
   —— [error.rs:L418-470](file:///workspace/_vendors/codex/codex-rs/protocol/src/error.rs#L418-L470)
3. **执行** `handle_response_stream_error`：内容过滤引导 → 无界连接重试（5s→60s）→
   传输回退（WebSocket→HTTPS）→ 有界重试
   —— [responses_retry.rs:L58-177](file:///workspace/_vendors/codex/codex-rs/core/src/responses_retry.rs#L58-L177)

### 2.5 数据流

- **app-server**：JSON-RPC 2.0 子集，`MessageProcessor` 按领域分发
  —— [app-server/src/message_processor.rs:L144-175](file:///workspace/_vendors/codex/codex-rs/app-server/src/message_processor.rs#L144-L175)
- **rollout**：`RolloutRecorder` 异步 writer + **延迟物化** + **写恢复**
  （首次写失败 → `enter_recovery_mode` 重开文件再试）+ SQLite 索引
  —— [rollout/src/recorder.rs:L1029-1053](file:///workspace/_vendors/codex/codex-rs/rollout/src/recorder.rs#L1029-L1053)、
  [L1875-1900](file:///workspace/_vendors/codex/codex-rs/rollout/src/recorder.rs#L1875-L1900)
- **持久化策略** `policy.rs`：瞬态事件（`Error`/`Warning`/delta）**不落盘**
  —— [rollout/src/policy.rs:L127-227](file:///workspace/_vendors/codex/codex-rs/rollout/src/policy.rs#L127-L227)

## 3. Claude Code

- **六层架构**：入口 → Agent Loop → 工具执行 → 权限安全 → 服务 → 终端 UI
- **共享引擎**：CLI / SDK / 子代理共用 `query.ts`（~1729 行 AsyncGenerator）
- **ReAct 状态机 7 态**：`BuildConfig → CallModel → ProcessStream → CheckStop →
  ExecuteTools → BuildConfig`；`end turn → Terminal`
- **错误处理**：`max_tokens`/413 → 回 `BuildConfig` 重试；stream error → `FallbackModel`；
  **Doom Loop Detector**；权限三层（规则 → 分类器 → 用户）+ hooks
- **数据流**：SSE 流式（`message_start`/`content_block_delta`/`message_stop`）；
  65+ 片段中间件管线组装系统提示；`CLAUDE.md`（`memdir`）跨会话记忆
- **许可**：`LICENSE.md` = "All rights reserved"（**source-available，非开源**），
  完整核心以编译后 npm 二进制分发

## 4. Copilot coding agent

- **闭源**，无公开仓库。异步云端代理，运行在 **GitHub Actions 驱动的临时 VM**
- **以 PR 为中心**：issue/评论触发 → 会话 → 分支 commit → draft PR → 人工 review → 迭代
- **安全**：防火墙隔离执行环境 + 受限网络 + 受限仓库权限 + 分支保护 + 人工审批
- 定位：低至中等复杂度、测试充分的代码库任务；**不合并代码、不部署**

## 5. 横向对比：遇到一个问题后，他们怎么改、怎么分析、怎么得出结论

| 环节 | ai-coding（本项目） | opencode | Codex | Claude Code |
|---|---|---|---|---|
| **发现问题** | 工具结构化错误 + `ToolResult.error` 机器码 + trace jsonl | `Step.Failed` 事件 + `SessionError` 分类 | `CodexErrorDetails` 分类 + `ErrorEvent` | 7 态状态机 + `FallbackModel` |
| **要不要重试** | 回合墙钟/迭代预算/`tool_timeout`；无自动重试阶梯 | `Outcome.Retry/Continue` + `retry.wait`（仅 `!outputStarted`） | `retry_delay` 三档（不重试/仅服务端/指数退避） | 413→回 `BuildConfig`；stream error→Fallback |
| **怎么改** | 模型读结构化错误自行改策略；`[strategy-gate]` nudge | 注入 `MAX_STEPS_PROMPT` / `CONTINUE_AFTER_OUTPUT_LIMIT` | 内容过滤引导 + 传输回退 | 上下文压缩级联 + 工具结果回灌 |
| **上下文爆了** | L1 裁剪 / L2 信封 / L3 持久化 / L4 LLM 摘要 | `compact` 三路径 + 收缩阶梯 `[0.85,0.7,0.5,0.35]` + **预计算复用 cache** | compaction 滚动 + post-turn 压缩 | compact 服务级联 + Doom Loop Detector |
| **得出结论** | 验收门（证明命令）+ 失败升级阶梯 + 交接报告 | `terminal` → succeeded/interrupted/failed | `TurnComplete`/`TurnAborted` | stop reasons + 回滚 checkpoint |

### 关键差异（本项目 vs 三家）

1. **恢复模型**：opencode 有 **write-ahead claim**、Codex 有 **rollout 写恢复**、
   Claude Code 有 **checkpoint**；本项目的等价物是 **armed session 重扫 + `open_turn` 续跑**，
   粒度更粗（会话级，非 step 级）。
2. **重试策略**：Codex 有**显式三档重试决策**（哪些绝不重试）；本项目**没有自动重试阶梯**，
   靠模型自己读错误改策略 —— 更依赖模型能力。
3. **事件溯源**：opencode 把「投影与事件同事务」作为硬保证；本项目是**直接写会话 JSON**，
   简单但缺少重放能力。
4. **压缩**：opencode 的**预计算摘要为复用 prompt cache**、Codex 的 compaction 滚动，
   都比本项目的「按预算分页 + `/compact`」更精细。
5. **沙箱**：Codex 的 **execpolicy/sandboxing 策略-机制分离** + 跨平台后端
   （Seatbelt/bwrap+landlock/MXC）远比本项目的 `security/sandbox.py` 路径限制彻底。

## 6. 本项目的相对优势（实测）

- **记忆层深度罕见**：BM25 + 中文 bigram + **双门禁**（`min_relevance` + `relative_band`）
  + **未命中诊断**（`terms_known`/`near_misses`/`index` 概览）+ **来源位置引用**（`m<idx>`）
  + **场景标签** + **写侧总开关** —— 实测可达 R@3=0.99 / FP@5=0.46，
  **这套「可解释召回」在四家公开资料里没有对应物**。
- **可靠性收口**：回合墙钟 + 工具超时 + 结构化 `tool_timeout` + 中断结构化
  （`cancelled_by_user` 不判失败）—— 实测 8s 掐断且已产出保留。
- **记忆/压缩的实测纪律**：文档里大量「实测推翻了我自己的说法」的自我否证记录，
  这种**用基准说话**的工程文化比多数竞品文档更诚实。
- **零框架依赖**：自持循环，可审计性强。

## 7. 来源

- opencode：[anomalyco/opencode](https://github.com/sst/opencode)（MIT）、
  [深度架构分析](https://martianlee.github.io/kb/2026-06-29-opencode-architecture)、
  [permissions](https://opencode.ai/docs/permissions)
- Codex：[openai/codex](https://github.com/openai/codex)（Apache-2.0）、
  [codex-rs](https://github.com/openai/codex/tree/main/codex-rs)、
  [Windows 沙箱](https://openai.com/index/building-codex-windows-sandbox/)
- Claude Code：[anthropics/claude-code](https://github.com/anthropics/claude-code)、
  [LICENSE](https://raw.githubusercontent.com/anthropics/claude-code/main/LICENSE.md)、
  [Inside Claude Code](https://y-agent.github.io/inside-claude-code/00-birds-eye-architecture.html)、
  [How it works](https://code.claude.com/docs/en/how-claude-code-works)
- Copilot：[Meet the coding agent](https://github.blog/news-insights/product-news/github-copilot-meet-the-new-coding-agent/)、
  [About cloud agent](https://docs.github.com/en/copilot/concepts/agents/cloud-agent/about-cloud-agent)
- 横向：[Inside the Scaffold, arXiv:2604.03515](https://arxiv.org/html/2604.03515v1)
