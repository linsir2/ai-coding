# AI-Coding — M3 阶段分析方案（上下文压缩 + 记忆 + 项目指令 + Skill 目录）

> 定位：在 M2 已打通「8 工具 + 权限门 + SDK 自动多轮」之上，落地「长会话不 400」的价值高地。
> 组成：四层上下文压缩管线的 Python 实现 + 记忆 Store/Service + 项目指令加载 + 系统提示装配。
> 依据：`ThoughtCoding_Python_改造方案.md` §3.3/§5/#附录A 的既定不变量；以当前已核验的实际代码为事实来源。
> ⚠️ 自检中发现一处**既有缺陷**（M2 引入、真实网络路径才暴露）：SDK 历史回放时 assistant 的 tool_calls 未写入输入，导致压缩后的历史含 tool 结果但缺函数调用定义会 400。M3 必须一并修复（见 §4.2 / §8）。

---

## 1. 现在要实现的内容（M3 组成）

| 件 | 说明 | 归属里程碑 |
|---|---|---|
| 上下文压缩管线 | `ContextManager`：L3 落盘 → L1 裁中段 → L2 旧结果占位 → L4 LLM 摘要 → 硬截断 → sanitizeToolPairs；改副本、顺序铁律不可换 | M3-1 / M3-2 |
| Token 估算 | 中文/2 + 其他/4 + arguments | M3-1 |
| 回调配对兜底 | 复用/对齐 `SessionData.ensure_tool_pairing`，压缩边界不破坏配对 | M3-1 |
| SDK 映射修复 | `ai/mapping.py`：assistant 消息必须输出 `tool_calls`（`function_call` 输入项），否则回放 400 | M3-1 |
| 项目指令加载 | `ProjectInstructionsLoader`：CLAUDE.md / AGENTS.md，64KB 截断，层级优先级 | M3-2 |
| 记忆 Store | `MemoryStore`：磁盘索引，原子写，永不抛异常 | M3-3 |
| 记忆 Service | `MemoryService`：recall / remember / dream；LLM 追加可注入 | M3-3 |
| 系统提示装配 | `PromptAssembler`：base + 项目指令 + skill 目录 + 记忆目录 | M3-3 |
| Skill 目录 | `SkillRegistry`：SKILL.md frontmatter 解析、目录扫描、按需加载 | M3-3 |
| 集成 | `AgentLoop` 注入压缩/记忆/项目指令/skill；`AIService.execute_turn` 增加 `instructions` 参数；CLI 装配 | M3-3 |

> 现状核对（非臆造）：压缩、记忆、项目指令在现有代码中完全缺失；`AIConfig` 已具备 `max_context_tokens/max_messages/snip_keep_head/snip_keep_tail/keep_recent_tool_results/per_result_persist_bytes/l4_keep_tail` 全套默认值（M0 预留），`MemoryConfig` 已就绪。`skill` 工具（M2-4）已可用，M3 用 `SkillRegistry` 承接系统提示里的「技能目录」。

## 2. 对代码的影响 / 执行范围

- **M1/M2 现有文件改动极小**：`ai/base.py` 的 `execute_turn` 增加可选 `instructions` 参数；`agents_sdk_service.py` 使用该参数；`ai/mapping.py` 修复 assistant tool_calls 回放（这是唯一「触碰到现有绿测」的修复，其余均为新增文件后的演进）；`core/agent_loop.py` 增加可选组件注入并把「压缩 + 装配后历史」交给 AI；`cli.py` 装配 M3 组件。
- **新增包/模块**：`core/context_compression.py`（压缩）、`ai/prompt.py`（装配）、`core/project_instructions.py`、`memory/store.py`、`memory/service.py`、`skills/registry.py`。
- **SDK 依赖保持单点**：仍只有 `agents_sdk_service.py` + `tools/sdk_adapter.py` 两个文件 import `agents`。L4 摘要与记忆 LLM 均走**可注入回调**，不新增对 SDK 的耦合。
- **离线可测**：压缩 L1-L4、硬截断、配对、项目指令、记忆 Store、Promp装配、SkillRegistry 全部离线单测；真实 LLM 依赖路径（L4 摘要、记忆 recall/remember/dream 的 LLM 分支）用假回调注入，标注 network-only。

