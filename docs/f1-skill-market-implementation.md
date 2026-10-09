# F1: Skill Market — 完整实现方案

> **目标**：用三个自适应信号替代静态配置，使系统具备动态学习能力
> **原则**：最小化改动、第一性原理、TDD、不造轮子

---

## 1. 问题分析

### 当前系统的三个静态缺陷

| 缺陷 | 现状 | 后果 |
|------|------|------|
| **技能评分** | 固定权重 `keyword=3, tool=2, task_type=2, failure=4` | 技能有效性漂移后无法自适应（如 React skill 在代码库转向 Vue 后仍被推荐） |
| **记忆淘汰** | 固定公式 `importance + 0.5×hits + 0.5×freshness` | 被矛盾的记忆和经过验证的记忆获得相同的保留优先级 |
| **技能发现** | 被动——模型必须失败/停滞才能发现下一个技能 | 技能切换延迟，模型走入死胡同 |

### 根因

系统缺少**经验反馈回路**。信号（成功/失败）发生了，但没有被量化并注入决策。

---

## 2. 设计方案

### 组件 1：技能动态定价（Dynamic Skill Pricing）

**核心思想**：每个技能维护一个价格 `p ∈ [0,1]`，通过贝叶斯更新反映其历史成功率。

**公式**：
```
p_new = clamp(p_old + α × (outcome - p_old), p_min, p_max)
```
- `outcome ∈ {0, 1}`（失败/成功）
- `α = 0.3`（学习率）
- 新技能以 `p = 0.5` 入场（最大不确定性）

**评分公式变更**：
```
total_score = base_score × (0.5 + price) + freshness
```
- `price = 0.5` → 乘数 = 1.0（中性，不影响原评分）
- `price = 1.0` → 乘数 = 1.5（成功技能获得 50% 加成）
- `price = 0.0` → 乘数 = 0.5（失败技能被削弱 50%）

**为什么用 `0.5 + price` 而非直接乘 `price`**：
- 直接乘 `price`：新技能（p=0.5）的 keyword 匹配得分从 3.0 → 1.5，低于阈值 3.0，永远无法被推荐
- 用 `0.5 + price`：新技能（p=0.5）乘数为 1.0，不影响原有行为；只有表现极差的技能才会被压制

### 组件 2：记忆质押（Memory Staking）

**核心思想**：每条记忆携带一个 stake，成功使用增加，被矛盾时罚没。

**公式**：
```
成功使用: s ← s × (1 + reward_rate)    # reward_rate = 0.2
被矛盾:   s ← s × (1 - slash_rate)     # slash_rate = 0.3
```

**淘汰公式变更**：
```
value = importance + 0.5 × min(hits, 4) + 0.5 × freshness + stake
```

**检索优先级**：stake 已经通过 value 间接影响淘汰，检索使用 BM25 不需要直接注入 stake。

### 组件 3：技能共激活图（Rhizomatic Skill Graph）

**核心思想**：追踪技能使用序列，当技能 A 被使用后，预加载常跟随技能 B 的上下文。

**机制**：
- 记录 `coactivation_graph[A][B] += 1`（B 在 A 之后 3 回合内被使用）
- 路由时，对 top skill 检查共激活图，预加载高频后续技能

---

## 3. 影响范围分析

### 3.1 需要修改的文件

| 文件 | 改动类型 | 改动量 |
|------|---------|--------|
| `ai_coding/core/sop_router.py` | 修改 SkillStats、SkillMatch、route()、新增方法 | ~50 行 |
| `ai_coding/memory/note.py` | MemoryNote 新增 stake 字段 | ~10 行 |
| `ai_coding/memory/store.py` | `_value_of()` 加入 stake，新增 `update_stake()` | ~15 行 |
| `ai_coding/memory/service.py` | recall 成功时增加 stake，矛盾时罚没 | ~20 行 |
| `ai_coding/domain/session.py` | 新增 `coactivation_graph` 字段 | ~3 行 |
| `ai_coding/core/agent_loop.py` | `_finish_turn` 记录技能结果 | ~10 行 |

### 3.2 需要新建的文件

| 文件 | 用途 |
|------|------|
| `tests/test_skill_market.py` | 完整 TDD 测试 |

### 3.3 不需要修改的文件

| 文件 | 原因 |
|------|------|
| `skills/registry.py` | 定价在 router 层，不在 registry 层 |
| `request_assembly.py` | 已通过 router 间接使用新评分 |
| `request_builder.py` | 尾块构建逻辑不变 |
| `turn_books.py` | 不需要新字段（技能结果通过现有 failures 判断） |
| `turn_policy.py` | 决策逻辑不变 |
| `subagent.py` | 不涉及 |

