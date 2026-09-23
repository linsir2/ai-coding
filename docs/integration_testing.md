# 集成联调测试

本文说明如何对本项目做**真实网络联调**（连接任意 OpenAI 兼容端点）。
普通单元测试不触网；只有本文描述的两个脚本会真正发请求：

| 脚本 | 覆盖 | 断言强度 |
|---|---|---|
| `scripts/integration_smoke.py` | 工具回合 → 多轮回放 → **子智能体委派（含 ledger）** → L4 摘要 + 记忆提取 | 四步全部硬断言，任一步失败退出码非 0 |
| `scripts/sample_tasks.py` | 六个样例场景：MCP 关闭 / read / **todo 跨回合** / write+edit / **危险命令拒绝** / 子智能体 | 每场景一条硬断言，输出 PASS/FAIL 汇总 |

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
| `bash` 调用长时间不返回 | 工具超时依赖 `asyncio.wait_for(proc.communicate())`，而该协程的**取消**在子进程管道不 EOF 时无法完成（本沙箱即如此），因此超时抛不出来 | 环境问题；正常 Linux 下无此现象。健壮性改进（工具级看门狗）待排期 |

## 回归

```bash
pytest          # 单元/集成(离线)全部通过
ruff check .    # 0 错误
mypy ai_coding  # 0 问题
```

网络相关执行（`agents_sdk_service.execute_turn`、`llm_helpers._chat`）不列入离线单测；
其纯解析函数已单独单测（`tests/test_agents_sdk_stream_helpers.py`、
`tests/test_llm_helpers.py`），并用本文的冒烟脚本做真实覆盖。
