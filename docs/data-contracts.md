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
| `TurnOutcome.truncated_by_length` | `bool = False` | 最后一轮模型输出是否被 `max_tokens` 截断（`finish_reason="length"`）。裸停与写完无法区分，只有这一道可检测；置位时 UI 出红色终态块（优先于预算提示），而非静默当写完 |
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
R11a 加 `extraction_enabled=true`（写侧总闸，关则收割与自写全停、召回照常）、`extraction_extractors=None`（写通道白名单：`harvest`/`memory_write`，`None`/缺段/非 list = 全放行；判定谓词 `service.is_extraction_allowed`）。
R11b 加 `recall_timeout_seconds=2.0`（一次召回的墙钟预算，0/负/不可解析 = 关闭；超预算 → 命中照返 + 跳过 `touch` 写盘 + `data.timed_out=true` + 一条 `Degraded`）。**注意它在 `MemoryConfig` 与 `service._default_config()` 两处**，与 R10 的三处纪律同源：`RecallParams` 不读它，所以不需要改第三处。

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
| `scene`（R11c，默认 `""`） | 话题分组标签，提取器/模型写的**自由文本**（`normalize_scene`：strip + 80 字截断）。只做**分组与过滤**，不进 `dedupe_key`、不进打分。`""` = 未分组 |
| `source_message_ids`（R11c，默认 `()`） | `m<下标>` 位置引用，指向 `sessions/<source_session>.json` 的 `messages`。**只覆盖真正喂给提取器的那段窗口**（`harvest_max_messages`），不是整个 delta |

`.memory` 文件：`{"version": 2, "notes": [...]}`；读到 v1（`entries`）自动迁移为 v2 字段默认值。
**R11c 不 bump 版本**：新键全可选、缺失走默认，旧代码读新文件会忽略未知键——没有形状层面的不兼容。

### 收割契约（`memory/schema.py`，R3）

提取器输出 JSON 数组：`[{"text", "kind", "importance", "action", "target_id"}]`，
`action ∈ add|update|supersede|noop`。解析容错：代码块、前后散文、坏行、旧的「一行一条」格式都能吃，
完全不可用时返回空列表（记忆永不抛错）。

R11c 追加可选 `scene`（同一 `normalize_scene` 口径，非字符串被字符串化、`null`→`""`），并在
`ai/llm_helpers.py::_EXTRACT_SYSTEM_PROMPT` 里作为**可选**字段索要（有测试钉住这段文本，
因为收割路径上没有第二个生产者，改回去没人会发现）。

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
| `turn_usages: list[TurnUsage]` | `turnUsages` | 每轮 token 花费（`{turn, promptTokens, completionTokens, totalTokens}`），`reporter` 填数后在 `close()` 里追记并重存一次；零花费/无上报不写行、不重存。旧文件缺键即空表 |

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
| `data.terms_known` | **两个指代**：命中路径量的是过滤后剩下的候选集；没命中且传了过滤时量的是整个索引——因为被过滤清空的候选集会让排序直接返回一个不提名任何词的空结果，文案会把「没有未知词」读成「每个词都认识」。列出的词都确实存在，只是集合未必是模型以为的那个。**空 `terms_known` 有两种含义**：真的一个词都没量到（索引为空、或查询分词后为空），和不适用于上一行的旧路径；文案不得把它读成「每个词都认识」 |
| `data.near_misses` / `data.index` | 与 Task 4 相同：只在没命中时带，且都**有界**（3 条 / 500 条记忆的索引 JSON 实测 315 字符，验收脚本那份 40–88 字符的语料是 317，同一上限内）；匹配不到任何查询词的近邻一律丢弃。键是 `index.active` 而不是 `index.total`——`status()` 已经把 `total` 用在「含被取代的条目」上，这里数的是活跃集 |
| `output` 的没命中文案 | 四种情形分别说：词索引里一条都没有 / 被过滤排除（带**实测**条数，并点名调用方真正设过的过滤参数）/ 门槛拒绝（**且 `terms_known` 非空**——空 `terms_unknown` 只说明「没有未知词」，不说明「每个词都认识」）/ 索引实测为空（明说没有东西可搜，不劝换说法）。传了过滤时，末尾不再劝模型「按 kind 缩小范围」 |
| `output` 的词汇缺席句 | 只陈述测量到的事实：「这些词索引里一条笔记都没出现过：X、Y」+「只说明这些词本身没被记过，不说明记忆里有没有相关内容」。**不得**从词的缺席推出内容的不在场——`no_vocabulary_match` 只说明没有查询词的 `df > 0`，而跨词 bigram（如 `tokenize("怎么构建")` 的 `么构`）在任何相关笔记里都不会出现，其缺席对内容不构成证据。spec 5.2 把「见过哪些词」定为模型**换说法**的信号，所以换说法必须留在建议里 |