---

## 4. 现有设计复用分析

### 4.1 可复用的契约

| 现有契约 | 如何复用 |
|---------|---------|
| `SkillStats` dataclass | 直接添加 `price` 字段，不新建类 |
| `SkillMatch` frozen dataclass | 添加 `price` 字段用于展示 |
| `SOPRouter.record_load()` | 已有记录技能加载的回调机制 |
| `MemoryNote.frozen` + `replace()` | 通过 `with_stake()` 创建新实例 |
| `MemoryStore._mutate()` | `update_stake()` 复用原子写入模式 |
| `session.skill_stats: dict[str, Any]` | 已有 per-session 技能统计存储 |
| `session.tool_observations` | 已有跨 turn 观察机制 |

### 4.2 不需要新建的内容

| 不新建 | 原因 |
|--------|------|
| 独立的 PriceTracker 类 | 价格只是 SkillStats 的一个字段 |
| 独立的 StakeManager 类 | stake 只是 MemoryNote 的一个字段 |
| 持久化存储 | 当前 skill_stats 是 per-session，保持一致性 |
| 新的事件类型 | 价格更新是内部计算，不需要事件 |

---

## 5. 数据流设计

### 5.1 技能定价数据流

```
┌─────────────────────────────────────────────────────────────┐
│  技能定价数据流                                               │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌──────────┐   加载回调     ┌──────────────────┐          │
│  │SkillTool │───────────────►│ router.record_load│          │
│  │execute() │                │ (已有)            │          │
│  └──────────┘                └────────┬─────────┘          │
│                                       │                    │
│                                       ▼                    │
│  ┌──────────────────────────────────────────────┐          │
│  │ TurnBooks.loaded_skill = skill_name           │          │
│  │ (新增字段，记录本 turn 加载的技能)             │          │
│  └──────────────────────┬───────────────────────┘          │
│                         │                                  │
│                         ▼                                  │
│  ┌──────────────────────────────────────────────┐          │
│  │ _finish_turn()                               │          │
│  │ if books.loaded_skill:                       │          │
│  │   success = len(books.failures) == 0         │          │
│  │   router.record_outcome(skill, success)       │          │
│  └──────────────────────────────────────────────┘          │
│                         │                                  │
│                         ▼                                  │
│  ┌──────────────────────────────────────────────┐          │
│  │ 贝叶斯更新                                    │          │
│  │ p_new = p_old + α × (outcome - p_old)       │          │
│  │ 存储在 session.skill_stats[name].price       │          │
│  └──────────────────────────────────────────────┘          │
│                         │                                  │
│                         ▼                                  │
│  ┌──────────────────────────────────────────────┐          │
│  │ 下次 route() 调用                            │          │
│  │ total_score = base_score × (0.5 + price)     │          │
│  │              + freshness                      │          │
│  └──────────────────────────────────────────────┘          │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 5.2 记忆质押数据流

```
┌─────────────────────────────────────────────────────────────┐
│  记忆质押数据流                                               │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌──────────────────┐   检索成功    ┌──────────────────┐   │
│  │ recall_with_     │─────────────►│ 对每条 hit:       │   │
│  │ diagnostics()    │              │ stake ×= 1.2      │   │
│  └──────────────────┘              └──────────────────┘   │
│                                                             │
│  ┌──────────────────┐   矛盾检测    ┌──────────────────┐   │
│  │ _reconcile()     │─────────────►│ 被矛盾的记忆:     │   │
│  │ supersede 路径   │              │ stake ×= 0.7      │   │
│  └──────────────────┘              └──────────────────┘   │
│                                                             │
│  ┌──────────────────┐   淘汰决策    ┌──────────────────┐   │
│  │ evict()          │─────────────►│ value =          │   │
│  │                  │              │ importance + ... │   │
│  │                  │              │ + stake          │   │
│  └──────────────────┘              └──────────────────┘   │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## 6. 逐文件实现细节

### 6.1 `ai_coding/core/sop_router.py`

#### 6.1.1 新增常量

```python
# F1: Dynamic pricing
PRICE_ALPHA = 0.3
PRICE_MIN = 0.05
PRICE_MAX = 1.0
PRICE_QUARANTINE_THRESHOLD = 0.15

# F1: Co-activation graph
COACTIVATION_WINDOW = 3
COACTIVATION_THRESHOLD = 2
```

#### 6.1.2 修改 SkillStats

