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

## 常见问题

| 现象 | 原因与处理 |
|---|---|
| `no default model configured` | 未指定配置路径，默认加载空配置。改为 `--path config.local.yaml`（或你的配置）。 |
| `default model 'x' not found in models` | `default_model` 与 `models` 下的键不一致，检查 `config.local.yaml`。 |
| write/edit/bash 在 `ask` 中被放行、在 `repl` 中弹确认 | 设计如此：非交互自动批准 WARN，交互环境弹 y/N/s。危险 bash（如 `sudo`、`rm -rf /`）任何模式都被硬拒。 |
| 写文件被阻止（`NEVER_READ` / `STALE`） | 过期读保护：先让模型 `read` 该文件再写，或目标文件被外部改动。 |
| 文件工具路径报 `outside workspace` | 路径越出 `--workspace`，沙箱拦截。 |
| bash 非零退出但仍返回失败 | 命令返回码非 0 即 `success=false`，`output` 含 stdout+stderr。