## 3. 目前设计（契合已定稿契约，含关键转义）

- 历史以 `list[ChatMessage]` 表示（M0 契约），压缩管线**全程操作副本**、绝不改调用方传入列表（不变量 §5-2）。
- 配对以 `assistant.tool_calls[id]` ↔ `tool.tool_call_id` 表达（M0 契约 + M2 会话持久化），压缩后必须保持；收敛复用 `ensure_tool_pairing`。
- 压缩发生在**每轮用户输入、交给模型之前**：`AgentLoop.process_input` 对 `session.messages` 做一次压缩副本 → 传给 `execute_turn`。这是对 SDK 自动多轮的合理适配（SDK 拥有轮内循环，我们在轮边界压缩整段历史）。
- L4 摘要、记忆 LLM、项目指令评估的模型调用全部以「可注入 callable」提供，默认实现走主模型/标注，单测注入假实现（延续 `FakeAIService` 模式）。
- 系统提示装配产物为单一字符串 `instructions`，随每轮传给 `execute_turn`，替代构造期的 `_DEFAULT_INSTRUCTIONS`（base 仍为兜底）。

## 4. 逐文件「新增功能 + 实现步骤」

### 4.1 `ai_coding/core/context_compression.py`（新增，最核心）
- `estimate_tokens(text: str) -> int`：中文（"，。…）按 2 token，其余按 4（含 JSON arguments，主计划附录 A）。
- `snip_middle(messages, max_messages, keep_head, keep_tail) -> list`：`len > max_messages` 时保头 `keep_head` + 保尾 `keep_tail`，中间替换为单条 `role=user` 消息 `[snipped N messages]`；**切点保护**：插入占位时若与 tool 消息相邻，先调整到配对边界（不拦腰砍掉半个函数调用配对）。
- `placeholder_old_results(messages, keep_recent, persisted_marker) -> list`：tool 结果条数 > `keep_recent` 时，更旧的、不含 `<persisted-output>` 标记的正文替换为 `[Previous: used <tool>]`；不删消息、不动 `tool_call_id/tool_name`。
- `persist_large_outputs(messages, root, per_result_bytes, preview_len) -> list`：取尾部连续 `role=tool` 批，批总字节 > `max_context_tokens/2` 才触发；对单块 > `per_result_bytes` 的落盘到 `root/transcripts/persisted/`，正文替换为 `<persisted-output path=… bytes=… tool=…>` + `preview` 字预览；落盘失败降级保留原文。
- `hard_trim(messages, keep_tail, system_preserved) -> list`：仍超限时保尾、删孤立 tool 结果与无果 tool_calls（调 `snip_pairing`）。
- `sanitize_pairs(messages) -> list`：复用配对不变量，删孤立 tool、剥无果 calls。
- `ContextManager(settings, transcripts_root, summarizer=None)`：编排四层 + `_fail` 熔断计数 + `compress(messages) -> list`（深拷贝一次，逐层改副本；L4 无 summarizer 或熔断或不足触发则跳过，成功调用清零计数）。

### 4.2 `ai_coding/ai/mapping.py`（修复）
- `chat_message_to_input`：assistant 且携 `tool_calls` 时，除文本 `message` 项外，为每个 call 追加 `{"type":"function_call","call_id","name","arguments"}`；assistant 无文本仅 calls 时只输出 function_call 项。tool 角色不变（`function_call_output`）。
- 依据：SDK `chatcmpl_converter.py` 读取 `function_call` 项挂到当前 assistant 消息（`ensure_assistant_message`，L830-858），`function_call_output` 生成 `tool` 消息（L860）。缺前者则 tool 结果成为孤立 → Chat Completions API 400。
- 新增 `history_to_inputs` 仍保序。

