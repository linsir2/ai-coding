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

其余字段：

- `max_retries=3` / `retry_base_seconds=2.0`：P0-1 的有界重试预算，`0` 恢复旧行为。
- `price_input_per_1k` / `price_output_per_1k` / `price_cached_per_1k`：R4a/R11 的可选价格，`0` 表示未知，不假装 0 成本。
- `fallback_models: tuple[str, ...] = ()`：M2 的降级链，**元素是 `models` 的键名**（不是 `name`，
  两者可以不同，如 `qwen` → `qwen3.8-2.4t-a95b`）。可写列表 `[backup]` 或单个标量 `backup`。
  空元组表示不降级，工厂直接返回单个引擎，行为与 M2 之前完全一致。
  链在启动时解析：名字不存在或成环都直接抛 `ValueError`。

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

`MCPConfig`：`enabled=false`, `servers[]`。`MCPServerConfig`：`name`, `command=[]`（argv 一体，对齐 OpenCode v2）, `environment={}`（原 `env`）, `cwd`, `disabled=false`（极性反转，原 `enabled=false`）, `timeout{startup=60000, catalog=30000, execution=null}`（毫秒；startup 管握手、catalog 管 tools/list、execution 管单次调用，null=只受回合钟约束）, `tools=[]`（非空则只注册这些远端工具，R7d）, `auto_approve=false`（该 server 的工具免审批，R7d）, `permissions{allow=[], deny=[]}`（M2：按**远端工具名**授权的规则表，deny 优先且凌驾用户 always）。M1 起删 `auto_discover`（全仓无消费点）与全局 `connection_timeout`（被三段 timeout 取代）。

**M2 工具注入契约**：MCP 工具不再逐个注册为 function schema。LLM 只见一个工具 `mcp_call(server, tool, input)`，可调用的远端工具由 `MCPManager.available()` 单一来源决定（`tools` whitelist 生效、permission 规则**不**生效——目录可见性≠执行授权），渲染成有预算的目录文本（`build_mcp_catalog`：每条描述 ≤120 字符，总量 ≤8000 字符≈2000 token，超预算按配置顺序丢整个 server 并明示）挂进 loop 的 volatile tail **每步重建**（重连后新工具下一步即可见，不破坏 system prompt 静态的缓存纪律）。权限判定名 = `server__tool`（`BaseTool.permission_name`，`mcp_call` 对自身即返回该名）；审批提示、`ToolApprovalRequested` 事件、grant 落盘都用这个名字，operator 看到 `fs__write_file` 而非 `mcp_call`。`grant_key` 对 `server__tool` 返回全名本身（此前恒为 None，"Always" 对远端工具曾静默失效）。

**M3 生命周期契约**：`open_all` 非阻塞——只给每个可连 server 起一个 supervisor 任务即返回（操作者选定，对齐 OpenCode 的 fork 启动），故行新增第五态 `connecting`（`Connecting …`），首次 attempt 落定才变 settled 态；`per-server disabled` 归 `off`（detail「该 server 已禁用」，M1 极性翻转曾让它落到「与同名服务器重复」的错误归因）。每个 server 永久重试：间隔 1s→2s→4s…封顶 60s（无 jitter，Semaphore(4) 已吸收同步惊群），直到进程退出/`disabled`/`aclose`；`Degraded` 每 server 仅报首次。启动连接并发 ≤4。**掉线只在调用时可见**（owner park 后不读流，实测子进程退出无感知）：传输类异常（anyio `ClosedResourceError`/`BrokenResourceError`/`EndOfStream`/`ConnectionError`，惰性 import 判别）标记 dead（行 detail「连接已断开，正在重连」，工具当即从 catalog 摘除）并唤醒重连；普通工具错误不触发重连。IPC 新增 `mcp.connect{server}` → `{ok, reason?}`（失败返回不带 `error` 键——那是 JSON-RPC 错误字段，沿用 session.delete 的约定）；wire 事件 `mcp.status.changed{server}` 在每次 settle 时由 `make_handlers` 挂到 `service.notify` 推送，前端面板据此重拉 `mcp.list`；failed 行 enter 触发 `mcp.connect`（connected 无可重试、disabled 不可连、connecting 正在试）。

**M4 remote + OAuth 契约**：`type: local|remote`，remote 走 SDK 的 `streamable_http_client`（auth 经 `httpx2.AsyncClient` 注入，SDK 2.2.0 无 authProvider 参数）。OAuth 三件套只有应用能做的自己实现——`_TokenStore`（`~/.local/share/ai-coding/mcp-auth.json`，**0600、绝不进 config/trace/日志**）、redirect_handler（`webbrowser.open`，失败则日志打 URL 供 SSH 粘贴）、`_CallbackServer`（127.0.0.1 一次性监听，端口默认 19876、被占则 OS 分配并把真实端口写进 redirect_uri）；PKCE/state 校验/DCR/元数据发现/刷新全部 SDK 内部（`state` 由 SDK `compare_digest`，不重复造）。`oauth{callback_port, scope}` 两个旋钮——**无 client_id/secret**：DCR 覆盖大多数托管 server，预注册 client 是有真实需求再开的字段。connect 期不给 handler → 401 时 SDK 抛 `OAuthFlowError` → 行 `needs_auth`（第五态，OpenCode 的 `Sign in required →`）且 **supervisor 只等唤醒不进入 backoff 重试**（否则每秒打一次 AS 元数据端点）；`mcp.login`（IPC 同步返回=已启动，实际流程 spawn，模式同 session.turn）跑交互流程拿 token 落盘后唤醒 supervisor 普通连上；登录失败原因写到该 server 的 settled owner 上（行 detail 说真话）；`mcp.logout` 删 token 唤醒 → 回 needs_auth。异常分类走 `_find_leaf` 递归解 anyio ExceptionGroup（组本身的 str 只有 'unhandled errors in a TaskGroup'，对 operator 是废话）。

