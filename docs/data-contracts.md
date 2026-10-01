# 数据契约

本项目的领域数据契约集中在 `ai_coding/domain`，是各层共用的不可变边界。

## 消息模型

| 类型 | 字段 | 说明 |
|---|---|---|
| `Role` | — | 字面量 `user` / `assistant` / `tool` / `system` |
| `ToolCallRef` | `id`, `name`, `arguments` | 模型原始工具调用引用，`arguments` 为 JSON 字符串 |
| `ToolCall` | `id`, `name`, `params` | 由 `arguments` JSON 解析出的调用，`params: dict` |
| `ToolResult` | `success`, `output` | 工具执行结果，`output` 回喂模型 |
| `ToolOutput` | `call_id`, `tool_name`, `output` | 与来源调用配对的输出，供落盘 `role=tool` 消息 |
| `ToolExecution` | `tool_call`, `result` | 调用与结果的配对单元 |
| `ChatMessage` | `role`, `content`, `timestamp`, `tool_call_id`, `tool_name`, `tool_calls` | 单一消息承载四种角色 |

`ChatMessage` 关键不变量：`role=tool` 必须携带 `tool_call_id`；`role=assistant` 必须至少带 `content` 或 `tool_calls` 其一。解析工具参数用 `tool_call_from_ref(ref)`，非法 JSON 静默退化为空字典，不抛错。

## 一步与一回合

R1 之后「一步」与「一回合」是两个契约（`domain/loop.py`）：

| 类型 | 字段 | 说明 |
|---|---|---|
| `StepResult` | `text` / `tool_calls` / `finish_reason` / `usage` | **一次模型调用**的产物；引擎只跑一步 |
| `TurnOutcome` | `status` / `text` / `iterations` / `reason` | **一次 `process_input`** 的终态 |
| `TurnStatus` | `COMPLETED` / `MAX_ITERATIONS` / `STOPPED` / `ABORTED` / `ERROR` | 回合如何结束 |

旧的 `TurnResult` 已删除（循环归我们之后它没有意义）。

`tool_message(out, ts)` 把输出包进反馈封套 `wrap_tool_output(name, output, success=, data=)`。
信封形状是历史结构不变量：`success=True` 且 `data=None` 时与 M0–M6 **逐字节一致**；
失败时加 `ok="false"`；有结构化结果时内嵌 `<data>{json}</data>` 块。压缩与配对逻辑依赖该形状。

## 会话落盘格式

`SessionData` 字段 `session_id`/`title`/`created_time`/`last_access_time`/`messages`，其 `to_dict`/`from_dict` 输出 camelCase 键，与 Java 文件一致。一次工具回合的落盘示例：

```json
{
  "sessionId": "6c53b93be85841f5a5d0f2873db22926",
  "title": "",
  "createdTime": "2026-09-20T10:00:00Z",
  "lastAccessTime": "2026-09-20T10:00:05Z",
  "messages": [
    { "role": "user", "content": "读取 sample.txt", "timestamp": "2026-09-20T10:00:00Z" },
    {
      "role": "assistant",
      "content": null,
      "timestamp": "2026-09-20T10:00:02Z",
      "toolCalls": [
        { "id": "call_1", "name": "read", "arguments": "{\"path\":\"sample.txt\"}" }
      ]
    },
    {
      "role": "tool",
      "content": "<tool_output tool=\"read\">\n1  hello world from workspace\n</tool_output>",
      "timestamp": "2026-09-20T10:00:03Z",
      "toolCallId": "call_1",
      "toolName": "read"
    },
    { "role": "assistant", "content": "sample.txt 的内容为：hello world from workspace", "timestamp": "2026-09-20T10:00:05Z" }
  ]
}
```

`SessionData.ensure_tool_pairing(strict)` 在落盘前防御性清理：丢弃无主的 `tool` 结果、剔除无匹配结果的孤立工具调用（无文本的「载体」assistant 消息若调用全部失配则整条丢弃）。`SessionService` 以 `sessions/{id}.json` 原子写入（`os.replace`）。

## 模型连接配置

`ModelConfig`：`name`, `base_url`（纠正自 Java `baseURL`）, `api_key`, 以及默认值 `streaming=True`, `max_tokens=4096`, `temperature=0.7`, `top_p=0.9`, `timeout=60`。

## 配置 schema

`AppConfig` 是顶层根，字段均带默认值：

```yaml
models:            # dict[str, ModelConfig]，键为模型名
default_model: ""  # 必须有值，否则服务装配抛 ValueError
tools:             # ToolsConfig：bash/read/write/edit/glob 各自的 ToolConfig
ai:                # AIConfig：回合与压缩相关参数（见下表）
memory:            # MemoryConfig
mcp:               # MCPConfig（默认关闭）
```

