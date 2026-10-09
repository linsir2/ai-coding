# F2: Fatigue System — Paris' Law Error Accumulation

> **目标**：用帕里斯定律（Paris' Law）建模亚阈值错误的累积效应，在轨迹崩溃前触发优雅退出
> **原则**：最小化改动、只改根源、复用现有升级阶梯

---

## 1. 问题分析

### 当前系统的缺陷

`turn_policy.py` 的升级阶梯（escalation ladder）：

```
stall (无成功调用) → FINISH
failed_streak (连续失败) → NUDGE (策略变更)
否则 → ITERATE
```

**问题**：
1. `failed_streak` 只看连续失败——一次成功就归零，但之前的错误影响被遗忘
2. 严重错误（timeout、context overflow）和轻微错误（file not found）被同等对待
3. 错误不随时间衰减——10 轮前的错误和当前错误权重相同
4. 没有"优雅退出"机制——要么硬停（FINISH），要么继续（ITERATE）

### 根因

系统缺少**错误累积模型**。单个亚阈值错误无害，但多个错误的非线性累积会导致轨迹崩溃。

---

## 2. 设计方案

### 帕里斯定律错误累积器

**物理背景**：帕里斯定律描述金属疲劳——`da/dN = C × ΔK^m`。每次应力循环造成的损伤与应力幅值的 m 次方成正比。

**映射到 Agent**：
- 应力循环 = 每次工具调用失败
- 应力幅值 = 错误严重程度
- 损伤累积 = fatigue_index
- 断裂 = 触发优雅退出

**公式**：
```
fatigue_index = Σ C × severity(error_i)² × decay(turns_ago_i)
```

- `C = 1.0`（常数）
- `severity ∈ {1, 2, 3, 5}`（由错误内容决定）
- `decay(t) = exp(-0.3 × t)`（指数衰减）
- `m = 2`（非线性累积）

**严重程度分级**：

| severity | 关键词 | 示例 |
|----------|--------|------|
| 5 | timeout, context overflow, out of memory | 严重：浪费大量 token |
| 4 | rate limit, too many requests | 严重：需要等待 |
| 3 | permission denied, access forbidden | 中等：需要换方法 |
| 2 | not found, invalid, malformed, error | 低：常见工具错误 |
| 1 | （默认） | 轻微：其他错误 |

**阈值与响应**：

| fatigue_index | 响应 |
|---------------|------|
| < 5.0 | 正常（ITERATE） |
| ≥ 5.0 | 疲劳门（NUDGE）：强制策略变更 |
| ≥ 5.0 且 nudge 已用 | 断裂（FINISH）：优雅退出 |

### 在升级阶梯中的位置

```
1. budget_expired → FINISH
2. iteration > max → C3 或 FINISH
3. token_gate → FINISH
4. stall (无成功调用) → FINISH        ← 最严重：完全无进展
5. fatigue ≥ threshold → NUDGE         ← 新增：累积错误过高
6. failed_streak ≥ threshold → NUDGE   ← 已有：连续失败
7. 否则 → ITERATE
```

fatigue 在 stall 之后、failed_streak 之前。理由：
- stall = 完全无进展（最严重）
- fatigue = 有进展但累积错误过多（严重）
- failed_streak = 连续失败（中等，已有策略变更机制）

---

## 3. 影响范围分析

### 3.1 需要修改的文件

| 文件 | 改动类型 | 改动量 |
|------|---------|--------|
| `ai_coding/core/turn_books.py` | 新增 fatigue 字段和计算 | ~30 行 |
| `ai_coding/core/turn_policy.py` | 新增疲劳门决策 | ~15 行 |

### 3.2 不需要修改的文件

| 文件 | 原因 |
|------|------|
| `agent_loop.py` | 决策在 turn_policy 中，loop 只是调用 |
| `request_assembly.py` | nudge 注入机制已存在 |
| `session.py` | fatigue 是 per-turn 的，不需要 session 存储 |
| `dispatch.py` | 错误记录已存在 |

### 3.3 可复用的契约

| 现有契约 | 如何复用 |
|---------|---------|
| `TurnBooks.failures: list[Failure]` | 已有失败记录，从中计算 fatigue |
| `TurnSnapshot` | 新增 fatigue_index 字段 |
| `TurnDecision.NUDGE` | 已有 nudge 机制，fatigue 门使用相同机制 |
| `note_outcomes()` | 在记录失败时同步计算 fatigue |

---

## 4. 逐文件实现细节

### 4.1 `ai_coding/core/turn_books.py`

#### 4.1.1 新增常量

```python
# F2: Paris' Law fatigue tracking
_FATIGUE_C = 1.0
_FATIGUE_M = 2.0
_FATIGUE_THRESHOLD = 5.0
_FATIGUE_DECAY_LAMBDA = 0.3

_SEVERITY_KEYWORDS: dict[int, tuple[str, ...]] = {
    5: ("timeout", "context overflow", "out of memory", "token limit"),
    4: ("rate limit", "too many requests", "quota exceeded"),
    3: ("permission denied", "access forbidden", "unauthorized"),
    2: ("not found", "invalid", "malformed", "bad request", "error", "failed"),
}
```

