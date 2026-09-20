# AI-Coding（ThoughtCoding Python 化）— M0 阶段分析方案

> 高级开发工程师任务定位：基于已核验的原 Java 源码（`zengxinyueooo/ThoughtCoding`）与已定稿的《ThoughtCoding_Python_改造方案》文档（存于 /workspace），对 M0 范围进行「影响 / 范围 / 设计 / 逐文件新增功能 / 数据流影响 / TDD 用例 / 成功效果」的完整拆解，并自我核验。产出后据此按 TDD 实现。

---

## 0. 总体技术路线（已定，对全项目的约束）

- **Agent 运行时用 OpenAI Agents SDK**（`openai-agents`，实测安装版本 `0.22.3`，已核验官方文档）。
- **bash 工具仅面向 Linux**（弃用原 Java 的 Windows PowerShell 分支）。
- **数据契约 / 数据流 / 功能不偏移原项目语义**，同时纠正原项目设计债。
- **每里程碑以 git 提交推送**至 `github.com/linsir2/ai-coding`（仓库已建、当前为空，直接初始化 `main`）。
- 全程 **TDD**：先写测试（`pytest`）→ 跑红 → 实现 → 跑绿 → 静态检查（`ruff` + `mypy`）→ 提交推送。

### SDK 与原架构的映射（M0 只定契约，M1 落地运行，此处先定原则防返工）

| 原 Java 要素 | OpenAI Agents SDK 对应 | 说明 |
|---|---|---|
| LangChainService 流式对话 + 原生工具调用 | `Runner.run_streamed` + `Agent(tools=[...])` | `Agent` 接管内部轮次与 tool call 配对 |
| 子Agent（隔离上下文、只回结论） | `Agent.as_tool(description=..., prompt=...)` | 顶层 Agent 把委托子代理当工具调用 |
| 权限确认框（写/编辑/bash 危险） | `needs_approval=True` + `ToolApprovalItem` 中断流 | 命中中断 → `state.approve/reject` → `Runner.run(agent, state)` 恢复 |
| 权限门 DENY（硬拒绝列表等） | `needs_approval=<async 谓词>` 返回需审批 + `rejection_message` / `on_approval` 自动拒绝 | 决策仍由 `PermissionGate` 计算，结果喂给 SDK |
| MCP（stdio 本地服务器） | `MCPServerStdio`（依赖 `mcp>=1.19.0,<3`） | 替代手写 JSON-RPC；实测安装 `mcp 2.2.0` |
| 会话持久化 | SDK session store **或** 自家领域 `SessionData` JSON | **采用自家领域层**以便继续兼容旧 Java `sessions/*.json` |
| 内置工具（8 个） | `function_tool` 包装，保留 `BaseTool` 契约 | 工具契约仍是我们的领域不变量 |

> 结论：SDK 只作为 `ai` 层的一种「可替换运行时引擎」插入；领域契约（`domain`）、编排（hooks/security/pipeline/context 压缩）、会话、记忆、UI 仍是自家实现。因此 **M0 的 domain/config/infra/CLI 骨架与 SDK 无关**，可安全先行。

---

## 1. 影响分析（M0 对代码的影响 / 执行范围）

### 1.1 影响判定
- 这是**从克隆仓库（当前为空）初始化 Python 工程**，属"绿地起始"，不覆盖既有代码，影响范围=全新文件。
- M0 对应「骨架、数据契约、配置、CLI 壳、基础设施」五件套，**不触碰 AI 运行循环**（M1 才接入 SDK）。

### 1.2 执行范围（M0 交付物）
| # | 件 | 说明 |
|---|---|---|
| 1 | 工程初始化 | `pyproject.toml`、`.gitignore`、目录骨架、`README`、`docs/` |
| 2 | IllegalArgumentException 纠正原债 | 数据契约干净化（见 §1.3） |
| 3 | `domain/` 契约层 | 纯数据、零业务依赖、可单测 |
| 4 | `config/` 配置层 | Pydantic 模型 + YAML 加载 + 默认值对齐 |
| 5 | `infra/` 基础设施 | `json_utils`、`file_utils`、`logger_setup` |
| 6 | CLI 壳 | `cli.py`：
`--version`、`--help`、`--config-check` |
| 7 | TDD 测试 | `tests/` 全绿 |
| 8 | 静态检查 | `ruff` 0 错误、`mypy` 干净 |
| 9 | git 提交 | M0-1..M0-5 逐阶段 commit + push |