`AIConfig` 关键参数及默认值：

| 参数 | 默认 | 作用 |
|---|---|---|
| `max_context_tokens` | 48000 | 上下文预算，驱动压缩与 L4 熔断 |
| `max_messages` | 400 | L1 裁剪阈值（保留头/尾见下；R3b 起它只在预算被突破时才真正触发） |
| `snip_keep_head` / `snip_keep_tail` | 3 / 20 | L1 裁剪保留条数 |
| `keep_recent_tool_results` | 3 | L2 保留最近工具结果数 |
| `l2_window_chars` | 200 | L2 信封保留的头/尾各多少字符（R7b） |
| `l2_window_budget` | 4000 | L2 一次压缩里所有窗口的总字符预算，从新到旧分配（R7b） |
| `per_result_persist_bytes` | 30000 | L3 单结果持久化阈值 |
| `l4_keep_tail` | 6 | L4 摘要保留尾部条数 |
| `auto_process_tool_results` | true | 是否自动执行工具结果回喂 |
| `max_tool_iterations` | 10 | 单回合工具循环上限 |
| `max_concurrent_subagents` | 3 | 子代理并发上限 |
| `subagent_worktree_isolation` | true | 子代理是否 worktree 隔离 |

`MemoryConfig`：`enabled`, `auto_extract`, `consolidate_threshold=10`, `max_index_entries=200`, `max_per_turn_injections=3`（R3 起：这是 `memory_search` 每次返回的条数上限）。

`MCPConfig`：`enabled=false`, `auto_discover=true`, `connection_timeout=60`（R7d 起；npx 冷启动实测 ~27s）, `servers[]`。`MCPServerConfig`：`name`, `command`, `enabled=false`, `args[]`, `env{}`, `cwd`, `tools=[]`（非空则只注册这些远端工具，R7d）, `auto_approve=false`（该 server 的工具免审批，R7d）。

YAML 加载规则：键做 camel→snake 归一化（`baseURL`→`base_url` 视为同一键）；未知键忽略；缺失时用模型默认值补全。加载函数 `ConfigLoader.load_app`（AppConfig）与 `load_mcp`（MCPConfig）分盒解耦。

## 关键不变量汇总

- `role=tool` 必须有 `tool_call_id`；`role=assistant` 必须有 `content` 或 `tool_calls`。
- 工具输出以 `<tool_output tool="name">` 封套回喂，配对逻辑据此完成严格配对。
- 上下文压缩只改历史副本；任意阶段后 `sanitize_pairs` 恢复严格配对。
- 记忆、L4 摘要、MCP、worktree 等增强全部静默降级，永不向会话主链路抛错或污染状态。

---

## R1 编排层契约

R1 新增/变更的契约集中在四处。

### 任务清单（`domain/task.py`）

| 类型 | 字段 |
|---|---|
| `TodoItem` | `id`、`content`、`status`（`pending`/`in_progress`/`completed`/`blocked`/`dropped`）、`depends_on`、`evidence`、`created_at`、`updated_at` |
| `TaskState` | `goal`、`items`；方法 `add` / `update` / `clear` / `open_items` / `is_complete` / `focus` / `render` |

`render()` 产出 `<task-state>` 块（目标、进度、逐项状态、下一步）。
`depends_on` 目前只用于渲染与「下一步」选择，**不做硬门禁**。

### 会话新增字段（`SessionData`）

| 字段 | 说明 |
|---|---|
| `task: TaskState \| None` | **有 todo 项时才写入 JSON**（只有 goal 是可从首条 user 消息还原的噪声） |

### 工具结果（`domain/message.py`）

| 类型 | 字段 |
|---|---|
| `ToolResult` | `success`、`output`（给模型读）、`data`（给程序读）、`artifacts`、`error` |
| `ToolArtifact` | `path`、`kind`、`bytes`、`description` |
| `ToolOutput` | `call_id`、`tool_name`、`output`、`success`、`data`、`artifacts` |

`ToolResult(success, output)` 的旧形态仍然合法——这是 R1a 各步能保持增量的原因。
各拒绝路径有固定 `error` 码：`denied` / `not_approved` / `stopped` / `skipped` /
`unknown_tool` / `tool_error` / `mcp_error` / `subagent_error`。

R2 补两个码与一个字段：

