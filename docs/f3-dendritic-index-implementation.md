# F3: Dendritic Index (Cross-Presentation Layer) — 完整实现方案

> **目标**：实现"交叉呈递层"——当技能成功执行时提取结构签名，在新会话路由时匹配签名并提升相关技能分数
> **原则**：最小化改动、只改根源、复用现有基础设施（原子写入、Jaccard 相似度）
> **范围**：仅实现 Dendritic Index（交叉呈递层），Session Mycelium Graph 作为后续扩展

---

## 1. 问题分析

### 当前系统的知识孤岛

| 孤岛 | 现状 | 后果 |
|------|------|------|
| **技能间** | 重构技能的成功经验无法帮助调试技能 | 每个技能从零开始 |
| **会话间** | Session A 的发现对 Session B 完全不可见 | 重复踩坑 |
| **记忆与技能** | 记忆系统和技能系统没有对话 | 记忆中的经验无法指导技能推荐 |

### 根因

系统缺少**成功经验的结构化提取和匹配机制**。失败模式（`failure_pattern`）已被 SOP Router 使用，但成功模式未被利用。

---

## 2. 设计方案

### 核心概念：结构签名（Structural Signature）

当技能成功执行时，记录当时的**上下文特征**：

```python
@dataclass
class StructuralSignature:
    tools: frozenset[str]          # 使用的工具（如 {"bash", "read"}）
    error_keywords: frozenset[str] # 遇到的错误关键词（如 {"permission", "denied"}）
    task_keywords: frozenset[str]  # 任务关键词（如 {"fix", "sandbox", "workspace"}）
    skill_name: str                # 成功的技能名
    timestamp: str                 # 记录时间
```

### 树突索引（Dendritic Index）

持久化存储所有结构签名，支持：
- **记录**：添加新签名（FIFO 淘汰，上限 200）
- **匹配**：给定当前上下文，返回匹配的技能及相似度分数
- **持久化**：原子写入 `.dendritic/index.json`

### 匹配算法

```
score = 0.4 × Jaccard(当前工具, 签名工具)
      + 0.3 × Jaccard(当前错误, 签名错误关键词)
      + 0.3 × Jaccard(当前任务, 签名任务关键词)

if score ≥ MATCH_THRESHOLD (0.3):
    技能分数 += score × SIGNATURE_MATCH_WEIGHT (2.0)
```

权重理由：
- 工具重叠（0.4）：最稳定的信号——同一类工具面临的问题通常相似
- 错误关键词（0.3）：错误模式相似意味着问题域相似
- 任务关键词（0.3）：任务类型相似意味着解决方案可能通用

---

## 3. 影响范围分析

### 3.1 需要修改的文件

| 文件 | 改动类型 | 改动量 |
|------|---------|--------|
| `ai_coding/core/sop_router.py` | 新增 StructuralSignature、DendriticIndex，集成到 route() | ~80 行 |
| `ai_coding/core/agent_loop.py` | _finish_turn() 中提取并记录签名 | ~15 行 |

### 3.2 需要新建的文件

| 文件 | 用途 |
|------|------|
| `tests/test_cross_domain_immune.py` | TDD 测试 |

### 3.3 不需要修改的文件

| 文件 | 原因 |
|------|------|
| `domain/session.py` | 签名存储在独立文件，不需要 session 字段 |
| `memory/service.py` | 不涉及记忆系统 |
| `turn_books.py` | 不涉及 |
| `turn_policy.py` | 不涉及 |
| `request_assembly.py` | 不涉及（通过 router 间接使用） |

### 3.4 可复用的契约

| 现有契约 | 如何复用 |
|---------|---------|
| `from_json_file()` / `to_json_pretty()` (`infra/json_utils.py`) | 签名文件的序列化/反序列化 |
| 原子写入模式 (tmp + os.replace) | `memory/store.py:128-141` 的持久化模式 |
| `similarity()` (`memory/tokenize.py:135`) | Jaccard 相似度计算 |
| `SkillRegistry(Path)` 模式 | DendriticIndex 接受 path 参数 |
| `record_outcome()` | 在相同位置 (agent_loop.py:_finish_turn) 添加签名记录 |

---

## 4. 逐文件实现细节

### 4.1 `ai_coding/core/sop_router.py`

#### 4.1.1 新增 StructuralSignature 数据类

