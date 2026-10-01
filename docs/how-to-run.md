# 运行指南

面向首次使用与本地验证。bash 工具仅支持 Linux；其余功能在 Linux / macOS 均可。

## 环境要求

- Python >= 3.10
- Linux（使用 bash 工具时）
- 一个兼容 OpenAI Chat Completions 协议的模型端点与 API Key（示例默认配 DeepSeek）

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

`.[dev]` 会一并装好 `pytest`、`pytest-asyncio`、`ruff`、`mypy`。安装后 `ai-coding` 命令即用。

## 配置

复制示例配置为本地配置并填写密钥：

```bash
cp config.example.yaml config.local.yaml
# 编辑 config.local.yaml，把 deepseek-chat.api_key 换成真实 Key
```

`config.local.yaml` 在 `.gitignore` 中，不会误提交。通过 `--path config.local.yaml` 传入。
不传 `--path` 默认加载空配置（仅默认值，无模型），因此真实调用必须显式指定配置路径。

## 命令速查

R10 起只有一种调用方式：`ai-coding` 直接启动全屏 TUI。旧的 Typer 子命令
（`config-check` / `ask` / `memory` / `trace` / `repl`）已删除。

```bash
ai-coding --version
ai-coding --help
ai-coding --path config.local.yaml --workspace . --skills ./skills
```

| 参数 | 用途 |
|---|---|
| `--version` / `-V` | 打印版本号并退出 |
| `--path` / `-p` | 配置文件路径（YAML） |
| `--workspace` / `-w` | 文件工具的工作目录（默认 `.`） |
| `--skills` | 技能目录，启用 `skill` 工具 |

