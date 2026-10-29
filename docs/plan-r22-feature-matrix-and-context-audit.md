# R22 方案：功能矩阵真机验证 · 上下文有效性审查 · TUI 交互优化

> 状态：**方案待评审**（评审结论见 §8，评审后才会进入实施）
> 原则：先拿证据，再改代码。凡是"能不能用"的判断，必须有三类证据里至少两类；
> 凡是"这份上下文有没有用"的判断，必须能从请求体或 trace 里指出具体字符。

---

## §0 一句话结论

上一轮（R21）证明了两件事：长任务**可以**跑通（`long-r21c` 5 轮完成），坏 tool_call 能被挡住。
但"跑通"只证明了主闭环；**几十项功能里绝大多数只有单测、没有真机证据**，
而"每轮发给模型的上下文中哪些块真被用上了"从来没有量过。本轮做三件事：

1. 把能力清单变成**可执行的功能矩阵**，逐项真机跑，并区分「能用」与「能有效触发」；
2. 用 trace + 录制请求体，**逐迭代解剖一次请求**，给 12 个 tail 块和 6 段系统提示逐个下裁定；
3. 按矩阵结果修「不可用/不可触发」的功能，再优化 TUI 与每个 slash 的交互。

本轮**不发布、不改默认行为**；产出是证据、最小修复、与一份可复跑的脚本集。

---

## §1 判定口径（先把"能用"说清楚）

| 等级 | 含义 | 证据要求 |
|---|---|---|
| **L0 无入口** | 用户在本界面根本看不到 / 打不开 | 调色板/帮助里没有，且无从触发 |
| **L1 单测可用** | 有测试，但没在真机上跑过 | pytest/bun 用例通过 |
| **L2 真机可用** | 真机跑过、结果正确 | 会话日志或产物 + TUI 帧 |
| **L3 可触发** | **一条自然语言/命令就能稳定唤起它**，不是碰巧 | 指令 → 事件 → 参数 → 结果，四连都成立 |
| **L4 有效** | 触发后产出的信息被模型/用户**实际使用** | 答案或后续动作里引用了它的具体内容 |

**"能触发"的硬标准**（防"碰巧"）：同一条指令跑两次，两次都出现目标事件（如
`subAgent` 工具调用 / `ContextCompacted` / `PlanApproved`），且参数合理（不是空参、不是被拒）。
一次成功记为 L2，两次都成立才记 L3。

---

## §2 功能矩阵（现状盘点）

证据等级按现有仓库里的报告与代码判断；**空白处就是本轮要补的证据**。
既有来源：`docs/r20b-eval-report.md`（TUI 22 条命令面板矩阵）、`docs/plan-r21-six-fixes-and-longtask.md` §7（长任务）。

### §2.1 斜杠命令