```python
from dataclasses import dataclass, field
from typing import FrozenSet

@dataclass(frozen=True)
class StructuralSignature:
    """结构签名：技能成功执行时的上下文特征。"""
    
    tools: FrozenSet[str]          # 使用的工具集合
    error_keywords: FrozenSet[str] # 错误关键词集合
    task_keywords: FrozenSet[str]  # 任务关键词集合
    skill_name: str                # 成功的技能名称
    timestamp: str                 # ISO8601 时间戳
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "tools": sorted(self.tools),
            "error_keywords": sorted(self.error_keywords),
            "task_keywords": sorted(self.task_keywords),
            "skill_name": self.skill_name,
            "timestamp": self.timestamp,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StructuralSignature:
        return cls(
            tools=frozenset(data.get("tools", [])),
            error_keywords=frozenset(data.get("error_keywords", [])),
            task_keywords=frozenset(data.get("task_keywords", [])),
            skill_name=data.get("skill_name", ""),
            timestamp=data.get("timestamp", ""),
        )
```

#### 4.1.2 新增 DendriticIndex 类

```python
import os
from pathlib import Path
from ai_coding.infra.json_utils import from_json_file, to_json_pretty
from ai_coding.infra.time_utils import now_utc

MAX_DENDRITIC_SIGNATURES = 200
MATCH_THRESHOLD = 0.3
SIGNATURE_MATCH_WEIGHT = 2.0

# 匹配权重
WEIGHT_TOOLS = 0.4
WEIGHT_ERRORS = 0.3
WEIGHT_TASK = 0.3


class DendriticIndex:
    """技能成功执行的结构签名索引。
    
    持久化到 .dendritic/index.json，支持签名记录和相似度匹配。
    """
    
    def __init__(self, path: str | Path = ".dendritic/index.json") -> None:
        self._path = Path(path)
        self._signatures: list[StructuralSignature] = []
        self._load()
    
    # ---- public API -------------------------------------------------------
    
    def record(self, signature: StructuralSignature) -> None:
        """添加签名到索引（FIFO 淘汰）。"""
        self._signatures.append(signature)
        self._enforce_cap()
        self._save()
    
    def match(
        self,
        tools: set[str],
        errors: set[str],
        task_keywords: set[str],
    ) -> list[tuple[str, float]]:
        """匹配当前上下文与存储的签名。
        
        返回 [(skill_name, match_score), ...]，按分数降序。
        """
        results: list[tuple[str, float]] = []
        for sig in self._signatures:
            score = self._compute_similarity(sig, tools, errors, task_keywords)
            if score >= MATCH_THRESHOLD:
                results.append((sig.skill_name, score))
        results.sort(key=lambda x: x[1], reverse=True)
        return results
    
    # ---- persistence -------------------------------------------------------
    
    def _load(self) -> None:
        """从文件加载签名。"""
        if not self._path.is_file():
            return
        try:
            data = from_json_file(self._path)
        except (ValueError, OSError):
            return
        if not isinstance(data, dict):
            return
        for item in data.get("signatures", []):
            try:
                self._signatures.append(StructuralSignature.from_dict(item))
            except (ValueError, TypeError, KeyError):
                continue
    
    def _save(self) -> None:
        """原子写入。"""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = to_json_pretty({
                "version": 1,
                "signatures": [sig.to_dict() for sig in self._signatures],
            })
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            pass
    
    def _enforce_cap(self) -> None:
        """FIFO 淘汰，保持上限。"""
        if len(self._signatures) > MAX_DENDRITIC_SIGNATURES:
            self._signatures = self._signatures[-MAX_DENDRITIC_SIGNATURES:]
    
    # ---- matching ---------------------------------------------------------
    
    @staticmethod
    def _compute_similarity(
        sig: StructuralSignature,
        tools: set[str],
        errors: set[str],
        task_keywords: set[str],
    ) -> float:
        """计算综合相似度分数。"""
        tool_sim = DendriticIndex._jaccard(tools, set(sig.tools))
        error_sim = DendriticIndex._jaccard(errors, set(sig.error_keywords))
        task_sim = DendriticIndex._jaccard(task_keywords, set(sig.task_keywords))
        return (WEIGHT_TOOLS * tool_sim 
                + WEIGHT_ERRORS * error_sim 
                + WEIGHT_TASK * task_sim)
    
    @staticmethod
    def _jaccard(a: set[str], b: set[str]) -> float:
        """Jaccard 相似度。"""
        if not a and not b:
            return 1.0
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)
```