### `memory_search` 的 `data` 完整形状

命中和未命中共有六个键，未命中再加三个：

| 键 | 类型 | 命中时 | 说明 |
|---|---|---|---|
| `notes` | `list[dict]` | 有 | 每条 `id` / `kind` / `score`(三位小数) / `relevance` / `matched_terms` / `text` / `updated_time` / `pinned` / `evidence`。`matched_terms` 是**凭哪些词**回来的，可为空元组 |
| `no_match` | `bool` | `false` | `not notes` |
| `terms_known` | `list[str]` | 有 | 见上表「两个指代」 |
| `terms_unknown` | `list[str]` | 有 | 索引里一次都没出现过的查询词（CJK bigram 会被拆成 `['依赖','赖怎','么装']`） |
| `semantic_unavailable` | `bool` | 有 | **本轮恒为 `false`**。存在的唯一理由是可选语义层落地时 payload 不必改形状；现在没有任何代码把它置真 |
| `timed_out` | `bool` | 有 | R11b：本次召回**超过** `memory.recall_timeout_seconds`。命中照返（超时绝不饿死模型），被跳过的只有那一次可选的 `touch` 整文件写；工具同时经 `ctx.emit` 发一条 `Degraded(component="memory_recall")`。用 `getattr` 读，老 double 没有这个字段时读成 `false` |
| `near_misses` | `list[dict]` | **仅未命中时**（除非显式 `include_near_misses: true`） | `id`/`kind`/`score`/`relevance`/`preview`(40 字符)，最多 3 条 |
| `index` | `dict` | **仅未命中时** | `active` / `by_kind` / `recent`（3 条 `id`+`kind`+`preview`）。**没有索引概览时这个键不存在**（记忆关掉、排序抛错、或 pre-R10c 的 double），文案此时也不编造条数 |

失败路径另有三种 `success=False`：`error="empty_query"`、`error="bad_evidence"`（值必须是
`tool`/`model`/空，**在召回之前**校验，坏值到不了记忆层）、`error="memory_error"`。

### 排序与门禁的参数（`memory/relevance.py`）