**M5 引导式连接契约**：`/mcp`（slash → `mcp.list` 命令）打开面板，面板 `a` 动作打开向导（`dialog-mcp-add.tsx`：type(local/remote)→字段(name/command|url)→trust(y/n)），向导收集后调 `mcp.add{name,type,command[],url,auto_approve}`。后端顺序：**先 adopt 后落盘**——`MCPManager.adopt(cfg)` 校验活体（重名/缺 command|url/master off）并起 supervisor；`auto_approve` 时同时 `gate.trust_server(name)`（gate 启动时构建，运行时要信任活体对象）；落盘写**兄弟文件 `mcp.local.yaml`**（gitignore，0600，`ConfigLoader.append_mcp_server`），**绝不程序化重写 config.local.yaml**（它有注释与 API key）。`load_mcp` 与 `load_app` 加载时都把兄弟文件的 servers 追加在主文件之后——world 交给 manager 的是 `AppConfig.mcp`，只改 `load_mcp` 会让新增的 server 重启后消失。落盘失败返回 ok+reason（本次会话仍连上，但明确告知不会存活到下次启动）。server name 必须匹配 `[A-Za-z0-9_-]+`：它就是工具名前缀（`server__tool`），带空格的名字会生成任何 provider 都不接受的工具名。快捷键：palette 从 `ctrl+p` 改为 `<leader>p`（ctrl+x p）——ctrl+p 在 VS Code 终端里被编辑器抢走。
**审批面板的 payload（2026-10-10 修）**：`approval.request` 一直带 `params`，但面板只渲染 `toolVerb`+`toolTarget`，MCP 的 `input` 被丢弃——批准一个看不见副作用的远程调用。现由 `util/permission.ts::permissionPayload()` 渲染：仅当 `toolName` 含 `__` 且 `params` 有 `server`+`input` 时输出，本地工具不受影响（避免和既有摘要重复）；值 >160 字符截断并加 `…`，块 >12 行截断并说明还剩几个，换行折成 `⏎` 以免把审批选项挤出屏幕。
**catalog 行格式（M6）**：`  {tool}{signature}: {description}`。`signature` 由服务器自己的 `input_schema` 渲染（`core/mcp.py::tool_signature` → `_schema_signature`）：顶层是参数列表 `(text: string, flag?: boolean)`，`?` 表示非 required，嵌套对象 `{a?: string}`，数组 `Array<T>`，`$ref` 只解析本地 `#/$defs/`（外部 `$ref` 渲染为 `unknown`，不在渲染目录时发网络请求）。三重护栏保证它是全函数：深度上限 8（自引用 schema 不会拖死一个 turn）、宽度上限 160（超出的参数以 `, …` 明示，不静默截断）、服务器没给 schema 时退回改动前的 `  tool: description` 行。**有签名时描述上限 120→80**，13 工具实测净 -16 token。
**catalog 分配策略（M6）**：`available()` 按配置顺序返回（`build_mcp_catalog` 的 docstring 一直这么承诺，而实现按连接完成顺序）。预算**跨 server 轮转**：每个 server 先得一个工具，再进入下一轮；一个工具都放不下的 server 输出 `{name} ({n} tools, none shown)`；连 header 都放不下的计入结尾 `(N more server(s) omitted: catalog budget)`。**记账包含 header、闭合标签和结尾说明**，因此 `len(text) <= budget_chars` 对任意预算成立（`build_mcp_catalog(..., budget_chars=10) == ""`，因为放不下任何 server 时空目录比"omitted"更有用）。
**开关与注册**：`mcp.enabled: false` 时 `build_world` 不注册 `mcp_call`，模型的工具数组里没有它（`tests/test_server_world.py` 真跑 `build_world` 断言）。gate 仍按名字知道 `mcp_call`——那是给 MCP **开着**时的幻觉配对用的，不受影响。
**工具卡片**：`tool.started` 带的是壳名 `mcp_call`，真实配对在 `params` 里；`util/toolmeta.ts::toolTarget` 因此对 `mcp_call` 返回 `{server}__{tool}`，一处改动同时覆盖实时卡片、会话回放和折叠时间线。

