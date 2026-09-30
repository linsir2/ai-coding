# 功能清单

ai-coding 以「**自持 ReAct 循环** + 工具调用」为核心模型：用户输入进入 `AgentLoop`，
它自己驱动迭代——每轮压缩历史副本、组装系统提示、注入任务清单、调一次模型（`ChatEngine.step`），
必要时通过 `ToolDispatcher` 执行工具并把结构化结果回喂，直到满足停止条件。
整个循环、分发与权限都在本项目内，不依赖任何 agent 框架。下面按能力域展开。

## 入口

入口为 Typer 应用 `ai_coding.cli:app`。**R10 之后只有一种调用方式**：`ai-coding`（等价
`python -m ai_coding`）直接启动全屏 TUI。`--version/-V` 打印版本；`--path/-p` 指定配置；
`--workspace/-w` 指定工作区；`--skills` 挂载技能目录。

R10 之前存在 `config-check` / `ask` / `memory` / `trace` / `repl` 五个子命令，现已全部删除：
前四个的职责收进 TUI 的 slash 命令（见下节），`repl` 本身就是默认行为。

## 全屏 TUI（Textual）

界面由 Textual（`textual.App`）全屏渲染（`ui/textual_app.py`），取代 R10 的 `prompt_toolkit` 版本。
自上而下是状态栏、可滚动 transcript、输入栏；审批与 `ask_user` 是模态弹层（modal screen）。

| 区域 | 组件 | 内容 |
|---|---|---|
| 顶部 | `ui/status_bar.py` | workspace、当前 model、`● Ready / ● Working` |
| 中部 | `ui/transcript.py` + `ui/blocks.py` | 用户输入、assistant 流式 Markdown、工具事件块（开始/结束/chunk）、回合终态 |
| 底部 | `ui/input_bar.py` | 单行输入（Enter 提交；回合进行中禁用） |
| 弹层 | `ui/modals.py` | 审批（y/N/s）与 `ask_user` 提问 |
| 样式 | `ui/app.tcss` | 全屏布局与配色 |

键位：`Ctrl-Q` 退出；`Ctrl-C` 有选区时复制、无选区时退出；`Shift`+拖选使用终端原生选择。

## slash 命令

手打 `/xxx`（`ui/slash.py` 解析，`ui/repl.py::handle_input` 分发）；命令面板尚未接线（见 R12 节），
所以 `/` 本身只是普通文本。未知 `/xxx` 与裸 `/` 都会**当作普通输入发给模型**，不是错误。

| 命令 | 参数 | 功能 |
|---|---|---|
| `/help` `/h` | — | 列出全部直接命令 |
| `/status` | — | 会话 id、消息数、记忆条数 |
| `/model [name]` | 模型名 | 无参数列出可用模型（`*` 标记当前），带参数则切换 |
| `/resume [id]` | 会话 id | 无参数列出历史会话，带参数则恢复并返回 `NEW_SESSION` |
| `/permissions [auto\|confirm]` | 策略 | 无参数显示当前审批回调，带参数切换自动批准 / 逐次确认 |
| `/todo` | — | 渲染当前会话的 `TaskState`（目标、进度、逐项状态、下一步） |
| `/skills` | — | 列出 `SkillRegistry` 目录（`- name: description`） |
| `/plan` | — | 先弹 `What should I plan?` 收集主题，再以该主题发起计划回合（`ui/textual_app.py::_plan_turn`）。**不接受参数**：`/plan 改一下 loader` 会整条落回 `handle_input` 的旧固定 prompt，参数被静默丢弃 |
| `/review` | — | 向模型发一条「审查当前 git diff」的快捷 prompt |
| `/config` | — | 打印生效的 model / 上下文预算 / 迭代上限 / 停止策略 / 回合超时 |
| `/compact` | — | 主动压缩历史并落盘 |
| `/memory` | — | 记忆统计（总数、活跃、置顶、kind 与 evidence 分布） |
| `/trace` | — | 上一回合的轨迹摘要（token、耗时、降级、门卫拒绝） |
| `/budget` | — | 本会话累计 token 账本 |
| `/clear` | — | 开新会话（旧的仍可 `/resume`） |
| `/quit` `/q` `/exit` | — | 退出（`Ctrl-Q`） |

### 非命令键位

| 键 | 行为 |
|---|---|
| `Ctrl-Q` | 退出 |
| `Ctrl-C` | 有选区时复制选中文本；无选区时退出 |
| `Shift`+拖选 | 终端原生选择（在 app 捕获鼠标时仍可用） |


## 内置工具