### 4.3 `ai_coding/core/project_instructions.py`（新增）
- `ProjectInstructionsLoader(root, max_bytes=65536, names=("CLAUDE.md","AGENTS.md"))`：按优先级取首个存在的文件，读正文，超 `max_bytes` 截断并追加省略标记；返回字符串；文件不存在返回空字符串。
- `load() -> str`；纯文件读取，离线可测。

### 4.4 `ai_coding/memory/store.py`（新增）
- `MemoryStore(path)`：`dict` + 磁盘 JSON（原子写，复用 `infra` json 工具）；`add(text) -> id`、`all() -> list[entry]`、`search(keywords, limit) -> list`（子串/分词命中）、`delete(id)`、`clear()`、`count`；任何 IO 异常被吞（不变量 §5-11 记忆永不抛）。
- 持久形状：`{"entries":[{id, text, created_time}]}`；独立可复用。

### 4.5 `ai_coding/memory/service.py`（新增）
- `MemoryService(store, extractor=None, config=None)`：
  - `remember(messages, conclusion)`：`extractor(messages, conclusion) -> list[str]`，为 None 时离线兜底抽取「最近 user/assistant 文本行」（跳过 tool），存储；**永不抛**。
  - `recall(history, k)`：离线按关键词召回上限 `k`；LLM 分支通过 `extractor` 扩展（network-only 标注）。
  - `dream()`：触发整理（`consolidate_threshold` 计数达线）；用 `extractor` 汇总并合并/压缩重复，再落库；失败静默。

### 4.6 `ai_coding/ai/prompt.py`（新增）
- `PromptAssembler(base_instructions, project_instructions="", skills_catalog="", memory_catalog="", plan_mode=False)`：按主计划附录 A 顺序拼系统提示（指令 → 项目指令 `<system-reminder>` → plan 段 → 技能目录 → 记忆目录），返回单串。段存在才拼、隔空行；纯串接可测。

### 4.7 `ai_coding/skills/registry.py`（新增）
- `SkillRegistry(skill_dir)`：`names() -> list[str]`（扫描顶层含 `SKILL.md` 的目录）、`catalog_text() -> str`（`- name: description` 目录文本）、`load(name) -> str|None`（frontmatter 正则解析 description，返回 `SKILL.md` 全文）、`is_empty`。
- 空目录 → 不注入技能目录（对齐 §4-13「空目录不注册 skill 工具」）。skill 工具与 registry 各自独立，互不破坏 M2 绿测。

### 4.8 `ai_coding/ai/base.py`（扩展）
- `execute_turn(history, user_input, token_sink=None, *, instructions: str | None = None)`：新增可选 `instructions`；抽象签名更新（`FakeAIService` 需同步补参，但默认无关，向后兼容）。

### 4.9 `ai_coding/ai/agents_sdk_service.py`（修改）
- `execute_turn` 接受 `instructions`；`_build_agent(instructions)` 用它替代 `_DEFAULT_INSTRUCTIONS`（缺省时仍用 base）。

### 4.10 `ai_coding/core/agent_loop.py`（集成）
- 构造新增可选组件：`context: ContextManager | None`、`memory: MemoryService | None`、`prompt: PromptAssembler | None`、`skills: SkillRegistry | None`、`project: ProjectInstructionsLoader | None`（缺省 None，行为与 M2 完全一致，不破坏既有测试）。
- `process_input`：user 消息入会 → `compact = context.compress(session.messages) if context else session.messages` → `instructions = prompt.build(project, skills, memory_catalog)` → `turn = ai.execute_turn(compact, user_input, on_delta, instructions=instructions)` → assistant 入会 → `ensure_tool_pairing` → 存档。记忆 `remember` 在回合结束前静默触发。

### 4.11 `ai_coding/cli.py`（装配）
- `ask` 在 `--workspace` 下装配：`SkillRegistry(workspace/skills 或 --skills)`、`ProjectInstructionsLoader(workspace)`、`MemoryStore(workspace/.memory)`、`MemoryService`、`ContextManager(ai.ai-config, transcripts_root=workspace)`、`PromptAssembler`，一并构造 `AgentLoop(service, sessions, context=…, memory=…, prompt=…, skills=…, project=…)`。均为可选注入，断连/缺失静默降级。