**记忆面板契约（M7）**：`memory.list` → `{notes:[{id,text,kind,scene,pinned,importance,evidence,hits,updated}], enabled, stats}`；`memory.pin{id,pinned}` → `{ok,pinned}`；`memory.delete{id}` → `{ok}`。三者都经 `world.loop.turn_memory` 转发到 `MemoryService.store`——store 在 `_build_loop` 内部构造、从不交给 `build_world`，所以那里是运行中唯一够得着它的入口；无记忆时三个都返回空/false 而不抛。
**面板不做的两件事（以及为什么）**：① 不按 scene 分组——33 个 scene 覆盖 43 条记忆且互相近似（记忆模块/记忆系统/记忆配置），kind 才是稳定分类法，scene 作副标题且可搜；② 不做实时刷新——`server/bridge.py` 明确把 memory 排除在事件桥之外，没有可订阅的信号，这里不发明一个，面板在打开时和每次操作后重读。
：① `mcp.add` 接受可选 `token`，转为 `headers.Authorization: Bearer <token>`；空 token 被丢弃而非发成空 bearer。token 不出现在任何拒绝信息、面板行、`config.summary` 或事件里（`tests/test_server_ipc.py` 三条断言）。② `_manual_bearer(server)` 为真时 `_remote_session_factory` **不构造** `OAuthClientProvider`——有 token 就没有 401 要处理，也没有元数据可发现（GitHub 端点不支持 DCR，带 token 时实测 1 次请求 / 5.1s，无 token 时 4 次请求 / 60s 耗尽启动预算）。③ 远程连接超时文案为 `未在 Xs 内就绪（远程：查网络是否可达，以及该端的授权服务器是否响应；若它要 token，请在向导里填 API token）`；本地超时文案不变。
**思考后停止的步骤（M7）**：模型返回 reasoning、无正文、无工具调用时，若 `finish_reason == "stop"` 且本回合尚未追答过，则注入一条 `user` 消息（"请直接给出最终答案"）并重跑本轮，计数在 `SessionData.thinking_retries`（不落盘）；否则按 `ThinkingOnlyStepError` 结束为 `ERROR`，文案含思考字数与 finish_reason。`finish_reason == "length"` 不重试（预算耗尽，同预算再问只会复现截断）。
**端到端证明**：`tests/test_mcp_end_to_end.py`（CI 以 `AI_CODING_MCP_STDIO=1` 跑）是唯一让 `MCPCallTool` 对着真实 stdio 子进程执行的地方——此前真实子进程测试全走 `MCPToolAdapter`，而 M2 之后生产已不再构造该类。它断言：catalog 进了首次请求、请求里只有 `mcp_call` 一个工具、审批问的是 `fs__echo` 而非壳名、结果以 `<tool_output>` 回到下一次请求并落进 session、拒绝时不伪造答案、猜错的 server/tool 会被点名真实工具。

YAML 加载规则：键做 camel→snake 归一化（`baseURL`→`base_url` 视为同一键）；**`environment` 的键是数据不是字段名，原样透传**（env 变量名大小写敏感，M1 修的坑：`TOKEN` 曾被归一化成 `token`）；未知键忽略；缺失时用模型默认值补全。加载函数 `ConfigLoader.load_app`（AppConfig）与 `load_mcp`（MCPConfig）分盒解耦。

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
| `max_auto_continuations` | `3` | **C3**：正常回合迭代预算耗尽且 todo 仍有 open 项时，注入 `[continue]` 提示并**重新武装迭代预算**（新一轮 `max_tool_iterations`），最多 N 次；`0` 关闭。不受 `stop_policy` 影响；每用户回合重置。**`blocked` 项不算未完成**：blocked 是「这一步现在做不了」（模型放弃或验收否决），与 C3「模型自己的活没干完」的前提相反；过滤只发生在 policy 的判定里，`open_items()` 仍把 blocked 视为 open（runner 下次 `/plan` 要重试它）。实测：单个 blocked 项从 6 次模型调用 / 3 次续跑降到 2 次 / 0 次。token 预算耗尽优先判终态（不白烧续跑额度）。plan-runner 步内强制 `0`（一步一个有界回合，共享计划 task 不能让单步续跑） |

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

### R0 验收 / 台账 / 截断契约

| 类型 | 字段 | 说明 |
|---|---|---|
| `AcceptanceItem`（`domain/acceptance.py`） | `id`、`statement`、`verify`、`passed`（None=未检，fail-closed）、`output`（尾 500 字符） | 一条可机检的陈述 + 证明它的 shell 命令；`SessionData.acceptance`，仅非空落盘（键 `acceptance`，Java 兼容） |
| `AcceptanceState` | `id`、`passed`、`output` | policy 可见投影：verdict 与证据同行，nudge 引用失败输出而不盲重跑 |
| `UncertaintyItem`（`domain/ledger.py`） | `id`、`question`、`kind`（epistemic/aleatoric/preference）、`level`（high/medium/low）、`evidence`（尾 120）、`resolves`（AC id） | 纯派生：验失败/工具错误聚合/失败连胜；H 只降在记录证据上 |
| `LedgerUpdated`（事件） | `items` | open 集签名变化才发（边沿触发）；transcript 显示 dim 行，trace 泛型记录 |
| `SubagentResult` 扩展 | `truncated`、`exit_reason`、`usage` | 未完成必须明说；ledger 文件（`to_dict`）保留新字段 |
| `ToolResult.error`（子跑） | `child_timeout` / `child_max_iterations` / `child_error` | 非完成态恒为机器码；max_iterations 仍算成功（streak 照 reset），细节在 `error_detail` 与输出文本 |

`TurnSnapshot` 扩展（全带默认值，老决策表不受影响）：`acceptance`、`acceptance_nudges`。`TurnPolicyConfig` 扩展：`max_acceptance_nudges=2`（均可经 `AIConfig` 同名键覆盖，无 schema 变更）。`TurnBooks` 扩展：`verify`、`tool_success`（名集合）、`ledger_signature`、`step_latencies_ms/abandoned_steps/last_step_latency_ms`。