主工具栈由 `tools/builder.py` 装配。默认 7 个工具随配置注册（bash/read/write/edit/glob 受 `tools.*.enabled` 控制），`todo_write`、`subAgent` 始终注册；提供 `--skills` 技能目录后再追加 `skill` 工具，共 8 个。
记忆开启时组合根再追加 `memory_search`（读）与 **`memory_write`（写，R7e-1）**——后者是模型自己写一条耐久记忆的通道；
**子智能体的工具栈里没有任何记忆工具**（它连写入口都不该有）。每个工具实现 `BaseTool` 契约（`name`/`description`/`params_json_schema`/`execute`），经 `sdk_adapter` 包装成 SDK FunctionTool。

| 工具 | 权限级别 | 行为要览 |
|---|---|---|
| `read` | ALLOW | 行号读取，支持 `offset`/`limit` 分页，上限 10 MB；读取后记录快照供过期读保护 |
| `glob` | ALLOW | 通配符匹配文件，过滤符号链接逃逸项 |
| `todo_write` | ALLOW | 计划模式待办维护（始终注册） |
| `subAgent` | ALLOW | 委派子任务；工作区为 git 仓库且开启隔离时在临时 worktree 内运行 |
| `skill` | ALLOW | 读取技能目录中某技能 `SKILL.md` 全文（需 `--skills`） |
| `write` | WARN | 写入文件；未先读覆盖目标会触发过期读保护 |
| `edit` | WARN | 精确字符串替换；`old_string` 须唯一或显式 `replace_all`；同样受过期读保护 |
| `bash` | WARN（危险命令 DENY） | 执行 shell 命令，Linux 专用，`timeout_seconds` 超时、输出截断 50 000 字符 |

`subAgent` 默认加入主栈；子代理内部栈不嵌套 `subAgent`，避免无限递归。

## 安全与权限

- **权限门**：`PermissionGate` 对每次工具调用判定 ALLOW / WARN / DENY。`read`/`glob`/`todo_write`/`skill`/`subAgent`/`memory_search`/`memory_write` ALLOW；`write`/`edit`/`bash` WARN；未知工具保守归为 WARN；bash 命中硬拒绝模式（`rm -rf /`、`sudo `、`shutdown`、`reboot`、`mkfs`、`dd if=`、`> /dev/sd`、强制 git push、`chmod 777` 等）直接 DENY。
- **审批接线**：WARN 层由 `approval` 回调解析。无人可问的入口（子智能体）用 `AutoApproveCallback`（WARN 放行，DENY 仍拦截）；交互 TUI 用 `CLIApprovalCallback`，对 write/edit/bash 经模态弹 y/N/s 询问用户。既满足了「AI 助手能写文件」，又保留了危险操作的确认与硬拒绝兜底。
- **沙箱**：`Sandbox` 将文件工具的所有路径解析限制在工作区内，`..`、绝对路径、符号链接逃逸一律抛 `SandboxViolation`。
- **过期读保护**：`FileStateTracker` 记录最近一次读取的快照，写入/编辑前比对 `mtime`+`size`；未被读过（`NEVER_READ`）或内容被外部改动（`STALE`）时阻止写入，防止盲写与覆盖外部修改。

## 上下文与回合编排

`AgentLoop.process_input` 单回合流程：追加用户消息 → 在副本上执行上下文压缩 → 组装系统提示（基础 + 项目说明 + 技能目录 + 记忆目录）→ 调用 AI → 追加 assistant 消息与严格配对的 tool 消息 → 清理配对 → 原子落盘 → 静默记忆回填。

### 回合上限与调用上限（R2）

| 机制 | 配置 | 行为 |
|---|---|---|
| 回合墙钟上限 | `ai.turn_timeout_seconds`（默认 600，`0` 关闭） | 到期 → `TurnStatus.TIMEOUT`：保留已流式输出的文本、落盘、发 `TurnFinished` 事件；含人工审批等待时间 |
| 单次工具调用上限 | 工具自报 `max_duration_seconds` 与「剩余回合预算」取小 | 到期 → **结构化** `ToolResult(error="tool_timeout", data={tool,timeout_seconds,reason})`，写回配对的 `role=tool` 消息，模型可以自行改策略 |
| 子智能体上限 | `ai.subagent_timeout_seconds` | 子回合自带（略紧的）上限，先自然收尾并给出回包；父侧只做兜底 |
| 辅助步骤降级 | 同上，但**不终止**回合 | 压缩超时 → 用未压缩历史；记忆召回超时 → 无记忆；记忆提取超时 → 跳过 |
| MCP 连接上限 | `mcp.connection_timeout` | 超时只跳过该 server（MCP 是增强项） |

原则：**核心等待（模型步、工具调用）到期就收尾；辅助等待到期就降级**。

### 任务清单与停止判定（R2 拆分为纯单元）

「要不要再来一轮」「模型停下来了但 todo 没做完怎么办」由 `core/turn_policy.py` 决定
（纯函数、无 IO），`AgentLoop` 只负责编排。决策表见 [R2 分析](r2-analysis.md) §6.7。

**四层上下文压缩**（`ContextManager`，调用顺序为硬约束）：

