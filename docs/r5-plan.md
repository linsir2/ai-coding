# R5 实施计划（循环内核拆分）

> **For agentic workers:** 按任务顺序逐条执行，每个任务自带测试循环（先红 → 再绿 → 提交）。步骤用 `- [ ]` 跟踪。

**Goal:** 把 `core/agent_loop.py`（788 行 / `process_input` 229 行）拆成四个可单独阅读与测试的单元，行为与契约**逐字不变**。

**Architecture:** 抽出「对外陈述（事件/账本/降级）」「记忆侧工作」「压缩与强制压缩」「请求拼装（易失尾部）」四块；**迭代本⾝、模型步调用、工具分发、持久化、`_finalize` 留在循环里**（YAGNI 边界见 spec §2.2）。

**Tech Stack:** Python 3.10+ / dataclasses / pytest / ruff / mypy(strict)。**不新增依赖、不改公开构造 API。**

**Spec:** [`docs/r5-analysis.md`](r5-analysis.md)（现状实测、接口契约、风险自核、决策点都在那里；本计划不重复抄契约）

## Global Constraints

- 既有 **766 例测试全绿**；`ruff check .` 0；`mypy ai_coding`（strict，py3.10）0；`scripts/trace_cases.py` 6/6。
- **既有测试一行不改**。任何一条既有断言因拆分而失败 = 拆出了行为差异 → 回滚实现查原因，**不许改断言**。
- 不得改动这些名字/签名：`process_input(..., observer=)`、`last_outcome`、`last_artifacts`、`dispatcher`、`memory_status()`、`compact_session()`、`trace`、`aclose()`。
- `process_input(observer=...)` 的**每次调用覆盖观察者**语义必须保留。
- 事件**顺序**不变：`ContextAssembled` 在模型步之前；`Degraded(observer)` 在 `TurnFinished` 之前；`_finalize` 内先落盘再上报。
- 依赖单向：四个新单元 → `domain`/`core.events`/`infra.deadline`/`core.trace`；**不得**反向 import `agent_loop`。
- 命令一律在仓库根执行，Python 用 `.venv/bin/python`。

---

## 文件结构（先锁定边界）

| 文件 | 职责 | 任务 |
|---|---|---|
| `ai_coding/core/turn_report.py`（新） | `report_degraded()` + `TurnReporter`（事件/账本/降级） | T1–T2 |
| `ai_coding/core/agent_loop.py` | 接 `TurnReporter`，删 2 个发射方法 + 6 处内联 `Degraded` | T1–T2 |
| `ai_coding/core/mcp.py`、`core/subagent.py` | 删各自的 `_degrade`，改调共享函数 | T3 |
| `ai_coding/core/memory_turn.py`（新） | `TurnMemory`（块/收割/状态） | T4–T5 |
| `ai_coding/infra/deadline.py` | 加 `remaining_of(deadline)` | T4 |
| `ai_coding/core/compaction.py`（新） | `Compactor`（按预算压缩 / 强制压缩） | T6–T7 |
| `ai_coding/core/request_builder.py`（新） | `TailBlock` / `build_tail` / `tool_context` | T8 |
| `ai_coding/core/agent_loop.py` 头部 | 重写过期 docstring | T9 |
| `docs/architecture.md`、`docs/README.md` | 模块职责表 + 状态 | T9 |

---

## 提交 1：`turn_report`（风险最低，先做）

### Task 1: 共享的降级上报

**Files:** Create `ai_coding/core/turn_report.py`；Test `tests/test_turn_reporter.py`
**Interfaces — Produces:** `report_degraded(observer, component, reason, fallback="") -> None`

- [ ] Step 1: 写测试——发出一条 `Degraded`；观察者抛异常时**不抛**；`observer=None` 时静默
- [ ] Step 2: `Run: .venv/bin/python -m pytest tests/test_turn_reporter.py -q` → FAIL
- [ ] Step 3: 实现 `report_degraded`（best-effort，吞异常记 debug）
- [ ] Step 4: `Run` → PASS
- [ ] Step 5: 本任务不单独提交（与 Task 2 合并为 `R5-1`）

### Task 2: `TurnReporter`

**Files:** Modify `ai_coding/core/turn_report.py`；Modify `ai_coding/core/agent_loop.py`；Test `tests/test_turn_reporter.py`
**Interfaces — Consumes:** `BudgetLedger`、`canonical_request`/`prefix_shared`/`digest_of`、`estimate_tokens`、事件类型
**Interfaces — Produces:** `TurnReporter(*, tools_text, emit)` + `tools_chars` / `ledger` / `tokens_spent()` / `begin_turn(observer)` / `report_context(...)->str` / `report_step(*, index, step, latency_ms)` / `report_degraded(...)` / `finish_turn(outcome, observer)->TurnOutcome`

- [ ] Step 1: 写契约测试：① `report_context` 字段与手算一致（含 `tools_chars`）；② 返回值与 `canonical_request` 一致且能算前缀共享比；③ `report_step` 喂账本并带 cache 明细；④ `finish_turn` 补 `Degraded(observer)` 再发 `TurnFinished` 且 outcome 带 usage；⑤ 无失败时不发多余事件
- [ ] Step 2: `Run` → FAIL
- [ ] Step 3: 实现 `TurnReporter`；`agent_loop` 用 `self.reporter` 替换 `_emit_context_assembled` / `_emit_step_finished` / `_observer_failure_count` 与 6 处内联 `Degraded`；`_finalize` 改调 `finish_turn`
- [ ] Step 4: `Run` → PASS；再跑 `.venv/bin/python -m pytest tests/ -q`（**766 例必须全绿**）、`ruff`、`mypy ai_coding`
- [ ] Step 5: `git commit -m "R5-1: the turn reporter — events, ledger and degradation in one unit"`