### R1-1 计划执行契约

| 类型 | 字段 | 说明 |
|---|---|---|
| `TodoItem.verify` | `str = ""` | 证明该项的 shell 命令；`to_dict` 非空才写 `verify` 键，`todo_write` 的 `add`/`update` 透传；空 = 不可机检，走 todo 语义 |
| `SessionData.begin_plan(goal)`（R1-5b） | 建新 `TaskState(goal=goal)` + 清空 `step_deliveries`，返回新 task | **goal / items / 收据三位一体**：收据按 `stepId` 认领，而每次新清单 id 从 1 重启——带着旧收据走，新计划的完成会被 runner 的回填当成「已收据」而丢弃（实测：重置后 2 步全 done，回填产出 0 条）。消息历史与 `acceptance` 不动。**回合内换清单走同一条路**：模型 `todo_write(clear)` + 重新 add 时 id 同样从 1 重启，所以 `PlanRunner.run` 起步会调用 `_drop_foreign_receipts` 丢弃 statement 已不在当前清单里的收据——否则新计划的同名 id 被旧收据占住，out-of-band 完成永远拿不到收据（实测：计划 B 的第 1 步产出 0 条收据）。这是**有意的历史销毁**，与 `begin_plan` 同一取舍；只被细化（statement 未变）的清单则一条收据都不丢 |
| `StepDelivery`（`domain/plan_delivery.py`） | `stepId`、`statement`、`status`（completed/blocked/stalled）、`attempts`、`ac`（[{id, passed}]）、`summary`、`turns`、`usage`（R1-4）、`attested`（R1-5）、`seedChars`（M1，该步 seed 字符数；fiat 回填为 0，缺键或旧 snake_case 键读 0） | 一步的收据；`SessionData.step_deliveries`，仅非空落盘；老收据无新键照读默认值 |
| `PlanReport`（`core/plan_runner.py`） | `turns_used`（本轮 `process_input` 次数）、`tokens_used`（求和或 None）、`stopped` 新增 `budget:…` 值 | render 头行 `完成 n 步 · 用了 T 回合 [/ K tokens]`（R1-5b：n 只计 attested——本轮亲手结算的步；fiat 另列在下一行，`completed` 列表仍含全部 done id）；顶盖检查只在还有剩余工作时触发（正好用完跑完=干净结束） |
| `StepDelivery.proof`（派生属性，R1-6，不落盘） | `verified`（completed+attested+ac 非空即全过）/`on-claim`（completed+attested+ac 空）/`fiat`（attested=False）/`n/a`（未完成） | 只读推导，无字段、无 `to/from_dict` 改动；老收据 retroactive 可分级；非法组合 fallback 到 on-claim（宁可标没证过，不标证过） |
| `AIConfig.plan_step_max_iterations` | `int = 8` | plan-runner 单步迭代上限；runner 临时换 policy 副本（同时 `stop_policy=natural` 关 todo 门禁、`max_auto_continuations=0` 禁 C3），`finally` 恢复 |
| M1 短对话契约 | 每步全新 `SessionData`（`plan-<uuid>`、`ephemeral=True`），seed = goal + 计划当前状态（只列未完成项）+ 本步验收；步骤全文留子 session，父只收 prompt + 一行摘要 + 收据 | 每步上下文只由计划决定、不再由对话长度决定（实测 4 回合 41k/82k/130k/254k → 每步平稳；seed 仍随未完成项数增长，全计划合计对步骤数仍是二次，8/16/32/64 步 seed 2.4k/8.8k/34k/132k 字符）；`/plan-run` 删除，`/plan` 计划并执行；步会话不落盘（`TurnCloser.save` 对 ephemeral 跳过），不污染 `/resume` |
| `SessionData.ephemeral`（H1，不落盘） | `bool = False` | 内部会话旗标：正常 session 为 False；plan 步会话 True。`TurnCloser.save` 唯一持久化闸口处跳过（checkpoint/finalize 都走它） |
| `SessionData.continuations`（C3，不落盘） | `int = 0` | 本用户回合已用的自动续跑次数；`_begin_turn` 归零，续跑分支 +1；`to_dict` 白名单不含它（跨重启无意义） |
| `TurnSnapshot.continuations`（C3） | `int = 0` | 策略可见的续跑计数；循环顶部快照必须带 `open_items`（续跑判定的另一个输入），否则续跑永不触发（集成测试钉死） |
| `TurnPolicy.continuation_message`（C3） | `[continue] 上一轮预算耗尽，但还有未完成的任务项：…` | 与 `[task-gate]`（模型停手时）分开：C3 管"预算耗尽"，task-gate 管"模型停手"；两条消息都是持久化的 user 消息，replay 可重建 |
| `TurnBooks.total_iterations`（C3，H2） | `int`，`note_iteration` 每次 +1 | 跨续跑轮累计模型步数；`TurnOutcome.iterations`/handoff "N 轮" 一律用它，`books.iterations` 保持轮内序号（重启归零，预算判定用） |

### R1-4 思考时间线契约

