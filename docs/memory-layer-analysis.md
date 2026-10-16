# 记忆层架构分析

## 1. 概览

记忆层 (`ai_coding.memory`) 是一个跨会话持久化的 AI 记忆系统，替代了 M3 版本中仅能存储 `{id, text, created_time}` 的简单 append-only 列表。M3 存在五大问题：同一事实重复存储五次、按插入顺序排名、矛盾共存、驱逐时删除最重要条目、早期决策被压缩破坏。新设计通过结构化数据模型、双层作用域、BM25+语义混合检索、价值驱动驱逐等机制逐一解决这些问题。

**核心文件：**

| 文件 | 职责 |
|------|------|
| `note.py` | `MemoryNote` 核心数据契约 |
| `store.py` | `MemoryStore` 单级磁盘索引 |
| `scoped_store.py` | `ScopedStore` 双级作用域路由 |
| `relevance.py` | BM25 检索、相关性门控、评分排序 |
| `embeddings.py` | `EmbeddingIndex` 可选语义向量层 |
| `schema.py` | `HarvestCandidate` 提取器输出契约 |
| `service.py` | `MemoryService` 高层编排（recall/harvest/consolidate） |

---

## 2. 数据模型：MemoryNote

`MemoryNote` 是不可变冻结数据类（frozen dataclass），每条记忆包含以下字段：

### 2.1 基础字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | `str` | UUID hex，唯一标识 |
| `text` | `str` | 记忆正文（纯文本句子，拒绝序列化片段） |
| `kind` | `NoteKind` | 枚举：`fact` / `decision` / `preference` / `open_thread` |
| `scope` | `str` | 作用域：`"workspace"`（默认）或 `"user"`（用户级） |
| `scene` | `str` | 场景标签（自由文本，最大80字符，用于归组过滤） |
| `importance` | `int` | 重要度 1-5 |
| `created_time` / `updated_time` | `str` | ISO-8601 时间戳 |

### 2.2 溯源字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `source_session` | `str` | 来源会话 ID |
| `source_turn` | `int` | 来源轮次编号 |
| `source_message_ids` | `tuple[str, ...]` | 消息位置引用，格式 `m<index>` |
| `evidence` | `str` | 来源类型：`"tool"`（工具调用成功）或 `"model"`（对话产生） |

### 2.3 生命周期字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `supersedes` | `str \| None` | 被此条取代的老笔记 ID |
| `superseded_by` | `str \| None` | 取代此条的新笔记 ID |
| `hits` | `int` | 被召回次数 |
| `last_used` | `str` | 最后使用时间 |
| `pinned` | `bool` | 是否锁定（防驱逐） |

### 2.4 信任度字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `stake` | `float` | 信任赌注，范围 [0, 10.0]，默认 1.0。每次被召回增加 20%（STAKE_REWARD_RATE），被取代扣减 30%（STAKE_SLASH_RATE） |

### 2.5 派生属性

- **`dedupe_key`**：对标准化后的文本做 SHA-1 前 16 位哈希，用于内容去重
- **`is_active`**：`superseded_by` 为空时为 True，已取代的笔记不参与检索和驱逐

### 2.6 不可变性设计

所有变更方法（`touch()`, `superseded_by_note()`, `with_importance()`, `with_stake()`）都返回新的 `MemoryNote` 实例，通过 `dataclasses.replace()` 实现。修改操作在 `MemoryStore` 中通过锁保护后替换列表中的对象。

---

## 3. 存储层：从单级到双级

### 3.1 MemoryStore（单级磁盘索引）

`MemoryStore` 是 JSON 文件-backed 的内存索引，路径默认为 `.memory`。

**持久化策略：**
- 使用 `.tmp` 临时文件 + `os.replace()` 原子写入
- v1 格式（`entries` 键）自动读取迁移，v2 格式使用 `notes` 键
- 所有 IO 异常被吞没（"memory never raises"），损坏的文件被清空而非崩溃
- 读写通过 `threading.RLock` 保护，但写路径不持锁保存——这是已知并发隐患

**核心操作：**

```
add_note(note)     → 按 dedupe_key 去重，相同内容只更新 importance/pinned
update(note)       → 按 id 精确替换
supersede(old_id, newer)  → 退役旧笔记并保留新笔记（双向链接）
pin(id, bool)      → 设置锁定标志
touch_notes(ids)   → 批量记录命中次数和时间
delete(id)         → 物理删除
evict(cap)         → 价值驱动驱逐
```

**驱逐算法（`_value_of`）：**

```
value = importance + 0.5 * min(hits, 4) + 0.5 * freshness + stake
```

其中 `freshness = 0.5^(age_days / 30)`，半衰期 30 天。

**保护规则：**
- `pinned=True` 的笔记永不驱逐
- `kind=decision` 的笔记优先保留
- 仅当受保护笔记总数超过上限时才 fallback 到纯价值排序

