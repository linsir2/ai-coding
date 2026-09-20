# 功能清单

ai-coding 以「单回合对话 + 工具调用」为核心模型：用户输入进入 `AgentLoop`，组装系统提示后交给 AI 引擎（OpenAI Agents SDK），模型可发起工具调用，工具结果回喂，最终产出自然语言回答并持久化到会话。下面按能力域展开。

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

`MemoryService` 三层操作：`remember` 把提取的结论写入 `MemoryStore`；`recall` 按关键词检索最相关的既往笔记注入下一次系统提示；`dream` 在索引超过 `consolidate_threshold` 时聚合。真实路径由 LLM `extractor` 提取，缺失时回退到最近用户文本。任何异常静默降级，记忆操作永不抛错、永不阻塞回合。索引持久化为工作区下 `.memory` 单文件。

## 技能

`SkillRegistry` 扫描技能目录，凡含 `SKILL.md` 的顶层目录即视为一项技能；`catalog_text()` 把每项技能的 frontmatter `description` 汇成 `- name: description` 目录，供 `skill` 工具与系统提示使用。空目录不产生任何目录。

## MCP 集成

`MCPManager` 把 `MCPConfig` 翻译为 SDK 的 `MCPServerStdio`：仅保留 `enabled` 且 name/command 非空的服务器，MCP 块整体 `enabled=false` 时不连接任何服务器。构造完全离线，连接与工具发现发生在运行期 SDK 内部。MCP 是 SDK 边界，封装在单一的 config→SDK 映射点。

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

已知约束：bash 工具仅面向 Linux；`ask`/`repl` 需要提供 `--path`（配置发现不做自动探测，否则报 `no default model configured`）。