#### 4.1.3 修改 SOPRouter 集成 DendriticIndex

```python
class SOPRouter:
    def __init__(self, skills: SkillRegistry, threshold: float = DEFAULT_THRESHOLD,
                 max_matches: int = DEFAULT_MAX_MATCHES,
                 dendritic_path: str | Path | None = None):
        self._skills = skills
        self._threshold = threshold
        self._max_matches = max_matches
        # F3: dendritic index for cross-domain matching
        if dendritic_path is not None:
            self._dendritic = DendriticIndex(dendritic_path)
        else:
            self._dendritic = None  # type: ignore
```

#### 4.1.4 修改 route() 方法——添加树突匹配

```python
# 在 route() 的评分循环中，total_score 计算之后添加：

# F3: dendritic index matching — boost skill score if context matches
# past successful executions
if self._dendritic:
    dendritic_matches = self._dendritic.match(
        tools=set(recent_tools),
        errors=set(f[1] for f in all_failures),
        task_keywords=set(task_goal.lower().split()),
    )
    for match_name, match_score in dendritic_matches:
        if name == match_name:
            total_score += match_score * SIGNATURE_MATCH_WEIGHT
```

#### 4.1.5 新增 record_signature() 方法

```python
def record_signature(self, signature: StructuralSignature) -> None:
    """记录技能成功执行的结构签名。"""
    if self._dendritic is not None:
        self._dendritic.record(signature)
```

### 4.2 `ai_coding/core/agent_loop.py`

#### 4.2.1 修改 _finish_turn()

在 F1 的 `record_outcome()` 之后添加签名记录：

```python
async def _finish_turn(self, session, *, user_input, task, frame, books, outcome, closing=""):
    # F1: record skill outcome for dynamic pricing
    router = getattr(self.assembler, '_router', None)
    if router and session.loaded_skill:
        stats = getattr(session, 'skill_stats', {})
        success = not books.failures
        router.record_outcome(session.loaded_skill, success, stats)
        
        # F3: record structural signature on skill success
        if success and task:
            from ai_coding.core.sop_router import StructuralSignature
            # Extract error keywords from tool observations
            error_keywords = frozenset(
                word.lower()
                for obs in getattr(session, "tool_observations", [])
                if obs.error
                for word in obs.error.lower().split()
                if len(word) > 2
            )
            # Extract task keywords
            task_keywords = frozenset(
                word for word in task.goal.lower().split() if len(word) > 2
            ) if task.goal else frozenset()
            
            signature = StructuralSignature(
                tools=frozenset(books.tool_success),
                error_keywords=error_keywords,
                task_keywords=task_keywords,
                skill_name=session.loaded_skill,
                timestamp=now_utc(),
            )
            router.record_signature(signature)
    
    # ... rest of _finish_turn unchanged ...
```

---

## 5. 数据流设计

