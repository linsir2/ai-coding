# 功能清单

**P0 根因修复：✅ 全部完成**（2026-10-08 酒店会话复盘驱动）

| 任务 | 根因 | Commit | 核心改动 | 验收 |
|---|---|---|---|---|
| **T1** 工具 precheck + 沙箱错误可行动 | R2/R3 | `a2a8173` | `BaseTool.precheck` 契约，4 个文件工具、dispatcher、sandbox 消息加补救 | 17 次越界→0，6 次假审批→0 |
| **T2** /plan 工作区闸门 | R2 | `2532f08` | `outside_workspace_paths` 纯函数 + 双闸门（topic/items） | 真实酒店 topic 0 次模型调用即止 |
| **T3** 记忆形状门 + 存量清洗 | 记忆 HIGH#3 | `d5d87a7` | `is_plausible_note_text` 拒 JSON 碎片；`_load` 自愈；`.memory` 46→29 | 0 碎片、0 酒店进度态 |
| **T4** 账本回合语义 | R8/A6 | `c0357df` | `begin_turn` 重置 ledger | 同 loop 两回合 usage 互不污染 |
| **T5** 压缩指令锚定 + 事件 | R5 | `4cdeb36` | `_ensure_pinned` 保护当前指令 + `ContextCompacted` 事件 | T15 形状复现、golden 等价测试过 |
| **T6** ledger-gate 退役 | R8/A8/D1 | `da9e33e` | 删 gate 分支，保留 `LedgerUpdated` 观测；清理死代码 | 123 passed，grep 干净 |

**当前门禁**：`2024 passed, 2 skipped` / ruff clean / mypy 116 files clean。

---

ai-coding 以「**自持 ReAct 循环** + 工具调用」为核心模型：用户输入进入 `AgentLoop`，
它自己驱动迭代——每轮压缩历史副本、组装系统提示、注入任务清单、调一次模型（`ChatEngine.step`），
必要时通过 `ToolDispatcher` 执行工具并把结构化结果回喂，直到满足停止条件。
整个循环、分发与权限都在本项目内，不依赖任何 agent 框架。下面按能力域展开。

## 入口

入口为 Typer 应用 `ai_coding.cli:app`。**R10 之后只有一种调用方式**：`ai-coding`（等价
`python -m ai_coding`）直接启动全屏 TUI。`--version/-V` 打印版本；`--path/-p` 指定配置；
`--workspace/-w` 指定工作区；`--skills` 覆盖技能目录（默认含用户全局库 + 项目库）。
配置分层为 `~/.config/ai-coding/config.yaml`（全局，含 key/模型）叠加项目配置，无需每个项目
各放一份 key。子命令 `ai-coding skill install/list/remove` 管理技能（install 支持本地目录与
`owner/repo`；默认拒绝覆盖同名，需 `--force`；装入即模型会执行的指令，拒绝符号链接与无
description 的 skill）。

R10 之前存在 `config-check` / `ask` / `memory` / `trace` / `repl` 五个子命令，现已全部删除：
前四个的职责收进 TUI 的 slash 命令（见下节），`repl` 本身就是默认行为。

## 全屏 TUI（OpenTUI 前端）

界面由 `ai_coding/frontend/`（TypeScript + OpenTUI + Bun）全屏渲染，经 `server/` 的 Unix socket IPC
与后端对话；它取代了 R10 的 `prompt_toolkit` 版本与随后的 Textual 版本（`ai_coding/ui/`，
GUI 复刻里程碑中已删除）。旧 Textual 实现的布局/组件细节见文末「历史」备注。

| 区域 | 实现 | 内容 |
|---|---|---|
| 顶部 | `ai_coding/frontend/src/component/session-tabs.tsx` | 多会话标签（切换式）+ New |
| 中部 | `ai_coding/frontend/src/state/` + `component/` | 用户输入、assistant 流式 Markdown、思考折叠块、工具事件块（开始/结束/chunk）、子智能体嵌套行、todo/台账 dim 行、回合终态 |
| 底部 | `ai_coding/frontend/src/component/prompt.tsx` | 输入（Enter 提交；回合运行中仍可输入，提交的内容进队列，当前回合结束后按序自动执行；`/resume` 走会话对话框） |
| 弹层 | `ai_coding/frontend/src/dialog/` + `component/permission.tsx` | 审批（←/→ 选择，y/a/n 快捷键）、命令面板 / 模型 / 会话 / 技能 / 状态 / 帮助对话框 |

键位：`<leader>`（ctrl+x）+ `q` 退出；`esc` 两段式中断；`ctrl+x m/l/s/n` 打开对应对话框。键位表以 `ai_coding/frontend/src/keymap.ts` 为准。

## slash 命令

手打 `/xxx`（`app/slash.py` 解析，`app/commands.py::handle_input` 分发；TUI 侧由
`ai_coding/frontend/src/prompt` 的自动补全 + `util/slash.ts` 过滤）；输入 `/` 会唤起命令面板（见 R12 节），
所以 `/` 本身不是普通文本。未知 `/xxx` 仍会**当作普通输入发给模型**，不是错误。

> **TUI 接线状态（A2）**：TUI 里 `/` 开头的行统一经 `session.command` 交给上面的
> `handle_input` 分发（GUI 复刻 §A1/A2），输出以 `console.line` 落进对话。目录（`util/slash.ts`）只收录**真能跑**的条目：对话框型（models/sessions/skills/status/help/new/quit）、
命令型（plan/review/permissions）、文本型（budget/trace/todo/memory/config/tasks/compact——
选择即提交 `/<name>`，语义全在后端）。带参命令（`/permissions auto`、`/tasks stop <id>`）
在命令后带空格即可（补全遇空格自动收起，Enter 走提交）。

| 命令 | 参数 | 功能 |
|---|---|---|
| `/help` `/h` | — | 列出全部直接命令 |
| `/status` | — | 会话 id、消息数、记忆条数 |
| `/model [name]` | 模型名 | 无参数列出配置里声明的模型（`*` 标记 `default_model`），带参数则切换 |
| `/resume [id]` | 会话 id | **无参数弹出会话选择屏**（`ui/resume_screen.py`）：按最后访问时间倒序、按天分组（Today / Yesterday / 绝对日期）；每行显示时间 + 首条用户消息预览（截断 80 字符）+ 消息数；搜索框按预览/标题/id 子串过滤；↑↓ 选择、Enter 恢复、Esc 取消；已打开的会话不再列出；一个会话都没存时直接提示 `no saved sessions`。恢复后 transcript 清空并按消息时间顺序重建全部历史（用户气泡、模型正文、已完成工具卡默认折叠；只显示最终结果，不回放流式过程）。    带参数则直接恢复并返回 `NEW_SESSION`，**支持前缀匹配**（`/resume 0113e0acac03`），歧义前缀会列出候选而非静默取第一个；恢复走完整 `load`（列表视图是剥离正文的摘要，直接用它会导致历史丢失）。TUI 里由 `dialog/sessions.tsx` 呈现（后端 `session.list` 供数） |
| `/permissions [auto\|confirm]` | 策略 | 无参数显示当前审批回调，带参数切换自动批准 / 逐次确认 |
| `/todo` | — | 渲染当前会话的 `TaskState`（目标、进度、逐项状态、下一步） |
| `/tasks` | `[stop <id>]` | 列出后台子智能体运行（id/名/状态/耗时/tokens）；`stop` 停掉运行中的任务 |
| `/skills` | — | 列出 `SkillRegistry` 目录（`- name: description`） |
| 待审批技能面板 | `a` 批准 / `d` 拒绝 | 审阅 `skill_review` 生成的、尚未进入 `skills/` 的 skill |
| `ask_user`（工具） | — | TUI 里弹 `component/ask.tsx` 的 `◆ Question` 面板（M6.5）：模型给了 `options` 时在问题下方列出，↑/↓ 或 1-9 选择、enter 回答；无 `options` 时面板内置一行输入框可直接打字；esc 回答空串（模型收到 `no_answer` 后自行决定）。IPC 走 `user_prompt.ask` 双向请求（与 `approval.request` 同一范式） |
| `/plan` | `<topic>` | TUI 接线（A1）：`/` 开头走 `session.command` → 后端 `handle_input` 分发，计划/步骤/报告全程经 `console.line` + `plan.updated` 事件渲染。**计划并执行**（M1）：裸 `/plan` 先弹 `What should I plan?` 收集主题；`/plan <topic>` 直接用话题、不再弹窗。两条路径都走 `plan_runner.plan_and_run`（M4-a：此前 TUI 与 REPL 各写一份，TUI 那份因为先被路由拦下而成为唯一可达实现，于是 REPL 那份漂移成「把 `the current task` 当话题、不重开计划」）；裸 `/plan` 在两个前端都会先问话题（TUI 弹窗、REPL `Prompt.ask`），prompt 只有 `PLAN_PROMPT` 一处定义。两条路径都重开一份计划（`SessionData.begin_plan(topic)`：新 goal + 空清单 + 清空 step 收据）。计划回合写 todo 清单后**立即逐步执行**——`/plan-run` 已删除（两命令接力会让清单过期：实测 0 回合跑在上一轮的清单上）；仍输入 `/plan-run` 会得到「did you mean '/plan'?」而不是被当作一句话发给模型（`ui/slash.py` 的 `_DEPRECATED` 墓碑表，近似命令规则看不到 4 个编辑距离的它）。每步一个**短对话**（全新 `ephemeral` session，seed = goal + 计划当前状态 + 本步验收，不继承父历史；步会话不落盘，不污染 `/resume`）——实测 4 回合 prompt 从 41k/82k/130k/254k（每回合重发全部历史）降为每步只带 seed（seed 本身随未完成项数增长）。有 `verify` 的项走 R0 验收门（失败修一次再败标 `blocked`）；无 `verify` 且模型未标完成的标 `stalled`。每步落收据，`/resume` 重放为步骤 header。整轮有顶（`ai.plan_max_step_turns`，默认 20，0=关闭；budget 停后剩余项保持 open）。**分步外完成有收据**（R1-5）：模型在计划回合顺手标 done 的步，runner 补 `attested=false` 收据、0 花费。**收据带证明分级**（R1-6）：`verified`/`on-claim`/`fiat`/`n/a`，report 点名无证明的 id（`verified` 要求每条验收判定都是 `passed is True`，读取端不信任 `ac` 非空）；**失败步同样留记录**（`✗ 受阻` 收据 + 气泡），`/resume` 能看到；回合内换清单（模型 `todo_write(clear)` 后重写）会清掉不属于新清单的旧收据 |
| `/review` | — | 向模型发一条「审查当前 git diff」的快捷 prompt（TUI：命令入口见 `/plan` 行） |
| `/config` | — | 打印生效的 model / 上下文预算 / 迭代上限 / 停止策略 / 回合超时 |
| `/compact` | — | 主动压缩历史并落盘 |
| `/memory` | — | 记忆面板（M7）：按 kind 分组的每一条记忆，可搜索、enter 看全文、`ctrl+t` 钉住/取消、`ctrl+d` 二次确认删除。非 TUI 入口仍打印统计行 |
| `/trace` | — | 上一回合的轨迹摘要（token、耗时、降级、门卫拒绝） |
| `/budget` | — | 本会话累计 token 账本。区分 4 种状态：tracing 关闭 / 本会话还没跑过回合 / 跑了但零 token（模型未上报 usage）/ 正常用量 |
| `/clear` | — | 开新会话（旧的仍可 `/resume`） |
| `/quit` `/q` `/exit` | — | 退出（`Ctrl-Q`） |

