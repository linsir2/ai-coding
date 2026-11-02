# P2-A 实施方案（评审修订稿）：统一父子信息包与写回可观测

> 前置：P0/P1 已完成（`1fe8a58..23b8b32`），除本方案文档外工作区干净。
> 本方案只做 P2-A（契约收敛 + 写回可观测），**不做** WriteCoordinator / Scheduler /
> TaskRegistry / 合并队列（P2-B/C，已记入待办）。
> 依据：`docs/orchestration-p2-research.md`；行号以 HEAD `23b8b32` 为准。
> 已过一轮独立子智能体评审，本稿为修订稿（P0/P1 全部吸收）。

## 0. 范围与非目标

**做**
1. 一个信封 `AgentPacket`（header + `SubagentTask` + `SubagentResult`）成为父↔子线上形状。
2. 六条通道（同步工具结果 / 后台事件 / 模型 notice / ledger / meta / 列表行）由它投影。
3. `changed_paths`、`commit_error`、`degraded` 三个写回事实进入结果与所有通道。
4. 修掉“写成果静默丢/降级静默”：`commit_if_dirty` 区分“无改动”与“失败”；非 git
   退回主工作区必须可见；后台每个 turn 结束即提交，不再等 close。

**不做**
- 不加路径 claim / 租约 / fence / 写队列（B）。
- 不自动 merge / cherry-pick（B/C）。
- 不改 plan 规则、gate 语义、Scheduler、wire 既有字段名。
- 不新增没有消费者的字段；每个新字段下面写明谁读它。

## 1. 根因（现状事实）

| # | 事实 | 证据 |
|---|---|---|
| R1 | 父→子只有自由文本 `task`；`SubagentBrief` 只在 fan-out 使用 | `tools/subagent_tool.py:260`、`tools/agents_tool.py:152`、`core/research_fanout.py:142` |
| R2 | 子→父同一结算在 6 条通道各写一遍，字段不一致 | packet `domain/subagent.py:207`；event `core/events.py:470`；notice `core/subagent.py:1781`；ledger `:1673`；meta `:1516`；row `domain/subagent.py:449` |
| R3 | 后台 notice/event 丢 `facts/usage/todos/branch/commit` | `core/subagent.py:1082-1115`、`core/events.py:480-492` |
| R4 | `commit_if_dirty` 的“失败”与“无改动”都返回 `None` | `core/worktree.py:128-166` |
| R5 | 非 git / `can_isolate()==False` 静默退回主工作区，无提示 | `core/subagent.py:351-397`、`:1032-1054` |
| R6 | 后台持久 worktree 只在 `close()` 提交且 sha 被丢弃；事件结算早于提交 | `core/subagent.py:830-837`、`:1005-1007` |
| R7 | 父不知道子改了哪些文件；artifacts 路径可指向已删 worktree | `core/subagent.py:1638-1640` |
| R8 | 同步工具结果、事件、列表是三套投影；`subagent_list` 只有 4 字段 | `tools/subagent_tool.py:107-125,473-486` |

## 2. 契约设计

### 2.1 `PacketHeader`（`domain/subagent.py`）

```
v: int = 3                 # 消费者读它决定形状
message_id: str            # uuid4 hex[:12]；同时是 ledger 文件名
run_id: str = ""           # 同步/一次性子为空；后台/句柄为 run id
origin_session: str = ""   # 发起方 session；event/meta/bridge/ledger 读
```

消费点：`v` → ledger/event/tool 读取方；`message_id` → ledger 文件名与
`ledger_path`；`run_id` → notice/event/list/寻址；`origin_session` → event/meta/
bridge/ledger 来源标注。**不加 `correlation_id`/`created_at`**（当前无消费者）。

### 2.2 `SubagentTask`（父→子，唯一输入形状）

```
brief: SubagentBrief        # objective/context/scope/constraints（复用既有上限）
context_mode: str | None    # None = 用 runner 配置；"full"/"task_only"
agent: str | None
readonly: bool = False
isolate: bool = True
output_schema: dict | None

render() -> str
to_dict() / from_dict()     # ledger 的 task、meta 的 taskPacket；resume 反序列化
```