| 层 | 动作 |
|---|---|
| L3 | 超阈值的大段尾部工具输出持久化到 `transcripts/persisted/`，历史内替换为 `<persisted-output>` 占位 |
| L1 | 超 `max_messages` 时裁剪中间段，留 `snip_keep_head`/`snip_keep_tail` 条并插入 `[snipped n messages]` |
| L2 | 早于 `keep_recent_tool_results` 的旧工具结果换成**可回收的信封**：正文先落盘到 `transcripts/elided/`（内容寻址，同一正文不重复写），历史里留「清了多少字符 + 原文路径 + 头 `l2_window_chars` 字符 + 尾 `l2_window_chars` 字符」；窗口总量受 `l2_window_budget` 约束、**从新到旧**分配，预算用完之后只剩信封（路径仍在，所以信息永远取回得到）。比占位还短的正文原样保留（R7b） |
| L4 | 超上下文预算时用真实 LLM 摘要压缩历史（带 3 次失败熔断），降级为硬裁剪 |
| 兜底 | 硬裁剪 + `sanitize_pairs` 保证严格配对 |

所有阶段都在消息列表的浅拷贝上进行，绝不修改调用方历史。

## 记忆

R3 重写。记忆层现在回答三个问题：**写什么、什么时候写、以什么位置/形式交给模型**。

| 动作 | 行为 |
|---|---|
| 收割 `harvest` | 回合结束后，只把**本回合增量**（游标存在会话里）交给提取器；提取器输出结构化候选（`text/kind/importance/action/target_id`），服务逐条对账 **ADD / UPDATE / SUPERSEDE / NOOP**；同义（导语剥离后相似度 ≥0.80）合并；空结果不写；无提取器时**不写**（不再把闲聊原样入库） |
| 召回 `recall_notes` | 打分排序：**idf 覆盖度 + 新近性(半衰期 14 天) + 重要性 + 类型加权**，带**分数阈值 + 相关性硬门槛**；中文走字符 bigram + 虚词过滤；返回 id/kind/score/relevance 以便追溯 |
| 整理 `consolidate` | 合并近似重复、按命中提升 importance、淘汰过期 open_thread、按价值淘汰（`pinned` 与决策受保护）；`dream` 仅作为弃用别名保留 |

**注入位置（缓存约束，R3b）**：

| 通道 | 内容 | 缓存性质 |
|---|---|---|
| 系统提示 | 基础指令 + 项目说明 + 技能目录 | **逐字节静态**，会话内不变（测试锁死） |
| 尾部易失块 `<memory-constraints>` | `pinned` + 最近决策，≤600 字符，空则不注入 | 只有这一小块每轮变化 |
| 历史（只追加） | 对话、工具结果、`memory_search` 结果 | 追加不破坏前缀 |
| 工具 `memory_search` | 按需召回的详情（id/kind/score） | 结果进历史，**之后长期命中缓存** |
| 工具 `memory_write` | 模型主动写下的一条耐久记忆（R7e-1） | 走 `_reconcile` 这唯一写路径，带 `evidence="model"`，`/memory` 可查可删 |

实测对比（同一内容、同长度）：记忆放系统提示前缀命中 **7%**，放尾部 **94%**；
每轮压缩历史中段只有 **26%**（现在改为按预算分页压缩后 92%）。

**命令**：R10 起 `ai-coding memory list｜search｜forget｜pin` 已删除（无 CLI 入口）；
`MemoryService.write_note` / `MemoryStore.pin` / `MemoryStore.delete` / `store.all()` 仍在库里，
可从代码直接调用。TUI 里 `/memory` 看统计、`/compact` 主动压缩历史。
索引持久化为工作区下 `.memory`（schema v2，旧的 v1 文件只读迁移）。任何异常静默降级——
记忆永不抛错、永不阻塞回合，且受 R2 的回合预算约束。

## 技能

`SkillRegistry` 扫描技能目录，凡含 `SKILL.md` 的顶层目录即视为一项技能；`catalog_text()` 把每项技能的 frontmatter `description` 汇成 `- name: description` 目录，供 `skill` 工具与系统提示使用。空目录不产生任何目录。

## MCP 集成

`MCPManager` 用官方 `mcp` 包**直接连 stdio 服务器**：`open_all()` 逐个连接启用的 server、
`list_tools()` 发现远端工具，并把每个工具包装成 `MCPToolAdapter`（`BaseTool` 子类）
注册进**同一个工具注册表**——因此 MCP 工具与内置工具走**同一套权限门、审批与结构化结果**通道。