## 5. 数据流（M3 全线）

```
用户 ask --prompt "复述之前结论" (workspace)
 └ ConfigManager → AppConfig
     ├ SkillRegistry(workspace/skills)           ── catalog_text()
     ├ ProjectInstructionsLoader(workspace)      ── load()  (CLAUDE.md/AGENTS.md, 64KB)
     ├ MemoryStore(workspace/.memory) → MemoryService
     ├ ContextManager(ai.*, transcripts_root)    (summarizer=主模型顶层调用, 可注入)
     └ PromptAssembler(base, project, skills, memory_catalog, plan_mode=False)
AgentLoop.process_input(session, prompt)
  ├ user 消息入会
  ├ compact = ContextManager.compress(session.messages)   # 深拷贝→L3→L1→L2→L4→硬截断→sanitize
  │     └ 改副本，慢速语义不落入原 session；L3 落盘 transcripts/persisted/；L4 熔断计数
  ├ instructions = PromptAssembler.build(project.load(), skills.catalog_text(), memory.recall(...))
  ├ turn = ai.execute_turn(compact, prompt, on_delta, instructions=instructions)
  │     └ AgentsSDKChatService: _build_agent(instructions) → Runner.run_streamed
  │          输入 = history_to_inputs(compact)  # assistant 含 function_call 项(修复后)
  ├ assistant 消息入会 → ensure_tool_pairing → sessions.save
  └ memory.remember(session.messages, turn.text)   # 静默，永不抛
```

**关键不变量**（主计划 §5 继承）：
1. 压缩任何一层不改调用方历史（副本）。
2. 顺序铁律 L3→L1→L2→L4→硬截断→配对，不可换。
3. 压缩后 `assistant.tool_calls[id]` 与 `tool.tool_call_id` 一一对应（sanitize 兜底）。
4. 记忆永不抛异常；`dream` 计数达阈值才触发；失败静默。
5. 权限门 / 工具行为不因压缩状态改变（只影响喂给模型的历史文本）。

## 6. TDD 用例（验收锚点，先写后实现）

### M3-1（压缩 L1/L2/L3 + 映射修复）
1. `test_estimate_tokens.py` — 中文计 2、ASCII 计 4、空串 0、含 JSON 的 arguments。
2. `test_context_compression.py` — `test_snip_keeps_head_tail` / `test_snip_threshold_not_exceeded_noop` / `test_snip_placeholder_message` / `test_placeholder_marks_old_results` / `test_placeholder_keeps_recent` / `test_placeholder_skips_persisted_marker` / `test_persist_large_output_writes_disk` / `test_persist_never_breaks_on_io_error` / `test_persist_preview_truncated` / `test_compress_returns_new_list`（不改原对象）/ `test_sanitize_drops_orphan_results`。
3. `test_ai_mapping.py` 增补 — `test_assistant_with_tool_calls_emits_function_call_items` / `test_tool_msg_emits_function_call_output` / `test_assistant_no_content_only_calls` / `test_plain_user_assistant_unchanged`。

### M3-2（L4 + 硬截断 + ProjectInstructionsLoader）
4. `test_context_compression_l4.py` — `test_l4_skips_below_threshold_with_fake_summarizer` / `test_l4_returns_summary_plus_tail` / `test_l4_uses_injected_summarizer` / `test_l4_no_summarizer_skips` / `test_l4_circuit_breaker_after_3_failures` / `test_l4_success_resets_counter` / `test_hard_trim_keeps_tail_and_drops_oldest` / `test_hard_trim_drops_orphan_pairs`.
5. `test_project_instructions.py` — `test_loads_claude_md` / `test_agends_md_precedence` / `test_missing_returns_empty` / `test_64kb_truncation`。

