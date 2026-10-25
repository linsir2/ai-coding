# M-B 实施方案 · 搜索收敛（B1 + B2 + B5）与 B3/B4 状态校正

> 状态：**待拍板**
> 关联：[roadmap-milestones.md](roadmap-milestones.md) §2 领域 B
> 调研日期：2026-10-25（全部结论均有代码/实跑证据）
> 目标：解决 T2 实测 **63 次工具调用 / 900 秒 / 零结论**

---

## 一、调研先纠错：B 领域 5 个里程碑里，**3 个的前提是错的**

roadmap 对 B 领域的判断写在 2026-10-24 的评估报告里，但其中三条与代码现状不符。先摆证据。

### 1.1 M-B3（重试分类）—— ✅ **已经完整实现，roadmap 前提完全错**

roadmap 说：

> `ai/openai_engine.py::_request_with_retry` 只对 `RateLimitError/ServiceUnavailableError/APIConnectionError/TimeoutExceeded` **统一指数退避** —— 没有"绝不重试"这一档

**实际代码**（[openai_engine.py:34-56](file:///workspace/ai-coding/ai_coding/ai/openai_engine.py#L34-L56)）：`FailureKind` **六档**枚举
（`RATE_LIMITED / SERVER_ERROR / TRANSPORT / AUTH_ERROR / CONTEXT_OVERFLOW / FATAL`），
`classify_failure()` 按 SDK 异常类型 + `status_code` 分类（[:117-138](file:///workspace/ai-coding/ai_coding/ai/openai_engine.py#L117-L138)）。

三档行为 **全部就位**：

| roadmap 要求的分档 | 代码里的实现 | 位置 |
|---|---|---|
| **NO_RETRY**：认证失败 | `if kind is FailureKind.AUTH_ERROR: raise FallbackWorthwhile(...)` —— 注释原文："Waiting cannot fix a rejected key… **Exits on the first attempt instead of after four identical 401s**" | [:352-358](file:///workspace/ai-coding/ai_coding/ai/openai_engine.py#L352-L358) |
| **NO_RETRY**：上下文超限 / 未知 | `CONTEXT_OVERFLOW` / `FATAL` 不在 `WORTH_ANOTHER_MODEL`，直接 `raise _exhausted(...)` | [:89-96](file:///workspace/ai-coding/ai_coding/ai/openai_engine.py#L89-L96), [:339-351](file:///workspace/ai-coding/ai_coding/ai/openai_engine.py#L339-L351) |
| **SERVER_HINT**：429 用 `Retry-After` | `retry_delay()` 的 `if kind is RATE_LIMITED and retry_after is not None: return min(max(0, retry_after), cap)` | [:158-177](file:///workspace/ai-coding/ai_coding/ai/openai_engine.py#L158-L177) |
| **EXPONENTIAL_BACKOFF** | `return min(base * (2**attempt), cap)` | [:177](file:///workspace/ai-coding/ai_coding/ai/openai_engine.py#L177) |
| 等待计入 `turn_timeout_seconds` | 引擎外层由 loop 包在 `run_with_deadline` 里（[:306-311](file:///workspace/ai-coding/ai_coding/ai/openai_engine.py#L306-L311) 的 docstring 明写） | ✅ |
| 不占 `max_tool_iterations` | 重试在**一次** `step()` 内部，不产生新迭代 | ✅ |

**实跑证据**（`max_retries=3`，注入 401）：

```
config max_retries = 3
raised: FallbackWorthwhile
completions.calls = 1        ← 只调了 1 次，没有重试
```

且已有测试 `test_an_authentication_error_is_not_retried`（[test_openai_engine.py:445-459](file:///workspace/ai-coding/tests/test_openai_engine.py#L445-L459)）断言 `calls == 1`。

> **结论**：M-B3 **不需要任何代码改动**。roadmap 里的 `RetryStrategy` / `RETRY_LADDER` 设计
> 会**重复造轮子**（`FailureKind` + `retry_delay` 已是同一件事），按最小化原则**砍掉**。

### 1.2 M-B4（检索类任务的预算约束）—— ⚠️ **大部分已实现**

roadmap 说缺口是"`max_tool_iterations=30` + `max_auto_continuations=4` = 150 步，烧掉 900s"。
但代码里**已有的两道闸**：

| 机制 | 位置 | 作用 |
|---|---|---|
| `budget_pressure()`（G8-1） | [turn_policy.py:507-540](file:///workspace/ai-coding/ai_coding/core/turn_policy.py#L507-L540) | 预算将尽时注入 `[budget] 步数 12/30，token 40,000/50,000` + "用这一轮收尾，不要再开新的工作" |
| 自动收尾（G8-2） | `agent_loop` 的 close-out 调用 | 预算耗尽后仍尝试一次"总结"调用 |
| 默认值 | `max_tool_iterations=10`（[models.py:49](file:///workspace/ai-coding/ai_coding/config/models.py#L49)）、`max_auto_continuations=3`（[:127](file:///workspace/ai-coding/ai_coding/config/models.py#L127)） | roadmap 的 30/4 是**评估时的 config**，不是默认值 |

**仍然缺的**（roadmap 第 ①点）：**"纯检索/诊断"意图用更紧的迭代预算**。
但那需要**意图分类** —— 要么让模型自报（多一次调用，不可靠），要么用 `turn_policy` 猜（启发式误判）。
**这正是我说的"说不清就砍"**：我无法给出一个可辩护的意图判定契约，所以**本方案不做**，交你拍板（§九）。

### 1.3 M-B1 / M-B2 / M-B5 —— ✅ **真正缺失，而且是同一件事**

| 里程碑 | 现状（已核实） |
|---|---|
| **B1** 静态检查回喂 | **完全不存在**：`grep -rn "ruff\|mypy\|静态检查\|lint" ai_coding/ai/prompt.py ai_coding/core/turn_policy.py ai_coding/core/interventions.py` → **零命中** |
| **B2** "成功但无进展"阶梯 | **完全不存在**：只有 `STRATEGY` 处理"工具**失败**"（[interventions.py:107-116](file:///workspace/ai-coding/ai_coding/core/interventions.py#L107-L116)），没有任何 gate 处理"工具**成功但没有新信息**" |
| **B5** 收敛可观测 | **完全不存在**：`events.py` 的 33 个事件里没有任何干预/收敛相关事件 |

**关键洞察**：B1 和 B2 是**同一个 gate 的不同级别**，B5 是这个 gate 的**事件**。
拆成三个里程碑做，会造出三套重复机制 —— 违反最小化原则。**合并成一个特性。**

---

## 二、要实现的内容（一句话）

给循环加**一个缺失的信号**（"这次检索有没有带来新信息"），
接入**已有的干预阶梯**作为一个新 gate（`SEARCH`），三级文案分别对应 B2 的换策略 / B1 的跑静态检查 / B2 的收尾交接，
并在 gate 触发时发**一个事件**（B5）。

**不新增任何并行机制**：复用现有的 `interventions` 阶梯、`digest_of`、`Lo opEvent`/trace 序列化。

---

## 三、原设计 vs 目前设计

### 3.1 干预阶梯的既有设计（**本方案不改它，只加一个 gate**）

```
TurnBooks                TurnSnapshot              interventions.select()
  累积计数  ──────────►  纯数据快照  ──────────►   _triggered → _due → _build
  (note_outcomes)        (snapshot())              ↓
                                                    TurnDecision(nudge/finish)
                                                          ↓
                                              loop: 注入合成消息 / 收尾
                                                          ↓
                                              interventions.record → ledger
                                              （下一轮 settle 判断 "helped"）
```

四个现有 gate 的触发条件：

| gate | 触发 | 阶段 |
|---|---|---|
| `STRATEGY` | 失败 streak ≥2 **或** 迭代 >3 且**零成功** | PRE |
| `FATIGUE` | `fatigue_index ≥ 30`（Paris 律累积误差损伤） | PRE |
| `TASK` | 有未完成 todo | POST |
| `ACCEPTANCE` | 有未通过的验收项 | POST |

**缺的那一格**："工具**都成功了**，但**没有新信息**" —— 这正是 T2 的形状：
63 次调用全部成功，全部是 grep/read，模型以为自己"还在进展"。

### 3.2 为什么不能用现有的 `guard.py` 的 `strict_repeats`

[guard.py:115](file:///workspace/ai-coding/ai_coding/core/guard.py#L115) 已有 `sha1(f"{call.name}:{payload}")`
—— 它抓的是**完全相同的调用**（同工具 + 同参数）。

T2 的实际形状是**不同的调用、相同的信息**：
`grep "httpx2"` → `grep "import httpx"` → `grep "http client"` → 三次不同参数、零新信息。

**两者互补，不重复**：`guard` 管"重复动作"，新 gate 管"重复信息"。

---

## 四、复用的方法契约（**一个都不新建**）

| 契约 | 位置 | 本方案怎么用 |
|---|---|---|
| `interventions` 阶梯（`_triggered`/`_due`/`_helped`/`_ref_for`/`_build`/`select`/`record`/`settle`） | [interventions.py](file:///workspace/ai-coding/ai_coding/core/interventions.py) | **复用**：`SEARCH` 作为第 5 个 gate 接入，形状与 `STRATEGY` 相同 |
| `LadderLimits` | [:81-90](file:///workspace/ai-coding/ai_coding/core/interventions.py#L81-L90) | **复用**：加一个阈值字段 |
| `digest_of(text, limit)` | [trace.py:112-115](file:///workspace/ai-coding/ai_coding/core/trace.py#L112-L115) | **复用**：算输出指纹（`limit=len(text)` 取全文，防头部碰撞） |
| `TurnBooks.note_outcomes(outcomes)` | [turn_books.py:331](file:///workspace/ai-coding/ai_coding/core/turn_books.py#L331) | **复用**：在这里累积新信号（它已经是所有 outcome 的唯一入口） |
| `TurnBooks.snapshot()` | [turn_books.py:391-428](file:///workspace/ai-coding/ai_coding/core/turn_books.py#L391-L428) | **复用**：把新字段带进快照 |
| `LoopEvent` 联合类型 + `dataclasses.asdict` 序列化 | [events.py:517-551](file:///workspace/ai-coding/ai_coding/core/events.py#L517-L551)、[trace.py:634](file:///workspace/ai-coding/ai_coding/core/trace.py#L634) | **复用**：新事件**自动**进 trace jsonl，无需登记、无需改前端 |
| `_note_intervention()` | [agent_loop.py:1968](file:///workspace/ai-coding/ai_coding/core/agent_loop.py#L1968) | **复用**：所有已触发干预的唯一落点，事件在这里发 |

**没有新增**：函数、模块、配置项（除 1 个阈值字段）、前端改动、新测试文件（加在既有文件里）。

---

## 五、对代码的影响 + 执行范围

### 5.1 改动清单（6 个源文件 + 3 份文档）

| # | 文件 | 改动 | 行数级 |
|---|---|---|---|
| 1 | `ai_coding/core/turn_policy.py` | `TurnSnapshot` 加 2 个字段（有默认值） | +4 |
| 2 | `ai_coding/core/turn_books.py` | `RETRIEVAL_TOOLS` 常量 + 3 个字段 + `note_outcomes` 里的信号计算 + `snapshot()` 传参 | +25 |
| 3 | `ai_coding/core/interventions.py` | `SEARCH` gate：常量/lead/阈值 + 5 个分派函数分支 + 3 段文案 + `select` 优先级 | +45 |
| 4 | `ai_coding/core/events.py` | 1 个新事件 dataclass + `LoopEvent` 联合加一项 | +15 |
| 5 | `ai_coding/core/agent_loop.py` | `_note_intervention` 里发事件 | +6 |
| 6 | `tests/test_interventions.py` | 阶梯表驱动用例 | +60 |
| 7 | `tests/test_turn_books.py` | 信号计算用例 | +40 |
| 8 | `docs/features.md` / `docs/data-contracts.md` / `docs/roadmap-milestones.md` | 说明 + 事件契约 + 状态校正 | — |

### 5.2 明确**不改**

- ❌ `openai_engine.py`（M-B3 已实现）
- ❌ `guard.py`（不同职责）
- ❌ `turn_policy.py` 的决策分支（只加字段，不加分支 —— gate 经 `interventions.select` 走）
- ❌ `frontend/`（trace jsonl 是通用的，事件自动出现）
- ❌ 不加 `RetryStrategy` / `RETRY_LADDER`（B3 已有等价物，**重复造轮子，砍**）
- ❌ 不做意图分类（B4 ①，无法给出可辩护契约，见 §九）

---

## 六、逐文件实现（每一步）

### 6.1 `ai_coding/core/turn_policy.py` —— `TurnSnapshot` 加两个字段

```diff
     #: 13 §2.1: successful ``ask_user`` calls this turn — the helped signal of
     #: the ask levels (L3 for strategy/fatigue, L2 for task/acceptance).
     ask_user_calls: int = 0
+    #: B1/B2/B5: how the *retrieval* calls have gone.  A call that returns text
+    #: already seen this turn taught the model nothing, and a run of those is the
+    #: measured shape of the T2 turn (63 successful calls, zero conclusions).
+    #: Fed in by the books like every other counter — the policy counts nothing.
+    search_calls: int = 0
+    search_stale: int = 0
```

> **为什么两个都要**：`search_stale` 是触发条件；`search_calls` 只用于文案与事件
> （"你检索了 11 次，最近 4 次没有新信息"）—— 只给一个数字的文案说服不了模型换策略。

### 6.2 `ai_coding/core/turn_books.py` —— 信号计算（**唯一的业务逻辑新增**）

```diff
+#: B1/B2: tools whose contract is "read existing state".  A successful call to one
+#: of these is informational: its output either teaches the turn something new or
+#: repeats what it already saw.  ``bash`` is deliberately absent — its output is not
+#: a stable view of anything (the same command can print different bytes), and a
+#: *repeated* call is already caught by ``guard.py``'s ``strict_repeats``.  The two
+#: are complementary: the guard catches a repeated action, this catches a repeated
+#: *result*.
+RETRIEVAL_TOOLS = frozenset(
+    {"read", "grep", "glob", "history_search", "memory_search", "websearch", "webfetch"}
+)
```

```diff
     def __init__(self) -> None:
         ...
         self.ask_user_calls = 0
+        #: B1/B2: retrieval calls this turn, and the trailing run of them whose
+        #: output was not new.  ``_seen_outputs`` holds fingerprints only, so a
+        #: turn that reads 40 files costs 40 x 16 bytes, not 40 x 40 KB.
+        self.search_calls = 0
+        self.search_stale = 0
+        self._seen_outputs: set[str] = set()
```

```diff
     def note_outcomes(self, outcomes: Sequence[DispatchOutcome]) -> None:
         ...
+        self._note_information_gain(outcomes)
         if not outcomes:
             return
```

```diff
+    def _note_information_gain(self, outcomes: Sequence[DispatchOutcome]) -> None:
+        """Track whether the retrieval calls are still teaching us anything (B1/B2).
+
+        A digest already in ``_seen_outputs`` is the definition of "no new
+        information": the same file read twice, a grep that matched the same
+        lines, a glob over an unchanged tree.  A *new* digest clears the run —
+        finding something ends the stall, which is exactly the helped-signal the
+        ladder's next window will look for.
+        """
+        for item in outcomes:
+            if not item.result.success or item.call.name not in RETRIEVAL_TOOLS:
+                continue
+            self.search_calls += 1
+            # The whole output, not digest_of's 4 KB default: two different files
+            # that share a boilerplate header must not look like the same finding.
+            text = item.result.output or ""
+            digest = digest_of(text, limit=len(text))
+            if digest in self._seen_outputs:
+                self.search_stale += 1
+            else:
+                self._seen_outputs.add(digest)
+                self.search_stale = 0
```

```diff
             ask_user_calls=self.ask_user_calls,
+            search_calls=self.search_calls,
+            search_stale=self.search_stale,
             plan_submitted="present_plan" in self.tool_success,
```

### 6.3 `ai_coding/core/interventions.py` —— `SEARCH` gate

```diff
 STRATEGY = "strategy"
 FATIGUE = "fatigue"
 TASK = "task"
 ACCEPTANCE = "acceptance"
+SEARCH = "search"
```

```diff
 STRATEGY_GATE_LEAD = "[strategy-gate]"
 FATIGUE_GATE_LEAD = "[fatigue-gate]"
 ACCEPTANCE_GATE_LEAD = "[acceptance-gate]"
+SEARCH_GATE_LEAD = "[search-gate]"
```

```diff
 class LadderLimits:
     fail_streak_threshold: int = 2
     stall_iterations: int = 3
     strategy_l1: int = 1
     fatigue_l1: int = 1
     task_l1: int = 2
     acceptance_l1: int = 2
+    #: B1/B2: consecutive retrieval calls that taught the turn nothing before the
+    #: ladder speaks.  3 is the smallest number that is not a coincidence: two in a
+    #: row is a normal "read the file, then read it again after editing it".
+    search_stale_threshold: int = 3
```

**四处分派函数**（形状与 `STRATEGY` 一致）：

```diff
 def _triggered(gate: str, snap: TurnSnapshot, limits: LadderLimits) -> bool:
     if gate == STRATEGY:
         return _strategy_triggered(snap, limits)
+    if gate == SEARCH:
+        # A zero threshold disables the gate, like stall_iterations.
+        return bool(
+            limits.search_stale_threshold
+            and snap.search_stale >= limits.search_stale_threshold
+        )
     if gate == FATIGUE:
```

```diff
 def _helped(gate: str, entry: Mapping[str, Any], snap: TurnSnapshot) -> bool:
     ...
     if gate == STRATEGY:
         ...
+    if gate == SEARCH:
+        # Helped = the run of fruitless calls was broken.  Compared against the
+        # baseline taken when the nudge fired, not against 0: a turn that had
+        # already stalled 3 times and then stalled 2 is still stalled.
+        return snap.search_stale < int(ref.get("stale", 0) or 0)
     if gate == FATIGUE:
```

```diff
 def _ref_for(gate: str, snap: TurnSnapshot) -> dict[str, Any]:
     if gate == STRATEGY:
         return {"asks": int(snap.ask_user_calls)}
+    if gate == SEARCH:
+        return {"asks": int(snap.ask_user_calls), "stale": int(snap.search_stale)}
     if gate == FATIGUE:
```

```diff
 def _due(gate, entry, limits) -> tuple[int, str] | None:
     ...
     if outcome == "failed":
         if gate == STRATEGY:
             return (L4, "finish") if level >= L3 else (level + 1, "nudge")
+        if gate == SEARCH:
+            # Three rungs, then the turn is handed over: L1 change the query,
+            # L2 run the static check, L3 write the handoff and stop.
+            return (L3, "finish") if level >= L3 else (level + 1, "nudge")
         if gate == FATIGUE:
```

```diff
 def select(snapshot, *, phase, limits) -> InterventionDecision | None:
     if phase == PRE:
-        gates: tuple[str, ...] = (STRATEGY, FATIGUE)
+        # B1/B2: SEARCH goes last, so a turn whose calls are also *failing* hears
+        # the strategy ladder's advice ("stop repeating the same failing call")
+        # before this one's ("change what you are looking for").
+        gates: tuple[str, ...] = (STRATEGY, FATIGUE, SEARCH)
```

**三级文案**（`_build` 里加分支 + 三个 `_search_text`）：

```diff
     if kind == "finish":
         if gate == STRATEGY:
             ...
+        if gate == SEARCH:
+            return InterventionDecision(
+                gate, level, "finish",
+                f"search stalled: {snap.search_stale} retrieval calls in a row "
+                "returned nothing new",
+            )
         if gate == FATIGUE:
```

```diff
     if gate == STRATEGY:
         reason = ...
         return InterventionDecision(gate, level, "nudge", reason, _strategy_text(level, snap))
+    if gate == SEARCH:
+        reason = (
+            "retrieval is repeating itself"
+            if level == L1
+            else f"retrieval is repeating itself (L{level})"
+        )
+        return InterventionDecision(gate, level, "nudge", reason, _search_text(level, snap))
```

```python
def _search_text(level: int, snap: TurnSnapshot) -> str:
    """The three rungs of the search ladder (B1 + B2).

    L1 says the query is the problem, not the code.  L2 hands over the one move
    that is cheaper than another guess -- the static check, which answers "where
    is this symbol used / is this import real" without reading a single file.
    L3 stops: a stalled search is a conclusion about the *turn*, and the operator
    needs the handoff more than another round.
    """
    stale = max(1, snap.search_stale)
    total = max(stale, snap.search_calls)
    if level <= L1:
        return (
            f"{SEARCH_GATE_LEAD} 最近 {stale} 次检索（共 {total} 次）没有带来任何新信息。"
            "换的是关键词，不是范围 —— 试试："
            "① 缩小到具体目录/文件而不是整仓库；"
            "② 换成符号名（函数/类名）而不是自然语言描述；"
            "③ 读一个你还没读过的文件，而不是再搜一次。"
        )
    if level == L2:
        return (
            f"{SEARCH_GATE_LEAD} 仍在原地：最近 {stale} 次检索没有新命中。"
            "**不要再猜关键词了** —— 直接用静态检查一次问清："
            "`ruff check .`（未定义名字、未用导入、真 bug 都在这里）、"
            "`mypy <包名>`（类型不符、模块不存在）、"
            "`grep -rn <符号> --include=*.py .`（谁在用它）。"
            "静态检查的结论比再读 10 个文件都准，而且只花一次调用。"
        )
    return (
        f"{SEARCH_GATE_LEAD} 检索已连续 {stale} 次无新信息（共 {total} 次），"
        "继续搜下去不会有新结论。现在必须收尾："
        "① 你已经确认的事实（带文件:行号）；"
        "② 还缺什么、下一步该跑哪条命令（越具体越好）；"
        "③ 不要再发起新的检索调用。"
    )
```

### 6.4 `ai_coding/core/events.py` —— 一个事件（B5）

```diff
+@dataclass(frozen=True)
+class SearchStalled:
+    """The search ladder fired (B2/B5): retrieval stopped teaching the turn.
+
+    One event per rung, so a trace answers "how many calls did this turn spend
+    before it started repeating itself" -- the question the T2 post-mortem could
+    not answer.  Trace-friendly by construction: ``asdict`` on this is the payload.
+    """
+
+    iteration: int
+    level: int
+    #: ``"nudge"`` (another round) or ``"finish"`` (the turn is being handed over).
+    kind: str
+    calls: int
+    stale: int
```

```diff
     | MemoryHarvested
     | MemoryConsolidated
+    | SearchStalled
 )
```

### 6.5 `ai_coding/core/agent_loop.py` —— 发事件

`_note_intervention` 是所有已触发干预的唯一落点（PRE 与 POST 两条路径都经过它）：

```diff
     @staticmethod
     def _note_intervention(
         session: SessionData, decision: TurnDecision, snapshot: TurnSnapshot
     ) -> None:
         """Persist one fired decision (搭 checkpoint/close 的车落盘)."""
         if not decision.intervention:
             return
+        # B5: the ladder's own choice is invisible in a trace today -- the nudge is
+        # a synthetic message, and a message says *what* was injected, never *why*.
+        # Only the search ladder emits: the other four already have their signal in
+        # the counters, and emitting for all five would change every existing trace.
+        if decision.intervention == interventions.SEARCH:
+            self._emit(
+                SearchStalled(
+                    iteration=snapshot.iteration,
+                    level=decision.level,
+                    kind=str(getattr(decision.action, "value", "")),
+                    calls=snapshot.search_calls,
+                    stale=snapshot.search_stale,
+                )
+            )
         session.intervention_state = interventions.record(
```

> ⚠️ 实施注意：`_note_intervention` 现在是 `@staticmethod`，`self._emit` 需要 `self` → 改成实例方法。
> 调用点（`agent_loop.py:827`、`:1051`）已经在实例上调用，签名不变。

### 6.6 测试

`tests/test_interventions.py`（表驱动，与既有风格一致）：

| # | 用例 | 断言 |
|---|---|---|
| T1 | `search_stale=0` 不触发 | `select(PRE)` 返回 `None` |
| T2 | `search_stale=3`（阈值）→ L1 nudge | `(gate, level, kind) == ("search", 1, "nudge")`，文案含 `SEARCH_GATE_LEAD` |
| T3 | L1 `failed` → L2 nudge | 文案含 `ruff check`（**B1 的核心断言**） |
| T4 | L2 `failed` → L3 **finish** | `kind == "finish"`，reason 含 `search stalled` |
| T5 | `helped`：`ref.stale=4`，当前 `stale=2` | `settle` 判 `outcome == "helped"` |
| T6 | 未 helped：`ref.stale=3`，当前 `stale=3` | 窗口关闭后 `outcome == "failed"` |
| T7 | 阈值 0 = 关闭 | `select` 返回 `None` |
| T8 | **优先级**：strategy 与 search 同时触发 | 返回的是 `strategy`（证明顺序） |

`tests/test_turn_books.py`：

| # | 用例 | 断言 |
|---|---|---|
| T9 | 同一 `read` 输出两次 | `search_calls=2, search_stale=2` |
| T10 | 两次不同输出 | `search_stale == 0`（新信息清零） |
| T11 | 检索 → 非检索 → 检索重复 | stale 不被非检索调用重置（只由"新信息"重置） |
| T12 | 失败的检索调用 | `search_calls` 不增（失败不算"没有新信息"，它算失败，归 strategy） |
| T13 | `bash` 重复输出 | 不计入（`RETRIEVAL_TOOLS` 不含 bash） |

---

## 七、数据流（含架构图）

### 7.1 新信号在既有链路里的位置

```
 模型发起 4 个工具调用
        │
        ▼
 dispatch() → [DispatchOutcome(call, result), ...]
        │
        ├──────────────────────────────► TurnBooks.note_outcomes()
        │                                   ├─ 既有: attempted/success/failed_streak/fatigue
        │                                   └─ 新增: search_calls / search_stale  ◄── 本方案
        │                                              （读 result.output → digest_of）
        ▼
 下一轮 decide_next_iteration()
        │
        ├─ 既有: 迭代/时钟/token/C3 闸
        ▼
 interventions.select(snapshot, phase=PRE)
        │   gates = (STRATEGY, FATIGUE, SEARCH)   ◄── 本方案加第 5 个
        ▼
 _triggered(SEARCH)  ← snapshot.search_stale >= 3        ◄── 本方案
        ▼
 _due(SEARCH, entry) → L1/L2 nudge, L3 finish            ◄── 本方案
        ▼
 _build(SEARCH, level) → 文案（L2 = "跑 ruff/mypy"）      ◄── 本方案（B1）
        ▼
 TurnDecision
        ├─► loop 注入合成 user 消息（既有机制）
        ├─► _note_intervention → emit SearchStalled        ◄── 本方案（B5）
        └─► interventions.record → ledger
                 ▼
            下一轮 settle：search_stale 降了 → "helped"
                           没降且窗口关 → "failed" → 升一级
```

### 7.2 T2 场景的预期轨迹（本方案生效后）

```
迭代 1  grep "httpx2"        → 新信息 (1 hit)      stale=0
迭代 2  read mcp.py:780-800  → 新信息              stale=0
迭代 3  grep "import httpx"  → 与迭代1同结果        stale=1
迭代 4  grep "http client"   → 无匹配（同空结果）    stale=2
迭代 5  glob "**/httpx*"     → 与迭代1同结果        stale=3
        ┌─────────────────────────────────────────┐
        │ SEARCH L1 nudge：换范围/换符号名          │  emit SearchStalled(level=1)
        └─────────────────────────────────────────┘
迭代 6  grep "client.py"      → 无匹配              stale=4
        ┌─────────────────────────────────────────┐
        │ SEARCH L2 nudge：**直接跑 ruff/mypy**     │  emit SearchStalled(level=2)  ← B1
        └─────────────────────────────────────────┘
迭代 7  bash "ruff check ."   → F401 httpx2         stale=0  ← 新信息，stalled 破
迭代 8  收尾，给出 mcp.py:787 的结论
```

**这就是 roadmap 要的 "≤5 次工具调用 / ≤120s"** 的机制来源：
不是砍预算，而是**在模型开始空转的第 3 次就告诉它"你在空转"**，第 5 次告诉它"去跑静态检查"。

---

## 八、修改成功后的效果

| 观测点 | 改前 | 改后 |
|---|---|---|
| 工具**成功**但无进展时的提示 | **无**（只有失败 streak 有 gate） | `SEARCH` L1/L2/L3 三级 |
| 静态检查的提示 | **零命中**（prompt/gate 里都没有 ruff/mypy） | L2 文案明确给出三条命令 |
| 收敛可观测 | trace 里读不到"检索了几次 / 是否收敛" | `SearchStalled` 事件每级一条 |
| 既有 4 个 gate | 不变 | **不变**（`search_stale` 默认 0，不触发） |
| 产品行为（未空转的回合） | — | **逐字节不变**（gate 不触发 = 无注入、无事件） |
| M-B3 | roadmap 说要做 | **已存在**，roadmap 校正 |
| M-B4 | roadmap 说要做 | **大部分已存在**，仅记录 |

### 8.1 按此计划实施后的最终模样（我的推演）

- 普通回合（读文件 → 改 → 跑测试）：`search_stale` 每次读到新内容就归零 → gate 永不触发 → 与今天**完全一致**
- 空转回合：第 3 次重复检索出现 `[search-gate]` 提示；第 5 次出现"跑 ruff/mypy"；第 7 次收尾
- trace jsonl 里出现 `{"name": "SearchStalled", "payload": {"iteration": 5, "level": 1, "kind": "nudge", "calls": 5, "stale": 3, ...}}`
- `ruff`/`mypy` 全绿；全量 pytest 0 failed，skipped 仍为 6
- `docs/features.md` 的干预阶梯表从 4 个 gate 变成 5 个

---

## 九、待拍板（3 项）

### 9.1 B1 的"注入 ruff/mypy **增量输出**"要不要做（还是只提示）

- **① 只提示（推荐，本方案）**：L2 文案告诉模型该跑哪条命令。理由：
  项目哲学是"模型跑工具，harness 只提示"；由 harness 自己跑 ruff 需要沙箱子进程 + 超时 + 输出预算 +
  "哪个包/哪个 venv"的判定（这是**新的一整套机制**，违反最小化）。
- **② harness 主动跑并把输出注入**：省一次模型调用，但需要新增：子进程执行、超时、输出截断、
  venv/解释器探测、配置开关。**估算 +150 行且引入 5 个新失败模式。**

### 9.2 M-B4 的 ①（"纯检索意图用更紧的迭代预算"）要不要做

- **① 不做（推荐）**：无法给出可辩护的"检索意图"判定契约（模型自报不可靠；启发式会误判正常重构回合）。
  且 `budget_pressure` + 自动收尾**已经**在预算将尽时叫停 —— B4 的验收判据（"≤180s 内给结论或收尾"）靠 B1+B2 就能达到。
- **② 做**：需要新增意图分类（`turn_policy` 加启发式 + 配置），我无法保证不误伤。

### 9.3 `search_stale_threshold` 默认值

- **① 3（推荐）**：2 太小（"读文件→编辑→再读"是正常操作）；4 太晚（T2 已经在烧钱）。
- **② 2 或 4**：给理由我再改。

---

## 十、自我核验

1. **有没有重复造轮子？** —— 三个候选都被否掉了：
   ① B3 的 `RetryStrategy`（`FailureKind` 已是同一件事）；
   ② 新的"重复调用"检测（`guard.py:115` 的 `strict_repeats` 已管重复**动作**，本方案管重复**信息**，互补）；
   ③ 新的阶梯框架（复用 `interventions` 的 5 个分派函数）。
2. **会不会误伤正常回合？** —— `search_stale` 只在"输出指纹已出现过"时增；任何新文件/新匹配/新行数都清零。
   默认阈值 3 且 `0` 可关闭。**普通回合行为逐字节不变**。
3. **`digest_of` 的 4096 默认够不够？** —— 不够（两个文件共享样板头会碰撞）→ 显式 `limit=len(text)`，并在代码里写明原因。
4. **`bash` 该不该算检索？** —— 不算。理由写在 `RETRIEVAL_TOOLS` 的注释里：bash 输出不是稳定视图，
   且重复调用已被 guard 拦截。
5. **失败的重试调用会不会污染 `search_stale`？** —— 不会，`result.success` 为假直接跳过（失败归 strategy）。
6. **事件会不会污染既有 trace？** —— 不会，只有 `SEARCH` 触发时才发；`TurnSnapshot` 新字段有默认值，
   既有测试构造 `TurnSnapshot(**base)` 不受影响。
7. **`select` 加一路 gate 会不会改变既有优先级？** —— 不会。`SEARCH` 排在最后，且 `search_stale` 默认 0 不触发；
   T8 用例专门锁这条。
8. **B3 真的不用改？** —— 已实跑：`max_retries=3` + 401 → `calls == 1`。roadmap 前提错，需校正（不是"修完"，是"本就存在"）。
9. **B5 的 `/trace` 面板要不要改？** —— 不改。B5 的验收判据是"trace jsonl 里能读到事件"，
   而 `trace.py:634` 的 `asdict` 让任何新事件自动落盘。

---

## 十一、边界声明

| 项 | 归属 |
|---|---|
| **本方案改** | 5 个源文件 + 2 个测试文件 + 3 份文档 |
| **不改** | `openai_engine.py`（B3 已实现）、`guard.py`、`turn_policy.py` 的决策分支、`frontend/` |
| **不做** | B4 的意图分类、B1 的 harness 主动跑静态检查（见 §九） |
| **需校正** | roadmap 的 M-B3（已完成）、M-B4（大部分已完成） |