| 类型 | 字段 | 说明 |
|---|---|---|
| `StepResult.reasoning` | `str \| None = None` | 该步的思考（完成态文本）；引擎 join 网关 `reasoning_content`/`reasoning` 增量，无新 sink（`step()` 签名不动，所有 fake 无感） |
| `ChatMessage.reasoning` | `str \| None = None` | 随步落盘；`_message_to_dict` 非空才写 `reasoning` 键（Java 兼容）；不满足 assistant 不变量（从不单独旅行，触发是响亮的错） |
| `AssistantMessage.reasoning` | `str \| None = None` | 事件携带；无则 blocks 映射为 `None`（prose 只走 delta 流，不断片） |
| 渲染 | `Block("reasoning")` dim + `Block("subagent")` dim | 推理块 `_cap 4000` 折叠（`Turn.append_reasoning`，静态挂载，WP4b 安全）；子跑只取 started/finished/带正文结论三类，chunk/遥测留 trace；live 与 replay 同序（prose→thinking，不虚构“先想后说”） |

禁令：`to_chat_message` 无视 `reasoning`（测试钉死）——思考永不重发给模型；子跑 reasoning 仍按 R1a-5 丢弃（父只看子行为，不见子心路）。实测：qwen3 系经 dashscope 产 `reasoning_content`（`1+1=?` 一问即 145 字），deepseek-chat 无（静默无块）。

### R1-2 后台子任务契约

| 类型 | 字段 | 说明 |
|---|---|---|
| `BackgroundTask`（`domain/subagent.py`） | `runId`、`name`、`task`、`status`（running/completed/timeout/error/cancelled/rejected）、`started/ended`（monotonic 秒）、`error`、`usage` | runner 内存表（`task_list()` 按 started 排序）；200 行封顶（只剪终态）；重启丢失 |
| `BackgroundTaskDone`（事件） | `run_id`、`name`、`status`、`summary`（尾 200）、`truncated`、`origin_session`、`ledger_path`（无 ledger 时 None） | 捕获的 emit 投递（可能晚 spawning turn 数轮）；transcript dim 行（有指针时缀 `· 完整回包: 路径`），trace 泛型记录 |
| `subAgent(background)` | `bool = false` | 默认同步（老行为不变）；true 时秒返 handle（`data={run_id, background, status}`），孩子事件经 observer 进父 trace |
| 错误码 | `subagent_busy` | 信号量满时 spawn 直接失败（不排队）；stop 未知 id 返回 False（`/tasks` 明示） |

### R1-3 结构化 handoff 契约

| 类型 | 字段 | 说明 |
|---|---|---|
| `SubagentResult` 新增 | `facts: dict \| None`、`schema_valid: bool \| None`（三态）、`schema_errors: tuple[str, ...]` | 无 schema 时全 None/`()`（老包零变化）；`to_dict`/ledger 同步带键；`_with_ledger` 重构同步复制（R0 前例陷阱） |
| `subAgent(output_schema)` | JSON object（`required` 键表是唯一被解释的关键字） | runner 把格式指令拼进孩子 task 串尾（task 即孩子 user 消息，无需动 loop/engine）；`_packet` 对截断后结论解析（facts 永不描述父没见过的文本） |
| render/data | `facts: k=v, …` 紧凑行（valid 时）/`⚠ 结构校验未过 (…)`（invalid 时）；`data += facts/schema_valid/schema_errors` | invalid 时 success 照旧（按 status 定），原文照留 |
| 错误码 | `schema_too_large`（序列化超 4000 字符）、`schema_invalid`（非对象） | 前置校验，runner 不碰 |

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

路径：`<workspace>/.ai-coding/traces/<session_id>/turn-<n>.jsonl`，一行一事件。**循环事件由 `AgentLoop._bind_observer` 编进 `Braid` 直达 recorder**（构造期传入的 recorder 会被每回合的 per-call observer 顶掉，所以编辫子必须在 rebind 处做）；**`ToolChunk` 不记录**——调用本身已由 `ToolStarted`/`ToolFinished` 各记一次，分片只会随输出体积增长挤占同一回合的 2MB 预算。实测真实 2 回合 ≈ 4.1KB/回合、10 类事件：

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
`ai.turn_token_budget`、`ModelConfig.price_input_per_1k` / `price_output_per_1k` / `price_cached_per_1k`（R11 新增：缓存输入价，0 = 未知，此时按 input 价计而不是按 0 计）。`/budget` 只在 `recorder.pricing` 有价格时追加 `≈ ¥x` 估算（拆分新鲜/缓存输入），无价格则只有 token。
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