### 1.3 对原项目「已知设计债」的纠正（M0 落地项）
1. `ChatMessage` 手写 Getter/Setter + 散字段 → **`@dataclass` + 类型收窄**，可选字段显式 `None` 默认。
2. `ThoughtCodingContext` 上帝容器 → M0 先立「组合根」占位（`app/__init__.py` 声明依赖边界，M1 落地装配）。
3. 死配置（`tools.*.allowedCommands`/`allowedLanguages` / `maxToolResultBytes` 运行期覆写）→ **M0 的配置模型不引入不被读取的键**；`maxToolResultBytes` 决策留到 M3 由上下文压缩消费（M0 不定义）。
4. `SessionCommand/ConfigCommand` 占位 → M0 CLI 先提供可用的 `--config-check`/`--version`；完整子命令在 M6。
5. `baseURL` → `base_url`。
6. bash 双平台 → **仅 Linux**。

---

## 2. 设计（M0 模块边界与依赖方向）

```
cli.py (typer 入口)
  └─ ai_coding/
       ├─ domain/    # 纯数据契约（dataclass），零 import 业务层
       ├─ config/    # Pydantic 模型 + loader（仅依赖 domain/外部无关）
       ├─ infra/     # json_utils / file_utils / logger_setup（仅依赖标准库）
       └─ __init__.py# 版本
```

- **`domain` 最底层**，不被任何层反向 import；是未来 `ai/core` 共用的不可变契约。
- **`config`** 仅依赖 `domain`（`ModelConfig`）与 `pydantic`/`PyYAML`。
- **`infra`** 仅依赖标准库。
- **`cli`** 依赖 `config` + `infra` + `domain`。
- 所有公共方法抛异常语义与原 Java 一致（`WorkspaceSecurityException` 等后续里程碑再引入）。

---

## 3. 逐文件「新增功能 + 实现步骤」（M0 每文件怎么做）

### 3.1 `pyproject.toml`
- 元数据、`[project.optional-dependencies].dev = pytest,ruff,mypy`、`[tool.ruff]`、`[tool.mypy]`、`[tool.pytest.ini_options]`。
- 依赖：运行时 `openai-agents, pydantic, PyYAML, typer, rich, prompt_toolkit`；`mcp>=1.19.0,<3` 由 openai-agents 传递带入。
- 入口：`[project.scripts] ai-coding = "ai_coding.cli:app"`；`python -m ai_coding`。

### 3.2 `.gitignore`
- `.venv/`、`__pycache__/`、`*.pyc`、`sessions/`、`.memory/`、`transcripts/`、`worktrees` 状态、`.ruff_cache/`、`.mypy_cache/`、`config.local.yaml`。

### 3.3 `ai_coding/__init__.py`
- `__version__ = "0.1.0"`。

### 3.4 `ai_coding/domain/__init__.py` + `message.py`
- `Role = Literal["user","assistant","tool","system"]`
- `ToolCallRef(id: str, name: str, arguments: str)`：「模型原始参数 JSON 字符串」契约，对应 Java `ToolCallRef`。
- `ToolCall(id: str, name: str, params: dict)`：`params` 由 `ToolCallRef.arguments` JSON 解析得到。
- `ToolResult(success: bool, output: str)`：`output` 为回喂模型文本。
- `ToolExecution(tool_call: ToolCall, result: ToolResult)`：配对单元。
- `ChatMessage(role, content: str|None, timestamp: str, tool_call_id: str|None=None, tool_name: str|None=None, tool_calls: list[ToolCallRef]|None=None)`：单一消息承载 4 种角色；关键不变量——`tool` 角色须带 `tool_call_id`，`assistant` 带工具调用须带 `tool_calls`。

### 3.5 `ai_coding/domain/session.py`
- `SessionData(session_id, title, created_time, last_access_time, messages: list[ChatMessage])`
- `add_message(...)`；`to_dict()` / `from_dict()`（兼容 Java `sessions/*.json` 结构）。

### 3.6 `ai_coding/domain/model_config.py`
- `ModelConfig(name, base_url, api_key, streaming=True, max_tokens=4096, temperature=0.7, top_p=0.9, timeout=60)`（纠正 `baseURL→base_url`）。

### 3.7 `ai_coding/domain/subagent.py`
- `SubagentTurn(text: str, tool_calls: list[ToolCallRef]|None=None)`：子代理单轮结论 + 悬空工具调用。
- `SubagentResult(text: str|None)`：最终结论（可空，对应 Java「未产出文本结论」）。

