# 领域 C · 恢复与持久化（M-C1 ~ M-C3）

> 调研：两轮联网（55+ 引用源，全文见 [research-persistence-crash-recovery.md](research-persistence-crash-recovery.md)）
> + 两轮仓库核实（子智能体）。方案与评审记录见 §八。
>
> 本稿为**第 3 版**，已按两轮子智能体方案评审（§八）修订。

---

## 一、调研结论

### 1.1 会话持久化已收敛为双层模型

| 层 | 职责 | 代表 |
|---|---|---|
| **Checkpointer** | 短期、线程内图状态，按 `thread_id` 主键，每个 super-step 存快照 | LangGraph、OpenAI Agents SDK `Session` |
| **Store / 记忆层** | 跨线程、跨会话的事实与偏好 | LangGraph Store、Letta 三层记忆、Mem0 |

### 1.2 崩溃恢复的本质是**持久化执行**，不是重试

* LangGraph 三档 durability（`exit`/`async`/`sync`），**按"丢失的后果"选档**：会产生真实世界重复（支付/邮件/写 API）的流程必须 `sync`。
* 恢复**不是从断点续行，而是确定起点后重放** → 工作流必须**确定性 + 幂等**。
* **"最危险的崩溃窗口"**：副作用已成功、`COMPLETED` 未落库时崩溃 → at-least-once 重复副作用。
* 解法组合：**幂等键 + 接收端唯一约束 + 租约/围栏令牌 + 事务性发件箱 + 对账补偿**。

> **本方案封的是哪一半**：M-C1 的 `ok`（已成功）档封住"**成功后**崩溃被重放"。
> `running`（已声明、结果未知）档在恢复时**仍然重跑**，可能重复副作用 ——
> 那一半需要 Outbox/租约/围栏令牌（§七 明确不做）。**不假装它已被封住。**

### 1.3 三家对标

| | OpenCode | Codex CLI | Claude Code |
|---|---|---|---|
| 范式 | **数据库 + 预写声明** | **事件溯源 + 重放** | **转录 + 启动加载** |
| 机制 | WAL + 先写持久 claim（inbox 行/硬链接）再动作 + ack/rollback | 追加式 rollout JSONL 即 source of truth，resume = 重放 | 追加式 JSONL，启动时一次加载 |
| 中断工具 | claim 重放 | rollout 重放 | **标记"结果记录前被截断"，让模型先确认是否已生效再决定是否重跑** |

### 1.4 并发控制

线程隔离 / 乐观并发（版本 CAS）/ 悲观锁（`SELECT ... FOR UPDATE`）/ Redis 线程锁 + 409 /
双发策略（enqueue/reject/interrupt）。

---

## 二、仓库核实：三条缺口的真伪

### M-C1 · step 级 durable intent —— ✅ 缺口成立，⚠️ roadmap 描述部分过时

| roadmap 断言 | 判定 |
|---|---|
| 恢复扫描是会话级、只认 plan/goal | ✅ `world.py:90` 白名单 `("plan","goal")` |
| 会话 JSON **没有** step 级记录 | ⚠️ **过时** —— 已有 `StepDelivery`（落盘 `stepDeliveries`），PlanRunner 用它跳过已完成步骤 |
| 重启后"整段重跑" | ⚠️ 不准确 —— 已完成步骤会被跳过（`plan_runner.py:641-657`） |
| **真因（缺口仍在）** | ✅ ① 收据在步骤**结算后**才写（`plan_runner.py:1145` `_settle`），非 write-ahead；② 收据只存一行 `summary` + 验收结论，**不含已执行的工具调用**；③ 步骤自身 transcript 标 `ephemeral`，**从不落盘**（`plan_runner.py:875`、`turn_close.py:206`）→ **在飞步骤中途崩溃会整步重做** |

`resume_turn`（`agent_loop.py:685`）的 docstring 明写 *"the crash's batch never
checkpointed"* → **崩溃批次的工具会重跑**。

### M-C2 · 记忆并发写安全 —— ✅ 完全相符（但是**潜在**隐患，不是现实故障）

`_mutate`（`store.py:143`）在 `_save` **之前**放锁 → `_save` 无锁读 `self._notes`，
两线程落到同一 `<path>.tmp` → `os.replace` 后另一线程继续写正式文件 →
**文件变非法 JSON** → `_load` 吞掉 `ValueError` 返回 `[]` → **全部记忆丢失**。