~~`ui: UIConfig`，`ui.steering: bool = true`（回合进行中是否显示 `steer>` 提示并接受转向）。~~
**已删除**（GUI 复刻收尾）：该块的两个字段（`steering` / `tool_chunks`）在生产中零读取，
随旧 Textual UI 一并移除；`AppConfig`  `extra="ignore"` 会容忍配置里残留的 `ui:` 段。

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
| ~~`ui.tool_chunks`~~ | — | **已删除**（随旧 Textual UI）：工具输出片段现在由前端实时显示，无开关 |

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
| `clear` 的范围（R1-5b） | 清空 `items` **并清空 `goal`**——goal 是这张清单的目标，清单没了还留着它会让 `<task-state>` 把会话第一句提问当成已不存在的计划的目标（实测：计划回合末尾 `todo_write(clear)` 后出现 `目标: 这个项目最近一次提交干嘛的` + `（无任务项）`）。**这是永久的**：`_begin_turn` 只在 `task is None` 时新建 task，所以清单被清空后本会话不会再补种 goal，`<task-state>` 会一直显示无任务项；要新目标走 `/plan <topic>`（`begin_plan`）|

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
| `Skill` | M8 取代 `meta(name)`。一个文件 → 一次解析 → **一个冻结对象**，每个字段在每个实例上都存在。旧的 `meta()` 对不存在的文件返回 4 键、对存在的返回 6 键，于是读 `phase` 是一次等待正确输入的 `KeyError` |
| `parse_skill(path)` | 唯一的解析器。frontmatter **锚定在文件首行**（正文里的 `---` 分隔线不再被当成头）；一次 `yaml.safe_load`；失败则**逐行给畸形标量加引号后重试**（`description: Build for AWS: ECS` 是真实写法，旧的正则 fallback 就是为它而存在）；再失败则返回带 `error` 的 Skill 而非消失——Codex #11385 的教训是 skill 静默消失后唯一证据是日志里一行没人读的 warning |
| 身份 | **目录名**。OpenCode v1 用未校验的 frontmatter `name` 当身份，全局与项目 skill 同名就静默覆盖；v2 移到文件系统并把 `name` 降为展示。我们本来就是目录名，所以不动；**声明的 `name` 作为可接受别名**（`resolve()`），所以从别的 agent 拷来的 skill 两种拼法都能读，但系统里只有一个身份 |
| 未知字段 | 存进 `Skill.extra`，**不丢也不报错**。Codex 与 OpenCode 都发布了自家 loader 不读的字段，于是作者以为指令生效了而实际没有 |
| 标准字段 | `license`/`compatibility`/`allowed-tools`/`metadata` 读入并保存（Agent Skills 规范），本轮**不实现行为**——持有它们是为了将来能用 |
| `status: archived` | **不进 catalog、不被路由**，但**按名仍可读**（输出带 `[archived]`）。这是 Claude Code `disable-model-invocation` 的语义：禁止主动推荐，不禁止访问。拒绝读取会让一个误归档的 skill 彻底无法调试，而那已是一项策略，不属于一个没有闸门的工具 |
| 缓存 | 按 `(mtime, size)` 缓存。目录每个请求重建，但不再每轮重读每个文件 |
| `build_catalog(skills, budget)` | 取代 `catalog_text()`，**预算 8000 字符**（与 `core/mcp.py` 同值，因为两者都在系统提示词里争同一个前缀）。**三段预算分配**：先给每个 skill 名字（可达性的地板），再给描述，最后才是 triggers/phase 提示。逐个 skill 分配会让一个长描述把其余全部挤出目录——这正是 `build_mcp_catalog`「每个 server 先给一个 tool」的原因。压力下退化的是提示 → 描述 → 名字，**永不淘汰**（见下） |
| 为什么不用 Claude 的「按最少调用淘汰」 | 那是富者愈富策略，有冷启动死锁：从未被调用的 skill 使用历史为零 → 排第一 → 描述被删 → 模型匹配不到 → 永远不被调用。新 skill 结构性地挣不回预算。我们不需要淘汰任何人，因为名字这一级永远保留 |
| 目录变更声明 | `skill_review` 默认每 10 轮新增 skill，所以目录**必然**中途变化（三处文档曾声称不会）。`superseded_note(previous, current)` 把变化写出来；提示词只增不减，所以**移除必须写成否定句**（OpenCode v2 同理）。记在 `session.listed_skill_ids`，不落盘 |
| `skill list` / `skill read` | 共用同一个 `_suffix`：`version != "1"` 才显示 `(vN)`，`archived` 加 `[archived]`，解析失败加 `[broken]`。`read` 返回**正文**（不是整个文件），避免模型上下文里 frontmatter 出现第二次；`data={name, version, status}` |
| `SkillTool(registry)` | 接 registry 不接路径。旧版自己遍历目录、自己再解析一遍，于是可能与 catalog 不一致；更糟的是两个构建器拿到了**不同的 root**：`cli.py` 总是用 `<workspace>/skills`，而 `tools/builder.py` 只在传了 `--skills` 时注册工具——于是 `ai-coding -w .` 会向模型宣告一批它没有工具可加载的 skill |
| 推荐块的加载指令 | `skill(action="read", name="…")`。旧文本写 `skill(id="…")`，而 schema 是 `{action, name}`——那段提示每次推荐都注入一次，教模型发一个必然非法的调用 |
| `.pending/` 审批 | `skill_review` 生成的 skill 写入 `skills/.pending/<name>/SKILL.md`，**不进 live 树**；`.pending` 是点目录，遍历跳过它，所以未审批的 skill 不可读、不可路由、不可被宣告。TUI `a` 批准 / `d` 拒绝；非 TUI 入口本轮不提供 |

### 融合规则（R11d）

