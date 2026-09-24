# 集成联调测试

本文说明如何对本项目做**真实网络联调**（连接任意 OpenAI 兼容端点）。
普通单元测试不触网；只有本文描述的两个脚本会真正发请求：

| 脚本 | 覆盖 | 断言强度 |
|---|---|---|
| `scripts/integration_smoke.py` | 工具回合 → 多轮回放 → **子智能体委派（含 ledger）** → L4 摘要 + 记忆提取 | 四步全部硬断言，任一步失败退出码非 0 |
| `scripts/sample_tasks.py` | 八个样例场景：MCP 关闭 / read / **todo 跨回合** / write+edit / **bash 正常执行** / **危险命令拒绝** / 子智能体 / **回合看门狗（R2）** | 每场景一条硬断言，输出 PASS/FAIL 汇总 |

`sample_tasks.py` 是 R1（自持循环）新增的样例集，专门覆盖单元测试只能模拟的能力：
任务清单跨回合存活、非交互模式下 WARN 级工具真的能写文件、DENY 级真的被拒。

## 前置：本地配置（含密钥，git 已忽略）

项目默认走 DeepSeek 兼容端点。复制示例并按需修改，**密钥只写进 `config.local.yaml`**：

```bash
cp config.example.yaml config.local.yaml   # 已被 .gitignore 忽略，不会入库
```

关键项（`config.local.yaml`）：

```yaml
models:
  deepseek-flash:
    name: deepseek-flash          # 2026-09 当前可用模型；也可用 deepseek-v4-pro
    base_url: https://api.deepseek.com/v1
    api_key: sk-xxxxxxxx            # 你的密钥
default_model: deepseek-flash
```

> 模型名提示：`/v1/models` 当前返回 `deepseek-flash` 与 `deepseek-v4-pro`；
> 旧别名 `deepseek-chat` 仍可用并路由到 flash。改模型只需改 `config.local.yaml`，无需动代码。

### 利用环境变量（推荐，避免把密钥写死在本地文件）

代码里的 `_chat` 直接使用配置文件中的 `api_key`。若不想在本地落盘密钥，可在上层脚本注入
`model_config` 后再构造 helper；CLI 场景最简单的是用 `config.local.yaml` 并保持 git 忽略。

## 一键冒烟

```bash
.venv/bin/python scripts/integration_smoke.py            # 默认读 config.local.yaml
.venv/bin/python scripts/integration_smoke.py --config /path/to/custom.yaml
```

脚本依次验证（全绿退出码 0，任一断言失败退出码 2）：

1. **工具回合**：用 `read` 工具读取工作区文件，断言 `assistant` 工具调用与 `role=tool`
   结果被正确持久化到会话（M3-1 的严格配对）。
2. **多轮回放**：在同一会话续聊，断言历史（含 tool 输出）回放不触发 400，模型能回忆起
   文件内容（验证 `ai/mapping.py` 的回放修复）。
3. **真实 LLM 注入**：直接用 `llm_helpers` 调用 flash，断言 L4 摘要器返回非空摘要、
   记忆提取器返回可用的长期事实。

示例输出片段：

```
model     : deepseek-flash
base_url  : https://api.deepseek.com/v1
roles=['user', 'assistant', 'tool'] tool_msgs=1
replied: 'sample.txt 文件仅包含一行文本，内容是 "hello world from workspace"。'
summary=OK len=109
notes   =['用户用 Python 写后端', '用户喜欢简洁代码']
```

## 逐项手动联调

```bash
# 简单回合（非工具）
.venv/bin/python -m ai_coding ask \
  --prompt "回复ok两字即可" --path config.local.yaml --workspace /tmp/ws

# 工具回合（触发 read 多轮自动调用）
.venv/bin/python -m ai_coding ask \
  --prompt "请用read工具读取sample.txt并告诉我内容" --path config.local.yaml --workspace /tmp/ws

# 查看配置生效值
.venv/bin/python -m ai_coding config-check --path config.local.yaml
```

## 曾暴露并已修复的集成问题（回归教训）