#### 4.1.2 修改 TurnBooks

```python
# 新增字段
fatigue_index: float = 0.0
fatigue_nudges: int = 0  # fatigue nudge 已使用次数
```

#### 4.1.3 新增方法

```python
@staticmethod
def _severity_of(error: str) -> int:
    """Derive error severity from error text.
    
    Returns 1-5, where 5 is most severe (timeout, context overflow).
    """
    error_lower = error.lower()
    for level in (5, 4, 3, 2):
        if any(kw in error_lower for kw in _SEVERITY_KEYWORDS.get(level, ())):
            return level
    return 1

@staticmethod
def _decay(turns_ago: int) -> float:
    """Exponential decay: recent errors count more."""
    return math.exp(-_FATIGUE_DECAY_LAMBDA * turns_ago)

def _compute_fatigue(self) -> float:
    """Compute fatigue index using Paris' Law: D = Σ C × s^m × decay(t).
    
    Uses failure_turns to determine how long ago each error occurred.
    """
    fatigue = 0.0
    current_turn = self._current_turn  # need to track this
    for failure, turn in zip(self.failures, self.failure_turns):
        severity = self._severity_of(failure[1])  # error string
        turns_ago = max(0, current_turn - turn)
        decay = self._decay(turns_ago)
        fatigue += _FATIGUE_C * (severity ** _FATIGUE_M) * decay
    self.fatigue_index = fatigue
    return fatigue
```

Wait, I need to track WHEN each failure occurred. Currently `Failure = tuple[str, str, str]` doesn't include turn info. Let me reconsider...

Actually, since `TurnBooks` is per-turn, all failures in `self.failures` are from the current turn. Within a turn, iterations happen sequentially. So I can use a simpler approach:

- Track failures with their iteration number
- Decay is based on iterations within the turn, not turns

But that complicates the Failure type. Let me think of a simpler approach...

**Simpler approach**: Since `TurnBooks` is per-turn and all failures are "recent" (within the same turn), I can skip time decay for the first version. The Paris' Law severity weighting is the key innovation.

```python
def _compute_fatigue(self) -> float:
    """Compute fatigue index: D = Σ C × s^m (Paris' Law without decay)."""
    fatigue = 0.0
    for failure in self.failures:
        severity = self._severity_of(failure[1])
        fatigue += _FATIGUE_C * (severity ** _FATIGUE_M)
    self.fatigue_index = fatigue
    return fatigue
```

This is simpler and still captures the core behavior:
- 2 severity-2 errors: 2 × 4 = 8.0 (above threshold 5.0 → trigger)
- 1 severity-3 error: 1 × 9 = 9.0 (above threshold → trigger)
- 3 severity-1 errors: 3 × 1 = 3.0 (below threshold → OK)

This is good enough for v1. Time decay can be added later.

#### 4.1.4 修改 note_outcomes

```python
# 在 note_outcomes 末尾添加：
self._compute_fatigue()
```

#### 4.1.5 修改 snapshot

```python
# 在 snapshot() 中添加：
fatigue_index=self.fatigue_index,
fatigue_nudges=self.fatigue_nudges,
```

### 4.2 `ai_coding/core/turn_policy.py`

#### 4.2.1 修改 TurnSnapshot

```python
# 新增字段
fatigue_index: float = 0.0
fatigue_nudges: int = 0
```

#### 4.2.2 修改 decide_next_iteration

```python
# 在 stall 检查之后、failed_streak 检查之前添加：
# F2: fatigue gate — accumulated errors beyond threshold
if (
    snapshot.fatigue_index >= _FATIGUE_THRESHOLD
    and snapshot.fatigue_nudges < 1
):
    return TurnDecision(
        TurnAction.NUDGE,
        "accumulated errors (fatigue)",
        inject=self.fatigue_message(snapshot.fatigue_index),
    )
```

#### 4.2.3 新增 fatigue_message

```python
@staticmethod
def fatigue_message(fatigue_index: float) -> str:
    """The nudge when accumulated errors cross the fatigue threshold."""
    return "\n".join(
        [
            f"[fatigue-gate] 错误累积过高（fatigue index: {fatigue_index:.1f}）。",
            "不要继续当前路径：",
            "1) 停下来，总结你当前的理解和已取得的进展；",
            "2) 换一种完全不同的方法；",
            "3) 如果确实需要更多信息，用 ask_user 提问。",
        ]
    )
```

#### 4.2.4 新增常量

```python
FATIGUE_GATE_LEAD = "[fatigue-gate]"
```

#### 4.2.5 修改 TurnPolicyConfig

```python
# 新增配置
max_fatigue_nudges: int = 1
```

---

## 5. 数据流设计

```
工具调用失败 → note_outcomes() → failures 列表更新
    ↓
_compute_fatigue() → 遍历 failures，计算 severity² 之和
    ↓
fatigue_index 更新
    ↓
decide_next_iteration() → 检查 fatigue_index ≥ threshold
    ↓
触发 NUDGE → 注入 fatigue_message → 模型改变策略
    ↓
如果再次触发（fatigue_nudges 已用）→ FINISH
```

