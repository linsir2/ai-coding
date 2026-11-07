# B5（子代理 / 后台）修复实现

- 批次：B5 子代理 / 后台（W1）
- 项目根：`/workspace/ai-coding`
- 覆盖：SUB-01 ~ SUB-05 + MCP-10b（仅注释）
- 状态：已实现并验证；**评审整改完成**（SUB-05 回退为 **DEFERRED**），未 commit
- 依据：`01-research.md`、`02-design-review.md`（"裁决"列逐条落实）

---

## 1. 逐项改动

### SUB-01 产物路径指向已删除的 worktree

文件：`ai_coding/tools/subagent_tool.py`

- 新增 `_artifact_lines(result: SubagentResult) -> list[str]`（第 51 行）：
  - 无产物 → `[]`（不打印空标题）。
  - 无 `worktree_branch` → `["产物:", "  - path", ...]`，与旧输出**逐字节一致**（回归护栏）。
  - 有 branch + `commit` → 标题注明"隔离工作区已删除，路径相对分支 {branch}"。
  - 有 branch + 无 `commit` → 标题写"未提交；路径相对分支 {branch}"（不声称已提交）。
  - 有 branch + `commit_error` → 标题写"提交失败：{error}，以下文件已丢失"。
  - 上限沿用 `artifacts[:10]`。
- `_render` 第 115 行改为 `lines.extend(_artifact_lines(result))`。
- **未**新增 `if not path: continue`（评审裁决 #7）。
- 未改 `data["workdir"]`（仍是诊断字段；`tests/test_subagent_runner.py:431` 依赖）。

### SUB-02 停机关闭子资源 / 空闲 handle 剪枝 / `stop` 任务引用

文件：`ai_coding/core/scheduler.py`

- 常量 `_SHUTDOWN_CLOSE_SECONDS = 20.0`（第 58 行）。
- 新增 `Scheduler.shutdown()`（第 1200 行）：
  - **第一行** `self._closed = True`；
  - 物化 `_tasks` 后逐个 `cancel()`，`gather(..., return_exceptions=True)`，再 `clear()`；
  - 对 `_live_run_ids()` 逐个 `await asyncio.wait_for(close(run_id), 20s)`，异常吞掉（与 `aclose` 既有 best-effort 风格一致）。
- `Scheduler.aclose()`（第 1228 行）开头 `await self.shutdown()`；`server/world.py` **未改**（现有 `world.aclose → scheduler.aclose` 顺序已在主 loop 关闭前）。
- `run()` / `spawn()` / `_begin()` 在 `_closed` 时抛 `RuntimeError("scheduler is closed")`（拒绝新准入；`_ensure_reaper` 因 `_closed` 不再重启）。
- 新增 `_live_run_ids()`（第 1250 行）：`task_list()` 的 `run_id` ∪ `_request_runs.values()`，去重保序。
- `stop()`（第 693 行）：`stop:{run_id}` 入 `_tasks`，done callback 自清理；无 loop / 无 `stop_run` 时仍走同步 `cancel`，`-> bool` 契约不变。

文件：`ai_coding/core/subagent.py`

- `AgentHandle.idle_since: float = 0.0`（第 224 行）。
- 所有转为 `idle` / `interrupted` 的路径刷新 `idle_since`（第 899、935、958、1022、1066、1131、1473、1708 行），覆盖：`_schedule_turn` 失败、`_run_followup` 两端、`interrupt`、`resume`（内存与磁盘两条）、`_supervised`、`_reap` 兜底。
- 常量 `_MAX_IDLE_HANDLE_SECONDS = 1800.0`（第 107 行，模块常量，不新增 settings 键）。
- `_prune_background()` 先调 `_retire_idle_handles()`，原有按行裁剪不动。
- `_retire_idle_handles()`（第 1373 行）：只看 `status in ("idle","interrupted")` + `idle_since`，`_tasks["retire:{id}"]` 去重，返回 `list[asyncio.Task]` 供调用方显式 await；`_retire`/`_retire_done` 负责执行与清理。无运行 loop 时 `break`，不退休。
- `close()` 单飞保护（第 1152 行）：`_closing: set[str]` 入口检查，`finally` 移除；并发第二次直接 `return False`。

### SUB-03 同 idempotency_key 并发可双跑

文件：`ai_coding/core/scheduler.py`

- `run()` 在 `create_task` **前** `_idem_put(key, digest, request_id, status="running")`（第 175 行）；创建失败 / `CancelledError` / 异常路径 `_idem_forget`（第 184、195、199 行）。
- 新增 `_idem_forget(key)`（第 1102 行）。
- `spawn()` **未改**：从 lookup 到 `_idem_put` 全程无 `await`，单事件循环无竞态（评审裁决 #3）。
- 成功路径仍由 `_settle` 写 `result` 覆盖在途登记，"已完成 key 返回缓存"语义不变。

