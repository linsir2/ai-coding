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

> **用项目 venv 跑**（`.venv/bin/python -m pytest`）。裸 `python` 若 site-packages 里存在同名
> `tests` 常规包（本机 conda 里就有一个 Ultralytics 带来的），它会遮蔽本仓库的 `tests/` 目录，
> 4 个 `from tests.… import …` 的模块会以 `ModuleNotFoundError` 收集失败——那是解释器选错了，
> 不是仓库坏了。

## R11 新增的配置

| 配置 | 默认 | 说明 |
|---|---|---|
| `memory.extraction_enabled` | `true` | **写侧总闸**。`false` = 收割与 `memory_write` 全停，召回/注入照常（只读记忆模式）。缺段或非法值 → 放行，只有显式关掉才停 |
| `memory.extraction_extractors` | `null` | 写通道白名单：`["harvest"]` / `["memory_write"]` / 两者。`null`、缺段或非 list = 全放行。**必须与上一项同时理解**：开关是总闸，本项是分流 |
| `memory.recall_timeout_seconds` | `2.0` | 一次召回的墙钟预算（覆盖过滤、语义层、排名与未命中探针）。超预算 → **命中照返**，只跳过那次可选的 `touch` 写盘，并发一条 `Degraded(memory_recall)`；`0` 或负数关闭。出厂 2.0s 对实测 ~5ms 是虚位以待，等语义层真的变慢它才开始起作用 |

三项都只加在 `memory.*` 下，旧配置不改也能跑；`memory_search` 的新参数 `scene` 由模型按 schema
自己会用（详见[数据契约](data-contracts.md) 的 R11c 节）。

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

## R10 新增的配置与行为

| 配置 | 默认 | 说明 |
|---|---|---|
| `memory.recall_relative_band` | `0.55` | **新增**。只保留本次查询里分数达到最高分这一份额的候选。绝对阈值跨查询不可移植，这道门禁才是把 FP@5 从 0.52 压到 0.35 的那道（单独去掉它回到 0.40）。调到 `1.0` 等于关掉它 |
| `memory.recall_min_relevance` | `0.25` | **R3 时代是 `0.05`**，本轮提上来。0.05 实测等于没有门禁：新近性 0.20 + 重要性 0.15 = 0.35 已经越过 0.25 的分数阈值，于是无关但新鲜且重要的笔记照样被注入。**配置文件里显式写的 `0.05` 依然被照办**——改的是默认值，不是覆盖你配置的权力 |
| `memory.recall_threshold` | `0.25` | 未改：混合分的绝对下限 |

两道门禁各有职责、不能互相替代：`min_relevance` 回答「这条笔记**是不是在讲这个查询**」，
`relative_band` 回答「它**是不是还在候选里**」。band 卡的是**混合分**（BM25 + 新近性 + 重要性 +
类型加权），不是相关度——所以 `min_relevance=0.25` 不等于「相关就留」。

```yaml
# 想要更严的召回（更少误报、可能更少真命中）：
memory:
  recall_relative_band: 0.70
  recall_min_relevance: 0.35
# 逐条改一个数字不叫调参。改完跑：
#   .venv/bin/python scripts/retrieval_cases.py
# FP@5 涨过 0.36 或 R@3 掉到 0.80 以下，就是换亏了。
```

新增的 `memory_search` 参数（无需配置，模型按 schema 自己会用）：

| 参数 | 做什么 | 边界 |
|---|---|---|
| `scope` | 精确匹配 `note.scope` | 本仓库写出的条目一律 `workspace`，所以它**只可能返回空**；保留它是因为它已被 spec 定义，且导入/外部构造的笔记可以是别的 scope。它占了 schema 1129 字符里的 218 字符（见 [功能清单](features.md) 记的取舍） |
| `since_days` | 只看最近 N 天内**动过**的（按 `updated_time`，没更新过就按 `created_time`） | 时间基准是**调用方的钟**，生产路径取 `datetime.now(timezone.utc)`。`0` 与负数＝不限；没给索引概览时不会说「索引是空的」 |
| `include_near_misses` | 未命中时默认返回「最接近但没过门禁」的候选；置 `true` 则命中时也返回 | 最多 3 条，且**与查询没有共享任何词的候选一律丢弃**（它不接近任何东西） |

两条行为上的可见变化：

1. **过滤在排序之前**：`kind` / `evidence` / `scope` / `since_days` 先决定候选集，`k` 只花在
   候选上。此前是排完 top-k 再过滤，于是「四条 fact 压过唯一一条 decision」时
   `kind=decision, k=3` 返回**空**，而模型要的那条就在索引里。被过滤掉的候选**不算使用**
   （模型没看见它，所以不记 `hits`）。
2. **召回会写盘**：命中每条记一次 `hits` + `last_used`，因此 `consolidate` 的
   `hits >= 3 → importance + 1` 第一次真正可达。代价是命中路径每轮多一次**同步整文件写**。

验收脚本（两个都是离线、无网络、时钟钉死）：

```bash
.venv/bin/python scripts/retrieval_cases.py            # 检索基准：分类指标表 + 护栏
.venv/bin/python scripts/retrieval_cases.py --check    # 护栏门禁，不达标退出码非 0（本地门禁，CI 目前不调）
.venv/bin/python scripts/retrieval_cases.py --only-paraphrase   # 只把最难的改述类打全
.venv/bin/python scripts/r10_cases.py                   # 本轮验收 22 条
.venv/bin/python scripts/r10_cases.py --only the_decoy_still_leaks_and_says_so
```

基线（78 笔记 / 124 问法：可达 R@3 0.99 / 可达 FP@5 0.46 / paraphrase@3 0/46 / no-answer 4/10）见
[集成测试](integration_testing.md)。

## R10d：可选的语义档（默认关）

```yaml
memory:
  embeddings:
    enabled: false      # 默认关。打开需要下面的 provider
    provider: "qwen"    # 指向 models.<name>，复用它的 base_url / api_key
    model: text-embedding-v4
    dimensions: 1024    # 0 = 用端点默认
    min_cosine: 0.60    # 只作绝对门的 OR 分支，不参与排序
    cache_path: .memory/embeddings.json
    timeout_seconds: 10
```

打开之后 `memory_search` 多一路余弦打分：排序按 0.5·BM25 ⊕ 0.5·余弦融合，
绝对门变成「词面相关度 ≥ 0.25 **或** 余弦 ≥ 0.60」。关掉（或没配、端点不支持、
网络失败、缓存损坏）时检索结果与纯词面**逐字节一致**——这是硬契约，有单测钉死。

三条必须知道的：

1. **默认关是有原因的**：`deepseek` 端点调 `/embeddings` 直接 404，只有 dashscope 系可用。
   默认开＝一半配置开箱即坏。
2. **`min_cosine=0.60` 是量出来的，不是调出来的**：text-embedding-v4 上正例 min 0.44 /
   负例 max 0.66，分布重叠，没有阈值能分干净。0.60 买 paraphrase@3 6/46 只花 +0.02 FP@5。
   **换 provider 或模型必须重跑基准重标这个数**（见[集成测试](integration_testing.md)），
   否则门要么漏放噪音，要么把真正要救的也挡在外面。
3. **向量只在读时算**：从没被搜过的笔记不产生缓存条目；写路径（harvest / memory_write）
   不碰网络。用的人付，不用不付。缓存 key 含端点地址，同名模型换端点不会吃到陈旧向量。
