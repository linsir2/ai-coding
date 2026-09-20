# AI-Coding — M2 阶段分析方案（工具系统 + 权限门 + 多轮循环）

> 定位：在 M1 打通的「用户→AI→会话持久化」单轮流之上，落地工具系统与多轮循环。
> 工具 8 个 + 权限门 + HITL + 沙箱 + 文件一致性 + SDK 自动多轮。
> 以已核验的 SDK `0.22.3` `function_tool`/`needs_approval`/`Runner.run_streamed` 自动循环为唯一事实来源。

---

## 1. 现在要实现的内容（M2 组成）

| 件 | 说明 | 归属里程碑 |
|---|---|---|
| 工具基类 + Sandbox 沙箱 | `BaseTool` 端口 + `Sandbox` 路径约束 | M2-1 |
| 5 个基础工具 | `bash` / `read` / `write` / `edit` / `glob` | M2-1 |
| 文件一致性追踪 | `FileStateTracker`（stale-read 校验） | M2-2 |
| 权限门 | `PermissionGate`（ALLOW/WARN/DENY + bash 硬拒绝） | M2-2 |
| HITL 接口 | `ApprovalCallback` 抽象 + CLI 实现占位 | M2-2 |
| 工具注册表 | `ToolRegistry`（注册/查询/转 SDK FunctionTool） | M2-3 |
| SDK 工具适配 | 把 `BaseTool` 包装成 SDK `FunctionTool`，`needs_approval` 接权限门 | M2-3 |
| AgentLoop 多轮 | 从「单轮」改为「SDK 自动多轮」，收集所有 tool_call/tool_output 回写会话 | M2-3 |
| todo_write 工具 | 内存待办清单 | M2-4 |
| skill 工具 | 技能目录读取 | M2-4 |
| subAgent 骨架 | 派发子 Agent（M3 完善 Worktree 隔离） | M2-4 |
| CLI ask 多轮交互 | 流式输出 + 工具调用展示 + HITL 确认 | M2-4 |

## 2. 对代码的影响 / 执行范围

- **M1 文件改动极少**：`ai/base.py` 增加 `tools` 参数；`agents_sdk_service.py` 接入工具列表和 approval；`agent_loop.py` 从单轮变多轮收集；`cli.py` 新增流式/工具展示/HITL。
- **新增包**：`ai_coding/tools/`（基类+8工具+注册表+SDK适配）、`ai_coding/security/`（sandbox+permission_gate+file_state_tracker）。
- **SDK 依赖保持单点**：`agents_sdk_service.py` + 新增 `tools/sdk_adapter.py` 是仅有的两个 import `agents` 的文件。
- **离线可测**：所有工具、权限门、沙箱、注册表 100% 离线单测；SDK 适配器用假工具验证 FunctionTool 构造；真实网络路径同 M1 标注。

## 3. 目前设计（契合 M0/M1 已定稿契约）

- 工具输出沿用 `ToolResult(success, output)`（M0 `domain/message.py` 已定）。
- 工具调用沿用 `ToolCallRef(id, name, arguments)`，会话历史中 assistant 消息带 `tool_calls`、tool 消息带 `tool_call_id`/`tool_name`（M0 已定）。
- `TurnResult.tool_calls` 在 M1 预留，M2 填入真实工具调用（M1 为空 list，契约不变）。
- `AIService.execute_turn` 签名不变（入参 history+input，出参 TurnResult），但内部从"单轮文本"变为"多轮工具调用+最终文本"——SDK `Runner.run_streamed` 自动管循环，我们只收集事件。
- M3 的上下文压缩、MCP 工具、权限策略文件化 后续插入，本阶段预留扩展点。

## 4. 逐文件「新增功能 + 实现步骤」

### 4.1 `ai_coding/security/sandbox.py`
- `Sandbox(workspace: Path)`：所有路径经 `resolve()` 展开 `~`，强校验 `is_within_workspace()`，越界抛 `SandboxViolation`。
- 纯路径逻辑，全离线可测。

