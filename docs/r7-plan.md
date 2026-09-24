# R7 实施计划（内核瘦身 / L2 头尾窗口 / 人在环收口 / MCP 工具面）

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans`
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 R5/R6 之后重新长出来的结构债收回去，并修掉两条「每一步都在付钱 / 每一次都可能挂死」
的路径——行为不变（结构）、信息可回收（上下文）、等待可中断（人在环）、工具面可收窄（MCP）。

**Architecture:** 四个互相独立的切片。R7a 把 `process_input` 的 9 个回合状态收进
`TurnBooks`、把「回合在磁盘上留下什么」收进 `TurnCloser`；R7b 让 L2 的占位从「正文即丢」
变成「信封 + 头尾窗口 + 落盘可回收」；R7c 给审批等待套上回合钟并让 REPL 的提问复用同一个
读取器；R7d 让 MCP 的审批与注册都由 server 配置决定。

**Tech Stack:** Python 3.13（mypy 按 3.10 下限检查）、asyncio、pydantic v2、Typer/rich/
prompt_toolkit、pytest（asyncio mode=auto）、ruff、mypy；MCP 用官方 `mcp` 包（stdio）。

**Spec:** `docs/r7-analysis.md`（本文的每一处判断都出自它的 §1 实测与 §3 设计）

## Global Constraints

- **不改既有测试断言**，唯一例外是 R7b 的 L2 契约（`docs/r7-analysis.md` §12 Q2 授权后）：
  `tests/test_context_compression.py` 里两条 L2 断言按新契约改写，并在提交信息里写明理由。
- **行为不变**（R7a）：事件序列 golden 逐条一致，既有 864 条测试零改动全绿。
- **不新增 TurnStatus 成员**；新状态用现有的 `reason` 与新的 `ToolResult.error` 取值表达。
- **记忆/摘要/窗口都不进系统提示**；历史只在越过水位线时被改写（R3 的缓存前缀纪律）。
- **观测只写不改**；任何 elide 都必须可从磁盘取回（R7b 新增不变量）。
- **默认值不能改变未配置用户的行为**：`auto_approve=false`、`tools=[]`、
  `approval_timeout_seconds=0` 都等价于今天；唯一例外是 `connection_timeout=60`（Q6 已拍板）。
- 每个任务结束跑：`.venv/bin/python -m pytest tests/ -o addopts="" -q && .venv/bin/python -m ruff check . && .venv/bin/python -m mypy ai_coding`
- 提交信息含反引号时必须 `git commit -F <file>`。

---

## File Structure

| 文件 | 职责 | 任务 |
|---|---|---|
| `ai_coding/core/turn_books.py`（新） | 一个回合的账本：9 个计数器 + `note_*` + `snapshot()` | 1 |
| `tests/test_turn_books.py`（新） | 账本的纯单元用例 | 1 |
| `ai_coding/core/request_builder.py`（改） | 新增 `volatile_block` / `content_of`（从 loop 搬来） | 2 |
| `ai_coding/core/turn_close.py`（新） | 4 个 outcome 工厂 + `TurnCloser`（写回/落盘/交接/摘要环） | 3 |
| `tests/test_turn_close.py`（新） | 收尾的纯单元用例 | 3 |
| `ai_coding/core/agent_loop.py`（改） | `_begin_turn` / `_assemble` / `_finish_turn`；搬走 163 行 | 1–4 |
| `tests/golden/loop_events/*.json`（新） | 重构前的三条事件序列快照 | 4 |
| `tests/test_loop_refactor_equivalence.py`（新） | golden 比对 + 终态断言 | 4 |
| `ai_coding/core/context_compression.py`（改） | `elide_and_persist` + 新的 `placeholder_old_results` | 5 |
| `ai_coding/config/models.py`（改） | `l2_window_chars` / `l2_window_budget` / `approval_timeout_seconds` / `auto_approve` / `tools` / `connection_timeout` | 6, 9, 11 |
| `ai_coding/core/dispatch.py`（改） | `_ask` 受回合钟约束 + `approval_timeout` 结果 | 9 |
| `ai_coding/security/approval.py`（改） | `CLIApprovalCallback` 走 `asyncio.to_thread` | 9 |
| `ai_coding/security/user_prompt.py`（改） | `set_reader` | 10 |
| `ai_coding/ui/repl.py` + `ai_coding/cli.py`（改） | `run_repl(user_prompt=…)` 与 CLI 接线 | 10 |
| `ai_coding/security/permission_gate.py`（改） | `from_config` + MCP server 级 allow | 11 |
| `ai_coding/core/mcp.py`（改） | `tools` 过滤 + 报告被过滤的工具 | 12 |
| `scripts/r7_cases.py`（新） | 离线 9 + 真实 3 的验收脚本 | 13 |

---

### Task 1: 一个回合的账本（`TurnBooks`）

**Files:** create `ai_coding/core/turn_books.py`、`tests/test_turn_books.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_turn_books.py
from ai_coding.core.turn_books import TurnBooks


def test_books_start_empty():
    b = TurnBooks()
    assert (b.iterations, b.nudges, b.attempted_calls) == (0, 0, 0)
    assert (b.successful_calls, b.failed_streak, b.strategy_nudges) == (0, 0, 0)
    assert b.failures == [] and b.last_text is None and b.summaries == []


def test_note_outcomes_counts_attempts_and_successes():
    b = TurnBooks()
    b.note_outcomes([_ok("bash"), _ok("read"), _fail("edit")])
    assert (b.attempted_calls, b.successful_calls) == (3, 2)
    assert b.failed_streak == 0  # something worked → the streak resets
    assert b.failures == [("edit", "boom", "output")]


def test_note_outcomes_raises_streak_when_everything_failed():
    b = TurnBooks()
    b.note_outcomes([_fail("bash"), _fail("bash")])
    assert b.failed_streak == 1
    b.note_outcomes([_fail("bash")])
    assert b.failed_streak == 2


def test_note_outcomes_ignores_an_empty_batch():
    b = TurnBooks()
    b.note_outcomes([])
    assert b.failed_streak == 0 and b.attempted_calls == 0


def test_snapshot_feeds_the_policy_identically():
    b = TurnBooks(iterations=2, nudges=1, attempted_calls=3, successful_calls=0, failed_streak=2)
    snap = b.snapshot(iteration=3, tokens_spent=1000, open_items=())
    assert snap == TurnSnapshot(
        iteration=3, nudges=1, tokens_spent=1000, failed_streak=2,
        attempted_calls=3, successful_calls=0, strategy_nudges=0,
    )
```

（`_ok` / `_fail` 是文件内的小工厂，构造 `DispatchOutcome`，形状照
`tests/test_turn_books.py` 里的 `_outcome(name, success, error, output)`。）

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_turn_books.py -q`
Expected: `ModuleNotFoundError: ai_coding.core.turn_books`

- [ ] **Step 3: 实现**

把 `agent_loop.py::process_input` 里现在那几段**逐字搬运**过来：
`attempted_calls += len(outcomes)` / `successful_calls += len(succeeded)` /
`failed_streak = 0 if succeeded else failed_streak + 1` / `failures.extend(...)` 的
`item.call.name, str(item.result.error or "failed"), (item.result.output or "")[:160]`；
`snapshot()` 组装 `TurnSnapshot`（含 `tokens_spent` / `open_items` / `final_text`）。

- [ ] **Step 4: 跑测试确认通过，并跑全量**

Run: `.venv/bin/python -m pytest tests/test_turn_books.py -q && .venv/bin/python -m pytest tests/ -o addopts="" -q`
Expected: 新增 12 passed，总计 876 passed / 1 skipped（**此时产品代码还没接线**）

- [ ] **Step 5: 提交**

```bash
git add ai_coding/core/turn_books.py tests/test_turn_books.py
git commit -F - <<'MSG'
R7a-1: a turn's books, not nine loose locals

process_input held nine counters as locals across 228 lines; R6 added four of
them and R5's extraction could not stop it.  The counters now live in one pure
unit with the policy snapshot built in the same place.
MSG
```

---

### Task 2: 尾部两个小函数搬进 `request_builder`

**Files:** modify `ai_coding/core/request_builder.py`、`tests/test_request_builder.py`

- [ ] **Step 1: 写失败测试**

```python
def test_volatile_block_returns_none_for_empty_text():
    assert volatile_block("memory", "") is None
    assert volatile_block("memory", None) is None


def test_volatile_block_wraps_text_in_a_user_message():
    block = volatile_block("task", "<task-state>x</task-state>")
    assert block.name == "task" and block.message.role == "user"
    assert block.message.content == "<task-state>x</task-state>"


def test_content_of_tolerates_none():
    assert content_of(None) is None
    assert content_of(ChatMessage(role="user", content="hi", timestamp="")) == "hi"
```

- [ ] **Step 2: 跑测试确认失败** → `ImportError: cannot import name 'volatile_block'`
- [ ] **Step 3: 实现**：把 `agent_loop.py` 的 `_volatile` / `_content_of` 原样移过去并改名，
  更新 `agent_loop.py` 的两处调用（`_volatile("memory", …)` → `volatile_block("memory", …)`）。
- [ ] **Step 4: 跑全量**：`.venv/bin/python -m pytest tests/ -o addopts="" -q`（876 passed）
- [ ] **Step 5: 提交**：`R7a-2: the tail helpers live with the tail`

---

### Task 3: 回合收尾的唯一入口（`TurnCloser`）

**Files:** create `ai_coding/core/turn_close.py`、`tests/test_turn_close.py`

- [ ] **Step 1: 写失败测试**（10 条，逐条对应 `docs/r7-analysis.md` §7.2）

```python
async def test_close_adds_a_handoff_only_when_the_model_said_nothing():
    closer, session = _closer()
    silent = await closer.close(
        session, user_input="修好它", task=TaskState(goal="修好它"), turn_number=1,
        outcome=TurnOutcome(status=TurnStatus.TIMEOUT, text=None, iterations=2,
                            reason="turn deadline exceeded"),
        failures=[("bash", "tool_timeout", "")],
    )
    assert silent.text is None
    assert silent.handoff.startswith("[回合未完成")
    assert session.turn_digests[-1].turn == 1
```

```python
async def test_close_never_rewrites_the_text_it_was_given():
    closer, session = _closer()
    spoken = await closer.close(..., outcome=TurnOutcome(
        status=TurnStatus.COMPLETED, text="做完了", iterations=1, reason="model finished"))
    assert spoken.text == "做完了" and spoken.handoff == ""
```

- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 实现**：搬 `_terminal_outcome` / `_stopped_outcome` / `_aborted_outcome` /
  `_failed_outcome`（改名去掉下划线）、`_write_tool_messages`、`_checkpoint`、`_finalize`、
  `_save`、`_close_turn`；`TurnCloser.__init__(sessions, reporter, settings, *, emit)`。
  `agent_loop` 侧只留转发（`self.closer = TurnCloser(...)`，`last_artifacts` 变成
  `self.closer.artifacts`）。
- [ ] **Step 4: 跑全量**（876 passed；既有 `tests/test_loop_digest.py` 等必须零改动通过）
- [ ] **Step 5: 提交**：`R7a-3: what a turn leaves behind`

---

### Task 4: 循环重新变成一个循环（含 golden 取证）

**Files:** modify `ai_coding/core/agent_loop.py`；create `tests/golden/loop_events/*.json`、
`tests/test_loop_refactor_equivalence.py`

- [ ] **Step 1: 先录 golden（在改动之前）**

写 `tests/test_loop_refactor_equivalence.py` 的录制函数（`AI_CODING_RECORD_GOLDEN=1`
时才写文件），跑一次生成三条剧本的 JSON。剧本用假引擎：正常回合 / task-gate nudge /
迭代预算耗尽。**这一步只跑测试，不改产品代码。**

- [ ] **Step 2: 跑测试确认失败**（golden 已存在，但断言写好后应通过；把断言先写成
  `assert events == []` 走一次红，再改成读 golden）
- [ ] **Step 3: 重构 `process_input`**：`_begin_turn`（绑定 observer/trace、`turn_count+1`、
  重置 guard、`reporter.begin_turn`、`hooks(turn.start)`）→ 循环 → `_assemble(...) -> _Assembled`
  → `_finish_turn`（close + harvest + `turn.end` hooks + `return text or handoff`）；
  `_iteration_decision` 换成 `policy.decide_next_iteration(books.snapshot(...), budget_expired=…)`。
- [ ] **Step 4: 核验行数**：
  `wc -l ai_coding/core/agent_loop.py`（≤650）、
  `.venv/bin/python - <<'PY'` 复算 `process_input`（≤150）；若 >650，把
  `_hook_context/_fire_hooks` 移入 `core/turn_hooks.py`（-39 行）后重跑。
- [ ] **Step 5: 跑全量**：`pytest tests/ -o addopts="" -q` → **既有 864 条一条不改，全绿**
- [ ] **Step 6: 提交**：`R7a-4: the loop is a loop again`

---

### Task 5: L2 的信封、窗口与回收（`context_compression.py`）

**Files:** modify `ai_coding/core/context_compression.py`；create
`tests/test_context_compression_l2_window.py`

- [ ] **Step 1: 写失败测试**（14 条，`docs/r7-analysis.md` §7.4）

```python
def test_long_body_becomes_an_envelope_with_head_and_tail(tmp_path):
    body = "HEAD-" + "x" * 800 + "-TAIL"
    out = placeholder_old_results(
        [_tool(body, "c1"), _tool("r2", "c2"), _tool("r3", "c3")],
        keep_recent=2, window_chars=50, elide_dir=tmp_path,
    )
    text = out[0].content
    assert text.startswith("[Previous: used bash")
    assert "chars elided" in text
    assert "transcripts/elided/" in text          # the artifact path is in the envelope
    assert text.splitlines()[1].startswith("HEAD-")
    assert text.splitlines()[-1].endswith("-TAIL")


def test_short_bodies_are_left_alone():
    out = placeholder_old_results([_tool("r1", "c1"), _tool("r2", "c2")], keep_recent=1)
    assert out[0].content == "r1"  # too short to be worth a placeholder
```

- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 实现** `elide_and_persist(body, tool, elide_dir) -> str | None` 与新的
  `placeholder_old_results(...)`（四条规则：只改真能变小的 / 信封 + 头尾 / 预算从新到旧 / 配对字段不动）；
  `ContextManager` 读 `l2_window_chars`、`l2_window_budget`，把 `transcripts_root/"transcripts"/"elided"` 传下去。
- [ ] **Step 4: 跑全量**：**此时 `tests/test_context_compression.py` 的两条旧 L2 断言会红**
  （这是设计使然，Task 6 处理）——先记录红的两条编号与原因，不要动它们。
- [ ] **Step 5: 提交**：`R7b-1: elided is not lost`

---

### Task 6: 配置 + 文档 + 两条既有断言（契约变更）

**Files:** modify `ai_coding/config/models.py`、`docs/features.md`、`docs/data-contracts.md`、
`tests/test_context_compression.py`（**唯一被授权的既有断言修改**）

- [ ] **Step 1: 加配置**：`AIConfig.l2_window_chars: int = 200`、`l2_window_budget: int = 4000`
- [ ] **Step 2: 改两条 L2 断言**为不变量式（而不是文本式）：

```python
def test_placeholder_keeps_a_head_and_tail_window():
    msgs = [_tool("h" + "x" * 800 + "t", "c1"), _tool("r2", "c2"), _tool("r3", "c3")]
    out = placeholder_old_results(msgs, keep_recent=2)
    assert out[0].content.startswith("[Previous: used bash")
    assert out[0].content.endswith("t")
    assert len(out[0].content) < len(msgs[0].content)      # it really shrank
    assert out[0].tool_call_id == "c1"                      # pairing survives
```

（提交信息里必须写明：**这不是「测试写错了」，是 L2 的契约按 R7b 的设计改了**，
并引用 `docs/r7-analysis.md` §12 Q2。）

- [ ] **Step 3: 文档同步**：`docs/features.md` line 70 的 L2 行改成
  「旧工具结果 → 信封 + 头尾窗口（总预算 4000 字符），正文落盘 `transcripts/elided/` 可回收」；
  line 212 段落同步；`docs/data-contracts.md` 加两行配置。
- [ ] **Step 4: 跑全量**：`pytest tests/ -o addopts="" -q` 全绿（含改写后的两条 L2 断言）
- [ ] **Step 5: 提交**：`R7b-2: the envelope reaches the docs`

---

### Task 7: 70 轮真实 fixture 的预算回归

**Files:** modify `tests/test_context_compression_l2_window.py`

- [ ] **Step 1: 写测试**：用 `scripts/r6_cases.py::_synth_history` 构造 292 条消息，套真配置
  （`max_messages=50 / keep_recent_tool_results=3`），断言：
  ① est tokens ≤ `0.5 × max_context_tokens`；② 最新一条被清的工具结果的头 40 字符仍可见；
  ③ 每个占位里的路径都存在且内容与原文一致。
- [ ] **Step 2: 跑测试**（预期红 → 绿，取决于 Step 3 的边界）
- [ ] **Step 3: 按需微调**（只允许调窗口/预算的**分配顺序**，不允许取消「可回收」这一条）
- [ ] **Step 4: 记录实测数字**（压缩前后 est tokens、占位数、恢复的字符数）到
  `docs/r7-analysis.md` §11 的提交说明或 `docs/features.md`
- [ ] **Step 5: 提交**：`R7b-3: the budget holds on a real long history`

---

### Task 8: `memory_write`（**仅当 Q8 拍板「做」**，否则跳过）

**Files:** create `ai_coding/tools/memory_write_tool.py`、`tests/test_tool_memory_write.py`；
modify `ai_coding/tools/builder.py`、`ai_coding/memory/service.py`

- [ ] **Step 1: 写失败测试**（6 条）：写入后 `store.all()` 多一条 `pinned` 的 note；
  空文本报错；`kind` 校验；子智能体栈**不注册**它（没有人确认的写要更谨慎，沿用 R6c 的规则）。
- [ ] **Step 2–4: 红 → 绿 → 全量**
- [ ] **Step 5: 提交**：`R7e: the agent can write its own note`

---

### Task 9: 审批等待受回合钟约束

**Files:** modify `ai_coding/core/dispatch.py`、`ai_coding/security/approval.py`、
`ai_coding/config/models.py`；create `tests/test_dispatch_approval_timeout.py`

- [ ] **Step 1: 写失败测试**（6 条，`docs/r7-analysis.md` §7.5）

```python
async def test_expired_approval_returns_approval_timeout():
    d = _dispatcher(approval=_SlowApproval(seconds=5))
    events = _Recorder()
    d.observer = events
    outcomes = await d.dispatch_batch([_bash_call()], _ctx(), deadline=Deadline.after(1.0))
    assert outcomes[0].result.error == "approval_timeout"
    assert outcomes[0].halted is False
```

```python
async def test_a_sync_blocking_approval_no_longer_freezes_the_loop():
    # the R7c regression: approval sleeps in a thread, so the clock can still fire
    started = time.monotonic()
    await _run_one_turn(approval=CLIApprovalCallback(input_fn=lambda _: time.sleep(5) or "y"),
                        turn_timeout=1.0)
    assert time.monotonic() - started < 3.0
```

- [ ] **Step 2: 跑测试确认失败**（今天应得 `tool_timeout` 或耗时 ~5s）
- [ ] **Step 3: 实现**：
  `_ask` → `run_with_deadline(approval.approve(...), timeout=_approval_budget(deadline), label="approval")`；
  `DeadlineExpired` → 新的 `_approval_timeout(call, seconds)`；`approval_timeout_seconds`
  作为「没有回合钟时的兜底上界」（0 = 不设）；`CLIApprovalCallback.approve` 里
  `answer = (await asyncio.to_thread(self._input, prompt)).strip().lower()`。
- [ ] **Step 4: 跑全量**：`tests/test_approval.py` **零改动**通过（注入式 `input_fn` 兼容）；
  `tests/test_tool_dispatch.py` 也需要全绿（`not_approved` 的既有语义只在超时那一刻改变）
- [ ] **Step 5: 提交**：`R7c-1: an approval that nobody answers`

---

### Task 10: 一个终端只有一个读取器

**Files:** modify `ai_coding/security/user_prompt.py`、`ai_coding/ui/repl.py`、`ai_coding/cli.py`；
create `tests/test_repl_ask_user_pty.py`

- [ ] **Step 1: 写失败测试**：`test_set_reader_replaces_the_input_factory`（纯单元）；
  `tests/test_repl_ask_user_pty.py::test_a_typed_line_reaches_the_tool_result`（门控
  `AI_CODING_PTY=1`）：pty 里跑 `run_repl`，脚本写一行，断言 `session.messages` 里的 tool
  消息内容包含它。机制已在 `docs/r7-analysis.md` §1.4 实测可行。
- [ ] **Step 2: 跑测试确认失败**（今天 REPL 用的是 `input()`，pty 下竞争 stdin）
- [ ] **Step 3: 实现**：`CLIUserPromptCallback.set_reader(fn)`；`run_repl(..., user_prompt=None)`
  在创建 `PromptSession` 后 `user_prompt.set_reader(prompt.prompt)`；`cli.repl` 复用同一个 callback。
- [ ] **Step 4: 跑全量 + 门控用例**：
  `AI_CODING_PTY=1 .venv/bin/python -m pytest tests/test_repl_ask_user_pty.py -q`
- [ ] **Step 5: 提交**：`R7c-2: one reader for one terminal`

---

### Task 11: 信任是声明出来的（MCP 审批策略）

**Files:** modify `ai_coding/config/models.py`、`ai_coding/security/permission_gate.py`、
`ai_coding/cli.py`；create `tests/test_permission_gate_mcp.py`

- [ ] **Step 1: 写失败测试**（7 条，`docs/r7-analysis.md` §7.6）

```python
def test_mcp_tool_still_warns_by_default():
    gate = PermissionGate()
    assert gate.check("everything__read_file", {}) == PermissionDecision.WARN


def test_auto_approve_server_is_allowed():
    gate = PermissionGate.from_config(_cfg(auto_approve={"everything": True}))
    assert gate.check("everything__read_file", {}) == PermissionDecision.ALLOW
    assert gate.check("other__read_file", {}) == PermissionDecision.WARN
```

- [ ] **Step 2–3: 红 → 绿**（`from_config(app_cfg)` 读 `app_cfg.mcp.servers`；`cli._build_loop`
  把 `gate=` 传给 `AgentLoop`）
- [ ] **Step 4: 跑全量**
- [ ] **Step 5: 提交**：`R7d-1: trust is declared, not assumed`

---

### Task 12: 只注册你要用的工具

**Files:** modify `ai_coding/core/mcp.py`、`ai_coding/config/models.py`；modify `tests/test_mcp.py`

- [ ] **Step 1: 写失败测试**（4 条）：`tools=["get-sum"]` 时只出现一个 adapter；
  `[]` 时全部出现；名字匹配的是**远端名**（不是 `server__tool` 前缀）；`connection_timeout` 默认 60。
- [ ] **Step 2–3: 红 → 绿**（`open_all` 过滤；被过滤数量走 `report_degraded`/日志）
- [ ] **Step 4: 跑全量 + 真 stdio**：
  `AI_CODING_MCP_STDIO=1 .venv/bin/python -m pytest tests/test_mcp_stdio.py -q`
- [ ] **Step 5: 提交**：`R7d-2: register what you need`

---

### Task 13: 验收脚本与六份文档

**Files:** create `scripts/r7_cases.py`（体例照 `scripts/r6_cases.py`）；modify 六份文档

- [ ] **Step 1: 写离线 9 条**（`docs/r7-analysis.md` §8 表 1–9）
- [ ] **Step 2: 写真实 3 条**（`--real` 才跑：pty 的 `ask_user`、MCP 工具面对照、真模型的
  一次 `read`+`bash` 回合里的审批事件计数）
- [ ] **Step 3: 跑**：`python scripts/r7_cases.py` → 9/9；
  `python scripts/r7_cases.py --real --config config.local.yaml` → 3 条**只报告**（LLM 有随机性，
  判据是结构化事实，照 R6 的纪律）
- [ ] **Step 4: 文档**：`features.md` / `data-contracts.md` / `architecture.md` /
  `how-to-run.md` / `integration_testing.md` / `r7-backlog.md`
- [ ] **Step 5: 提交**：`R7: the acceptance script, and the docs the round changed`

---

## Self-Review（写完计划后自查）

1. **每个任务都有红 → 绿 → 全量 → 提交**的四步节律，且每个任务都能被单独审阅、单独回滚。
2. **新增 12 个测试文件中的每一个都对应到 `docs/r7-analysis.md` 的一节**（§7.1→Task 1、
   §7.2→Task 3、§7.4→Task 5、§7.5→Task 9/10、§7.6→Task 11/12）。
3. **契约变更只有一处**（Task 6 的两条 L2 断言），且写明了理由与授权来源。
4. **默认值审查**已写进 Global Constraints，并对照 §5 的数据契约表逐条核过。
5. **未验证项**：Task 10 的 pty 用例可能在某些终端环境下不稳 → 已门控并写进
   `docs/integration_testing.md`；Task 12 的 npx 冷启动数字沿用 R4 实测（本机未再拉 npx）。
6. **顺序依赖**：Task 1–4 必须先做（后面三个切片都要改 `process_input`）；
   Task 5–7、8、9–10、11–12 相互独立；Task 13 最后。

---

## 执行状态（实施后回填，含与计划的偏差）

四件事全部落地。质量门禁在收尾时跑：**942 passed / 2 skipped**、`ruff` 0、`mypy` 0（91 文件）、
`scripts/r7_cases.py` 11/11（离线）+ 3/3（真实：pty / MCP 工具面 / 真模型回合）。

| Task | 状态 | 提交 | 与计划的偏差 |
|---|---|---|---|
| 1 账本 | ✅ | `R7a-1` | 测试写了 14 条（计划 12 条：补了「空批次不重置 streak」「成功不进失败表」） |
| 2 尾部小函数 | ✅ | `R7a-2` | —— |
| 3 收尾单元 | ✅ | `R7a-3` | 多了一条「保存失败只报不抛」的用例；`TurnCloser` 不接 `emit`（它只用 reporter） |
| 4 循环瘦身 | ✅ | `R7a-4a` + `R7a-4` | **多搬了三处**才到 ≤650：`TurnFrame` 进 `turn_books`、`RequestAssembler` 新建、`TurnHooks` 新建、`SafeObserver` 进 `observers`。最终 **578 行 / `process_input` 139 行**（目标 ≤650 / ≤150） |
| 5–7 L2 | ✅ | `R7b` | 13 条用例合一提交（计划拆 3 个）；**改了一条既有断言**（见下） |
| 8 `memory_write` | ⏸ | —— | Q8 未拍板，跳过（仍是候选） |
| 9–10 人在环 | ✅ | `R7c` | pty 用例门控；**多了一个默认跑的 stdin 回归用例**（实施时发现真正的坏场景是脚本管道，见 §1.4 的更正） |
| 11–12 MCP | ✅ | `R7d` | 多加了 `scripts/mcp_many_tools_server.py`（让工具面测量可复现）；**改了 `test_mcp_defaults` 的超时断言**（见下） |
| 13 验收脚本 | ✅ | `R7d` 之后 | 做了 11 离线 + 3 真实；没有单独做「真模型的 tools_chars/审批事件计数」那条（`turn_still_works` 报告了 `tools_chars`，够用） |

**两处既有断言被改**（都是本轮**按设计重定义**的契约，不是测试写错）：

1. `tests/test_context_compression.py::test_placeholder_marks_older_keeps_recent`：
   旧断言钉的是 `[Previous: used bash]` 这个「正文即丢」的字面量，正是 R7b 要治的病；
   改成不变量式（信封 + 路径 + 配对字段 + 确实变小），意图不变。
2. `tests/test_config_loader.py::test_mcp_defaults`：`connection_timeout` 30 → 60（Q6 已拍板）。

**计划外发现（值得记一笔）**：

- `max_messages` 的默认值文档里写的是 50，代码里是 **400**（R3b 之后它只在预算被突破时才触发），
  这次顺带把 `docs/data-contracts.md` 改正了 —— 它决定了 L1 会不会先动手，也就决定了 L2 的舞台有多大。
- **R6c 的未验证项其实没坏**：真人手里 `input()` 一直能读到答案；坏的是**脚本一次性喂多行**
  （prompt_toolkit 先占住 stdin → 工具读到 EOF）。为避免下次误传，更正写进了 `docs/r7-analysis.md` §1.4，
  并固化成默认跑的回归用例。