| 契约 | 位置 | 说明 |
|---|---|---|
| `RecallParams` | `memory/relevance.py` | 冻结数据类。R10 新增/改动的四个字段：`threshold=0.25`（混合分下限，未改）、`min_relevance=0.25`（**R10 从 0.05 提上来**）、`relative_band=0.55`（**R10 新增**，卡混合分而非相关度）、`recency_source="created"`（**没接上的旋钮**，见下节） |
| **`threshold` 在出厂默认下恒被带宽地板蕴含（从不单独拒任何东西）** | 同上 | **实测，不是推断**：`weight_rank=0.55` 意味着 BM25 最高的那条笔记拿到 `0.55 * (raw/best_raw) = 0.55`，其余项非负，于是只要 `best_score > 0` 就有 `best_score >= 0.55`，带宽地板 `0.55 * 0.55 = 0.3025 > threshold 0.25`。因此**任何过了带宽的笔记必然也过了 `threshold`**，删掉 `item.score >= resolved.threshold` 这个条件在出厂默认下不改变任何一次召回。400 组随机语料实测：`best_score` 的最小值是 0.55，带宽地板从未低于 `threshold`；20 000 条随机语料 × 40 次查询里，14 805 条「过了 `min_relevance` 却被拒」的笔记中，**被 `threshold` 单独拒掉的是 0 条**。它只有在 `relative_band` 被配低（或 `threshold` 被配高）时才成为有效门——这正是它作为「兼容老配置」下限的意义。spec §7 用例 8「`threshold` 仍然生效」由 `tests/test_memory_relevance.py::test_r10b_threshold_still_gates_when_the_band_does_not` 按这个口径钉住 |
| **`min_relevance` 与 `relative_band` 的三处默认值** | `relevance.RecallParams`、`config/models.MemoryConfig`、`service._default_config()` | **三处都要改**，不是两处：`service._default_config()` 是每一个无 config 的 service 实际读的那份（`MemoryService.__init__` 在 `config` 为 `None` 时用它），只改前两处会让整个默认路径静默保留旧值 |
| `RecallParams.from_memory_config(config)` | 同上 | duck-typed（`getattr` + 默认），测试里的 fake 也能过 |
| `ScoredNote.matched_terms: tuple[str, ...]` | 同上 | 该笔记命中的**已知**查询词（未知名不算） |
| `RankingResult(kept, rejected, terms_known, terms_unknown, no_vocabulary_match)` | 同上 | `rejected` 是「最接近但没过门禁」那一侧，诊断文案与 `near_misses` 都从它来；`no_vocabulary_match=True` 表示没有任何查询词在候选集里出现过（注意：是**候选集**，过滤清空时它无意义） |
| `bm25_scores(notes, query_tokens, *, k1=1.2, b=0.75) -> dict[str, float]` | 同上 | 按 id 索引，所以 **id 必须唯一**：两条同 id 的笔记分数会**相加**。`MemoryStore` 保证唯一（uuid），但这是公开函数，前置条件未强制执行——保留为文档而不是 raise，加检查会把一次静默合并变成热路径上的新崩溃点 |
| `best_score` 的取值范围 | `rank_notes_detailed` | 在**全部**已打分条目上取，然后才跑 `min_relevance`。于是已被相关度拒绝的条目仍然抬高所有人的地板。这是刻意的（门禁不该被它可能压过的那道门禁定义），但只是一个选择：目前没找到它翻转保留项的案例 |

### 使用信号（`memory/store.py`）

| 契约 | 说明 |
|---|---|
| `MemoryStore.touch_notes(note_ids) -> int` | 记一次使用，返回实际 touch 到的条数。经 `_mutate` **批量**走，所以五次命中只写一次文件。未知 id 忽略而不抛错：这是读路径上的记账，不能变成召回失败的方式 |
| `MemoryNote.touch(when)` | 冻结值对象上的纯变换，返回新条目（`hits + 1`、`last_used`） |
| 调用点 | **只有一处**：`MemoryService.recall_with_diagnostics` 在 `hits` 非空时。被过滤掉的候选**不算使用**（模型没看见它），所以过滤必须在 touch 之前跑 |

### 诊断数据结构（`memory/service.py`）

| 契约 | 说明 |
|---|---|
| `IndexSummary(active, by_kind, recent)` | 有界概览：`recent` 最多 3 条 `(id, kind, preview)`，`preview` 40 字符。失败的召回**不该把整个索引塞回上下文** |
| `RecallDiagnostics(terms_known, terms_unknown, near_misses, index_summary, semantic_unavailable, no_vocabulary_match, filtered_out)` | 「召回为什么失败」所需的全部信息。外层各字段默认可空；`pre-R10c` 的 double 没有 `recall_with_diagnostics` 时，工具走兼容分支并用 `_EmptyDiagnostics` 占位 |
| `RecallDiagnostics.timed_out` / `.elapsed_seconds` | R11b 追加在末尾（默认 `False` / `0.0`）：排名（**含语义层与未命中探针**）实测耗时与是否超预算。计时窗口从 `_candidates` 之后起、到探针结束止——同步 BM25 没有挂起点可抢占，所以这是**事后计量**而不是 `run_with_deadline` 式取消；超预算只买「跳过 touch」，买不到「掐断计算」 |

### 没接上的两个旋钮

