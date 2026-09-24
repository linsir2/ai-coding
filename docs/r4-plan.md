# R4 实施计划（可观测 / 门卫 / 钩子）

> **For agentic workers:** 按任务顺序逐条执行，每个任务自带测试循环（先红 → 再绿 → 提交）。步骤用 `- [ ]` 跟踪。

**Goal:** 给自持 ReAct 循环补上三层——可观测（trace + token 账本）、门卫（有状态拦截）、钩子（用户扩展点），全程不破坏 R1–R3 的既有不变量。

**Architecture:** 观测层是一个 `LoopObserver`（`TraceRecorder`）＋一个纯同步账本（`BudgetLedger`）；门卫是插入 `ToolDispatcher` 的有状态判定链；钩子是挂在固定生命周期点位的进程内/子进程回调。三者都通过既有接缝接入，不改变 `AgentLoop` 的编排形状。

**Tech Stack:** Python 3.10+ / asyncio / dataclasses / pydantic / pytest / ruff / mypy(strict) / JSONL。**不新增第三方依赖。**

**Spec:** [`docs/r4-analysis.md`](r4-analysis.md)（契约、管线、口径、TDD 用例、决策点全在那里；本计划不再抄写契约代码，只引用小节号）

## Global Constraints

- 既有 **633 例测试全绿**；`ruff check .` 0；`mypy ai_coding`（strict，`python_version=3.10`）0。
- **不改既有测试的断言**（除非测试自身写错，需在提交信息里说明）。
- **只追加带默认值的字段**（P7）；`TurnStatus.BUDGET` 与 `TurnOutcome.usage` 是仅有的两处合同变更。
- 观测层**只读**，永远不得影响回合（P1）；门卫**fail-closed**（P2）；钩子按能力分失败语义（P3）。
- 任何写盘先过 `Redactor`（P4）；默认只记元数据 + 预览（P5）。
- 进程执行只走 `infra/subprocess_runner`；任何等待都受 `infra/deadline` 约束。
- 每一步的 `Run:` 命令都在仓库根目录执行，Python 一律用 `.venv/bin/python`。

---

## 文件结构（先锁定边界）

| 文件 | 职责 | 任务 |
|---|---|---|
| `ai_coding/core/events.py` | 事件值（新增 4 个 + 2 处字段） | T1 |
| `ai_coding/core/observers.py`（新） | `Braid` / `ScopedObserver` | T2 |
| `ai_coding/core/trace.py`（新） | `Redactor` / `canonical_request` / `prefix_shared` / `TraceStore` / `TraceRecorder` | T3–T5 |
| `ai_coding/core/budget.py`（新） | `Pricing` / `UsageTotals` / `BudgetLedger` / `cache_from_usage` | T6 |
| `ai_coding/ai/openai_engine.py` | usage 归一加 cache 明细 | T7 |
| `ai_coding/domain/model_config.py` + `ai_coding/config/models.py` | 价格字段 + `TracingConfig` | T8 |
| `ai_coding/tools/base.py` | `ToolContext.call_id` | T9 |
| `ai_coding/core/agent_loop.py` | 发新事件、建账本、降级可见化、`TurnOutcome.usage` | T10–T12 |
| `ai_coding/core/dispatch.py` | 每次调用独立 ctx + 事件带 iteration | T13 |
| `ai_coding/core/subagent.py` + `tools/subagent_tool.py` | 子智能体父子 span | T14 |
| `ai_coding/ui/trace_render.py`（新） + `cli.py` + `ui/slash.py` + `ui/repl.py` | `trace show/budget`、`/trace` `/budget` | T15–T16 |
| `ai_coding/core/guard.py`（新） | 守卫契约 + 4 内置守卫 | T17–T20 |
| `ai_coding/core/dispatch.py` | 接守卫链 + A5 重过闸 + 改写转写 | T21 |
| `ai_coding/core/turn_policy.py` + `domain/loop.py` + `core/subagent.py` | token 闸门 + `TurnStatus.BUDGET` | T22–T23 |
| `ai_coding/infra/subprocess_runner.py` | `stdin_data` | T24 |
| `ai_coding/core/hooks.py`（新） | 钩子契约 + 两种 runner | T25–T26 |
| `ai_coding/core/dispatch.py` + `agent_loop.py` + `subagent.py` | 钩子接线 | T27 |
| `config/models.py` + `cli.py` | `HooksConfig` + 装配 | T28 |
| `scripts/trace_cases.py`（新） | 真实验收案例 | T29 |
| `docs/*` | 同步（见设计文档 §15） | T30 |