### SUB-04 自定义 agent 对模型不可见

文件：`ai_coding/ai/prompt.py`

- `render_agents_block(specs)`（第 126 行）：跳过 `hidden` / 空 id；行格式 `- {id} — {description or name}`；单行 ≤ `_AGENT_LINE_CHARS = 120`（截断补 `…`）；整段 ≤ `_AGENTS_TOTAL_CHARS = 600`（含头尾），超预算的行直接丢弃。
- `PromptAssembler.build(..., agents: Iterable[AgentSpec] | None = None)`（第 187 行）；`<agents>` 插在 `<skills>` **之后**（第 238-246 行）。
- 仅当 `tools is None` 或表面含 `subAgent` / `spawn_agent` 时渲染；`agents=None` / `[]` / 全 hidden → 不输出，逐字节不变。
- `__all__` 增加 `render_agents_block`。
- `core/agent_loop.py`（仅调用点，第 3533-3565 行）：`getattr(self, "agents", None)` + `callable(subagents)` 防御后传 `agents=`；空目录自然渲染为 `""`。（评审整改后已回退 `surface` 局部变量抽取，见 §7。）

### SUB-05 子代理 gate 固定主 workspace（评审整改后回退，DEFERRED）

文件：`ai_coding/core/subagent.py`

- **最终状态：DEFERRED**。原实现（`workspace or self._workspace`；`gate=self._child_gate(workdir or self._workspace)`）会因 `GrantStore` 按 workspace 读盘而让主树 "always" 授权对子代理整体失效；评审整改已回退，详见 §7。

### MCP-10b（仅注释）

文件：`ai_coding/tools/builder.py` 第 138 行，`build_tools` 闭包内注明"子 agent 不注册 MCP 工具"。无逻辑改动。

### `ai_coding/core/worktree.py`

白名单允许但**无需改动**：本批只引用其 `temporary()` 清理语义（SUB-01 的证据与既定行为）。

---

## 2. 评审裁决落实情况

| # | 裁决 | 落实 |
|---|---|---|
| 1 | `idle_since`，所有转 idle 刷新，TTL 只读它，`_restore_row` 不算起点 | 已做（8 处转场 + `_retire_idle_handles` 只读字段） |
| 2 | `shutdown` 第一行置 `_closed` 并拒绝准入，再取消 | 已做（`run`/`spawn`/`_begin` 三处拒绝） |
| 3 | 风险文案改写 | 见 §5 |
| 4 | `_closing` 防并发 close | 已做（`close` 入口 + `finally`） |
| 5 | 修复前即绿标"回归护栏" | 见 §4 |
| 6 | TTL 用例 await 退休任务；stop 用轮询 | 已做（`_retire_idle_handles` 返回 task；stop 轮询上限 50×10ms） |
| 7 | 不加 `if not path: continue`；无 branch 输出不变；有 branch 无 commit 写"未提交" | 已做 |
| 8 | `self.agents.subagents()` 加 `getattr` 防御 | 已做（`agent_loop.py`） |
| 9 | SUB-05 未提交规则不可见写入已知限制；仅 workdir 非空切换 | 已做 |

---

## 3. 测试证据

命令（新增用例见 §4 文件）：

```text
$ .venv/bin/python -m pytest tests/test_subagent_runner.py tests/test_scheduler.py \
    tests/test_tool_subagent.py tests/test_prompt.py tests/test_subagent_guards.py
157 passed in 3.07s（EXIT=0）

$ .venv/bin/python -m pytest tests/test_subagent_runner.py tests/test_scheduler.py \
    tests/test_tool_subagent.py tests/test_prompt.py tests/test_subagent_guards.py \
    tests/test_subagent_handles.py
183 passed in 3.83s

$ .venv/bin/python -m pytest <相邻 prompt/agent_loop 9 个文件>
184 passed in 4.26s

$ .venv/bin/ruff check ai_coding tests/test_subagent_runner.py tests/test_scheduler.py \
    tests/test_tool_subagent.py tests/test_prompt.py tests/test_subagent_handles.py
All checks passed!（EXIT=0）

$ .venv/bin/mypy ai_coding
Success: no issues found in 186 source files（EXIT=0）
```

新增用例分布：

- `tests/test_tool_subagent.py`（SUB-01，4 条）：隔离产物已删除、提交失败已丢失、有 branch 无 commit 写"未提交"、无 branch 文案不变。
- `tests/test_scheduler.py`（SUB-02/03，9 条）：
  - shutdown：关闭全部活动子代理、取消在途 turn、拒绝新准入、幂等；
  - stop：追踪 `stop:{run_id}` 任务并在完成后自清理；
  - idempotency：同 key 并发第二次被拒（**唯一红转绿**）、失败释放 key、取消释放 key、完成后走缓存。