| 契约 | 位置 | 说明 |
|---|---|---|
| `RecallParams.recall_fusion` / `.rrf_k` | `memory/relevance.py` | 默认 `"blend"` / `60`。`RecallParams` 里**未识别的值按 `blend` 处理**（这个数据类被测试与脚本直接构造，召回中途不为一个没人校验的字符串抛错）；`MemoryConfig.recall_fusion` 是 `Literal`，**加载期**拒绝非法值 |
| `rrf_rank_term(lexical, semantic, *, k=60)` | 同上 | 两路各自按 `(-score, id)` 确定性排序 → `Σ 1/(k+位次+1)` → **除以 `len(lists)/(k+1)`** 归一到 `[0,1]`。只对**出现在该榜里**的条目计分（缺席只算那一路没表态）。归一不可省：不归一会让 `weight_rank` 的含义被静默改掉 |
| 只在有 semantic 时生效 | `rank_notes_detailed` | 无 semantic 时 `rrf` **回退 blend**：一路榜不是融合，且 RRF 会把词面排序从分数幅度换成名次，从而改变所有未开语义档用户的排序 |
| `MemoryConfig.recall_fusion` / `.rrf_k` | `config/models.py` | 有默认值，旧配置不改也能跑。`rrf_k` **不进 `config.example.yaml`**（只为重测存在的旋钮） |
| `service._default_config()` | `memory/service.py` | 两项同步（R10 记录的「三处默认值」纪律：`RecallParams` / `MemoryConfig` / `_default_config`） |
| `scripts/retrieval_cases.py --fusion {blend,rrf}` | 同上 | 只影响基准；`--with-embeddings` 的语义表现在打**与词面表同口径**的 per-category + reachable + 门禁行（此前只打全集与 paraphrase） |
| `--fusion rrf --check` | 同上 | **不失败**，只打印对照与一行「not gated」。默认关闭且实测更差的开关不能被写成契约 |

R11d 不改任何门禁、不改排序器签名、不改落盘；`recall_fusion="blend"`（默认）下今天的
`retrieval_cases --check` 逐位不变。

### R11c 实测契约（`scripts/r11c_cases.py`）

| 契约 | 说明 |
|---|---|
| 判据先于运行 | `ADOPTION_BAR=0.5` / `MAX_DISTINCT_SCENES=5` / `MAX_SCENE_CHARS=12` 是脚本头部常量，跑之前就定死；C1 红了不改 prompt 重测 |
| C1 判**池化**采纳率 | 提取器是模型，同一段对话这一轮给候选、下一轮可能什么都不给；按批判等于拿 1/1 读模型的脾气。`pooled_adoption(reports)` 按批加权，样本 <5 时只提示不判通过 |
| 分母含 JSON 兜底行 | `build_extractor` 在模型不遵守 JSON 契约时回退「一行一条」，这种行**永远没有 scene**，必须留在分母里（`Report.legacy` 单列） |
| C5 的正确形态 | 未命中 → 文案列出场景 → 用该场景**自己的笔记正文**作查询 + `scene=<标签>` 命中它 → 换另一个场景搜不到它。**不**把标签当查询（实测 `CI/CD` vs「CI 使用 GitHub Actions」证伪），**不**假设 `top_scenes[0]` 与 `recent[0]` 是同一条（实测证伪） |
| 闸门 | `AI_CODING_R11C_IT=1`（形态照 `tests/test_mcp_stdio.py` 的 `GATE`），未设则只跑离线臂并说明原因 |
| 分析器离线可测 | `analyze()` / `check_navigation_roundtrip()` / `pooled_adoption()` 是纯函数，由 `tests/test_r11c_measure.py` 用录好的样本驱动；**花钱之前先确认读数字的那段代码是对的** |

顺带一处既有的 doc-vs-code 缺口：`scripts/memory_cases.py:362` 的 docstring 写着 gate
`AI_CODING_MEMORY_IT=1`，**全仓没有任何代码读这个变量**（该文件未改动，仅记录）。

## M9 契约：作用域、全局 skill 库、安装与标准互操作

### 为什么有这一节

一次审计发现全仓只有**两个**用户级路径（MCP token 和 grants 文件），其余全绑在 workspace，
所以「换个目录」= 换掉模型姓��、key、skills、API key（后者直接导致起不来）。三处真 bug 让它
比「设计局限」更糟：配置发现锚在进程 CWD 而非 `-w`；目录优先级压过文件优先级导致
`config.example.yaml` 遮住 `config.local.yaml`（key 就在后者）；向量缓存跟着 CWD 走。

### 两层模型

`paths.py` 从两个根算全部路径，一次 `resolve`：

| 层 | 内容 | 位置 |
|---|---|---|
| **用户全局** | 配置（key/模型）、全局 skills、grants、MCP token | `~/.config/ai-coding/`、`~/.local/share/ai-coding/`（XDG） |
| **工作区** | memory、sessions、traces、transcripts、hooks、项目 skills、项目配置 | `<ws>/.ai-coding/`、`<ws>/skills/` |

XDG 表达式原本在 `grants.py`/`mcp.py` 各写一份，现在统一向 `paths.py` 取，有测试保证不再重复。

### 各契约