---

## 阶段一：R4a 可观测

### Task 1: 事件契约

**Files:** Modify `ai_coding/core/events.py`；Test `tests/test_loop_events.py`
**Interfaces — Produces:** `ModelStepFinished`、`ContextAssembled`、`Degraded`、`ScopedEvent`（见 spec §4.1）；`ToolStarted.iteration`、`ToolFinished.iteration`

- [ ] Step 1: 写测试——4 个新事件是 frozen 且在 `LoopEvent` 联合内；`iteration` 默认 0；`ScopedEvent` 可包裹任意事件
- [ ] Step 2: `Run: .venv/bin/python -m pytest tests/test_loop_events.py -q` → 预期 FAIL（`ImportError`）
- [ ] Step 3: 实现（spec §4.1 的 dataclass 原样落地 + 扩 `LoopEvent`/`__all__`）
- [ ] Step 4: `Run` 同上 → 预期 PASS；再跑 `mypy ai_coding` 确认递归类型可接受
- [ ] Step 5: `git commit -m "R4a-1: loop events for usage, context attribution, degradation and scoped fan-out"`

### Task 2: 观察者扇出

**Files:** Create `ai_coding/core/observers.py`；Test `tests/test_observers.py`
**Interfaces — Consumes:** `LoopObserver`、`LoopEvent`、`ScopedEvent`。**Produces:** `Braid(*observers)`、`ScopedObserver(scope, parent, inner)`

- [ ] Step 1: 测试——`Braid` 顺序转发／单个观察者抛异常不影响其余／`ScopedObserver` 包出 `ScopedEvent` 且 `parent` 正确／空 `Braid` 不炸
- [ ] Step 2: `Run: .venv/bin/python -m pytest tests/test_observers.py -q` → FAIL
- [ ] Step 3: 实现（两者都实现 `on_event`，异常一律吞掉并记 debug）
- [ ] Step 4: `Run` → PASS
- [ ] Step 5: `git commit -m "R4a-2: observer fan-out and scoped observers"`

### Task 3: 脱敏

**Files:** Create `ai_coding/core/trace.py`（先只放 `Redactor`）；Test `tests/test_redactor.py`
**Interfaces — Produces:** `Redactor(patterns=None, enabled=True)`；`.redact(text)->str`；`.redact_obj(obj)->Any`

- [ ] Step 1: 测试——五类模式各命中；良性文本零改动；**幂等**；私钥整块；嵌套结构里也脱敏
- [ ] Step 2: `Run: .venv/bin/python -m pytest tests/test_redactor.py -q` → FAIL
- [ ] Step 3: 实现（spec §4.5 的模式表）
- [ ] Step 4: `Run` → PASS
- [ ] Step 5: `git commit -m "R4a-3: secret redaction for anything that reaches disk"`

### Task 4: trace 存储与归属度量

**Files:** Modify `ai_coding/core/trace.py`；Test `tests/test_trace_store.py`
**Interfaces — Produces:** `canonical_request(instructions, messages, tools_chars)->str`；`prefix_shared(prev, cur)->(int, float)`；`digest_of(text)->str`；`TraceStore(root, *, max_turn_bytes, retention_days)`：`.begin_turn(session_id)->TurnWriter`、`.prune()->int`、`.update_summary(session_id, payload)->None`

- [ ] Step 1: 测试——目录自动创建；`begin_turn` 编号递增且不与已有文件冲突；超 `max_turn_bytes` 截断并留标记；`prune` 清理过期目录；`summary.json` 原子写
- [ ] Step 2: `Run: .venv/bin/python -m pytest tests/test_trace_store.py -q` → FAIL
- [ ] Step 3: 实现（`O_APPEND` 单行写；`os.replace` 原子替换）
- [ ] Step 4: `Run` → PASS
- [ ] Step 5: `git commit -m "R4a-4: trace store with turn numbering, caps, retention and atomic summaries"`

### Task 5: 记录器

**Files:** Modify `ai_coding/core/trace.py`；Test `tests/test_trace_recorder.py`
**Interfaces — Consumes:** 上面的 store + redactor。**Produces:** `TraceRecorder(store, session_id, *, redactor=None, capture="previews")` 实现 `on_event`