**必须诚实框定**：`MemoryStore` 的 docstring 自己写了 *"A single-writer store is
fine; **that is every real path in this repo**"* 与 *"**R10 reviewed this and chose
to record it rather than restructure persistence**"*。今天确实全部单线程
（`recall_with_diagnostics` 里的 `touch_notes` 是直接同步调用，没有 `to_thread`）。
**所以 M-C2 修的是"把 store 写操作移到线程之前必须先修的隐患"，不是一个正在漏的洞。**

### M-C3 · 崩溃后续跑的验收门 —— ✅ 完全相符

`resume_count >= 5` 是唯一熔断（`handlers.py:340`）；`no-progress` 仅**单轮**判据
（`goal_runner.py:295`），**无跨续跑的前进判据**；唯一的续跑消息是字符串
`resumeNote`，**无结构化交接报告**。

### 现状数据流

```
boot → find_armed_session（会话级扫描，仅 plan/goal，按 lastAccessTime 取最新）
     → handlers.initialize
         resume_count += 1; persist            ← 先计数，不看进展
         → _launch_armed_run(boot=True)
             ├─ open_turn is not None → loop.resume_turn（历史止于上一个已 checkpoint 批次）
             └─ PlanRunner/GoalRunner.run → TaskDriver.run
                 每轮 should_stop(): model-complete | model-blocked | no-progress(本轮零工具) | budget | disarm
                 → 全部通过 → 下次 boot 再 resume_count += 1
                 → resume_count >= 5 → disarm + resumeNote("已达上限")
```

---

## 三、方案：逐项实现

### A. M-C1 · 工具级 write-ahead intent + 结果复用

**语义**（对标 OpenCode 的 claim/ack + 调研的幂等键）：

```
mutates_world 工具执行【前】
    → 写 sidecar: { callId, tool, digest, status: "running" }        ← write-ahead
执行【后】
    → 回填: { status: "ok"/"failed", output, data, artifacts }
再次出现同一 (tool, digest) 且 status == "ok"
    → 直接复用该结果，【不执行】
回合结束
    → 清空 sidecar                                                  ← 复用域 = 恢复窗口
```

#### A.1 落点：**sidecar**，不是会话字段

> **为什么不能走 `TurnCloser.save`**：`save` 的第一件事是
> `ensure_tool_pairing()`（`turn_close.py:208`），它会**丢弃没有配对结果的
> assistant 载体**。而 `claim` 必须在 `_execute` **之前**调（`dispatch.py:514`），
> 此刻 `write_back` 还没跑（`agent_loop.py:1951`）→ 载体被丢 → 随后写回的 tool
> 消息成了孤儿 → 下次落盘再被丢。**这个坑仓库里已经踩过三次，三次都写了注释
> 明令禁止**（`agent_loop.py:1794`、`session_service.py:122/168/212`、
> `data-contracts.md:68`）。审批等待就是为此改成 sidecar 的。

* `SessionService` 加 `_write_ahead_path` / `mark_write_ahead(session_id, records)` /
  `_read_write_ahead(session_id)` / `clear_write_ahead(session_id)`，写到
  **`sessions/write-ahead/<id>.json`**。
  * **必须是子目录**：会话扫描 glob `*.json`，`session_service.py:134-138` 记录了
    "兄弟 marker 被 `list_sessions` 读成幽灵空会话"的坑。
* `SessionData.write_ahead` 是**运行时字段**（`pending_approval` 同款，`load` 时从
  sidecar 读回，`session_service.py:283` 是现成接线点），**不进 `to_dict` 白名单**
  → 会话 JSON 不因此变大，save 节奏不变（每个 golden test 钉着的节奏）。
* **生命周期**（评审新问题 ①②）：
  * `delete()` 必须清（`session_service.py:345` 已清 `pending`/`pending-posture`/
    `pending-reminder`/`undo` 四个 sidecar —— 这是仓库第 5 次修同一类孤儿）；
  * **回合结束时清**（`TurnCloser.close` → `clear_write_ahead`）。
    这一条同时把**复用域**限制在恢复窗口内（评审新问题 ③）：sidecar 只在回合中
    （以及崩溃后）存在，跨回合的同 `(tool, digest)` **不可能**命中。

#### A.2 契约：`WriteAheadIntent`（新，`domain/write_ahead.py`）

