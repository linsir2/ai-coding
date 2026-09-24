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

`config.local.yaml` 在 `.gitignore` 中，不会误提交。`ask`/`repl` 通过 `--path config.local.yaml` 传入，`config-check` 同理。不传 `--path` 默认加载空配置（仅默认值，无模型），因此真实调用必须显式指定配置路径。

## 命令速查

```bash
ai-coding --version
ai-coding --help
ai-coding config-check --path config.local.yaml
ai-coding ask --prompt "如何删除多余列" --path config.local.yaml
ai-coding ask --prompt "读取 sample.txt" --path config.local.yaml --session <sid>
ai-coding repl --path config.local.yaml
```

| 命令 | 用途 |
|---|---|
| `config-check` | 打印生效配置：默认模型、各模型 base_url/max_tokens/temperature、上下文预算、启用的工具 |
| `ask` | 单回合对话（带全套工具），直接拿到最终答案 |
| `repl` | 交互式多轮回话；内置 `/help` `/status` `/clear` `/quit` |

常用参数：`--workspace/-w` 指定文件工具的工作目录（默认 `.`）；`--skills` 指定技能目录（启用 `skill` 工具）；`--session/-s` 续接指定会话。

## 端到端示例

准备一个临时工作区和一个文件：

```bash
mkdir -p /tmp/demo && printf 'hello world from workspace\n' > /tmp/demo/sample.txt
ai-coding ask --prompt "用 read 工具读取 sample.txt，然后告诉我内容" \
  --path config.local.yaml --workspace /tmp/demo
```

模型会调用 `read`，工具结果持久化，最终给出回答。同一会话续聊（多轮 + 记忆注入）：

```bash
SID=$(ls -t sessions/*.json | head -1 | xargs basename | sed 's/\.json//')
ai-coding ask --session "$SID" --prompt "sample.txt 里写了什么英文？原样引用" \
  --path config.local.yaml --workspace /tmp/demo
```

写文件验证（依赖 WARN 放行与过期读保护）：

```bash
ai-coding ask --prompt "用 write 工具创建 hello.txt，内容写：content-line-one" \
  --path config.local.yaml --workspace /tmp/demo
cat /tmp/demo/hello.txt   # content-line-one
```

交互 REPL：

```
$ ai-coding repl --path config.local.yaml --workspace /tmp/demo
ai-coding REPL — /help for commands
you> 用 write 把版本号改为 2.0
```

在 REPL 中，write/edit/bash 会触发 y/N/s 确认；`/quit` 退出。

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
2. **`repl` 里模型可以反问你**：它会打印问题（附带可选项），你输入的回答直接作为工具结果回给模型；
   `ask`（一次性、无人工）里这个工具不存在，模型只能自行决定并写明假设。
   注意：`ask_user` 现在**不在**审批门前（问一句不改变任何东西，答案要变成动作还得再走门），
   所以你不会先被问一次「Allow ask_user?」。等待上限是 `tools.ask_user.timeout_seconds`（默认 300s）。
   **R7c 修掉了脚本化场景**：`run_repl` 现在把 `ask_user` 的读取器换成 REPL 自己的
   `PromptSession`，所以「一次喂多行」的脚本也能把答案送到工具里（实测旧代码 `reason=eof`、
   新代码 `answer="改 A"`）；真终端行为不变（`tests/test_repl_ask_user_stdin.py` 默认跑，
   `tests/test_repl_ask_user_pty.py` 门控 `AI_CODING_PTY=1`）。

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
3. **`tools_chars` 可以压下来了**：`repl` 的 `/trace` 里能看到每步的工具面大小。

## 常见问题

| 现象 | 原因与处理 |
|---|---|
| `no default model configured` | 未指定配置路径，默认加载空配置。改为 `--path config.local.yaml`（或你的配置）。 |
| `default model 'x' not found in models` | `default_model` 与 `models` 下的键不一致，检查 `config.local.yaml`。 |
| write/edit/bash 在 `ask` 中被放行、在 `repl` 中弹确认 | 设计如此：非交互自动批准 WARN，交互环境弹 y/N/s。危险 bash（如 `sudo`、`rm -rf /`）任何模式都被硬拒。 |
| 写文件被阻止（`NEVER_READ` / `STALE`） | 过期读保护：先让模型 `read` 该文件再写，或目标文件被外部改动。 |
| 文件工具路径报 `outside workspace` | 路径越出 `--workspace`，沙箱拦截。 |
| bash 非零退出但仍返回失败 | 命令返回码非 0 即 `success=false`，`output` 含 stdout+stderr。