- [ ] Step 1: 测试——事件 → 一行 JSONL（`v/ts/session/turn/span/parent/iter/evt/data`）；`span/parent` 正确；**写盘失败发 `Degraded` 且不抛**；敏感串逐字节搜不到；`capture=full` 才写完整 `ContextAssembled`
- [ ] Step 2: `Run: .venv/bin/python -m pytest tests/test_trace_recorder.py -q` → FAIL
- [ ] Step 3: 实现（`TurnFinished` 结束本回合并写 summary）
- [ ] Step 4: `Run` → PASS
- [ ] Step 5: `git commit -m "R4a-5: the trace recorder (JSONL, spans, redaction, silent degradation)"`

### Task 6: 账本与缓存口径

**Files:** Create `ai_coding/core/budget.py`；Test `tests/test_budget_ledger.py`、`tests/test_cache_normalization.py`
**Interfaces — Produces:** `Pricing`；`UsageTotals`；`BudgetLedger.record_step/record_assembly/totals/calibration/cache_hit_ratio/cost`；`cache_from_usage(dict)->(cached|None, write|None)`

- [ ] Step 1: 测试（两组）——累加、`usage=None`、成本、价格未知→`None`、校准比、cache 率、四种 provider 形状 + `null` 值不炸
- [ ] Step 2: `Run: .venv/bin/python -m pytest tests/test_budget_ledger.py tests/test_cache_normalization.py -q` → FAIL
- [ ] Step 3: 实现（spec §4.3–4.4）
- [ ] Step 4: `Run` → PASS
- [ ] Step 5: `git commit -m "R4a-6: the budget ledger and provider-truth cache accounting"`

### Task 7: 引擎上报 cache 明细

**Files:** Modify `ai_coding/ai/openai_engine.py`；Test `tests/test_openai_engine.py`（扩）
**Interfaces — Produces:** `_usage_to_dict` 额外返回 `cached_tokens` / `cache_write_tokens`（有才带）

- [ ] Step 1: 测试——OpenAI 形状 `prompt_tokens_details.cached_tokens` 被带出；Anthropic 形状带出 `cache_read/creation`；都没有时字典里无这两个键
- [ ] Step 2: `Run: .venv/bin/python -m pytest tests/test_openai_engine.py -q` → FAIL
- [ ] Step 3: 实现（复用 `core.budget.cache_from_usage` 的归一逻辑，避免两处口径分叉）
- [ ] Step 4: `Run` → PASS
- [ ] Step 5: `git commit -m "R4a-7: carry prompt-cache details out of the engine"`

### Task 8: 配置面

**Files:** Modify `ai_coding/domain/model_config.py`、`ai_coding/config/models.py`；Test `tests/test_config_loader.py`（扩）
**Interfaces — Produces:** `ModelConfig.price_input_per_1k/price_output_per_1k`；`TracingConfig`；`AppConfig.tracing`

- [ ] Step 1: 测试——默认值与解析；`config.local.yaml` 不加新块也能加载
- [ ] Step 2: `Run: .venv/bin/python -m pytest tests/test_config_loader.py -q` → FAIL
- [ ] Step 3: 实现
- [ ] Step 4: `Run` → PASS
- [ ] Step 5: `git commit -m "R4a-8: tracing and pricing configuration"`

### Task 9–12: 循环接线

**Files:** Modify `ai_coding/tools/base.py`（T9）、`ai_coding/core/agent_loop.py`（T10–T12）、`ai_coding/core/dispatch.py`（T13）

- [ ] Step 1: T9 `ToolContext.call_id` + 测试 `tests/test_tool_context_call_id.py`（每次调用独立 ctx）
- [ ] Step 2: T10 循环发 `ContextAssembled`（每轮）与 `ModelStepFinished`（每步）+ 测试 `tests/test_context_assembled.py`、`tests/test_model_step_finished.py`
- [ ] Step 3: T11 七个降级点发 `Degraded` + 测试 `tests/test_degraded_events.py`
- [ ] Step 4: T12 `TurnOutcome.usage` + 测试 `tests/test_loop_types.py`（扩）
- [ ] Step 5: T13 dispatcher 每次调用 `replace(ctx, call_id=...)`、事件带 `iteration`
- [ ] 每步都 `Run` 对应测试 → PASS；每步一次提交（`R4a-9` … `R4a-13`）

### Task 13–14: 子智能体与 CLI