```python
@dataclass
class WriteAheadIntent:
    """One side-effecting tool call, recorded before it runs (M-C1).

    The record is the claim OpenCode writes before it wakes the session and the
    idempotency key the recovery literature lands on: ``(tool, digest)`` names
    the *intent*, so a call the model regenerates after a crash matches the one
    that already ran.  ``callId`` is traceability only -- a resumed turn mints
    new ids.

    ``running`` is deliberately *not* a hit: the side effect may or may not have
    happened, so recovery re-runs it.  Only ``ok`` is reused.  Closing the
    remaining window (effect done, ``ok`` not yet on disk) needs an outbox and
    fencing tokens, which this repo does not have (§七).

    ``output``/``data``/``artifacts`` are carried because a reuse has to be
    *indistinguishable* from a fresh run: the model reads ``output``, and the
    turn's digest/handoff read ``artifacts`` (``turn_close.py:177``).  Dropping
    them would silently shrink the turn's evidence.  All three are capped --
    an unbounded record per call is how a sidecar becomes a second transcript.
    """

    call_id: str
    tool: str
    #: ``call_fingerprint(call)`` -- reused from ``core/guard.py``, not reinvented.
    digest: str
    #: "running" | "ok" | "failed"
    status: str = "running"
    output: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
```

* **`digest` 复用 `core/guard.py:109` 的 `call_fingerprint`**（sha1 截 12，
  `(tool, params)` 顺序不敏感、值敏感）—— 不新造第二个指纹函数。
  `DuplicateCallGuard` 已经在用它拦"同参重复调用"（阈值 2、只数成功、跨轮重置），
  write-ahead 补的是它缺的**跨崩溃**那一半（§A.5）。
* **三个上限**：`output` 4000 字符（评审新问题 ⑤：崩溃批次的 tool 消息**不在盘上**
  ——`resume_turn` 的 docstring 说历史止于上一个已完成结果，`write_back`(1951) 与
  `checkpoint`(1962) 之间崩溃即未落盘 —— 所以续跑时模型看到的只有这里，200 字符
  对大输出是实质信息损失）；`data` 8 键 × 200 字符；`artifacts` 4 条。

#### A.3 行为：`WriteAheadLog`（新，`core/write_ahead.py`）

```python
class WriteAheadLog:
    """The session's write-ahead records, and the reuse decision (M-C1).

    Owned by the loop (it holds the session and the ``SessionService``), handed
    to the dispatcher on the tool context -- the same shape as
    ``note_approval`` / ``note_checkpoint`` / ``note_plan_approved``, which exist
    for the same reason: the dispatcher needs session-scoped durable state it
    must not fetch itself.

    Both methods are **synchronous**: the dispatcher's ALLOW group runs a batch
    through ``asyncio.gather`` (``dispatch.py:253``), and a sync method is what
    keeps two same-param calls in one batch from both claiming a miss.

    The reuse domain is **the turn**: ``clear`` runs at turn close, so a match
    can only come from the turn that is running (or the one a crash interrupted).
    A later turn starts with an empty log -- this is crash recovery, not a
    session-long call cache.
    """

    def claim(self, call: ToolCall) -> ToolResult | None: ...
    def settle(self, call: ToolCall, result: ToolResult) -> None: ...
    def clear(self) -> None: ...
```

* **覆盖哪些工具**：`item.tool.mutates_world`（评审 P1-4 修正）。
  * `ACTION_TOOLS`（M-B）**不是**这份语义 —— 它是搜索连胜阶梯里"什么算 acting"
    的清单（`turn_books.py:79-87` 的 docstring 原文），与"改世界"不等价：
    `job_kill`/`memory_write` 是 `mutates_world=True` 却不在其中。
  * `mutates_world` 是**工具自己声明的**元数据（`base.py:167`，默认 True），
    也是 dispatcher 只读门的同一份判据 → **同一份语义，不新造清单**。
  * **注意它比 `ACTION_TOOLS` 宽**：`SubAgentTool`（`subagent_tool.py:119`）与
    `websearch`（`websearch_tool.py:80`）/`webfetch`（`webfetch_tool.py:198`）都
    **继承默认 True**。这是可接受的 —— 复用域是**回合**，同一回合内重复的
    `websearch`/`subAgent` 本来就命中 `DuplicateCallGuard`（阈值 2）；而
    `subAgent` 的结果复用是**收益**（重跑一个子智能体很贵）。
    （`subagent_tool.py:357` 那条 `mutates_world = False` 属于
    **`SubAgentListTool`**，不是 `SubAgentTool` —— 评审 P1-4 的更正。）
* **`ephemeral` 会话不记**：plan 步会话不落盘（`turn_close.py:206`），
  sidecar 是纯开销。`WriteAheadLog` 构造时判一次即可。
* **条数上界 256**：`max_iterations=10 × max_parallel=4 = 40`，重放再 +40，
  256 有余量；配合回合结束清空，实际长度是"一个批次"。

#### A.4 接线