```python
@dataclass
class SkillStats:
    """Per-skill tracking for freshness scoring and dynamic pricing."""
    last_recommended_turn: int = 0
    last_loaded_turn: int = 0
    recommend_count: int = 0
    load_count: int = 0
    price: float = 0.5  # NEW: Bayesian price, 0.5 = max uncertainty
```

#### 6.1.3 修改 SkillMatch

```python
@dataclass(frozen=True)
class SkillMatch:
    """A skill that matched the current context."""
    name: str
    description: str
    score: float
    matched_dimensions: tuple[str, ...]
    triggers: dict[str, list[str]] = field(default_factory=dict)
    price: float = 0.5  # NEW: skill price for display
```

#### 6.1.4 新增 SOPRouter 方法

```python
def record_outcome(self, skill_name: str, success: bool,
                   stats: dict[str, SkillStats]) -> None:
    """Update skill price via Bayesian updating.
    
    :param skill_name: name of the skill that was used
    :param success: whether the skill usage was successful
    :param stats: session's skill stats dict
    """
    if not skill_name:
        return
    if skill_name not in stats:
        stats[skill_name] = SkillStats()
    s = stats[skill_name]
    outcome = 1.0 if success else 0.0
    s.price = max(PRICE_MIN, min(PRICE_MAX, s.price + PRICE_ALPHA * (outcome - s.price)))

def record_coactivation(self, from_skill: str, to_skill: str,
                        graph: dict[str, dict[str, int]]) -> None:
    """Record that to_skill was used after from_skill within the window."""
    if not from_skill or not to_skill or from_skill == to_skill:
        return
    if from_skill not in graph:
        graph[from_skill] = {}
    graph[from_skill][to_skill] = graph[from_skill].get(to_skill, 0) + 1

def get_followup_skills(self, skill_name: str,
                        graph: dict[str, dict[str, int]]) -> list[str]:
    """Get skills that frequently follow the given skill."""
    edges = graph.get(skill_name, {})
    return [name for name, count in edges.items() if count >= COACTIVATION_THRESHOLD]
```

#### 6.1.5 修改 route() 方法

```python
# 在 route() 中，修改评分计算部分：
price = stats.get(name, SkillStats()).price
total_score = base_score * (0.5 + price) + freshness
```

```python
# 在创建 SkillMatch 时添加 price：
matches.append(SkillMatch(
    name=name,
    description=meta.get("description", "") or "",
    score=total_score,
    matched_dimensions=tuple(dimensions),
    triggers=triggers,
    price=price,  # NEW
))
```

#### 6.1.6 修改 format_block() 方法

```python
# 在 format_block 中添加价格显示：
lines.append(f"- {m.name}: {m.description} (confidence: {int(m.price * 100)}%)")
```

### 6.2 `ai_coding/memory/note.py`

#### 6.2.1 修改 MemoryNote

```python
@dataclass(frozen=True)
class MemoryNote:
    ...
    # F1: stake for staking/slashing
    stake: float = 1.0
```

#### 6.2.2 修改 __post_init__

```python
# 在 __post_init__ 中添加：
object.__setattr__(self, "stake", max(0.0, float(self.stake or 1.0)))
```

#### 6.2.3 新增 with_stake 方法

```python
def with_stake(self, stake: float, when: str | None = None) -> MemoryNote:
    """Return a new note with updated stake."""
    return replace(self, stake=max(0.0, stake), updated_time=when or now_utc())
```

#### 6.2.4 修改 to_dict

```python
# 在 to_dict 中添加：
if self.stake != 1.0:
    data["stake"] = round(self.stake, 4)
```

#### 6.2.5 修改 from_dict

```python
# 在 from_dict 中添加：
stake=float(data.get("stake", 1.0) or 1.0),
```

### 6.3 `ai_coding/memory/store.py`

#### 6.3.1 修改 _value_of

```python
def _value_of(note: MemoryNote, moment: datetime) -> float:
    """Higher is more worth keeping: importance, usage, freshness, stake."""
    freshness = recency(
        note.created_time, now=moment, half_life_days=_EVICTION_HALF_LIFE_DAYS
    )
    return note.importance + 0.5 * min(note.hits, 4) + 0.5 * freshness + note.stake
```

#### 6.3.2 新增 update_stake 方法

```python
def update_stake(self, note_id: str, new_stake: float) -> bool:
    """Update a note's stake; False if not found."""
    def _do() -> bool:
        for index, note in enumerate(self._notes):
            if note.id != note_id:
                continue
            self._notes[index] = note.with_stake(new_stake)
            return True
        return False
    return self._mutate(_do)
```

### 6.4 `ai_coding/memory/service.py`

#### 6.4.1 新增常量