| 契约 | 说明 |
|---|---|
| `ConfigLoader.discover(start)` | 锚在**工作区**（非进程 CWD）；**真实配置优先于 example 配置**，无论谁离得近（example 是文档，不是配置） |
| `ConfigLoader.load_layers(paths)` | 全局 config 为 base，项目 overlay **深度合并、项目按 key 赢**。`mcp.servers` 是唯一累加的 list（全局服务器和项目服务器都是真的）；其余 list 替换，避免项目重开全局关掉的开关 |
| `load_layers_report` | 额外返回**每个顶层 key 来自哪个文件**——合并后的配置回答不了「这个项目为什么用这个模型」，而「两个目录行为不同」正是本改动要修的 bug |
| 坏层降级 | 某一层 YAML 损坏则跳过该层并告警，不致命——个人的坏配置不能让所有项目起不来 |
| `SkillRegistry(roots)` | 多 root，**项目在后即项目赢**（Claude Code 反过来让个人层压项目层，这会让仓库改写使用者的工具与约定）。冲突经 `shadowed()` 报告，不静默 |
| `SkillRegistry.write(root=)` | 默认写项目库；要装到全局须显式指定 root |
| `ai-coding skill install/list/remove` | 本地目录或 `owner/repo`（浅克隆、有界、入临时目录故远程无法自选目的地）。**装入的 skill 是模型会执行的指令**，故：默认拒绝覆盖同名（需 `--force`）、拒绝符号链接（仓库可携带 `skills/x/SKILL.md -> /etc/shadow`）、拒绝无 description 的 skill（description 是模型唯一的选它的依据） |
| 标准互操作 | 遵守 Agent Skills 标准：`name`/`description` 语义与 Claude Code/Codex 一致，`license`/`compatibility`/`allowed-tools`/`metadata` 读入保存；他运行时自定义的键（`context: fork` 等）保留在 `extra` 但**不呈现为已生效的指令**；YAML 破损的 description（如 `Build for AWS: ECS`）修复后重解析 |

### 项目指令：父目录也算（M9 补）

`ProjectInstructionsLoader` 原来只读 workspace 根，于是 `/projects/CLAUDE.md` 在
`/projects/sub` 里启动的会话中**静默不可见**——无报错、无警告，只是少了上下文。现在从
workspace 根向上走到用户主目录为止（home 是硬边界，否则 `~/CLAUDE.md` 会渗进机器上每个
项目），**由宽到窄全部拼接**，每层最多贡献一个文件（CLAUDE.md 优先于 AGENTS.md，
与原来同名优先级一致）。

预算从「每层 64KiB」改成**全栈一个 64KiB**——每层各一份的预算等于让模型收到四倍。
被截断的那层在尾部以 `(src/<层级名>)` 标出：只写「truncated」的话，一处被策略截断的
内容看起来和文件本来就结束在这里一模一样。

单文件场景与 M9 之前**逐字节一致**（既有契约），一级分隔符只在实际有多层时才出现。
某个文件读失败只跳过它，其余层照常生效（比原来的整体失败强）。

### 明确未做：memory 分层

`.memory` 仍**每工作区一份**。全局化需要给 `MemoryNote` 加 scope 字段、跨库召回合并，
并有把一个项目的技术决策带进另一个项目的风险。本轮有意推迟，记在此处。


真实库实测：43 条记忆、35 个标签、29 个是单例（67%）。因为标签是模型从**开放词表**里挑的，
它的数量**不会收敛**——Heaps 定律（`arxiv.org/pdf/0704.3316`）给出的是词表无界增长而
**头部占比稳定**，所以正确的目标是分出一个受治理的头部，而不是锁死一份词表。

量化危害：`scene=` 过滤的代价是召回，而这些标签里 67% 的只挂在一条记忆上——按它过滤只能
召回那一条，严格差于不过滤。

| 契约 | 位置 | 规则 |
|---|---|---|
| `IndexSummary.SCENE_SUPPORT = 3` | `memory/service.py` | 多少条记忆共用才算一个组。三条起，因为两条和巧合无法区分，一条就是全部问题 |
| `MemoryService.governed_scenes()` | 同上 | **读时**计算的头部：支持度 ≥3 的标签，按 heat 倒序、名字做 tie-break。不落盘、无新字段、无需迁移；标签靠被使用自动晋升 |
| `IndexSummary.governed_scenes` | 同上 | 头部标签名，供调用方判断"这个标签是不是组" |
| `top_scenes` | 同上 | **只取头部**。它是 schema description 指给模型看的东西，把单例塞进去就是教模型按不能收窄的标签收窄 |
| `by_scene` | 同上 | 仍是全部标签的原始计数——标签就在每条记忆上，只是不再被当成组 |
| `recall_*(scene=非头部)` | 同上 | **忽略该过滤并说明**，记在 `RecallDiagnostics.ignored_scene`。不报错、不静默丢弃：静默丢弃会让模型以为"这就是那个组的全部" |
| 命中与未命中文案都说明 | `tools/memory_search_tool.py` | 命中路径同样要说明，否则模型会以为过滤生效了 |
| `harvest` 头部提示 | `memory/service.py::_scene_hint` | 以 `<scene-groups>` 系统消息把头部标签给提取器，并写明"能归入就原样用，没有合适的可以自己起新标签，实在归不了组就省略"——**邀请复用，不禁止新造** |
| `normalize_scene` 不变 | `memory/note.py` | 词表若落在这一层，所有既有归一化测试会被打碎；闸门只在读时与过滤时 |
| 一次性拼写归并 | `scripts/migrate_scenes.py` | 手写映射，默认 dry-run，`--apply` 写入前把旧标签落到 `<store>.scenes.bak`。**逐条读原文定的**：模糊匹配会把"工具约束"和"执行约束"并到一起，而它们讲的是不同的事 |