> 说明：下表记录的是 **M1–M6 时代**的问题。其中与 OpenAI Agents SDK 内部相关的根因
> （`raw_response_event`、`ToolCallItem`、`function_call` 输入项）在 R1 之后**已不复存在**——
> SDK 已从项目中移除，映射改为原生 `tool_calls` 形态。保留此表是为了不重复踩同类坑。

| 现象 | 根因 | 修复 |
|---|---|---|
| 工具参数 JSON（如 `{"path":...}`）混进最终文本 | `raw_response_event` 未区分 `output_text.delta` 与 `function_call_arguments.delta` | 只收集 `response.output_text.delta` |
| `tool_calls`/`tool_outputs` 恒为空，工具回合不落库 | `ToolCallItem` 属性是 `tool_name` 而非 `name`；输出项无 `name` | 用 `_item_tool_name`/`_item_call_id` + `call_id→name` 映射 |
| CLI 输出整段重复 | 边流式回显边 `typer.echo(answer)` | 去掉重复回显 |
| 回放历史触发 400 | assistant 工具调用在历史中缺失 | 修复 `ai/mapping.py`，工具调用分流为 `function_call` 输入项 |

### R1 之后新增的两条联调注意

| 现象 | 原因 | 处理 |
|---|---|---|
| 非交互 `ask` 拒绝 write/edit/bash | R1a-4 换掉 `AgentLoop` 构造后 CLI 不再传审批回调，而分发器对 WARN 是 fail-closed | 已修：组合根把 `approval=None` 解释为「非交互 → 自动批准 WARN」；回归用例 `test_build_loop_defaults_to_non_interactive_auto_approval` |
| `bash` 调用长时间不返回 / worktree 测试永久挂起 | **根因见下**（沙箱 + 两个真实设计缺陷） | R2a/R2b 已修；`tests/test_tool_bash.py`、`tests/test_worktree.py` 现在沙箱内也全绿 |

### 「工具卡住」的根因与修复（R2，2026-09-23）

**现象**：一条 `echo err >&2` 报 `timeout: command exceeded 60s limit`；`tests/test_worktree.py` 在第 3 个用例处永久挂起；MCP 服务端连 `initialize` 都不回。

**根因**：本机沙箱的 seccomp 过滤器（网络限制那条）把 asyncio 事件循环的**自管道写**一起挡掉了：

```text
$ grep -E "Seccomp|NoNewPrivs" /proc/self/status     # 沙箱内
Seccomp: 2        Seccomp_filters: 1
NoNewPrivs: 1
$ grep -E "Seccomp" /proc/self/status                # 沙箱外（真实环境）
Seccomp: 0        Seccomp_filters: 0

csock: <socket.socket fd=5, family=1 (AF_UNIX), type=1 (SOCK_STREAM)>
self-pipe send FAILED: errno=1 (EPERM) [Errno 1] Operation not permitted
```

`BaseEventLoop._write_to_self()` 写这个 socket 失败时**静默吞掉**异常，于是事件循环失去唯一的唤醒通道，
只能在定时器到期时醒。凡是「等线程 / 等信号 / 等子进程」的等待都会一直睡到下一个定时器：

| 实验 | 沙箱内 | 沙箱外 |
|---|---|---|
| `subprocess.run(["echo"])`（同步） | 6/6 通过 | — |
| `asyncio.create_subprocess_exec` + `communicate()` | **8 次挂 5 次** | **12/12 通过** |
| `asyncio.to_thread(time.sleep, 0.2)` | 3/3 超时（连线程都醒不过来） | — |
| 同上 + 50ms 心跳定时器 | 0.21s 正常返回 | — |

**它同时暴露了两个与环境无关的真实设计缺陷**：

1. `asyncio.wait_for(coro, t)` 必须**等取消完成**才抛超时；`proc.communicate()` 的取消要等管道 EOF，
   只要还有进程（含孙子进程 `sleep 30 &`）攥着管道，超时**永远抛不出来** —— 工具自己的 timeout 是纸糊的。
2. `WorktreeManager._git()` **完全没有超时**；循环层也**没有回合上限**，所以一次卡住 = CLI 永久挂起（问题 ⑦）。