| 位置 | 改动 |
|---|---|
| `domain/write_ahead.py`（新） | `WriteAheadIntent` + 三个上限的裁剪 |
| `domain/session.py` | `write_ahead`（运行时字段） |
| `service/session_service.py` | sidecar 四方法 + `load` 读回 + `delete` 清理 |
| `core/write_ahead.py`（新） | `WriteAheadLog` |
| `tools/base.py` | `ToolContext.write_ahead: Any = None`（与 `jobs` 同款 duck-typed） |
| `core/request_builder.py` | `tool_context` 透传 |
| `core/agent_loop.py` | `_run_tools` 造 log 并传入 |
| `core/dispatch.py` | `_decide` 里 claim/settle |
| `core/turn_close.py` | `close` 里 `clear()` |

**精确插入点**：

```
_decide(call)
  ... gate / readonly / guards / hooks / precheck ...      ← 都用 rewrite 后的 call
  blocked = tool.precheck(call.params)                     ← dispatch.py:507
  if blocked is not None: return blocked
  # ↓ claim 必须在这里：precheck 之后（参数已定型）、_execute 之前
  reused = ctx.write_ahead.claim(call) if ctx.write_ahead else None
  if reused is not None:
      return reused, False, False          # 复用：不执行、不 denied、不 halted
  result = await self._execute(tool, call, ctx, deadline)   # dispatch.py:514
  ... guards.after / TOOL_AFTER hooks ...
  ctx.write_ahead.settle(call, result)
  return result, ...
```

* `digest` 必须用 **guard/hook rewrite 之后**的 `call.params` —— 否则同一意图会算出
  两个不同的键。
* **复用时跳过 `guards.after` 与 `TOOL_AFTER` hooks**：它们的输入是"刚刚执行的
  结果"，而复用没有执行。`TOOL_BEFORE` 与 guards 的 `check` **已经跑过**
  （在 claim 之前），所以权限语义不 bypass。**这是有意的**，写入 docstring。

#### A.5 与现有机制的关系

| 机制 | 粒度 | 时机 | 消费者 | 与本方案 |
|---|---|---|---|---|
| `resume_turn` | 回合 | 崩溃后 | loop | **互补**：它决定"从哪继续"，本方案保证"继续后不重复副作用" |
| `StepDelivery` | 步骤 | **事后**（结算） | replay | **不合并**：事后收据 vs 事前声明 |
| `DuplicateCallGuard` / `call_fingerprint` | 单次调用 | 执行前 | guard | **互补**：它拦"同一进程内的同参重复"（阈值 2、跨轮重置），本方案补**跨崩溃**那一半；`digest` 直接复用它 |
| 审批 sidecar（`mark_pending_approval`） | 单次调用 | 等待中 | `resume_turn` | **同形**：中批次耐久状态的唯一正确落点 |

### B. M-C2 · 记忆并发写安全

**三层**：

1. **`_save` 进锁**：
   ```python
   with self._lock:
       result = fn()
       self._save()
   return result
   ```
   **必须保留 `_save` 外层的 try/except**：`to_json_pretty` 可能抛非 `OSError`，
   逃逸出锁会让后续写全部卡死。`_save` 内部已有 `except OSError`；`_mutate`
   外层再包一层 `except Exception`（与今天一致）。
2. **`fcntl.flock` 跨进程**：`_save` 在写 tmp + `os.replace` 期间对**正式文件**加
   排他锁。两个 `ai-coding` 实例跑同一 workspace 时，进程内 `RLock` 互不感知。
3. **唯一 tmp 路径**：`<path>.<uuid4hex>.tmp`。即使锁失效（非 POSIX 平台没有
   `fcntl`），两个写者也不会把内容拼进同一文件 —— **文件始终合法**，最坏是
   last-write-wins（丢一条更新），而不是"全部记忆消失"。

```python
def _replace_under_lock(self, tmp: Path) -> None:
    """Atomically move ``tmp`` onto the live path, serialized across processes.

    The lock must span the *rename*, not just the write: releasing before
    ``os.replace`` leaves a window where another process renames its own tmp in
    between, and the two end up interleaved.  ``fcntl`` is POSIX-only; elsewhere
    this degrades to a bare ``os.replace`` and the docstring says so -- the
    process-internal lock still applies.
    """
```

* **跨平台**：`fcntl` 用 `try/except ImportError` 可选导入；不可用时**不假装修了**，
  docstring 与 `data-contracts` 明写"跨进程保护仅 POSIX"。
* **诚实框定**（§二）：今天所有真实路径都是单写者，这是**潜在隐患的预防性修复**。
* **不动 `session_service.py` 的 4 处会话 `tmp + os.replace`** —— 会话写是单写者
  （event loop + `_turn_lock`），无对应缺口。

### C. M-C3 · 崩溃后续跑的验收门