**未知命令**：`/xxx` 不在别名表时，若与某已知命令编辑距离 ≤ 2（如 `/resum` → `/resume`），提示 `did you mean` 且**不发给模型**；否则（如 `/path/to/file`）照旧发给模型。

### 非命令键位

| 键 | 行为 |
|---|---|
| `Ctrl-Q` | 退出 |
| `Ctrl-C` | 有选区时复制选中文本；无选区时退出 |
| `Ctrl-Y` | 把上一条已完成工具调用的全文（参数 + 输出）复制进系统剪贴板；无可复制内容时在 transcript 里明示 |
| `Esc` | 中断整个回合：模型停、工具停，已产出保留并落盘，transcript 留红色 `[turn] 已中断` 标记，输入恢复可用。弹窗开着时 Esc 归弹窗（拒绝/关闭），到不了这里。后台子任务不受影响（另议） |
| `Shift`+拖选 | 终端原生选择（在 app 捕获鼠标时仍可用） |


## 内置工具

主工具栈由 `tools/builder.py` 装配。默认 7 个工具随配置注册（bash/read/write/edit/glob 受 `tools.*.enabled` 控制），`todo_write`、`subAgent` 始终注册；只要工作区存在 `skills/` 就追加 `skill` 工具，共 8 个。
记忆开启时组合根再追加 `memory_search`（读）与 **`memory_write`（写，R7e-1）**——后者是模型自己写一条耐久记忆的通道；
**子智能体的工具栈里没有任何记忆工具**（它连写入口都不该有）。每个工具实现 `BaseTool` 契约（`name`/`description`/`params_json_schema`/`execute`），经 `sdk_adapter` 包装成 SDK FunctionTool。

| 工具 | 权限级别 | 行为要览 |
|---|---|---|
| `read` | ALLOW | 行号读取，支持 `offset`/`limit` 分页，上限 10 MB；读取后记录快照供过期读保护 |
| `glob` | ALLOW | 通配符匹配文件，过滤符号链接逃逸项 |
| `todo_write` | ALLOW | 计划模式待办维护（始终注册） |
| `subAgent` | ALLOW | 委派子任务；工作区为 git 仓库且开启隔离时在临时 worktree 内运行 |
| `skill` | ALLOW | 读取技能目录中某技能的正文（`name=`；默认工作区 `skills/` 始终可用，`--skills` 只用于改位置） |
| `write` | WARN | 写入文件；未先读覆盖目标会触发过期读保护 |
| `edit` | WARN | 精确字符串替换；`old_string` 须唯一或显式 `replace_all`；同样受过期读保护 |
| `bash` | WARN（危险命令 DENY） | 执行 shell 命令，Linux 专用，`timeout_seconds` 超时、输出截断 50 000 字符。超时默认 60s，单次调用可用 `timeout_seconds` 参数加宽到 600s（上限对齐回合墙钟）；超时结果会写明上限，模型自行加宽重试，不再需要 shell 的 `timeout(1)` 自残或 nohup+轮询 |

`subAgent` 默认加入主栈；子代理内部栈不嵌套 `subAgent`，避免无限递归。

## 安全与权限

- **权限门**：`PermissionGate` 对每次工具调用判定 ALLOW / WARN / DENY。`read`/`glob`/`todo_write`/`skill`/`subAgent`/`memory_search`/`memory_write` ALLOW；`write`/`edit`/`bash` WARN；未知工具保守归为 WARN；bash 命中硬拒绝模式（`rm -rf /`、`sudo `、`shutdown`、`reboot`、`mkfs`、`dd if=`、`> /dev/sd`、强制 git push、`chmod 777` 等）直接 DENY。
- **审批接线**：WARN 层由 `approval` 回调解析。无人可问的入口（子智能体）用 `AutoApproveCallback`（WARN 放行，DENY 仍拦截）；交互 TUI 用 `CLIApprovalCallback`，对 write/edit/bash 经模态弹 y/N/s 询问用户。既满足了「AI 助手能写文件」，又保留了危险操作的确认与硬拒绝兜底。
- **审批面板显示参数**：WARN 层的确认面板会打印这次调用**将要做什么**，而不只是工具名。本地工具本来就有一行摘要（`read` 的路径、`bash` 的命令）；MCP 调用过去只有 `fs__write_file` 这个名字，参数到了前端却被丢在渲染层——也就是在批准一个你看不见副作用的远程调用。现在 `permissionPayload()` 把 `input` 的每个参数逐行打印，单值截断 160 字符、整块上限 12 行（超了明说还剩几个），长值不换行以免把选项挤出屏幕。
- **沙箱**：`Sandbox` 将文件工具的所有路径解析限制在工作区内，`..`、绝对路径、符号链接逃逸一律抛 `SandboxViolation`。
- **过期读保护**：`FileStateTracker` 记录最近一次读取的快照，写入/编辑前比对 `mtime`+`size`；未被读过（`NEVER_READ`）或内容被外部改动（`STALE`）时阻止写入，防止盲写与覆盖外部修改。
- **项目配置不被信任授予执行（N-4）**：配置分两层 —— `~/.config/ai-coding/config.yaml`（用户层，可信）与仓库里 `discover()` 找到的 `config.local.yaml` / `config.yaml` / `config.example.yaml`（项目层）。项目层**逐键覆盖**用户层，这对"怎么干"是对的（`default_model`、`max_tool_iterations` 等项目层照常生效），但**执行类字段只认用户层**：
  - `hooks`（`command` 执行任意程序、`kind: python` 的 `module` 走 `exec_module` 执行任意 Python）
  - `mcp`（`servers[].command` spawn 任意子进程）
  - `mcp.local.yaml` sidecar（`_mcp_doc` 以"哪个文件回答了配置"为锚点解析它，所以锚点也不能推进到项目层）

  项目层设置了这些键时**丢弃并发一条 warning**（不静默）。要信任某个仓库，`export AI_CODING_TRUST_PROJECT=1` —— 这是环境变量而不是配置键，因为**仓库不能替 agent 进程设置它**，这正是"许可"与"配置"的区别。过滤在 `_normalize` **之后**做，所以 `Hooks:` / `MCP:` / `Mcp:` 这些大小写变体同样被挡住。

  `models` 的**条目**也逐条过滤：项目层不能新增模型条目，已有条目只保留行为键，`base_url` /
  `api_key` / `provider` 一律丢掉 —— 后者是 M-H2 加的，因为它同时决定端点**和**读哪个环境变量。

  仍未覆盖的同类项（另立项）：`tools.extra_workspace_roots`（越权读写）、`ai.websearch_api_key`
  （外发 key，已在 `PROJECT_FORBIDDEN_MEMBERS` 里但不在本节的清单中）、`tracing.dir`（越界写）、
  `hooks.plugin_dir`（python hook 的模块来自不可信仓库树）。

### 密钥从哪里来（M-H2）

**`api_key` 的解析顺序**：`${VAR}` 引用 → 明写的字面值 → provider 的约定变量
（`DEEPSEEK_API_KEY` / `OPENAI_API_KEY` / `DASHSCOPE_API_KEY`）。环境变量**只填空位，不覆盖
字面值** —— 反过来会让一台导出了 `DEEPSEEK_API_KEY` 的 CI 机器静默用错账号，而且让测试结果
取决于跑在哪台机器上。`${VAR}` 缺失**或为空**是硬错，消息给出变量名（以前会静默变成 `""`，
一路走到 `AsyncOpenAI(api_key="")` 才报 `The api_key client option must be set`）。

**provider 是一张 5 行的表**（`ai_coding/providers.py`）：openai / deepseek / qwen / ollama /
generic。三个字段各有消费者 —— 默认 `base_url`（`provider: deepseek` 就不必再写端点）、约定
环境变量名、以及表键本身。**不做的**：不做能力矩阵、不做第二套协议、不做价目表。原因写在
`domain/usage.py` 与 `openai_engine` 里：usage 形状、reasoning 字段、context overflow 都是
"挨个试"，本来就不需要知道 provider。

**推断用 host+port**，不是 host：只按 host 会让 `http://localhost:8000/v1` 命中 `ollama`
（默认 `localhost:11434`）。TUI 的 provider 标签仍走原来的 `provider_from_base_url`
（从 host 猜），M-H2 没有动它 —— 它已经猜得够准。

**子进程看不到密钥**：被消费的变量名由 `secret_env_names()` 从原始 models 文档算出（必须早于
`app_from_doc`，否则 `${VAR}` 的名字就丢了），经 `LayeredConfig` 交给组合根，最后复用
`process_sandbox` 里 `--unsetenv` 那个既有循环。边界要说全：`mode=off`、bwrap 不可用、或没有
policy 的工具栈（脚本/测试）仍继承环境；**stdio MCP server 以 `env=None` 启动、继承完整环境**，
不受这条保护；`static_check` 也不在沙箱内。沙箱生效时 `/proc/<pid>/environ` 那条路被
`--unshare-pid` / `--tmpfs /proc` 关着 —— 那才是"子进程看不到密钥"的完整前提，不只是
`--unsetenv`。

**`config init` / `config path`**：前者写用户层（目录 0700、文件 0600、`O_EXCL`），后者打印
位置。这是 onboarding 断头的根因修复 —— 以前文档教 `cp config.example.yaml config.local.yaml`，
而那正是会被信任过滤丢掉的那一层。

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

### 验收契约与不确定性台账（R0）

| 机制 | 行为 |
|---|---|
| 验收项（`session.acceptance`） | 一条可机检的陈述 + 一条 shell 证明命令；落盘仅当非空（Java 兼容）。暂由调用方/计划 runner 填充，不做模型自由文本解析 |
| 验收门 | 模型停下先跑未过项的证明命令（单项 120s/总额 300s，只验未过项）；全过 → `COMPLETED（acceptance verified）`；失败 → `[acceptance-gate]` nudge（自有预算 2 次）把失败输出拍进历史；预算型 FINISH 时若有时钟余量则补验一次，全过改判 COMPLETED（预算用尽但活干完不再撒谎） |
| 不确定性台账 | 纯派生（验失败/工具错误聚合/失败连胜），H 只降在记录证据上；open 集签名变化才发 `LedgerUpdated` 事件，transcript 显示 dim 行；ledger 仅作 trace 观测，不再注入 nudge |
| 子跑截断信号 | `SubagentResult` 带 `truncated/exit_reason/usage`；`ToolResult.error` 为机器码（`child_timeout`/`child_max_iterations`/`child_error`，max_iterations 仍算成功）；子事件经 observer 进父 trace；ledger 文件保留新字段 |
| 步骤时间会计 | books 记每步 latency 与被放弃步数；被墙钟掐断的模型步发 `Degraded(model_step)`（verdict 不变）；transcript 已有的流式 partial 即保留 |