`render()` 规则（修 P0-3）：
- brief 只有 objective（无 context/scope/constraints）→ **直接返回 objective 原文**，
  不套 `[TASK]`，保证模型自由文本与旧路径逐字一致。
- brief 有额外段落 → 返回 `brief.render()`（内部 fan-out 的固定信封）。

便捷构造：`SubagentTask.from_objective(text, **dispatch)` 内部包
`SubagentBrief(objective=text)`，避免各处手写 brief；`to_dict` 仍输出 `brief` 的四个字段。

派发参数优先级（修 P0-3 双源）：
- runner 公共入口接受 `str | SubagentTask`。传 `SubagentTask` 时，**packet 字段是唯一
  权威**（`context_mode/agent/readonly/isolate/output_schema` 全部取 packet；`context_mode
  为 None` 时用 runner 配置）。传 `str` 时沿用既有 kwargs（测试/旧调用不受影响）。
- 工具层只传 `SubagentTask`，不再同时传这些 kwargs；`str` 分支仅为兼容与测试。

`SubagentBrief` 增加 `to_dict()/from_dict()`（加法，唯一序列化基础）。

### 2.3 `SubagentResult` 增量（子→父结果体）

新增字段（全部默认可选，v3）：
- `changed_paths: tuple[str, ...] = ()` —— 子 loop `last_artifacts` 中
  `kind=="file"` 的去重路径；语义是“**工具申报的改动**”，bash 直接改的文件不在内
  （B 再做 git diff）。消费：notice/列表/event 投影/前端。
- `commit_error: str | None = None` —— 有改动但提交失败的原因；消费：
  `_render`/notice/列表/event 投影/前端。
- `degraded: str | None = None` —— `worktree_unavailable` / `worktree_create_failed`；
  消费：同上（文案“未能隔离，已在主工作区执行”）。

`to_dict()` 升 v3；保留原 17 键，只加不删。

### 2.4 `AgentPacket`（唯一线上信封）

```
header: PacketHeader
task: SubagentTask | None
result: SubagentResult

to_dict() -> 扁平 dict:
  {**header, "task": task.to_dict() | None, **result.to_dict()}
```

只提供 `to_dict`：ledger 的唯一内部读取方是 `_refresh_ledger`（合并提交后的字段），
它直接读 JSON dict，不需要类型化 `from_dict`；模型用 `read` 读原文。`SubagentTask.from_dict`
有消费者（resume 从 meta 重建），所以提供。

### 2.5 通道映射（唯一真源 → 投影）

| 通道 | 由谁产生 | 形状 | 变化 |
|---|---|---|---|
| 同步工具结果 | `tools/subagent_tool.py` | `ToolResult.data` | 只加小字段 `changed_paths/commit/commit_error/degraded`；不塞整个 packet（避免 `<data>` 重复全文） |
| 后台事件 | `_finish_background` | `BackgroundTaskDone` | 新增 `details: dict\|None`，**只放投影**：`v/messageId/status/truncated/exit_reason/facts/schema_valid/changed_paths/branch/commit/commit_error/degraded/usage/ledger_path`，不含 conclusion/task；bash job 事件为 None |
| 模型 notice | `_notice_text` | 单行文本 | 由结果投影渲染：状态、摘要、facts 摘要、改动数、分支/commit、提交失败/降级、ledger 指针 |
| ledger | `_run_turn` | `<ws>/transcripts/subagents/<message_id>.json` | 扁平 v3 packet（含 task）；`_refresh_ledger` 读旧文件合并 result 字段，不覆盖 task/header |
| meta | `_write_meta` | `<agents>/meta/<run_id>.json` | 新增 `taskPacket`、`outputSchema`、`worktreeBranch`、`commit`、`commitError`、`degraded` |
| 列表行 | `BackgroundTask` | `/tasks`、`list_agents`、`subagent_list` | 新增 `summary`、`worktree_branch`、`commit`、`changed_paths`、`commit_error`、`degraded` |