**判据**：**boot 到 boot 比较**，而不是 run 的首尾比较。

> **为什么**：`rounds_run` 在两个 profile 里每轮必增（`plan_runner.py:715`、
> `goal_runner.py:261`），跑完 ≥1 轮就必变；而崩在轮中途时"记 after"这行代码
> 根本不会执行。

```
boot N 的 initialize:
    若 resume_fingerprint 已存在（上一次续跑留下的）:
        now = fingerprint(task, session)
        now == resume_fingerprint  → no_progress_resumes += 1   ← 上次续跑什么都没做
        否则                      → no_progress_resumes = 0
    若 no_progress_resumes >= 2 → disarm + ResumeStalled + note（同一个响应）
    否则:
        resume_fingerprint = fingerprint(task, session)        ← 先存"之前"
        → _launch_armed_run(boot=True)                        ← resume_count 仍在其中自增
```

* **指纹** = `(rounds_run, tokens_used, 非合成消息数, len(step_deliveries))`。
  * **非合成消息数**（评审新问题 ⑨）：`resume_turn` 在有待审批时会**追加一条合成
    user 消息并 save**（`agent_loop.py:718`），`len(messages)` 因此变化 → 指纹会
    读成"有进展"，掩盖一次真正的停滞。合成消息是 harness 说的，不是任务推进。
  * 四者全等 = **上一次续跑没有完成任何一轮、没产生任何真实消息、没结算任何步骤**。
* **为什么 2 次**：一次可能是崩溃在很早期（`resume_turn` 刚开头）。
* **disarm 与 note 在同一个 `initialize` 响应里** —— 判定发生在 `initialize`
  （同步），不在异步 run 里。
* **`resume_count` 的位置**（评审新问题 ⑧）：它在 `run_resume`（异步，
  `handlers.py:609`）自增并落盘，而 `initialize` 自己在 `handlers.py:372` 已
  persist 过一次。**指纹在 `initialize` 落盘**（早于 372），所以两次 persist 之间
  崩溃会丢 `resume_count` 的自增、但留下指纹 —— 下一次 boot 会把它读成"停滞"
  并计数。可接受（宁紧不松）。
* **"0 轮"不等于"已完成计划"**：已完成的计划已被 runner disarm
  （`goal_runner.py:167` 的 `finally`），不会再 armed 到 boot。

**交接报告**：新增 `ResumeStalled` 事件（`resumes`/`no_progress`/`before`/`after`）。

* **落 trace 需要显式绑定**（评审新问题 P1-2）：trace recorder **不在**
  `initialize` 的观察者辫子里 —— `AgentLoop.__init__` 只把 base observer 包成
  `SafeObserver`（`agent_loop.py:347`），trace 进辫子只发生在 `_bind_observer`
  （`:1734`），而它只被 `_begin_turn`（`:1269`）调用。disarm 分支**不启动任何
  turn** → trace 从未绑定 → `trace.session_id` 还是构造时的值，事件会落错目录
  （`trace.py:263` 的 `session_dir("")` → `root/"unknown"`）。
  **所以**：`AgentLoop` 新增公开方法 `bind_session(session)`（内部调
  `_bind_observer(None)` + `_bind_trace(session)`），`initialize` 在 emit 前调它。
* **不改前端**：`bridge.to_wire` 是白名单，新分支要动前端 reducer。本方案把
  结构化记录放进 trace（自动 `dataclasses.asdict`），把操作者可读的总结放进
  **已在流动的** `initialize` 响应的 `resumeNote`。

---

## 四、逐文件改动清单

| # | 文件 | 改动 | 为什么 |
|---|---|---|---|
| 1 | `ai_coding/domain/write_ahead.py`（新） | `WriteAheadIntent` + 三个上限裁剪 | M-C1 的 sidecar 契约 |
| 2 | `ai_coding/domain/session.py` | `write_ahead`（运行时字段） | 契约挂载 |
| 3 | `ai_coding/service/session_service.py` | sidecar 四方法 + `load` 读回 + `delete` 清理 | M-C1 的落点 |
| 4 | `ai_coding/core/write_ahead.py`（新） | `WriteAheadLog` | M-C1 的行为 |
| 5 | `ai_coding/tools/base.py` | `ToolContext.write_ahead` | 传给 dispatcher |
| 6 | `ai_coding/core/request_builder.py` | `tool_context` 透传 | 同上 |
| 7 | `ai_coding/core/agent_loop.py` | `_run_tools` 造 log 并传入；`bind_session` | 接线 |
| 8 | `ai_coding/core/dispatch.py` | `_decide` 里 claim/settle | M-C1 的落点 |
| 9 | `ai_coding/core/turn_close.py` | `close` 里 `clear()` | 复用域 = 回合 |
| 10 | `ai_coding/memory/store.py` | `_save` 进锁 + 唯一 tmp + `_replace_under_lock`；docstring 重写 | M-C2 |
| 11 | `ai_coding/domain/task.py` | `resume_fingerprint`、`no_progress_resumes`；to/from_dict | M-C3 的状态 |
| 12 | `ai_coding/core/events.py` | `ResumeStalled` + 进 `LoopEvent` | M-C3 的报告 |
| 13 | `ai_coding/server/handlers.py` | 指纹比较 + 提前 disarm + 发事件 + note | M-C3 的落点 |
| 14 | `docs/data-contracts.md` | 三条契约（`WriteAheadIntent` / `TaskState` 两字段 / `ResumeStalled`） | 契约同步 |