### 后台子任务（R1-2）

| 机制 | 行为 |
|---|---|
| `subAgent(background=true)` | 调用即返 run id（毫秒级），父回合零等待；孩子照旧走信号量、超时、ledger 纪律；完成以事件 + 表行落地，**永不写第二条 tool 消息**（严格配对只允许一 call 一消息） |
| 完成通知 | `BackgroundTaskDone` 事件顺捕获的 emit 投递（TUI 落 transcript dim 行，跨会话标注来源会话）；全文包仍在 `transcripts/subagents/` ledger |
| `/tasks` | 列 run id/名/状态/耗时/tokens；`/tasks stop <id>` 停（→ cancelled 包 + 完成事件）；无 runner 时降级提示 |
| 容量与重启 | 信号量满时 spawn 直接报错（不静默排队）；表 200 行封顶（只剪终态行）；重启丢表（运行中的孩子无法证副作用，不恢复） |

### 结构化 handoff（R1-3）

| 机制 | 行为 |
|---|---|
| `subAgent(output_schema={...})` | 子结论的 JSON 形状合同：孩子被要求把 verdict 以 JSON 对象返回，包带解析出的 `facts` + `schema_valid` 三态 verdict（None=没要合同）；父可编程取数，不再读散文摘数字 |
| 宽容解析，内容严格 | 去围栏→全文解析→散文内首个平衡 `{…}` 块；只认 `required` 键表（其余 JSON Schema 关键字忽略，文档写明是子集）；多余键容忍；值永不强制转换 |
| 坏形状不杀工作 | `schema_valid=false` 只标形态失败：包状态照旧（success 按 status 定）、原文照留、render 警告行点名缺的键；ledger 文件同样落 `facts/schema_valid/schema_errors` |
| 合同大小卡 | schema 序列化超 4000 字符直接拒（`schema_too_large`，runner 不碰）——schema 要进孩子 prompt，无界即 token 炸弹；非对象拒（`schema_invalid`） |
| 后台同样可用 | `background=true + output_schema` 照走：facts 进包与 ledger；完成事件带 `ledger_path` 指针（块尾 `· 完整回包: 路径`），无 ledger 时不显示 |

### 任务清单与停止判定（R2 拆分为纯单元）

「要不要再来一轮」「模型停下来了但 todo 没做完怎么办」由 `core/turn_policy.py` 决定
（纯函数、无 IO），`AgentLoop` 只负责编排。决策表见 [R2 分析](r2-analysis.md) §6.7。

### 预算耗尽自动续跑（C3）

正常回合迭代预算耗尽但 todo 仍有 open 项时，循环注入 `[continue]` 提示（点名剩余项）
并**重新武装迭代预算**（新一轮 `max_tool_iterations`），最多 `ai.max_auto_continuations`
（默认 3，`0` 关闭）次；todo 清空则正常结束。触发**不受 `stop_policy` 影响**——活没干完
不是停止策略问题；续跑额度按**用户回合**重置。token 预算耗尽（`ai.turn_token_budget>0`）
是终态，优先于续跑——不会注入注定无效的提示白烧一次额度。handoff/`TurnOutcome.iterations`
报**累计模型步数**（跨续跑轮），不是最后一轮的序号。实测（qwen，`max_tool_iterations=2`）：
一个三步任务连续 3 次，续跑提示里剩余项随实际进展收缩（3→2→1），最后模型自己收尾"全部完成 3/3"。
plan-runner 的步内强制关闭（`max_auto_continuations=0`）：步是共享计划 task 的一个有界回合，
开着会让单步把整个计划的剩余项当成"未完成工作"。

**四层上下文压缩**（`ContextManager`，调用顺序为硬约束）：

| 层 | 动作 |
|---|---|
| L3 | 超阈值的大段尾部工具输出持久化到 `transcripts/persisted/`，历史内替换为 `<persisted-output>` 占位 |
| L1 | 超 `max_messages` 时裁剪中间段，留 `snip_keep_head`/`snip_keep_tail` 条并插入 `[snipped n messages]` |
| L2 | 早于 `keep_recent_tool_results` 的旧工具结果换成**可回收的信封**：正文先落盘到 `transcripts/elided/`（内容寻址，同一正文不重复写），历史里留「清了多少字符 + 原文路径 + 头 `l2_window_chars` 字符 + 尾 `l2_window_chars` 字符」；窗口总量受 `l2_window_budget` 约束、**从新到旧**分配，预算用完之后只剩信封（路径仍在，所以信息永远取回得到）。比占位还短的正文原样保留（R7b） |
| L4 | 超上下文预算时用真实 LLM 摘要压缩历史（带 3 次失败熔断），降级为硬裁剪 |
| 兜底 | 硬裁剪 + `sanitize_pairs` 保证严格配对 |

所有阶段都在消息列表的浅拷贝上进行，绝不修改调用方历史。

### 压缩层的语义、判据与可观测（M-E1 ~ M-E4）

> 这四条来自 `docs/plan-m-e.md`：把"隐式的语义/缺失的不变量/缺失的观测"显式化，
> **不改压缩行为**（除新增每层统计外）。

**① L3 是回合中段护栏，不是压缩层**（M-E1）。`persist_large_outputs` 只取**末尾连续
`role=="tool"` 段**，且要求该段总字节 > 窗口一半、**单条** > `per_result_persist_bytes`（30000）。
所以：回合结束时末条是 assistant → batch 为空 → 直接返回（`eval-04`：10 轮真实对话后
`persisted=0`）；**非尾部**的大输出即使超大也**不**持久化（它保护的瞬间是"工具刚返回、
模型还没答"）。语义由 `test_l3_semantics_are_locked` 锁住，并通过 `CompactionEnded.layers`
的 `L3.skipped`（检查了但没动手的条数）可观测。

**② 计数稳定性：触发按 token、成功按内容真值，不按条数**（M-E2）。
* 触发：`should_compact` 取 `max(provider observed, _history_tokens) >= ceiling`（token 判据，
  `compaction_ceiling` 对齐 OpenCode）。
* 成功：`Compactor._compress_once` 用**内容真值** `result != history` 判定，**不是**条数比较。
  按条数判会把"省了 36% token 但条数没变"误判为失败 → 假性耗尽 → 会话重置（Hermes 的 bug，
  本仓已由 `test_a_compaction_that_saves_tokens_without_changing_the_count_is_a_success` 锁住）。
* **管线顺序是硬不变量** `L3 → L1 → L2 → L4`，由 `test_the_pipeline_order_is_l3_l1_l2_l4`
  锁住（`force=True`，否则 vacuous）。L4 摘要会吃掉 L2 刚生成的信封 → **不以压缩后条数/
  信封数为目标**，现存断言都是 token 判据。
* tool_call/result **原子淘汰**：`sanitize_pairs` 结构性保证"无孤儿 result、无无 result 的 call"，
  且所有管线出口都过它；管线级回归锁见 `test_no_orphan_tool_result_survives_a_full_pipeline_compress`。

**③ 压缩预算可观测**（M-E3）。`CompactionEnded` 新增 `layers: tuple[LayerStat, ...]`、
`estimated_before`/`estimated_after`（**层序边界**，不是 Compactor 边界——后者含分页循环）。
`LayerStat` 字段：`name`（L1/L2/L3/L4）、`removed`（**净减条数**，比"真的减少的条数"少一个占位符
——L1 装 `[snipped N messages]`、L4 装摘要条，只 L1/L4）、`rewritten`（保条数改内容，L2/L3）、
`skipped`（检查了但没动手，**仅 L2/L3**：L1/L4 改条数而非扫描候选，恒 0）、`before_tokens`/
`after_tokens`（用 `_history_tokens`，含每张图固定 1600）。顺序写死 = 管线顺序；L4 未跑时 `layers`
**不含** L4 项，`/trace` 该行输出「L4 未触发」。
`/trace` 的 `render_turn` 按 **token 差**渲染压缩行（条数对 L2/L3 无意义）；前端压缩卡片显示每层
统计。`Compactor._compress_once` 从 `context.last_layer_stats` 读取后填入（这是 plumbing 的关键
一行，缺它 `layers` 永远为空）。

**④ 前缀缓存友好度**（M-E4）。注入全部落在**可变区**（尾部），稳定前缀不被改写。
守护用**布尔不变量** `stable_b.startswith(stable_a)`，其中
`stable = canonical_request(instructions, Assembled.history, tools_text)`——**必须传 `Assembled.history`
（pre-tail）**，传 `request` 必然断裂（`canonical_request` 不知道 tail 边界）。**不用百分比阈值**
（M-A6 已弃用：整请求比率随工作区路径漂移）。跨回合前缀稳定的五类合法断裂作为 skip：
① `task_injection="system"` ② 回合内压缩 ③ instructions 变化 ④ `tools_text` 变化 ⑤ plan reminder
（追加到 history 末尾、每次重加）。系统提示与工具定义**无秒级时间戳**（时间戳放尾部块是正确设计）。

## 记忆

R3 重写（✅ **P0 完成**）。记忆层现在回答三个问题：**写什么、什么时候写、以什么位置/形式交给模型**。

| 动作 | 行为 |
|---|---|
| 收割 `harvest` | 回合结束后，只把**本回合增量**（游标存在会话里）交给提取器；提取器输出结构化候选（`text/kind/importance/action/target_id`/`scene`），服务逐条对账 **ADD / UPDATE / SUPERSEDE / NOOP**；同义（导语剥离后相似度 ≥0.80）合并；空结果不写；无提取器时**不写**（不再把闲聊原样入库） |
| 召回 `recall_notes` | 排序用 **Okapi BM25**；两道门禁分别管两件事（见下）；中文走字符 bigram + 虚词过滤；返回 id/kind/score/relevance/`matched_terms`/`source_refs` 以便追溯 |
| 整理 `consolidate` | 合并近似重复（**同 kind 且同 scene** 才合并）、按命中提升 importance、淘汰过期 open_thread、按价值淘汰（`pinned` 与决策受保护）；`dream` 仅作为弃用别名保留 |

**P0 完成项**：
- ✅ 形状门：`is_plausible_note_text` 拒绝 JSON 碎片（首字符 `{ [ ] } "` 直接拒），`MemoryNote.__post_init__` 抛异常，加载时 `_load` 自动跳过畸形行，`.memory` 46→29，0 碎片残留。
- ✅ 加载自愈：`_load` 逐行捕获 `ValueError`，畸形条目静默跳过。
- ✅ 存量清洗：`.memory` 46→29，12 条 JSON 碎片 + 11 条 hotel 进度态笔记已清洗，剩余 29 条全为干净知识。
- ✅ 易变状态过滤：收割/写入双重过滤 `_is_volatile_note`，拒绝"下一步/已完成/当前进度/临时路径"等瞬时任务态。