进入 TUI 后直接输入，`Enter` 提交（回合进行中输入栏会禁用）。slash 命令可手打完整命令
（如 `/help`），也可输入 `/` 唤起命令面板。`Ctrl-Q` 退出；`Ctrl-C` 在有选区时复制、
无选区时退出；`Shift`+拖选使用终端原生选择。完整的命令列表见
[功能清单 · slash 命令](features.md#slash-命令)。

## 端到端示例

准备一个临时工作区和一个文件，然后启动 TUI：

```bash
mkdir -p /tmp/demo && printf 'hello world from workspace\n' > /tmp/demo/sample.txt
ai-coding --path config.local.yaml --workspace /tmp/demo
```

启动后界面自上而下是状态栏、transcript、输入栏。直接描述任务即可开始，模型会调用 `read`
读文件，回复流式写进 transcript，工具调用显示为事件块。

> v1 的一条已知退化（`/review`、未知 `/foo` 等模型驱动的 slash 命令不渲染工具事件块；
> `/plan` 不在此列）详见[功能清单](features.md) 的「R12：Textual UI」节。

## 产物位置

| 路径 | 说明 |
|---|---|
| `sessions/` | 会话 JSON（`{id}.json`），相对当前目录 |
| `{workspace}/.memory` | 记忆索引单文件 |
| `{workspace}/transcripts/persisted/` | L3 持久化的超大工具输出 |
| `{workspace}/transcripts/summary_*.txt` | L4 摘要审计副本 |

## 验证与质量门禁

```bash
pytest          # 全部测试通过
ruff check .    # 静态检查 0 问题
mypy ai_coding  # 类型检查（strict）通过
```

真实网络链路可用 `scripts/integration_smoke.py` 验证（工具回合、多轮回放、L4 摘要、记忆提取）。

R6 的验收（回合摘要环 / 收尾交接 / 失败升级 / `ask_user` / 记忆证据）：

```bash
.venv/bin/python scripts/r6_cases.py                                        # 离线 9 条，退出码即结论
.venv/bin/python scripts/r6_cases.py --real --config config.local.yaml      # 追加 3 条真实案例（只报告）
```

## R6 新增配置与行为

| 配置 | 默认 | 说明 |
|---|---|---|
| `ai.working_memory_keep` | 12 | 压缩回合附带的「每轮一行摘要」条数（更早的细节请用 `memory_search` 或让记忆钉住决策） |
| `ai.working_memory_chars` | 1200 | 该块渲染上限；普通回合不注入，所以不花钱 |
| `ai.failed_streak_threshold` | 2 | 连续这么多轮「工具全军覆没」就注入一次 `[strategy-gate]`；0 关闭 |
| `ai.max_strategy_nudges` | 1 | 每回合最多几次换策略提示 |
| `ai.stall_iterations` | 3 | 这么多轮后若「尝试过调用但一次都没成功」就提前收尾并给出交接报告；0 关闭 |
| `tools.ask_user.enabled` | true | 是否允许模型主动提问（`ask` 与子智能体不会注册它，因为没有可问的人） |
| `tools.ask_user.timeout_seconds` | 300 | 等人回答的上限（实际调用预算是它 +15s，且不超过回合剩余） |

行为上的两处可见变化：

1. **未完成的回合不再返回空字符串**：轮次/token/时钟/引擎导致回合中断而模型没留下话时，
   你会看到 `[回合未完成 · …]` 的报告（目标、已完成、未完成、最近一次失败、产物、轨迹路径）。
   模型自己说了话的回合不受影响。
2. **TUI 里模型可以反问你**：问题与可选项经 `AskScreen` 模态弹出，你输入的回答直接作为工具结果回给模型；
   无人可问的入口（子智能体）不注册这个工具，模型只能自行决定并写明假设。
   注意：`ask_user` 现在**不在**审批门前（问一句不改变任何东西，答案要变成动作还得再走门），
   所以你不会先被问一次「Allow ask_user?」。等待上限是 `tools.ask_user.timeout_seconds`（默认 300s）。
   R7c 曾用「把 `ask_user` 的读取器换成 REPL 自己的 `PromptSession`」修脚本化场景；
   R12 起 `prompt_toolkit` REPL 与那两个 pty/stdin 测试文件已删除，这条通道改走 Textual 模态，
   行为由 `tests/test_pty_textual.py` 的真 pty E2E 锁定。

想关掉摘要环：把 `ai.working_memory_keep` 设为 0（或 `working_memory_chars` 设 0）；

想关掉升级阶梯：把 `ai.stall_iterations` 与 `ai.failed_streak_threshold` 设为 0。

## R7 新增配置与行为

| 配置 | 默认 | 说明 |
|---|---|---|
| `ai.l2_window_chars` | 200 | L2 被清空的工具结果各保留头/尾多少字符（R7b） |
| `ai.l2_window_budget` | 4000 | 一次压缩里所有窗口的总字符预算，从新到旧分配；用完之后只剩信封（R7b） |
| `ai.approval_timeout_seconds` | 0 | 等审批的上限；0 = 只受回合钟约束（R7c） |
| `mcp.connection_timeout` | 60 | 连不上的 server 等多久（R7d 起，旧默认 30s 撞 npx 冷启动 27s） |
| `mcp.servers[].tools` | `[]` | 只注册这些远端工具（留空 = 全部）；每步都重发 schema，这是唯一能压它的开关（R7d） |
| `mcp.servers[].auto_approve` | `false` | 该 server 的工具免审批；默认关，不配就是今天的行为（R7d） |

```yaml
mcp:
  enabled: true
  connection_timeout: 60
  servers:
    - name: everything
      command: npx
      args: ["-y", "@modelcontextprotocol/server-everything"]
      enabled: true
      tools: ["get-sum"]     # 只注册这一个（13 个全注册 = 每步多付约 9000 字符）
      auto_approve: true     # 这个 server 是你在自己的配置里声明的
```

行为上的三处可见变化：

1. **被压缩掉的工具输出可以取回**：历史里留下的是一行信封 + 头尾窗口，原文在
   `<workspace>/transcripts/elided/`（文件名是内容哈希，重复正文不会写第二份）。
2. **没人回答的审批不再拖住整回合**：结果是结构化的 `ToolFinished.error="approval_timeout"`，
   不是「用户拒绝」——模型被明确告知「自己决定并写明假设」。
3. **`tools_chars` 可以压下来了**：TUI 的 `/trace` 里能看到每步的工具面大小。

## 常见问题

| 现象 | 原因与处理 |
|---|---|
| `no default model configured` | 未指定配置路径，默认加载空配置。改为 `--path config.local.yaml`（或你的配置）。 |
| `default model 'x' not found in models` | `default_model` 与 `models` 下的键不一致，检查 `config.local.yaml`。 |
| write/edit/bash 在无人入口被放行、在 TUI 中弹确认 | 设计如此：无人可问（子智能体）自动批准 WARN，交互 TUI 弹 y/N/s。危险 bash（如 `sudo`、`rm -rf /`）任何模式都被硬拒。 |
| 写文件被阻止（`NEVER_READ` / `STALE`） | 过期读保护：先让模型 `read` 该文件再写，或目标文件被外部改动。 |
| 文件工具路径报 `outside workspace` | 路径越出 `--workspace`，沙箱拦截。 |
| bash 非零退出但仍返回失败 | 命令返回码非 0 即 `success=false`，`output` 含 stdout+stderr。


## R7e 新增配置与行为

| 配置 | 默认 | 说明 |
|---|---|---|
| `ui.steering` | true | 回合进行中显示 `steer>` 提示并接受转向；关掉＝回到今天「等它跑完」的安静 UX |
| `ai.steering_max_messages` | 5 | 一次抽干最多吸收几条（多出来的留在队列，下一轮/下一回合继续） |
| `ai.steering_max_chars` | 2000 | 单条上限，超出截断 |
| `ai.tools_chars_warn` | 12000 | 工具面超此值发一次 `[tools] …` 提示；0 关闭 |
| `ai.subagent_context_mode` | `full` | `task_only` 让子智能体用干净窗口（省每一步的上下文，但要求你把背景写进 `task`） |

**四件事怎么用**：

1. **回合中改方向**：v1 未接线——steering 队列已建好但 Textual UI 尚未接入。
   以 `/` 开头的行是命令，会留到回合结束再执行（与 Claude Code 的排队语义一致）。
2. **模型自己记一笔**：`memory_write` 工具，写一句话、选 `fact/decision/preference/open_thread`；
   重复写同一句不会报错（返回既有 id），要改旧结论就带 `supersedes`。写完 `/memory` 里能看到
   `evidence=model` 的那条。
3. **工具面超支会说一次**：`[tools] 13301 字符（阈值 12000）· everything=10062 · local=4280`，
   并提示用 `mcp.servers[].tools` 只注册需要的工具。
4. **子智能体的账单**：`ai.subagent_context_mode: task_only` 时它不再继承父回合的请求视图
   （省的是它每一步的 prompt），代价是它看不到你的对话——父回合必须把事实写进 `task`；
   工具描述会自动改成提醒这件事的措辞。

验收脚本：`.venv/bin/python scripts/r7e_cases.py`（离线 9 条）；
`--real` 再加 3 条真实用例（真 pty 的转向、真 pty 的提问、真模型一个回合；其中两条 pty 用例的 driver
已随 R10 删除，现不可跑，见 [集成测试](integration_testing.md)）。

## R8 新增的配置与行为

| 想做的事 | 怎么做 | 代价 / 边界 |
|---|---|---|
| 让模型自己写那句「这一轮该记住什么」 | 什么都不用开：`checkpoint_write` 已注册（REPL/组合根） | 它只进**本会话**的摘要环，且只在压缩回合注入；要跨会话用 `memory_write`。上限 `ai.working_memory_note_chars`（默认 120） |
| 关掉流式输出（回到「结束时一行预览」） | `ui.tool_chunks: false` | 默认开；每次调用 0.15s 最多一行 |
| 只找「确实动过手的那一轮」记下的记忆 | `memory_search(evidence="tool")` | `model` 表示「非 tool」，**包含** R6d 之前没有 evidence 字段的老数据 |
| 丢掉当前卡住的那次工具调用 | 在终端按 **Esc** | 工具结果回 `cancelled_by_user`，**回合继续**；`bash` 的进程组会被真的杀掉；这次取消不算失败、也不推进「换策略」提示。**R10 起该键位暂不可用**：按键绑定在已删除的 `ui/reader.py` 里 |
| 中止整个回合 | **Ctrl-Q**（退出应用；`Ctrl-C` 现在是「有选区复制，无选区退出」） | 回合被取消，已完成的部分照常落盘（`process_input` 捕获 `CancelledError`）；保留已完成的文本与轨迹 |
| 让 MCP 服务器的进度可见 | 服务器调 `ctx.report_progress(...)` 即可 | 不报进度的服务器什么都不会显示；`scripts/mcp_progress_server.py` 是可用样例 |

验收脚本：`.venv/bin/python scripts/r8_cases.py`（离线 13 条）；
`--real` 再加 3 条真实用例（真模型回合、真模型 + 真 bash 的流式、真模型的 checkpoint 往返）。

## R9 新增的配置与行为

| 想做的事 | 怎么做 | 代价 / 边界 |
|---|---|---|
| 让 `todo_write` 挡住「依赖没做完就开始/完成」 | 不用开：`ai.enforce_dependencies` 默认 `true` | 同时挡正向引用与自指（`add` 直接拒绝）。老清单若真有环/幽灵依赖，`update` 会返回 `blocked_by_dependencies` 并把 `open` / `unknown` / `cycle` 一起给你 |
| 关掉依赖门禁（只给历史遗留清单） | `ai.enforce_dependencies: false` | 回到 R1 行为：只渲染、不强制 |
| 一眼看完所有会话的 token 与命中率 | R10 起暂无 CLI 入口（`ai-coding trace` 已删除）；`core.aggregate.build_report(TraceStore(root), since_days=N)` 仍在库里 | `--since` 依赖「写文件会刷新目录 mtime」这一文件系统行为，`rsync` / 复制过的目录会整体漂移（与 `prune` 同病同源） |
| 把一次会话导成一个能直接打开的页面 | `core.export_html.render_session_html([read_session_view(store, sid)])` | 单文件、内联 CSS、无 JS、无外链；**所有动态串都转义**（工具输出里的 `<script>` 只会显示成文本） |
| 把轨迹喂给别的观测系统 | `core.export_otlp.rows_to_otlp(views)` | 零依赖：只写 OTLP/JSON 文件，**不发 HTTP**、不装 OTel SDK |
| 连消息全文一起导出（取证用） | 加 `--full` | 默认**不含**全文；只有 `capture: full` 的轨迹才有 `turn-<n>.full.json` 可导 |

两个诚实提醒：`mismatch`（`summary.turns` 与磁盘回合数对不上）**只标注不报错**，退出码仍是 0；
`--since` 依赖「写文件会刷新目录 mtime」这一文件系统行为，`rsync` / 复制过的目录会整体漂移
（与 `prune` 同病同源）。

验收脚本：`.venv/bin/python scripts/r9_cases.py`（离线 12 条）；
`--real` 再加 1 条：真模型跑一个回合，断言 `build_report` 把这次会话算进去了。
