# M-B 实施方案 · 搜索收敛（B1 + B2 + B5）与 B3/B4 状态校正

> 状态：**待拍板（v2，已按方案评审的 2 个 P0 重新设计）**
> 关联：[roadmap-milestones.md](roadmap-milestones.md) §2 领域 B
> 调研：2026-10-25 · 方案评审：子智能体 worktree 独立实跑（见 §十二）
> 目标：解决 T2 实测 **63 次工具调用 / 900 秒 / 零结论**

---

## 一、调研先纠错：B 领域 5 个里程碑里，**3 个的前提与代码不符**

### 1.1 M-B3（重试分类）—— ✅ **已完整实现**

roadmap 说"只对四类异常统一指数退避，没有'绝不重试'这一档"。实际：

| roadmap 要求 | 代码实现 | 位置 |
|---|---|---|
| NO_RETRY：认证失败 | `if kind is FailureKind.AUTH_ERROR: raise FallbackWorthwhile(...)`，注释原文 "**Exits on the first attempt instead of after four identical 401s**" | [openai_engine.py:352-358](file:///workspace/ai-coding/ai_coding/ai/openai_engine.py#L352-L358) |
| NO_RETRY：上下文超限 / 未知 | `CONTEXT_OVERFLOW`/`FATAL` 不在 `WORTH_ANOTHER_MODEL` → 直接 raise | [:89-96](file:///workspace/ai-coding/ai_coding/ai/openai_engine.py#L89-L96) |
| SERVER_HINT：429 用 `Retry-After` | `if kind is RATE_LIMITED and retry_after is not None: return min(max(0,retry_after),cap)` | [:158-177](file:///workspace/ai-coding/ai_coding/ai/openai_engine.py#L158-L177) |
| EXPONENTIAL_BACKOFF | `min(base * 2**attempt, cap)` | [:177](file:///workspace/ai-coding/ai_coding/ai/openai_engine.py#L177) |

**实跑**（`max_retries=3` + 401）：`completions.calls = 1`（不重试），抛 `FallbackWorthwhile`。

> **roadmap 的 `RetryStrategy` / `RETRY_LADDER` 是重复造轮子 → 砍。**
>
> ⚠️ 评审 P2-7 找到一个**真实小缺口**：429 的 `insufficient_quota`（配额用尽）被归类为
> `RATE_LIMITED` → 会重试满 4 次，而 roadmap 要求它属 NO_RETRY。见 §九 待拍板 3。
> **I1/R-1 后续修正**：它现在是独立的 `FailureKind.QUOTA` —— 不重试（首次即切备用
> provider），既保留了 NO_RETRY 的"不空转"，又不再把额度耗尽误当成"请求本身有问题"。

### 1.2 M-B4（检索预算）—— ⚠️ **大部分已实现，且我有夸大**

| 机制 | 位置 | 真实作用 |
|---|---|---|
| `budget_pressure()`（G8-1） | [turn_policy.py:507-540](file:///workspace/ai-coding/ai_coding/core/turn_policy.py#L507-L540) | 预算将尽时注入 `[budget] 步数 12/30，token …` + "用这一轮收尾" |
| 自动收尾（G8-2） | [agent_loop.py:2040-2059](file:///workspace/ai-coding/ai_coding/core/agent_loop.py#L2040-L2059) | **只在 iteration 预算分支**触发（评审 P2-6 校正；clock/token 收尾不走这条） |
| 默认值 | `max_tool_iterations=10`（[models.py:49](file:///workspace/ai-coding/ai_coding/config/models.py#L49)）、`max_auto_continuations=3`（[:127](file:///workspace/ai-coding/ai_coding/config/models.py#L127)）、`config.local.yaml:16` 也是 10 | roadmap 的 30/4 **无法从仓库证实**（评审 P3-3；只能作为假设） |

**仍缺**：roadmap 第 ①点"纯检索意图用更紧预算" —— 需要意图分类，我给不出可辩护契约 → **不做**（§九 待拍板 2）。

### 1.3 M-B1 / M-B2 / M-B5 —— ✅ **真正缺失，且是同一件事**

- **B1 静态检查回喂**：`grep -rn "ruff\|mypy\|静态检查\|lint" ai_coding/ai/prompt.py ai_coding/core/turn_policy.py ai_coding/core/interventions.py` → **零命中**
- **B2 "成功但无进展"阶梯**：只有 `STRATEGY` 处理工具**失败**（[interventions.py:107-116](file:///workspace/ai-coding/ai_coding/core/interventions.py#L107-L116)），没有任何 gate 处理"工具**成功**但没推进"
- **B5 收敛可观测**：33 个事件里没有任何干预/收敛事件

**B1 和 B2 是同一个 gate 的两级，B5 是这个 gate 的事件。合并成一个特性。**

---

## 二、🔴 方案 v1 被评审否决的信号设计（必须记录，否则会重犯）

v1 提议：对检索工具的输出算 `digest_of(output)`，指纹已出现过就算"无新信息"。

**评审 P0-2 + 我的复核：这个定义双向都是错的，而且物理上达不到阈值。**

| # | 证据（实跑/读码） | 后果 |
|---|---|---|
| 1 | `DuplicateCallGuard` 默认 `threshold=2`（[models.py:218](file:///workspace/ai-coding/ai_coding/config/models.py#L218)），`check()` 在 `previous >= 2` 时 **DENY**（[guard.py:231-251](file:///workspace/ai-coding/ai_coding/core/guard.py#L231-L251)） | 相同 (tool,params) 最多成功 **2** 次，第 3 次被拒（`success=False` → 我的代码跳过）→ **`stale` 永远到不了 3** |
| 2 | `grep` 与 `glob` 的无匹配输出**逐字节相同**：都是 `ToolResult(success=True, output="(no matches)")`（[grep_tool.py:206](file:///workspace/ai-coding/ai_coding/tools/grep_tool.py#L206)、[glob_tool.py:145](file:///workspace/ai-coding/ai_coding/tools/glob_tool.py#L145)）；实跑两个 digest 都是 `d5fd3d5729146fc1` | **跨工具碰撞** → 3 次不同关键词的 no-match 立即误判"空转"（false positive） |
| 3 | `glob` 按 **mtime 倒序**排序（[glob_tool.py:127-132](file:///workspace/ai-coding/ai_coding/tools/glob_tool.py#L127-L132)） | 任何写文件后重复 glob 输出顺序变化 → 永远"有新信息"（false negative） |
| 4 | `history_search` / `memory_search` / `websearch` 的输出**内嵌 query** | 换措辞就变字节 → 永远"有新信息"（false negative） |
| 5 | `grep` 把命中行裁到 300 字符（[grep_tool.py:36](file:///workspace/ai-coding/ai_coding/tools/grep_tool.py#L36)） | 第 300 列之后的内容变了也看不出来（false positive） |
| 6 | 两个空文件 `read` 输出都是 `""` | false positive |

**结论：从 `output` 文本推断"信息增益"在本项目里不可行。彻底放弃。**

### 2.1 🔴 评审 P0-1：v1 还有一个**自相矛盾**

v1 的 L2 文案让模型跑 `ruff check .`（走 `bash`），但 v1 把 `bash` 排除在检索工具外
→ `search_stale` **不会因此下降** → 窗口到期判 `failed` → 升级到"必须收尾"。
**模型完全照做却被惩罚。** v1 的 §7.2 那条"跑 ruff 后 stale=0"的轨迹在代码上不可能发生。

---

## 三、✅ v2 的信号设计：连续"只检索不动作"

### 3.1 定义（纯用工具名 + 成功位，**不解析任何输出**）

```
对每个成功的工具结果：
    若 call.name 属于 RETRIEVAL_TOOLS  → search_streak += 1，search_calls += 1
    否则                                → search_streak = 0
失败的结果                              → 两者都不变（失败归 STRATEGY 管）
```

- `search_streak`：**连续成功的检索调用数，中间没有一次成功的"其它"调用**
- `search_calls`：本回合成功的检索调用总数（只用于文案与事件）

### 3.2 实跑验证（5 种形状）

```
T2 形状（7 次检索，没动手）      : streak=7, calls=7   → 触发
正常修复回路（读→改→读→测试）    : streak=0, calls=3   → 不触发
模型照做跑 ruff 之后             : streak=1, calls=4   → 清零 ✅（解决 P0-1）
纯调研（读 8 个不同文件）         : streak=8, calls=8   → 触发（已知局限，见 3.4）
连续失败的检索                   : streak=0, calls=0   → 不触发（归 strategy）
```

### 3.3 为什么这个信号是对的（第一性原理）

T2 的病根**不是**"搜到了重复内容"，而是"**连续 63 次只看不动，从不验证**"。
"只看不动"是**精确可算**的（工具名 + 成功位）；"重复内容"是不可算的（§二）。
选可算的那个。

### 3.4 已知局限（必须写在代码注释里）

它**分不清**"读了 8 个不同文件"（合法调研）与"8 次 grep 同一个写错的符号"（空转）。
因此：

- L1/L2 的文案**必须对两种情形都成立**（不能只说"换关键词"）
- ~~**不做"强制收尾"那一级**~~ —— **I1/R-7 已加**：`search_streak_l3`（`LadderLimits`
  默认 0 保持既有单测契约）与归一化证据指纹（`stale_l3=6` 且 `repeated_nonempty>=2`）都会
  强制 `FINISH`（`reason="search unconverged"`），并用 `tools=[]` 向模型要一次收尾。
  原判断"分不清合法调研与空转"由证据指纹解决：不同文件是 novel，同批证据才是 stale；
  原始输出哈希仍按 §二 否决。
  **eval-08 修正**：计数档的默认值从 16 改回 0（关闭）。实测它截断的是"18 次调用、18 个新发现"
  的合法设计调研，而它针对的 61 次空转回合夹着 `bash`、streak 从没到 7 —— 见 `docs/features.md`。

---

## 四、原设计 vs 目前设计

干预阶梯的既有设计（**本方案不改框架，只加第 5 个 gate**）：

```
TurnBooks.note_outcomes(outcomes)     —— 所有工具结果的唯一入口
        ↓ 累积计数
TurnBooks.snapshot()                  —— 纯数据快照
        ↓
TurnPolicy.decide_next_iteration()    —— 预算闸（迭代/时钟/token/C3）在前
        ↓
interventions.select(snapshot, PRE)   —— gates = (STRATEGY, FATIGUE, SEARCH)
        ↓ _triggered → _due → _build
TurnDecision                          —— nudge（注入合成消息）/ finish
        ↓
interventions.record → ledger         —— 下一轮 settle 判 "helped"
```

| gate | 触发 | 阶段 |
|---|---|---|
| `STRATEGY` | 失败 streak ≥2 或 迭代>3 且零成功 | PRE |
| `FATIGUE` | `fatigue_index ≥ 30` | PRE |
| `TASK` | 有未完成 todo | POST |
| `ACCEPTANCE` | 有未通过验收 | POST |
| **`SEARCH`（新增）** | **`search_streak ≥ 4`** | **PRE** |

### 4.1 与既有两个 guard 的分工（评审 P3-1 校正了我 v1 的归因）

| 机制 | 抓什么 | 位置 |
|---|---|---|
| `FailedRepeatGuard`（`strict_repeats` 武装） | **失败**调用的重复 | [guard.py:296-322](file:///workspace/ai-coding/ai_coding/core/guard.py#L296-L322) |
| `DuplicateCallGuard` | **成功**调用的**同参数**重复（≤2 次） | [:200-272](file:///workspace/ai-coding/ai_coding/core/guard.py#L200-L272) |
| **`SEARCH` gate（新增）** | **不同**调用但**只读不动作**的连续段 | 本方案 |

三者正交，不重叠。

---

## 五、复用的方法契约（**一个都不新建**）

| 契约 | 位置 | 怎么用 |
|---|---|---|
| `interventions` 阶梯 6 个分派函数 | [interventions.py](file:///workspace/ai-coding/ai_coding/core/interventions.py) | **复用**：`SEARCH` 形状同 `STRATEGY` |
| `LadderLimits` | [:81-90](file:///workspace/ai-coding/ai_coding/core/interventions.py#L81-L90) | **复用**：加 1 个阈值字段 |
| `TurnBooks.note_outcomes` | [turn_books.py:331](file:///workspace/ai-coding/ai_coding/core/turn_books.py#L331) | **复用**：唯一入口，在这里数 streak |
| `TurnBooks.snapshot()` | [:391-428](file:///workspace/ai-coding/ai_coding/core/turn_books.py#L391-L428) | **复用**：带新字段进快照 |
| `LoopEvent` + `dataclasses.asdict` | [events.py:517-551](file:///workspace/ai-coding/ai_coding/core/events.py#L517-L551)、[trace.py:634](file:///workspace/ai-coding/ai_coding/core/trace.py#L634) | **复用**：新事件自动进 trace jsonl |
| `_note_intervention` | [agent_loop.py:1968](file:///workspace/ai-coding/ai_coding/core/agent_loop.py#L1968) | **复用**：事件在这里发（**仅 nudge 路径**，见 §六.5 的修正） |

**不新增**：函数/类/模块/配置项（除 1 个阈值）/前端改动/新测试文件。
**v2 还删掉了 v1 唯一的"新函数" `_note_information_gain`+`digest_of` 复用** —— 信号计算压缩成 `note_outcomes` 里的 5 行。

---

## 六、逐文件实现

### 6.1 `ai_coding/core/turn_policy.py` —— `TurnSnapshot` 加 2 个字段

```diff
     ask_user_calls: int = 0
+    #: B1/B2: consecutive successful *retrieval* calls with no successful
+    #: non-retrieval call in between.  This is the measured shape of the T2 turn
+    #: (63 successful looks, never an action) and the only part of "is this turn
+    #: learning anything" the harness can compute exactly -- see
+    #: docs/plan-m-b.md section 2 for why the output-text version was rejected.
+    search_streak: int = 0
+    #: Successful retrieval calls this turn, for the nudge text and the event.
+    search_calls: int = 0
```

### 6.2 `ai_coding/core/turn_books.py` —— 信号（**5 行，唯一新增业务逻辑**）

```diff
+#: B1/B2: tools whose contract is "look at existing state".  ``bash`` is
+#: deliberately absent: the static check itself runs through it, and a model that
+#: follows this gate's advice must be *rewarded* (its streak resets), not punished.
+#: Repeated identical calls are already covered by ``DuplicateCallGuard``, and
+#: repeated failing ones by ``FailedRepeatGuard`` -- this covers neither.
+RETRIEVAL_TOOLS = frozenset(
+    {"read", "grep", "glob", "history_search", "memory_search", "websearch", "webfetch"}
+)
```

```diff
     __all__ = ["STATE_ONLY_TOOLS", "Failure", "TurnBooks", "TurnFrame"]
+    __all__ = ["RETRIEVAL_TOOLS", "STATE_ONLY_TOOLS", "Failure", "TurnBooks", "TurnFrame"]
```

```diff
         self.ask_user_calls = 0
+        self.search_streak = 0
+        self.search_calls = 0
```

在 `note_outcomes` 里，紧跟 `self.attempted_calls += len(outcomes)` 之后：

```diff
         self.attempted_calls += len(outcomes)
         succeeded = [item for item in outcomes if item.result.success]
         self.successful_calls += len(succeeded)
         self.tool_success.update(item.call.name for item in succeeded)
+        # B1/B2: a successful look extends the streak; a successful *anything else*
+        # ends it.  Failures move neither number -- a failed call is STRATEGY's
+        # evidence, not evidence that the turn is looking without acting.
+        for item in succeeded:
+            if item.call.name in RETRIEVAL_TOOLS:
+                self.search_streak += 1
+                self.search_calls += 1
+            else:
+                self.search_streak = 0
```

```diff
             ask_user_calls=self.ask_user_calls,
+            search_streak=self.search_streak,
+            search_calls=self.search_calls,
             plan_submitted="present_plan" in self.tool_success,
```

### 6.3 `ai_coding/core/interventions.py` —— `SEARCH` gate（**6 个分派函数**）

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

`__all__` 加 `"SEARCH"`、`"SEARCH_GATE_LEAD"`（评审 P2-2）。

```diff
 class LadderLimits:
     acceptance_l1: int = 2
+    #: B1/B2: consecutive look-only calls before the ladder speaks.  4 is the
+    #: smallest number a normal turn does not reach: read a file, read another,
+    #: edit, test -- the streak is back to 0 by the third call.  0 disables.
+    search_streak_threshold: int = 4
```

**分派函数 1/6 —— `_triggered`**

```diff
     if gate == STRATEGY:
         return _strategy_triggered(snap, limits)
+    if gate == SEARCH:
+        return bool(
+            limits.search_streak_threshold
+            and snap.search_streak >= limits.search_streak_threshold
+        )
     if gate == FATIGUE:
```

**2/6 —— `_helped`**（评审 P1-2 修正：只看 streak 下降会误判"模型已经不检索了"）

```diff
     if gate == STRATEGY:
         ...
+    if gate == SEARCH:
+        # Helped has two honest shapes, and both are exactly computable: the run
+        # was broken (streak dropped) or the model stopped looking altogether
+        # (no new retrieval call since the nudge -- it is answering or acting).
+        return (
+            snap.search_streak < int(ref.get("streak", 0) or 0)
+            or snap.search_calls <= int(ref.get("calls", 0) or 0)
+        )
     if gate == FATIGUE:
```

**3/6 —— `_ref_for`**

```diff
     if gate == STRATEGY:
         return {"asks": int(snap.ask_user_calls)}
+    if gate == SEARCH:
+        return {
+            "asks": int(snap.ask_user_calls),
+            "streak": int(snap.search_streak),
+            "calls": int(snap.search_calls),
+        }
     if gate == FATIGUE:
```

**4/6 —— `_due`**（评审 P1-4 修正：**只有两级，没有 finish**）

```diff
     if outcome == "failed":
         if gate == STRATEGY:
             return (L4, "finish") if level >= L3 else (level + 1, "nudge")
+        if gate == SEARCH:
+            # L1 "change what you are doing" -> L2 "run the static check" -> keep
+            # repeating L2.  Deliberately *no* finish: this signal cannot tell a
+            # legitimate survey from a stall (section 3.4), and ending a survey
+            # costs more than one extra nudge.  The budget gates already end a
+            # turn that truly runs away.
+            return (L2, "nudge") if level >= L2 else (level + 1, "nudge")
         if gate == FATIGUE:
```

**5/6 —— `_build` + 文案**

```diff
     if gate == STRATEGY:
         reason = ...
         return InterventionDecision(gate, level, "nudge", reason, _strategy_text(level, snap))
+    if gate == SEARCH:
+        reason = (
+            "look-only streak"
+            if level == L1
+            else f"look-only streak (L{level})"
+        )
+        return InterventionDecision(gate, level, "nudge", reason, _search_text(level, snap))
```

```python
def _search_text(level: int, snap: TurnSnapshot) -> str:
    """The two rungs of the search ladder (B1 + B2).

    Both rungs must read correctly for *both* shapes the streak cannot tell
    apart -- a legitimate survey and a model guessing the same wrong symbol --
    because a nudge that only fits one of them is noise half the time.
    """
    streak = max(1, snap.search_streak)
    calls = max(streak, snap.search_calls)
    if level <= L1:
        return (
            f"{SEARCH_GATE_LEAD} 你已经连续 {streak} 次只读检索（本回合共 {calls} 次），"
            "其间没有做任何修改或验证。继续读下去，收益会越来越小："
            "① 如果你在找**某个符号/字符串**：换成符号名（函数名、类名、变量名），"
            "不要用自然语言描述；把范围缩小到具体目录，而不是整仓库；"
            "② 如果你在**了解一个模块**：读它的入口文件（`main` / `__init__` / 路由注册处），"
            "不要按关键词撒网；"
            "③ 如果你已经知道要改哪里：直接改，然后用测试或命令验证，不要再搜。"
        )
    return (
        f"{SEARCH_GATE_LEAD} 仍是只读检索（连续 {streak} 次，共 {calls} 次），没有推进。"
        "**不要再猜关键词了** —— 让静态检查替你回答："
        "`ruff check .`（未定义名字 F821、未用导入 F401、真 bug 都在这里）、"
        "`mypy <包名>`（模块不存在、类型不符）、"
        "`grep -rn '<符号>' --include='*.py' .`（谁在用它）。"
        "静态检查一次调用就能给出结论，比再读十个文件准。"
    )
```

**6/6 —— `select`**

```diff
     if phase == PRE:
-        gates: tuple[str, ...] = (STRATEGY, FATIGUE)
+        # B1/B2: SEARCH last, so a turn whose calls are also *failing* hears the
+        # strategy ladder first ("stop repeating the failing call").
+        gates: tuple[str, ...] = (STRATEGY, FATIGUE, SEARCH)
     else:
         gates = (ACCEPTANCE,) if snapshot.acceptance else (TASK,)
```

> **POST 不接 SEARCH**（评审 P2-5）：模型自己停下时由 `TASK`/`ACCEPTANCE`/收尾逻辑接管，
> 不需要再催。这条要写成注释，防止未来有人误接。

### 6.4 `ai_coding/core/events.py` —— 一个事件（B5）

```diff
+@dataclass(frozen=True)
+class SearchStalled:
+    """The search ladder fired (B2/B5): look-only calls are piling up.
+
+    The trace could not answer "how many looks did this turn spend before anyone
+    noticed it was not acting" -- the nudge is a synthetic message, and a message
+    says what was injected, never why.  One event per rung.
+    """
+
+    iteration: int
+    level: int
+    calls: int
+    streak: int
```

`__all__` 加 `"SearchStalled"`（评审 P2-1），`LoopEvent` 联合加 `| SearchStalled`。

> `kind` 字段**去掉**（v1 有）：SEARCH 只有 nudge，永远同一个值 —— 冗余字段，砍。

### 6.5 `ai_coding/core/agent_loop.py` —— 发事件（**两处修正**）

```diff
-from ai_coding.core.events import (
+from ai_coding.core.events import (
     ...
+    SearchStalled,
 )
```

```diff
-    @staticmethod
-    def _note_intervention(
-        session: SessionData, decision: TurnDecision, snapshot: TurnSnapshot
-    ) -> None:
+    def _note_intervention(
+        self, session: SessionData, decision: TurnDecision, snapshot: TurnSnapshot
+    ) -> None:
         """Persist one fired decision (搭 checkpoint/close 的车落盘)."""
         if not decision.intervention:
             return
+        # B5: only the search ladder emits.  The other four gates already leave
+        # their evidence in the counters, and emitting for all five would change
+        # every existing trace.  Only the *nudge* path reaches here: a ladder that
+        # chose FINISH goes through ``_finish_at``, which drops the intervention
+        # metadata -- a known defect (review P1-1, out of this plan's scope).
+        if decision.intervention == interventions.SEARCH:
+            self._emit(
+                SearchStalled(
+                    iteration=snapshot.iteration,
+                    level=decision.level,
+                    calls=snapshot.search_calls,
+                    streak=snapshot.search_streak,
+                )
+            )
         session.intervention_state = interventions.record(
```

> 评审 P2-1：不 import 会 `NameError`。
> 调用点（[agent_loop.py:827](file:///workspace/ai-coding/ai_coding/core/agent_loop.py#L827)、[:1051](file:///workspace/ai-coding/ai_coding/core/agent_loop.py#L1051)）已在实例上调用，签名不变。

### 6.6 测试（2 个既有文件，**不新增测试文件**）

`tests/test_interventions.py`：

| # | 用例 | 断言 |
|---|---|---|
| T1 | `search_streak=3`（低于阈值） | `select(PRE)` → `None` |
| T2 | `search_streak=4` | `("search", 1, "nudge")`，文案含 `SEARCH_GATE_LEAD` |
| T3 | L1 `failed` → **L2 nudge** | 文案含 `ruff check` + `mypy`（**B1 的核心断言**；评审 P1-4 已把"L3=finish"改正） |
| T4 | L2 `failed` → 仍是 **L2 nudge**（不 finish） | `kind == "nudge"`，且**永不** `"finish"` |
| T5 | helped：`ref={streak:4,calls:4}`，当前 `streak=1` | `settle` → `outcome == "helped"` |
| T6 | helped（评审 P1-2）：`ref={streak:4,calls:4}`，当前 `streak=4,calls=4` | `outcome == "helped"`（模型已停止检索） |
| T7 | 未 helped：`ref streaks 4/calls 4`，当前 `streak=5,calls=5` | 窗口关闭 → `failed` |
| T8 | 阈值 0 = 关闭 | `select` → `None` |
| T9 | **优先级**：strategy 与 search 同时满足 | 返回 `strategy`（锁死顺序） |
| T10 | **POST 不触发 SEARCH** | `select(POST, search_streak=99)` → `None` |
| T11 | 跨回合 `unfinished`：新回合 streak=0 | `select` → `None`（`_triggered` 先拦，评审 P1-3 已用实跑确认这条成立） |

`tests/test_turn_books.py`：

| # | 用例 | 断言 |
|---|---|---|
| T12 | read→read→read | `search_streak=3, search_calls=3` |
| T13 | read→edit→read | `search_streak=1`（**非检索调用清零**） |
| T14 | **bash 跑静态检查后清零**（P0-1 回归网） | `read,read,bash,read` → `streak=1` |
| T15 | 失败的检索不计数 | `grep(失败)×3` → `streak=0, calls=0` |
| T16 | 混合：成功后失败 | `read, grep(失败)` → `streak=1`（失败不改变） |

---

## 七、数据流

### 7.1 新信号在既有链路里的位置

```
 模型发起工具调用
        │
        ▼
 dispatch() → [DispatchOutcome(call, result), ...]
        │
        ├───────────────► TurnBooks.note_outcomes()
        │                   ├─ 既有: attempted / successful / failed_streak / fatigue
        │                   └─ 新增: search_streak / search_calls      ◄── 本方案
        │                            （只看工具名与 success，不解析输出）
        ▼
 下一轮 decide_next_iteration()
        ├─ 既有: 迭代 / 时钟 / token / C3 闸
        ▼
 interventions.select(snapshot, PRE)
        │   gates = (STRATEGY, FATIGUE, SEARCH)                        ◄── 本方案
        ▼
 _triggered(SEARCH)  ← search_streak >= 4 或 stale_streak >= 3           ◄── 本方案
        ▼
 _due(SEARCH, entry) → L1 nudge → L2 nudge → L3 finish                ◄── I1/R-7 补 L3
        ▼
 _build → 文案（L2 = 跑 ruff/mypy；L3 = search unconverged + note）    ◄── 本方案（B1）
        ▼
 TurnDecision(nudge)
        ├─► loop 注入合成 user 消息（既有机制）
        ├─► _note_intervention → emit SearchStalled                   ◄── 本方案（B5）
        └─► interventions.record → ledger
                 ▼
            下一轮 settle：streak 降 或 calls 未增 → "helped"
                           否则窗口关 → "failed" → 升到 L2
```

### 7.2 T2 场景的预期轨迹（**每个数字都对应 §3.2 的实跑**）

```
迭代 1  grep "httpx2"          streak=1
迭代 2  read mcp.py:780-800     streak=2
迭代 3  grep "import httpx"     streak=3
迭代 4  glob "**/httpx*"        streak=4
        ┌──────────────────────────────────────────────┐
        │ SEARCH L1：连续 4 次只读，换手段/动手            │  emit SearchStalled(level=1)
        └──────────────────────────────────────────────┘
迭代 5  grep "client.py"        streak=5   （窗口观察中）
迭代 6  read mcp.py:600-700     streak=6
        ┌──────────────────────────────────────────────┐
        │ SEARCH L2：**直接跑 ruff check / mypy**        │  emit SearchStalled(level=2)  ← B1
        └──────────────────────────────────────────────┘
迭代 7  bash "ruff check ."     streak=0   ← 非检索调用清零（P0-1 已修）
迭代 8  收尾：mcp.py:787 的 httpx2 结论
```

**roadmap 要的"≤5 次工具调用"** 的现实版本是：**第 4 次只读就开始提示，第 6 次给出静态检查这条明路**。
不是砍预算，而是**在模型开始空转时就把最省的那条路指给它**。

---

## 八、修改成功后的效果

| 观测点 | 改前 | 改后 |
|---|---|---|
| 工具**成功**但只读不动作 | **无提示** | `SEARCH` L1 → L2（I1/R-7 再加 L3 强制收尾） |
| 同一批证据反复出现 | **无提示** | `stale_streak` 3/4/6 → L1/L2/**L3 finish**（归一化证据指纹） |
| 静态检查的提示 | prompt/gate 里**零命中** | L2 明确给出 `ruff check .` / `mypy` / `grep -rn` |
| 收敛可观测 | trace 读不到 | `SearchStalled` 事件（每级一条） |
| 正常修复回合（读→改→测） | — | **streak 恒为 0，gate 永不触发**；请求与 trace **逐字节不变** |
| 既有 4 个 gate | — | **不变**（`search_streak` 默认 0） |
| 模型照 L2 建议跑静态检查 | — | **streak 清零，被判 helped**（P0-1 已修） |

### 8.1 按此实施后的最终模样

- 普通回合：`search_streak` 在每次 edit/bash 后归零 → 与今天完全一致
- 长时只读回合：第 4 次出现 `[search-gate] 换手段`；第 6 次出现"跑 ruff/mypy"
- trace jsonl 出现 `{"name": "SearchStalled", "payload": {"iteration": 4, "level": 1, "calls": 4, "streak": 4, ...}}`
- `ruff`/`mypy` 全绿；全量 pytest 0 failed，skipped 仍 6
- `features.md` 的干预阶梯表从 4 个 gate 变 5 个

---

## 九、拍板结论（已按你的选择执行）

| # | 问题 | 你的决定 | 影响 |
|---|---|---|---|
| 9.1 | B1 注入强度 | ✅ **harness 主动跑并注入**（不是推荐项） | 新增 §十三：一个模块 + 一组配置 + 接线 |
| 9.2 | B2 收尾级 | ✅ **不做强制收尾**（L1/L2 两级） | §六.3 的 `_due` 已是此形状 |
| 9.3 | 429 `insufficient_quota` | ✅ **算 NO_RETRY，且首次即切备用 provider**（I1/R-1 改为 `QUOTA`） | 新增 §十三.5；`WORTH_ANOTHER_MODEL` + 首次即切分支 |

### 9.1 为什么不推荐 harness 主动跑（我原先的判断），以及为什么仍然做

原判断：项目分层是"模型跑工具、harness 只提示"，harness 跑命令需要新的子进程机制。
**实际调研后，机制全都存在**，成本远低于我原估的 +150 行：

| 需要的能力 | 既有实现 | 复用方式 |
|---|---|---|
| 受控子进程（超时 + 进程组 killpg） | [infra/subprocess_runner.py:248](file:///workspace/ai-coding/ai_coding/infra/subprocess_runner.py#L248) `run_process_async`（含 `env` 与 `max_output_chars`） | **直接复用** |
| "在 workspace 里跑一条 shell 命令并拿输出"的样板 | [core/acceptance.py:179-253](file:///workspace/ai-coding/ai_coding/core/acceptance.py#L179-L253) `verify_items` | **照抄形状** |
| 输出截断 | 同上 + `max_output_chars` | **复用** |

真正的增量只有"**决定跑哪条命令**"与"**把结果拼成一段话**"。

### 9.2 🔴 调研否决了"自动推断检查命令"（记住这个数字）

我原本打算自动探测并跑 `mypy .`。实测：

```
$ .venv/bin/ruff check .   → All checks passed!                       0.035s
$ .venv/bin/mypy .         → Found 271 errors in 31 files (188 files)  4.8s
$ .venv/bin/mypy ai_coding → Success: no issues found in 152 files     ~2s
```

`mypy .` 把 `scripts/` 也算进来（`pyproject.toml` 只 `exclude = ["tests/", ".venv/"]`），
**注入 271 条错误是纯噪声**；而 CI 跑的是 `mypy ai_coding`（0 errors）。

**结论：检查命令不可能被自动推断，必须由配置声明。** 默认只给最便宜且零噪声的
`ruff check .`；`mypy` 由使用方声明（本项目应写 `mypy ai_coding`）。

---

## 十三、B1 主动跑静态检查（按拍板实现）

### 13.1 分工：为什么不在 `interventions.py` 里跑

`interventions` 是**纯状态机**（无 IO、表驱动测试）。在那里跑子进程会破坏它的契约。

```
interventions（纯）     决定"什么时候该看静态检查" —— L2
        ↓
agent_loop（有 IO）    拿到 L2 决策后真去跑命令，把输出拼进注入文本
        ↓
static_check（新模块） 唯一职责：在 workspace 里跑配置的命令，返回一段话
```

### 13.2 新模块 `ai_coding/core/static_check.py`（约 70 行）

```python
"""Run the project's own static checks when a turn stalls in look-only mode (B1).

``core/interventions.py`` decides *when* to ask; this module answers with actual
command output, so the model gets the finding instead of another keyword to guess.

Small on purpose: one bounded command per configured check, a size cap, and no
new failure modes -- a check that cannot run is reported as such, never raised.
"""

__all__ = ["run_static_checks"]


async def run_static_checks(
    workspace: str,
    commands: Sequence[str],
    *,
    timeout: float = 20.0,
    max_chars: int = 2000,
) -> str: ...
```

| 约束 | 做法 |
|---|---|
| ruff 通常不在 PATH | `env["PATH"] = f"{workspace}/.venv/bin{os.pathsep}{PATH}"` |
| 一条命令挂了不能拖垮提示 | 每条独立 `try/except`，失败写 `could not run: …`，继续下一条 |
| 输出可能上万行 | `run_process_async(max_output_chars=max_chars)` + 总量 `<= max_chars` |
| 可能挂死 | `timeout`（默认 20s） |
| 不能混淆"谁说的" | 块首写明 `[static-check] harness 自动执行` |

输出形状（喂给模型的样子）：

```
[static-check] harness 自动执行了 2 条检查（工作区: /path/to/repo）
$ ruff check .  → exit 0, 0.03s
All checks passed!
$ mypy ai_coding  → exit 1, 2.1s
ai_coding/core/mcp.py:787: error: Cannot find implementation or library stub for module named "httpx2"
```

> 最后一行就是 T2 的答案 —— 模型不必再 grep 任何东西。

### 13.3 配置（`AIConfig.static_check`）

```diff
+class StaticCheckConfig(BaseModel):
+    """The commands the harness runs itself when the search ladder reaches L2 (B1).
+
+    Commands are *declared*, never inferred: measured on this repository,
+    ``mypy .`` reports 271 errors (it does not exclude ``scripts/``) while
+    ``mypy ai_coding`` -- what CI runs -- reports none.  No heuristic can tell
+    those apart.
+    """
+
+    model_config = ConfigDict(extra="ignore")
+
+    #: Shell commands, run in the workspace.  Empty = feature off.
+    commands: list[str] = Field(default_factory=lambda: ["ruff check ."])
+    timeout_seconds: float = 20.0
+    #: Cap on what the whole block may add to the prompt.
+    max_chars: int = 2000
```

`AIConfig` 加 `static_check: StaticCheckConfig = Field(default_factory=StaticCheckConfig)`；
`config.example.yaml` 的 `ai:` 段加同名键（注释里写明本项目应写 `mypy ai_coding`）。

### 13.4 接线（`agent_loop.py`，PRE nudge 分支）

```diff
                     elif decision.intervention == "strategy":
                         books.note_strategy_nudge()
+                    elif decision.intervention == interventions.SEARCH:
+                        # B1: L2 is the rung that asks *for* the static check, so
+                        # run it here and hand over the finding.  Once per turn:
+                        # a re-fired L2 must not start mypy again.
+                        if decision.level >= interventions.L2 and not books.static_check_ran:
+                            books.static_check_ran = True
+                            block = await run_static_checks(
+                                str(self.workspace or ""),
+                                list(self.settings.static_check.commands),
+                                timeout=self.settings.static_check.timeout_seconds,
+                                max_chars=self.settings.static_check.max_chars,
+                            )
+                            if block:
+                                decision = replace(
+                                    decision,
+                                    inject=(
+                                        f"{decision.inject}\n\n{block}"
+                                        if decision.inject
+                                        else block
+                                    ),
+                                )
```

`TurnBooks` 加 `static_check_ran: bool = False`（每回合新建，天然按回合重置）。

> **注意**：注入文本字段是 `TurnDecision.inject`（[agent_loop.py:897](file:///workspace/ai-coding/ai_coding/core/agent_loop.py#L897) 用它建合成消息），
> 不是 `InterventionDecision.text`。

### 13.5 `insufficient_quota` 归 NO_RETRY，并首次即切备用 provider（§9.3 拍板 + I1/R-1）

```diff
         if status == 429:
-            return FailureKind.RATE_LIMITED
+            # A quota that is used up does not refill in `Retry-After` seconds:
+            # waiting four times over burns four round trips to learn nothing.
+            # Only when the provider *says* it is the quota -- a plain 429 is a
+            # rate limit, and waiting is exactly right for it.
+            if _mentions_quota(exc):
+                return FailureKind.QUOTA
+            return FailureKind.RATE_LIMITED
```

`QUOTA` 同时加入 `WORTH_ANOTHER_MODEL`，并在 `step()` 的 `AUTH_ERROR` 分支旁加一个
**首次即 `raise FallbackWorthwhile`** 的 `QUOTA` 分支。**三处缺一不可**：只加集合会让
它走进通用分支先重试 `max_retries` 次，复现当初修掉的 4 次空转；只改分类（FATAL）则
额度耗尽时不会切备用 provider。

测试：429 + `insufficient_quota` → `classify_failure` = `QUOTA`；`max_retries=3` 下
`calls == 1` 且抛 `FallbackWorthwhile`（首次即切）。429 普通限速仍是 `RATE_LIMITED`
（先重试，不切）；401 行为不变（首次即切）。

### 13.6 B1 的测试（**需新增 1 个测试文件 —— 新模块必须有自己的测试**）

`tests/test_static_check.py`：

| # | 用例 | 断言 |
|---|---|---|
| S1 | `commands=["echo hi"]` | 块含 `$ echo hi`、`exit 0`、`hi` |
| S2 | `commands=[]` | 返回 `""` |
| S3 | 不存在的命令 | 块含 `could not run`，**不抛异常** |
| S4 | `sleep 5` + `timeout=0.2` | 含 `timeout`，**耗时 < 2s** |
| S5 | 输出 5000 字符 + `max_chars=200` | 长度 ≤ 200 + 固定头部 |
| S6 | `.venv/bin` 里的可执行文件能被找到 | 临时 venv 桩验证 PATH 前置 |

---

## 十四、自我核验（追加 B1 部分）

10. **harness 跑命令安全吗？** —— 命令来自**操作者配置**（不是模型产出），
    与 `plan_runner` 的 verify 命令不同，**不需要**权限门；仍受 `cwd`、超时、输出上限三重约束。
11. **会不会拖慢正常会话？** —— 只在 `SEARCH` **L2** 且**每回合一次**时跑；
    正常回合 `search_streak` 到不了 4，一次都不跑。
12. **ruff 不在 PATH？** —— `.venv/bin` 前置；仍找不到则该条写 `could not run`，
    其余继续，nudge 照常发出（降级不失败）。
13. **注入会不会撑爆 prompt？** —— 总块 ≤ `max_chars`（默认 2000）。

---

## 十、自我核验

1. **信号可算吗？** ✅ 只用工具名 + `result.success`，不碰输出文本。§3.2 五种形状实跑验证。
2. **模型照做会被惩罚吗？** ✅ 不会 —— `bash` 不在 `RETRIEVAL_TOOLS`，跑静态检查即清零（P0-1 已修）。
3. **会误伤正常回合吗？** ✅ 不会 —— 读→改→测的循环每次 edit/bash 都清零；阈值 4 且可关。
   仍会的：纯长调研（8 次连续 read）会收到 L1/L2 —— 已知局限，文案已写成对两种情形都成立。
4. **会重复造轮子吗？** ✅ 三个候选都被否掉：B3 的 `RetryStrategy`（已有 `FailureKind`）、
   输出指纹（v1，已否决）、重复调用检测（已有两个 guard）。
5. **既有优先级会变吗？** ✅ 不会 —— `select(PRE)` 末尾追加；`_triggered` 无副作用；T9 锁死。
6. **既有测试会破吗？** ✅ 不会 —— `TurnSnapshot` 新字段有默认值；`books.snapshot()` 的既有断言
   用关键字参数比对，需确认（实施时先跑 `tests/test_turn_books.py`）。
7. **事件会自动落盘吗？** ✅ 是（[trace.py:634](file:///workspace/ai-coding/ai_coding/core/trace.py#L634) 的 `asdict`）；但要补 `__all__` 与 import（P2-1）。
   ⚠️ 前端 `/trace` 不展示它（评审 P3-2）—— B5 的验收判据只到"jsonl 能读到"。
8. **跨回合账本会越级吗？** ✅ 不会 —— 新回合 `search_streak=0`，`_triggered` 先拦，`_due` 根本不执行（T11）。
9. **`armed()` 要改吗？** ✅ 不用 —— 它只武装 `FailedRepeatGuard`，只认 STRATEGY/FATIGUE
   （[interventions.py:290-297](file:///workspace/ai-coding/ai_coding/core/interventions.py#L290-L297)）。要写成注释。

---

## 十一、边界声明

| 项 | 归属 |
|---|---|
| **本方案改** | 6 个源文件（含 1 个新模块 `core/static_check.py`）+ `config/models.py` + `config.example.yaml` + 3 个测试文件 + 3 份文档 |
| **明确不改** | `guard.py`、`turn_policy.py` 的**决策分支**（只加字段）、`frontend/`、`plan_runner.py` |
| **明确不做** | B4 意图分类、B2 强制收尾、基于输出文本的任何推断（§二） |
| **记录不修（评审发现）** | **P1-1**：`_finish_at` 丢掉 intervention 元数据 → 任何走 FINISH 的 ladder 不发事件、账本不记 `exhausted`（Strategy L4 现已受影响） |
| **需校正** | roadmap M-B3（已完成）、M-B4（大部分已完成） |

## 十二、方案评审记录（子智能体，`git worktree` 独立实跑）

评审在 `/tmp/mb-review`（detached HEAD `5378131`）**不复用方案任何结论**，自写脚本重跑；
基线 120 passed；评审后副本已删除，主仓库干净。

### 12.1 评审发现（v2 已全部处理）

| # | 问题 | 处理 |
|---|---|---|
| 🔴 **P0-1** | v1 的 L2 让模型跑 `ruff`（走 bash），但 bash 被排除在检索工具外 → stale 不降 → **模型照做却被升级惩罚**；v1 §7.2 的成功轨迹在代码上不可能 | ✅ v2 换信号为 `search_streak`：**bash 清零**（§3.2 实跑：`read,read,bash,read` → `streak=1`） |
| 🔴 **P0-2** | v1 的输出指纹双向都错：grep/glob 的 `(no matches)` 逐字节相同（跨工具碰撞）、glob 按 mtime 排序、history/memory/websearch 内嵌 query、grep 裁到 300 字符、空文件碰撞；**且 `DuplicateCallGuard` 拒绝第 3 次相同调用 → stale 永远到不了 3** | ✅ v2 **完全不解析输出** |
| 🟠 **P1-1** | `_finish_at` 丢弃 intervention → 走 FINISH 的 ladder 不发事件、账本不记 `exhausted`；我"唯一落点"的说法是错的 | ✅ v2 不再有 finish 级，并在 §十一 记录该缺陷（不在范围） |
| 🟠 **P1-2** | `_helped` 只看 streak 下降 → 模型停止检索被判 failed | ✅ v2 `_helped` 加 `calls` 未增分支（T6） |
| 🟠 **P1-3** | 跨回合 `unfinished` 会跳级 | ✅ v2 用实跑确认 `_triggered` 先拦（T11） |
| 🟠 **P1-4** | v1 的 `_due` 让 L2→L3 变 nudge，与 T4 断言矛盾 | ✅ v2 `_due` 只有 L1/L2 两级，无 finish |
| 🟡 **P2-1..6** | 漏 `events.__all__` + import；漏 `interventions.__all__`；"5 个分派函数"实为 6；`armed` 不需改（但要写注释）；POST 不接 SEARCH 要声明；B4 描述夸大 | ✅ 全部已改 |
| 🟡 **P2-7** | 429 `insufficient_quota` 被当 RATE_LIMITED 重试满 4 次 | ✅ §九 待拍板 3；I1/R-1 改为 `QUOTA`（不重试 + 首次即切） |
| 🟡 **P3-1..3** | D 的归因错（成功重复是 `DuplicateCallGuard`）；前端不展示新事件；"30/4 是评估 config"无法证实 | ✅ 已校正 |


---

## 十五、代码评审后的修复（第二版，`ef1c036` 之后）

子智能体对实现做了独立评审（worktree + 自写探针 + 真 trace），抓到 **1 个 P0 + 3 个 P1**。
全部已修，逐条记录如下 —— 这是本轮最有价值的部分。

### 15.1 🔴 P0：仓库里被跟踪的 `config.example.yaml` 就能设置要执行的命令

我原以为"命令来自操作者配置，不是模型产出，不需要权限门"（§十四.10）。**这是事实错误**：

- `ConfigLoader.DEFAULT_NAMES` 会从 workspace **向上查找** `config.local.yaml` /
  `config.yaml` / `config.example.yaml`，而 `config.example.yaml` 是**仓库里被跟踪的文件**。
- 实测：在一个只有 `config.example.yaml` 的目录里 `ConfigLoader.load_app_from('.')`
  → `commands = ['echo PWNED-FROM-REPO-EXAMPLE']`。

攻击链：克隆不受信任的仓库 → 在该仓库里启动 agent → 正常读 6 个文件 → L2 自动执行仓库里的命令。
而 `core/acceptance.py` 的 verify 命令（**模型产出**）反而有 `bash_denied` + 工作区外检查 +
approval —— 我把方向和安全性都搞反了。

**修法（结构性，不是补检查）**：`shlex.split` + **`shell=False`** + 首词白名单
（`ALLOWED_PROGRAMS`，比对 basename 所以 `.venv/bin/ruff` 仍可用）。于是：

```
$ ruff check .  → exit 0            （照常工作）
$ echo evil ; touch /tmp/PWNED  → skipped: 不在允许的静态检查工具里   （PWNED 未生成）
```

`;`/`&&`/`|` 只是普通参数，且任意二进制名不能被执行。**残留风险**（已写进 docstring）：
仓库可以在自己的 `.venv/bin` 里放一个叫 `ruff` 的恶意程序 —— 这是"运行项目自带的 linter"
这个功能的固有代价，与"模型用 bash 跑 `make`"同级，但已从"任意命令"收窄到"必须叫静态检查工具名"。

### 15.2 🟠 P1：`insufficient_quota` 修复对真实 SDK 无效

我只看 `str(exc)`。实测真实 `openai.RateLimitError`：

```
str(exc)          = "You exceeded your current quota, please check your plan and billing details."
marker in str     : False          ← 我的判断失效
marker in body    : True           ← 真正的位置
```

测试之所以绿，是因为我把 marker 写进了 message。**修法**：新增 `_mentions_quota()`
同时读 `exc.body`，并加一条用 body-only 的 429 的测试。

### 15.3 🟠 P1：默认预算下 L2 的输出没人读

默认 `max_tool_iterations=10`，一次 read/step 时 L2（原设计 streak 8）落在第 10 个迭代 ——
**静态检查跑完、注入完，回合就结束了**。B1 的全部意义（把结论递到模型面前）落空。

**修法**：L2 阈值与 L1 解耦（`search_streak_l2: int = 6`，原先是 `2×L1=8`）。实测修后：

```
SearchStalled it=8 lvl=2 calls=6 streak=6
IterationStarted 9        ← L2 之后还有两步可以读它
IterationStarted 10
```

并新增用例 `test_the_static_check_output_is_readable_under_the_default_budget`
（跑默认预算，断言 L2 事件之后仍 `IterationStarted`）。原有用例只断言提示条数，漏掉了这条。

### 15.4 🟠 P1：`todo_write` 能把空转藏起来

原规则是"任何成功的非检索调用清零"，于是 `todo_write`/`checkpoint_write`/goal 工具也能清零 ——
模型每读 3 次写一次 todo，gate 永远不触发。**修法**：改成**动作白名单**
`ACTION_TOOLS = {write, edit, bash}`；其余既不计数也不清零。顺带把 `job_output`/
`job_list`/`subagent_list` 补进检索集（它们是"看状态"）。

### 15.5 🟡 P2/P3（都已处理）

| # | 问题 | 修法 |
|---|---|---|
| P2-5 | `_helped` 的 SEARCH 分支是死代码（决策不读 outcome） | 删分支，改成一行注释 + 一条用例锁住"outcome 只是审计" |
| P2-8 | `search_streak_threshold` 接不到 YAML，"0 可关"只是测试能力 | 接到 `AIConfig` → `TurnPolicyConfig` → `LadderLimits`（本功能会自己跑子进程，操作者必须能关） |
| P2-10 | plan / 只读姿态被误伤（它们本来就该只看不动） | `_triggered` 对 `mode == "plan"` 或 `readonly` 直接返回 False |
| P3-12 | `max_chars` 小于前导行时静默返回 `""` | 头部按 cap 截断，且"skipped"也算说过话 |
| P3-13 | 异常分支可能把块丢掉 | 异常照样写头部（`could not run: …`），`spoke` 计数覆盖拒绝与异常 |
| P3-14 | 文档 S3 写"含 could not run"，实际是 exit 127 | 现按 `shell=False` 后未安装的工具会走 `outcome.error` → 文案确实是 `could not run`；用例改为断言它 |
| P3-6/7 | 文档说 L1 在第 4 次、L2 在第 6 次，trace 形如 `{"name":..., "payload":...}` | 已改为实测：L2 在第 8 个**迭代**；jsonl 是 `{"evt": "SearchStalled", "data": {...}}` |

### 15.6 修后判据（全部实跑）

| 判据 | 结果 |
|---|---|
| `tests/test_static_check.py`（含 2 条安全用例） | **10 passed** |
| `tests/test_interventions.py` + `test_interventions_loop.py` | 全绿（含 2 条全链路） |
| 全量 `pytest` | **3840 passed / 6 skipped / 0 failed** |
| `ruff` / `mypy ai_coding` | **0 / 0**（153 文件） |
| 真仓库跑默认命令 | `ruff check .` → exit 0；`mypy ai_coding` → exit 0 |
| 恶意命令 | `echo evil ; touch /tmp/PWNED` → **skipped，文件未生成** |

### 15.7 第二轮评审（对修复本身的评审）：P0 其实没修掉

上一节的修法把"任意命令"收窄成"允许名 + 非 shell"，但第二轮评审用探针证明**它仍不是安全边界**：

| 绕过 | 实测 |
|---|---|
| 仓库放 `.venv/bin/ruff`（一个 `touch` 脚本） | `ruff check .` **执行了它**，marker 生成 |
| `./ruff check .`、`/tmp/evil/ruff check .` | 同样执行（basename 比对只认名字） |
| `cargo check`（在旧白名单里） | 执行了仓库的 `build.rs` |
| `go run .`、`eslint .`（旧白名单） | 执行仓库代码 / 加载配置模块 |

关键结论：**白名单只约束"叫什么名字"，不约束"跑谁"**。而 `commands` 仍可由仓库的
`config.example.yaml` 设置 —— 所以原攻击链依然成立，我只是把 `echo` 换成了 `cargo`。

**真正的修法：把"是否允许执行"放到仓库碰不到的信道**。`AI_CODING_STATIC_CHECK=1`
（环境变量）现在是唯一开关，默认关闭；实测未开启时 `run_static_checks` 返回 `""`，
仓库里的 `.venv/bin/ruff` **一次都没有被执行**。开启等于操作者对该仓库表态。

同时把"本身就执行仓库代码"的工具移出白名单（`cargo`/`go`/`eslint`/`pylint`/`rubocop`/`phpstan`）。

> **既有同源风险（不在本里程碑范围，需单独立项）**：`core/hooks.py:283` 也直接执行
> **配置来源**的 `command`，且同样没有权限门；项目配置还能设 `mcp.servers[].command`。
> 这说明缺的是**「项目配置是否可信」这一层机制**（`--trust-project-config` 之类），
> 不是某一个字段的补丁。已记入 roadmap 待立项。

### 15.8 第二轮评审发现的 3 个新 bug（已修）

| # | 问题 | 实测 | 修法 |
|---|---|---|---|
| N-1 | 阈值 ≥6 时 **L1 被吞掉**（`max(l2, threshold)` 让 L1/L2 同阈值） | `threshold=8` → 只出现 `(9, 2)` | 改成 `max(l2, threshold + 1)`；实测 `threshold=8` → `(8,1)`、`(9,2)` |
| N-2 | **负数阈值**每轮都 nudge，一个模型步都没有（直接烧完预算） | `threshold=-1` → 10 次 nudge、`engine_calls=1` | `AIConfig` 加 `ge=0`（实测 `ValidationError`） |
| N-3 | L2 与**小预算**耦合：`max_tool_iterations=8` 时 L2 仍落在最后一步；`=6` 时 L2 根本不触发 | 8 → L2 在最后；6 → 只有 L1 | 运行静态检查前加 `index < max_iterations`（文案照发、只跳过执行）；预算依赖写进 features 与配置注释 |

另外：`mcp_call`/`subAgent` 补进 `ACTION_TOOLS`（两者都可能改变世界，原先既不计检索也不清零，
会让 streak 在真实工作之后继续上涨）。

### 15.9 §15.6 的事实订正

| 项 | §15.6 写的 | 实际 |
|---|---|---|
| `tests/test_static_check.py` | 10 passed | 现 **11 passed**（含 env 开关与"执行型工具被拒"两例） |
| 全量 | 3840 passed | 修 P0/N-1..N-3 后 **3844** |
| "任意二进制名不能被执行" | 表述过强 | 正确表述：名字必须在白名单内；**跑的是哪个文件不受约束**（这就是为什么开关必须放在 env） |
| "与模型用 bash 跑 make 同级" | 错 | bash 走 dispatch + 权限门 + approval；本功能原先三者皆无 → 现由 env 开关弥补 |

### 15.10 两轮修完后的判据（实跑）

| 判据 | 结果 |
|---|---|
| 全量 `pytest` | **3844 passed / 6 skipped / 0 failed** |
| `ruff` / `mypy ai_coding` | 0 / 0（153 文件） |
| `tests/test_static_check.py` | 11 passed |
| `test_interventions.py` + `test_interventions_loop.py` | 39 passed |
| 未 opt-in 时执行 | `run_static_checks` → `""`，仓库二进制**未执行** |
| opt-in 后恶意命令 | `;`/`&&`/`$()`/`|` 均只作参数；`cargo`/`go`/`eslint` 被 skipped |
| 真仓库默认命令（opt-in） | `ruff check .` exit 0、`mypy ai_coding` exit 0 |