事件坚持“只带指针”的既有规则（`core/events.py:476-477`）：`details` 是投影，
全文仍在 ledger。

## 3. 数据流

### 3.1 同步（`subAgent`）

```
工具 execute
  params → SubagentTask.from_objective(task, context_mode=None, agent,
                                       readonly, isolate, output_schema)
  runner.run(packet, origin_session=ctx.session_id, worktree_manager, ...)
    _split: text=packet.render()（objective-only 时原文）, request=packet
    _run_isolated
      isolate 判定（_should_isolate）
      ├─ 非 git → degraded="worktree_unavailable"（report_degraded + 结果字段）
      ├─ 创建失败 → degraded="worktree_create_failed"
      └─ 成功 → worktree 内跑
        _run_turn
          header = PacketHeader(message_id=uuid, run_id="", origin_session)
          result.changed_paths ← artifacts
          AgentPacket → 写 ledger（<message_id>.json）
      隔离成功且 result 有改动：commit_if_dirty
        ├─ (sha, None)   → result.commit + branch
        ├─ (None, None)  → 无改动
        └─ (None, "err") → result.commit_error
        _refresh_ledger(result)  # 读旧文件，merge result，保留 task/header
  data ← result 小字段投影；_render(result) 文本（新增提交失败/降级行）
```

### 3.2 后台（`spawn_agent` / `subAgent(background=true)`）

```
spawn_background(packet, origin_session, ...) → run_id
  _open_handle(request=packet, readonly, ...) → handle.request
  _write_meta(handle)  # taskPacket/outputSchema
  _supervised
    isolate 判定；attach 失败/非 git → degraded
    _run_turn → result + changed_paths + ledger
    _commit_turn(handle, result)   # 每个 turn 结束即提交（修 P0-2）
      → commit/branch/commit_error 回填 result + handle
      → _refresh_ledger(result)
    _write_meta(handle)
    _finish_background
      row ← result 投影
      notice ← _notice_text(result)
      event ← BackgroundTaskDone(details=result 投影)
  _run_followup 同样：_run_turn → _commit_turn → _finish_background
close(handle)
  commit_if_dirty 兜底 → handle.commit/commit_error
  _write_meta(handle)   # 先落盘再删 worktree，sha 不再丢
  remove(worktree)
  CloseAgentTool 通过 runner.close_info(run_id) 读出 commit/commit_error 并回显
```

## 4. 逐文件改动

### 4.1 `ai_coding/domain/subagent.py`
- `SUBAGENT_PACKET_VERSION = 3`（注释写明 v3 内容）。
- `SubagentBrief` 增加 `to_dict/from_dict`。
- 新增 `PacketHeader`、`SubagentTask`（`render/to_dict/from_dict`；objective-only 原文）。
- `SubagentResult` 加 `changed_paths/commit_error/degraded`；`to_dict` 输出这三项。
- 新增 `AgentPacket.to_dict`（扁平合并）。
- `BackgroundTask` 加 `summary/worktree_branch/commit/changed_paths/commit_error/
  degraded`，`to_dict/from_dict` 同步（加法）。

### 4.2 `ai_coding/core/worktree.py`
- `commit_if_dirty(...) -> tuple[str | None, str | None]`：
  无改动 `(None, None)`；失败 `(None, "<命令>：<原因>")`；成功 `(sha, None)`。
- docstring 说明“失败不再与无改动混同”。

### 4.3 `ai_coding/core/subagent.py`
- `AgentHandle` 新增 `request: SubagentTask | None`、`commit: str|None`、
  `commit_error: str|None`、`degraded: str|None`。
- `run(task: str | SubagentTask, ..., origin_session="")`、
  `spawn_background(task: str | SubagentTask, ...)`；内部 `_split_task()` 归一化：
  packet 权威、str 用 kwargs。
- `_run_isolated`：`degraded` 三态——`not isolate`（含只读子）→ `None`；
  `can_isolate()==False` → `worktree_unavailable`；`WorktreeError` →
  `worktree_create_failed`；manager 为 None → `None`。commit 解包 →
  `commit/commit_error`；随后 `_refresh_ledger`。