| 动作 | 行为 |
|---|---|
| 收割 `harvest` | 回合结束后，只把**本回合增量**（游标存在会话里）交给提取器；提取器输出结构化候选（`text/kind/importance/action/target_id`/`scene`），服务逐条对账 **ADD / UPDATE / SUPERSEDE / NOOP**；同义（导语剥离后相似度 ≥0.80）合并；空结果不写；无提取器时**不写**（不再把闲聊原样入库） |
| 召回 `recall_notes` | 排序用 **Okapi BM25**；两道门禁分别管两件事（见下）；中文走字符 bigram + 虚词过滤；返回 id/kind/score/relevance/`matched_terms`/`source_refs` 以便追溯 |
| 整理 `consolidate` | 合并近似重复（**同 kind 且同 scene** 才合并）、按命中提升 importance、淘汰过期 open_thread、按价值淘汰（`pinned` 与决策受保护）；`dream` 仅作为弃用别名保留 |

### 写侧总开关（R11a）

两个正交开关，判定谓词只有 `service.is_extraction_allowed` 一处：总闸 `extraction_enabled`
（`false` = 收割与 `memory_write` 全停，召回/注入照常，即「只读记忆」模式），分流
`extraction_extractors`（`["harvest"]` / `["memory_write"]` / 两者；`null`、缺段或非 list = 全放行）。
**缺段或写错一律放行**，只有显式关掉才停——否则一份残缺的 yaml 会静默停掉写入。
门被关上时**连 `evict` 也不做**：门的意思是「手别碰索引」，裁剪也是写。

### 查询清洗（R11a）

召回的 query 在分词**之前**先过 `clean_query_for_recall`：整块剥掉 `<tool_output>` 与
`<memory-constraints>` / `<working-memory>` / `<task-state>` / `<hook-context>` 等易失尾块，
再剥残留裸标签（首字符必须是字母或 `/`，所以 `a < b` 不受影响），最后截断到 2048 字符。
动机是实测的：粘贴的工具结果会把 `tool`/`output`/`read` 变成召回词，上一轮注入的记忆块会把
旧决策词变成这一轮的查询词。对标 TencentDB Agent Memory 的 injector 在检索前剥
`<user_info>`/`<additional_data>`——同一类病，同一处治。
**只影响召回，不影响收割**（喂给提取器的原文不能洗，那是另一份契约）。

### 召回预算与超时可见（R11b）

`memory.recall_timeout_seconds`（默认 2.0，0/负关闭）是一次召回的墙钟预算，计时窗口覆盖
过滤 → 语义层 → 排名 → 未命中探针。超预算时**命中照返**（绝不饿死模型），只跳过那次可选的
`touch_notes` 整文件写，并经 `ctx.emit` 发一条 `Degraded(component="memory_recall")`，
回包带 `data.timed_out`。它是**事后计量**而不是 `run_with_deadline` 式抢占——同步 BM25 循环
没有挂起点可放弃，硬套只会把「慢」伪装成「可取消」。出厂 2.0s 对实测 ~5ms 是虚位以待，
今天不会触发，等语义层真的变慢才开始起作用。

### 场景分组与来源位置引用（R11c）

| 契约 | 行为 |
|---|---|
| `MemoryNote.scene` | **可选**的自由文本话题标签（strip + 80 字截断）。只做分组与过滤：**不进 `dedupe_key`**（完全同文即使 scene 不同仍折叠成一条）、不进打分。两个生产者：提取器 prompt 里作为**可选**字段索要、`memory_write(scene=…)` |
| 合并的第二维 | `find_similar` 同 kind **且同 scene**；`consolidate` 不跨 scene 合并。方向是收紧：合并会把「构建用 uv」和「发布用 uv」并成一条，而那正是限定词在起作用的地方 |
| 去重命中回填 | 收割先写、`memory_write` 后补 scene 时把 scene 补到既有条目上，否则「先收割后标注」这个最常见顺序会静默丢标签 |
| `memory_search(scene=)` | 与 `scope` 同构的**精确匹配**，过滤先于排序。标签是自由文本，模型猜错就匹配不到——`filtered_out` 会点名「是 scene 过滤把它们排除了」 |
| 未命中导航 | `data.index.by_scene` + `top_scenes`（`(scene, count, heat)`，heat = 该场景 `hits` 之和，倒序取 5、名字 tie-break），文案追加一行列出**库自己的词**让模型照抄。**无 scene 的库不输出新键、不加新句** |
| `/memory` | 非空时多一段 `scenes: 构建=1, 发布=1` |
| `MemoryNote.source_message_ids` | `m<下标>` 位置引用，指向 `sessions/<session>.json` 的 `messages`，**只覆盖真正喂给提取器的那段窗口**（实测：20 条 delta + 窗口 12 + `source_offset=5` → `m13..m24`，不是 `m5..m24`）。`memory_search` 的 `notes[].source_refs` 恒带（空 list） |

**位置引用的前提**：消息只 append、压缩只改副本、`ensure_tool_pairing` 在落盘前清洗，所以下标
长期稳定。**若将来支持编辑或删除历史消息，这个字段必须重审。**

**同义折叠（M-F2）**：`MemoryNote.scene` 是自由文本，提取器给同一个话题起了不同拼写
（「记忆模块」/「记忆系统」/「记忆配置」），于是它们各自都是单例、永远达不到
`SCENE_SUPPORT=3`，`scene=` 过滤被静默丢弃。折叠表只有一份 ——
`ai_coding/memory/scenes.py::SCENE_SYNONYMS`（`scripts/migrate_scenes.py` 导入同一份，
不再有第二份词表），`fold_scene()` 先 `normalize_scene` 再查表。`normalize_scene` 本身
**不变**（`docs/data-contracts.md:868-869` 的契约：词表若落在归一化层，所有既有归一化
测试都会被打碎）。表是**幂等**的：值不得再是键，否则二次折叠会过折
（`tests/test_memory_scenes.py::test_fold_scene_is_idempotent` 钉死）。

折叠有**四处**应用点，缺一处就静默失效：

| 路径 | 位置 | 为什么必须 |
|---|---|---|
| **闸门之前** | `service.py` 的 `governed_scenes()` 闸门 | 闸门用原始 `scene` 比较，折叠放晚了，「记忆模块」会被当"不是组"丢弃 |
| **过滤时** | `service.py::_candidates` | 对入参 `scene` 与 `note.scene` **都**套 `fold_scene` |
| **读时** | `store.py::_load` | 磁盘上的旧拼写 |
| **写时** | `service.py::_reconcile`（唯一写路径） | 只折叠读路径会让同进程内新写的旧拼写与旧笔记并存，`governed_scenes()` 计成两个标签。**只有这一处**：`write_note` 与 harvest 都汇流到 `_reconcile`，在 `write_note` 里再折一次就是冗余，且会让写路径的折叠不被任何测试钉住 |

磁盘上的旧拼写由 `scripts/migrate_scenes.py --apply` 一次性归并（默认 dry-run，写前把旧
标签落到 `<store>.scenes.bak`）；本轮**不自动迁移**。

**为什么不给 `scene` 设全局上限**：消费者里 `governed_scenes()`（≥3）与
`index_summary().top_scenes`（取前 5）都有界，但 `index_summary().by_scene` 与
`status()["by_scene"]` **无界**，且未命中时 `by_scene` 原样发给模型。真正的上界是
`max_index_entries=200`（标签数 ≤ 笔记数 ≤ 200），所以默认配置下不设全局上限站得住 ——
但**调大 `max_index_entries` 时 `by_scene` 会线性膨胀并直接进入模型上下文**，必须一起评估
（M-F3 的 1_000/10_000 压力档正是这样）。

#### 真机实测（`scripts/r11c_cases.py --real`，2026-10-03）

单测钉住形状，只有真模型能回答「生产者到底产不产」。三次运行（每轮 3 会话 × 3 回合、
dashscope `models.qwen`、临时工作区）：

| 主张 | 判据 | 实测 |
|---|---|---|
| C1 采纳率 | ≥50% 候选带 scene | **13/14 = 93%**（分三次：5/6、5/5、3/3） |
| C2 标签粒度 | ≤5 个/会话、≤12 字 | 每会话 ≤2 个，最长 5 字（`依赖管理`/`构建流程`/`CI/CD`/`发布流程`） |
| C3 去重 | 同文仍折叠 | 三轮均无重复正文 |
| C4 refs | 不越界、不超窗口 | 多回合下窗口推进正确：`m0,m1` → `m2,m3` → `m4,m5` |
| C5 导航 | 文案列场景 + `scene=` 收窄且不跨场景泄漏 | 重载 store 后每会话通过 |

**实测推翻了我自己的两个说法，都写在这里：**

1. **场景标签不是查询词。** 有一轮 top 场景是 `CI/CD`，而它的笔记正文是「CI 使用 GitHub
   Actions」——把标签原样当查询去搜，什么都搜不到，而地图其实点名正确了那条笔记。因此
   `scene` 是**过滤器**不是召回器：未命中文案原来那句「可用 scene=<场景> 精确再搜」会诱导模型
   做出必然失败的动作，已改成「收窄候选（查询词仍要自己写）」。C5 的断言也随之从「拿标签当查询
   搜」改成「文案列标签 + 按标签能搜到本场景的笔记 + 换个标签搜不到它」。
2. **JSON 片段会被当成记忆存进去**（**已修；非 R11c 引入**）。有一轮落了一条正文为
   `[{"text": "CI 跑在` 的笔记——模型输出被 `max_tokens=400` 截断导致 `json.loads` 失败，
   于是走 `ai/llm_helpers.py` 里旧格式兜底，而**那条路径不跳过 JSON 行**。`[{"text": "CI 跑在`
   因此变成一条 fact。它同时把 C1 的分母算成「没带 scene」，所以那轮是 5/6。

   修的时候发现**两条**路径都会漏，不止记录里的这一条：
   - `memory/schema.py::_from_lines` 的守卫只拦 `{`/`[`，所以**多行**截断数组里每个字段行
     （`"kind": "decision",`）和结束行（`},`）照样变成记忆。R11c 那轮只抓到单行样本，没看到这条。
   - 旧格式兜底与 `_from_lines` 是同一逻辑的第二份实现，且**完全没有守卫**；`parse_candidates`
     返回 `[]` 时它会把同一段坏文本再切一遍。

   修法是补全 `_from_lines` 已有的守卫（加上 `"`/`}`/`]`）并**删掉**第二份实现，
   让 `parse_candidates` 成为唯一解析入口。畸形回答现在一律降级为「这次 harvest 什么都没抽到」，
   而不是变成记忆。`max_tokens=400` 未动：抽取器那次调用没有 trace，触发源无证据，
   而守卫修好后截断本身无害。