```python
# F1: Memory staking
STAKE_REWARD_RATE = 0.2
STAKE_SLASH_RATE = 0.3
```

#### 6.4.2 修改 recall_with_diagnostics

```python
# 在 recall_with_diagnostics 中，修改 hit 处理部分：
if hits and not timed_out:
    try:
        for hit in hits:
            new_stake = hit.note.stake * (1 + STAKE_REWARD_RATE)
            self._store.update_stake(hit.note.id, new_stake)
        self._store.touch_notes([hit.note.id for hit in hits])
    except Exception:
        pass
```

#### 6.4.3 修改 _reconcile

```python
# 在 _reconcile 中，supersede 路径添加 stake 罚没：
if candidate.action == "supersede" and candidate.target_id:
    old_note = self._store.get(candidate.target_id)
    if old_note:
        slashed = old_note.stake * (1 - STAKE_SLASH_RATE)
        self._store.update_stake(candidate.target_id, slashed)
    if self._store.supersede(candidate.target_id, note):
        superseded.append(note.id)
```

### 6.5 `ai_coding/domain/session.py`

#### 6.5.1 新增字段

```python
# 在 SessionData 中添加：
# F1: in-process only — skill co-activation graph.
# Key = from_skill, Value = {to_skill: count}
# Not persisted (to_dict is a whitelist).
coactivation_graph: dict[str, dict[str, int]] = field(default_factory=dict)
```

### 6.6 `ai_coding/core/agent_loop.py`

#### 6.6.1 修改 _finish_turn

```python
# 在 _finish_turn 中，harvest 之前添加：
router = getattr(self.assembler, '_router', None)
if router and hasattr(books, 'loaded_skill') and books.loaded_skill:
    stats = getattr(session, 'skill_stats', {})
    success = not books.failures
    router.record_outcome(books.loaded_skill, success, stats)
```

#### 6.6.2 修改 SkillTool 加载回调

```python
# 在 _begin_turn 中，修改 SkillTool 回调以记录 loaded_skill：
if isinstance(skill_tool, SkillTool) and router:
    def _on_skill_loaded(name: str, _books=books):
        router.record_load(name, session.turn_count, session.skill_stats)
        _books.loaded_skill = name
    skill_tool.set_on_loaded(_on_skill_loaded)
```

#### 6.6.3 修改 TurnBooks

```python
# 在 TurnBooks 中添加：
loaded_skill: str = ""  # Skill loaded this turn (for outcome tracking)
```

---

## 7. 测试计划

### 7.1 测试用例清单

| # | 测试用例 | 验证目标 | 文件 |
|---|---------|---------|------|
| 1 | `test_price_initial_value` | 新技能 price = 0.5 | test_skill_market.py |
| 2 | `test_price_increases_on_success` | 成功后 price 增加 | test_skill_market.py |
| 3 | `test_price_decreases_on_failure` | 失败后 price 减少 | test_skill_market.py |
| 4 | `test_price_clamped_min` | price 不会低于 PRICE_MIN | test_skill_market.py |
| 5 | `test_price_clamped_max` | price 不会超过 PRICE_MAX | test_skill_market.py |
| 6 | `test_price_weighted_scoring` | 同 base score 下高 price 胜出 | test_skill_market.py |
| 7 | `test_price_display_in_match` | SkillMatch 包含 price | test_skill_market.py |
| 8 | `test_price_convergence` | 多次使用后 price 收敛 | test_skill_market.py |
| 9 | `test_stake_initial_value` | 新记忆 stake = 1.0 | test_skill_market.py |
| 10 | `test_stake_increases_on_recall` | 检索成功后 stake 增加 | test_skill_market.py |
| 11 | `test_stake_slash_on_supersede` | 被矛盾时 stake 罚没 | test_skill_market.py |
| 12 | `test_stake_weighted_eviction` | 低 stake 记忆先被淘汰 | test_skill_market.py |
| 13 | `test_stake_serialization` | stake 正确序列化/反序列化 | test_skill_market.py |
| 14 | `test_coactivation_tracking` | 技能共激活被记录 | test_skill_market.py |
| 15 | `test_followup_preload` | 高频后续技能被识别 | test_skill_market.py |
| 16 | `test_full_workflow` | 完整工作流：推荐→使用→结果→价格更新 | test_skill_market.py |

### 7.2 测试设计原则

- 每个测试独立，不依赖其他测试状态
- 使用 `tmp_path` fixture 隔离文件系统
- 使用 `SimpleNamespace` 模拟 session 对象
- 测试覆盖正常路径和边界条件

---

## 8. 架构设计图