### 3.2 ScopedStore（双级作用域路由）

`ScopedStore` 在单个接口上封装两级存储：

```
ScopedStore(
    project=MemoryStore(".memory"),      # workspace 级
    user=MemoryStore("~/.ai-coding/memory")  # user 级（可选）
)
```

**关键设计原则：**

1. **写入按 scope 路由**：仅 `scope == "user"` 写入 user 层，其余全部留在 workspace
2. **读取合并两层**：`active()` 返回 project + user 的并集，project 优先
3. **去重严格限制在目标 tier**：`find_by_dedupe` 和 `find_similar` 只在即将写入的 tier 内搜索，防止项目知识泄露到用户级
4. **驱逐每层独立**：`evict(cap)` 分别对两层执行容量修剪

**保守路由策略：** 一个拼写错误不能把笔记写入全局偏好文件。只有显式声明 `USER_SCOPE` 才打开用户层。

---

## 4. 检索层：BM25 + 语义混合

### 4.1 RecallParams（可调参数）

```python
threshold=0.25          # 最低综合得分门控
min_relevance=0.25      # 最低相关性质询门控
relative_band=0.55      # 相对最佳分数的比例门控
weight_rank=0.55        # 排名权重
weight_recency=0.20     # 时效权重
weight_importance=0.15  # 重要性权重
half_life_days=14.0     # 时效半衰期
bm25_k1=1.2             # BM25 术语频率参数
bm25_b=0.75             # BM25 长度归一化参数
min_cosine=0.60         # 语义向量最低余弦相似度
recall_fusion="blend"   # 融合方式：blend 或 rrf
```

### 4.2 两阶段检索流程

```
query → _candidates()  → rank_notes_detailed()  → ScoredNote[]
           │                      │
           ▼                      ▼
     预过滤集合          BM25 + 语义混合评分
     (kinds/evidence     三重门控
      scope/scene/
      since_days)
```

**第一阶段：预过滤（`_candidates`）**

在评分之前先过滤候选集，确保 k 预算花在真正符合条件的笔记上。支持五种过滤器：
- `kinds`：按笔记类型过滤
- `evidence`：按来源类型过滤（"tool" 或 "model"）
- `scope`：按作用域过滤
- `scene`：按场景标签精确匹配（需达到 SCENE_SUPPORT=3 的支持数才能成为有效分组）
- `since_days`：时间窗口

**第二阶段：评分排序（`rank_notes_detailed`）**

#### 4.2.1 词法评分：BM25

使用 Okapi BM25 公式计算术语匹配分数，具有：
- 术语频率饱和效应（避免长文档优势）
- 长度归一化（短而精确的笔记优于长而模糊的笔记）

#### 4.2.2 语义评分：EmbeddingIndex

可选的向量层，通过 `EmbeddingIndex` 实现：

- **惰性嵌入**：读时计算，写时不付费
- **磁盘缓存**：按 endpoint + model + dimensions + dedupe_key 哈希键缓存，每条笔记只嵌入一次
- **能力探测**：每个进程只尝试一次 `/embeddings` 端点，失败结果被记住
- **批量处理**：最多每批 10 条笔记发送嵌入请求
- **维度校验**：缓存向量的维度与配置不符时自动重新计算

#### 4.2.3 融合策略

两种融合方式将词法和语义排名合并为一个排名项：

1. **blend**（默认）：min-max 归一化各自列表后取平均
2. **rrf**：倒数排名融合（Reciprocal Rank Fusion），阻尼常数 K=60

#### 4.2.4 三重门控

```
score >= threshold              → 绝对门控
relevance >= min_relevance      → 相关性质询门控
score >= relative_band * best   → 相对最佳分数门控
```

三个门控条件必须同时满足。注意 `min_relevance` 是必要但不充分的——一条完全相关的旧笔记仍可能因时效衰减而被丢弃。

#### 4.2.5 排序键

```python
(-score, -relevance, evidence_rank, _scope_rank, note.id)
```

- 分数越高越靠前
- 相关性越高越靠前
- `tool` 证据优先于 `model`（平局时使用）
- workspace 级优先于 user 级（平局时使用）
- ID 作为最终确定性打破

### 4.3 相关性度量

使用 IDF 加权覆盖率（coverage），而非 BM25 本身：

```
relevance = Σ[idf(term) for term in known ∩ present] / Σ[idf(term) for term in known]
```

只统计语料库中存在的查询术语（避免 CJK 大元噪声稀释真实匹配），且与 BM25 分工明确：BM25 负责排序，coverage 负责门控。

### 4.4 Kind Boost

不同笔记类型获得加分：

| 类型 | 加分 |
|------|------|
| decision | +0.25 |
| preference | +0.15 |
| open_thread | +0.10 |
| fact | +0.0 |

---

## 5. 服务层：MemoryService 三层操作

### 5.1 Recall（回忆）