**修复**：`infra/deadline.py`（硬超时 + 心跳 tick + 放弃式取消，不等待取消完成）+
`infra/subprocess_runner.py`（线程内 `communicate(timeout)`，超时 `killpg(SIGKILL)` 杀整组）+ 回合级看门狗。

### 环境矩阵：哪类验证必须在正常 OS 上做

| 验证 | 本沙箱 | 沙箱外 / CI | 命令 |
|---|---|---|---|
| 单元 + 离线集成（521 例） | ✅ | ✅ | `pytest -q`（**不排除任何文件**） |
| bash / git worktree 真实子进程 | ✅（R2b 之后） | ✅ | 同上 |
| MCP stdio 真实往返 | ❌ 子进程的 asyncio 循环同样醒不过来 | ✅ 本次实测通过 | `AI_CODING_MCP_STDIO=1 pytest tests/test_mcp_stdio.py -q` |
| 真实 LLM 联调 | ❌ **必须在正常环境跑** | ✅ 本次 8/8 通过 | `scripts/integration_smoke.py` / `scripts/sample_tasks.py` |

> 为什么真实 LLM 联调在本沙箱内**不行**：`httpx`（openai 客户端）在 anyio 后端上会调用事件循环的
> **默认线程池**（DNS/线程类操作），完成通知同样走自管道 → 丢失唤醒后，请求会一直挂在
> `select` 上，且 `asyncio.Runner.close()` 的 `shutdown_default_executor()` 也要跨线程唤醒，
> 于是**连进程退出都会卡死**（faulthandler 实测：主线程停在 `runners.py` 的 `run_until_complete`，
> 另有 `concurrent.futures.thread._worker` 空闲线程）。这不是产品缺陷，但意味着
> 「带真实 LLM 的验证」必须在正常 OS / CI 上做——本轮即如此执行（见 r2-analysis.md §12）。
> 反过来，这也解释了为什么 R2a 的心跳 tick 是必要的：它让「等线程完成」的等待能被及时唤醒。

### R2d 真连发现的两个真缺陷（只有真跑才会暴露）

1. **anyio 的 cancel scope 绑定任务**：把「连接」放进一个带超时的任务、却在调用方任务里 close，会炸
   `Attempted to exit cancel scope in a different task than it was entered in`。
   修法：每个 server 一个**会话属主任务**（`_MCPSessionOwner`），连接与关闭都在同一个任务里。
2. **`ClientSession.__aenter__` 不做握手**：R1a-6 的 factory 从没调用 `session.initialize()`，
   真服务端因此对每个请求回 `-32602 Invalid request parameters`。假 session 的单测永远看不见这一点。
   修法：factory 里显式 `await session.initialize()`，并有针对性单测 + gate 真连测试。

## 回归

### R7 验收脚本（`scripts/r7_cases.py`）

离线 11 条（无网络）：

```bash
.venv/bin/python scripts/r7_cases.py
```

覆盖：账本喂给决策 / 无话可说的回合给交接 / L2 信封可回收 / 窗口预算从新到旧 /
短正文不动 / 70 轮长史压到预算一半以下 / 真 ContextManager 落盘被清正文 /
审批超时（有界且与「用户拒绝」区分）/ 同步审批回调不再冻住事件循环 /
MCP 信任按 server 生效 / MCP 注册白名单过滤。

真实 3 条（需要 `config.local.yaml`；**只报告不判定退出码**）：

```bash
.venv/bin/python scripts/r7_cases.py --real --config config.local.yaml
```

| 案例 | 判据 |
|---|---|
| `pty_ask_user` | 真 pty 里跑 `run_repl`，脚本先申请任务、再回答模型的问题；断言**工具结果**里出现所答内容 |
| `mcp_tool_surface` | 真 stdio 起 `scripts/mcp_many_tools_server.py`（13 工具），打印 no-MCP / 全注册 / 白名单三档 `tools_chars` |
| `turn_still_works` | 用组合根跑一个真模型回合，断言有答案、有终态、`tools_chars > 0` |

另有两条各自门控的用例（默认 skip，见 `docs/r7-analysis.md` §12 Q4）：

```bash
AI_CODING_PTY=1 .venv/bin/python -m pytest tests/test_repl_ask_user_pty.py -q   # 真终端
AI_CODING_MCP_STDIO=1 .venv/bin/python -m pytest tests/test_mcp_stdio.py -q     # 真 stdio
```