```
┌─────────────────────────────────────────────────────────────────────┐
│                     Skill Market Layer                              │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │  Dynamic Pricing                                            │   │
│  │                                                             │   │
│  │  SkillTool.load() ──► record_load() ──► TurnBooks.loaded   │   │
│  │                                          │                  │   │
│  │  _finish_turn() ◄── record_outcome() ◄──┘                  │   │
│  │       │                                                     │   │
│  │       ▼                                                     │   │
│  │  p_new = p_old + α(outcome - p_old)                        │   │
│  │       │                                                     │   │
│  │       ▼                                                     │   │
│  │  route(): score = base × (0.5 + price) + freshness         │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │  Memory Staking                                             │   │
│  │                                                             │   │
│  │  recall() ──► hit ──► stake ×= 1.2                         │   │
│  │  supersede() ──► old note ──► stake ×= 0.7                 │   │
│  │       │                                                     │   │
│  │       ▼                                                     │   │
│  │  evict(): value = importance + hits + freshness + stake    │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │  Co-activation Graph                                        │   │
│  │                                                             │   │
│  │  Skill A used ──► Skill B used (within 3 turns)            │   │
│  │       │                                                     │   │
│  │       ▼                                                     │   │
│  │  graph[A][B] += 1                                           │   │
│  │       │                                                     │   │
│  │       ▼                                                     │   │
│  │  route(A) ──► check graph[A] ──► preload followups         │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 9. 自审清单

### 9.1 最小化原则

- [x] 不新建独立类（PriceTracker、StakeManager）
- [x] 不新建事件类型
- [x] 不修改持久化格式（stake 序列化是向后兼容的）
- [x] 不修改 turn_policy.py（决策逻辑不变）
- [x] 不修改 request_builder.py（尾块构建不变）

### 9.2 第一性原理

- [x] 价格公式 `0.5 + price` 确保新技能不受惩罚
- [x] stake 默认 1.0 确保现有记忆不受影响
- [x] 贝叶斯更新是标准的在线学习算法
- [x] 共激活图是推荐系统的标准技术

### 9.3 复用检查

- [x] SkillStats 已有 → 只加字段
- [x] SkillMatch 已有 → 只加字段
- [x] MemoryNote.frozen + replace() 已有 → 只加方法
- [x] _mutate() 已有 → 复用原子写入
- [x] session.skill_stats 已有 → 复用存储

### 9.4 数据一致性

- [x] MemoryNote 是 frozen → stake 更新通过 with_stake() 创建新实例
- [x] SkillStats 是普通 dataclass → price 可直接修改
- [x] session 字段都是 in-process → 不需要持久化
- [x] to_dict 是 whitelist → 新字段不会泄漏到磁盘

### 9.5 边界条件

- [x] price 有 min/max  clamp
- [x] stake 有 min=0 保护
- [x] record_outcome 处理空 skill_name
- [x] record_coactivation 处理 self-loop
- [x] update_stake 处理不存在的 note_id

### 9.6 向后兼容

- [x] 现有测试不受影响（price 默认 0.5 = 中性）
- [x] 现有 .memory 文件可读（stake 默认 1.0）
- [x] 现有 session 文件可读（新字段不持久化）
- [x] format_block 输出格式变化但不破坏解析

---

## 10. 实施步骤（TDD 顺序）

1. **先写测试** `tests/test_skill_market.py`（16 个测试用例）
2. **运行测试** → 全部失败（红灯）
3. **修改 `sop_router.py`** → 添加 price 字段、record_outcome、评分公式
4. **运行测试** → 定价相关测试通过
5. **修改 `note.py`** → 添加 stake 字段、with_stake
6. **修改 `store.py`** → 修改 _value_of、添加 update_stake
7. **运行测试** → 质押相关测试通过
8. **修改 `service.py`** → recall 增加 stake、supersede 罚没 stake
9. **运行测试** → 服务层测试通过
10. **修改 `session.py`** → 添加 coactivation_graph
11. **修改 `agent_loop.py`** → _finish_turn 记录结果
12. **运行测试** → 全部通过（绿灯）
13. **运行全量测试** → 无回归
14. **ruff + mypy** → 无警告

---

## 11. 预期效果

实施完成后：

1. **技能定价**：一个技能在连续失败 3 次后，price 从 0.5 降至 ~0.17，其推荐得分被大幅压制，系统自动转向其他技能
2. **记忆质押**：一条被检索成功 5 次的记忆，stake 从 1.0 增至 ~2.49，在淘汰时优先保留
3. **共激活图**：当 "debug-test-failure" 被使用后，"read-stack-trace" 被预加载到推荐中
4. **无回归**：所有现有测试继续通过，新行为是纯增量的