- `request` 贯穿内部链路：`run`/`spawn_background` → `_run_isolated`/`_supervised`
  → `_run_in`/`_open_handle` → `handle.request`；否则 fanout 的 `output_schema` 与
  ledger 的 `task` 会丢失。
- `_run_turn(handle, text, request=None)`：
  - `request ?? handle.request ?? SubagentTask.from_objective(text)`；
  - **message_id 只在这里生成**：`header = PacketHeader(message_id=uuid4().hex[:12],
    run_id=handle.run_id, origin_session=handle.origin_session)`；
  - `_packet` 只产出 result（含 changed_paths），随后建 `AgentPacket`，
    由 `_write_ledger(packet, root)` **唯一一处**写盘并返回路径（文件名 =
    `message_id`）；`result` 用 `replace(..., ledger_path=...)`，不逐字段重建；
  - 超时分支也走同一路径（保留 changed_paths 与 ledger）；
  - 原 `_with_ledger` 删除/替换为 `_write_ledger`，避免“第二个 uuid 生成点”。
- `_packet`：`changed_paths` 从 `loop.last_artifacts` 取 `kind=="file"` 去重。
- `_refresh_ledger`：读旧 JSON → `update(result.to_dict())` → 写回；保留 task/header。
- `_attach_for_run` 返回 `degraded: str|None`，三态区分（修评审）：
  manager 为 None（未配置隔离）→ `None`（不误报）；`can_isolate()==False` →
  `worktree_unavailable`；`create()` 抛 `WorktreeError` → `worktree_create_failed`。
  `_supervised` 接收并 `replace` 进结果；`_run_followup` 复用 handle 已建的
  worktree，不再重复 attach，但每轮同样要提交。
- 新增 `_commit_turn(result, worktree_manager, worktree_path, name)`：显式收三件套，
  不依赖 handle（同步路径的 manager 是 `_run_isolated` 的局部变量，`core/subagent.py:369-383`）；
  回填 result（commit/branch 或 commit_error）并刷新 ledger；同步临时 worktree
  与后台持久 worktree 复用同一 helper。
- `_finish_background`：一次投影到 row/notice/event（`details`）。`details["messageId"]`
  **不新增字段**：由 `result.ledger_path` 的文件名 stem 派生（ledger 写失败则 `None`）；
  `_reap` 的 details 固定含 `v=3`、`messageId=None` 与可得 status/error，
  与 §2.5 一致。
- `_notice_text` 改收 `SubagentResult`（test 同步改）；`_reap`（`core/subagent.py:1191`）
  先构造 `SubagentResult(status=status, error=error, truncated=True)` 再传入，
  不再用旧标量参数。
- `close()`：解包 commit，写 handle 字段，先 `_write_meta` 再 `remove`；
  新增 `close_info(run_id) -> dict`（读 meta 的 branch/commit/commitError）供工具回显。
- `_write_meta`：写 `taskPacket`、`outputSchema`、`worktreeBranch`、`commit`、
  `commitError`、`degraded`。
- `resume()`：恢复 `request`（`SubagentTask.from_dict`）、`output_schema`、
  `worktree_branch`、`commit/commit_error/degraded`。
- `_open_handle`：`request` 存入 handle。

### 4.4 `ai_coding/tools/subagent_tool.py`
- `execute`：构造 `SubagentTask`（`context_mode=None`）；`runner.run(packet, ...)` /
  `_spawn_background(packet, ...)`。
- `_to_tool_result`：`data` 增加 `changed_paths/commit/commit_error/degraded`
  （不塞整包）。
- `_render`：新增提交失败/降级两行；改动文件已由 artifacts 行覆盖，不重复。
- `SubAgentListTool`：输出行加改动数/分支/提交失败/降级（有才显示）。