- [ ] Step 1: T14 `SubAgentRunner(observer=...)` + `SubAgentTool` 传 `call_id` + 测试 `tests/test_subagent_scoped_events.py`
- [ ] Step 2: T15 `ui/trace_render.py` + `ai-coding trace list|show|budget` + 测试 `tests/test_cli_trace.py`
- [ ] Step 3: T16 `/trace` `/budget` + `tests/test_slash.py`（扩）
- [ ] Step 4: R4a 收口——`pytest tests/ -q`（既有 + 新增全绿）、`ruff`、`mypy`

---

## 阶段二：R4b 门卫

### Task 17–21

- [ ] T17 `core/guard.py` 契约 + `GuardChain` + 参数指纹 → `tests/test_guard_chain.py`
- [ ] T18 `DuplicateCallGuard` → `tests/test_guard_duplicate.py`
- [ ] T19 `BashPolicyGuard` + `PathEscapeGuard` → `tests/test_guard_bash_policy.py`、`tests/test_guard_path_escape.py`
- [ ] T20 `SecretGuard`（后置）→ `tests/test_guard_secret.py`
- [ ] T21 dispatcher 接线：A3 前置、**A5 改写后重跑权限门**、C2 后置、`rewritten_from` 转写 → `tests/test_dispatch_rewrite_regate.py`
- [ ] T22–T23 `max_turn_tokens` + `TurnStatus.BUDGET` + `_STATUS_MAP` → `tests/test_turn_policy.py`（扩）、`tests/test_subagent_status_map.py`
- [ ] R4b 收口——三个门禁全绿

---

## 阶段三：R4c 钩子

### Task 24–28

- [ ] T24 `run_process(..., stdin_data=...)` → `tests/test_subprocess_runner_stdin.py`
- [ ] T25 `core/hooks.py` 契约 + 能力矩阵校验 → `tests/test_hooks_runner_command.py`（部分）
- [ ] T26 `CommandHookRunner`（超时/环境白名单/失败语义/脱敏）与 `PythonHookRunner`（自动发现/entrypoint）→ 两个 runner 测试
- [ ] T27 接线 `tool.before/after`（dispatcher）、`turn.*/step.before/compact.*/memory.harvest/stop.blocked`（loop）、`subagent.*`（runner）→ `tests/test_hooks_loop.py`
- [ ] T28 `HooksConfig` + CLI 装配 + `config-check` → `tests/test_config_tracing_guard_hooks.py`
- [ ] R4c 收口——三个门禁全绿

---

### Task 29: 真实验收脚本

**Files:** Create `scripts/trace_cases.py`（Test: 自校验脚本 + `tests/test_trace_cases_smoke.py`）

- [ ] Step 1: 写脚本骨架 `--only/--real/--config`，先实现离线 5 条（C1/C5/C6/C8/C9）
- [ ] Step 2: `Run: .venv/bin/python scripts/trace_cases.py` → 全部通过
- [ ] Step 3: 加真 LLM 4 条（C2/C3/C4/C7），**C2 先打印真实 usage 键**
- [ ] Step 4: `Run: .venv/bin/python scripts/trace_cases.py --real --config config.local.yaml` → 通过（或记录 provider 限制）
- [ ] Step 5: `git commit -m "R4: real acceptance cases for tracing, guards and hooks"`

### Task 30: 文档同步

- [ ] 按 spec §15 更新 7 个文档 + `.gitignore`
- [ ] 最终核验：`pytest tests/ -q`、`ruff check .`、`mypy ai_coding`、案例脚本
- [ ] `git commit -m "docs(R4): sync data contracts, architecture, features and how-to-run"`

---

## 自检（写计划后复核）

1. **Spec 覆盖**：设计文档 §4/§5/§6 的每个模块都有对应任务（`events`→T1、`observers`→T2、`trace`→T3–T5、`budget`→T6、引擎→T7、配置→T8/T28、`ToolContext`→T9、循环→T10–T12、`dispatch`→T13/T21、子智能体→T14、CLI→T15–T16、门卫→T17–T20、token 闸门→T22–T23、`stdin`→T24、钩子→T25–T27、脚本→T29、文档→T30）。无缺口。
2. **占位符扫描**：无 TBD/TODO；每个任务给出确切文件、接口签名、测试文件与命令。
3. **类型一致**：计划中的名字（`TraceStore.begin_turn`、`BudgetLedger.record_step`、`GuardChain.check`、`GuardDecision.params`）与 spec §4–6 的签名逐字一致。