| 新增 | 取值 | 含义 |
|---|---|---|
| `ToolResult.error` | `tool_timeout` | 分发器的**调用预算**耗尽：工具没跑完（或没启动），没有结果 |
| `ToolResult.error` | `timeout` / `nonzero_exit` / `spawn_failed` | `bash` 自身的命令超时 / 非零退出 / 起不来（此时 `data` 带 `exit_code`、`timed_out`、`duration_seconds`） |
| `ToolResult.data`（超时） | `{tool, timeout_seconds, reason}` | `reason` = `call_budget_exhausted`（跑着被掐）或 `turn_budget_exhausted`（没预算，压根没启动） |

### 工具上下文与子智能体

| 类型 | 字段 | 说明 |
|---|---|---|
| `ToolContext` | `task`、`workspace`、`session_id`、`messages`、`instructions`、`emit` | 工具的**每次调用**上下文；`messages`/`instructions` 是父回合当次请求视图 |
| `ParentContext` | `messages`、`instructions` | 传给子智能体的「相同上下文」 |
| `SubagentResult` | `status`、`conclusion`、`todos`、`artifacts`、`iterations`、`workdir`、`ledger_path`、`error` | 子智能体的结构化信息包（`text` 是 `conclusion` 的只读别名） |

`BaseTool` 增加两个契约成员：`execute(params, ctx=None)` 与 `parallel_safe: bool`（默认 `False`，
只读工具置 `True`；一批调用只要有**一个**不安全或非 ALLOW 级，整批串行）。

R2 再加一个只读属性 `max_duration_seconds: float | None`：工具自报的单次调用上限
（`bash` = 命令超时 + 15s 收割余量；`subAgent` = 子智能体超时 + 15s；`None` 表示只受回合预算约束）。
分发器实际使用的预算是 `min(max_duration_seconds, 回合剩余)`。

### 循环事件（`core/events.py`）

`LoopEvent` 是以下冻结事件的联合：`IterationStarted`、`AssistantTextDelta`、`AssistantMessage`、
`ToolApprovalRequested`、`ToolStarted`、`ToolFinished`、`TaskUpdated`、`TurnFinished`。
`LoopObserver.on_event` 是同步入口；观测者抛异常不会中断回合（`_SafeObserver` 兜底并记日志）。

R2 给 `ToolFinished` 追加末尾字段 `error: str | None = None`（与 `ToolResult.error` 同源），
让事件流也能区分「失败」与「超时」而不必解析文本。

### `AIConfig` 新增项

| 参数 | 默认 | 作用 |
|---|---|---|
| `stop_policy` | `"todo_aware"` | `natural` / `todo_aware` / `strict` |
| `max_todo_nudges` | `2` | todo 门禁最多提醒次数 |
| `task_injection` | `"message"` | `message`（易失末尾消息）/ `system`（并入指令） |
| `max_concurrent_tools` | `4` | 同批工具并发上限 |
| `subagent_timeout_seconds` | `600` | 子智能体超时（超时是一种 `status`，不是异常） |
| `max_result_data_bytes` | `8000` | 结构化工具结果上限 |
| `max_subagent_conclusion_chars` | `4000` | 子智能体结论截断（全文只进 ledger） |
| `stream_usage` | `true` | 是否请求 `stream_options.include_usage` |
| `persist_checkpoint_messages` | `200` | 超过此条数后，回合内落盘按步长（每 3 轮）节流 |
| `turn_timeout_seconds` | `600.0` | **回合墙钟上限**（R2）：到期 → `TurnStatus.TIMEOUT`，保留已产出文本与轨迹；`0` 关闭。含人工审批等待时间 |

### 回合终态（`domain/loop.py`）

`TurnStatus`：`completed` / `max_iterations` / **`timeout`**（R2 新增）/ `stopped` / `aborted` / `error`。

### 记忆条目（`memory/note.py`，R3）

| 字段 | 说明 |
|---|---|
| `id` / `text` | 标识与内容 |
| `kind` | `fact` / `decision` / `preference` / `open_thread`（决定加权、常驻与淘汰保护） |
| `scope` | 默认 `workspace`（同工作区跨会话共享） |
| `importance` | 1–5（提取器给，整理时按命中提升） |
| `created_time` / `updated_time` | 新近性衰减与审计 |
| `source_session` / `source_turn` | 溯源 |
| `supersedes` / `superseded_by` | 取代链：旧条**只标记不删除**，可回滚 |
| `hits` / `last_used` / `pinned` | 使用统计与淘汰保护 |
| `dedupe_key`（派生） | `sha1(normalize(text))`，同内容幂等 |

`.memory` 文件：`{"version": 2, "notes": [...]}`；读到 v1（`entries`）自动迁移为 v2 字段默认值。