### 4.5 `ai_coding/tools/agents_tool.py`
- `SpawnAgentTool.execute`：构造 `SubagentTask`（`fork_turns`→`context_mode`），
  `spawn_background(packet, parent=parent, worktree_manager=..., origin_session=...)`；
  **必须保留 `parent`**（`fork_turns=none` 时仍要携带 `permissions`），否则
  规则层会丢（`tests/test_agents_tools.py:154-170`）。
- `ListAgentsTool`：行渲染加改动数/分支/降级。
- `CloseAgentTool`：`closed` 后调 `runner.close_info`，输出“提交 sha / 提交失败原因”。

### 4.6 `ai_coding/core/research_fanout.py`
- `_run_one`：`SubagentTask(brief=brief, context_mode="task_only", agent="explore",
  readonly=True, isolate=False, output_schema=RESEARCH_SCHEMA)`，`runner.run(packet, ...)`
  （`output_schema` 不能丢，否则 facts/schema_valid 消失，`test_research_fanout.py:61` 会红）。
- **不改** `ResearchPacket`（只读子无写回，不引入死字段）；测试只断言 task packet
  与 `result.artifacts==()`。

### 4.7 `ai_coding/core/events.py`
- `BackgroundTaskDone` 加 `details: dict[str, Any] | None = None`（投影，不是全文）。

### 4.8 `ai_coding/server/bridge.py` + 前端
- `background.done`：`details` 存在时增 `messageId/changedPaths/worktreeBranch/
  commit/commitError/degraded`；不存在（bash job）时保持旧形状，精确断言不破。
- `ai_coding/frontend/src/state/session.ts::background.done` 渲染分支（未合并）、
  改动数、提交失败/降级；无新键时原样输出。

### 4.9 `ai_coding/app/commands.py`
- `/tasks` 行加改动数/分支/降级（与列表行一致），避免三处列表不同源。

### 4.10 文档
- `docs/data-contracts.md`：packet v3、`BackgroundTaskDone.details`、`BackgroundTask` 行。
- `docs/subagent-handoff-contract.md`：v2 示例与版本说明改 v3。

## 5. TDD 测试用例（先写失败，再实现）

| 文件 | 用例 | 断言 |
|---|---|---|
| `tests/test_agent_packet.py`（新） | `test_task_objective_only_renders_verbatim` | `SubagentTask(SubagentBrief(objective="x")).render()=="x"`（不是 `[TASK]`） |
| | `test_task_with_sections_uses_the_frozen_envelope` | 含 context/scope → `[TASK]…` |
| | `test_task_round_trips_through_dict` | from_dict(to_dict()) 等值；brief 也往返 |
| | `test_packet_is_flat_and_versioned` | `v==3`；header 键 + `task` + result 17 键 |
| | `test_header_message_id_names_the_ledger_file` | ledger stem == message_id |
| `tests/test_worktree.py` | `test_commit_reports_failure_separately_from_no_change` | 成功 `(sha,None)`；无改动 `(None,None)`；坏仓库 `(None, 非空)` |
| `tests/test_subagent_handoff_v2.py` | 既有 3 例改 v3/二元组 | ledger v3、task、changed_paths；commit 失败 → `commit_error` + `_render` 告警 |
| | **新增** `test_commit_failure_with_changes_reaches_ledger_and_report` | fake 返回 `(None,"err")`；result.commit_error=="err"；ledger 合并后有；文本告警；`changed_paths` 仍在 |
| `tests/test_structured_handoff.py` | `test_the_packet_wire_carries_its_version` | v3；17 键不少；新增 3 键存在 |
| `tests/test_background_tasks.py` | `test_notice_carries_facts_changes_branch_and_degraded` | notice 含 facts/改动数/分支/降级/提交失败 |
| | `test_completion_event_carries_a_details_projection` | `details["v"]==3`、含 messageId/changed_paths/degraded/branch；**不含 conclusion** |
| | `test_row_projects_the_packet` | row 六字段 |
| | **新增** `test_background_turn_commits_before_it_settles` | fake manager；`_finish_background` 前 result.commit 非空 |
| `tests/test_subagent_runner.py` | `test_non_git_workspace_is_not_silently_shared` | manager 存在且 `can_isolate=False` → `degraded=="worktree_unavailable"` 且 `report_degraded` 被调 |
| | `test_no_manager_means_no_degraded` | `worktree_manager=None` → `degraded is None`（不误报） |
| | `test_request_is_persisted_in_ledger_and_meta` | ledger `task.objective`；meta `taskPacket` |
| | `test_timeout_still_writes_the_ledger_with_changes` | 超时结果有 ledger、`changed_paths` 不丢 |
| `tests/test_subagent_handles.py` | `test_close_persists_commit_before_removing` | meta 有 branch+commit；失败 → `commitError`；`close_info` 返回 |
| | `test_resume_rebuilds_task_packet_and_schema` | `handle.request` 与 `output_schema` 恢复 |
| | `test_resume_tolerates_old_meta_without_task_packet` | 旧 meta 无 `taskPacket`/`outputSchema` → `request is None`、不崩 |
| `tests/test_tool_subagent.py` | `test_data_carries_small_write_facts` | data 有 changed_paths/commit/commit_error/degraded，无整包 |
| | `test_render_warns_on_commit_failure_and_degraded` | 文本告警 |
| `tests/test_agents_tools.py` | `test_spawn_builds_a_task_packet_from_fork` | none→task_only、all→full；readonly/agent 进 packet |
| | `test_close_reports_the_commit` | close 输出含 sha/提交失败 |
| `tests/test_research_fanout.py` | `test_fanout_tasks_are_readonly_and_not_isolated` | packet 字段；`result.artifacts==()` |
| `tests/test_server_bridge.py` | `test_background_done_forwards_details` | data 含 changedPaths/branch/degraded；bash 事件形状不变 |
| `ai_coding/frontend/test/reducer.test.ts` | `test background.done names branch and degraded` | 行文本含分支/降级；无新键原样 |