> **不新造轮子**：`call_fingerprint` 复用的；`mutates_world` 复用的；
> sidecar 形状复用的；`tmp + os.replace` 不变重复用的；trace 落盘复用的；
> `initialize` 响应复用的。

### 4.1 语义等价性

* **无 write-ahead 时**（非 `mutates_world` / `ephemeral` / 无 log）：行为与今天
  **逐字节相同**。
* **有 write-ahead 时**：副作用工具**照常执行**，只是多一个 sidecar 文件。
  只有当**同一 `(tool, digest)` 在同一回合内第二次出现**时才复用。
* **会话 JSON 不变大**（write-ahead 在 sidecar 里）；save 节奏不变。
* **M-C2**：单写者路径行为不变；并发路径从"可能损坏"变成"不损坏"。
* **M-C3**：有前进的续跑行为不变；只新增"连续 2 次无前进 → 提前终止"。

### 4.2 对数据流的影响

* **新增 sidecar 目录** `sessions/write-ahead/`（子目录，避免幽灵会话；
  `delete` 与回合结束都会清）。
* **写放大**（评审新问题 ⑦）：`mark_write_ahead` 每次全量重写列表 ——
  256 条 × ~250B ≈ 64 KB/次；一批 40 个 `mutates_world` 调用 × 2 次
  （claim + settle）≈ 80 次写 ≈ **5 MB/回合**。相对 `bash`/`write` 自身的 IO
  可忽略，但**必须写进文档**，别让下一个人以为它是零成本的。
* **`resume_turn` 的语义被修正**：续跑后模型重复生成的副作用调用会被复用 ——
  今天会执行两次。

---

## 五、RIPR 用例

### Red（先写，必须变红）

| 编号 | 用例 | 期望红相（今天必须失败） |
|---|---|---|
| U1 | `test_write_ahead.py::test_a_side_effect_tool_is_claimed_before_it_runs` | 工具执行中读 sidecar → 已有 `status:"running"`（今天：无 sidecar） |
| U2 | `test_write_ahead.py::test_the_outcome_is_settled_after_the_call` | 执行后 → `status:"ok"` + output/data/artifacts（今天：无） |
| U3 | `test_write_ahead.py::test_a_regenerated_call_reuses_the_recorded_result` | 同 `(tool, digest)` 第二次 → **不执行**（fake tool 计数 1）（今天：2 次） |
| U4 | `test_write_ahead.py::test_the_reuse_carries_the_data_and_artifacts` | 复用结果带原 `data`/`artifacts`（今天：无） |
| U5 | `test_write_ahead.py::test_a_later_turn_does_not_reuse_a_previous_turns_call` | 回合结束清空后，同 `(tool, digest)` **重新执行**（今天：无机制 → 断言"执行了"在实现后成立，**本条是 Prove**） |
| U6 | `test_write_ahead.py::test_the_claim_does_not_disturb_the_session_file` | claim 后读会话 JSON → 在飞的工具调用**没被丢**（锁住 P0-1 的修法） |
| U7 | `test_write_ahead.py::test_the_digest_is_call_fingerprint` | `digest == call_fingerprint(call)`（今天：无 digest） |
| U8 | `test_memory_store.py::test_concurrent_writes_never_corrupt` | 4 线程 × barrier × 25 次写 → 0 损坏 + 重载条数一致（今天：4-6 次损坏） |
| U9 | `test_memory_store.py::test_two_processes_never_corrupt` | 两进程各写 25 次 → 文件始终合法（今天：无跨进程保护） |
| U10 | `test_resume_gate.py::test_one_stalled_resume_does_not_stop_the_task` | 指纹不变 1 次 → 不 disarm（今天：无判据） |
| U11 | `test_resume_gate.py::test_two_stalled_resumes_stop_the_task` | 连续 2 次不变 → disarm + `ResumeStalled`（今天：要 5 次） |
| U12 | `test_resume_gate.py::test_progress_resets_the_counter` | 中间有一次前进 → 计数归零（今天：无） |
| U13 | `test_resume_gate.py::test_a_synthetic_resume_message_is_not_progress` | `resume_turn` 追加合成消息 → 指纹**不变**（今天：无判据） |

