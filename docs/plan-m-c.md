# 领域 C · 恢复与持久化（M-C1 ~ M-C3）

> 调研：两轮联网（55+ 引用源，全文见 [research-persistence-crash-recovery.md](research-persistence-crash-recovery.md)）
> + 两轮仓库核实（子智能体）。方案与评审记录见 §八。
>
> 本稿为**第 2 版**，已按子智能体方案评审（§八）修订。

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

**必须诚实框定**（评审 P1-7）：`MemoryStore` 的 docstring 自己写了两句关键的话 ——
*"A single-writer store is fine; **that is every real path in this repo**"* 与
*"**R10 reviewed this and chose to record it rather than restructure persistence**"*。
今天确实全部单线程（`recall_with_diagnostics` 里的 `touch_notes` 是直接同步调用，
没有 `to_thread`）。**所以 M-C2 修的是"把 store 写操作移到线程之前必须先修的隐患"，
不是一个正在漏的洞。** 方向与 docstring 自己给的修法一致。

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
    → 回填: { status: "ok"/"failed", output: 摘要 }
再次出现同一 (tool, digest) 且 status == "ok"
    → 直接复用该结果，【不执行】
```

**为什么是 `(tool, digest)` 而不是 `callId`**：`resume_turn` 会让模型**重新生成**
工具调用，新调用的 `callId` 与崩溃前不同。能对齐的只有**意图**（工具 + 规范化参数）。

#### A.1 落点：**sidecar**，不是会话字段（评审 P0-1）

> **P0 修正**：初版方案写"落盘走 `TurnCloser.save`"。那是**错的** ——
> `TurnCloser.save` 的第一件事是 `ensure_tool_pairing()`（`turn_close.py:208`），
> 它会**丢弃没有配对结果的 assistant 载体**。而 `claim` 必须在 `_execute` **之前**
> 调（`dispatch.py:514`），此刻 `write_back` 还没跑（`agent_loop.py:1951`）→
> 载体被丢 → 随后写回的 tool 消息成了孤儿 → 下次落盘再被丢。
>
> **这个坑仓库里已经踩过三次，三次都写了注释明令禁止**：
> `agent_loop.py:1794`（"Deliberately not a session save … destroying the pending
> call (verified)"）、`session_service.py:122/168/212`、`data-contracts.md:68`。
> 审批等待就是为此改成 sidecar 的。

**所以 write-ahead 走 sidecar**，照抄 `mark_pending_approval` 的形状：

* `SessionService` 加 `_write_ahead_path` / `mark_write_ahead(session_id, records)` /
  `_read_write_ahead(session_id)`，写到 **`sessions/write-ahead/<id>.json`**。
  * **必须是子目录**：会话扫描 glob `*.json`，`session_service.py:134-138` 记录了
    "兄弟 marker 被 `list_sessions` 读成幽灵空会话"的坑。
* `SessionData.write_ahead` 改为**运行时字段**（`pending_approval` 同款，`load` 时从
  sidecar 读回，`session_service.py:283` 是现成的接线点），**不进 `to_dict` 白名单**。
  → 会话 JSON 不因此变大，save 节奏不变（每个 golden test 钉着的节奏）。

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
    """

    call_id: str
    tool: str
    #: ``call_fingerprint(call)`` -- reused from ``core/guard.py``, not reinvented.
    digest: str
    #: "running" | "ok" | "failed"
    status: str = "running"
    #: Tail of the output, capped (see §A.3).  The full result already rides the
    #: history as a tool message.
    output: str = ""
```