判据与门槛在跑之前就定死了（见脚本头部常量 `ADOPTION_BAR` / `MAX_DISTINCT_SCENES` /
`MAX_SCENE_CHARS`）：**C1 红了不会去改 prompt 再测**。样本不足 5 个候选时脚本只打印提示、不判
通过——采纳率在 1/1 上等于在读模型的脾气。

复现：

```bash
.venv/bin/python scripts/r11c_cases.py                      # 离线，无模型无网络
AI_CODING_R11C_IT=1 .venv/bin/python scripts/r11c_cases.py --real --config config.local.yaml
```

### 两道门禁（R10b/R10c，各有各的职责，不能互相替代）

| 门禁 | 回答的问题 | 默认值 | 为什么是它 |
|---|---|---|---|
| `min_relevance`（idf 加权覆盖度） | 这条笔记**是不是在讲这个查询** | `0.25`（R3 时代是 0.05） | 0.05 实测等于没有门禁：新近性 0.20 + 重要性 0.15 = 0.35 已经越过 0.25 的分数阈值，于是一条完全无关但新鲜且重要的笔记照样被注入 |
| `relative_band`（占本次最高分的份额） | 这条笔记**是不是还在候选里** | `0.55`（R10c 新增） | 绝对阈值跨查询不可移植。在自建语料上，**是这一道门禁把 FP@5 从 0.52 压到 0.35**；单独去掉它会回到 0.40 |

`relative_band` 卡的是**混合分**（BM25 + 新近性 + 重要性 + 类型加权），不是相关度——所以
`min_relevance = 0.25` **不等于**「相关就留」，相关是必要条件不是充分条件。另外 BM25 只负责排序、
**从不单独当门禁**：它的长度归一化会把短低信息笔记的相关度顶到 1.0（实测案例 9），所以门禁仍然是
覆盖度。

### 召回失败会说清楚为什么（R10c）

`memory_search` 未命中时不再是死胡同，`data` 带 `no_match`、`terms_known` / `terms_unknown`
（索引里有 / 从没见过哪些查询词）、`near_misses`（最接近但没过门禁的候选）、有界的 `index`
概览（按 kind 计数 + 3 条预览，500 条记忆的 JSON 实测 315 字符），以及固定为 `false` 的
`semantic_unavailable`（为尚未落地的可选语义层预留，payload 形状不必再变）。
`matched_terms` 告诉你每条命中**是凭哪个词**回来的。文本里每一句都是实测出来的：不带
`index` 概览时不编造条数；「查询用词索引都认识」只在**真的量过词、且每个词都认识**时才出现
（`terms_known` 非空才算量过）；索引实测为空时明说「记忆里一条笔记都没有」而不是劝模型换个说法
——没有东西可搜时改措辞不可能有结果；过滤自己造成的误命中会指名道姓说是哪个参数排掉的
（`有 N 条通过了相关度门槛，是 kind 过滤把它们排除了`）。

**「哪些词没见过」说的是词，不是内容**（这条曾经写反了）。`no_vocabulary_match` 只表示
**没有任何一个查询词在候选集里出现过（`df == 0`）**，它对「记忆里有没有相关内容」不构成任何
证据——实测：`memory` 里有「打包命令是 npm run build 产出 dist」，查询「怎么构建」，
`tokenize` 得到 `['么构','构建']`，其中 `么构` 是跨词 bigram，任何相关笔记里都不会出现它，
所以它的缺席对内容一字不值。旧文案据此断言「不是措辞问题，是这批内容不在记忆里」，
既超出了测量范围，又和下一行「换一个说法再搜」直接矛盾。按 spec 5.2，见过哪些词的缺席
**就是**模型该换说法的信号，所以文案现在只陈述测量到的事实，并保留换说法这条建议。

**过滤器先缩小候选集、再排序（R10c 的行为变更）**：`kind` / `evidence` / `scope` / `since_days`
在打分**之前**决定候选集，所以 `k` 不会浪费在模型没要的那批笔记上。此前是排序后再过滤：
`kind=decision`、`k=3` 而四条 fact 排在唯一那条决策前面时，返回**空**，而模型要的那条就在索引里。

新增参数：`scope`（精确匹配；本仓库写入的全是 `workspace`，只有导入或外部构造的笔记才可能是别的值）、
`since_days`（按 `updated_time or created_time` 相对调用方的钟，0 和负数表示「不设窗口」）、
`include_near_misses`（默认只在未命中时返回候选，置 `true` 则命中时也返回）。

**一处已知不精确**：`data.terms_known` 在未命中时覆盖整个索引，命中时只覆盖过滤后的候选集
（工具 `description` 里写明了这一点）。

### 命中即使用（R10b）

召回**现在会写**。每条命中经 `MemoryStore.touch_notes` 记一次 `hits` + `last_used`，
因此 `consolidate` 里 `hits >= 3 → importance + 1` 这条规则第一次真正可达——R3 写下它的时候，
`hits` 永远是 0。未命中不写任何东西；被过滤掉的候选**不算使用**（模型没看见它）。

代价（明说）：命中路径每轮多 `1 + len(hits)` 次**同步整文件写**（每条命中一次
`update_stake`，加一次 `touch_notes`）。M3 那句「记忆永不阻塞回合」因此不再成立，
`service.py` 的模块 docstring 已改成现在的事实；「永不抛错」那条不变。

**`recall_timeout_seconds=2.0` 的实测依据（M-F3）**：它比较的是 `RecallDiagnostics.elapsed_seconds`，
也就是**只含排序**的时间 —— `elapsed` 在写盘与 `index_summary` **之前**算完，所以 `1 + len(hits)`
次整文件写、以及每次召回都跑的 `index_summary`，都**不在**这个预算里。基准
（`scripts/memory_latency_cases.py`，造库不走 `add_note`，因为那是 O(N²)）实测：

| 规模 | 命中档 elapsed p50/p95 | 普通未命中 elapsed p50/p95 | 过滤未命中 elapsed p50/p95 | 写盘 p50 | `index_summary` p50 |
|---|---|---|---|---|---|
| 50 | 0.95 / 0.97 ms | 1.01 / 1.04 ms | 1.04 / 1.08 ms | 2.2 ms | 0.21 ms |
| 200 | 3.60 / 3.67 ms | 3.75 / 4.39 ms | 3.81 / 3.86 ms | 4.1 ms | 0.73 ms |
| 1_000 | 17.8 / 18.9 ms | 18.2 / 19.1 ms | 18.5 / 20.5 ms | 16.5 ms | 3.6 ms |
| 10_000（显式压力档） | 185.6 / 193.1 ms | 188.8 / 197.7 ms | 194.2 / 201.6 ms | 144.5 ms | 41.3 ms |

（本机实测，随环境与负载浮动；结论是量级，不是精确值。三档 × 三规模由
`scripts/memory_latency_cases.py` 打印，结构断言只用计数桩。）

三档的 `elapsed` **几乎相同**，因为它们都是一次全库 BM25：命中档与未命中档的差别只在
`hits` 是否为空，而排序成本与是否命中无关。真正的代价差在 `elapsed` **之外** —— 写盘
（`1 + len(hits)` 次）与 `index_summary`，两者都不计入预算。

**"普通未命中"必须是真未命中**：夹具若用 `"nothing matches this"` 这种库里一个词都不认识的
查询，`rank_notes_detailed` 会在词表检查处提前返回（`relevance.py:451`），量到的是提前返回
（200 条库 0.70ms）而不是一次排序（3.75ms），**差 5.4×**。夹具用五个只出现在单条笔记里的
稀有数字，使每条笔记的 IDF 覆盖只有 1/5 = 0.20 < `min_relevance` 0.25，排序跑满而门禁全拒。
`tests/test_memory_latency.py::test_a_plain_miss_ranks_once` 用
`diag.no_vocabulary_match` 钉住这条，防止夹具退化成提前返回。

所以 2.0s 是 1_000 条库命中档 p95 的约 **100×**、10_000 条压力档的约 **10×** —— 对当前生产规模
（`max_index_entries=200`）**非常宽松**，实际是"排序卡死"的兜底，不是性能 SLA。基准**不设硬阈值**：
阈值依赖部署环境，操作者按自己环境的 p95 定 buffer（共享 runner 上绝对时间断言天然抖动，所以基准
只打印分位，断言只用计数桩）。

被过滤掉的候选**不算使用**（模型没看见它），所以过滤必须在 touch 之前跑。`k` 截断掉的候选
同样不算——`touch_notes` 只收**真的返回给模型**的那些 id。这条不变量曾经只写在文档里、
没有任何测试钉住：把 `touch_notes([hit.note.id for hit in hits])` 换成 touch 全部候选，
1301 条测试全绿，而 6 条过了门槛的候选配 `k=1` 会记 6 次而不是 1 次，直接喂给
`hits >= 3 → importance + 1` 与驱逐价值函数。现在由
`tests/test_memory_service.py::test_r10_only_the_notes_the_model_saw_count_as_a_use`
和 `scripts/r10_cases.py::only_what_the_model_saw_counts_as_a_use` 双向钉住。

### 通用词重叠的诱饵：门禁修不掉（§1.6，本轮第三次否证，最重要的一条）

`n52`「今天要把 docs 目录里的 CLI 章节重写一遍」会回答「CLI 命令怎么加一个新子命令」，
只凭 `cli` **一个词**。它没被门禁挡住，因为 `coverage()` 的分母只算「索引认识的查询词」
（R3 的刻意选择：避免 CJK bigram 噪声稀释真命中）。

三种修法全部**实测否掉**：

| 候选修法 | 实测结果 | 判决 |
|---|---|---|
| 门槛改成「索引认识多少比例的查询词」（`known_fraction`） | 无答案集最高 0.33（就是那条诱饵），可回答集最低 0.00 | 否 |
| 覆盖度分母换成全部查询词（未知词按最大 idf 计入） | 诱饵与 5 条无答案全被拒、FP@5 掉到 0.13，但 **R@3 0.85→0.65、paraphrase 6/12→2/12** | 否（用召回换精度，换亏了） |
| 未知词按系数计入分母（0.15/0.25/0.35/0.5 逐档） | 只要能挡掉诱饵（w≥0.15、floor≥0.30），**paraphrase 立刻掉到 5/12 以下、R@3 掉到 0.78** | 否 |

所以这一条是**被接受的残留**，本轮接受它而不是假装它是零：

| 指标 | R10 前 | R10 后 | 门禁 |
|---|---|---|---|
| R@1 | 0.68 | **0.72** | 不设（0.85 属于未实现的语义层） |
| R@3 | 0.80 | **0.85** | ≥0.80 |
| FP@5 | 0.518 | **0.350** | ≤0.36 |
| paraphrase@3 | 6/12 | 6/12 | ≥6/12 |
| no-answer | 4/6 | 4/6 | **不设**，只打印 |

复现：`.venv/bin/python scripts/retrieval_cases.py`（`answered-but-unlabelled` 那一行仍然
点着诱饵和一条 PostgreSQL 连接串，这是预期输出，不是回归）。

**注入位置（缓存约束，R3b）**：

