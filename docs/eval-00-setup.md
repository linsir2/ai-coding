# eval-00 · 环境准备与真机冒烟基线

> 评估目标：把 `linsir2/ai-coding` 在真机上跑起来，产出可对比的真实数据。
> 本文记录环境、配置形态与首次真机冒烟结果。**不含任何明文密钥。**

## 1. 环境

| 项 | 值 |
|---|---|
| 操作系统 | Linux (Ubuntu 24.04, x86_64) |
| 沙箱限制 | `Seccomp: 0` / `NoNewPrivs: 0` —— **不在**项目文档警告的受限沙箱内 |
| Python | 3.14.8（venv 于 `/workspace/ai-coding/.venv`） |
| Node / Bun | Bun 1.4.4（`~/.bun/bin`）+ mise 管理的 1.2.14 |
| tmux | 3.4 |
| git 身份 | `linsir2` / `linsir365@qq.com` |
| 仓库 | `/workspace/ai-coding`，HEAD `d1da11a`（`refactor(ui): fold redundant PLAN suffix for self-named plan agent`） |

### 网络可达性

```
github.com                 -> 200
dashscope.aliyuncs.com     -> 404（可达）
api.deepseek.com           -> 401（可达）
```

## 2. 安装步骤（已执行）

```bash
git clone --depth 1 https://github.com/linsir2/ai-coding.git /workspace/ai-coding
apt-get install -y tmux
curl -fsSL https://bun.sh/install | bash
cd /workspace/ai-coding
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

`pip install` 关键依赖：`openai 2.54.0`、`mcp 1.30.0`、`pydantic 2.13.4`、`typer 0.24.1`、
`rich 15.0.0`、`tree-sitter 0.25.2` + `tree-sitter-bash 0.25.0`、`python-lsp-jsonrpc 1.1.2`、
`httpx 0.28.1`、`pathspec 1.1.1`；dev：`pytest 9.1.1`、`ruff 0.17.0`、`mypy 1.19.1`。

## 3. 配置形态

配置文件 `config.local.yaml`（已在 `.gitignore` 中，**不入库**）：

```yaml
models:
  qwen:
    name: qwen3.7-flash
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key: sk-****            # 已打码，真实值只存在于本地文件
    streaming: true
    max_tokens: 4096
    temperature: 0.7
default_model: qwen
# 其余 ai.* / memory.* / tools.* / mcp.* 与 config.example.yaml 一致
```

> ⚠️ **安全提醒**：该 key 曾以明文出现在对话中，建议评估结束后**轮换**。

## 4. 冒烟结果

### 4.1 CLI

```
$ ai-coding --version
ai-coding 0.1.0

$ ai-coding --help
Usage: ai-coding [OPTIONS] [COMMAND] [ARGS]...
  --version / --path / --workspace / --skills
```

### 4.2 真机冒烟（`scripts/integration_smoke.py`，四步硬断言全绿）

```
model     : qwen
base_url  : https://dashscope.aliyuncs.com/compatible-mode/v1
======================================================== [1] tool turn
roles=['user', 'assistant', 'tool', 'assistant'] tool_msgs=1
tool_calls=[ToolCallRef(id='call_792ab13731664b1ab6232cfa',
                        name='read',
                        arguments='{"path": "/tmp/it_ws_*/sample.txt"}')]
======================================================== [2] follow-up replay
replied(70 chars): 'sample.txt 文件中只有一行文字："hello world from workspace"（来自工作区的 hello world）。'
======================================================== [3] sub-agent delegation
replied(90 chars): '子智能体已成功完成任务！它读取了 `sample.txt` 文件并回报：该文件共有 1 行。'
subAgent tool messages=2 ledgers=1
packet preview: '<tool_output tool="subAgent">... "status": "completed", "iterations": 1,
                 "ledger_path": "/tmp/it_ws_*/transcripts/subagents/b2d52044f0a5.json" ...'
======================================================== [4] L4 summarizer + memory extractor
summary=OK len=227
notes   =[HarvestCandidate(text='用户使用 Python 进行后端开发', kind=FACT, importance=5, scene='技术栈'),
          HarvestCandidate(text='偏好简洁的代码风格', kind=PREFERENCE, importance=4, scene='代码规范')]
======================================================== SMOKE OK
```

**结论**：真机链路（模型 → 工具调用 → 落盘 → 子代理 ledger → L4 摘要 → 记忆提取）**全部跑通**。
本沙箱不受项目文档所述的 seccomp 限制，真机验证可行。

## 5. 结论与下一步

- 环境就绪，真机可用，无需降级到离线模式。
- 下一步：阶段 1 离线质量基线（`pytest` / `ruff` / `mypy` / 离线脚本）。