### M3-3（记忆 + Prompt + Registry + 集成）
6. `test_memory_store.py` — `test_add_and_all` / `test_search_keyword` / `test_delete_and_clear` / `test_persists_and_reloads` / `test_io_error_swallowed`.
7. `test_memory_service.py` — `test_recall_offline_keyword` / `test_remember_injects_and_stores` / `test_remember_never_raises` / `test_dream_threshold` / `test_extractor_injected`.
8. `test_prompt.py` — `test_base_only` / `test_appends_project_and_skills_sections` / `test_plan_mode_segment` / `test_memory_catalog`.
9. `test_skill_registry.py` — `test_scan_names` / `test_empty_dir_no_catalog` / `test_catalog_text` / `test_load_frontmatter_description` / `test_load_missing_none`.
10. `test_agent_loop_m3.py`（FakeAI + 假 ContextManager/记忆）— `test_process_input_injects_and_persists_each` / `test_compression_applied_to_history` / `test_instructions_passed_to_ai` / `test_memory_remember_called_silently` / `test_components_optional_backwards_compatible`.
11. `test_cli.py` 增补 — `test_ask_wires_m3_components`（离线，验证不崩）/ `test_ask_missing_workspace_degrades`.

> 预估新增 30-35 用例；全部离线（L4/记忆 LLM 注入假实现，真实路径标注 network-only）。

## 7. 成功之后是什么样

- 长会话不再触发模型 400：历史超 `max_messages` → 自动裁中段；旧超大工具结果 → L3 落盘并用占位符替换；超 token → L4 摘要 + 熔断；极端情况硬截断 + 配对兜底。
- 项目指令自动注入：`workspace/CLAUDE.md` 或 `AGENTS.md` 被读入系统提示；技能目录、记忆目录按需拼入。
- 记忆闭环：多轮后 `MemoryService` 静默整理并落盘 `workspace/.memory/`，后续轮次可召回。
- 压缩/装配产物对 SDK 完全可消费：assistant tool_calls 正确回放（映射修复），工具多轮与历史续聊不再 400。
- `pytest` 全绿（164 + 30+）；`ruff`、`mypy` 0 错误；`main` 新增 **M3-1 / M3-2 / M3-3** 三个提交。

## 8. 自我核验（含发现并须修复的问题）

- ✅ **SDK 只出现在 2 个文件**：压缩/记忆/项目指令/装配均为纯逻辑 + 可注入回调，无对 `agents` 的 import。
- ✅ **改副本铁律**：`compress` 入口 `list(messages)` 深拷贝，各层只操作副本，原 `session.messages` 不受污染（有专门用例兜底）。
- ✅ **顺序不变量**：编排在单个 `compress` 内硬编码 L3→L1→L2→L4→硬截断→sanitize，不可被调用方打乱。
- ⚠️ **发现 M2 缺陷（必须在此修复）**：`chat_message_to_input` 对 assistant 仅输出 `{role, content}`，丢弃 `tool_calls`。SDK 回放历史时，`function_call_output`（tool 结果）将无对应 `function_call` 定义 → Chat Completions 拒绝（400）。这在本阶段因「压缩后的历史含 tool 结果」而必然触发，故 M3 一并修复映射，并补 `test_ai_mapping` 用例。
- ✅ **L4 熔断**：连续失败计数，达 3 即本会话不再摘要；成功清零；计数器实例级（沿用主计划「会话级」语义，简化不与网络测试耦合）。
- ✅ **记忆永不抛**：Store/Service 所有 IO 用 try/except 兜底；`recall` 无命中返回空列表而非异常。
- ⚠️ **限制/风险**：真实 LLM 的 L4 摘要、记忆 LLM recall/remember/dream 分支依赖网络，单测用假回调注入、标注 network-only，需手动冒烟；压缩发生在「轮边界」而非 SDK 轮内每次 chat（SDK 拥有内循环），对齐「喂给模型的历史做一次压缩」语义。
- ✅ **向后兼容**：`AgentLoop` / `AIService` 新参全部可选，缺省时行为与 M2 完全一致，既有 164 个测试不应回归。

*Acknowledge：以上基于 `ThoughtCoding_Python_改造方案.md` §3.3/§5/#附录A、已核验的 SDK `chatcmpl_converter.py` 输入回放语义、以及当前 M0-M2 实际代码结构。若无异议我按此 TDD 落地 M3（含映射缺陷修复）。*