- `tests/test_subagent_handles.py`（SUB-02，5 条）：TTL 退休、近期 idle 保留、`idle_since` 是唯一时钟（resume 重置）、并发 close 只生效一次、close 释放 loop/handle。
- `tests/test_prompt.py`（SUB-04，7 条）：有/无 delegation 工具、空列表、全 hidden、预算上界、位于 `<skills>` 之后、默认字节不变。
- `tests/test_subagent_runner.py`（SUB-05，3 条）：gate 绑定 worktree、降级/main 保持主树、worktree 下 config deny 仍生效。

---

## 4. 回归护栏（修复前即为绿）

- SUB-01：`test_render_keeps_plain_artifact_list_without_branch`。
- SUB-02：`test_a_recent_idle_handle_is_kept`、`test_close_releases_the_child_loop_and_handle`。
- SUB-03：`test_a_failed_run_releases_the_key_for_retry`、`test_a_cancelled_run_releases_the_key_for_retry`、`test_a_completed_run_still_returns_the_cached_result`（均为新登记逻辑的防回退护栏；`01-research.md` 的失败/取消两例在修复前本为绿）。
- SUB-04：`test_agents_block_is_omitted_without_the_tool`、`test_agents_block_default_is_byte_identical`。
- SUB-05：`test_a_degraded_child_keeps_the_main_workspace_gate`。

---

## 5. 偏离、风险与已知限制

### 偏离

1. **SUB-02 `idle_since` 同时覆盖 `interrupted`**：评审原文为"所有转为 idle 的转场刷新"。TTL 的候选集合包含 `interrupted`，若它不刷新会继承更早的 idle 起点而被提前退休，因此一并刷新。属裁决语义的补全，非方向改变。
2. **`_retire_idle_handles` 返回 task 列表**，但 `_prune_background()` 签名保持 `-> None`（评审要求"签名不变"）；测试直接调用前者并 `await`。
3. **`_child_gate` 测试用 `read` 而非 `write`**：`write` 属 WARN 工具，规则层会先返回"requires confirmation"，`_outside_resource_reason` 只在规则全 ALLOW 时生效；`read` 才能稳定观察到 workspace 边界（不改变被测逻辑）。
4. **`agent_loop.py` 的白名单边界**：只改 `_build_instructions` 的 `prompt.build` 调用点（提取 `surface` 局部变量 + 传 `agents=`），未触碰其他逻辑。

### 风险（按评审裁决 #3 改写）

- **shutdown 20s 上限**：`close()` 内的 `commit_if_dirty` 可能跑 git。超时被切掉时**遗留孤儿 worktree**，但不会丢失已提交内容——每轮结束的 `_commit_turn` 已先行提交（`core/subagent.py:1387`、`561`），close 的提交只是安全网。
- **前景取消未 commit**：`_run_isolated` 用 `temporary()`，取消时未提交的写入随目录删除，属**既有语义**，本批未改变。
- **SUB-05 grant 维度变化（已回退，见 §7）**：原改动把 gate 的 workspace 从主树切到 worktree，会让子代理 dispatch gate 读不到操作者在该主树上的 "always" 授权。评审认定这是授权回归，已回退为 DEFERRED。
- **SUB-04 每请求成本**：主会话每请求多 ≤600 字符（约 150-200 tokens）；子代理表面无 `subAgent`，不输出。

### 已知限制

1. **空闲 handle 只在 `spawn_background` / `resume` 触发的 prune 中退休**：长期不再 spawn 的静默会话保留其空闲子代理（有界，评审接受）。
2. **worktree 未提交的项目规则不可见**（SUB-05，`_child_gate` 注释已写明）。
3. **`data["workdir"]` 仍指向已删除目录**：诊断字段，未改（会触及未在白名单的 `domain/subagent.py`，且现有测试断言其值）。
4. **idle TTL 不可配置**：模块常量 1800s（避免 `config/models.py` 越界与死配置）。
5. **20s 上限下大仓库 git 最坏耗时未实测**：留人工验证（评审非阻塞项）。
6. **`core/scheduler.py` 的 `_Record` 缺 `is_terminal/branch/commit` 字段**（`01-research.md` §9.6）不在本批范围，未修。

---

## 6. 改动文件清单

代码：