| 通道 | 内容 | 缓存性质 |
|---|---|---|
| 系统提示 | 基础指令 + 项目说明 + 技能目录 | **逐字节静态**，会话内不变（测试锁死） |
| 尾部易失块 `<memory-constraints>` | `pinned` + 最近决策，≤600 字符，空则不注入 | 只有这一小块每轮变化 |
| 历史（只追加） | 对话、工具结果、`memory_search` 结果 | 追加不破坏前缀 |
| 工具 `memory_search` | 按需召回的详情（id/kind/score/relevance/`matched_terms`） | 结果进历史，**之后长期命中缓存** |
| 工具 `memory_write` | 模型主动写下的一条耐久记忆（R7e-1） | 走 `_reconcile` 这唯一写路径，带 `evidence="model"`，`/memory` 可查可删 |

实测对比（同一内容、同长度）：记忆放系统提示前缀命中 **7%**，放尾部 **94%**；
每轮压缩历史中段只有 **26%**（现在改为按预算分页压缩后 92%）。

> ⚠️ **口径说明（2026-10-25，M-A6）**：上面几个数字都是 **R3 的「同一内容、同长度」测量**。
> 真实 loop 里历史会增长，且易失尾部（`<environment>` 的工作区路径 + `<task-state>` + `[budget]`）
> 每轮重渲染、位于历史**之后** —— 字节前缀在尾部处断裂。因此**整请求的前缀比率不是定值**：
> 实测随工作区路径长度漂移（8 / 32 / 66 / 442 字符路径 → 87% / 87% / 86% / 77%）。
> `memory_cases` 的 case11 已改用**路径无关的布尔不变量**（剥离尾部后，请求 N 的稳定部分
> 是请求 N+1 的精确前缀），不再使用百分比阈值。

**命令**：R10 起 `ai-coding memory list｜search｜forget｜pin` 已删除（无 CLI 入口）；
`MemoryService.write_note` / `MemoryStore.pin` / `MemoryStore.delete` / `store.all()` 仍在库里，
可从代码直接调用。TUI 里 `/memory` 看统计、`/compact` 主动压缩历史。
索引持久化为工作区下 `.memory`（schema v2，旧的 v1 文件只读迁移）。任何异常静默降级——
记忆永不抛错，且受 R2 的回合预算约束。「永不阻塞回合」**自 R10b 起不再成立**：召回命中会写
`hits`/`last_used`，是 `1 + len(hits)` 次同步整文件写。

**并发写不是安全的，别按线程安全用。** `MemoryStore` 的读路径都在锁里，但 `_mutate` 在
`_save` **之前**就放掉了锁，而 `_save` 无锁读 `self._notes`，两个线程会落到同一个
`<path>.tmp` 上，`os.replace` 把这个 inode 换到正式路径后，另一个线程仍开着它、剩下的写就落在
正式文件上了，于是文件变成**非法 JSON**——一个完整文档后面接着另一个文档的尾巴——下次加载读到
**0 条笔记**，不是少一条，是全丢。

复现方式很挑，这一点必须写清楚，否则它看起来像不存在：**要在每次写之前**
`threading.Barrier.wait()`，4 线程 × 30 次写，25 次里 5–6 次损坏。只在线程启动处同步一次，
25 次里 0 次损坏；不强制同步是 200 次里 3 次。也就是说**只有把两次写真正排在同一时刻才会触发**。

`scripts/r10_cases.py::recall_survives_concurrent_harvest` 跑的是「并发召回撞并发收割」并打印
内存条数与重载条数，它断言的只有**存活**（没有线程抛错、使用的计数落了盘、文件还在且可解析、
条数对得上）。它**不**复现上面那个损坏，所以它跑绿**不能**当作 hazard 不存在的证据——实测它
跑了 32 次，一次都没撞上。hazard 早于本轮存在，本轮决定不为了修它而改动持久化结构；
`store.py` 的类 docstring 带着那段复现。修法是让 `_save` 进锁，但那会改动每个调用方的持久化形状。

**一处可见的取舍**：`scope` 参数占了 schema 1129 字符里的 218 字符（删掉它整个 schema 是 911），
而本仓库写入的笔记 `scope` 全是 `workspace`，所以它**只可能返回空**。仍然保留：为了省每步
token 删掉一个已被 spec 定义的参数，是在优化那把尺子；语料也确实建模了会话级 scope。
代价写在这里，不藏。

### 可选语义档（R10d，默认关）

`memory.embeddings` 打开后，`memory_search` 多一路余弦打分：排序按 0.5·BM25 ⊕ 0.5·余弦
融合，绝对门变成「词面相关度 ≥ 0.25 **或** 余弦 ≥ 0.60」。三条硬约束：默认关（deepseek 系
端点没有 `/embeddings`，开了等于没开）；关掉/没配/端点不支持/网络失败/缓存损坏时检索结果
与纯词面**逐字节一致**（有单测钉死）；向量只在读时算，写路径不碰网络。

实测（text-embedding-v4，同 78/124 语料同时钟）：paraphrase@3 0/46→**6/46**，
可达 R@3 0.99→**1.00**，可达 FP@5 0.46→**0.48**。`min_cosine=0.60` 是量出来的（正例 min 0.44 /
负例 max 0.66 重叠，没有阈值能分干净），**换 provider 或模型必须重跑基准重标**——否则门
要么漏放噪音要么错杀。融合权重 0.5 未调参：46 条上 0.7 多救 1 条属噪声，未采纳。

**"配了但不工作"现在可观测（M-F1）**：`RecallDiagnostics.semantic_status` 四态 ——
`off`（未配置/不可用）/ `endpoint_unsupported`（端点 404，探测每进程一次并闩锁）/
`on`（已配置且端点可用，含无候选）/ `error`（调用中途失败，此时 `semantic_unavailable=True`）。
工具 payload 带 `semantic_status`（`getattr`，因为 pre-R10c 的 double 没这个字段）——
操作者配了 `enabled=True` 而端点没有 `/embeddings` 时，不再静默。标定脚本是
`scripts/calibrate_memory_threshold.py`（注入 transport，扫阈值出 FP/FN 曲线 + 推荐值，
按 `case.relevant` 过滤掉无 `relevant` 条目，并同时报 FP@5）；`retrieval_cases.py` 默认离线路径
只报 `lexical_hit` / `paraphrase_missed` 两档，`paraphrase_recovered` / `lexical_regression`
走 `--with-embeddings`。**有效域 = f(语料, embedding 模型)**，脚本输出里也打印这一句。

`scope` 那条取舍之外，这里还有一处：语义档让 `recall_with_diagnostics` 第一次出现 `await`，
之前"无 await 所以并发串行"的论证不再成立（touch 本身仍同步，所以写不交错，但 embed 并发）。
`store.py` 的 docstring 已同步到现在的事实。

### 融合规则可切换：RRF 实测更差，所以默认是 blend（R11d）

`memory.recall_fusion` 有两档：`blend`（今天的行为，两路各自 min-max 后取平均）与 `rrf`
（Reciprocal Rank Fusion，按**名次**融合：`Σ 1/(k+60+位次)`，归一到 `[0,1]`）。**默认 `blend`**，
理由是实测：

| 口径（124 条查询，同语料同时钟） | blend | rrf |
|---|---|---|
| 生产路径 · 可达 R@3 / FP@5 | **1.00 / 0.48** | 0.93 / 0.53（**越过门禁**） |
| 生产路径 · paraphrase@3 | 6/46 | **7/46** |
| 生产路径 · 全集 R@1 / MRR / FP@5 | **0.48 / 0.56 / 0.64** | 0.35 / 0.48 / 0.67 |
| 生产路径 · no-answer 拒答 | 4/10 | 4/10 |
| 裸排序（不过门禁）· reachable R@3 | **1.00** | 0.96 |
| 裸排序（不过门禁）· paraphrase R@3 | 0.41 | **0.26** |

两条结论，方向相反，都写在这里：

1. **RRF 换不来改述召回的整体提升，只换来 +1 条**（6/46 → 7/46），代价是可达集 R@3 掉到 **0.93**
   ——**低于 0.95 门禁** —— FP@5 0.48→0.53 也越过 0.50 门禁，全集 R@1 掉 0.13。所以它不能当默认。
2. **裸排序与生产路径的结论相反**（裸排序上 rrf 的 paraphrase R@3 是 0.41→0.26，生产路径上是
   0.13→0.15，两者的分母与门禁都不同）。这不是某一边算错了，是**两个不同的问题**：融合规则改的是
   `rank_term`，而三道门禁作用在混合分上，所以「换融合规则」的影响只能过完整路径量，不能用裸排序代答。
   这也是为什么 `_run_semantic_table` 现在打与词面表**同口径**的分解（此前它只打全集与 paraphrase，
   docs 里那组语义档的可达数字无法从脚本复现）。

**没有 semantic 信号时 RRF 不生效，自动回退 blend**：一路榜不存在「融合」，而且 RRF 会把词面排序
从「分数幅度」换成「名次」，那样会悄悄改变所有没开语义档用户的排序 —— 这条由
`test_r11d_rrf_without_a_semantic_signal_falls_back_to_blend` 钉住。归一化同理不可省：不归一的话
RRF 原始分（2/61≈0.033）会让 `weight_rank=0.55` 的排名项被 `weight_recency=0.20` 压过去，等于
偷偷重调了整套权重，测出来的结论就不可信了。

**它不设门禁**：`--fusion rrf --check` 只打印对照表与差异、退出码仍为 0。一个默认关闭且实测更差的
开关不该被写成契约；它欠读者的是那张表，不是 CI 的红灯。`memory.rrf_k`（默认 60，论文与
TencentDB Agent Memory 同值）同样只为重测而存在，故 `config.example.yaml` 里不给示例。

复现（需要网络与一个支持 `/embeddings` 的端点，例如 `models.qwen`）：

```bash
.venv/bin/python scripts/retrieval_cases.py --with-embeddings --config config.local.yaml
.venv/bin/python scripts/retrieval_cases.py --with-embeddings --fusion rrf --config config.local.yaml
```

## 技能

`SkillRegistry` 扫描技能目录，凡含 `SKILL.md` 的顶层目录即视为一项技能（M8 起 `.` 点目录除外，`.pending/` 就靠这条不可见）；一次解析产出一个 `Skill` 对象，`skills/catalog.py::build_catalog` 把它们汇成带 8000 字符预算的目录，供 `skill` 工具与系统提示使用。空目录不产生任何目录。

**版本与状态回显（R11c）**：`SKILL.md` frontmatter 里的 `version` / `status` 会被读出来，模型在
`skill list` / `skill read` 的结果里看得到——`version != "1"` 才显示 `(vN)`，`status` 不是
`active` 再加 `[archived]`、解析失败加 `[broken]`，`skill read` 额外给 `data={name, version, status}`。三处刻意的克制：