- 工具名带 `{server}__{tool}` 前缀，避免两个 server 同名工具互相覆盖；
- `MCPToolAdapter.parallel_safe = False`（远端副作用未知，保守串行）；
- 权限上落到「未知工具」分支 → WARN：非交互入口自动批准，交互入口弹确认；
- 单个 server 连接失败**静默跳过**，不影响其它 server 与主链路（增强组件不阻塞会话）；
- `aclose()` 统一拆除会话，幂等且对未开启的 manager 安全；
- 传输层可注入（`session_factory`），因此单测不需要真的 spawn 子进程。
- **R2 起**：每个 server 由一个「会话属主任务」持有（连接与关闭必须在同一任务里，
  这是 anyio cancel scope 的硬约束）；连接受 `mcp.connection_timeout` 约束，装死的 server 到点跳过；
  server 的 `env` / `cwd` 会透传给子进程；factory 在交出 session 前显式 `initialize()` 完成握手。

`mcp.enabled=false`（默认）时 `open_all()` 立即返回空列表，不连接任何服务器。

真连验证：`AI_CODING_MCP_STDIO=1 pytest tests/test_mcp_stdio.py -q`
（默认 skip 并打印原因，详见[集成测试](integration_testing.md) 的环境矩阵）。

**R4 实测（真实第三方 server，2026-09-24）**：

| 验证 | 结果 |
|---|---|
| 自建 echo server 端到端 | 模型调用 `echo__echo` → `echo:mcp works`，轨迹 11–59ms，会话里 tool 消息正确配对 |
| 官方 `@modelcontextprotocol/server-everything`（npx） | 发现 **13 个工具**（前缀 `everything__`），`get-sum(1234,5678)` → `6912` |
| 失败隔离 | 一个 `command: /bin/false` 的 server 被跳过，另一个照常工作，会话不中断 |
| 残留进程 | 运行结束后无孤儿 npx/python 子进程 |

**三个需要知道的代价与坑（R7d 之后都给了开关）**：

1. **工具 schema 会显著推高每一步的 prompt**：原生 function calling 每次调用都要发完整 schema，这份成本**每一步都付**。
   可复现的实测（`scripts/mcp_many_tools_server.py`，13 个「描述写得像真 server 那样啰嗦」的工具）：
   不用 MCP **4,280** 字符 → 全注册 **13,301**（3.11×）→ `servers[].tools` 只留 2 个 **5,666**（1.32×，**-57%**）。
   开关就是 `tools: [远端工具名]`（留空 = 全部，今天的行为）。
2. **冷启动曾经撞上默认超时**：`npx -y @modelcontextprotocol/server-everything` 首次运行（含下载）实测约 **27s**，
   旧的默认 `mcp.connection_timeout` 是 30s——R7d 起默认 **60**；npx/uvx 类 server 还可以再调到 90 或先预热缓存。
3. **MCP 工具默认仍是 WARN 级**（远端工具未知 → 保守 WARN）：无人可问的入口自动放行，TUI 里每次调用都会弹审批。
   若这个 server 是你自己声明在 `config.local.yaml` 里的，可以按 server 声明 **`auto_approve: true`**，
   它的工具就不再弹审批（**默认 false，不声明就完全是今天的行为**；bash 的硬拒 DENY 表仍然优先）。
   审批与超时都会留下事件（`ToolApprovalRequested`、`ToolFinished.error=approval_timeout`），可在轨迹里看到。

R4 起，server 连不上不再静默：会发一条 `Degraded(component="mcp_server", reason="server '<name>': did not become ready within Ns")`
并跳过它的工具（`/trace --degraded` 可见）。

## 命令分发与普通输入

`parse_slash` 只做**纯解析**（离线、无终端状态）：已知 `/xxx` 返回 `SlashCommand`，
其余（普通文本、未知 `/xxx`、裸 `/`）返回 `None`，调用方把它交给模型跑单回合。
`handle_input` 是一处 if/elif 链，产出 `REPLAction`（`CONTINUE` / `QUIT` / `NEW_SESSION`）
与（可能已替换的）会话对象——`NEW_SESSION` 供 `/clear` 与 `/resume <id>` 使用。

R10 之前由 `InteractiveReader` 独占 stdin 并在「问题 / 转向 / 命令 / 输入」四个消费者之间仲裁
（`ui/reader.py`，含 Esc 取消当前工具批次的 R8c 语义）。全屏 App 自带焦点栈与按键分发，
该文件随之删除；取消与转向的语义在 R12 的 v1 里仍未接回，见下方「R12：Textual UI」节。

## 可观测层（R4a）

每回合默认落一份**轨迹**（`<workspace>/.ai-coding/traces/<session>/turn-<n>.jsonl`，一行一事件，
写入前强制脱敏，默认只记元数据 + ≤500 字符预览），并维护会话级 `summary.json` 账本。