`memory.detail_max_chars` 在 `MemoryConfig` 里声明了，`_default_config()` 里也有
（1200），**全仓库没有任何地方读它**（`grep -rn "detail_max_chars" ai_coding/ tests/ --include=*.py`
只命中这两处声明和一个照抄默认值的测试 fixture）。本轮**故意不接**：召回返回的
`preview` 已经固定在 40 字符（`MemorySearchTool._note_payload` 与 `IndexSummary` 各有一处硬编码），
而 52 条语料里的笔记长度中位数 40.5、最长 88——也就是说这个旋钮真接上，第一件事是决定**要不要
让 `preview` 超过 40 字符**，那是 prompt 预算的改动，不是截断。声明留着是为了不破坏已有
配置文件；一次有行为后果的设计决定不该塞进检索轮次顺手做掉。

`RecallParams.recency_source` 是本轮**新引入**的同类旋钮，形状不同：`created` 是默认值、
`"accessed"`（按 `last_used` 衰减）是代码里真实存在的分支，`_time_of` 会读它，
`tests/test_memory_relevance.py::test_r10b_recency_source_accessed_is_off_by_default`
也钉住了两者行为不同。但**没有任何配置能打开它**：`from_memory_config` 不读它，
`MemoryConfig` 没有对应字段（全仓只有那一个测试设过 `"accessed"`）。

**本轮的选择是记录，不接线**，理由有三条，按重要性排：

1. spec 5.3 对这一项写的是「`accessed` 作为可选项……**在基准里做 A/B**」。那个 A/B 从没跑过，
   而 `accessed` 会改变每一条笔记的分数，也就是改变 R@1 / FP@5 这四个门禁数字。接上一个
   没有实测支撑的排序开关，就是拿门禁当调参记录用。
2. spec 5.3 的默认值是 `created`，「默认不变」也是它写的；接线本身不改变行为，但会把
   「R10 发布的是哪套 recency」变成一个配置问题而非一次决定。
3. plan 的「Deliberately uncovered」已经把它列为**故意不覆盖**（「§5.3 lists it as a
   parameter with a default of `created`, which is what is built; the alternative is never
   exercised」）。事后把它接上，等于推翻计划里已经写下的裁决，且没有新证据。

复访触发条件（写下来免得下次当成 bug）：跑一次 `recency_source` 的 A/B，拿到
`scripts/retrieval_cases.py` 的分类指标对比，再决定是否进 `MemoryConfig` +
`from_memory_config`（两处必须同时改——同一个字段有**三个**默认值站位，见上表）。

### R10d 可选语义档（`memory/embeddings.py`，默认关）

| 契约 | 内容 |
|---|---|
| `EmbeddingsConfig`（`config/models.py`） | `enabled/provider/model/dimensions/min_cosine/cache_path/timeout_seconds`，全有默认，默认全关。`provider` 指向 `models.<name>`，复用其 `base_url/api_key` |
| `data.semantic_unavailable` | 三态语义：`False` + 有命中＝词面或语义正常返回；`False` + 无命中＝词面没找到（语义没开或端点不支持，**静默**）；`True`＝配了但这次坏了（超时/网络/坏响应），结果是纯词面回退 |
| 关掉逐字节一致 | `enabled=false` 或整块缺席时，`memory_search` 的 output 与 data 与纯词面逐字节相同（归一化掉随机 id 后比对）。唯一允许不同的键不存在——连 `semantic_unavailable` 都保持 `False`，R11b 的 `timed_out` 在预算内同样是 `false` |
| 缓存 key | `sha1(base_url\|model\|dimensions\|dedupe_key)`。端点在 key 里：同名模型换端点不会吃陈旧向量。`dedupe_key` 已覆盖文本，笔记改写自动重算，旧条目成孤儿（不删） |
| 能力闩锁 | 端点首次 404 置位，之后短路零网络。配 deepseek 的用户每次搜索不再多等一次 612ms 往返 |
| `min_cosine=0.60` | 只作绝对门的 OR 分支（`coverage>=0.25 or cos>=min_cosine`），是 text-embedding-v4 上的测量值（正例 min 0.44 / 负例 max 0.66 重叠）。**换 provider 或模型必须重跑基准重标**，否则门要么漏放噪音要么错杀 |
| 融合权重 | 0.5/0.5 写死在 `relevance.py`（非配置项）。46 条上 w=0.7 多救 1 条但属噪声，未采纳 |
| `rank_notes_detailed(..., semantic=None)` | `None` 时与今天逐字节相同。`relevance.py` 不 import `embeddings`；`benchmark.py` 不 import 任何一边 |