* **`digest` 复用 `core/guard.py:109` 的 `call_fingerprint`**（sha1 截 12，
  `(tool, params)` 顺序不敏感、值敏感）—— 不新造第二个指纹函数（评审 P2-1）。
  `DuplicateCallGuard` 已经在用它拦"同参重复调用"（阈值 2、只数成功、跨轮重置），
  write-ahead 补的是它缺的**跨崩溃**那一半（§A.4）。

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
    """

    def claim(self, call: ToolCall) -> ToolResult | None: ...
    def settle(self, call: ToolCall, result: ToolResult) -> None: ...
```

* **覆盖哪些工具**：`item.tool.mutates_world`（评审 P1-4）。
  * `ACTION_TOOLS`（M-B）**不是**这份语义 —— 它是搜索连胜阶梯里"什么算 acting"
    的清单（`turn_books.py:79-87` 的 docstring 原文），与"改世界"不等价：
    `job_kill`/`memory_write` 是 `mutates_world=True` 却不在其中，而 `bash`
    在其中有自己的理由（"为了奖励跑静态检查"）。
  * `mutates_world` 是**工具自己声明的**元数据（`base.py:167`，默认 True），
    也是 dispatcher 只读门的同一份判据 → **同一份语义，不新造清单**。
  * **已知边界**：`subAgent` 声明为 `mutates_world=False`（`subagent_tool.py:357`）。
    子智能体的副作用经它自己的工具产生，记在子会话里 —— 本方案**不**为父调用记
    write-ahead。这是有意的（子会话有自己的持久化路径），也是既有的声明。
* **`ephemeral` 会话不记**：plan 步会话不落盘（`turn_close.py:206`），
  sidecar 是纯开销。`WriteAheadLog` 构造时判一次即可。
* **有界**：保留最近 **256** 条（评审 P2-3：`max_iterations=10 × max_parallel=4 = 40`，
  重放再 +40，64 不够）。**`output` 截断到 200 字符**（评审 P2-2）——
  有界条数 × 无界长度仍会撑大会话文件。

#### A.4 接线

| 位置 | 改动 |
|---|---|
| `domain/write_ahead.py`（新） | `WriteAheadIntent`（dataclass + to/from_dict） |
| `domain/session.py` | `write_ahead: list[WriteAheadIntent] = field(default_factory=list)`（**运行时**，非落盘字段） |
| `service/session_service.py` | `_write_ahead_path` / `mark_write_ahead` / `_read_write_ahead`；`load` 里读回 |
| `core/write_ahead.py`（新） | `WriteAheadLog` |
| `tools/base.py` | `ToolContext.write_ahead: Any = None`（与 `jobs` 同款 duck-typed） |
| `core/request_builder.py` | `tool_context` 透传 |
| `core/agent_loop.py` | `_run_tools` 造 log 并传入 |
| `core/dispatch.py` | `_decide` 里 claim/settle |

**精确插入点**（评审 P2-6）：

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
* **复用时跳过 `guards.after` 与 `TOOL_AFTER` hooks**（评审 P2-9）：它们的输入是
  "刚刚执行的结果"，而复用没有执行。`TOOL_BEFORE` 与 guards 的 `check` **已经跑过**
  （在 claim 之前），所以权限语义不 bypass。**这是有意的**，写入 `WriteAheadLog`
  的 docstring。

#### A.5 与现有机制的关系

| 机制 | 粒度 | 时机 | 消费者 | 与本方案 |
|---|---|---|---|---|
| `resume_turn` | 回合 | 崩溃后 | loop | **互补**：它决定"从哪继续"，本方案保证"继续后不重复副作用" |
| `StepDelivery` | 步骤 | **事后**（结算） | replay | **不合并**：事后收据 vs 事前声明 |
| `DuplicateCallGuard` / `call_fingerprint` | 单次调用 | 执行前 | guard | **互补**：它拦"同一进程内的同参重复"（阈值 2、跨轮重置），本方案补**跨崩溃**那一半；`digest` 直接复用它 |
| 审批 sidecar（`mark_pending_approval`） | 单次调用 | 等待中 | `resume_turn` | **同形**：中批次耐久状态的唯一正确落点 |

### B. M-C2 · 记忆并发写安全

**三层**：

1. **`_save` 进锁**：`_mutate` 改成
   ```python
   with self._lock:
       result = fn()
       self._save()
   return result
   ```
   **必须保留 `_save` 外层的 try/except**（评审 P2-5）：`to_json_pretty` 可能抛非
   `OSError`（例如 `default=` 处理不了的 object），逃逸出锁会让后续写全部卡死。
   `_save` 内部已有 `except OSError`；`_mutate` 外层再包一层 `except Exception`
   （与今天一致）。
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
    between, and the two end up interleaved (review P2-4).  ``fcntl`` is
    POSIX-only; elsewhere this degrades to a bare ``os.replace`` and the
    docstring says so -- the process-internal lock still applies.
    """