* `version` 是**字符串**，只做回显和「是不是默认值」判断，不做 semver 比较（否则 `1.10` 会排到 `1.9` 之下）；
* `status` 是白名单 `active|archived`，未知值读作 `active`，不发明第三种状态；
* **版本信息不进 L1**——目录是系统提示词里最贵、最缓存敏感的槽位，所以 `(vN)` 只出现在工具结果
  （进历史、缓存友好）。注意：这条在 M8 之前写的是「`catalog_text()` 一字未动 / 逐字节静态」，
  而代码每轮重读所有文件，且 `skill_review` 每 10 轮就会新增 skill——那个不变量本来就没成立。
  M8 改成 `(mtime, size)` 缓存 + 变更时显式声明，而不是继续声称一份假的稳定性。

全部老 skill（无 frontmatter、v1、active）的输出与 R11c 之前**逐字节相同**。`archived` 仍然可读，
只陈述状态、不推荐替代品（那需要知道替代品是谁）；真要拒读是策略，得走门禁层，registry 保持中立。

## MCP 集成

`MCPManager` 用官方 `mcp` 包**直接连 stdio 服务器**：`open_all()` 起 supervisor（M3 起非阻塞）、
`list_tools()` 发现远端工具。**M2 起每个远端工具不再是注册表里的一条 function
schema**：LLM 只见一个 `mcp_call(server, tool, input)`，可调用的工具由
`MCPManager.available()` 单一来源（`tools` whitelist 在此生效）渲染成有预算的 catalog
文本（每条描述 ≤120 字符、总量 ≤8000 字符），**每步**挂进 loop 的 volatile tail——
所以重连后新工具下一步就可见，且目录可见性与执行授权分离（被 deny 的工具仍在目录里）。

- 工具名带 `{server}__{tool}` 前缀，避免两个 server 同名工具互相覆盖；
- `MCPToolAdapter.parallel_safe = False`（远端副作用未知，保守串行）；
- 权限上落到「未知工具」分支 → WARN：非交互入口自动批准，交互入口弹确认；
- 单个 server 连接失败**静默跳过**，不影响其它 server 与主链路（增强组件不阻塞会话）；
- `aclose()` 统一拆除会话，幂等且对未开启的 manager 安全；
- 传输层可注入（`session_factory`），因此单测不需要真的 spawn 子进程。
- **R2 起**：每个 server 由一个「会话属主任务」持有（连接与关闭必须在同一任务里，
  这是 anyio cancel scope 的硬约束）；连接受 `mcp.servers[].timeout.startup`（ms）约束，装死的 server 到点跳过；
  server 的 `environment` / `cwd` 会透传给子进程（`environment` 的键大小写敏感，YAML 里原样透传，不参与 camel 归一化）；
  factory 在交出 session 前显式 `initialize()` 完成握手。
- **M5 起（引导式连接）**：`/mcp` slash 打开 MCP 面板（连了谁的 server、什么状态、失败原因）；面板 `a` 起引导向导：选类型（local/remote）→ 填 name 与 command/url → 选是否信任该 server 的工具（默认每次调用都弹审批）→ 后台校验、起连接、写入 `mcp.local.yaml`（重启后自动重连）。运行时可增不可删/改（删改需编辑文件）。快捷键：命令面板 `ctrl+x p`（原 ctrl+p 与 VS Code 冲突）。
- **M4 起（remote + OAuth）**：`type: remote` 的 server 走 Streamable HTTP（`url`/`headers`/`oauth{callback_port,scope}`）。401 → 面板显示 `Sign in required →`，enter 触发 `mcp.login`：开浏览器 → 本地一次性回调 → DCR/PKCE/token 交换（SDK）→ token 落盘 `~/.local/share/ai-coding/mcp-auth.json`（0600）→ supervisor 醒来用 token 连上，**全程无需重启**；`mcp.logout` 删 token 回到 needs_auth。needs_auth 的 server **不做定时重试**（等人不等时）；其余失败仍走 M3 的 backoff。
- **M2 起**：每个远端工具不再是注册表里的一条 function schema（13 工具实测每步多付 ~9,000 字符）。LLM 只见 `mcp_call(server, tool, input)`，可调用工具由 `MCPManager.available()` 单一来源渲染成有预算的 catalog（每条描述 ≤120 字符、总量 ≤8,000 字符）挂进 loop 的 volatile tail 每步重建，重连后新工具下一步即可见；`permissions{allow,deny}` 按远端工具名授权（deny 优先且凌驾 always），目录可见性与执行授权分离。
- **M3 起（生命周期）**：启动不阻塞——`open_all` 起 supervisor 即返回，TUI 秒起，行显示 `Connecting …` 直到首次 attempt 落定；连接并发 ≤4（否则 10 个 npx server 同时冷启动）；每个 server 永久 backoff 重连（1s 翻倍、封顶 60s），修好 PATH/装好依赖后自愈无需重启；`Degraded` 每 server 仅首次。**掉线只在调用时可见**（parked owner 不读流，实测）：传输类调用失败才标记「连接已断开，正在重连」并重连，参数错等普通工具错误不重启 server。面板 failed 行 enter → `mcp.connect` 立即重试；`mcp.status.changed` 事件驱动开门的面板刷新。
- **M1 起（v2 契约）**：`command` 是 argv 一体（`["npx","-y","pkg"]`，原来的 `args` 并入）；
  per-server 开关从 `enabled` 翻成 `disabled`（默认开）；`env` 改名 `environment`；单个 `connection_timeout`
  拆成 `timeout{startup, catalog, execution}` 三段（毫秒）——握手、tools/list、单次调用各管各，
  挂住的工具列表不再躲在为 npx 冷启动准备的启动预算后面；删除无消费点的 `auto_discover`。

`mcp.enabled=false`（默认）时 `open_all()` 立即返回（`-> None`，不连接任何服务器）；
可调用工具一律从 `MCPManager.available()` 读，`open_all` 从不返回工具列表。

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
   旧的默认 `mcp.connection_timeout` 是 30s——R7d 起默认 **60**（M1 起为 `timeout.startup=60000` 毫秒）；
   npx/uvx 类 server 还可以再调高，或先预热缓存。
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
| 缓存前缀兜底 | 端点不报 cache 时，用「与上一次请求的最长公共前缀」比率兜底（纯追加回合实测 0.95，指**剥离易失尾部后**的稳定部分占比；含尾部的整请求比率会随工作区路径漂移，见 M-A6 的口径说明） |
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

### 只检索不动作：`[search-gate]`（B1/B2/B5）

上一档只看**失败**。实测的病根是另一种形状：T2 的 63 次调用**全部成功**，全是 `read`/`grep`，
一次也没改代码或验证 —— 模型以为自己在推进。判据是**精确可算**的：

| 信号 | 定义 | 谁能算 |
|---|---|---|
| `search_streak` | 连续**成功**的检索调用（`read`/`grep`/`glob`/`history_search`/`memory_search`/`websearch`/`webfetch`），中间没有一次成功的"其它"调用 | 只看工具名 + `success`，**不解析输出** |
| 重置 | 任何成功的非检索调用（`edit`/`write`/`bash`/…）把它清零 | 同上 |
| 不计数 | 失败的检索调用（那归 `[strategy-gate]`） | 同上 |

> **为什么不按"输出有没有新内容"判定**：实测双向皆错 —— `grep` 与 `glob` 的无匹配输出
> 逐字节相同（`(no matches)`）、`glob` 按 mtime 排序、`history_search`/`websearch` 的输出
> 内嵌 query、`grep` 把命中行裁到 300 字符；且 `DuplicateCallGuard` 默认拒绝第 3 次相同调用，
> "相同输出"最多出现 2 次。详见 [plan-m-b.md](plan-m-b.md) §二。

上升两档（**没有强制收尾档**：这个信号分不清"读了 8 个不同文件的调研"与"8 次写错符号的空转"，
强行交接的害处更大；收尾交给 `[budget]` + 自动收尾）：

| 档 | 触发 | 动作 |
|---|---|---|
| L1 | `search_streak >= 4` | `[search-gate]`：换符号名 / 换范围 / 读入口文件 / 直接动手 |
| L2 | `search_streak >= 6` | `[search-gate]`：**不要再猜关键词**，并**附上 harness 自己跑出来的静态检查输出** |

L2 定在 6 而不是 8：默认 `max_tool_iterations=10`，而一次 nudge 占一个迭代 —— 8 会把 L2
推到最后一个迭代，输出生成了却没人读。实测 6 时 L2 落在第 8 个迭代，后面还剩两步。

**动作白名单**：只有 `write`/`edit`/`bash` 清零 streak。`todo_write`、`checkpoint_write`、
`ask_user`、goal 工具**不清零** —— 它们不改工作区，若算作动作，模型写一次 todo 就能把空转
藏起来。（`bash` 在名单里是故意的：跑静态检查就是动作。）

`plan` 与只读姿态**不触发**：那两种姿态本来就该"只看不动"。

L2 的输出来自 `ai.static_check.commands`（默认 `["ruff check ."]`），但**默认不执行**：
需要操作者 `export AI_CODING_STATIC_CHECK=1`。原因不是保守 —— 该字段能被**仓库里被跟踪的
`config.example.yaml`** 设上（`ConfigLoader` 会向上查找它），配置本身不能充当"允许运行代码"
的凭证，否则克隆一个不受信仓库就等于把执行权交给它。环境变量是仓库碰不到的信道。

开启后仍有两道约束：

- 每条是 **argv 不是 shell 字符串**：`shlex` 切分 + `shell=False`，所以
  `;`/`&&`/`|`/`$(...)`/`>`/通配/`$VAR` **都不展开**，只是普通参数；
- 首个词必须在白名单里（`ruff`/`mypy`/`pyright`/`flake8`/`black`/`isort`/`tsc`/`gofmt`/
  `staticcheck`/`shellcheck`）；`cargo`/`go`/`eslint`/`pylint`/`rubocop`/`phpstan`
  **故意不在名单**——它们本身就是"执行仓库代码"（`build.rs`、`go run`、配置模块、
  `--init-hook`）。

> 没开启时，L2 的文案仍然把该跑的命令写给模型，由它经 `bash`（有权限门）去跑。
> 开启等于操作者表态"这个仓库的 linter 我信"，与"在这个仓库里跑 make"是同一个决定。

`.venv/bin` 优先 PATH，超时 20s、总量 ≤2000 字符，**每回合最多跑一次**，且**不在回合的最后
一步跑**（跑完没人读就是纯成本）。`ai.search_streak_threshold: 0` 可整机关闭该阶梯
（含 L1）。

同一档只在 streak 又长出一整个 L1 阈值后才再次发声（不然模型会把预算花在读提示上）。
每次触发发一个 `SearchStalled` 事件（`iteration`/`level`/`calls`/`streak`），
在 trace jsonl 里长这样：`{"evt": "SearchStalled", "data": {...}}`。

## ask_user：模型主动提问（R6c）

与被动审批互补：审批是「这一调用能不能做」，`ask_user` 是「我该做哪个」。

- 阻塞式：答案作为**工具结果**回给模型，避免「回合停在半个工具调用上」被配对清理掉；
- 有界：`tools.ask_user.timeout_seconds`（默认 300）+ 15s 收割余量，等待走线程池，
  因此回合上限和调用预算真的能打断它；