---

## R11c 契约（scene 分组 / 来源位置引用 / skill 版本回显）

R11c 只加**可选字段与只读回显**，不改排序、不改门禁、不改落盘版本。

### 记忆条目与检索

| 契约 | 位置 | 说明 |
|---|---|---|
| `MemoryNote.scene` | `memory/note.py` | 自由文本标签；`normalize_scene` strip + 80 字截断。**不进 `dedupe_key`**：完全同文即使 scene 不同仍折叠成一条，scene 只影响近似合并与过滤 |
| `HarvestCandidate.scene` / `write_note(scene=)` | `memory/schema.py` / `memory/service.py` | 两个生产者：提取器 JSON 里的 `scene`，`memory_write` 的 `scene` 参数 |
| `_candidates(scene=None)` / `recall_*(scene=None)` | `memory/service.py` | 与 `scope` 同构的**精确匹配**，过滤先于排序；被过滤掉的候选不算使用（不 touch） |
| `store.find_similar(scene=None)` | `memory/store.py` | `None` = 不限（旧调用零改动）；`""` = 只匹配未分组那组 |
| 去重命中回填 | `memory/service.py::_reconcile` | 收割先写、`memory_write` 后补 scene 时，把 scene 补到既有条目上（否则最常见的顺序会静默丢弃标签） |
| `consolidate` 不跨 scene 合并 | 同上 | 场景是合并的第二维精确键，方向是收紧 |
| `IndexSummary.by_scene` / `.top_scenes` | 同上 | `by_scene` 只数非空 scene；`top_scenes` 是 `(scene, count, heat)`，heat = 该场景 `hits` 之和，按 heat 倒序取前 `TOP_SCENES=5`，名字做 tie-break（两次运行一致） |
| `status()["by_scene"]` → `/memory` | `ui/repl.py` | 非空才渲染成 `scenes: a=1, b=1`，未分组库不出现这一段 |

### `memory_search` 载荷

| 键 | 说明 |
|---|---|
| `data.notes[].source_refs` | 恒带（空 list），`list(note.source_message_ids)`。位置引用 `m<下标>` 指向 `sessions/<source_session>.json`；**稳定性前提**：`messages` 只 append、压缩只改副本、`ensure_tool_pairing` 在落盘前清洗。**若将来支持编辑/删除历史消息，此字段必须重审** |
| `data.index.by_scene` / `.top_scenes` | **仅当存在非空 scene 时输出**，否则键不存在（未命中为剩余会话付 token，空库不增字节） |
| 未命中文案 | 仅在 `top_scenes` 非空时追加一行「记忆里的场景分组：构建 1 条（被召回 4 次）…；可用 scene=<场景> 精确再搜。」——列出的是**库自己的词**，模型照抄即可命中（自由文本标签的换词问题靠这个缓解） |

### skill 版本回显

| 契约 | 说明 |
|---|---|
| `SkillRegistry.meta(name)` | `{name, version, status, description}`；文件不存在时返回**默认值**（v1/active/`description=None`）——「没有版本」与「版本是 1」是两句话，只有前者对不存在的 skill 为真 |
| `_parse_version` / `_parse_status` | 只取 frontmatter 首行；`version` 是**字符串**（无 semver 比较，`1.10` 不会排到 `1.9` 之下）；`status` 白名单 `active|archived`，未知值读作 `active`，不发明第三种状态 |
| `skill list` / `skill read` | 共用同一个 `_suffix`：`version != "1"` 才显示 `(vN)`，`archived` 再加 `[archived]` → **全部老 skill 输出逐字节不变**。`read` 成功新增 `data={name, version, status}`（此前无 `data`）。archived 仍可读，只陈述状态、不推荐替代品（那是策略，registry 保持中立） |
| `catalog_text()` | **不动**：它进系统提示词（L1 必须逐字节静态），且 `/skills` 复用它 |