### 收割契约（`memory/schema.py`，R3）

提取器输出 JSON 数组：`[{"text", "kind", "importance", "action", "target_id"}]`，
`action ∈ add|update|supersede|noop`。解析容错：代码块、前后散文、坏行、旧的「一行一条」格式都能吃，
完全不可用时返回空列表（记忆永不抛错）。

### 新增事件（`core/events.py`，R3）

`MemoryInjected(note_ids, chars)`、`MemoryRecalled(note_ids, scores, chars)`、
`MemoryHarvested(added, updated, superseded, skipped)`、`MemoryConsolidated(merged, promoted, expired, evicted)`。

### 会话新增字段（R3b）

`SessionData.harvested_messages`（JSON: `harvestedMessages`）：已收割的消息数游标。
存在会话里而不是循环里，因为 CLI 每回合新建 loop，而会话每回合重新加载。

---

## R4 可观测 / 门卫 / 钩子契约

R4 加了三层，但只动了**两处既有对外契约**，其余全是新增或追加默认值。

### 新增事件（`core/events.py`）

| 类型 | 字段 | 说明 |
|---|---|---|
| `ModelStepFinished` | `index`、`finish_reason`、`text_chars`、`tool_call_count`、`usage`、`cached_tokens`、`cache_write_tokens`、`latency_ms` | 一次模型步的**真实**用量与延迟；`cached_tokens` 是 provider 报的缓存命中（不是估算） |
| `ContextAssembled` | `iteration`、`system_chars`、`tools_chars`、`history_messages`、`history_chars`、`tail_blocks`、`estimated_tokens`、`prefix_shared_chars`、`prefix_shared_ratio`、`digest` | 请求由什么拼成；`tools_chars` 计入估算（工具 schema 也是 prompt 的一部分，实测约 5k 字符） |
| `ToolSurfaceWarning`（R7e-3） | `tools_chars`、`threshold`、`origins`、`hint` | 工具面超阈值时**每个 loop 一次**的成本提示（纯观测，不改请求） |
| `InputSteered`（R7e-2） | `text_chars`、`iteration`、`total` | 用户回合中途补的那句话已经变成指令 |
| `Degraded` | `component`、`reason`、`fallback` | 增强组件静默降级**可见化**（压缩/记忆/落盘/隔离/观察者） |
| `ScopedEvent` | `scope`、`parent`、`event` | 子智能体事件带父子归属扇出到父观察者 |

字段扩展：`ToolStarted.iteration`、`ToolFinished.iteration`（默认 `0`）。

### 合同变更（仅两处）

| 变更 | 影响面 |
|---|---|
| `TurnStatus` 新增 `BUDGET = "budget"` | 时间/轮次/token 三种耗尽可区分；`core/subagent.py::_STATUS_MAP` 把 `budget` 映射为 `max_iterations`（干到一半，不是错误） |
| `TurnOutcome.usage: dict[str,int] \| None` | 回合终态自带账本（`prompt_tokens`/`completion_tokens`/`total_tokens`），调用方不必翻轨迹 |

### 工具上下文扩展

`ToolContext.call_id: str = ""`：分发器给**每次调用**一份独立上下文（一批调用共用一个 ctx），
这样并发批次里的执行能自证身份，子智能体的 span 也能挂到发起它的 `subAgent` 调用之下。

### 新的结构化拒绝码

| `ToolResult.error` | 含义 | `data` |
|---|---|---|
| `guard_denied` | 门卫拒绝（重复调用、配置红线、路径逃逸） | `{decision, reason, guard, tool}` |
| `guard_error` | 门卫自身抛异常 → 失败关闭 | 同上（`guard` 为出错守卫名） |
| `hook_blocked` | 用户钩子拒绝 | `{decision, reason, hook, tool}` |

改写（门卫或钩子）会在结果里留痕：`data.rewritten_from` + `data.rewritten_by`，
且 output 追加一行 `note: parameters were adjusted before execution`——模型必须知道它要的和执行的不是一回事。

### 轨迹文件（`core/trace.py`）

路径：`<workspace>/.ai-coding/traces/<session_id>/turn-<n>.jsonl`，一行一事件：

```json
{"v":1,"ts":"2026-09-24T00:56:26.849Z","session":"<id>","turn":1,"span":"i1","parent":"turn","iter":1,"evt":"ContextAssembled","data":{...}}
```