**必须同步修改的既有断言（实现前先确认，不是迁就实现）**
- `tests/test_subagent_handoff_v2.py:73`（`_notice_text` 新签名）、`:156`（v3）。
- `tests/test_structured_handoff.py:328`（v3）。
- `tests/test_agents_tools.py:137-150,342-359`、`tests/test_tool_subagent.py:57,259-287`、
  `tests/test_mode_gate.py:376-378,420-421`（runner 收到 packet 后断言方式）。
- `tests/test_research_fanout.py:56-65,164`、`tests/test_parallel_research_tool.py:58-60`
  （fan-out 传 packet）。
- `tests/test_subagent_runner.py:111`、`tests/test_subagent_handoff_v2.py:125,180,212`
  （`commit_if_dirty` 二元组）。
- `tests/test_server_bridge.py:268-290`（`background.done` 新增键）。

兼容性：`tests/test_subagent_context_mode.py:133` 的 `to_dict()["context_mode"]` 不变。

## 6. 影响面与不改

- 触及：`domain/subagent.py`、`core/subagent.py`、`core/worktree.py`、`core/events.py`、
  `core/research_fanout.py`、`tools/subagent_tool.py`、`tools/agents_tool.py`、
  `app/commands.py`、`server/bridge.py`、前端 `state/session.ts`、相关测试与两份契约文档。
- 不改：gate/权限、plan 规则、`ParentContext`、`SubagentBrief` 上限与 `problems()`、
  worktree 分支命名、`/readonly` 语义、`BackgroundTaskDone` 既有字段名、
  `jobs.py` 的 bash 事件形状、`ResearchPacket`。
- 风险：
  1) ledger/meta 形状升级 → 旧文件仍可被 `read`，新代码不解析旧文件；
  2) 后台每 turn 提交 → 分支上会出现多个 commit（预期，工作不丢）；
  3) 非 git 环境会显示“降级” → 原本静默行为变可见（预期）；
  4) 事件 `details` 变大但受投影裁剪；前端旧事件兼容由条件键保证。

## 7. 验收

- 全量 `pytest` 0 failed；`ruff`/`mypy` 全绿；前端 `bun test` + `tsc`。
- 一条结算事实在 tool data / ledger / event details / notice / meta / list 六处一致，
  差异只允许“投影裁剪”，不允许语义不同。