- **没人也是答案**：非交互入口（`ask`）与子智能体**不注册**该工具（工具列表是第一道 harness）；
  即使注册了而没人，也返回 `no_user` 并让模型「明确写出假设后自行决定」；
- 空行 / EOF 记 `no_answer`，通道自身出错记 `user_prompt_error`——都不会中断回合。

**TUI 接线**（M6.5，GUI 复刻后补的最后一环）：`world.build_world` 装配
`CLIUserPromptCallback`，`handlers._wire_user_prompt` 把 provider 装成对前端的
`user_prompt.ask` 服务端请求——前端 `AskPanel`（`◆ Question`）显示问题与选项，
答案作为该请求的 JSON-RPC 应答返回，工具结果即 `用户回答：…`；esc / 回合结束
（后端已走远）回空串，模型按契约自行决定。此前该入口用
`UnavailableUserPromptCallback`（模型恒被告知「没人可回答」）。

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
  模型表由 `cli.py` 作为 `AgentLoop(models=...)` 传入——它挂在 `AppConfig` 上，`AIConfig` 从不携带模型表（曾因此使 `/model` 恒报 unknown model）。

**v1 明确不做**：欢迎页、侧栏、计划卡；`Esc` 取消工具批；steering 接回。
（命令面板**已实现**：输入 `/` 唤起内置 `CommandPalette`，覆盖全部 slash 命令。）

**折叠体的呈现约定**：Textual 的 `Collapsible` 自带 `border-top: hkey $background` 与
`padding-bottom: 1`——我们从没过问过这两条默认值，结果一个折叠块**花三行只放一行字**，还在
transcript 上划一道全屏横线（实测一屏 3 条）。`Transcript Collapsible` 一律 `border: none;
padding-bottom: 0`。折叠标题的展开指示由**持有状态符的一方**决定：工具标题自带 `▸/✓/✗`，所以
工具折叠用 `collapsed_symbol=" "` 让位（否则渲染成 `▶ ✓ 已读取文件`，两个符号讲同一件事）；思考
折叠与计划步折叠没有自己的状态符，保留默认 `▶`。

**工具活动折叠体**：一个回合的所有工具折叠体收进**一个** `▶ 工具 N 个` 活动折叠体，展开才见各工具。
此前每个工具一行。活动折叠体在**回合开始就建好（隐藏）**——Textual 的 `Collapsible` 异步组合 body，
若在工具到达的同一 tick 才建，body 未组合，同 tick 的一批工具（并行批 / `/resume` 重放）会 `MountError`；
延迟挂载的重试时机不确定（实测同一测试 5 次红 3 次）。预建让 body 提前组合，生产路径直接挂载；同 tick
的残余批次由 `_activity_pending` + `call_after_refresh` 兜底。代价：活动折叠体是每个 Turn 的隐藏子件，
`query(Collapsible)` 会匹配到它。工具不再与散文交错（收进活动），顺序恒为 `[思考][工具]`。

**思考聚合**：`append_reasoning` 每回合只保留**一个** `▶ 思考过程` 折叠体，各步 reasoning 累积进去
（`\n\n` 分隔）。此前每个 model step 各挂一个，实测一个单工具回合就有 2 条、15 步回合 15 条。累积用
**重建折叠体**（mount 新的、remove 旧的，同 `_swap_fold`/`plan_state` 的 WP4b 结构纪律），不用
`.update()`：`/resume` 重放会在同一 tick 连续调用多次，重建挂在 Turn 上、不依赖旧折叠体 body 是否
已组合。用户展开/折叠的选择随重建保留。工具折叠体不受影响，仍各自独立（Stage 2 再聚合）。

**用户气泡宽度**：`width: 1fr`。原本是 `auto`（shrink-wrap），实测在 118 列终端上只占 54 列，
右边空 60 多列，正文被挤成两三行却把横向空间白扔；`margin: 0 2` 保留。后去掉四面包围盒与
灰底，只留左边一条 `heavy` 竖线（`$surface-lighten-2`——注意 `$text-muted` 展开后含空格，
不符合 border 的两段式，样式表解析失败）。输入区右边那个永远对不齐的 `➤`（`content-align`
是盒内对齐，管不到 3 行高的父容器）直接删掉，它零功能、无引用，参照客户端也没有。

**计划折叠体**：每个 `TaskUpdated` 曾经在 transcript 顶层挂一份全量计划渲染——一次 `/plan`
产生 15 次更新，实测 110 列终端下约 29 物理行 × 15 ≈ 8 屏。现在每 Turn 只保留一个折叠体
（标题 `计划 1/7`，中性 muted 配色），后续更新只改标题；展开后是去 `<task-state>` 标签的
完整计划。模型读的那份（`todo_write` 输出、prompt 注入）保持原样。

**工具折叠卡片**：工具调用渲染为可折叠卡片——折叠标题 `▸ 正在运行命令 · <命令头60字>` /
`✓ 已运行命令 · …` / `✗ 已读取文件 · …`（动词-目标中文标题，标题左侧是状态符本身，展开时状态符
不变）；
展开后卡片四行：类型标签（`Shell`/`Read`/…，未知工具用原名）、`$` 完整命令（换行不断）、
全文输出（回合结束时从会话回填，受 20000/2000 封顶）、`✓ 成功` / `✗ 失败` 右对齐 footer。
运行中折叠体同样是精确高度正文（随 chunk 长大），无滚动嵌套——内层滚动已随 `RichLog` 的
退役而消除，外层 `Transcript` 是唯一的滚动容器。**吸底策略**（§3.3-3 规格，`docs/superpowers/plans/2026-09-28-textual-ui.md`）：
transcript 用 Textual 的 **anchor** 而不是"采样 `is_vertical_scroll_end` 再滚一次"——挂载的控件要到下一次
layout 才有高度，采样读到的是已经不存在的视图（实测：60 个块的突发追加把滚动目标留在 0/36，30 回合
`/resume` 重放停在历史中段）。anchor 由 compositor 在每次 arrange 时用 `set_reactive` 把视图钉在底部，
用户滚动即释放；`_pinned` 只负责"用户走了之后不要把他拽回来"（anchor 本身无法为已离开的用户重新武装）。
app 的 50ms flush 定时器**只在真的写了内容时**才吸底，否则空闲 tick 会与用户滚动打架。`Ctrl-Y` 把上一条已完成工具的全文
（参数 + 输出）送进系统剪贴板（OSC 52，macOS Terminal 除外）；标题 chip 保持 60 字截断
（参考实现亦如此），全文永远在卡片第二行。

**计划步的实时视图（M3）**：`/plan` 执行时每个步在 transcript 里开一个**折叠块**（标题 `[plan] 步骤 k/N：<步骤内容>`），
步的模型正文按 50ms 定时器与助手正文同节奏刷新（`remove+mount` 换块，不是原地更新——WP4b 的滚动计数规则），
步结算后标题追加收据摘要，于是**实时视图与 `/resume` 重放出的是同一句话**。步正文**不进父会话、不落盘**（M1 的短对话
设计：步 transcript 用完即弃），所以重放只能重建收据那一行——这是有意的取舍，不是漏掉。REPL 没有折叠控件，改为
直接打印步正文（同样不接受"只给一行摘要"）。

**UI 文本边界（markup）**：Textual 与 rich 默认把 `str` 内容当 console markup 解析，而工具输出、
工具参数、用户消息、会话预览里都有 `[`——解析结果要么**静默吞字**（`build [k=v]` → `build `），
要么**崩掉整个应用**（`MarkupError: Expected markup value`，如 `[path=/tmp/x]`）。因此所有外部
文本只经两个出口进界面（`ui/plain.py`）：

| 出口 | 用在哪 | 为什么不能只用别的 |
|---|---|---|
| `Plain`（`markup=False` 的 `Static`） | 渲染字符串的挂件位置 | — |
| `plain(str) -> Content` | 传数据的通道（命令面板的 `Option.prompt`） | `CommandPalette` 自建 `OptionList(markup=True)`，改不到它的参数；`Content` 是 `Visual`，`visualize()` 直接交给渲染器，不进 markup 解析 |

已审计并确认**天生安全**的通道：`Collapsible(title=…)`（Textual 内部走 `Content.from_text`）、
`Prose`（rich markdown 自己解析）、`rich.text.Text` 输入的状态栏/审批/问答（走 `from_rich_text`）。
`DialogueConsole` 的三处 `print` 显式 `markup=False`（生产休眠路径，但契约在 `repl.py`）。
`tests/test_ui_text_boundary.py` 用 AST 强制：全包禁止裸 `Static(`、禁止把非字面量当显示值传给
`Option/Hit/DiscoveryHit`、`console.py` 的 print 必须 `markup=False`、`RichLog` 不得复现；文件顶部
维护**已审计通道表**，新增通道必须同步守卫。

**一条已知退化**：

1. **模型驱动的 slash 命令不渲染工具事件块**：`/review`、未知 `/foo` 在
   `handle_input` 里直接调 `loop.process_input` 且不传 observer，期间的 `ToolStarted` /
   `ToolFinished` 不进 transcript（流式回答本身仍进 transcript）。`/plan` 不在此列——
   `_plan_turn` 自己调 `process_input` 并传了 `observer`，工具块照常渲染。状态栏会照常置忙
   （命令与回合同样串行），缺的只是工具事件块；旧 UI 的 loop 级 observer 会渲染，属 v1 退化。

**验证方式**：`tests/test_server_ipc.py`（socket 级审批/会话/中断 E2E）、
`tests/test_server_bridge.py`（事件→IPC 映射）、`tests/test_console_text_boundary.py`
（markup 边界守卫），前端渲染由 `ai_coding/frontend/test/`（bun test）覆盖，另以真实终端人工验证。



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
   （前端实时显示）。chunk 是**纯观测**：不进请求、不进历史、不进会话文件，
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

`TraceStore.sessions()` 让「有哪些会话」可枚举（`last_activity` 倒序、tie 按 id），`--since` 与 `prune`
**共用同一个口径**：`SessionRef.last_activity`，即会话自己 trace 里最后一个可解析的 `ts`（读不到时
回退目录 mtime）。M-G2 之前用的是**目录 mtime**，而 `rsync` 不带 `-t` 会把它改成传输时间 —— 于是每个
会话都像刚写的，`--since` 返回全部、`prune` 再也删不掉旧会话。所以「prune 会删什么」和「什么算旧」
现在由数据本身决定，而不是文件系统。
某会话的 `summary.json` 缺失或损坏时记零（不抛异常），`summary.turns != 磁盘回合数` 时**只标注**
`mismatch`、退出码仍为 0。导出**默认不含**消息全文，`--full` 显式打开（`turn-<n>.full.json` 只在
`capture: full` 时才会存在）。

验收脚本：`.venv/bin/python scripts/r9_cases.py`（离线 12 条）；
`--real` 再加 1 条：真模型跑一个回合，断言那份报告真的把这次会话算进去了。