---

## 6. 测试计划

### 6.1 测试用例

| # | 测试用例 | 验证目标 |
|---|---------|---------|
| 1 | `test_fatigue_initial_zero` | 无失败时 fatigue = 0 |
| 2 | `test_fatigue_accumulates` | 多次失败累积 |
| 3 | `test_fatigue_severity_weighting` | 严重错误权重更高 |
| 4 | `test_fatigue_below_threshold` | 低于阈值不触发 |
| 5 | `test_fatigue_triggers_nudge` | 超过阈值触发 NUDGE |
| 6 | `test_fatigue_nudge_once` | 只触发一次 nudge |
| 7 | `test_fatigue_nudge_exhausted` | nudge 用尽后 FINISH |
| 8 | `test_fatigue_with_failed_streak` | 与 failed_streak 共存 |
| 9 | `test_severity_keywords` | 严重程度关键词匹配 |
| 10 | `test_fatigue_reset_per_turn` | 每 turn 重置 |

---

## 7. 架构设计图

```
┌────────────────────────────────────────────────────────┐
│               Fatigue System                            │
├────────────────────────────────────────────────────────┤
│                                                        │
│  ┌──────────────┐   记录失败    ┌──────────────────┐  │
│  │ Tool Call    │─────────────►│ TurnBooks        │  │
│  │ Failed       │              │ .failures        │  │
│  └──────────────┘              └────────┬─────────┘  │
│                                          │            │
│                                          ▼            │
│                               ┌──────────────────┐   │
│                               │ _compute_fatigue │   │
│                               │                  │   │
│                               │ Σ severity²      │   │
│                               │                  │   │
│                               │ timeout → 5²=25 │   │
│                               │ not found → 2²=4│   │
│                               │ default → 1²=1  │   │
│                               └────────┬─────────┘   │
│                                        │            │
│                                        ▼            │
│  ┌──────────────────────────────────────────────┐   │
│  │ TurnPolicy.decide_next_iteration()           │   │
│  │                                              │   │
│  │  if fatigue ≥ 5.0:                          │   │
│  │    → NUDGE (fatigue-gate)                    │   │
│  │    "错误累积过高，换方法或总结"               │   │
│  │                                              │   │
│  │  if fatigue ≥ 5.0 and nudge used:           │   │
│  │    → FINISH (graceful fracture)              │   │
│  └──────────────────────────────────────────────┘   │
│                                        │            │
│                                        ▼            │
│                               ┌──────────────────┐   │
│                               │ Model receives   │   │
│                               │ fatigue message  │   │
│                               │ and changes      │   │
│                               │ strategy         │   │
│                               └──────────────────┘   │
│                                                        │
└────────────────────────────────────────────────────────┘
```

---

## 8. 自审清单

### 8.1 最小化原则

- [x] 不新建独立类（FatigueTracker、InflammationBudget）
- [x] 不新建事件类型
- [x] 不修改持久化格式
- [x] 不修改 agent_loop.py
- [x] 只修改 2 个文件（turn_books + turn_policy）

### 8.2 第一性原理

- [x] 帕里斯定律是物理学验证的损伤累积模型
- [x] severity² 捕捉"大错误比小错误危害非线性增长"
- [x] 阈值触发是工程上的标准做法

### 8.3 复用检查

- [x] 复用 TurnBooks.failures（已有）
- [x] 复用 TurnSnapshot（扩展）
- [x] 复用 TurnDecision.NUDGE（已有）
- [x] 复用 note_outcomes()（已有）

### 8.4 数据一致性

- [x] fatigue_index 是 per-turn 的，TurnBooks 也是 per-turn
- [x] 每 turn 自动重置（新 TurnBooks 实例）
- [x] fatigue_nudges 防止无限 nudge

### 8.5 向后兼容

- [x] fatigue_index 默认 0.0 → 现有行为不变
- [x] fatigue_nudges 默认 0 → 现有行为不变
- [x] 无失败时 fatigue = 0 → 不触发任何新逻辑

---

## 9. 实施步骤

1. 写 `tests/test_fatigue_system.py`（10 个测试）
2. 运行 → 全部失败（红灯）
3. 修改 `turn_books.py` → 添加 fatigue 字段和计算
4. 运行 → fatigue 计算测试通过
5. 修改 `turn_policy.py` → 添加疲劳门
6. 运行 → 全部通过（绿灯）
7. 运行全量测试 → 无回归
8. ruff + mypy → 无警告

---

## 10. 预期效果

实施完成后：

1. **错误累积可见**：`fatigue_index` 量化了当前 turn 的错误负担
2. **严重错误优先处理**：timeout/context overflow 等高严重错误更快触发保护
3. **优雅退出**：fatigue 触发时，模型被引导总结进展并换方法，而非继续浪费 token
4. **无回归**：所有现有测试继续通过