- 写回：非 git/创建失败必现 `degraded`；有改动但提交失败必现 `commit_error`；
  两者在工具文本、后台 notice、`/tasks` 行与前端通知中都可见。
- 后台 turn 结束即有 commit；close 兜底不会丢 sha。
- 真机（qwen3.7-flash）：git 仓库里子代理写文件 → branch+commit+changed_paths；
  非 git 临时工作区 → 降级提示；readonly spawn 仍只读；plan 仍不出现编排工具。

## 8. 实施顺序（每步独立可评审）

1. **A1 契约**：`SubagentBrief.to_dict/from_dict`、`PacketHeader`、`SubagentTask`、
   `AgentPacket`、`SubagentResult` v3；`test_agent_packet.py`；旧 v2 断言改 v3。
2. **A2 写回事实**：`commit_if_dirty` 二元组；`changed_paths`/`degraded`/`commit_error`
   进结果；`_run_isolated`/`_attach_for_run`/`_commit_turn`；测试。
3. **A3 通道收敛**：`_run_turn` 信封化（ledger/meta）、`_finish_background`
   （row/notice/event details）、工具 data、fan-out、`/tasks`、bridge/前端；测试。
4. **A4 复核**：全量校验 + 独立子智能体评审 + 真机矩阵。

## 9. 实施记录（完成）

- **A1/A2/A3 全部落地**：`SubagentBrief.to_dict/from_dict`、`PacketHeader`、
  `SubagentTask`（objective-only 原文渲染）、`AgentPacket`、`SubagentResult v3`
  （`changed_paths/commit_error/degraded`）、`BackgroundTask` 投影字段；
  `commit_if_dirty` 二元组；`_split_task` packet 权威；`_run_turn` 唯一生成
  `message_id` 与 `_write_ledger`；`_commit_turn`（同步/后台两路）；`_supervised`/
  `_run_followup` 每轮提交后再结算；`close()` 先 meta 后 remove + `close_info`；
  `_finish_background` 的 row/notice/event(details) 同一投影；工具/fan-out/`/tasks`/
  bridge/前端全部接上。
- **独立子智能体评审**：无 P0；P1（多轮后台 facts 被后轮清空、文档 v2 承诺）已修；
  P2（close 用例无效、refresh 保留 task、attach 降级用例、fanout 重复断言、
  details 键统一、`BackgroundTask.summary` 死字段）已修。
- **真机发现并修复**：非 git 后台子代理的 row 有 `degraded`，但 ledger 没有——
  因为 `_run_turn` 先写盘、degraded 在之后才 `replace`，且旧代码只在有 branch 时
  `_refresh_ledger`。改为同步/后台在事实变化后**无条件合并刷新**，补两条用例。
- **全量校验**：`pytest` exit 0（28 skipped 均为 bwrap 不可用）；`ruff` 通过；
  `mypy` 173 文件无问题；前端 `bun test` 545 pass、`tsc` 干净。
- **真机（qwen3.7-flash）**：
  | 场景 | 结果 |
  |---|---|
  | git 仓库后台写文件（相对路径） | row/ledger：branch `ai_sub_*`、commit `aea9b41`、`changed_paths=['p2a_bg.txt']`、v3、task 原文 |
  | 非 git 工作区写文件 | row/ledger 一致：`degraded=worktree_unavailable`、`changed_paths=['p2a_nogit.txt']`、文件落在主工作区 |
  | readonly 会话 spawn(readonly=true) | 正常起子代理并 wait 完成（P1b 不回归） |
  | 后台事件 | `details` 含 v/messageId/changed_paths/branch/commit/degraded，不含 conclusion |
- **已知边界（如实记录）**：`changed_paths` 只含工具申报的 write/edit 改动，bash 直接
  改文件不在内（P2-B 的 git diff）；模型若在 task 里写父工作区绝对路径，隔离子代理会
  写到父区（工具 description 已警告，属既有行为）。
