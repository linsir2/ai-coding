# 功能清单

ai-coding 以「**自持 ReAct 循环** + 工具调用」为核心模型：用户输入进入 `AgentLoop`，
它自己驱动迭代——每轮压缩历史副本、组装系统提示、注入任务清单、调一次模型（`ChatEngine.step`），
必要时通过 `ToolDispatcher` 执行工具并把结构化结果回喂，直到满足停止条件。
整个循环、分发与权限都在本项目内，不依赖任何 agent 框架。下面按能力域展开。

## CLI 入口

入口为 Typer 应用 `ai_coding.cli:app`，三种调用方式等价：`ai-coding ...`、`python -m ai_coding ...`、`python ai_coding/cli.py ...`。

| 命令 | 作用 | 关键参数 |
|---|---|---|
| `--version` / `-V` | 打印版本号 | — |
| `config-check` | 加载配置并打印生效值（模型清单、工具开关、上下文预算） | `--path/-p` |
| `ask` | 执行单回合（带工具），一次 Prompt 拿到最终回答 | `--prompt`、`--session/-s`、`--path`、`--workspace/-w`、`--skills` |
| `repl` | 进入交互式 REPL | `--path`、`--workspace`、`--skills` |

## 内置工具

主工具栈由 `tools/builder.py` 装配。默认 7 个工具随配置注册（bash/read/write/edit/glob 受 `tools.*.enabled` 控制），`todo_write`、`subAgent` 始终注册；提供 `--skills` 技能目录后再追加 `skill` 工具，共 8 个。每个工具实现 `BaseTool` 契约（`name`/`description`/`params_json_schema`/`execute`），经 `sdk_adapter` 包装成 SDK FunctionTool。

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

- **权限门**：`PermissionGate` 对每次工具调用判定 ALLOW / WARN / DENY。`read`/`glob`/`todo_write`/`skill`/`subAgent` ALLOW；`write`/`edit`/`bash` WARN；未知工具保守归为 WARN；bash 命中硬拒绝模式（`rm -rf /`、`sudo `、`shutdown`、`reboot`、`mkfs`、`dd if=`、`> /dev/sd`、强制 git push、`chmod 777` 等）直接 DENY。
- **审批接线**：WARN 层由 `approval` 回调解析。非交互 `ask` 默认 `AutoApproveCallback`（WARN 放行，DENY 仍拦截）；交互 `repl` 用 `CLIApprovalCallback`，对 write/edit/bash 弹 y/N/s 询问用户。既满足了「AI 助手能写文件」，又保留了危险操作的确认与硬拒绝兜底。
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
| L2 | 早于 `keep_recent_tool_results` 的旧工具结果替换为 `[Previous: used X]` |
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

实测对比（同一内容、同长度）：记忆放系统提示前缀命中 **7%**，放尾部 **94%**；
每轮压缩历史中段只有 **26%**（现在改为按预算分页压缩后 92%）。

**命令**：`ai-coding memory list｜search｜forget｜pin`；REPL 里 `/memory` 看状态、`/compact` 主动压缩历史。
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
（默认 skip 并打印原因——沙箱内子进程的 asyncio 循环醒不过来，详见
[集成测试](integration_testing.md) 的环境矩阵）。

## 交互式 REPL 与 slash 命令

`repl` 基于 prompt_toolkit，用富文本输出。`/` 前缀走 `parse_slash` 直接命令解析：

| 命令 | 别名 | 作用 |
|---|---|---|
| `/help` | `/h` | 显示帮助 |
| `/status` | — | 显示当前会话 id 与消息数 |
| `/clear` | — | 开启新会话 |
| `/quit` | `/q`、`/exit` | 退出 REPL |

非 `slash` 输入交给模型执行单回合，输出用 wide/cyan 样式实时流式展示。

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

已知约束：bash 工具仅面向 Linux；`ask`/`repl` 需要提供 `--path`（配置发现不做自动探测，否则报 `no default model configured`）。

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