### Task 3: 统一跨模块的降级上报

**Files:** Modify `ai_coding/core/mcp.py`、`ai_coding/core/subagent.py`；Test `tests/test_mcp.py`、`tests/test_subagent_scoped_events.py`（既有）

- [ ] Step 1: 删掉 `MCPManager._degrade` 与 `SubAgentRunner._degrade`，改调 `report_degraded(self._observer, ...)`
- [ ] Step 2: `Run: .venv/bin/python -m pytest tests/test_mcp.py tests/test_subagent_runner.py tests/test_subagent_scoped_events.py -q` → PASS（既有断言不改）
- [ ] Step 3: 全量门禁 + `scripts/trace_cases.py`
- [ ] Step 4: `git commit -m "R5-2: one degradation reporter instead of three copies"`

---

## 提交 2：`memory_turn`

### Task 4: `remaining_of` 归位

**Files:** Modify `ai_coding/infra/deadline.py`；Test `tests/test_deadline.py`（扩）

- [ ] Step 1: 测试——`remaining_of(None) is None`；有 deadline 时返回剩余秒数且**不为负**
- [ ] Step 2: `Run` → FAIL
- [ ] Step 3: 实现并让 `agent_loop._remaining` 改为委托（保持既有调用点不变）
- [ ] Step 4: `Run` → PASS

### Task 5: `TurnMemory`

**Files:** Create `ai_coding/core/memory_turn.py`；Modify `ai_coding/core/agent_loop.py`；Test `tests/test_memory_turn.py`
**Interfaces — Produces:** `TurnMemory(memory, *, emit, save)` + `block()` / `harvest(session, *, deadline, summaries, outcome)` / `status()`

- [ ] Step 1: 写契约测试（8 条，见 spec §6）
- [ ] Step 2: `Run` → FAIL
- [ ] Step 3: 实现；`agent_loop` 删除 `_memory_block` / `_harvest`，`memory_status` 变 1 行委托
- [ ] Step 4: 全量门禁
- [ ] Step 5: `git commit -m "R5-3: the memory-facing half of a turn lives in its own unit"`

---

## 提交 3：`compaction` + `request_builder` + 文档

### Task 6: `Compactor`

**Files:** Create `ai_coding/core/compaction.py`；Modify `ai_coding/core/agent_loop.py`；Test `tests/test_compaction.py`
**Interfaces — Produces:** `Compactor(context, *, emit)` + `compress(history, *, deadline, on_summary=None)` / `compact_now(session, *, timeout)`

- [ ] Step 1: 写契约测试（7 条，见 spec §6）
- [ ] Step 2: `Run` → FAIL
- [ ] Step 3: 实现；`agent_loop` 删 `_compress`，`compact_session` 变薄委托
- [ ] Step 4: 全量门禁

### Task 7: `request_builder`

**Files:** Create `ai_coding/core/request_builder.py`；Modify `ai_coding/core/agent_loop.py`；Test `tests/test_request_builder.py`
**Interfaces — Produces:** `TailBlock(name, message)` + `build_tail(blocks)` + `tool_context(*, task, workspace, session, history, instructions, emit)`

- [ ] Step 1: 写契约测试（5 条，见 spec §6）
- [ ] Step 2: `Run` → FAIL
- [ ] Step 3: 实现；`process_input` 的尾部三块改成 `build_tail([...])`，`_tool_context` 删除
- [ ] Step 4: 全量门禁

### Task 8: docstring 与文档

- [ ] Step 1: 重写 `agent_loop.py` 模块 docstring（反映 R3/R4 后的真实形状：易失尾部、门卫、钩子、观测）
- [ ] Step 2: `docs/architecture.md` 模块职责表补四个新单元；`docs/README.md` 状态补 R5
- [ ] Step 3: 最终门禁：`pytest` / `ruff` / `mypy` / `scripts/trace_cases.py` / `scripts/memory_cases.py`
- [ ] Step 4: `git commit -m "R5-4: compaction and request assembly units, plus the docstring that had gone stale"`

### Task 9: 收尾核验（真实运行对照）

- [ ] Step 1: 记录拆分前基线（本计划已在 spec §1 记录：788 / 229 行）
- [ ] Step 2: 拆分后重新统计：`agent_loop.py` 行数、`process_input` 行数、「上报降级」实现份数
- [ ] Step 3: 真跑一次 `ask`（`config.local.yaml`），比对轨迹事件序列与用量量级与拆分前一致
- [ ] Step 4: 汇总逐文件说明与数据流对照

---

## 自检（写计划后复核）

1. **Spec 覆盖**：spec §2.1 的四个单元 → T2（turn_report）、T5（memory_turn）、T6（compaction）、T7（request_builder）；§1.3 的三处重复 → T2（降级）、T3（跨模块统一）、T4+T6（deadline/压缩）；§1.5 陈旧 docstring → T8。**无缺口。**
2. **占位符扫描**：无 TBD/TODO；每个任务给出确切文件、接口签名、测试文件与命令。
3. **类型一致**：计划中的名字与 spec §3 的接口签名逐字一致（`report_context` 返回 `str`、`compact_now` 返回 `dict[str,int]`、`build_tail` 返回 `(list[ChatMessage], tuple[tuple[str,int],...])`）。
