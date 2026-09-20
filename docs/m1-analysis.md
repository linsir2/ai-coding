# AI-Coding — M1 阶段分析方案（AI 服务 + 主循环 + 会话持久化）

> 定位：在已推送的 M0 骨架（domain/config/infra/cli 绿色，pytest 43、ruff、mypy 全过）之上，落地第一条真实数据流——
> `用户输入 → 会话历史 → OpenAI Agents SDK 流式对话 → 结果回写会话 → 持久化`。工具(8个)、权限、上下文压缩、MCP、UI 留到 M2+。
> 以**已在本机实际核验**的 SDK `0.22.3` 真实源码/签名为唯一事实来源，不臆造。
>
> **核验修正（2026 实施时确认）**：SDK 0.22.3 的**顶层导入包名为 `agents`**（`from agents import Runner, Agent`），并非 `openai.agents`。流式事件为 `stream_events.py` 定义的三种判别联合：
> `RawResponsesStreamEvent(type="raw_response_event", data=TResponseStreamEvent)` / `RunItemStreamEvent(type="run_item_stream_event", name="message_output_created", item=MessageOutputItem)` / `AgentUpdatedStreamEvent`。
> 文本增量走 `event.data`（`ResponseTextDeltaEvent.delta`）；最终文本用 `ItemHelpers.text_message_output` 从 `RunItemStreamEvent.item` 提取。
> `Runner.run_streamed(starting_agent, input, ...)` 的 `input` 接受 `str | list[TResponseInputItem] | RunState`。

---

## 1. 现在要实现的内容（M1 组成）

| 件 | 说明 | 归属里程碑 |
|---|---|---|
| AI 抽象层 + OpenAI Agents SDK 适配器 | `AIService` 端口 + `AgentsSDKChatService` 适配器 + 输入映射 | M1-1 |
| 会话持久化 | `SessionService`（`sessions/{id}.json` 原子写入） | M1-2 |
| 主循环 | `AgentLoop`：一轮「取历史→跑 AI→收集增量→回写→保存」 | M1-3 |
| CLI 挂接 | `ai-coding ask --prompt "..." [--session id]` 单轮入口 | M1-4 |

## 2. 对代码的影响 / 执行范围

- **只增不改既有 M0 文件**（`domain/session.py`、`config/`、`infra/`、`cli.py` 除新增命令外不改）。
- 新增包：`ai_coding/ai/`、`ai_coding/service/`、`ai_coding/core/`；新增文件 `ai_coding/domain/run.py`、`ai_coding/infra/time_utils.py`。
- **引出 SDK 依赖首个落地**：`openai-agents`（已装在 `.venv`）只在 `ai_coding/ai/agents_sdk_service.py` 一个文件被 import，其余代码对 SDK 无知——保证「SDK 只是可替换引擎」的解耦。
- **离线可测原则**：所有 TDD 用例不触发网络。真实 SDK 适配器的非网络部分（构造 Agent、输入映射、delta 拼接）单测覆盖；编排（AgentLoop）用 `FakeAIService` 全量覆盖；需要网络的 `execute_turn` 装配路径以「工厂 + 无模型报错」间接覆盖，不 mock Runner。

## 3. 目前设计（如何契合已是定稿的改造方案）

- 数据契约沿用 M0 `ChatMessage`/`ToolCallRef`；`TurnResult` 携带 `text` + `tool_calls`（预留 M2）。
- 会话沿用 M0 `SessionData`，`SessionService` 用其 `to_dict/from_dict` 保证与 Java `sessions/*.json` 双向兼容。
- Hook/权限/四层压缩在 M2/M3 插入；本 M 的 `AgentLoop` 是先导骨架，回调位（delta 处理器）已留出，M2 挂 Hook。

## 4. 逐文件「新增功能 + 实现步骤」