- `ai_coding/tools/subagent_tool.py`（SUB-01）
- `ai_coding/core/scheduler.py`（SUB-02、SUB-03）
- `ai_coding/core/subagent.py`（SUB-02、SUB-05）
- `ai_coding/ai/prompt.py`（SUB-04）
- `ai_coding/core/agent_loop.py`（SUB-04，仅 `PromptAssembler.build` 调用点）
- `ai_coding/tools/builder.py`（MCP-10b，仅注释）

测试：

- `tests/test_tool_subagent.py`、`tests/test_scheduler.py`、`tests/test_subagent_runner.py`、`tests/test_prompt.py`、`tests/test_subagent_handles.py`

未改：`ai_coding/core/worktree.py`、`ai_coding/server/world.py`。

---

## 7. 评审整改（REQUEST-CHANGES）

### P1-1 SUB-05 workspace 切换回退（→ DEFERRED）

- `_child_gate()` 恢复无参数签名，gate 固定 `workspace=self._workspace`；`_build_loop_for` 调用点恢复 `gate=self._child_gate()`。
- 注释写明已知限制：worktree 内绝对路径的项目规则不可见；把 gate 切到 worktree 会让主树的 "always" 授权整体失效（`GrantStore` 按 workspace 读盘）。修复需 gate API 同时携带两个根并迁移 grants。
- 测试 `test_the_child_gate_is_bound_to_the_worktree` 改为 `test_the_child_gate_does_not_switch_to_the_worktree`（断言 worktree 路径被判 "outside workspace"）；其余两条调用点去掉 workspace 实参。
- **SUB-05 最终状态：DEFERRED**（本批不修授权回归）。

### P1-2 退休 task 异常与重复退休

- `_retire_done` 消费 `task.exception()`（取消则直接返回），失败时把 `idle_since` 重置为当前时间，避免每次 spawn 重复退休。
- `_retire` 在 `close` 返回 False 时同样重置 `idle_since`。
- 新增 `SubAgentRunner.drain_retirements()`；`Scheduler.shutdown()` 在关闭后 await 它（受总时限约束）。

### P1-3 close 复核 resting 状态

- `close(target, *, retiring: bool = False)` 在 `_closing` 检查后、同一同步块内复核 retirement 调用：`status not in ("idle","interrupted")` 或 `turn_task is not None` 直接返回 False。显式操作者 close 仍可取消 running turn（`test_close_and_reap_release_the_same_slot` 依赖该语义）。
- 退休路径 `_retire` 以 `retiring=True` 调用。

### P2-1 shutdown 只关非终态 + 总时限

- `_live_run_ids()` 现在跳过终态记录/行：有 `_Record` 时看 `state in TERMINAL_EVENTS`，只有行时看 `row.status != "running"`；终态行不再 commit/删 worktree/改写为 cancelled。
- 新增 `_SHUTDOWN_TOTAL_SECONDS = 30.0`，每个 close 的 `wait_for` 取 `min(20s, 剩余总预算)`；预算耗尽即停止。

### P2-2 fanout `_closed` 守卫

- `fanout()` 入口与 `_begin_fanout()` 均拒绝 `_closed` 状态，shutdown 后不再产生永久 claimed 记录。

### P2-3 预登记 running 覆盖

- `_settle()` 对已存在（预登记）的 idem 条目覆盖 `status=event`，有 result 时一并写入，有 error 时记录 error；不再留下永久 `running`。
- `run()` 的缓存命中分支区分"终态但无结果"（明确 `IdempotencyConflict`：already finished without a cached result）与真正在途；异常/取消路径继续 `_idem_forget`。

### P2-4 prompt 预算与越界重构

- `ai/prompt.py`：`render_agents_block` 的超预算分支由 `break` 改为 `continue`，跳过该条继续尝试后续短行（与注释一致）。
- `ai_coding/core/agent_loop.py`：回退 `surface` 局部变量抽取，只保留 `PromptAssembler.build(agents=...)` 一处改动；`tools=` 恢复内联 `self._prompt_tools(...)`。

### P2-5 空断言

- `tests/test_scheduler.py::test_shutdown_is_idempotent` 增加 `_closed`、`_tasks`、未关闭终态行三条断言。
- `tests/test_subagent_handles.py::test_a_recent_idle_handle_is_kept` 增加 `handle.status == "idle"` 断言。

### 整改后验证

```text
$ .venv/bin/python -m pytest tests/test_subagent_runner.py tests/test_scheduler.py \
    tests/test_tool_subagent.py tests/test_prompt.py tests/test_subagent_handles.py \
    tests/test_subagent_guards.py -q
183 passed

$ .venv/bin/ruff check ai_coding tests/test_subagent_runner.py tests/test_scheduler.py
All checks passed!

$ .venv/bin/mypy ai_coding
Success: no issues found in 186 source files
```