| 能力 | 说明 |
|---|---|
| 用量审计 | `ModelStepFinished` 带**真实** `prompt/completion/total` 与延迟；回合终态 `TurnOutcome.usage` 是累加值 |
| 预算归因 | `ContextAssembled` 分别记 system / **tools schema** / 历史 / 各尾部块的长度与估算 token（工具 schema 实测约 5k 字符，漏掉它估算会差一个数量级） |
| 缓存度量 | **provider 真值优先**：OpenAI `prompt_tokens_details.cached_tokens`、Anthropic `cache_read/creation`、DeepSeek `prompt_cache_hit/miss`；不认识就不猜（`None`）。实测 DashScope 会上报 |
| 缓存前缀兜底 | 端点不报 cache 时，用「与上一次请求的最长公共前缀」比率兜底（纯追加回合实测 0.95） |
| 成本 | `ModelConfig.price_*` 配置后才算；未配置返回 `None`，不假装是 0 |
| 降级可见 | 压缩超时、记忆读失败、落盘失败、worktree 隔离失败、观察者异常等都会发 `Degraded` 事件 |
| 子智能体 | 子循环事件以 `ScopedEvent` 扇出到父轨迹（父子 span 可重建；推理仍不回传） |

命令面：TUI 里 `/trace`、`/budget`（R10 删掉 `ai-coding trace` 子命令后，`TraceStore` 的读取入口仍在库里）。

## 门卫（R4b）

权限门是静态表（「这个动作危不危险」），门卫是**带状态**的运行时判定（「这个动作在此刻还得当吗」）。

| 守卫 | 默认 | 行为 |
|---|---|---|
| `duplicate_call` | **开（阈值 2）** | 同一 `(工具, 参数)` 第 3 次调用起拒绝，并指出上一次结果的 call_id |
| `bash_policy` | 空表不生效 | 配置追加 bash 红线 / 必须确认模式 |
| `path_escape` | 只观察 | `bash` 引用工作区外绝对路径时记录；`strict_paths: true` 才拒绝 |
| `secret` | history 默认不脱敏 | 轨迹永远脱敏；开启后才改写进入历史的工具输出 |

安全规则：**改写过参数必须重新过权限门**（否则钩子/门卫就是绕过硬拒表的通道）；
门卫抛异常 = 拒绝（失败关闭）；拒绝仍然写回配对的 `role=tool` 消息，配对永不破。

回合级 token 闸门走 `core/turn_policy.py`（纯单元），超限以 `TurnStatus.BUDGET` 收尾。

## 钩子（R4c）

`hooks.events` 配置，默认关闭。两种形态：

- `kind: command`：子进程，stdin 一行 JSON（默认**脱敏**，`raw: true` 才给原值），stdout 一行 JSON；
- `kind: python`：进程内回调，从 `plugin_dir`（默认 `<workspace>/.ai-coding/hooks`）自动发现。

点位：`turn.start`、`step.before`、`tool.before`（唯一可 block/rewrite）、`tool.after`、
`compact.*`、`memory.harvest`、`subagent.*`、`turn.end`、`stop.blocked`。

纪律：能力矩阵校验（点位不支持的能力直接报配置错误）；同一批钩子串行、按配置顺序；
子进程走 `infra.subprocess_runner`（进程组 kill）并受超时约束；**钩子环境不含模型 API key**（白名单构造）；
失败语义按能力分（观察/注入 fail-open，阻断/改写 fail-closed）；`inject` 只进**易失尾部块** `<hook-context>`，
不进系统提示、不进历史（保住 R3 的缓存前缀纪律）。

## 回合摘要环与收尾交接（R6a）

压缩会挖空中段：实测 60 轮工具密集历史里消息一条没少，但除最后 3 条外所有工具正文都变成
`[Previous: used bash]`，而 L4 摘要在结构压缩之后才评估，几乎不触发（见 r6-analysis.md §1.1）。
于是模型要么考古（实测：用 bash 翻 270KB 的会话文件，prompt 从 3,363 涨到 6,363），
要么抱怨「工具输出没有返回给我」。

R7b 把这段话的前半句治掉了：L2 不再丢正文。实测同一份 70 轮历史，套 R6 的真实配置
（`max_messages=50`）压缩后，被清掉的最旧那条工具结果的头一行
（`build-backend = poetry.core.masonry.api …`）**现在就在请求里**——正是那个 fixture 里
「先看看现在用什么」的答案；代价是压缩后 est tokens 从 2,371 涨到 2,743（+372，预算的 0.8%），
三条正文全文各落一份盘（7.5KB）。默认配置（`max_messages=400`，L1 不触发）下是 69 条信封、
其中 20 条带窗口（`l2_window_budget=4000` 用尽）、69 份原文共 205KB（docs/r7-analysis.md 3.2）。

`domain/turn_digest.py` 给出**由 harness 派生**的一行回合摘要（用户请求 + 终态 + 产物 +
未完成项），**不额外调用模型**：

> R8a 之后这一行**也可以是模型自己写的**：`checkpoint_write` 的 note 会替掉「结果那句话」，
> harness 的产物与状态照旧补在后面（详见文末「R8」一节）。不写就与今天逐字节相同。