不变式：写盘前**强制脱敏**；默认只记元数据 + ≤500 字符预览（事件是显示通道，R1a-3 规定大输出不得搭事件的车）；`capture: full` 时额外写 `turn-<n>.full.json`，内容取自**会话**（全量文本的唯一真源），同样脱敏、同样受 `max_turn_bytes` 约束；
单回合超 `max_turn_bytes` 截断并写 `truncated` 标记；`TurnFinished` 结束后到达的
tail 事件（收割/整理）**追加到本回合文件**，不会凭空造出新回合。
`summary.json` 记该会话的累计用量，`os.replace` 原子写。

### 新增配置块

`tracing`（enabled/dir/capture/max_turn_bytes/retention_days/redact）、
`guard`（duplicate_threshold/bash_deny/bash_confirm/strict_paths/redact_history）、
`hooks`（enabled/timeout_seconds/plugin_dir/events[]）、
`ai.turn_token_budget`、`ModelConfig.price_input_per_1k` / `price_output_per_1k`。
三块全部有默认值，**旧配置不改也能跑**。

---

## R6 回合记忆 / 失败升级 / 人在环 / 记忆证据契约

R6 只改了**一处**对外行为（未完成回合的返回值），其余全是新增字段与默认值。

### 回合终态（`domain/loop.py`）

| 字段 | 说明 |
|---|---|
| `TurnOutcome.handoff: str = ""` | **harness 写的**收尾报告（目标 / 已完成 / 未完成 / 最近一次失败 / 产物 / 轨迹）。**不改 `text`**：记忆用 `text is None` 判断「本回合有没有结论」，子智能体用 `text` 当结论，轨迹要能区分「模型交付了没有」 |

`AgentLoop.process_input` 的返回值：`outcome.text or outcome.handoff`（R6 之前是 `outcome.text or ""`）。
`COMPLETED` 回合永远不产生 handoff（正常结束不编造「未完成」）。

### 会话新增字段（`SessionData`）

| 字段 | JSON 键 | 说明 |
|---|---|---|
| `turn_count: int = 0` | `turnCount` | 会话级回合号（1 起）。旧会话从 0 开始重新计数（不回溯） |
| `turn_digests: list[TurnDigest]` | `turnDigests` | 有界回合摘要环（空则不写，与 `task` 同规则）。坏行忽略 |

`TurnDigest`（`domain/turn_digest.py`）：`turn` / `request` / `outcome` / `status` / `files` / `openItems`（camelCase），
`render()` 产出一行。派生函数 `build_digest(...)` 不发模型调用；`render_working_memory(...)` 产出
`<working-memory>` 块（仅当本轮历史真被改写）；`append_digest(...)` 维护有界环且永不抛错。

### 记忆条目与事件（R6d）

| 契约 | 变更 |
|---|---|
| `MemoryNote.evidence: str = ""` | `tool` / `model` / `""`（R6 之前）。**只是记录，不做过滤** |
| `MemoryService.harvest(..., evidence="")` | 回合级事实由循环给出（本回合有无成功工具调用） |
| `MemoryService.status()["by_evidence"]` | 计数，`/memory` 会显示 |
| `MemoryHarvested.evidence: str = ""` | 进轨迹 |

`source_turn` 现在填**回合号**（`SessionData.turn_count`），不再填消息条数。

### 工具与人在环（R6c）

| 类型 | 字段 |
|---|---|
| `UserAnswer` | `answered: bool`、`text: str`、`reason: str`（`""`/`no_user`/`eof`/`empty`） |
| `UserPromptCallback` | `async ask(question, options=()) -> UserAnswer` |
| `CLIUserPromptCallback` | stdin 读一行（`asyncio.to_thread`，可被回合上限打断）；`input_fn` 可注入 |
| `UnavailableUserPromptCallback` | 立即返回 `no_user` |
| `AskUserTool` | `name="ask_user"`、`parallel_safe=False`、`max_duration_seconds=timeout+15` |

`ToolResult.error` 新增：`missing_question`、`no_user`、`no_answer`、`user_prompt_error`。
**注册规则**：`include_subagent and tools.ask_user.enabled and user_prompt is not None` 三者同时成立才注册。

### `AIConfig` / `ToolsConfig` 新增项

| 参数 | 默认 | 作用 |
|---|---|---|
| `ai.working_memory_keep` | 12 | 摘要环保留的回合数 |
| `ai.working_memory_chars` | 1200 | `<working-memory>` 渲染上限（含包装与提示行） |
| `ai.failed_streak_threshold` | 2 | 连续「全失败」多少轮后换策略；0 关闭 |
| `ai.max_strategy_nudges` | 1 | 每回合最多几次换策略提示 |
| `ai.stall_iterations` | 3 | 多少轮后若仍「尝试过但零成功」就提前收尾；0 关闭 |
| `tools.ask_user` | `ToolConfig(timeout_seconds=300)` | 提问工具的开关与等待上限 |