```python
async def recall_with_diagnostics(query, kinds, evidence, scope, since_days, scene)
```

完整回忆路径，返回 `(ScoredNote[], RecallDiagnostics)`。诊断信息包括：
- `terms_known` / `terms_unknown`：语料库知道/未知的术语
- `near_misses`：最接近但未通过的三条笔记
- `index_summary`：当前索引摘要
- `filtered_out`：被过滤器排除的数量
- `ignored_scene`：因支持数不足被忽略的场景标签
- `timed_out` / `elapsed_seconds`：超时和耗时

命中后自动触发：
1. **Stake 奖励**：`new_stake = hit.stake * (1 + STAKE_REWARD_RATE)`
2. **Touch 记录**：批量更新 hits 和 last_used

### 5.2 Harvest（收割）

```python
async def harvest(messages, turn_index, session_id, summary, evidence, source_offset)
```

在轮次结束时从消息 delta 中提取记忆并整合到索引。

**处理流程：**

```
messages → extractor → candidates → _reconcile() → HarvestReport
                                    │
                          ┌──────────┼──────────┐
                          ▼          ▼          ▼
                       supersede   update     add
                    (明确取代)   (相似合并)  (全新添加)
```

**`_reconcile` 中的四种操作：**

1. **supersede**：明确取代指定 ID 的笔记，同时扣减被取代者的 stake（-30%）
2. **update**：找到相似笔记（similarity ≥ 0.80），合并文本（取较长者）和最高 importance
3. **skip（dedupe）**：`dedupe_key` 已存在则跳过
4. **add**：全新添加

**volatile 过滤**：拒绝包含 "下一步"、"已完成"、"/tmp/"、"pid" 等瞬态内容的笔记。

**提取增强：**
- `_known_memory_hint`：向提取器展示已有笔记，使其能够命名 `target_id` 进行取代
- `_scene_hint`：提供已形成的场景标签，鼓励复用而非创造新标签

### 5.3 Consolidation（整合）

```python
async def consolidate(force=False)
```

定期执行的记忆整理操作：

1. **合并近重复**：同 tier 内、同 kind、同 scene、similarity ≥ 0.80 的笔记配对，保留价值更高者
2. **晋升高频笔记**：`hits >= 3` 的笔记 importance +1（上限 5）
3. **过期开放线程**：`kind=open_thread` 且超过 30 天的笔记被删除
4. **容量驱逐**：超出 `max_index_entries`（默认 200）的笔记按价值驱逐

整合仅在笔记总数 ≥ `consolidate_threshold`（默认 10）时执行，除非 `force=True`。

---

## 6. 约束注入块（Index Block）

`index_block()` 方法生成一个始终附加在请求末尾的小约束块：

```xml
<memory-constraints>
- [decision#a1b2c3d4] 必须使用 poetry 管理依赖
- [fact#e5f6g7h8] 改用 uv 作为包管理器
</memory-constraints>
```

**选择规则：**
- 优先 `pinned` 笔记
- 其次 `decision` 类型
- 再次看起来像约束的 `fact`（包含"必须"、"只能"、"禁止"等关键词）
- 按字符预算（默认 600）截断

**注入位置：** 请求尾部，非系统提示——避免污染指令。

---

## 7. 场景标签系统（Scene Groups）

`scene` 字段是自由文本的分组标签，由提取器模型生成。系统设计为自组织：

- 一个标签需要至少 3 条笔记共享才能获得"治理"资格（`SCENE_SUPPORT = 3`）
- 治理标签按热度（sum of hits）排序，形成 `top_scenes`
- 未达标的标签不被用作过滤器——防止模型猜错标签导致空结果
- 标签增长遵循 Heap's Law：词汇量随笔记数稳定增长，头部保持稳定比例

---

## 8. 关键设计决策总结

| 决策 | 原因 |
|------|------|
| 双层作用域 | 项目记忆不污染用户偏好，用户偏好跨项目可用 |
| 去重限于目标 tier | 防止项目知识意外提升到用户级 |
| 仅 USER_SCOPE 打开用户层 | 保守路由，拼写错误不会写入全局文件 |
| 驱逐按层独立 | 防止某一层的条目挤占另一层空间 |
| BM25 排序 + coverage 门控 | BM25 长度归一化使短文档覆盖率达 1.0，不适合做门控 |
| 三重门控 | 绝对阈值 + 相关性质询 + 相对最佳比例，缺一不可 |
| 不可变 MemoryNote | 并发安全的数据视图，变更返回新实例 |
| 原子写入（tmp + os.replace） | 防止部分写入导致索引损坏 |
| 记忆从不抛出异常 | 降级为空列表，不影响主循环 |
| Stake 奖惩机制 | 频繁召回的记忆更可信，被取代的记忆可信度下降 |
| 惰性嵌入 | 写时不付费，搜索时才计算向量 |
| 约束块注入尾部 | 不污染系统提示，仅提供上下文约束 |