### 4.1 `ai_coding/domain/run.py`
- `TurnResult(text: str | None, tool_calls: list[ToolCallRef])`：单轮成果（预留工具）。
- 纯函数 `assistant_message(turn, timestamp) -> ChatMessage`：把 TurnResult 包成 `role="assistant"` 消息（`tool_calls` 非空时 `content=None`）。
- 纯函数 `user_message(content, timestamp) -> ChatMessage`。

### 4.2 `ai_coding/infra/time_utils.py`
- `now_utc() -> str`：UTC ISO-8601（`Z`）。供 user/assistant 消息打时间戳、会话 created/lastAccess。

### 4.3 `ai_coding/ai/base.py`
- `class AIService(ABC)`：`async execute_turn(self, history: list[ChatMessage], user_input: str, token_sink: Callable[[str],None]|None=None) -> TurnResult`；抽象，供 AgentLoop / 未来 Hook 调用。

### 4.4 `ai_coding/ai/mapping.py`（纯函数，可单测）
- `chat_message_to_input(msg: ChatMessage) -> dict`：领域消息 → SDK 输入消息 dict（user/assistant/tool 三种；tool 带 `tool_call_id`）。
- `history_to_inputs(history) -> list[dict]`。

### 4.5 `ai_coding/ai/agents_sdk_service.py`（唯一 import openai-agents 处）
- `AgentsSDKChatService(AIService)`：持有 `ModelConfig`。
- `_build_agent()`：`AsyncOpenAI(base_url=model.base_url, api_key=model.api_key, timeout=model.timeout, max_retries=0)`；`OpenAIChatCompletionsModel(model=model.name, openai_client=client)`；`Agent(name=model.name, instructions=<默认指令>, model=<该Model>)`。**构造不触网**，可单测。
- `async execute_turn(history, user_input, token_sink)`：`Runner.run_streamed(agent, input=history_to_inputs(history)+[user_input])`；遍历 `stream_events()`：`type=="raw_response_event"` 且 `isinstance(data, ResponseTextDeltaEvent)` → 送 `token_sink` + 追加 `data.delta`；`type=="run_item_stream_event"` 且 `item.type=="message_output_item"` → 记录完整文本（M2 接工具事件）。汇总为 `TurnResult`。**需网络，不在单测网络路径**。
- `_instructions()` 常量：与后 M 一致的系统提示头。

### 4.6 `ai_coding/ai/factory.py`
- `build_service_from_config(app_config: AppConfig) -> AIService`：取 `default_model`；缺 → 抛 `ValueError("no default model configured")`；存在则在 `models` 中取 `ModelConfig` 构造 `AgentsSDKChatService`。参数收窄、可单测。

### 4.7 `ai_coding/service/session_service.py`
- `SessionService(root: Path = Path("sessions"))`。
- `create(title="")->SessionData`（`session_id=uuid4.hex`、时间戳）。
- `save(session)`：`to_dict`→`json_utils.to_json_file` 写 `{root}/{id}.json`；先写 `*.tmp` 再 `os.replace`（原子）。
- `load(id)->SessionData|None`；`list_sessions()->list[SessionData]`（无消息摘要）；`delete(id)->bool`。
- 全离线，用 `tmp_path` 单测。

### 4.8 `ai_coding/core/agent_loop.py`
- `AgentLoop(ai: AIService, sessions: SessionService)`。
- `async process_input(session, user_input, on_delta=None) -> str`：
  1. `ts=now_utc()`；`session.add_message(user_message(user_input, ts))`
  2. `turn=await ai.execute_turn(session.messages, user_input, token_sink=on_delta)`
  3. `session.add_message(assistant_message(turn, now_utc()))`
  4. `session.ensure_tool_pairing()`；`sessions.save(session)`
  5. 返回 `turn.text or ""`。
- 用 `FakeAIService` 单测：消息顺序、持久化、delta 回调、历史透传。

### 4.9 `ai_coding/cli.py`（扩展）
- 新增命令 `ask`：`--prompt`(必填)、`--session id`、`--path config`。流程：`ConfigManager().initialize(cfg)` → `build_service_from_config`（无默认模型→stderr 报错退出码 1）→ `SessionService()` → `AgentLoop.process_input(...)` → 打印最终文本。
- 单测覆盖「无默认模型报错退出码 1」（离线）；网络路径文档标注不单测。