后端权威词表在 [`slash.py:22-51`](file:///workspace/ai-coding/ai_coding/app/slash.py#L22-L51)（25 个 canonical），
TUI 调色板在 [`slash.ts:42-143`](file:///workspace/ai-coding/ai_coding/frontend/src/util/slash.ts#L42-L143)（22 条）。

| 命令 | 形态 | 触发/判定信号 | 现有等级 |
|---|---|---|---|
| `/help` `/h` | 本地面板 | 对话框出现、可滚动、esc 关闭 | L2（r20b 真机） |
| `/status` | 本地面板 | 状态对话框，含 session/turn/token | L2 |
| `/models` | 本地面板 | 模型列表、可切换（切换后请求里的 model 变） | L2（切换未验） |
| `/connect` | 本地面板 | 向导、写回配置 | L2（未验写回） |
| `/mcp` | 本地面板 | 服务器列表 + 连接流程 | L2 |
| `/sessions` | 本地面板 | 列表、可打开 | L2 |
| `/skills` | 本地面板 | 技能列表（来自 skills 目录） | L1 |
| `/new` | 本地 | 新会话、历史清空 | L2 |
| `/quit` | 本地 | 退出 | L1 |
| `/review` | 本地/后端 | git diff 评审 | L1 |
| `/permissions` | 面板 + 参数文本 | 面板展示；`/permissions auto\|confirm\|clear` 改策略并落盘 | L2（参数式未验） |
| `/memory` | 本地面板 | 笔记可 pin/delete；`.memory` 变化 | L1 |
| `/todo` | 后端文本 | 任务列表渲染 | L1 |
| `/budget` | 后端文本 | token 账本 | L1 |
| `/trace` | 后端文本 | 上一轮 trace 摘要 | L1 |
| `/config` | 后端文本 | 生效配置 | L1 |
| `/stats` | 后端文本 | 全部 traced session 汇总 | L1 |
| `/tasks` | 后端文本 | 后台子代理运行清单 | L1 |
| `/compact` | 后端文本 | `ContextCompacted` 事件、消息数/字符数下降 | L1 |
| `/undo` `/redo` | 后端文本 | 历史 + 文件一起回滚/恢复 | L1 |
| `/export` | 后端文本 | `[export] wrote <绝对路径>`，产物可打开 | L2（html 已截图） |
| `/model <key>` | 只得手打（`/models` 面板等效） | 切模型，下一请求 model 变；**参数是配置里的 key**，不是 wire name | 别名缺口 |
| `/resume <id>` | 只得手打（`/sessions` 面板等效） | 载入旧会话 | 别名缺口 |
| `/clear` | 只得手打（`/new` 等效） | 新会话 | 别名缺口 |
| `/agents [id]` | 命令面板有 `dialog.agents.show`，slash 无 | 切 agent，posture 事件 | 补全缺口 |
| `/goal <obj\|resume\|off>` | 两处都没有 | `TaskArmed(profile=goal)`、goal 回合自动续 | **L0** |
| `/plan` 家族 | 两处都没有（help 也隐藏） | 见 §2.3 | **L0** |
| `/readonly on\|off` | 两处都没有（help 也隐藏） | posture 事件（R21 已修，现在会发） | **L0** |

**要点**：`/model`、`/resume`、`/clear`、`/agents` 有**等效入口**（`/models`、`/sessions`、`/new`、
命令面板），只是名字对不上；真正"没有任何入口"的只有 **`/goal`、`/plan`、`/readonly`**。
`/plan`/`/readonly` 的隐藏是产品决定（内化批次把四行内部命令从 help 摘掉），本轮只验证"手打可用"。

### §2.2 工具

注册表分两处装配：文件/网络/任务类在 [`builder.py:265-410`](file:///workspace/ai-coding/ai_coding/tools/builder.py#L265-L410)，
记忆类在 [`cli.py:195-207`](file:///workspace/ai-coding/ai_coding/cli.py#L195-L207)。共 25 个：

| 工具 | 用途 | 需要触发的能力 | 现有等级 |
|---|---|---|---|
| `read` `grep` `glob` | 只读探索 | 一次只读任务 | L2（长任务里大量使用） |
| `edit` `write` | 改文件 | 写一个文件并复核 | L2 |
| `bash` | shell（审批/沙箱） | 跑测试；验证审批与沙箱两条路径 | L2（审批路径未验） |
| `todo_write` | 任务清单 | 「先列步骤再执行」 | L2（plan 链路） |
| `subAgent` | 派发子智能体（前台/后台） | 见 §2.4 | L1 |
| `subAgentList` | 子代理清单/取回结论 | 后台子代理跑完后取结论 | L1 |
| `job_list` `job_output` `job_kill` | 后台 shell 作业管理 | 起后台命令后读输出/杀掉 | L1 |
| `skill` | 加载技能 | 见 §2.5 | L1 |
| `present_plan` | 提交计划 | 见 §2.3 | L2 |
| `finish_research` | 计划模式的"研究收尾"出口 | 调研类请求不走计划直接给结论 | L1 |
| `ask_user` | 向人提问并等回答 | 歧义需求时主动问 | L1 |
| `start_goal` `update_goal` | 自驱目标 | 见 §2.3 goal 链路 | L1 |
| `websearch` `webfetch` | 联网 | 需要外部信息的任务 | L1 |
| `mcp_call` | MCP 远程工具 | 见 §2.5 | L1 |
| `history_search` | 压缩后取回原文 | 压缩后引用旧措辞 | L1 |
| `memory_search` `memory_write` | 记忆检索/写入 | 见 §2.5 | L1 |
| `checkpoint_write` | 手动 checkpoint | 长任务中途固化结论 | L1 |

> 注意：`ask_user` 与 `present_plan` 需要 `user_prompt` 回调，评测 harness 传的是 `None`
> （`eval_fullstack_task.py:252`），所以**这两个在 harness 场景里根本不存在**；
> 要验它们必须走 TUI 或 CLI 入口。

### §2.3 编排：plan 与 goal

| 环节 | 信号 | 现有等级 |
|---|---|---|
| 进入计划模式（`/plan <topic>`） | `AgentSwitched(readonly=true)`、工具面变只读 | L2（r20b tmux） |
| 计划评审卡（`present_plan`） | `AskRequest(intent=plan_review)`、条目表、approve/keep/dismiss | L2（R21 又修了 detail 渲染） |
| 批准后自动执行 | `PlanApproved`、plan 回合逐条执行、`[plan] 完成 N 步` | L2 |
| 退出计划模式（`/plan run`） | `AgentSwitched(readonly=false)`（R21 修） | L2（本轮补只读轴证据） |
| 分步验收 / fiat 回填 | 每条 step 的 `StepDelivery(attested)` | L1 |
| goal 链路（`/goal obj`） | `TaskArmed(profile=goal)`、每轮自动续、`update_goal(complete)` 收尾 | L1 |
| 干预阶梯（重复/停滞） | `intervention_state`、L2/L3 拒绝 | L1 |
| todo nudge / steering | `TaskUpdated`、steering 行进入历史 | L1 |

### §2.4 子智能体与后台任务

| 能力 | 入口 | 信号 | 现有等级 |
|---|---|---|---|
| `subAgent` 前台 | 模型自主调用 | `subagent.started/finished`、结论 packet | L1（`long-r21c` 里触发过一次但被坏 JSON 挡下） |
| `subAgent` 后台 | `background: true` | 父回合结束后仍跑、`job.changed` | L1 |
| `/tasks` | 命令 | 运行中/已完成清单、`stop <id>` | L1 |
| bash 后台 job | `bash(run_in_background)` | `job.started`、日志文件、退出码回填 | L1 |
| worktree 隔离 | 配置 `subagent_worktree_isolation` | 子代理在独立 worktree、合并回主工作区 | L1 |

对照：`long-r21c` 里子代理的**参数非法**（截断）被 R21 的守卫拒绝后，模型**没有重试子代理**，
改为自己顺序写文件——这说明"能调用"与"能被有效使用"是两回事，本轮要单独量。

### §2.5 记忆 / 压缩 / 技能 / MCP / 钩子

| 能力 | 信号 | 现有等级 |
|---|---|---|
| 自动压缩 | `ContextCompacted(before/after chars)`；压缩后仍能正确引用旧事实 | L1 |
| checkpoint 摘要 | checkpoint 消息（`[以下为早期对话摘要]`）进入历史 | L1 |
| 手动 `/compact` | 同上，消息数下降 | L1 |
| `history_search` | 压缩后取回原文并引用 | L1 |
| 记忆 harvest | `.memory` 增长、下一轮 `MemoryInjected(note_ids)` | L1 |
| 记忆块（tail） | 请求体里有 `<memory-index>`，含 pin/decision | 未验 |
| 技能路由（recommended_skills） | 请求体里有推荐块；模型真的调用 `skill` | 未验 |
| `@file` 引用块 | 请求体里有文件内容；答案引用了它 | 未验 |
| MCP 连接与 `mcp_call` | 服务器 connected、远程工具出现在工具面 | L1 |
| 钩子（command/python） | `TOOL_BEFORE` 拦截/改写生效 | L1 |
| 沙箱（bwrap） | 工作区内写成功、工作区外被拒 | L2（R20 实测） |

### §2.6 TUI 界面与 slash 交互

| 区域 | 组件数 | 现有证据 |
|---|---|---|
| 转录（message/toolcard/reasoning） | 3 | L2（截图） |
| 输入与补全（prompt） | 1 | L2（R20b 修了 3 个 bug） |
| 状态栏 / 头部 / 侧栏 | 3 | L2 |
| 对话框 | **15** | 部分 L2 |
| plan 评审卡 / ask 卡 / 技能待批卡 | 3 | 评审卡 L2，另两个 L1 |

对话框 15 个：help / models / connect / mcp / mcp-add / sessions / skills / status / permissions /
memory / jobs / agents / inbox / select / skill-pending。`skill-pending` 是"模型生成技能、等人批准"，
本方案第一版漏了它——而它正是"技能层能不能被有效触发"的关键一问。

---

## §3 真机验证协议

### §3.1 夹具

- 工作区：`/workspace/eval-workspace/r22/<scenario>/`（每个场景一个，互不污染）。
- 配置：[`config.record.yaml`](file:///workspace/ai-coding/config.record.yaml)，`base_url` 指向录制代理
  `127.0.0.1:8899`，`process_sandbox.network.mode: "off"`（引号不能省，否则 YAML 解析成布尔 false）。
- 流量：**用仓库自带的** [`scripts/recording_proxy.py`](file:///workspace/ai-coding/scripts/recording_proxy.py)
  （不是临时脚本），每个场景单独 `--out /tmp/record/<scenario>.jsonl`。
  它的每行是 `{path, request, status, events, content_chars}`，**没有 session/turn 字段**，
  所以与 trace 的关联只能按"场景内第 n 个请求 ↔ 第 n 个 `ContextAssembled`"对齐（§3.3 步骤 6）。
- TUI：tmux 190×46，`capture-pane -p -e` 取帧；启动命令固定
  `python -m ai_coding -p config.record.yaml -w <scenario>`，这样 `tracing.dir` 的相对路径
  解析到**工作区**而不是仓库根；脚本头里写死 `cd <workspace>`。
- 终止条件：TUI 场景最多 **12 轮 / 每轮 ≤ 600s**；harness 长任务场景按 `--rounds 8`
  （harness 自己会把 `max_tool_iterations` 提到 30、`turn_timeout` 提到 1800、
  `max_auto_continuations` 提到 4——见 `eval_fullstack_task.py:231-233`；
  所以 S2 的长度**不能在 TUI 里复现**，必须用 harness）。

### §3.2 三类证据，每项功能至少两样

| 证据 | 来源 | 能证明 |
|---|---|---|
| **A 界面帧** | tmux `capture-pane` | 用户看到了什么 |
| **B 事件/产物** | trace jsonl、session json、`.memory`、exports、jobs 日志 | 系统真的做了什么 |
| **C 请求体** | `/tmp/record/<scenario>.jsonl` | 模型真的收到了什么 |

> **已核实缺口**：评测 harness（[`scripts/eval_fullstack_task.py`](file:///workspace/ai-coding/scripts/eval_fullstack_task.py)）
> 自己构造 `AgentLoop`，`_build_loop(..., None)` 最后一个参数是 `trace=None`，**没有接 `TraceRecorder`**。
> 接了的只有两个真实入口：[`cli.py:285`](file:///workspace/ai-coding/ai_coding/cli.py#L285)（CLI/REPL）与
> `server/world.py:246`（TUI server）。所以 `long-r21c` 没有产生任何 trace 目录——"读运行日志"
> 这个要求目前在 harness 上**做不到**。批次 0 的第一件事就是把这条线接上（约 10 行）。

### §3.3 单项脚本模板（每个功能都走一遍）

1. 新建工作区与配置；启动代理（`python scripts/recording_proxy.py --out /tmp/record/<scenario>.jsonl`）；
   确认 `pgrep -f scripts/recording_proxy.py`。
2. 起 tmux，`cd <workspace>` 后启动 TUI，等到首帧。
3. 发**自然语言**指令（不是关键命令），让模型自己选工具；记录是否触发目标能力。
4. 同一指令**再跑一次**（新会话），判定"可触发"而非"碰巧"。
5. 取帧 A；取 trace B（`<ws>/.ai-coding/traces/...`）；从 C 里抠出目标请求体片段。
6. 对齐 A/B/C：trace 是权威顺序（每个迭代一条 `ContextAssembled`），代理行按**场景内序号**
   与迭代一一对应；对不上就记"未对齐"，不要猜。
7. 填一行矩阵：能力 / 触发指令 / 是否触发 / 参数是否合理 / 结果 / 等级。
8. 失败时：先判「模型没想到用」还是「用了但坏了」，分两类记（前者是提示词问题，后者是代码问题）。

### §3.4 场景清单（8 个，覆盖 §2 全部）

| 场景 | 覆盖 | 入口 | 一句话指令 |
|---|---|---|---|
| S1 面板巡检 | 22 条调色板命令 + 15 个对话框 | TUI | 逐条按、逐个截帧、esc 关闭 |
| S2 长任务主闭环 | 长任务能力（读/写/bash/todo） | **harness**（TUI 跑不完，见 §3.1） | harness 自带的 TaskFlow 任务 |
| S3 计划模式全链路 | plan 进入→评审→批准→执行→收尾 | TUI | 「/plan 给这个仓库加一个功能」 |
| S4 goal 链路 | start/update_goal、自动续轮 | TUI（手打 `/goal`） | 「/goal 把测试覆盖率提到 80%」 |
| S5 子智能体与后台 | `subAgent` 前台/后台、`subAgentList`、`job_*`、`/tasks` | TUI | 「并行调研三个模块，用子代理」 |
| S6 压缩/记忆/undo/export | compact、checkpoint、history_search、`.memory`、`/undo` `/redo`、`/export` | TUI | 「先读十个文件，然后压缩，再问早期细节」 |
| S7 MCP/技能/钩子/沙箱 | mcp_call、skill、skill-pending、hook、沙箱拒绝、ask_user | TUI | 「用 MCP 工具查一下，再用技能包」 |
| S8 上下文解剖 | §4 的全部块 | 只读 | 复用 S2/S3 的流量，不额外跑 |

S1 用旧办法（tmux + 截图）；S2 用 harness（2 次：探路 + 判定）；S3–S7 各 2 次（TUI）；
S8 只读不跑。总计约 13 次真机运行。

### §3.5 数据面清单（"梳理数据"）

| 数据 | 位置 | 内容 | 本轮用途 |
|---|---|---|---|
| 会话 | `<ws>/sessions/<id>.json` | messages、task、read_ledger、step_deliveries、agent_history | 判定状态变化 |
| 待批姿态 | `sessions/pending-posture/<id>.json` | 排队中的 posture 翻转 | 验证排队路径 |
| 写前日志 | `sessions/write-ahead/` | 崩溃恢复的调用记录 | 验证 resume |
| 摘要 | `<ws>/transcripts/summary_<id>.txt`（+`archive/`） | 每回合的人读摘要 | 快速复盘 |
| 记忆 | `<ws>/.memory` | 笔记 JSON（pin/decision/fact） | §4 H1 |
| 链路 | `<ws>/.ai-coding/traces/<session>/<turn>/…` | 每迭代事件（含 `ContextAssembled`） | §4 主证据 |
| 后台日志 | `<ws>/.ai-coding/jobs/<job>.log` | 子进程 stdout/stderr | §5 后台任务 |
| 导出 | `<ws>/.ai-coding/exports/` | html / otlp | 验证 `/export` |
| 原始流量 | `/tmp/record/<scenario>.jsonl` | 完整请求体与响应 | §4 唯一全量真相 |

**注意**：`tracing.dir` 是相对**进程 cwd** 解析的（`config.record.yaml` 写的是 `.ai-coding/traces`），
所以从仓库根启动时 trace 落在仓库、从工作区启动时落在工作区。本轮统一固定 cwd 并写进脚本头。

---

## §4 上下文有效性审查（本轮重点）

### §4.1 一次请求里到底有什么（已核对）

系统提示（[`prompt.py:128-174`](file:///workspace/ai-coding/ai_coding/ai/prompt.py#L128-L174)）：
`BASE_INSTRUCTIONS` + `<system-reminder>Project instructions` + `Agent instructions` +
methods 表（按可用工具剪枝）+ `<plan-mode>`（仅计划模式）+ `<skills>` 目录。

历史（[`request_assembly.py:204-251`](file:///workspace/ai-coding/ai_coding/core/request_assembly.py#L204-L251)）：
压缩后的 messages + plan reminder（`reconcile`）。

尾部 12 个易变块（[`request_assembly.py:347-365`](file:///workspace/ai-coding/ai_coding/core/request_assembly.py#L347-L365)），
顺序即优先级。**只有非空的块才会出现**（`request_builder.py:43-51`），所以一次普通请求通常只有 2–8 块；
逐迭代表必须处理"该块为空"。

| # | 块 | 内容 | 上限/来源 |
|---|---|---|---|
| 1 | `environment` | 工作区、git 分支、本地时间、越界路径提醒、压缩提醒 | 无显式上限 |
| 2 | `memory` | `<memory-index>`：pinned + decision + 约束型 fact | **600 字**（`index_max_chars`），`scope=None`（跨会话） |
| 3 | `read_ledger` | 本会话读过哪些行/哪些是缺口 | **900 字 / 8 文件** |
| 4 | `mcp_catalog` | 远程工具目录 | 由 MCP manager 生成 |
| 5 | `task` | 当前任务/计划注入（`inject_task`） | 由 turn_policy |
| 6 | `recommended_skills` | 路由推荐 + 人类点名的技能正文 | 路由结果 |
| 7 | `referenced_files` | `@path` 的文件内容 | `_FILE_CHARS` |
| 8 | `error_recovery` | 同类错误的历史解法 | 路由缓存 |
| 9 | `anchor` | 压缩前锚点（约束） | 压缩后才出现 |
| 10 | `hook` | 用户钩子注入 | 钩子 |
| 11 | `digest` | 工作记忆：本回合各步摘要 | **1200 字**（`working_memory_chars`） |
| 12 | `budget` | 预算压力提示 | 阈值触发 |

仪器已经存在：`ContextAssembled` 事件带**每个块的字符数**、历史消息数与字符数、
`estimated_tokens`、`prefix_shared_ratio`、`digest`（[`turn_report.py:121-152`](file:///workspace/ai-coding/ai_coding/core/turn_report.py#L121-L152)）。
缺的只是 harness 没接 tracer（§3.2）。

### §4.2 三把尺子（不靠感觉）

1. **体积**：每块 chars 与本轮 `estimated_tokens` 的占比；12 块合计占比是否失控（例如 48k 上下文里
   tail 占 8k 就是 1/6 的预算）。
2. **命中**：答案/工具参数里是否出现该块**独有**的信息——具体值（路径、行号、约束词「必须/禁止」、
   时间、预算数字、记忆里的事实）。命中率 = 被引用块数 / 该轮出现的块数。
3. **缓存**：`prefix_shared_ratio` 是否被尾部抖动拖低；若同一回合内多次迭代的 ratio 持续偏低，
   说明有块在无谓地变（H5）。

补充手段：**消融**（同一任务跑两次，第二次关掉某块或压低上限），比较工具成功率与答案质量。
样本小，只作定性。**可消融的只有有配置开关的块**：`memory`（`index_max_chars` / 关闭 memory）、
`digest`（`working_memory_chars`）、`budget`（阈值）；`read_ledger` / `environment` /
`recommended_skills` **没有开关**，只能靠"改后用同一任务复跑"对比，或先记录不改。

### §4.3 待验证/待否证的假设（每条都要落到具体数字）

| # | 假设 | 怎么验 | 若成立的动作（候选） |
|---|---|---|---|
| H1 | memory 600 字、`scope=None`，旧会话的 decision 会挤掉本轮约束 | 造两个会话，A 写 5 条 decision，B 提新约束；看 B 的 `<memory-index>` | 按 scope/时间过滤；本轮约束优先 |
| H2 | read_ledger 900 字/8 文件，长会话变成"路径墙" | 读 20 个文件后看该块字符占比与命中 | 只列缺口与最近命中；折叠未用路径 |
| H3 | `<anchor>` + `digest` + `<plan-mode>` + plan reminder 说同一件事 | 同一请求里四块都出现时逐字比对 | 合并或删其一 |
| H4 | recommended_skills 经常为空或乱推 | 统计 S2–S7 每轮是否有该块、被调用次数 | 路由改成"有把握才推" |
| H5 | `environment` 的时间行让 tail 每轮都不同 | 同一回合两次迭代的组件 diff | 时间只到分钟，或移出请求 |
| H6 | `budget` 压力提示触发得太晚 | 记录首次出现时的 tokens/上下文比 | 调阈值 |
| H7 | `task` 块与 `todo_write` 的返回重复 | 同一请求里比对 | 二者留一 |
| H8 | 工具被 surface 剪枝后，模型不知道该能力存在（如 plan 回合没有 goal 工具） | S3/S4 里模型是否尝试调用不存在工具 | 在 methods 段给一行"本回合不可用" |
| H9 | `digest` 1200 字上限会截断关键结论 | 找一条被截断的摘要 | 关键结论单独占一行 |

### §4.4 产物

`docs/r22-context-audit.md`：
- 一张**逐迭代表**（iteration × 12 块 × chars / 命中 / 备注），数据来自 trace + 请求体；
- 每个块一段裁定：**保留 / 合并 / 删 / 降级 / 改选择规则**，附证据行号；
- 一份"改前/改后"对比预案（只列候选，不在本轮实施，除非矩阵显示它确实挡住了功能）。

---

## §5 TUI 与 slash 交互优化清单

分两组：**已核实**（有代码或实测证据，可直接验收）与**待测**（S1 巡检时用帧判定，
不预设结论）。评审已删掉第一版里三条误判（见 §8）。

### §5.1 已核实

| # | 现象 | 证据 | 验收标准 |
|---|---|---|---|
| F1 | `/goal`、`/plan`、`/readonly` 在 slash 调色板与命令面板里都搜不到，只能靠背下来手打 | `slash.ts:42-143` 无这三项；`slash.py:22-51` 有 | 至少 `/goal` 能搜到并有参数提示（`plan`/`readonly` 若确认属"隐藏产品决定"，只在帮助里加一行"存在"即可） |
| F2 | 纯 `<text>` 对长中文不换行（word / char 两种模式都实测不生效），整行溢出视口；`<markdown>` 正常 | R21 §5.3 的 `createTestRenderer` 实测 | 200 汉字在 80 列内必须换行；帧宽不超过视口 |
| F3 | `ask_user` 卡、`skill-pending`（技能待批）卡没有任何真机证据，后者连方案第一版都漏了 | 无既有报告 | S7 里各触发一次并截帧；两种情况都要有 esc 出口 |

### §5.2 待测（S1 逐项填）

- **每个命令的 6 问**：① 有入口吗 ② 有参数提示吗 ③ 有进行中态吗 ④ 成功反馈是人话吗
  ⑤ 失败反馈是人话吗 ⑥ 长内容能滚动/退出吗。**手打的那 7 条**（`/model /resume /agents /goal
  /plan /readonly /clear`）也要逐条填，不能只测调色板里的 22 条。
- 状态栏在 **80 列**下的截断情况（`status-bar.tsx:55-72` 现有 5 段：agent/location/token/提示/版本，
  用 `stringWidth` 量是否溢出）。
- 15 个对话框的空态与错误态是否都有文案（`dialog-*.tsx` 逐个看）。
- `/trace` `/stats` `/config` `/budget` 的长输出在转录里是否只是 notice 行、会不会被
  `MAX_TRANSCRIPT_MESSAGES` 淘汰（`replay.ts:259-260`）。

> 已核实**不是**缺陷、不要浪费时间的三条：`/export` 已经打印绝对路径
> （`commands.py:1300`）；`/tasks` 的停止入口是 `/tasks stop <id>`（`dialog-jobs.tsx:113`）；
> 对话框按键表只有 `REVIEW_KEY_BINDINGS` 一处来源（`app.tsx` 注册的是命令，不是按键）。

---

## §6 实施批次与门禁

| 批次 | 内容 | 依赖 | 门禁 |
|---|---|---|---|
| **0** | 证据基建，三件事：(a) `scripts/eval_fullstack_task.py` 接 `TraceRecorder`（复用 `cli.py:_make_recorder` 的构造，`capture: full`）；(b) `config.record.yaml` 显式写 `tracing.capture: full` 与绝对 `dir`；(c) 新脚本 `scripts/r22_matrix.py`：入参 `<scenario> <ws>`，输出固定列 `request# / iteration / estimated_tokens / system_chars / tools_chars / history_chars / block:memory / block:read_ledger / … / tool_calls / errors`，代理行按序对齐，对不上写 `unaligned` | 无 | 在 `long-r21c` 的 workspace 上跑通并产出至少 1 行真实数据；列名固定 |
| 1 | S1 面板巡检（22 命令 + 7 条手打 + 15 对话框），只记录不修 | 0 | `docs/r22-feature-matrix.md` 第一版，每条命令 6 问填满 |
| 2 | S2–S7 真机跑（每项 2 次），填满矩阵 | 0 | 每个功能 L0–L4 有据 |
| 3 | §4 上下文解剖（复用 2 的流量） | 0, 2 | `docs/r22-context-audit.md` + 每块裁定 |
| 4 | 按矩阵结果修「不可用/不可触发」项（最多 5 个，逐项先写方案再实现） | 2, 3 | 每项有真机复验 |
| 5 | TUI/slash 优化（F1–F3 + §5.2 里被帧确认的项） | 1 | 帧对比 + 前端测试 |
| 6 | 回归 + 代码评审 + 收尾报告 | 4, 5 | 全量 pytest/ruff/mypy/bun/tsc + 变异全杀 |

**流程铁律**：每一批先出方案（哪怕 10 行），评审，再实现；实现完做一次代码评审（子智能体），
评审意见逐条并入后再进下一批。批次 4 里每个修复都要先复现（红），再修（绿），再变异验证。

### §6.1 批次 0 已完成（真机验证过的门禁）

- harness 接上 recorder 后，`r22-b0` 一轮真机**第一次**产出了 `turn-1.jsonl`（177 行）与
  `turn-1.full.json`；`scripts/r22_matrix.py` 在它与代理日志上产出 **26 行、26 行 aligned**。
- 结构自检（`wire_messages == 1 + history_messages + 非空块数`）在 26/26 行成立，
  并且抓出了 1 条**辅助调用**（记忆抽取，无 `tools`）——按位置对齐的老做法会把它和下一行错配。
- 首轮实测数字（1 轮 26 次迭代）：
  - `tools_chars` **恒为 16375**，`system_chars` 1128，`tail_chars` 776→1277；
  - `history_chars` 603→73935，`estimated_tokens` 4856→30998；
  - **`prompt_tokens_actual` 与估算差 ~10%**（30998 vs 34728）——§4 要用真实值，不用估算值；
  - `prefix_ratio` 从 0.903 升到 0.960，tail 抖动没有破坏缓存。
- 已知遗留：`capture: full` 只保留工具参数/正文，**tail 块文本仍要从代理请求体取**
  （`ContextAssembled` 只有计数）；trace 触 `max_turn_bytes` 会停写，脚本现在会警告。

### §6.2 批次 1 已完成（S1 面板巡检，只记录不修）

产物：`docs/r22-feature-matrix.md`（22 条调色板命令 + 7 条手打 + 15 个对话框，每格 6 问 + 判定 + 帧证据）。

- **口径结果**：调色板 22 条 **19×L2 + 3×L4**；手打 7 条里 **5 条默认路径被建议条劫持**（A-2）。
- **最严重的发现 A-2**：`filterSlashCommands` 用 `threshold: 0`，任何 query 都有命中；建议条可见时
  回车绑在 `autocomplete.select` 上，于是 `/readonly`→`/export`、`/plan`→权限对话框、
  `/agents`→`/tasks`、`/clear`→权限对话框、`/resume`→`/export`。`/goal`、`/model` 只是碰巧可用。
- **L4 实证**：`/review` 产出可用的 markdown 表格评审；plan 模式端到端（`presentPlan` → Plan review 卡 →
  逐步执行 + 每步 `Verify:` 真跑 → `# Todos 3/3`），磁盘上的 `src/mathx.py` 真被改写；
  `edit` 的 `Always allow` 把 `edit:<path>@build`（ttl 86400）写进 `/root/.config/ai-coding/grants.json`。
- **环境处置**：本沙箱 `bwrap: Creating new namespace failed`，`enforce` 下每次 bash 等 ~32s 并报
  `error: denied`；S1 改用 `process_sandbox.mode: "off"`（eval-only，产品默认不变），错误码问题记为 A-4。
- **供批次 3 复用**：S1 已留 2 个会话 5 个 turn 的 trace（`43f3ab1a` 2 个、`957ab4e2` 3 个）与 18 条代理请求
  （`/tmp/record/r22-s1.jsonl`），不必重跑。
- 异常清单 A-1…A-10 + 观察项 U-1（一次 `Working…` 卡死，未复现，待批次 2 用日志定性）。

**本轮不做的**：不发布、不改默认值（压缩阈值/记忆上限只在 §4 里给候选）、不引入新依赖、
不重写 TUI 框架。

---

## §7 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| 真机跑得太多，token/时间超支 | 每项 2 次 × 7 场景 | 合并场景、复用 S2/S3 的流量做 S8 |
| 模型不按指令触发（比如就是不用子代理） | 判成"不可触发"但其实是提示词问题 | 记录"是否尝试过"；区分 没想到 / 用了坏 |
| 代理/trace 体积大 | `/tmp/record/llm.jsonl` 已 15MB | 每场景单独归档，跑完即分析 |
| 上下文审查结论主观 | 无法反驳"有没有用" | 必须给具体引用字符串与字符数，不给形容词 |

---

## §8 评审结论

独立子智能体逐条核对代码后报 6 条 blocker/major + 8 条 minor，**全部已并入上文**（下表"处置"列）。

| # | 严重度 | 意见 | 处置 |
|---|---|---|---|
| 1 | blocker | 代理日志（`scripts/recording_proxy.py`）只有 `path/request/status/events`，**没有 session/turn**，与 trace 无法天然 join；批次 0 的"汇成矩阵行"没定义输入/输出 | 已改：§3.1 改为用仓库自带代理 + 每场景独立 `--out`；§3.3 步骤 6 规定按"场景内序号 ↔ 第 n 个 `ContextAssembled`"对齐、对不上写 `unaligned`；§6 批次 0 固定列名 |
| 2 | major | S2 说走 TUI，但 harness 才把 `max_tool_iterations` 提到 30、`turn_timeout` 1800、`max_auto_continuations` 4；TUI 用 10/600 跑不完长任务 | 已改：§3.1 写明这三个数字与出处；§3.4 S2 改回 harness，TUI 场景限 12 轮/600s |
| 3 | major | `/models`、`/sessions`、`/new` 就是 `/model`、`/resume`、`/clear` 的等效入口；`dialog.agents.show` 在命令面板；把这些判成 L0 会重复造能力 | 已改：§2.1 把四条标"别名缺口/补全缺口"，真 L0 只剩 `/goal`、`/plan`、`/readonly`；§5.1 F1 同步收窄 |
| 4 | major | F3"按键表写两遍"是误判：`app.tsx` 注册的是命令，`REVIEW_KEY_BINDINGS` 是唯一按键来源 | 已删；并写进 §5.2 末尾的"不要浪费时间"清单 |
| 5 | major | F4/F5/F6/F8 现象不成立：`/export` 已打印绝对路径（`commands.py:1300`）；`/tasks` 的停止入口是 `/tasks stop <id>`；console 输出在转录 scrollbox 内可滚动；状态栏没有 job 计数 | 已改：四条删除；§5 改为"已核实 3 条 + 待测 4 类"，并列出"不要浪费时间的三条" |
| 6 | major | §2 覆盖不全：漏 `job_list/job_output/job_kill`、`subAgentList`、`ask_user`、`finish_research`、`memory_search/memory_write/checkpoint_write` | 已改：§2.2 补到 25 个工具，并注明 `ask_user`/`present_plan` 在 harness 里因 `user_prompt=None` **根本不存在** |
| 7 | minor | "只有 cli.py 接 recorder"不准确：`server/world.py:246` 也接 | 已改：§3.2 写明两个真实入口都接了，缺的只是 harness |
| 8 | minor | 12 个 tail 块只在非空时出现，普通请求通常 2–8 块 | 已改：§4.1 补一句；§4.4 的逐迭代表要处理空值 |
| 9 | minor | `/model <name>` 的参数是**配置 key**，不是 wire name | 已改：§2.1 与示例 |
| 10 | minor | TUI 须写死 `-p config.record.yaml -w <scenario>`，否则 trace 落仓库根 | 已改：§3.1 |
| 11 | minor | 对话框不是 12 个：还有 `dialog-select`、`mcp-add`、`skill-pending` | 已改：§2.6 改为 15 个，并把 `skill-pending` 单列为 F3 |
| 12 | minor | §5.2 只覆盖 22 条面板命令，与"每个 slash 命令"不符 | 已改：§5.2 明确要求 7 条手打命令也要填 6 问 |
| 13 | minor | 消融只对 memory/digest/budget 可行 | 已改：§4.2 注明哪几块没有开关 |
| 14 | minor | "最多 N 轮"未取值 | 已改：§3.1 给出 12 轮 / 600s / `--rounds 8` |

评审同时确认**正确、不需要再查**的：25 个 canonical 命令与 22 条面板的计数；
手打已知命令走 `session.command`（`submission.ts`/`session.tsx`）；harness 未接 trace；
`tracing.dir` 按进程 cwd 解析；12 块顺序与 `request_assembly.py` 一致；
上限 600 / 900+8 / 1200 属实；`ContextAssembled` 确实带每块 chars 与 `prefix_shared_ratio`；
系统提示 6 段与 `prompt.py:128-174` 一致；`network.mode:"off"` 需要引号。

**评审后的结论**：方案可执行，批次 0 是所有后续的前提（没有 trace 就没有"模型收到了什么"的证据）。
下一步等待确认后从批次 0 开始。

### §8.1 批次 0 的代码评审（两轮，均已并入）

第一轮（实现后）报 2 blocker + 2 major：

| # | 意见 | 处置 |
|---|---|---|
| 1 | 按位置对齐会被**辅助 LLM 调用**（记忆抽取，无 `tools`）和**引擎重试**错位；单轮恰好在末尾所以没暴露 | 改为：无 `tools` 丢为辅助；重试按 trace 的 `ProviderRetried` 逐迭代消费 `1+retries` 行（不再用"body 相同"猜） |
| 2 | `rglob` 扫全部 session、按目录名字典序；`_tools_for_iteration` 不按 scope 过滤，子代理污染父行 | 改为单 session（多 session 时**拒绝猜**，要求 `--session`）+ turn 数字排序 + scope 过滤 |
| 3 | `capture: full` 拿不到 tail 文本，且 `params` 不再截断会让 trace 涨到 2MB 后**静默停写** | 修正注释（full 只保参数/正文，tail 文本从代理取）；`max_turn_bytes` 提到 8MB；矩阵检测 `truncated` 并警告 |
| 4 | 列口径不可比：`wire_messages` 含 system+tail、`wire_chars` 不含 tools、`history_chars` 不计 arguments | 加 `prompt_tokens_actual`（真实值）与 `wire_sha`（回溯到具体请求）；在文档里写清每个口径 |

第二轮（复审）报 1 blocker + 2 major，全部修掉并补了测试：

| # | 意见 | 处置 |
|---|---|---|
| 1 | 同一代理文件若被两轮复用（代理是追加写），run1 的行会与 run2 的事件错配且仍标 aligned | 新增**结构自检**：一行 wire body 必须正好 `1 + history_messages + 非空块数` 条消息，不匹配标 `count_mismatch`；多出来的标 `wire_only` |
| 2 | 截断只被"推迟"到 8MB | 保留警告；复跑时要求按场景独立 `--out`（§3.1 已写） |
| 3 | 测试把"两条相同 body 折叠"写成了期望，等于把误折叠钉死 | 改为 `ProviderRetried` 驱动，并新增"无事件的两条相同请求必须两行"用例；另补多 turn 文件、双 session、截断、辅助行夹在中间四类 |

最终：`tests/test_r22_matrix.py` **10 例**；真机 26 行 26 aligned。
