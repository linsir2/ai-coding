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
| `max_messages` | 50 | L1 裁剪阈值（保留头/尾见下） |
| `snip_keep_head` / `snip_keep_tail` | 3 / 20 | L1 裁剪保留条数 |
| `keep_recent_tool_results` | 3 | L2 保留最近工具结果数 |
| `per_result_persist_bytes` | 30000 | L3 单结果持久化阈值 |
| `l4_keep_tail` | 6 | L4 摘要保留尾部条数 |
| `auto_process_tool_results` | true | 是否自动执行工具结果回喂 |
| `max_tool_iterations` | 10 | 单回合工具循环上限 |
| `max_concurrent_subagents` | 3 | 子代理并发上限 |
| `subagent_worktree_isolation` | true | 子代理是否 worktree 隔离 |

`MemoryConfig`：`enabled`, `auto_extract`, `consolidate_threshold=10`, `max_index_entries=200`, `max_per_turn_injections=5`。

`MCPConfig`：`enabled=false`, `auto_discover=true`, `connection_timeout=30`, `servers[]`。`MCPServerConfig`：`name`, `command`, `enabled=false`, `args[]`。

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
