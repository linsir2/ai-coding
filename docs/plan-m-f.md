# 领域 F · 记忆（M-F1 ~ M-F3）

> 调研：一轮仓库侦察（子智能体）+ 一轮联网（我亲自检索，10+ 来源）+ 仓库内既有实测文档。
> **每条结论都给了出处**；子智能体找不到的项显式标"未找到"。
>
> **前情**：roadmap `M-F1~F3 记忆 | ⬜ 未开始`；仓库里没有 `plan-m-f.md`。本方案从零建立。

---

## 一、调研结论

### 1.1 联网调研（2026 年的四条决定性发现）

**① 架构分三派，且分歧点是"谁决定记什么"**

| 框架 | 记忆是什么 | 检索 | 出处 |
|---|---|---|---|
| **Mem0** | **蒸馏优先**：单遍 ADD-only 抽取，事实是唯一存储单元，冲突检测器防膨胀 | **多信号**：semantic + **keyword** + entity | [Zep vs Mem0](https://mem0.ai/blog/zep-vs-mem0-which-ai-memory-layer-should-you-choose) |
| **Zep / Graphiti** | **历史优先**：原始轮次入库 + 实体抽取，时序知识图谱 | 向量 + 图谱 | [Mem0 vs Zep](https://mem0.ai/library/agent-memory/mem0-vs-zep) |
| **Letta（原 MemGPT）** | **OS 式三层**：core（常驻上下文）/ recall（可搜索历史）/ archival（向量）；**agent 自己编辑** | 工具调用 | [Letta 对比](https://iotdigitaltwinplm.com/mem0-vs-letta-vs-zep-agent-memory-frameworks-comparison-2026/)、[MemGPT 模式](https://colab.research.google.com/github/NirDiamant/Agent_Memory_Techniques/blob/main/all_techniques/26_letta_memgpt_patterns/letta_memgpt_patterns.ipynb) |

> **对本仓库的直接意义**：本仓库是 **Letta 的 archival 层 + Mem0 的多信号检索** 的混合体。
> `scene` 过滤器 ≈ Letta 的 metadata 标签；`memory_write` 工具的抽取 ≈ Mem0 的蒸馏；
> **但没有"谁决定记什么"的可观测性** —— 这正是 M-F2 要补的。

**② "小库规模下向量检索并不更好"—— 有实证支持**

| 来源 | 结论 | 标签 |
|---|---|---|
| [KSE-Web](https://arxiv.org/abs/2608.21365)（arXiv 2608.21365） | 3K 文档 / 300 查询：**BM25 Recall 0.943 > hybrid 0.929 > dense 更低** | [CONFIRMED] |
| [Curse of Dense Low-Dimensional IR](https://arxiv.org/html/2012.14210v1)（arXiv 2012.14210） | dense 性能随索引规模**下降更快**，存在 **tipping point** 让 sparse 反超；与维度数强相关 | [CONFIRMED] |
| [BM25 Wins at Scale](https://www.alphaxiv.org/abs/2607.26497)（arXiv 2607.26497） | 28 层嵌套语料（~450×）：约 **10M corpus tokens 后 BM25 领先**，全量时差距近 20 分 | [CONFIRMED] |

> **本仓库的位置**：语料 78 notes / 124 queries —— **在 tipping point 之下**。
> 所以"paraphrase@3 词面 0/46 → 语义 6/46"说明的是**词面在这一小撮改写问句上全 miss**，
> **不能**推出"语义档整体更好"。`MIN_DANGEROUS`-式的**分档指标**才是诚实的度量（见 M-F1）。

**③ MemoryTax：设计好的记忆对强模型收益有限**（[arXiv 2610.29823](https://arxiv.org/html/2610.29823)）

* 同一 LoCoMo 对话（419 轮 / 152 问）跑 full context / BM25 / Mem0 / Zep × 3 个 agent × 3 个模型；
* **Mem0 只在 GPT-4o-mini 下胜过 full context**，且**构建成本超过整轮 full context**；
* **记忆的天花板由"写它的模型"决定** —— 更强的读者救不回更弱的写者漏掉的东西；
* Claude Opus 5.5 + Pi（保留原始历史 + 自己搜索）在成本-准确率前沿领先（99%，成本不到 full context 一半）。

> **对本仓库的直接意义**：**不要指望语义档单独提升召回质量**。它的价值是
> **在词面全 miss 的那一小撮查询上兜底**（6/46），以及**省 token**。方案必须**分档度量**，
> 而不是只报一个总的 R@3。

**④ 召回预算：buffer 要按 P95 定，不是 P50**

| 来源 | 结论 | 标签 |
|---|---|---|
| [Supermemory: Latency Budgets for Memory Retrieval](https://supermemory.ai/blog/latency-budgets-memory-retrieval) | 语音 AI <100ms、对话 200ms、企业 copilot 400ms；**"Build the buffer around your P95, not your P50"**；两段式检索把延迟压到 40-80ms；索引碎片化静默劣化 40-60% | [CONFIRMED] |
| [GenAI Mindmap: Timeout Budget](https://genai4a11.github.io/concepts/timeout-budget.html) | 按阶段分配并**留 headroom**（retrieval 0.5s / rerank 0.3s / generation 5s…）；**不要单阶段吃满**；超时时**返回部分结果**而非报错 | [MAJORITY] |
| [Agensi: Degraded Result and Fallback Design](https://www.agensi.io/skills/degraded-result-and-fallback-design) | 三级 fallback（缓存 → 静态 → 兜底）；**必须披露降级状态**（`X-Degraded: true`），"zero phantom success" | [CONFIRMED] |
| [Zylos: Graceful Degradation Patterns](https://zylos.ai/research/2026-05-30-graceful-degradation-patterns-ai-agent-systems/) | LLM 系统"概率性失败"（429 / 畸形 JSON / 挂 60-120s）；需要熔断 + fallback 链 + 舱壁隔离 | [MAJORITY] |

> **本仓库的现状（侦察实测）**：**已经符合 ④ 的"披露降级"** ——
> [memory_search_tool.py:181-198](file:///workspace/ai-coding/ai_coding/tools/memory_search_tool.py#L181-L198) 发 `Degraded(component="memory_recall")`，
> `RecallDiagnostics.timed_out` / `elapsed_seconds` 已在。**缺的是"阈值有实测依据"**（M-F3）。

### 1.2 仓库侦察的关键事实（子智能体 + 我复核）

**M-F1 语义档 —— 探测已存在，"默认开"才是缺口**

| 事实 | 位置 |
|---|---|
| `EmbeddingIndex.available()` —— **纯配置层**，无 IO | [embeddings.py:107-114](file:///workspace/ai-coding/ai_coding/memory/embeddings.py#L107-L114) |
| `provider_lacks_embeddings()` —— **每进程恰好探测一次**，404 → 置闩锁 | [embeddings.py:136-153](file:///workspace/ai-coding/ai_coding/memory/embeddings.py#L136-L153) |
| `EmbeddingsConfig.enabled` **默认 `False`** | [config/models.py:567](file:///workspace/ai-coding/ai_coding/config/models.py#L567) |
| `min_cosine` **默认 0.60**，唯一消费点 | [config/models.py:571](file:///workspace/ai-coding/ai_coding/config/models.py#L571) / [relevance.py:518](file:///workspace/ai-coding/ai_coding/memory/relevance.py#L518) |
| 配置示例**整块注释掉**，写明"deepseek 系端点没有 /embeddings（404）" | [config.example.yaml:109-119](file:///workspace/ai-coding/config.example.yaml#L109-L119) |
| **无 provider 抽象**，`urllib_transport` 硬编码 OpenAI 形状 | [embeddings.py:51-79](file:///workspace/ai-coding/ai_coding/memory/embeddings.py#L51-L79) |

> **侦察最重要的发现**：roadmap 说"加端点探测"——**探测已经有了**（404 + 闩锁）。
> 真正的缺口是：① 没有"支持就自动开"的语义；② `min_cosine` **没有标定基准**，
> 换 provider 时无从下手；③ 探测只在**运行时**，配置层看不到"这个端点行不行"。

**M-F2 场景标签 —— "过滤器"已实测，缺"治理"**

| 事实 | 位置 |
|---|---|
| `MemoryNote.scene` —— 自由文本，**不进 `dedupe_key`、不进打分** | [note.py:96](file:///workspace/ai-coding/ai_coding/memory/note.py#L96) |
| 召回时**评分前过滤**（精确匹配） | [service.py:419-423](file:///workspace/ai-coding/ai_coding/memory/service.py#L419-L423) |
| `governed_scenes()` 只保留 **≥3 条**共享的标签（`SCENE_SUPPORT`） | [service.py:350-366](file:///workspace/ai-coding/ai_coding/memory/service.py#L350-L366) |
| **文档已写明"是过滤器不是召回器"** | [features.md:329-333](file:///workspace/ai-coding/docs/features.md#L329-L333) |
| **标签总数无上限**（未找到任何"最多 N 个不同 scene"的限制） | — |
| JSON 碎片防护**已存在**（5 处） | [note.py:244-252](file:///workspace/ai-coding/ai_coding/memory/note.py#L244-L252) 等 |

**M-F3 召回预算 —— 阈值无实测依据**

| 事实 | 位置 |
|---|---|
| `recall_timeout_seconds` **默认 2.0**，唯一消费点 | [config/models.py:612](file:///workspace/ai-coding/ai_coding/config/models.py#L612) / [service.py:551](file:///workspace/ai-coding/ai_coding/memory/service.py#L551) |
| 计时窗口：`_candidates` → `_semantic_scores` → `rank_notes_detailed` → 未命中探针（**事后计量，非抢占**） | [service.py:492-547](file:///workspace/ai-coding/ai_coding/memory/service.py#L492-L547) |
| **`scripts/` 下无任何延迟基准**（grep `elapsed_seconds` 在 `scripts/` 零命中） | — |
| **"~5ms" 无原始出处** —— 仅 5 处文档口头引用；最接近的实测是 [r3-analysis.md:255-256](file:///workspace/ai-coding/docs/r3-analysis.md#L255-L256) 的 BM25-lite `1.31 ms/查询`(n=200) / `37.10 ms/查询`(n=5000) | — |
| 延迟的实际来源：逐条 tokenize（O(N·L)）+ **全库 JSON 同步写盘**（阻塞事件循环） | [service.py:511-546](file:///workspace/ai-coding/ai_coding/memory/service.py#L511-L546)、[store.py:127-144](file:///workspace/ai-coding/ai_coding/memory/store.py#L127-L144) |

### 1.3 顺带发现的两处文档-代码不一致（本轮修）

1. [features.md:475-489](file:///workspace/ai-coding/docs/features.md#L475-L489) **仍在描述 M-C2 修复前的并发写状态**
   （"`_mutate` 在 `_save` 之前就放掉了锁"、"并发写不是安全的"），而 [store.py:175-190](file:///workspace/ai-coding/ai_coding/memory/store.py#L175-L190)
   已实现"`_save` 进锁 + flock + 唯一 tmp"。**文档滞后于代码**。
2. [features.md:442](file:///workspace/ai-coding/docs/features.md#L442) 的 `paraphrase@3 6/12`（旧 52 条语料）与
   [features.md:503](file:///workspace/ai-coding/docs/features.md#L503) 的 `0/46→6/46`（新 124 条语料）并存，未标注语料版本。

---

## 二、方案：逐项实现

### A. M-F1 · 语义档可用化

**范围**：① 让"端点支持"成为**可声明的配置语义**；② 给 `min_cosine` 一条**可复跑的标定路径**；③ 分档度量。

#### A.1 `enabled` 增加 `"auto"` 档（不改变默认）

```python
# config/models.py
class EmbeddingsConfig(BaseModel):
    enabled: bool | Literal["auto"] = False     # 默认不变
```

* `False`（默认）/ `True` —— 与今天完全一致；
* `"auto"` —— **运行时探测**：`provider_lacks_embeddings()` 为假则启用，为真则本进程永久禁用。

> **为什么不是"默认开"**：默认开意味着**每次新工作区的商务信息咨询都打一次网络**，
> 而 404 探测本身就是一次网络调用。`auto` 让**想要的人显式要**，默认仍然离线。
> 这也是 OWASP "excessive functionality" 的直接应用：能力默认不启用。

`cli._embeddings_for` 透传该语义；`EmbeddingIndex.available()` 保持纯配置判定（无 IO），
`auto` 的解析放在**首次召回时**（已有 `provider_lacks_embeddings` 的位置）。

#### A.2 `min_cosine` 的标定基准（新脚本）

`scripts/calibrate_memory_threshold.py`：

* 输入：`retrieval_corpus.json`（78 notes / 124 queries）+ 一个 transport（真实端点或注入）；
* 输出：**FP/FN 随阈值变化的曲线** + **推荐阈值**（在"词面已命中的查询不退化"的约束下取最大的 FN 改善）；
* 复用 `retrieval_cases.py` 的语料加载与 `MemoryService`（不新写召回逻辑）。

**判据（关键）**：以**词面 ranker 已命中的查询**为"不该退化"集合，
以**词面 miss 的 paraphrase 查询**为"可改善"集合。阈值在两者之间取，
而不是在全体查询上取平均 —— 后者会被"词面本来就够用"的查询稀释。

#### A.3 分档度量写进 `retrieval_cases.py`

在现有 `paraphrase@3` 之外，分档报：

| 档 | 定义 | 现状（实测） |
|---|---|---|
| `lexical_hit` | 词面 ranker 已命中的查询 | 78/124 |
| `paraphrase_recovered` | 词面 miss、语义命中的 | **6/46** |
| `paraphrase_lost` | 词面 miss、语义也 miss 的 | 40/46 |
| `lexical_regression` | 词面命中、加语义后**丢掉**的 | 门禁 **0** |

> **为什么分档**：MemoryTax 的结论（③）说明**总的 R@3 会被"词面本来就够用"的查询稀释**。
> 分档之后，"语义档的价值"就是 `paraphrase_recovered`，而"代价"就是 `lexical_regression` —— 两个数都看得见。

### B. M-F2 · 场景标签治理

**范围**：① 标签**上限**与**合并**策略；② 文档回写（实测结论）；③ 顺带修 §1.3 的文档滞后。

#### B.1 标签上限（`SCENE_MAX_DISTINCT`）

现状：`scene` 是开放词表（设计理由见 [service.py:174-183](file:///workspace/ai-coding/ai_coding/memory/service.py#L174-L183)，
引 Heaps 定律），**但没有任何总数上限**。

**策略**：**不设"不同标签总数"上限，设"单条 note 的 scene 数"与"导航展示数"上限**。

* `MemoryNote.scene` 是**单值**（`str`），所以"单条 note 的标签数"天然是 1；
* 真正无界的是**全局不同标签数** —— 而它的消费者只有两个：`governed_scenes()`（≥3 才可见）与
  `index_summary` 的 `top_scenes`（`TOP_SCENES = 5`，[service.py:187](file:///workspace/ai-coding/ai_coding/memory/service.py#L187)）。
* 两者都**已经有界**（≥3 过滤 / top-5 截断）。

> **结论（评审时确认）**：**不需要加全局上限** —— 加了是过度设计。
> 缺的是**"标签合并"**：当两个标签语义相同（`auth` / `authentication`）时，它们**各占一个 scene**，
> 于是**都不满足 ≥3**，于是**都不进导航** —— 一个真实的治理缺口。

#### B.2 标签合并（`normalize_scene` 的同义折叠）

在 `normalize_scene`（[note.py:46-56](file:///workspace/ai-coding/ai_coding/memory/note.py#L46-L56)）里加**同义折叠表**：

```python
_SCENE_SYNONYMS = {
    "authentication": "auth",
    "authorization": "auth",      # 或分开，见拍板项
    "config": "configuration",
    "configs": "configuration",
    "db": "database",
    "tests": "test",
    "testing": "test",
    ...
}
```

* **只折叠拼写变体，不做语义聚类**（语义聚类需要 embeddings，是 M-F1 的事，且会引入不可解释的合并）；
* 折叠表**显式、可审计、可单测** —— 这是"最小可用"的治理，不是一套 NLP。

#### B.3 文档回写

* 在 `features.md` 的召回契约处补：**标签是过滤器**（已有，[features.md:329-333](file:///workspace/ai-coding/docs/features.md#L329-L333)）、
  **同义折叠表的存在与位置**、**为什么不做全局上限**；
* 修 §1.3 的两处文档滞后（并发写状态、paraphrase 语料版本标注）。

### C. M-F3 · 召回预算实测

**范围**：建**召回延迟基准**，让 `recall_timeout_seconds` 有实测依据。

#### C.1 基准脚本 `scripts/memory_latency_cases.py`

分档测（**全部离线、无网络、无真实端点**）：

| 档 | 变量 | 判据 |
|---|---|---|
| **库规模** | notes 数：100 / 1_000 / 10_000 | 报告每档的 p50 / p95 召回延迟 |
| **是否语义** | 词面 only vs 语义（注入假 transport） | 语义档的额外延迟 |
| **是否命中** | 命中 vs 未命中（未命中会**再跑一遍全库**，见 [service.py:511-546](file:///workspace/ai-coding/ai_coding/memory/service.py#L511-L546)） | 未命中路径的额外成本 |
| **是否写盘** | `touch_notes` 开/关 | **全库 JSON 写盘**的成本（这是最大的同步阻塞） |

* 输出：每档的 `p50` / `p95`（`time.perf_counter()`，`statistics.quantiles`）；
* 门禁：**不设硬阈值**（硬阈值会让基准变成"性能回归测试"，那是另一件事）；
  输出一张**表**，并在文档里写明"**`recall_timeout_seconds=2.0` 相对实测的哪个分位**"。

> **为什么不直接给阈值**：Supermemory 的结论是"buffer 要按 P95 定，不是 P50"——
> 而"阈值该是多少"取决于**部署环境**（本地 SSD vs 网络盘、语义档开/关）。
> 基准给**分档数据**，阈值由操作者按自己的环境定。这与 M-A6 的教训一致（**比率会说好，不变量会说实话**）。

#### C.2 顺带：把 `retrieval_cases.py` 接进 CI（M-G1 的一部分）

侦察发现：`retrieval_cases.py` 的 docstring 说"`--check` is what CI should call"
（[retrieval_cases.py:9-10](file:///workspace/ai-coding/scripts/retrieval_cases.py#L9-L10)），但 **`ci.yml` 从未调用它**（grep 零命中）。

* 本轮**只接词面档**（`--check` 的词面部分，离线、无网络）；
* **不接语义档**（需真实端点，属 M-G3 的 live 层）。

---

## 三、逐文件改动清单

| 文件 | 改动 | 属于 |
|---|---|---|
| `ai_coding/config/models.py` | `EmbeddingsConfig.enabled: bool \| Literal["auto"]` | A.1 |
| `ai_coding/memory/embeddings.py` | `auto` 的解析（复用 `provider_lacks_embeddings`）；`available()` 保持纯配置 | A.1 |
| `ai_coding/cli.py` | `_embeddings_for` 透传 `auto` | A.1 |
| `scripts/calibrate_memory_threshold.py` | **新**：阈值标定基准（FP/FN 曲线 + 推荐阈值） | A.2 |
| `scripts/retrieval_cases.py` | 分档度量（lexical_hit / paraphrase_recovered / lexical_regression）；词面档进 CI | A.3 / C.2 |
| `.github/workflows/ci.yml` | 加 `python scripts/retrieval_cases.py --check`（词面档） | C.2 |
| `ai_coding/memory/note.py` | `normalize_scene` 加同义折叠表 `_SCENE_SYNONYMS` | B.2 |
| `ai_coding/memory/service.py` | 抽出 `_scene_synonym()` 供 `normalize_scene` 复用（不写两份） | B.2 |
| `docs/features.md` | 标签契约回写 + 两处文档滞后修正 | B.3 |
| `scripts/memory_latency_cases.py` | **新**：召回延迟分档基准 | C.1 |
| `docs/roadmap-milestones.md` | M-F1~F3 标完成 | 文档 |

---

## 四、RIPR 用例

### Red（先写，必须变红）

| # | 用例 | 断言 |
|---|---|---|
| U1 | `EmbeddingsConfig(enabled="auto")` | 合法；`enabled=True/False` 仍合法 |
| U2 | `enabled="auto"` + 探测为"支持" | 语义档启用（召回走 semantic） |
| U3 | `enabled="auto"` + 探测为"不支持"（404） | 语义档禁用，且**本进程只探测一次** |
| U4 | 标定脚本在注入 transport 上跑 | 输出 FP/FN 曲线 + 推荐阈值；**词面已命中的查询不退化** |
| U5 | `retrieval_cases.py` 分档 | `lexical_regression == 0`；`paraphrase_recovered` 与冻结值一致 |
| U6 | `normalize_scene("authentication")` / `("configs")` / `("db")` | 折叠成 `auth` / `configuration` / `database` |
| U7 | 同义折叠后，原本各 2 条的两个标签合并成 1 个 4 条的 | **进入 `governed_scenes()`**（原本两个都不满足 ≥3） |
| U8 | 延迟基准在 100 / 1_000 / 10_000 notes 三档 | 输出 p50/p95 表；**未命中档的 p95 显著高于命中档**（钉住双倍成本） |
| U9 | `retrieval_cases.py --check` 在 CI 里跑 | 退出码 0（词面档） |

### Prove

* **U3-P**：真实端点（或注入的 404 transport）——本进程只发一次 `/embeddings` 请求（计数断言）。
* **U7-P**：合并前的两个标签各自 2 条 → 合并后 4 条 → `governed_scenes()` 含它。
* **突变测试**：① `auto` 的探测删掉（永远启用）→ U3 变红；② `_SCENE_SYNONYMS` 清空 → U6/U7 变红；
  ③ 未命中探针删掉 → U8 的"未命中更慢"变红；④ 分档的 `lexical_regression` 改成恒 0 → U5 变红。

### Refactor

* 同义折叠只在 `normalize_scene` 一处；`service.py` 不复制。
* 标定脚本复用 `retrieval_cases.py` 的语料加载，不新写。

---

## 五、验收判据

* **M-F1**：`enabled="auto"` 可用；探测**每进程一次**；标定脚本可复跑并给出**分档**指标；
  `retrieval_cases.py` 的 `lexical_regression == 0`（加语义档不让词面命中的查询退化）。
* **M-F2**：同义折叠生效且**可单测**；`governed_scenes()` 的 ≥3 语义不变；
  文档写明"过滤器不是召回器"与"为什么不做全局上限"。
* **M-F3**：延迟基准给出 4 档 × 3 规模的 p50/p95 表；文档写明 `recall_timeout_seconds=2.0`
  相对实测**哪个分位**；`retrieval_cases.py` 进 CI（词面档）。
* 3.10 与 3.13 全量绿；真实 CI 双 job 绿。

---

## 六、不做的事

* **不做"默认开语义档"** —— 默认开等于默认触网；`auto` 让想要的人显式要。
* **不做标签语义聚类** —— 需要 embeddings，会引入不可解释的合并；只做**显式同义表**。
* **不设标签全局上限** —— 两个消费者（`governed_scenes` ≥3 / `top_scenes` 5）都已有界。
* **不给召回延迟设硬阈值** —— 阈值依赖部署环境；基准给分档数据，操作者按环境定（M-A6 的教训）。
* **不接语义档进 CI** —— 需真实端点，属 M-G3 的 live 层。
* **不做 provider 抽象类** —— 本轮只需要 `auto` 语义与标定路径；抽象类是另一个量级的改动。

---

## 七、评审记录

（待补）

---

## 八、调研来源

* Mem0 vs Zep（架构、多信号检索）—— https://mem0.ai/blog/zep-vs-mem0-which-ai-memory-layer-should-you-choose
* Mem0 vs Zep（生产对比）—— https://mem0.ai/library/agent-memory/mem0-vs-zep
* Mem0 / Letta / Zep / Cognee 四种下注 —— https://menuagentic.com/zh/blogs/mem0-vs-letta-vs-zep-vs-cognee/
* Mem0 vs Letta vs Zep（含基准口径分歧）—— https://iotdigitaltwinplm.com/mem0-vs-letta-vs-zep-agent-memory-frameworks-comparison-2026/
* Letta（MemGPT）三层记忆 —— https://aiwiki.ai/wiki/letta 、https://colab.research.google.com/github/NirDiamant/Agent_Memory_Techniques/blob/main/all_techniques/26_letta_memgpt_patterns/letta_memgpt_patterns.ipynb
* MemoryTax（记忆对强模型收益有限）—— https://arxiv.org/html/2610.29823
* KSE-Web（小库 BM25 胜过 dense）—— https://arxiv.org/abs/2608.21365
* Curse of Dense Low-Dimensional IR（dense 在大索引下反超）—— https://arxiv.org/html/2012.14210v1
* BM25 Wins at Scale（10M tokens 后 BM25 领先）—— https://www.alphaxiv.org/abs/2607.26497
* Supermemory：召回延迟预算（按 P95 定 buffer）—— https://supermemory.ai/blog/latency-budgets-memory-retrieval
* GenAI Mindmap：Timeout Budget（分阶段 + 部分结果）—— https://genai4a11.github.io/concepts/timeout-budget.html
* Agensi：降级与 fallback 设计（必须披露）—— https://www.agensi.io/skills/degraded-result-and-fallback-design
* Zylos：AI agent 系统的优雅降级模式 —— https://zylos.ai/research/2026-05-30-graceful-degradation-patterns-ai-agent-systems/
* AI Agent Memory Architecture Guide 2026（含延迟 10-50ms 量级）—— https://baeseokjae.github.io/posts/agent-memory-architecture-guide-2026/