### 索引不变量（R6e）

`<memory-constraints>` 的 `index_max_chars` 约束的是**渲染后的整块**（含包装标签），
由三条测试锁死：每行都是指针、总量不超预算、普通事实增长不推高它。


## R7e 记忆写 / 转向 / 工具面告警 / 子智能体上下文契约

### `AIConfig` 新增项

| 配置 | 默认 | 作用 |
|---|---|---|
| `subagent_context_mode` | `full` | 子智能体起始上下文：`full`（父回合请求视图 + 父系统提示）或 `task_only`（干净窗口 + 组合根基础提示） |
| `tools_chars_warn` | `12000` | 工具面超此阈值时发一次 `ToolSurfaceWarning`；0 = 关闭 |
| `steering_max_messages` | `5` | 一次抽干最多吸收几条转向输入；多出来的留在队列 |
| `steering_max_chars` | `2000` | 单条转向输入的字符上限（超出截断并加省略号） |

### `AppConfig` 新增块

`ui: UIConfig`，目前只有 `ui.steering: bool = true`（回合进行中是否显示 `steer>` 提示并接受转向）。

### 字段与工具

| 契约 | 位置 | 说明 |
|---|---|---|
| `SubagentResult.context_mode` | `domain/subagent.py` | 这次子回合用的是哪种窗口（也在 `to_dict()` 里） |
| `BaseTool.origin` | `tools/base.py` | 工具来源，默认 `"local"`；`MCPToolAdapter` 返回 server 名 |
| `ToolContext.turn` | `tools/base.py` | 当前回合号（`source_turn` 用；默认 0） |
| `MemoryService.write_note(...)` | `memory/service.py` | 模型写记忆的唯一入口，内部复用 `_reconcile`，`evidence="model"` |
| 工具 `memory_write` | `tools/memory_write_tool.py` | 参数 `text`/`kind`/`importance`/`supersedes`；文本上限 500 字符 |
| `SteeringQueue` | `core/steering.py` | `put(text) -> bool`（`/` 开头与空行返回 False）、`pending`、`drain()` |
| `InteractiveReader` | `ui/reader.py` | **已删除（R10）**；历史契约：唯一读取器 + 仲裁（问题 / 转向 / 命令 / 输入） |


## R8 契约（模型写的 checkpoint / evidence / 流式与取消）

### `AIConfig` / `UIConfig` 新增项

| 配置 | 默认 | 作用 |
|---|---|---|
| `ai.working_memory_note_chars` | 120 | `checkpoint_write` 一句话的上限（替掉 digest 行的结果句） |
| `ui.tool_chunks` | `true` | 是否把工具输出片段（含 MCP 进度）逐行显示；关闭＝回到「结束时一行预览」 |

### 字段与工具