- **有界环**：`ai.working_memory_keep`（默认 12）条，最旧的先淘汰，随会话落盘（`turnDigests`）；
- **只在该注入时才注入**：本轮历史**真的被改写**时（`Compactor.did_compact`）才把
  `<working-memory>` 作为**易失尾部第四块**（顺序 memory / task / hook / digest）附上；
  普通回合一分钱不花（`ai.working_memory_chars` 默认 1200 字符 ≈ 230 est tokens，占预算 0.5%）；
- **只覆盖最近 12 轮**：更早的决策属于记忆层（pinned decision 进 `<memory-constraints>`），
  不是摘要环的职责——这条边界是实测确认的，见 r6-analysis.md §10。

**收尾交接**：回合因轮次 / token / 时钟 / 引擎原因中断、而模型没留下话时，
`TurnOutcome.handoff` 会填一份 harness 写的报告（目标 / 已完成 / 未完成 / 最近一次失败 / 产物 / 轨迹），
`process_input` 返回 `text or handoff`。这是 R6 **唯一**的对外行为改变：以前这种情况返回空字符串。
`text` 语义不变（仍然只表示「模型说了什么」），因为记忆用 `text is None` 判断本回合有没有结论，
子智能体也用它当结论。

## 失败升级三档（R6b）

| 档 | 触发（可测量） | 动作 |
|---|---|---|
| 局部修 | 同一 `(tool, params)` 重复 | 门卫拒绝（R4b 已有） |
| 换策略 | 连续 `failed_streak_threshold`（默认 2）轮工具**全军覆没** | 注入一次 `[strategy-gate]`（每回合至多 `max_strategy_nudges=1` 次），要求先读错误、换手段或提问 |
| 收尾 | `stall_iterations`（默认 3）轮后**尝试过调用但零成功** | 提前 `FINISH` + 交接报告（R6a） |

「尝试过但零成功」是判据的关键：只说话不调工具的回合归停止策略管（`strict` 会一直提醒），
这条边界被既有测试当场钉住。提前收尾的 `TurnStatus` 仍是 `max_iterations`，但报告标题写
「零进展（提前收尾）」，不谎称预算用尽。

## ask_user：模型主动提问（R6c）

与被动审批互补：审批是「这一调用能不能做」，`ask_user` 是「我该做哪个」。

- 阻塞式：答案作为**工具结果**回给模型，避免「回合停在半个工具调用上」被配对清理掉；
- 有界：`tools.ask_user.timeout_seconds`（默认 300）+ 15s 收割余量，等待走线程池，
  因此回合上限和调用预算真的能打断它；
- **没人也是答案**：非交互入口（`ask`）与子智能体**不注册**该工具（工具列表是第一道 harness）；
  即使注册了而没人，也返回 `no_user` 并让模型「明确写出假设后自行决定」；
- 空行 / EOF 记 `no_answer`，通道自身出错记 `user_prompt_error`——都不会中断回合。

## 记忆证据（R6d）

每条 note 带 `evidence`：`tool`（该回合有工具调用成功）/ `model`（该回合只说话）/ `""`（R6 之前）。
它**只是记录，不是过滤**：实测两次「不许用工具就断言」的回合，提取器写的都是
`open_thread` 而不是编造的 fact，而样本里最重要的一条记忆恰恰来自用户声明——用户声明不需要任何工具调用。
`/memory` 会多打一行 `evidence: tool=N model=M`，`MemoryHarvested.evidence` 进轨迹。
同时修正了 `source_turn`（以前填的是消息条数，实测一条属于第 1 回合的 note 写着 16）。

## 会话持久化

`SessionService` 在 `sessions/` 目录以 `{id}.json` 落盘，原子写入（临时文件 + `os.replace`）。文件结构与 Java `sessions/*.json` 字节兼容，camelCase 键，完整消息体包含角色、内容、时间戳与成对的工具调用/结果。`--session` 可续接指定会话。

## 现有功能与里程碑状态

| 里程碑 | 交付 | 状态 |
|---|---|---|
| M0 | 骨架、domain 契约、配置、CLI 壳、infra | 完成 |
| M1 | AI 服务、AgentLoop 主循环、会话持久化 | 完成 |
| M2 | 8 个内置工具、权限门、计划模式待办 | 完成 |
| M3 | 上下文压缩、记忆、技能、Prompt 装配 | 完成 |
| M4 | 子代理 + worktree 隔离 | 完成 |
| M5 | MCP 集成、REPL/富文本 UI | 完成 |
| M6 | 收尾、全量迁移测试 | 完成 |
| R1 | 编排层重构：自持循环 + todo 门禁 + 分发器 + 子智能体 + MCP 直连 + 移除 SDK | 完成 |