```
┌─────────────────────────────────────────────────────────────────────┐
│  技能成功 → 签名提取与记录                                          │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  SkillTool.load()                                                  │
│       │                                                             │
│       ▼                                                             │
│  session.loaded_skill = "sandbox-fix"                              │
│       │                                                             │
│       ▼                                                             │
│  模型执行工具调用（bash, read, write...）                           │
│       │                                                             │
│       ▼                                                             │
│  _finish_turn() → success = not books.failures                     │
│       │                                                             │
│       ▼ (if success)                                                │
│  提取签名：                                                         │
│    tools = books.tool_success = {"bash", "read"}                   │
│    error_keywords = {关键词 from tool_observations}                │
│    task_keywords = {关键词 from task.goal}                         │
│    skill_name = session.loaded_skill                               │
│       │                                                             │
│       ▼                                                             │
│  dendritic_index.record(signature)                                 │
│       │                                                             │
│       ▼                                                             │
│  .dendritic/index.json (原子写入)                                  │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│  路由匹配 → 技能分数提升                                            │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  route() 被调用                                                     │
│       │                                                             │
│       ▼                                                             │
│  计算 base_score (keyword + tool + task_type + failure)           │
│       │                                                             │
│       ▼                                                             │
│  dendritic_index.match(current_tools, current_errors, task)        │
│       │                                                             │
│       ▼                                                             │
│  [("sandbox-fix", 0.65), ("other-skill", 0.42)]                   │
│       │                                                             │
│       ▼                                                             │
│  for each match:                                                   │
│    if skill_name == match_name:                                    │
│      total_score += match_score × 2.0                              │
│       │                                                             │
│       ▼                                                             │
│  sandbox-fix: 3.0 + (0.65 × 2.0) = 4.3 → 超过阈值 → 推荐         │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 6. 逐文件实现细节（精确到行）

### 6.1 `sop_router.py` 改动

**位置 1**：文件顶部，`from dataclasses import dataclass, field` 之后添加：
```python
from ai_coding.infra.json_utils import from_json_file, to_json_pretty
from ai_coding.infra.time_utils import now_utc
```
（注意：`now_utc` 已经在文件顶部导入，不需要重复添加）

**位置 2**：`_WEIGHT_TASK_TYPE` 常量之后，添加 DendriticIndex 相关常量：
```python
# F3: Dendritic Index — cross-presentation layer.
MAX_DENDRITIC_SIGNATURES = 200
MATCH_THRESHOLD = 0.3
SIGNATURE_MATCH_WEIGHT = 2.0
WEIGHT_TOOLS = 0.4
WEIGHT_ERRORS = 0.3
WEIGHT_TASK = 0.3
```

**位置 3**：`SkillMatch` 类之后，添加 `StructuralSignature` 和 `DendriticIndex` 类。

**位置 4**：`SOPRouter.__init__()` 添加 `dendritic_path` 参数和 `self._dendritic`。

**位置 5**：`SOPRouter.route()` 的评分循环中，`total_score` 计算后添加树突匹配。

**位置 6**：`SOPRouter` 末尾添加 `record_signature()` 方法。

### 6.2 `agent_loop.py` 改动

**位置 1**：`_finish_turn()` 中，`record_outcome()` 之后添加签名记录逻辑。

---

## 7. 测试计划

### 7.1 测试用例

| # | 测试用例 | 验证目标 |
|---|---------|---------|
| 1 | `test_signature_creation` | 结构签名包含正确字段 |
| 2 | `test_signature_serialization` | to_dict/from_dict 往返 |
| 3 | `test_dendritic_index_record` | 记录签名后文件存在 |
| 4 | `test_dendritic_index_persistence` | 签名在 reload 后保留 |
| 5 | `test_dendritic_index_cap` | FIFO 淘汰 |
| 6 | `test_jaccard_similarity` | Jaccard 计算正确 |
| 7 | `test_matching_identical_context` | 完全匹配 → 高分 |
| 8 | `test_matching_partial_context` | 部分匹配 → 中分 |
| 9 | `test_matching_no_overlap` | 无重叠 → 低于阈值 |
| 10 | `test_dendritic_boosts_route_score` | 匹配的技能分数被提升 |
| 11 | `test_no_dendritic_no_boost` | 无 DendriticIndex 时行为不变 |
| 12 | `test_full_workflow` | 记录 → 保存 → 加载 → 匹配 → 提升 |

---

## 8. 架构设计图

```
┌──────────────────────────────────────────────────────────────┐
│                  Dendritic Index System                       │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌─────────────┐    成功执行     ┌────────────────────┐     │
│  │ SOPRouter   │───────────────►│ DendriticIndex     │     │
│  │ record_     │                │                    │     │
│  │ signature() │                │ record(signature)  │     │
│  └─────────────┘                │        │           │     │
│                                 │        ▼           │     │
│                                 │ ┌────────────────┐ │     │
│  ┌─────────────┐    匹配查询    │ │ Signatures     │ │     │
│  │ SOPRouter   │◄──────────────│ │ [              │ │     │
│  │ route()     │               │ │   {tools,      │ │     │
│  │             │  boost score  │ │    errors,     │ │     │
│  │  +2.0×sim   │◄──────────────│ │    task,       │ │     │
│  └─────────────┘               │ │    skill},     │ │     │
│                                 │   ...            │ │     │
│                                 │ ]                │ │     │
│                                 │                  │ │     │
│                                 │ match(tools,     │ │     │
│                                 │   errors, task)  │ │     │
│                                 │        │         │ │     │
│                                 │        ▼         │ │     │
│                                 │ [(skill, score)] │ │     │
│                                 │                  │ │     │
│                                 │ FIFO cap: 200    │ │     │
│                                 └────────┬─────────┘ │     │
│                                          │           │     │
│                                          ▼           │     │
│                                 ┌────────────────┐   │     │
│                                 │ .dendritic/    │   │     │
│                                 │ index.json     │   │     │
│                                 │ (atomic write) │   │     │
│                                 └────────────────┘   │     │
│                                                      │     │
└──────────────────────────────────────────────────────────────┘
```

---

## 9. 自审清单

### 9.1 最小化原则

- [x] 不新建 Session Graph（推迟到 v2）
- [x] 不新建事件类型
- [x] 不修改 session.py
- [x] 不修改 memory/service.py
- [x] 只修改 2 个生产文件

### 9.2 第一性原理

- [x] 结构签名捕捉"什么上下文中什么技能有效"
- [x] Jaccard 相似度是标准的集合相似度度量
- [x] 加权组合（工具 0.4 + 错误 0.3 + 任务 0.3）反映各维度的重要性

### 9.3 复用检查

- [x] 复用 `from_json_file()` / `to_json_pretty()` 序列化
- [x] 复用原子写入模式 (tmp + os.replace)
- [x] 复用 SOPRouter 的现有评分框架
- [x] 复用 `SkillRegistry(Path)` 的路径参数模式
- [x] 复用 `now_utc()` 时间戳

### 9.4 数据一致性

- [x] DendriticIndex 是独立持久化，不依赖 session
- [x] 原子写入防止文件损坏
- [x] FIFO 淘汰防止无限增长

### 9.5 向后兼容

- [x] `dendritic_path=None` 时不创建索引，行为不变
- [x] 无签名文件时 `match()` 返回空列表
- [x] 现有路由逻辑完全不受影响（仅添加额外分数）

### 9.6 边界条件

- [x] 空签名列表 → match 返回 []
- [x] 损坏的 JSON 文件 → _load 忽略
- [x] 写入失败 → _save 静默忽略（best-effort）
- [x] 两个集合都为空 → Jaccard 返回 1.0
- [x] 一个集合为空 → Jaccard 返回 0.0

---

## 10. 实施步骤

1. 写 `tests/test_cross_domain_immune.py`（12 个测试）
2. 运行 → 全部失败（红灯）
3. 修改 `sop_router.py` → 添加 StructuralSignature、DendriticIndex 类
4. 运行 → 数据类和索引测试通过
5. 修改 `sop_router.py` → 集成到 route() 和 __init__()
6. 运行 → 路由提升测试通过
7. 修改 `agent_loop.py` → 添加签名提取
8. 运行 → 全部通过（绿灯）
9. 运行全量测试 → 无回归
10. ruff + mypy → 无警告

---

## 11. 预期效果

实施完成后：

1. **技能成功经验被记录**：每次技能成功执行后，其上下文特征被存入树突索引
2. **跨域匹配**：新会话中遇到相似上下文时，历史成功的技能被自动提升
3. **分数提升显著**：匹配的技能获得额外 0.6~2.0 分（取决于相似度），显著提高被推荐概率
4. **无回归**：所有现有测试继续通过，新行为是纯增量的
5. **持久化**：索引在会话间保持，文件损坏自动恢复

### 示例场景

```
Session 1: "修复沙箱文件写入问题"
  - 加载 sandbox-fix 技能
  - 使用 bash, read 工具
  - 遇到 "permission denied" 错误
  - 成功完成 → 记录签名:
    {tools: {bash, read}, errors: {permission, denied}, 
     task: {修复, 沙箱, 文件, 写入}, skill: sandbox-fix}

Session 2: "修复 Git 权限问题"（新会话）
  - 当前上下文: tools={bash, read}, errors={permission denied}, task={修复, git, 权限}
  - 路由时匹配: Jaccard(tools) = 1.0, Jaccard(errors) = 1.0, Jaccard(task) = 0.2
  - 综合分数: 0.4×1.0 + 0.3×1.0 + 0.3×0.2 = 0.76
  - sandbox-fix 获得 0.76 × 2.0 = 1.52 分提升
  - 即使 task 不同，工具和错误的高度重叠仍触发推荐
```
