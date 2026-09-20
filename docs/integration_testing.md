# 集成联调测试

本文说明如何对本项目做**真实网络联调**（连接 DeepSeek 等兼容端点），覆盖 M1/M3 的核心链路：
工具调用回合、多轮历史回放、L4 上下文压缩摘要、记忆提取。普通单元测试不触网，只有这里与 `scripts/integration_smoke.py` 会真正发请求。

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

| 现象 | 根因 | 修复 |
|---|---|---|
| 工具参数 JSON（如 `{"path":...}`）混进最终文本 | `raw_response_event` 未区分 `output_text.delta` 与 `function_call_arguments.delta` | 只收集 `response.output_text.delta` |
| `tool_calls`/`tool_outputs` 恒为空，工具回合不落库 | `ToolCallItem` 属性是 `tool_name` 而非 `name`；输出项无 `name` | 用 `_item_tool_name`/`_item_call_id` + `call_id→name` 映射 |
| CLI 输出整段重复 | 边流式回显边 `typer.echo(answer)` | 去掉重复回显 |
| 回放历史触发 400 | assistant 工具调用在历史中缺失 | 修复 `ai/mapping.py`，工具调用分流为 `function_call` 输入项 |

## 回归

```bash
pytest          # 单元/集成(离线)全部通过
ruff check .    # 0 错误
mypy ai_coding  # 0 问题
```

网络相关执行（`agents_sdk_service.execute_turn`、`llm_helpers._chat`）不列入离线单测；
其纯解析函数已单独单测（`tests/test_agents_sdk_stream_helpers.py`、
`tests/test_llm_helpers.py`），并用本文的冒烟脚本做真实覆盖。