已知约束：bash 工具仅面向 Linux；需要提供 `--path`（配置发现不做自动探测，否则报 `no default model configured`）。

## R12：Textual UI（v1 会话闭环）

R12 把 R10 的 `prompt_toolkit` 壳整体换成 Textual，并接回 R10 断掉的数据流；旧 UI 模块
（`app.py` / `body.py` / `header.py` / `footer.py` / `palette.py` / `scroll.py` / `theme.py` /
`markdown.py` / `observer.py`）连同 `prompt_toolkit` 依赖一起删除，`ai-coding` 只有一个入口。

**已接线（v1 会话闭环）**：

- 循环事件 → `ui/blocks.py::TranscriptObserver` → transcript（工具开始/结束/chunk、任务清单、回合终态）；
- 输入提交：Enter 提交自然语言跑完整回合（回合进行中禁用输入，命令与回合串行）；
- 审批：`CLIApprovalCallback` 经 `set_provider` 接到 `ui/modals.py::ApprovalScreen`；provider 收到的是
  **结构化的 `(tool_name, params, reason)`**（不是格式化后的 prompt 字符串），所以模态里显示的就是 gate
  实际决定的那份参数，不存在截断。键盘操作与 CLI 一致：`y` 允许 / `n` 拒绝 / `s` 中止 / `Esc` 拒绝；
  焦点默认落在 **Deny**，所以裸 Enter 是拒绝而不是批准；
- `ask_user`：`CLIUserPromptCallback` 经 `set_provider` 接到 `AskScreen`；
- slash 命令：`TextualConsole` 把命令输出写进 transcript；`/model <name>` 经 `AgentLoop.set_model` 真的切换。

**v1 明确不做**：欢迎页、侧栏、计划卡；`Esc` 取消工具批；steering 接回。
（命令面板**已实现**：输入 `/` 唤起内置 `CommandPalette`，覆盖全部 slash 命令。）

**一条已知退化**：

1. **模型驱动的 slash 命令不渲染工具事件块**：`/review`、未知 `/foo` 在
   `handle_input` 里直接调 `loop.process_input` 且不传 observer，期间的 `ToolStarted` /
   `ToolFinished` 不进 transcript（流式回答本身仍进 transcript）。`/plan` 不在此列——
   `_plan_turn` 自己调 `process_input` 并传了 `observer`，工具块照常渲染。状态栏会照常置忙
   （命令与回合同样串行），缺的只是工具事件块；旧 UI 的 loop 级 observer 会渲染，属 v1 退化。

**验证方式**：`tests/test_pty_textual.py`（真 pty 的审批/ask_user E2E）、
`tests/test_blocks.py`（事件→块）、`tests/test_textual_app.py`（`run_test` 驱动的壳与提交），
另以真实终端人工验证流式输出、模态与 `Ctrl-Q`。



## R1 带来的变化（相对 M6）

| 能力 | M6 | R1 |
|---|---|---|
| 迭代循环 | 由 OpenAI Agents SDK 内部驱动，不可见 | **自持循环**，每轮有事件与落盘点 |
| 轮次上限 | `ai.max_tool_iterations` 是死配置 | 真正生效，耗尽即 `MAX_ITERATIONS` 并保留部分成果 |
| 任务清单 | `todo_write` 纯内存、不注入、不落盘 | `TaskState` 一等公民：每轮注入、跨回合持久化、可门禁停止 |
| 停止条件 | 只有「模型不再调工具」 | 显式 5 条：自然结束 / 轮次耗尽 / todo 门禁 / 用户中断 / 审批 STOP |
| 工具结果 | 一段文本 | 结构化：`output`（给模型）+ `data`（给程序）+ `artifacts` |
| 工具执行 | SDK 内部，看不到放行过程 | `ToolDispatcher` 统一收口：权限门 + 审批 + 有界并发 + 结构化拒绝 |
| 子智能体 | 空历史起跑，只回一段文本 | **共享父回合上下文**，返回结构化信息包并落 ledger |
| 回合中断 | 无 | 可取消，保留已完成的工具轨迹 |
| 提示可见性 | 无 | `ask`/`repl` 打印 `[turn i/n]`、`[tool: x]`、结果预览、任务清单 |
| 依赖 | openai-agents（含 numpy） | 零 agent 框架：`openai` + `mcp` |

## R8：模型写的 checkpoint、evidence 过滤、工具流式与取消

1. **模型可以自己写那一行摘要**（R8a）：`checkpoint_write(note)` 把一句话交给当前回合，
   它替掉 digest 行里的「结果句」，harness 仍然补 `（改了 …）` 与非 completed 的 status；
   上限 `ai.working_memory_note_chars`（默认 120）。三条边界：它**只**进会话内的环（要活到下一个
   会话用 `memory_write`）、**只在压缩回合注入**、写完的那句话**第 N+1 轮才可能被看到**。
   实测（真模型）：模型真的会用它——`scripts/r8_cases.py --real` 里那一条的 digest note 是
   「README 不存在于工作区；下一步向用户确认…」。