### 3.8 `ai_coding/config/__init__.py` + `models.py` + `loader.py`
- `ToolConfig(enabled=True, max_file_size=10485760, timeout_seconds=30)`（去掉死配置键）。
- `ToolsConfig(bash/read/write/edit/glob: ToolConfig | None)`。
- `AIConfig(auto_process_tool_results=True, max_tool_iterations=10, max_concurrent_subagents=3, subagent_worktree_isolation=True, max_context_tokens=48000, max_messages=50, snip_keep_head=3, snip_keep_tail=20, keep_recent_tool_results=3, per_result_persist_bytes=30000, l4_keep_tail=6)`。
- `MemoryConfig(enabled=True, auto_extract=True, consolidate_threshold=10, max_index_entries=200, max_per_turn_injections=5)`。
- `AppConfig(models: dict[str, ModelConfig], default_model: str, tools: ToolsConfig, ai: AIConfig, memory: MemoryConfig)`。
- `MCPServerConfig(name, command, enabled=False, args: list[str])`、`MCPConfig(enabled=False, auto_discover=True, connection_timeout=30, servers: list[MCPServerConfig])`。
- `ConfigLoader.load(path|None) -> AppConfig`：优先级 显式路径 > 类路径 `config.example.yaml` > 纯默认；YAML 键转 snake_case；未知键忽略；缺省用模型默认值。
- `ConfigManager.initialize(path|None) -> (AppConfig, MCPConfig)`：AppConfig 与 MCPConfig 分盒解耦（纠正原"一张文件两套解析"）。

### 3.9 `ai_coding/infra/json_utils.py`
- `to_json / from_json / pretty_print / is_valid_json / to_json_file / from_json_file`（镜像 `JsonUtils`）。

### 3.10 `ai_coding/infra/file_utils.py`
- `read_file / write_file / append_to_file / exists / create_directories / list_files / list_files_recursive / delete_recursive / copy / get_file_size / get_file_extension / file_name_without_extension`（镜像 `FileUtils`；UTF-8）。

### 3.11 `ai_coding/infra/logger_setup.py`
- `setup_logging(level, format)`：root logger 幂等配置，返回 configured。

### 3.12 `ai_coding/cli.py`
- `app = typer.Typer()`；`--version` → 打印 `__version__`；`--config-check [PATH]` → 加载配置，成功打印模型清单与工具开关，失败打印错误并非零退出（供 M6 增强为 ConfigCommand）。

---

## 4. 数据流影响（M0）

- M0 阶段尚无 AI/工具执行数据流；数据流影响集中在**数据契约形状**与**配置→初始化**两条线上：
  1. 契约线：`ChatMessage`（含 `tool_calls`/`tool_call_id`/`timestamp`）贯穿「模型请求 ↔ 会话持久化 ↔ 上下文压缩」——M0 定死形状，后续 M1-M3 直接复用，**不偏移原 Java JSON 结构**。
  2. 配置线：`ConfigLoader.load → AppConfig/Pydantic 校验 → CLI --config-check 输出`；为 M1 的 `AgentLoop` 装配提供一致的内存配置。
- 会话线：`SessionData.to_dict/from_dict` 保证 `sessions/{id}.json` 与 Java 旧文件**双向兼容**（M0 用单元测试锁死）。

---

## 5. 新增功能对数据流/契约的影响核验（是否有坑）

| 新增/设计决策 | 影响 | 核验 |
|---|---|---|
| dataclass 收窄可选字段 | 下游 M1-M3 判空从「null 判断」变「None 判断」 | 语义等价，OK |
| 去掉死配置键 | 配置模型更干净，不破坏任何读取方 | 原键本就无人读，OK |
| `base_url` 改名 | 仅序列化字段名变化，与 YAML 键对齐 | YAML 用 `baseURL`→读取时做兼容映射，待 M1 定 |
| M0 不接 SDK | 数据契约先定型，SDK 后接入不影响契约 | 契约独立于 SDK，OK |
| bash 仅 Linux | `BashTool` 丢弃 PowerShell 分支（M2 落地） | 符合用户明确要求 |

---

## 6. TDD 用例（M0 具体测试清单） — 先写出，作为验收锚点