| 契约 | 位置 | 说明 |
|---|---|---|
| `TurnDigest.note` / `.source` | `domain/turn_digest.py` | 模型自己写的那一行，与「谁写的」（`model` / `harness`）。**落盘持久，注入易失**（只在压缩回合进 `<working-memory>`）；`source` 不进渲染文本 |
| `TurnDigest.display()` | 同上 | 环里显示的那句话：`note or outcome` |
| `build_digest(..., model_note="", note_chars=120)` | 同上 | note 非空时作为结果句；`outcome` 字段保留原值供回放 |
| `TurnBooks.checkpoint` / `.note_checkpoint(text, *, limit)` | `core/turn_books.py` | 本回合的 checkpoint（后写覆盖前写；空写不覆盖） |
| `TurnBooks.evidence_calls` | 同上 | **算作「世界证据」的成功调用数**：`checkpoint_write` 不计入（D-14）；收割用的是它 |
| `ToolContext.note_checkpoint` | `tools/base.py` | 工具写回回合状态的唯一通道（`checkpoint_write` 用） |
| `ToolContext.kill_event` | 同上 | 每个调用一份的 `threading.Event`；取消时由分发器点亮，`bash` 把它交给进程运行器 |
| 工具 `checkpoint_write` | `tools/checkpoint_tool.py` | 参数 `note`；ALLOW 档、`parallel_safe`、**只在组合根注册**；错误码 `missing_note` / `checkpoint_unavailable` |
| `memory.relevance.evidence_rank(note)` | `memory/relevance.py` | 召回同分时的 tie-break：`tool`=0，其余=1（`""` 与 `model` 同档） |
| `memory_search.evidence` | `tools/memory_search_tool.py` | `tool` / `model`（=「非 tool」）/ 空=不限；未知值 → `bad_evidence` |
| 事件 `ToolChunk(call_id, tool_name, stream, text, seq)` | `core/events.py` | 流式片段（`stdout` / `stderr` / `progress`）。**永不进请求 / 历史 / 会话文件** |
| `run_process(..., on_chunk=None, cancel=None)` | `infra/subprocess_runner.py` | `on_chunk(stream, text)` 从读线程调用；`cancel`（`threading.Event`）会 `killpg` 并 reap |
| `run_process_async(..., on_chunk=None, cancel=None)` | 同上 | chunk 经 `loop.call_soon_threadsafe` 回到事件循环 |
| `ProcessOutcome.cancelled` | 同上 | 取消语义（与 `timed_out` 区分）；取消/超时仍返回 `returncode=None` 与部分输出 |
| `RunCancelled(label)` | `infra/deadline.py` | 「用户不要等了」——与 `DeadlineExpired`（时钟到点）并列的第二种放弃理由 |
| `run_with_deadline(..., cancel=None)` | 同上 | 事件置位即放弃并抛 `RunCancelled` |
| `CANCELLED_BY_USER = "cancelled_by_user"` | `domain/message.py` | `ToolResult.error` 的取值（D-7：调用级结果，不是回合状态） |
| `TurnBooks.note_outcomes` 的失败计数 | `core/turn_books.py` | `cancelled_by_user` 的调用**不计入** `failed_streak`、不进 `failures`（D-11） |
| `ToolDispatcher(..., cancel=None)` / `AgentLoop(..., cancel=None)` | `core/dispatch.py` / `core/agent_loop.py` | 取消事件一路贯通；`_run_tools` 在每批开始前 `clear()`（一次 Esc 只影响一批） |
| `InteractiveReader.cancel_requested` / `request_cancel()` / `key_bindings()` | `ui/reader.py` | **已删除（R10）**；历史契约：Esc 键的落点（`for_terminal` 负责把它装进 `PromptSession`） |

## R9 契约（依赖门禁 / 轨迹出口）

### `AIConfig` 新增项

| 配置 | 默认 | 作用 |
|---|---|---|
| `ai.enforce_dependencies` | `true` | `todo_write` 的依赖门禁（add 校验 + 状态门禁）；`false` ＝ R1 行为，只给历史清单用 |

### 依赖查询（`domain/task.py`）

| 契约 | 说明 |
|---|---|
| `Blockers` | 冻结数据类，字段 `open` / `unknown` / `cycle`（都是 `tuple[str, ...]`）+ 只读属性 `any`；空＝可以走 |
| `TaskState.blockers(item_id) -> Blockers` | **纯查询**：`open` 是存在但未终态的依赖，`unknown` 是已不存在的依赖，`cycle` 是非终态依赖链又绕回自己的路径（老数据才有） |
| `TaskState.dependency_invariant() -> tuple[str, ...]` | `depends_on` 不指向「存在且更早」的条目的 id 列表；工具层用它保证新数据合法 |
| `TaskState.next_id() -> str` | 原来的 `_next_id` 公开出来（工具层要预判新 id 才能认出自指） |

### 工具与错误码（`tools/todo_write_tool.py`）

| 契约 | 说明 |
|---|---|
| `TodoWriteTool(enforce_dependencies: bool = True)` | 关键字构造参数；`_enforce` 是它的落点 |
| `unknown_dependency` | `add` 的 `depends_on` 里有列表里不存在的 id（含正向引用），清单不变 |
| `self_dependency` | `add` 的 `depends_on` 指向本次将要分配的 id，清单不变 |
| `blocked_by_dependencies` | `update` 到 `in_progress` / `completed` 时依赖未满足；`data` 带 `id` / `open` / `unknown` / `cycle` |
| 门的形态 | `in_progress` 与 `completed` 检查；`pending` / `blocked` / `dropped` 放行；**没有** `diagnose` 动作，三条出路写在 `output` 里 |

### 轨迹读取与导出（`core/trace.py`、`core/aggregate.py`、`core/export_*.py`）