## 5. 数据流（M1 全线）

```
用户 ask --prompt
 └─ ConfigManager(load) → AppConfig
     └─ build_service_from_config → AgentsSDKChatService(model)
SessionService(load/create) ── SessionData(id,title,msgs)
AgentLoop.process_input
   ├─ user msg(CHAT) 加入 session
   ├─ ai.execute_turn(history, input)
   │    └─ Runner.run_streamed(Agent, input=list[dict])   [SDK]
   │         └─ stream_events(): raw delta → token_sink + 拼接
   │              └─ TurnResult(text, tool_calls=[])
   ├─ assistant msg 加入 session
   ├─ ensure_tool_pairing + sessions.save   → sessions/{id}.json (原子)
   └─ return text → 打印
```

- 关键不变量：**每条消息带时间戳；每轮 user→assistant 各 append 一次；回合结束必落盘**；历史以 `ChatMessage` 领域对象贯穿，仅在最外层适配器转成 SDK 输入 dict。

## 6. TDD 用例（验收锚点，先写后实现）

1. `test_domain_run.py`
   - `test_turn_result_default_empty_tool_calls` / `test_user_message_fields` / `test_assistant_message_text` / `test_assistant_message_none_content_when_tool_calls` / `test_assistant_message_timestamp`.
2. `test_time_utils.py` — `test_now_utc_isoformat_ends_z`.
3. `test_ai_mapping.py`
   - `test_user_to_dict` / `test_assistant_to_dict_content` / `test_tool_to_dict_with_tool_call_id` / `test_history_to_inputs_order`.
4. `test_ai_service.py`
   - `test_build_agent_name_model`（离线，断言 `Agent.name` 与 `model` 已接上）
   - `test_build_agent_uses_sdk_model_wrapper`.
5. `test_ai_factory.py`
   - `test_factory_default_model` / `test_factory_no_default_model_raises`.
6. `test_session_service.py`
   - `test_create_session_fields` / `test_save_load_roundtrip` / `test_load_missing_none` / `test_persist_creates_json_at_root` / `test_list_empty_then_nonempty` / `test_delete` / `test_generate_unique_ids`.
7. `test_agent_loop.py`（`FakeAIService`）
   - `test_process_input_appends_and_persists` / `test_returns_final_text` / `test_delta_callback_called` / `test_history_includes_user` / `test_creates_session_if_missing`.
8. `test_cli.py`（增补）
   - `test_ask_no_default_model_errors_nonzero`.

> 规则：这些是行为规格；除非用例本身确实写错，否则不得修改；红→实现→绿。预估新增约 25 用例。

## 7. 成功之后是什么样

- `ai-coding ask --prompt "hi"`：有默认模型时走真实 SDK 流式并打印回答、在 `sessions/` 落一条可读 JSON（network-only 路径，标注）。
- `pytest` 全绿（43+25）；`ruff`、`mypy` 0 错误。
- `main` 分支新增 **M1-1…M1-4** 四个提交。
- 数据流第一环打通：领域契约→SDK→回写→落盘，全程离线可测（除真实 LLM 调用）。

## 8. 自我核验

- SDK 只在 `agents_sdk_service.py` 出现，解耦成立。
- 会话 JSON 继续兼容 Java（复用 M0 `SessionData.to_dict/from_dict`）。
- `TurnResult.tool_calls` 预留 M2，避免 M2 改契约。
- 限制/风险：M1 的 `execute_turn` 真实网络路径未单测（诚实标注）；真实测试需 API key，建议你在有 key 环境用手动冒烟 `ask` 验证；如无问题我再在 M2 引入工具并用 SDK 的中断/HITL 做权限确认。

*Acknowledge：以上基于已核验的 SDK 0.22.3 流式 API；若无异议我按此 TDD 落地 M1。*