### 4.2 `ai_coding/tools/base.py`
- `class BaseTool(ABC)`：`name: str`、`description: str`、`params_json_schema: dict`、`async execute(params: dict) -> ToolResult`。
- 入参已解析为 dict（SDK 会自动解析 JSON），出参 `ToolResult`（M0 契约）。

### 4.3 `ai_coding/tools/bash_tool.py`
- `BashTool`：`asyncio.create_subprocess_shell` 执行，`timeout` 默认 60s，输出截断 50K 字符，返回 `ToolResult(success, output)`。
- 不做权限判断（权限门在外层）。

### 4.4 `ai_coding/tools/read_tool.py`
- `ReadTool(sandbox, file_state_tracker, max_file_size=10MB)`：按 `offset`/`limit` 分页读，`cat -n` 风格行号。读后登记文件快照到 `FileStateTracker`。

### 4.5 `ai_coding/tools/write_tool.py`
- `WriteTool(sandbox, file_state_tracker, max_chars=2M)`：写入/覆盖，自动建父目录。写前做 stale-read 校验（从未读过 → 拒绝；外部改过 → 拒绝）。

### 4.6 `ai_coding/tools/edit_tool.py`
- `EditTool(sandbox, file_state_tracker)`：精确字符串替换，`old_string` 必须唯一匹配，支持 `replace_all`。强制先读后改（stale 校验）。

### 4.7 `ai_coding/tools/glob_tool.py`
- `GlobTool(sandbox, max_results=250, max_depth=20)`：`Path.glob`/`rglob`，按 mtime 倒序，最多 250 条。

### 4.8 `ai_coding/security/file_state_tracker.py`
- `FileStateTracker`：`dict[path, FileSnapshot(mtime, size)]`。
- `record(path)` 登记快照；`check(path) -> FileState(CLEAN/STALE/NEVER_READ)`。

### 4.9 `ai_coding/security/permission_gate.py`
- `PermissionGate`：`check(tool_name, params) -> PermissionDecision(ALLOW/WARN/DENY, reason)`。
- 默认规则：read/glob ALLOW（越界 WARN）、write/edit WARN、bash WARN（硬拒绝列表直接 DENY）、todo_write/skill/subAgent ALLOW、未知工具 WARN。
- bash 硬拒绝：`rm -rf /`、`sudo `、`shutdown`、`reboot`、`mkfs`、`dd if=`、`> /dev/sda`、`git push --force` 等。

### 4.10 `ai_coding/security/approval.py`
- `ApprovalCallback` 协议：`async def approve(tool_name, params, reason) -> ApprovalResult(YES/NO/STOP)`。
- CLI 实现占位（M2-4 真正接交互式）。

### 4.11 `ai_coding/tools/registry.py`
- `ToolRegistry`：`register(tool)`、`get(name) -> BaseTool|None`、`list_names() -> list[str]`、`to_sdk_tools() -> list[FunctionTool]`。
- 不同 owner 重名拒绝覆盖（简化版：单 owner 即可）。

### 4.12 `ai_coding/tools/sdk_adapter.py`
- `tool_to_function_tool(tool: BaseTool, gate: PermissionGate, approval: ApprovalCallback) -> FunctionTool`。
- 用 `FunctionTool` 构造（或 `function_tool` 工厂），`name`/`description`/`params_json_schema` 直传。
- `on_invoke_tool` 内部调用 `tool.execute(params)` 并返回 `output` 字符串。
- `needs_approval` 回调：`gate.check(tool_name, params)` → DENY 返回 False（阻断）、ALLOW 返回 True、WARN 调 `approval.approve()`。

### 4.13 `ai_coding/ai/agents_sdk_service.py`（修改）
- 构造函数新增 `tools: list[Tool]` 参数。
- `_build_agent()` 把 tools 传给 `Agent(tools=...)`。
- `execute_turn()` 除了收集文本 delta，还要收集 `RunItemStreamEvent(name="tool_called"/"tool_output")` 事件，汇总到 `TurnResult.tool_calls` 和会话历史的 tool 消息。**关键**：SDK 自动执行工具，我们不需要手动调度，只需收集事件用于持久化。