2. **`evidence` 从「只能看」变成「能用」**（R8b）：按需召回（`memory_search`）在**分数与相关性
   完全相同**时才用来源破平局（`tool` 优先，老数据的空值不惩罚）；`memory_search` 新增
   `evidence` 过滤（`tool` / `model` / 空=不限，未知值报 `bad_evidence`）。合并近重复的判定
   **不动**（`_better_of` 仍按 importance → hits → 时间）。
3. **长命令不再黑屏**（R8c-S1）：`bash` 的输出边跑边进控制台，每次调用 0.15s 最多一行
   （`ui.tool_chunks`，默认开）。chunk 是**纯观测**：不进请求、不进历史、不进会话文件，
   模型仍然只拿到最终的 `ToolResult`。
4. **Esc 丢掉当前这一批，而不是整个回合**（R8c-S2）：工具结果回
   `error="cancelled_by_user"`，模型换做法继续；被取消的调用**不算失败**（不会触发换策略提示）；
   `bash` 的进程组会被真的杀掉（不是只不再等）。Ctrl-C 仍是整回合中止。
5. **MCP 的进度也能看见**（R8c-S3）：服务器调 `ctx.report_progress` 时，适配器把它转成同一路
   `ToolChunk(stream="progress")`；`scripts/mcp_progress_server.py` 是可复现的真 server。

验收脚本：`.venv/bin/python scripts/r8_cases.py`（离线 13 条）；
`--real` 再加 3 条真实用例（真模型一个回合、真模型 + 真 bash 的流式、真模型的 checkpoint 往返）。

## R9：依赖门禁 + 轨迹的三个只读出口

### 依赖硬门禁（R9a）

`depends_on` 从 R1 起就是「只渲染、不强制」的装饰字段。R9 让它变成约束，但**只约束在工具层**，
领域对象保持宽松（`TaskState.update()` 的语义一个字都没变）：

1. **`add` 只接受存在、非空、不自指、不重复的依赖**：正向引用（`depends_on=["2"]` 而 2 还不存在）
   现在返回 `unknown_dependency`，自指返回 `self_dependency`，清单不变。原来可以造出互指环
   （`1→2` 再 `2→1`），现在第一条就被拒。
2. **`in_progress` / `completed` 要检查依赖**：任一依赖未终态就返回 `blocked_by_dependencies`，
   `data` 里带 `open` / `unknown` / `cycle`；`pending` / `blocked` / `dropped` 一律放行。
3. **错误信息自带三条出路**（不加 `diagnose` 动作）：完成依赖、把作废的依赖 `dropped`、
   环里丢一项。模型看到的是可执行的下一步，而不是一句「不行」。
4. **开关** `ai.enforce_dependencies`（默认 `true`）。关掉＝回到 R1 行为，只给历史遗留清单用。

由此新数据天然是 DAG：`add` 拒绝未知 id，而 `depends_on` 创建后没有任何修改入口，
所以**不需要环检测算法**；`Blockers.cycle` 只负责把老数据里的环**说清楚**。

### 轨迹的三个出口（R9b/c/d）

轨迹本来就是 `turn-*.jsonl`（一行一个 span）+ 每会话一份 `summary.json`。R9 补上一个读路径和三个出口，
三者共用同一份数据，**不改写入格式、不新增依赖、不发网络请求**：

| 出口 | 命令 | 形态 |
|---|---|---|
| 跨会话总览 | `ai-coding trace overview [--since N] [--format json]` | 一张表 + `TOTAL` 行（turns / files / steps / prompt / completion / cached / 命中率 / 龄期） |
| 单文件 HTML | `ai-coding trace export --format html --session <id> --out t.html` | 内联 CSS、无 JS、无外链；**每个动态字符串都 `html.escape`** |
| OTLP/JSON | `ai-coding trace export --format otlp-json --session <id>` | 按 span 分组的真 span 树；`sha256(session\|turn\|span)` 派生 id，导出两次字节一致 |

`TraceStore.sessions()` 让「有哪些会话」可枚举（目录 mtime 倒序、tie 按 id），`--since` 与 `prune`
**共用同一个口径**（目录 mtime），所以「prune 会删什么」和「什么算旧」永远不会打架。
某会话的 `summary.json` 缺失或损坏时记零（不抛异常），`summary.turns != 磁盘回合数` 时**只标注**
`mismatch`、退出码仍为 0。导出**默认不含**消息全文，`--full` 显式打开（`turn-<n>.full.json` 只在
`capture: full` 时才会存在）。

验收脚本：`.venv/bin/python scripts/r9_cases.py`（离线 12 条）；
`--real` 再加 1 条：真模型跑一个回合，断言那份报告真的把这次会话算进去了。