`tests/`：
1. `test_domain_message.py`
   - `test_tool_call_ref_fields`：构造/字段。
   - `test_tool_call_from_ref_args_json`：`ToolCallRef("1","read",'{"path":"a"}')` → `ToolCall.params=={"path":"a"}`。
   - `test_tool_call_from_ref_bad_args_defaults_empty`：非法 JSON → `params=={}` 不抛。
   - `test_chat_message_user_defaults`：`role=user, content 必填, tool_* 默认 None`。
   - `test_chat_message_tool_requires_tool_call_id`：`role=tool` 缺 `tool_call_id` 抛 `ValueError`。
   - `test_chat_message_timestamp`：显式传入保留。
   - `test_tool_result_success_error`。
2. `test_domain_session.py`
   - `test_session_add_message_appends`。
   - `test_session_to_dict_from_dict_roundtrip`。
   - `test_session_from_dict_java_shape`：按 Java `sessions/*.json` 结构喂入能正确还原 messages。
3. `test_config_loader.py`
   - `test_load_returns_defaults_on_none`：无文件 → 默认模型/温度/超时等。
   - `test_load_from_yaml_str_overrides`：YAML 覆写 `default_model`、`ai.max_context_tokens`。
   - `test_load_unknown_keys_ignored`。
   - `test_tools_default_enabled`。
   - `test_mcp_defaults`：`MCPConfig()` 默认 `enabled=False, auto_discover=True, connection_timeout=30`。
   - `test_model_config_defaults`：`temperature=0.7, max_tokens=4096, timeout=60, top_p=0.9, streaming=True`。
   - `test_missing_file_falls_back_defaults`（`config.example.yaml` 不在时仍可加载纯默认）。
4. `test_infra_json.py`
   - `test_to_json_from_json_roundtrip` / `test_is_valid_json` / `test_pretty_print` / `test_json_file_io`。
5. `test_infra_file_utils.py`
   - `test_write_read` / `test_append` / `test_create_directories` / `test_list_files` / `test_recursive` / `test_delete_recursive` / `test_extension_helpers`（用 tmp_path）。
6. `test_cli.py`（通过 `typer.testing.CliRunner`）
   - `test_version`：`--version` 包含 `0.1.0`，退出码 0。
   - `test_config_check_valid`：给定临时 YAML，`--config-check` 退出码 0 且输出含默认模型名。
   - `test_config_check_missing_file`：给不存在路径，退出码非 0。

**规则**：这些测试是「行为规格」，实现阶段除非测试本身错误，否则不得改动；跑红→实现→跑绿。

---

## 7. 成功之后是什么样（M0 验收效果）

- `pip install -e .` 后 `ai-coding --version` → `ai-coding 0.1.0`。
- `ai-coding --config-check` → 打印模型清单与工具开关，退出码 0。
- `pytest`：全部通过（估算 18~24 个用例）。
- `ruff check .` 0 错误；`mypy ai_coding` 干净。
- 仓库 `main` 上出现 **M0-1…M0-5** 五个渐进提交，每提交可见、可回滚。
- 数据契约：`domain` 4 个模块全部 dataclass、可被 M1 直接 import；会话 JSON 与 Java 旧文件双向兼容（有测试锁死）。

---

## 8. 自我核验（方案是否有问题）

- **契约一致性**：`ChatMessage`/`ToolCallRef`/`SessionData`/`ModelConfig` 字段与原 Java 源码逐字比对过，无不必要增删；`base_url` 改名是明确纠正项。
- **SDK 对接**：M0 不依赖 SDK，规避"先锁死默认字段再发现 SDK 不兼容"的返工；M1 在 `ai` 层用 adapter 吸收 SDK。
- **依赖方向**：domain/config/infra/cli 单向，无环；可加 CI 钩子（后续里程碑）。
- **范围克制**：M0 不做 AI 循环、不做工具实现、不做 UI 交互主体——避免一次交付过大难验收；每项留到对应里程碑。
- **潜在风险**：PyYAML 对 `baseURL` 大小写与 snake_case 转换——已用"显式字段名映射 + 未知键忽略 + 单测锁死"兜底。
- **不足/待拍板**：`config.example.yaml` 的字段命名约定（Java 原文件用 camelCase `maxTokens` 等，Python 倾向 snake_case）——本次采用 snake_case 域模型 + 兼容读取，需你拍板是否接受对旧 `config.yaml` 的字段名变化（默认即可）。

---

*Acknowledge：以上基于已核验的 Java 源码与 OpenAI Agents SDK 官方文档（0.22.3），非臆造；若你认可，我即按此清单 TDD 落地 M0。*