### 4.14 `ai_coding/core/agent_loop.py`（修改）
- `process_input()` 流程：user msg → `ai.execute_turn()` → assistant msg（带 tool_calls）→ **若有工具调用，追加 tool output 消息** → `ensure_tool_pairing()` → save → 返回文本。
- 工具调用的所有中间结果由 SDK 自动处理；我们只需把每对 tool_call/tool_output 写回 session 历史。

### 4.15 `ai_coding/tools/todo_write_tool.py`
- `TodoWriteTool`：内存 `list[TodoItem]`，支持 add/update/list。无副作用，ALLOW。

### 4.16 `ai_coding/tools/skill_tool.py`
- `SkillTool(skill_dir)`：列出技能目录、读取 `SKILL.md` 内容。纯读，ALLOW。

### 4.17 `ai_coding/tools/subagent_tool.py`
- `SubAgentTool`：骨架实现——派发任务给子 Agent，复用同一模型配置。M2 只实现单工同步调用；M3 加 Worktree 隔离和后台模式。

### 4.18 `ai_coding/cli.py`（扩展）
- `ask` 命令新增 `--workspace` 参数。
- 构造 `Sandbox` → `FileStateTracker` → `PermissionGate` → `ToolRegistry` → 注册所有启用工具 → `build_service_from_config` 增加 tools 参数。
- delta 流式输出；工具调用时打印 `[tool: bash] $ cmd` 风格提示；WARN 级工具走交互式确认（y/n/stop）。

## 5. 数据流（M2 全线）

```
用户 ask --prompt "帮我读 foo.py 并改个 bug"
 └─ ConfigManager → AppConfig
     └─ Sandbox(workspace) → FileStateTracker → PermissionGate → ToolRegistry
         └─ 注册 bash/read/write/edit/glob/todo_write/skill/subAgent (按配置)
             └─ sdk_adapter → list[FunctionTool] → AgentsSDKChatService(tools=...)
AgentLoop.process_input
  ├─ user msg 加入 session
  ├─ ai.execute_turn(history, input, token_sink=delta_printer)
  │    └─ Runner.run_streamed(Agent with tools, input=history+user)
  │         ├─ raw_response_event → text delta → token_sink
  │         ├─ run_item_stream_event (tool_called) → 记录 tool_call
  │         ├─ run_item_stream_event (tool_output) → 记录 tool_output
  │         └─ ... 自动多轮，直到 final output ...
  │         └─ TurnResult(text, tool_calls=[...], tool_outputs=[...])
  ├─ 把所有 tool_call 包进 assistant msg，所有 tool_output 生成 tool msg
  ├─ ensure_tool_pairing + sessions.save
  └─ return text → 打印
```

**关键不变量**：
1. 每轮 user 输入 → 可能多轮 tool_call/tool_output → 最终 assistant 文本
2. 所有 tool_call 有对应 tool_output（SDK 保证，`ensure_tool_pairing` 兜底清理孤儿）
3. 每条消息带时间戳
4. 权限门永远 fail-closed（未知工具默认 WARN）
5. 沙箱路径校验在工具执行前完成

## 6. TDD 用例（验收锚点，先写后实现）

### M2-1
1. `test_sandbox.py` — `test_resolve_within_workspace` / `test_resolve_outside_raises` / `test_tilde_expansion` / `test_nested_paths_ok` / `test_parent_traversal_blocked`.
2. `test_base_tool.py` — 抽象基类不可实例化、子类必须实现 execute。
3. `test_tool_bash.py` — `test_bash_echo` / `test_bash_stderr_captured` / `test_bash_timeout` / `test_bash_nonzero_exit` / `test_bash_output_truncation`.
4. `test_tool_read.py` — `test_read_basic` / `test_read_offset_limit` / `test_read_line_numbers` / `test_read_nonexistent_fails` / `test_read_records_snapshot`.
5. `test_tool_write.py` — `test_write_creates_file` / `test_write_overwrites` / `test_write_creates_parent_dirs` / `test_write_outside_sandbox_denied` / `test_write_stale_denied`.
6. `test_tool_edit.py` — `test_edit_replace` / `test_edit_old_string_not_unique` / `test_edit_replace_all` / `test_edit_requires_prior_read` / `test_edit_stale_denied`.
7. `test_tool_glob.py` — `test_glob_finds_files` / `test_glob_pattern` / `test_glob_max_results` / `test_glob_sorted_by_mtime`.