> U5 是 **Prove**（断言"重新执行"，实现前后都成立），不是 Red。

### Prove

1. `.venv/bin/python -m pytest -q`
2. `.venv/bin/ruff check .` / `.venv/bin/mypy ai_coding`
3. **突变测试**（每层一处）：去掉 `claim` 的复用分支 → U3/U4 变红；把 `_save`
   挪出锁 → U8 变红；去掉 `no_progress_resumes >= 2` → U11 变红；
   去掉回合结束的 `clear` → U5 变红
4. **3.10 venv 跑 CI 同构七步**（CI 矩阵是 **3.10 + 3.13**，本地 venv 是 3.14 ——
   "与 CI 同构"指步骤，不是解释器）
5. **端到端**：真跑一个会话 → `write`/`bash` 后读到 sidecar；杀掉进程 →
   重启续跑 → 断言副作用工具没有第二次执行

### Refactor

* 确认 `WriteAheadLog` 的三个上限（`output`/`data`/`artifacts`）与条数上界**都有界**
* 确认 `call_fingerprint` 是**唯一**的 `(tool, params)` 指纹（没有第二份）

---

## 六、验收判据

* **M-C1**：`mutates_world` 工具执行前写 sidecar `running`、执行后写 `ok`/`failed`
  且带 `data`/`artifacts`；同一 `(tool, digest)` 在同回合内第二次出现时**不执行**
  并复用；**跨回合不复用**；只读工具与 `ephemeral` 会话不写；
  **会话文件不受影响**；sidecar 在 `delete` 与回合结束时清理。
* **M-C2**：4 线程 × barrier × 25 次写 **0 损坏**，重载后条数一致；两进程并发写
  文件始终合法。
* **M-C3**：boot 到 boot 指纹连续 2 次不变 → 提前 disarm + `ResumeStalled`
  （落 trace 且 `session_id` 正确）；有前进 → 计数归零。
* 3.10 与 3.13 全量绿；真实 CI 双 job 绿。

---

## 七、不做的事

* **不改 `resume_turn` 的语义** —— 它决定"从哪继续"，本方案保证"继续后不重复副作用"。
* **不合并 `StepDelivery` 与 `WriteAheadIntent`** —— 事后收据 vs 事前声明。
* **不新造指纹函数** —— 复用 `call_fingerprint`。
* **不引入配置项**（哪些工具要 write-ahead）—— 用 `mutates_world`。
* **不改 `session_service.py` 的 4 处会话 `tmp + os.replace`** —— 会话写是单写者。
* **不改前端** —— 结构化记录走 trace，操作者总结走 `initialize` 响应。
* **不做 Outbox / 租约 / 围栏令牌** —— 那是分布式多 Worker 的解法；`running` 档的
  残留窗口（§一 的框定）本方案**不封**，并写进了 `WriteAheadIntent` 的 docstring。
* **不做跨回合的 write-ahead 复用** —— 复用域是回合（恢复窗口），这是**有意的**
  （评审新问题 ③）。

---

## 八、评审记录

### 8.1 方案评审（子智能体，第 1 轮）

结论：调研质量高，三条缺口的代码断言逐条核对**基本准确**；1 个 P0、7 个 P1、10 个 P2。

| 编号 | 内容 | 处置 |
|---|---|---|
| **P0-1** | 用 `TurnCloser.save` 落 write-ahead 会**丢弃在飞的工具调用** —— `save` 先跑 `ensure_tool_pairing()`，而 `claim` 在 `_execute` **之前**，`write_back` 还没跑 → 载体被丢。仓库已三次明令禁止 | **已改 §A.1**：落到 **sidecar**，`SessionData.write_ahead` 改为**运行时字段**，**不进 `to_dict`** |
| P1-1 | M-C3 判据**不可满足**（`rounds_run` 每轮必增；崩在轮中途时"记 after"不执行） | **已改 §C**：**boot 到 boot 比较** |
| P1-2 | `ResumeStalled` 没接桥 | **已改 §C**：走 trace + `initialize` 响应（**第 2 轮又发现 trace 也不可达，见 8.2**） |
| P1-3 | note 与 disarm 时序矛盾 | **已解决**：判定移到 `initialize` |
| P1-4 | `ACTION_TOOLS` 语义不符 | **已改 §A.3**：改用 `mutates_world`（**第 2 轮更正了 `subAgent` 的引用错误，见 8.2**） |
| P1-5 | 漏了 `DuplicateCallGuard` | **已补 §A.5** + 复用 `call_fingerprint` |
| P1-6 | 未更新 `data-contracts.md` | **已补 §四 #14** |
| P1-7 | M-C2 未交代 R10 的 deferral | **已补 §二**：框定为**潜在隐患** |
| P2-1 ~ P2-10 | 新造 digest / `output` 无界 / 64 条不够 / fd 生命周期 / try/except / 插入点 / 无效 Red / CI 矩阵 / 复用跳过 hooks / 并发安全 | **全部已改**（见 §A.2/§A.3/§A.4/§五） |
| 附录 B | `running` 档残留风险 | **已改 §一**：显式框定 |