```

* **跨平台**：`fcntl` 用 `try/except ImportError` 可选导入；不可用时**不假装修了**，
  docstring 与 `data-contracts` 明写"跨进程保护仅 POSIX"。
* **诚实框定**（§二）：今天所有真实路径都是单写者，这是**潜在隐患的预防性修复**，
  不是正在漏的洞。
* **不动 `session_service.py` 的 4 处 `tmp + os.replace`** —— 会话写是单写者
  （event loop + `_turn_lock`），无对应缺口。

### C. M-C3 · 崩溃后续跑的验收门

**判据**（评审 P1-1 修正后）：**boot 到 boot 比较**，而不是 run 的首尾比较。

> **为什么**：初版判据"续跑前后 `(rounds_run, tokens_used, len(messages))` 相等"
> **不可满足** —— `rounds_run` 在两个 profile 里每轮必增（`plan_runner.py:715`、
> `goal_runner.py:261`），跑完 ≥1 轮就必变；而崩在轮中途时"记 after"这行代码
> 根本不会执行。唯一相等的情形是 0 轮的 resume。

**修正后的形状**：

```
boot N 的 initialize:
    若 resume_fingerprint 已存在（上一次续跑留下的）:
        now = fingerprint(task, session)
        now == resume_fingerprint  → no_progress_resumes += 1   ← 上次续跑什么都没做
        否则                      → no_progress_resumes = 0
    若 no_progress_resumes >= 2 → disarm + ResumeStalled + note（同一个响应）
    否则:
        resume_fingerprint = fingerprint(task, session)        ← 先存"之前"
        resume_count += 1; persist
        → _launch_armed_run(boot=True)