### M2-2
8. `test_file_state_tracker.py` — `test_initial_never_read` / `test_after_read_clean` / `test_external_modification_stale` / `test_after_reread_clean_again`.
9. `test_permission_gate.py` — `test_read_allow` / `test_write_warn` / `test_bash_hard_deny` / `test_bash_normal_warn` / `test_unknown_tool_warn` / `test_todo_write_allow` / `test_bash_force_push_deny`.

### M2-3
10. `test_tool_registry.py` — `test_register_and_get` / `test_get_missing_none` / `test_list_names` / `test_duplicate_name_rejected`.
11. `test_sdk_adapter.py` — `test_adapter_preserves_name_and_desc` / `test_adapter_invoke_returns_output` / `test_adapter_needs_approval_allow` / `test_adapter_needs_approval_deny` / `test_adapter_approval_callback`.
12. `test_agent_loop_tools.py`（FakeAIService 带工具调用）— `test_process_input_with_tool_calls_persists_both` / `test_tool_output_message_has_call_id` / `test_multiple_tool_calls_in_one_turn` / `test_empty_tool_calls_still_works`.

### M2-4
13. `test_todo_write_tool.py` — `test_todo_add` / `test_todo_update_status` / `test_todo_list`.
14. `test_skill_tool.py` — `test_skill_list` / `test_skill_read_content`.
15. `test_subagent_tool.py` — `test_subagent_basic_call`（stub 验证参数透传）.
16. `test_cli.py` 增补 — `test_ask_with_tools_enabled`（离线，验证不崩）/ `test_ask_workspace_flag`.

> 预估新增约 50+ 用例；全部离线可测（真实 LLM 路径标注 network-only）。

## 7. 成功之后是什么样

- `ai-coding ask --prompt "列出当前目录的 py 文件"` → 模型调用 glob → 返回结果 → 模型总结 → 打印。
- `ai-coding ask --prompt "删除 foo.txt"` → 权限门弹确认 `[bash] 即将执行 rm foo.txt，是否继续？[y/N/s]`。
- `pytest` 全绿（68+50）；`ruff`、`mypy` 0 错误。
- `main` 分支新增 **M2-1…M2-4** 四个提交。
- 工具系统、权限系统、沙箱、一致性校验全部就绪，M3 可直接插入 MCP、上下文压缩、SubAgent Worktree 隔离。

## 8. 自我核验

- **SDK 只在 2 个文件出现**：`agents_sdk_service.py`（M1）+ `sdk_adapter.py`（M2）。解耦成立。
- **工具执行不绕过权限门**：SDK 的 `needs_approval` 回调是唯一放行通道，fail-closed。
- **沙箱是所有文件操作的必经之路**：read/write/edit/glob 都注入 sandbox，越界直接抛异常。
- **stale-read 校验**：write/edit 前必查 FileStateTracker，防止盲写和覆盖外部修改。
- **会话历史兼容 Java**：tool_call 用 `ToolCallRef`，tool 消息用 `tool_call_id`/`tool_name`，JSON 序列化走 `to_dict`/`from_dict`，与 Java `sessions/*.json` 格式一致。
- **风险/限制**：M2 的 subAgent 是骨架（无 Worktree 隔离），M3 完善；真实网络路径同 M1 不单测，需手动冒烟验证。

*Acknowledge：以上基于已核验的 SDK 0.22.3 function_tool/needs_approval/Runner 自动多轮 API，以及 ThoughtCoding 原始 8 工具清单。若无异议我按此 TDD 落地 M2。*