### 8.2 方案评审（子智能体，第 2 轮）

结论：8 条里 **6 条真闭合**；**P1-2（trace 不可达）与 P1-4（`subAgent` 引用错误）未闭合**；
另新抓 1 个 P1、8 个 P2。逐条处置：

| 编号 | 内容 | 处置 |
|---|---|---|
| **P1-2（未闭合）** | trace recorder **不在** `initialize` 的观察者辫子里 —— `AgentLoop.__init__` 只包 `SafeObserver(base)`（`agent_loop.py:347`），trace 进辫子只在 `_bind_observer`（`:1734`），而它只被 `_begin_turn`（`:1269`）调用；disarm 分支不启动任何 turn → 事件落错会话目录（`trace.py:263` 的 `session_dir("")` → `root/"unknown"`） | **已改 §C**：新增公开方法 `AgentLoop.bind_session(session)`（内部 `_bind_observer(None)` + `_bind_trace(session)`），`initialize` 在 emit 前调它 |
| **P1-4（未闭合）** | `subagent_tool.py:357` 是 **`SubAgentListTool`**，不是 `SubAgentTool`；后者（`:119`）**继承 `mutates_world=True`** → 按方案自己的判据 `subAgent` **会**被 claim，与"本方案不为父调用记 write-ahead"直接矛盾 | **已改 §A.3**：更正引用；承认 `SubAgentTool`/`websearch`/`webfetch` 都继承 True，并论证**在"复用域 = 回合"下这是可接受的**（同回合重复的 `websearch` 本就命中 `DuplicateCallGuard`；`subAgent` 复用是收益） |
| 新③（P1） | **复用域是"整会话"**：`claim` 每次 `_decide` 都跑，sidecar 全量 → 跨回合复用 → `websearch`/`bash("npm install")` 在后续回合返回旧结果。方案实际变成了"整会话的调用缓存" | **已改 §A.1/§A.3**：**回合结束清空 sidecar** → 复用域 = 恢复窗口；跨回合不可能命中 |
| 新①（P2） | `SessionService.delete` 清了 4 个 sidecar，**没有** `write-ahead`（仓库第 5 次修同类孤儿） | **已改 §A.1**：`delete` 必须清 |
| 新②（P2） | sidecar 从不清空 → 按会话数无限增长 | **已改 §A.1**：回合结束清（与新③同一处修法） |
| 新④（P2） | 复用的 `ToolResult` 缺 `data`/`artifacts` → `turn_close.py:177` 的 `artifacts.extend` 取不到 → 回合 digest/handoff 少证据 | **已改 §A.2**：`WriteAheadIntent` 加 `data`/`artifacts`（有界） |
| 新⑤（P2） | 200 字符上限的理由已失效（sidecar 不影响会话文件）；且崩溃批次的 tool 消息**不在盘上**，续跑时模型只能看到尾部 | **已改 §A.2**：上限提到 **4000**，理由改为"崩溃批次的消息不在盘上" |
| 新⑥（P2） | `WriteAheadLog` 的 reset 位置未定 | **已改 §A.1/§A.3**：回合结束 `clear`（`TurnCloser.close`） |
| 新⑦（P2） | 写放大未量化（80 次写 × 64 KB ≈ 5 MB/回合） | **已改 §4.2**：写明量级 |
| 新⑧（P2） | `resume_count` 伪代码位置与实际不符（在异步 `run_resume`，不在 `initialize`） | **已改 §C**：指纹在 `initialize` 落盘，`resume_count` 仍在 `run_resume` 自增 |
| 新⑨（P2） | `resume_turn` 追加合成消息 → `len(messages)` 变化 → 掩盖真正停滞 | **已改 §C**：指纹用**非合成消息数**；新增 U13 |

### 8.3 实现评审（子智能体）

_待填。_