```

* **指纹** = `(rounds_run, tokens_used, len(messages), len(step_deliveries))`。
  四者全等 = **上一次续跑没有完成任何一轮、没产生任何消息、没结算任何步骤**。
* **为什么 2 次**：一次可能是崩溃在很早期（`resume_turn` 刚开头）。
* **disarm 与 note 在同一个 `initialize` 响应里**（评审 P1-3）—— 判定发生在
  `initialize`（同步），不在异步 run 里。
* **"0 轮"不等于"已完成计划"**：已完成的计划已被 runner disarm（`goal_runner.py:167`
  的 `finally`），不会再 armed 到 boot。

**交接报告**：新增 `ResumeStalled` 事件（结构化：`resumes`/`no_progress`/`before`/`after`），
进 `LoopEvent` → **自动落 trace jsonl**（`dataclasses.asdict`，无需登记），
并在 `initialize` 的响应里给一条 note。
* **不改前端**（评审 P1-2）：`bridge.to_wire` 是白名单，加分支要动前端 reducer；
  本方案把报告放进**已在流动的** trace + `initialize` 响应，不新增 UI 通道。

---

## 四、逐文件改动清单

| # | 文件 | 改动 | 为什么 |
|---|---|---|---|
| 1 | `ai_coding/domain/write_ahead.py`（新） | `WriteAheadIntent` | M-C1 的 sidecar 契约 |
| 2 | `ai_coding/domain/session.py` | `write_ahead`（运行时字段）+ reset | 契约挂载 |
| 3 | `ai_coding/service/session_service.py` | sidecar 三方法 + `load` 读回 | M-C1 的落点 |
| 4 | `ai_coding/core/write_ahead.py`（新） | `WriteAheadLog` | M-C1 的行为 |
| 5 | `ai_coding/tools/base.py` | `ToolContext.write_ahead` | 传给 dispatcher |
| 6 | `ai_coding/core/request_builder.py` | `tool_context` 透传 | 同上 |
| 7 | `ai_coding/core/agent_loop.py` | `_run_tools` 造 log 并传入 | 接线 |
| 8 | `ai_coding/core/dispatch.py` | `_decide` 里 claim/settle（精确点见 §A.4） | M-C1 的落点 |
| 9 | `ai_coding/memory/store.py` | `_save` 进锁 + 唯一 tmp + `_replace_under_lock`；docstring 重写 | M-C2 |
| 10 | `ai_coding/domain/task.py` | `resume_fingerprint`、`no_progress_resumes`；to/from_dict | M-C3 的状态 |
| 11 | `ai_coding/core/events.py` | `ResumeStalled` + 进 `LoopEvent` | M-C3 的报告 |
| 12 | `ai_coding/server/handlers.py` | 指纹比较 + 提前 disarm + 发事件 + note | M-C3 的落点 |
| 13 | `docs/data-contracts.md` | 三条契约 | 评审 P1-6 |

> **不新造轮子**：`call_fingerprint` 复用的；`mutates_world` 复用的；
> sidecar 形状复用的；`tmp + os.replace` 不变重复用的；trace 落盘复用的。

### 4.1 语义等价性

* **无 write-ahead 时**（非 `mutates_world` / `ephemeral` / 无 log）：行为与今天
  **逐字节相同**。
* **有 write-ahead 时**：副作用工具**照常执行**，只是多一个 sidecar 文件。
  只有当**同一 `(tool, digest)` 在同一会话里第二次出现**时才复用。
* **会话 JSON 不变大**（write-ahead 在 sidecar 里）；save 节奏不变。
* **M-C2**：单写者路径行为不变；并发路径从"可能损坏"变成"不损坏"。
* **M-C3**：有前进的续跑行为不变；只新增"连续 2 次无前进 → 提前终止"。

### 4.2 对数据流的影响

* **新增 sidecar 目录** `sessions/write-ahead/`（子目录，避免幽灵会话）。
* **副作用工具多一次 sidecar 写**（`mark_write_ahead` 每次 claim/settle 各一次）。
* **`resume_turn` 的语义被修正**：续跑后模型重复生成的副作用调用会被复用 ——
  今天会执行两次。

---

## 五、RIPR 用例

### Red（先写，必须变红）

| 编号 | 用例 | 期望红相（今天必须失败） |
|---|---|---|
| U1 | `test_write_ahead.py::test_a_side_effect_tool_is_claimed_before_it_runs` | 工具执行中读 sidecar → 已有 `status:"running"`（今天：无 sidecar） |
| U2 | `test_write_ahead.py::test_the_outcome_is_settled_after_the_call` | 执行后 → `status:"ok"` + output 摘要（今天：无） |
| U3 | `test_write_ahead.py::test_a_regenerated_call_reuses_the_recorded_result` | 同 `(tool, digest)` 第二次 → **不执行**（fake tool 计数 1）（今天：2 次） |
| U4 | `test_write_ahead.py::test_the_reuse_is_visible_to_the_model` | 复用结果 `data.reused is True`（今天：无） |
| U5 | `test_write_ahead.py::test_a_read_only_tool_is_not_claimed` | `read`/`grep` 不产生 sidecar（今天：无机制 → 断言"sidecar 不存在"在实现后仍成立，**本条是 Prove 不是 Red**） |
| U6 | `test_write_ahead.py::test_the_claim_does_not_disturb_the_session_file` | claim 后读会话 JSON → `messages` 里在飞的工具调用**没被丢**（今天：若走 save 会被丢；本条锁住 P0-1 的修法） |
| U7 | `test_write_ahead.py::test_the_digest_is_call_fingerprint` | `digest == call_fingerprint(call)`（今天：无 digest） |
| U8 | `test_memory_store.py::test_concurrent_writes_never_corrupt` | 4 线程 × barrier × 25 次写 → 0 损坏 + 重载条数一致（今天：4-6 次损坏） |
| U9 | `test_memory_store.py::test_two_processes_never_corrupt` | 两进程各写 25 次 → 文件始终合法（今天：无跨进程保护） |
| U10 | `test_resume_gate.py::test_one_stalled_resume_does_not_stop_the_task` | 指纹不变 1 次 → 不 disarm（今天：无判据） |
| U11 | `test_resume_gate.py::test_two_stalled_resumes_stop_the_task` | 连续 2 次不变 → disarm + `ResumeStalled`（今天：要 5 次） |
| U12 | `test_resume_gate.py::test_progress_resets_the_counter` | 中间有一次前进 → 计数归零（今天：无） |

> U5 是 **Prove**（它断言的是"没有记录"，实现前后都成立），不是 Red —— 初版把它
> 误列为 Red（评审 P2-7）。

### Prove

1. `.venv/bin/python -m pytest -q`
2. `.venv/bin/ruff check .` / `.venv/bin/mypy ai_coding`
3. **突变测试**（每层一处）：去掉 `claim` 的复用分支 → U3/U4 变红；把 `_save`
   挪出锁 → U8 变红；去掉 `no_progress_resumes >= 2` → U11 变红
4. **3.10 venv 跑 CI 同构七步**（CI 矩阵是 **3.10 + 3.13**，本地 venv 是 3.14 —
   评审 P2-8："与 CI 同构"指的是步骤，不是解释器）
5. **端到端**：真跑一个会话 → `write`/`bash` 后读到 sidecar；杀掉进程 →
   重启续跑 → 断言副作用工具没有第二次执行

### Refactor

* 确认 `WriteAheadLog` 的上限与 `output` 截断**都有界**
* 确认 `call_fingerprint` 是**唯一**的 `(tool, params)` 指纹（没有第二份）

---

## 六、验收判据

* **M-C1**：`mutates_world` 工具执行前写 sidecar `running`、执行后写 `ok`/`failed`；
  同一 `(tool, digest)` 再次出现时**不执行**并复用结果；只读工具与 `ephemeral`
  会话不写；**会话文件不受影响**（P0-1 的回归锁）；记录有界。
* **M-C2**：4 线程 × barrier × 25 次写 **0 损坏**，重载后条数一致；两进程并发写
  文件始终合法。
* **M-C3**：boot 到 boot 指纹连续 2 次不变 → 提前 disarm + `ResumeStalled`；
  有前进 → 计数归零。
* 3.10 与 3.13 全量绿；真实 CI 双 job 绿。

---

## 七、不做的事

* **不改 `resume_turn` 的语义** —— 它决定"从哪继续"，本方案保证"继续后不重复副作用"。
* **不合并 `StepDelivery` 与 `WriteAheadIntent`** —— 事后收据 vs 事前声明。
* **不新造指纹函数** —— 复用 `call_fingerprint`。
* **不引入配置项**（哪些工具要 write-ahead）—— 用 `mutates_world`。
* **不改 `session_service.py` 的 4 处会话 `tmp + os.replace`** —— 会话写是单写者。
* **不改前端** —— 报告走 trace + `initialize` 响应。
* **不做 Outbox / 租约 / 围栏令牌** —— 那是分布式多 Worker 的解法；`running` 档的
  残留窗口（§一 的框定）本方案**不封**，并写进了 `WriteAheadIntent` 的 docstring。

---

## 八、评审记录

### 8.1 方案评审（子智能体，第 1 轮）

结论：调研质量高，三条缺口的代码断言逐条核对**基本准确**；1 个 P0、7 个 P1、10 个 P2。
逐条处置：

| 编号 | 内容 | 处置 |
|---|---|---|
| **P0-1** | 用 `TurnCloser.save` 落 write-ahead 会**丢弃在飞的工具调用** —— `save` 先跑 `ensure_tool_pairing()`（`turn_close.py:208`），而 `claim` 在 `_execute` **之前**（`dispatch.py:514`），`write_back` 还没跑（`agent_loop.py:1951`）→ 载体被丢、写回成孤儿。仓库已三次明令禁止（`agent_loop.py:1794`、`session_service.py:122/168/212`、`data-contracts.md:68`） | **已改 §A.1**：write-ahead 落到**sidecar** `sessions/write-ahead/<id>.json`（照抄 `mark_pending_approval`），`SessionData.write_ahead` 改为**运行时字段**（`load` 时读回，`session_service.py:283` 是现成接线点），**不进 `to_dict`** → 会话 JSON 不变大、save 节奏不变。新增 U6 锁住这个回归 |
| P1-1 | M-C3 判据**不可满足**：`rounds_run` 每轮必增（`plan_runner.py:715`、`goal_runner.py:261`）→ 跑完 ≥1 轮必变；崩在轮中途时"记 after"根本不执行 | **已改 §C**：改为 **boot 到 boot 比较** —— 指纹在续跑**前**持久化，在**下一次** `initialize` 比较；判据 = 四元组全等（`rounds_run` + `tokens_used` + `len(messages)` + `len(step_deliveries)`） |
| P1-2 | `ResumeStalled` 没接桥，`bridge.to_wire` 是白名单，前端 reducer 会丢弃 | **已改 §C**：报告走 **trace（自动）+ `initialize` 响应 note**，明确**不改前端** |
| P1-3 | "initialize 响应里给 note" 与 disarm 在异步 run 里矛盾 | **已解决**：判定移到 `initialize`（同步），note 与 disarm 同一响应 |
| P1-4 | `ACTION_TOOLS` 语义是"搜索连胜阶梯里什么算 acting"，**不是**"改世界"：`job_kill`/`memory_write` 是 `mutates_world=True` 却不在其中 | **已改 §A.3**：改用 **`item.tool.mutates_world`**（工具自己声明的元数据，也是只读门的同一判据）。`subAgent` 声明为 False 是已知边界，写入 docstring |
| P1-5 | 漏了 `DuplicateCallGuard` / `call_fingerprint` | **已补 §A.5** 表格 + §A.2 复用 `call_fingerprint` |
| P1-6 | 未更新 `data-contracts.md` | **已补 §四 #13** |
| P1-7 | M-C2 未交代 R10 的显式 deferral 与"单写者才是真实路径" | **已补 §二**：明确框定为**潜在隐患的预防性修复**，不是正在漏的洞 |
| P2-1 | 新造 digest 与 `call_fingerprint` 重复 | **已改**：复用 `call_fingerprint`（sha1 截 12） |
| P2-2 | `output` 无长度上界 | **已改**：截断 200 字符 |
| P2-3 | 64 条上限不够（重放叠加 40+40） | **已改**：256 条 |
| P2-4 | `_replace_under_lock` 未说明 fd 生命周期 | **已改**：锁必须覆盖 `os.replace`，写入 docstring |
| P2-5 | `_mutate` 重写丢了 `_save` 外层 try/except | **已改**：保留外层 `except Exception` |
| P2-6 | claim/settle 精确插入点未定义 | **已改 §A.4**：`precheck` 之后、`_execute` 之前，用 rewrite 后的 params |
| P2-7 | U5/U6/U10/U12 不是有效 Red | **已改**：U5 明确标为 Prove；U6 改成"锁住 P0-1 回归"的有效 Red |
| P2-8 | "3.14 全量绿"与 CI 矩阵不符 | **已改 §六**：CI 是 3.10 + 3.13；"同构"指步骤 |
| P2-9 | 复用时跳过 `guards.after`/hooks | **已改 §A.4**：明确这是有意的，写入 docstring |
| P2-10 | 并发安全未提（ALLOW 组 `gather`） | **已改 §A.3**：`claim`/`settle` 必须同步，写入 docstring |
| 附录 B | `running` 档的残留风险未框定 | **已改 §一**：显式写明 M-C1 只封"成功后崩溃"那一半，`running` 仍会重跑 |

### 8.2 方案评审（子智能体，第 2 轮）

_待填。_

### 8.3 实现评审（子智能体）

_待填。_