| 契约 | 位置 | 说明 |
|---|---|---|
| `SessionRef(session, path, mtime, turn_files)` + `.turns` | `core/trace.py` | 磁盘上的一个会话目录 |
| `TraceStore.sessions() -> tuple[SessionRef, ...]` | 同上 | mtime 倒序、tie 按 session id；根目录不存在返回 `()`；从不抛异常 |
| `TraceStore.prune()` | 同上 | 改为走 `sessions()`，行为不变（`--since` 与它同口径＝目录 mtime） |
| `SessionRow(session, turns, files, steps, prompt_tokens, completion_tokens, cached_tokens, age_days)` | `core/aggregate.py` | 一行读数；`mismatch` = `turns != files`，`cache_ratio` = `cached / prompt`（prompt=0 时为 0.0） |
| `AggregateReport(rows, totals)` + `.cache_ratio` | 同上 | `totals.session` 固定为 `"(all)"` |
| `aggregate_sessions(rows)` / `row_from_summary(...)` / `build_report(store, *, since_days=0, now=None)` | 同上 | 前两个纯函数；`build_report` 是唯一的读盘入口，坏 summary 记零 |
| `SpanRow` / `TurnView` / `SessionView` / `read_session_view(store, session, *, full=False)` | `core/export_html.py` | 读盘 → 视图对象；`full=True` 才去读 `turn-<n>.full.json` |
| `render_session_html(view)` / `render_index_html(report, views)` | 同上 | 纯函数；单文件 HTML，内联 CSS、无 JS/外链、所有动态串经 `html.escape` |
| `rows_to_otlp(views, *, service_name="ai-coding")` | `core/export_otlp.py` | 按 `span` 分组 → `resourceSpans[0].scopeSpans[0].spans`；`traceId`/`spanId` 由 sha256 派生（**确定性**，与真 SDK 的随机 id 不同） |
| `render_overview(report)` | `ui/trace_render.py` | 纯函数，一张表 + `TOTAL` 行 + `!mismatch` 标注 |

OTLP 字段映射：`latency_ms` / `duration_ms` 的最大值决定 `endTimeUnixNano`（无时长则
`start+1` 并标 `ai-coding.instant=true`）；行内 `data` 逐键成 attribute（bool/int/float/str 各自成类型，
嵌套结构转 JSON 文本，`None` 丢弃）；任一行有 `error` → `status.code = 2`。

## R10 契约（`memory_search` 的候选集过滤与诊断）

### 候选集过滤（`memory/service.py`）

| 契约 | 位置 | 说明 |
|---|---|---|
| `MemoryService._candidates(*, kinds, evidence, scope, since_days, now)` | `memory/service.py` | **过滤在排序之前**：四个过滤器决定候选集，`k` 只花在候选上。R10c 之前是排完 top-k 再过滤，于是「四条 fact 压过唯一一条 decision」时 `kind=decision, k=3` 返回空 |
| `recall_with_diagnostics` / `recall_notes` 的 `kinds` / `evidence` / `scope` / `since_days` | 同上 | 默认 `None`＝不限；`recall_notes` 只做转发，过滤与 touch 各只有一个调用点 |
| `since_days` 的时间基准 | 同上 | 截止点 = `now - N 天`；`now` 缺省取真实时钟（`datetime.now(timezone.utc)`），测试注入 `now`。**0 与负数＝不限**；设了窗口时，没有可解析 `updated_time`/`created_time` 的条目被丢弃 |
| `scope` | 同上 | 精确匹配 `note.scope`。本仓库所有写入路径都不传 scope（`store.add` / `new_note` / `harvest._reconcile`），因此本构建写出的条目一律 `workspace`；别的值只能来自导入或外部直接构造 |
| `RecallDiagnostics.filtered_out` | 同上 | 只在「传了过滤 + 一条没命中」时测出，来源是**一次未过滤的排序**；探针不加 `limit`，所以它是集合大小而不是 `min(命中数, k)`。`0`＝没有可归咎于过滤的东西。探针不 touch 条目 |

### `memory_search` 的参数与返回（`tools/memory_search_tool.py`）

| 契约 | 说明 |
|---|---|
| `scope` / `since_days` | 新增、均可选；过滤在服务侧生效。工具自身**不再有第二条过滤路径**（`_filter_hits` 已删除），只有「服务返回什么就发什么」 |
| `data.terms_known` | **两个指代**：命中路径量的是过滤后剩下的候选集；没命中且传了过滤时量的是整个索引——因为被过滤清空的候选集会让排序直接返回一个不提名任何词的空结果，文案会把「没有未知词」读成「每个词都认识」。列出的词都确实存在，只是集合未必是模型以为的那个 |
| `data.near_misses` / `data.index` | 与 Task 4 相同：只在没命中时带，且都**有界**（3 条 / ~200 字符）；匹配不到任何查询词的近邻一律丢弃 |
| `output` 的没命中文案 | 三种情形分别说：词索引里一条都没有 / 被过滤排除（带**实测**条数，并点名调用方真正设过的过滤参数）/ 门槛拒绝（只有拿到索引概览时才说）。传了过滤时，末尾不再劝模型「按 kind 缩小范围」 |