`tests/test_repl_ask_user_stdin.py` 默认跑：它是 R7c 的回归用例（旧代码 `reason=eof`、新代码答对）。

### R6 验收脚本（`scripts/r6_cases.py`）

离线 9 条（无网络，退出码即结论）：

```bash
.venv/bin/python scripts/r6_cases.py
```

覆盖：摘要环有界 / 未压缩回合不注入 / 压缩回合注入且 ≤ 1200 字符 / 预算耗尽返回交接而非空串 /
连续失败只插一次 `[strategy-gate]` / 零成功提前收尾并交接 / 无人 `ask_user` / 有人 `ask_user` /
记忆证据与索引不变量。

真实 3 条（需要 `config.local.yaml`；**只报告不判定退出码**，因为 LLM 的首步选择有随机性）：

```bash
.venv/bin/python scripts/r6_cases.py --real --config config.local.yaml
```

它们复现 r6-analysis.md §1 的实验，判据是结构性事实：

- **R1**（对应 §1.3）：`max_tool_iterations=2` 的 `make test` 回合——返回值必须非空且含
  `[回合未完成`。实测：`159 chars, head='[回合未完成 · 迭代预算用尽（2 轮）]'`。
- **R2**（对应 §1.2）：压缩后的中段询问——环必须带住被压缩掉的那一轮（实测 `ring_carries_t66=True`），
  且首步 prompt < 5000（实测 4,043）。**实测两种结局都出现过**：一次模型直接据环回答且没有翻会话文件；
  另一次它仍去翻 `sessions/*.json` 并耗尽 5 轮预算。结论：环提供的是「有路可查」，
  不保证模型不去考古；更早的决策本来该由记忆（pinned note）承担。
- **R3**（对应 §2.3）：交互组合确实接了 CLI 提问通道（`CLIUserPromptCallback`）+ 等待有上限 +
  脚本化回答变成结构化 `data.answer`。

### R6c 在真 REPL 里的实测（做不到的部分也写清楚）

本机没有可交互终端，于是用三种方式真跑：

1. **管道喂 stdin 的 repl**：`ask_user` 的问题被打印出来；但 prompt_toolkit 的读取器已经把后续行
   读进自己的缓冲，工具的 `input()` 读到 EOF，于是走**降级路径**——工具返回 `no_answer`，
   模型自己写「用户未回答，先假设…」并把假设记进任务清单。**降级是优雅的，不是挂死。**
2. **`script` 造的伪终端**：问题确实被打印（`优先修哪个文件？\n> `），工具停在**等待输入**上，
   直到我 240s 的外部超时把它杀掉。也就是说：`tools.ask_user.timeout_seconds`（默认 300）
   + 15s 收割余量确实是这条等待的上限。
3. 早先的失败还暴露并修掉一个真缺陷：`ask_user` 原本落在权限门的「未知工具 = WARN」档，
   REPL 会**先弹一次「Allow ask_user?」**——一个问题两次提示，而且任何不是 `y` 的输入都会变成
   「user declined」。现在它在 ALLOW 表里（问一句不改变任何东西；答案要变成动作还得再走门）。

**结论与后续**：机制接好了、可见、有界、降级优雅；「人在真终端里敲的那一行能否传给工具」
在本环境无法验证。正确的修法是让 REPL 把它的 `PromptSession` 作为提问通道的 `input_fn`
（同一读取器），记为 R7 候选。

```bash
pytest          # 单元/集成(离线)全部通过；含 bash/worktree，无排除
ruff check .    # 0 错误
mypy ai_coding  # 0 问题

# 需要正常 OS（真实子进程）的最后一项：
AI_CODING_MCP_STDIO=1 pytest tests/test_mcp_stdio.py -q
```

网络相关执行（`agents_sdk_service.execute_turn`、`llm_helpers._chat`）不列入离线单测；
其纯解析函数已单独单测（`tests/test_agents_sdk_stream_helpers.py`、
`tests/test_llm_helpers.py`），并用本文的冒烟脚本做真实覆盖。
