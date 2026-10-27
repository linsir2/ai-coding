# 领域 F · 记忆（M-F1 ~ M-F3）

> 调研：一轮仓库侦察（子智能体）+ 一轮联网（我亲自检索，10+ 来源）+ 仓库内既有实测文档。
> **每条结论都给了出处**；子智能体找不到的项显式标"未找到"。
> **两轮方案评审**（第 1 轮 3 P0 + 10 P1，第 2 轮 3 P0 + 11 P1）全部收口，见 §七。

---

## 一、调研结论

### 1.1 联网调研（2026 年的四条决定性发现）

**① 架构分三派，分歧点是"谁决定记什么"**

| 框架 | 记忆是什么 | 检索 | 出处 |
|---|---|---|---|
| **Mem0** | **蒸馏优先**：单遍 ADD-only 抽取，事实是唯一存储单元，冲突检测器防膨胀 | **多信号**：semantic + **keyword** + entity | [Zep vs Mem0](https://mem0.ai/blog/zep-vs-mem0-which-ai-memory-layer-should-you-choose) |
| **Zep / Graphiti** | **历史优先**：原始轮次入库 + 实体抽取，时序知识图谱 | 向量 + 图谱 | [Mem0 vs Zep](https://mem0.ai/library/agent-memory/mem0-vs-zep) |
| **Letta（原 MemGPT）** | **OS 式三层**：core（常驻上下文）/ recall（可搜索历史）/ archival（向量）；**agent 自己编辑** | 工具调用 | [Letta 对比](https://iotdigitaltwinplm.com/mem0-vs-letta-vs-zep-agent-memory-frameworks-comparison-2026/)、[Letta 三层](https://aiwiki.ai/wiki/letta) |

> **对本仓库的直接意义**：本仓库是 **Letta 的 archival 层 + Mem0 的多信号检索** 的混合体。
> `scene` 过滤器 ≈ Letta 的 metadata 标签；`memory_write` 的抽取 ≈ Mem0 的蒸馏。
> **但没有"谁决定记什么"的可观测性** —— 这正是 M-F2 要补的。

**② "小库规模下向量检索并不更好"—— 有实证支持**

| 来源 | 结论 | 标签 |
|---|---|---|
| [KSE-Web](https://arxiv.org/abs/2608.21365)（arXiv 2608.21365） | 3K 文档 / 300 查询：**BM25 Recall 0.943 > hybrid 0.929 > dense 更低** | [CONFIRMED] |
| [Curse of Dense Low-Dimensional IR](https://arxiv.org/html/2012.14210v1)（arXiv 2012.14210） | dense 性能随索引规模**下降更快**，存在 **tipping point** 让 sparse 反超；与维度数强相关 | [CONFIRMED] |
| [BM25 Wins at Scale](https://www.alphaxiv.org/abs/2607.26497)（arXiv 2607.26497） | 28 层嵌套语料（~450×）：约 **10M corpus tokens 后 BM25 领先**，全量时差距近 20 分 | [CONFIRMED] |

> **本仓库的位置**：语料 78 notes / 124 queries —— **在 tipping point 之下**。
> 所以"paraphrase@3 词面 0/46 → 语义 6/46"说明的是**词面在这一小撮改写问句上全 miss**，
> **不能**推出"语义档整体更好"。**分档指标**才是诚实的度量（见 M-F1）。

**③ MemoryTax：设计好的记忆对强模型收益有限**（[arXiv 2610.29823](https://arxiv.org/html/2610.29823)）

* 同一 LoCoMo 对话（419 轮 / 152 问）跑 full context / BM25 / Mem0 / Zep × 3 个 agent × 3 个模型；
* **Mem0 只在 GPT-4o-mini 下胜过 full context**，且**构建成本超过整轮 full context**；
* **记忆的天花板由"写它的模型"决定** —— 更强的读者救不回更弱的写者漏掉的东西。

> **对本仓库的直接意义**：**不要指望语义档单独提升召回质量**。它的价值是
> **在词面全 miss 的那一小撮查询上兜底**（6/46），以及**省 token**。方案必须**分档度量**。

**④ 召回预算：buffer 要按 P95 定，不是 P50**

| 来源 | 结论 | 标签 |
|---|---|---|
| [Supermemory: Latency Budgets for Memory Retrieval](https://supermemory.ai/blog/latency-budgets-memory-retrieval) | 语音 AI <100ms、对话 200ms、企业 copilot 400ms；**"Build the buffer around your P95, not your P50"**；索引碎片化静默劣化 40-60% | [CONFIRMED] |
| [GenAI Mindmap: Timeout Budget](https://genai4a11.github.io/concepts/timeout-budget.html) | 按阶段分配并**留 headroom**；**不要单阶段吃满**；超时时**返回部分结果**而非报错 | [MAJORITY] |
| [Agensi: Degraded Result and Fallback Design](https://www.agensi.io/skills/degraded-result-and-fallback-design) | 三级 fallback；**必须披露降级状态**（`X-Degraded: true`），"zero phantom success" | [CONFIRMED] |
| [Zylos: Graceful Degradation Patterns](https://zylos.ai/research/2026-05-30-graceful-degradation-patterns-ai-agent-systems/) | LLM 系统"概率性失败"（429 / 畸形 JSON / 挂 60-120s）；需要熔断 + fallback 链 + 舱壁隔离 | [MAJORITY] |

> **本仓库的现状（侦察实测）**：**已经符合 ④ 的"披露降级"** —— `Degraded(component="memory_recall")` +
> `RecallDiagnostics.timed_out` / `elapsed_seconds`。**缺的是"阈值有实测依据"**（M-F3）。

### 1.2 仓库侦察的关键事实（子智能体 + 我逐条复核）

**M-F1 语义档 —— 探测已存在，"可观测"才是缺口**

| 事实 | 位置 | 核实 |
|---|---|---|
| `EmbeddingIndex.available()` —— **纯配置层**，无 IO | [embeddings.py:107-114](file:///workspace/ai-coding/ai_coding/memory/embeddings.py#L107-L114) | ✅ |
| `provider_lacks_embeddings()` —— **每进程恰好探测一次**，404 → 置闩锁 | [embeddings.py:136-153](file:///workspace/ai-coding/ai_coding/memory/embeddings.py#L136-L153) | ✅ |
| `EmbeddingsConfig.enabled: bool` **默认 `False`** | [config/models.py:567](file:///workspace/ai-coding/ai_coding/config/models.py#L567) | ✅ |
| `min_cosine` **默认 0.60**，唯一消费点 | [config/models.py:571](file:///workspace/ai-coding/ai_coding/config/models.py#L571) / [relevance.py:518](file:///workspace/ai-coding/ai_coding/memory/relevance.py#L518) | ✅ |
| **"关 / 未配 / 端点不支持"三种情况全部静默** | [service.py:443-450](file:///workspace/ai-coding/ai_coding/memory/service.py#L443-L450) | ✅ **（我复核确认）** |
| **"支持则默认开"（roadmap M-F1 后半句）有意不做** | — | ✅ 见 §六 |
| `bool("auto")==True` —— 但 **`enabled: bool` 经 pydantic 校验，`"auto"` 加载期即 `ValidationError`** | [config/models.py:567](file:///workspace/ai-coding/ai_coding/config/models.py#L567) | ✅ **（评审指出，我复核确认）** |
| **无 provider 抽象**，`urllib_transport` 硬编码 OpenAI 形状 | [embeddings.py:51-79](file:///workspace/ai-coding/ai_coding/memory/embeddings.py#L51-L79) | ✅ |

> **侦察最重要的发现**：roadmap 说"加端点探测"——**探测已经有了**（404 + 闩锁）。
> 真正的缺口是：**操作者配了 `enabled=True` 而端点不支持时，拿不到任何信号**。

**M-F2 场景标签 —— "过滤器"已实测，缺"折叠"与"治理理由"**

| 事实 | 位置 | 核实 |
|---|---|---|
| `MemoryNote.scene` —— 自由文本，**不进 `dedupe_key`、不进打分** | [note.py:96](file:///workspace/ai-coding/ai_coding/memory/note.py#L96) | ✅ |
| 召回时**评分前过滤**（精确匹配） | [service.py:419-423](file:///workspace/ai-coding/ai_coding/memory/service.py#L419-L423) | ✅ |
| **`governed_scenes()` 闸门在 `_candidates` 之前**（原始 `scene` 不在组里就丢弃） | [service.py:480-488](file:///workspace/ai-coding/ai_coding/memory/service.py#L480-L488) | ✅ **（评审指出，我复核确认）** |
| `governed_scenes()` 只保留 **≥3 条**共享的标签 | [service.py:350-366](file:///workspace/ai-coding/ai_coding/memory/service.py#L350-L366) | ✅ |
| **写路径只走 `normalize_scene`、不折叠**（新旧拼写并存） | [service.py:780](file:///workspace/ai-coding/ai_coding/memory/service.py#L780)、[service.py:859-869](file:///workspace/ai-coding/ai_coding/memory/service.py#L859-L869) | ✅ **（评审指出，我复核确认）** |
| 文档**已写明"是过滤器不是召回器"** | [features.md:329-333](file:///workspace/ai-coding/docs/features.md#L329-L333) | ✅ |
| **`by_scene` 无界且进模型上下文** | [service.py:319-324](file:///workspace/ai-coding/ai_coding/memory/service.py#L319-L324) / [memory_search_tool.py:228-229](file:///workspace/ai-coding/ai_coding/tools/memory_search_tool.py#L228-L229) | ✅ |
| **真正的上界是 `max_index_entries=200`** | [config/models.py:583](file:///workspace/ai-coding/ai_coding/config/models.py#L583) | ✅ |
| **契约明写 `normalize_scene` 不变**；折叠闸门在**读时与过滤时**；一次性归并在 `migrate_scenes.py` | [data-contracts.md:868-869](file:///workspace/ai-coding/docs/data-contracts.md#L868-L869) | ✅ |
| 折叠词表**已存在一份**（`SPELLING_MERGES`，**全中文**） | [migrate_scenes.py:44-76](file:///workspace/ai-coding/scripts/migrate_scenes.py#L44-L76) | ✅ |
| `from_dict` 会调 `normalize_scene` | [note.py:223](file:///workspace/ai-coding/ai_coding/memory/note.py#L223)、[store.py:85](file:///workspace/ai-coding/ai_coding/memory/store.py#L85) | ✅ |

**M-F3 召回预算 —— 阈值无实测依据，且成本模型比方案初版写的更贵**

| 事实 | 位置 | 核实 |
|---|---|---|
| `recall_timeout_seconds` **默认 2.0**，唯一消费点 | [config/models.py:612](file:///workspace/ai-coding/ai_coding/config/models.py#L612) / [service.py:551](file:///workspace/ai-coding/ai_coding/memory/service.py#L551) | ✅ |
| **命中路径才写盘**：每条 hit 一次 `update_stake` + 一次 `touch_notes` = **`1 + len(hits)` 次整文件写** | [service.py:555-562](file:///workspace/ai-coding/ai_coding/memory/service.py#L555-L562) | ✅ |
| **`elapsed` 在写盘与 `index_summary` 之前算完** —— 两者都不在 `elapsed_seconds` 里 | [service.py:547](file:///workspace/ai-coding/ai_coding/memory/service.py#L547) vs [555-567](file:///workspace/ai-coding/ai_coding/memory/service.py#L555-L567) | ✅ |
| **"过滤未命中"才跑探针**（普通未命中不跑） | [service.py:510-546](file:///workspace/ai-coding/ai_coding/memory/service.py#L510-L546) | ✅ |
| `scripts/` 下**无任何延迟基准**；"~5ms" **无原始出处** | — | ✅ |
| 延迟的实际来源：逐条 tokenize（O(N·L)）+ **整文件 JSON 同步写盘** | [relevance.py:363-376](file:///workspace/ai-coding/ai_coding/memory/relevance.py#L363-L376)、[store.py:127-144](file:///workspace/ai-coding/ai_coding/memory/store.py#L127-L144) | ✅ |
| **`add_note` 造库是 O(N²)** | [store.py:197-212](file:///workspace/ai-coding/ai_coding/memory/store.py#L197-L212) | ✅ |
| **`MemoryStore(path)` 直接吃写好的 JSON；load 不触发 evict** | [store.py:65-88](file:///workspace/ai-coding/ai_coding/memory/store.py#L65-L88)、[store.py:120-123](file:///workspace/ai-coding/ai_coding/memory/store.py#L120-L123) | ✅ |
| `retrieval_cases.py` 的**词面档确实离线**；但**词面门禁已由 pytest 覆盖** | [retrieval_cases.py:78-80](file:///workspace/ai-coding/scripts/retrieval_cases.py#L78-L80) / [test_retrieval_benchmark.py:221-234](file:///workspace/ai-coding/tests/test_retrieval_benchmark.py#L221-L234) | ✅ |
| `tests/test_retrieval_benchmark.py` **钉死三条字面输出行** | [test_retrieval_benchmark.py:227-234](file:///workspace/ai-coding/tests/test_retrieval_benchmark.py#L227-L234) | ✅ **（评审指出，我复核确认）** |
| 语料各类计数：`lexical 55 / identifier 9 / multi 4 / paraphrase 46 / noanswer 9 / trap 1` | `scripts/retrieval_corpus.json` | ✅ **（评审指出 78 是 notes 数，我复核确认）** |

### 1.3 本轮修的文档滞后 —— **只修与 F 直接相关的那一处**（评审 P1）

| 项 | 位置 | 债主 | 本轮？ |
|---|---|---|---|
| ① 并发写旧状态（"`_mutate` 在 `_save` 前放锁"、"并发写不是安全的"） | [features.md:475-489](file:///workspace/ai-coding/docs/features.md#L475-L489) | **M-C2**（该方案只改了 `store.py`，未回写文档） | ❌ **移交 M-C2 收尾** |
| ② 整列门禁过时（`R@3 ≥0.80` / `FP@5 ≤0.36` / `paraphrase@3 ≥6/12`） | [features.md:439-443](file:///workspace/ai-coding/docs/features.md#L439-L443) | **R10 检索基准线**（roadmap 已把 CI 化归 M-G1） | ❌ **移交 M-G1** |
| ③ **"命中只多一次整文件写"三处写错**（实际 `1 + len(hits)`） | [service.py:23-25](file:///workspace/ai-coding/ai_coding/memory/service.py#L23-L25)、[features.md:410-411](file:///workspace/ai-coding/docs/features.md#L410-L411)、[features.md:471-473](file:///workspace/ai-coding/docs/features.md#L471-L473) | **本轮新发现，且是 M-F3 的事实基础** | ✅ **本轮修** |

> 三处全塞进 F 会让"F 完成"不可验收。**只做 ③。**

---

## 二、方案：逐项实现

### A. M-F1 · 语义档可用化

**范围**：① "配了但不工作"**可观测**；② `min_cosine` 的**标定路径**；③ **分档度量**。

#### A.1 "配了但不工作"可观测（新行为）

`_semantic_scores` 现在把三种情况**合并成静默**（[service.py:443-450](file:///workspace/ai-coding/ai_coding/memory/service.py#L443-L450)）。
拆分"端点不支持"这一种，**并改返回签名**：

```python
# memory/service.py
async def _semantic_scores(self, query, candidates) -> tuple[dict[str, float] | None, bool, str]:
    index = self._embeddings
    if index is None or not index.available() or not candidates:
        return None, False, "off"
    try:
        if await index.provider_lacks_embeddings():
            return None, False, "endpoint_unsupported"   # 配了但端点不行 ← 新
        return await index.cosine_scores(query, candidates), False, "on"
    except EmbeddingsUnsupportedError:
        return None, False, "endpoint_unsupported"
    except Exception:                                     # noqa: BLE001 — recall is best-effort
        return None, True, "error"                        # 配了、能用、这次失败
```

**调用点必须同步改**（[service.py:501](file:///workspace/ai-coding/ai_coding/memory/service.py#L501)）：

```python
semantic, unavailable, semantic_status = await self._semantic_scores(query, candidates)
```

`RecallDiagnostics` 增加 `semantic_status: str = "off"`（**默认 `"off"`**，三条早退路径因此天然正确）。

工具侧渲染（**必须 `getattr`** —— `_EmptyDiagnostics` 没有该字段）：

```python
semantic_status = getattr(detailed, "semantic_status", "off")
```

> **为什么这是真缺口**：`enabled=True` 的操作者**不知道**自己的配置没生效 —— 代码在运行期沉默。
> 可观测是唯一能让"配了但不工作"被发现的机制。

#### A.2 `min_cosine` 的标定基准（新脚本）

`scripts/calibrate_memory_threshold.py`：

* 输入：`retrieval_corpus.json` + 一个 transport（真实端点**或注入**）；
* **复用** `retrieval_cases.py` 的语料加载与 `_run_semantic_table` 的服务构造（**不新写召回逻辑**）；
* 输出：**FP/FN 随阈值变化的曲线** + **推荐阈值**。

**判据（关键）**：

| 集合 | 定义 | 角色 |
|---|---|---|
| `lexical_hit` | 词面 ranker **已命中**的查询（**可达子集 68/124**） | **不该退化**集合 —— 约束召回 |
| `paraphrase` | 词面 top3 **miss** 的查询（46 条） | **可改善**集合 —— 度量收益 |
| **无 `relevant` 的条目** | `noanswer` 9 条 + `trap` 1 条 | **必须排除**（按 `if case.relevant` 过滤，不硬编码类名） |

* 推荐阈值 = 在 **`lexical_hit` 不退化**（`lexical_regression == 0`）且 **无 `relevant` 条目的 FP 不上升**
  的约束下，`paraphrase` 命中数最大的那个；
* **同时报 FP@5**（正例 min 0.44 / 负例 max 0.66，[features.md:504](file:///workspace/ai-coding/docs/features.md#L504)
  已写明开语义后 FP@5 从 0.46 升到 0.48）—— 阈值选择**必然有 FP 代价**，必须一起报。

> **有效域（必须显式声明）**：标出来的阈值是 **`f(语料, embedding 模型)`** 的函数。
> `config/models.py:562` 的 docstring 已经写了"换模型必须重标"；脚本输出里也要打印这一句。

#### A.3 分档度量（**口径统一**：注入 transport 即可离线算）

| 档 | 定义 | 怎么算 |
|---|---|---|
| `lexical_hit` | 词面 ranker 已命中的查询 | 纯离线 |
| `paraphrase_missed` | 词面 top3 miss 的查询 | 纯离线 |
| `paraphrase_recovered` | 词面 miss、语义命中的 | **注入 transport 即可离线算** |
| `lexical_regression` | 词面命中、加语义后**丢掉**的 | **注入 transport 即可离线算** |

> 初版说后两档"需 live 端点"是**错的**：`EmbeddingIndex(..., transport=...)` 已支持注入
> （[embeddings.py:85-92](file:///workspace/ai-coding/ai_coding/memory/embeddings.py#L85-L92)），测试里就这么用。
> **但**：假 transport 上的分档只能验证**管线**，**不能当作质量证据** —— 所以质量分档仍走 live。

* `retrieval_cases.py` 的默认路径（离线、**无 transport**）只报前两档；
* 后两档走 `--with-embeddings`（live）；
* **必须保留 `test_retrieval_benchmark.py` 钉死的三条字面行**：
  `"reachable (lexical"`、`"paraphrase@3="`、`"no-answer correctly refused"`（[:227-234](file:///workspace/ai-coding/tests/test_retrieval_benchmark.py#L227-L234)）。

### B. M-F2 · 场景标签治理

**范围**：① 折叠（读时 + 过滤时 + **写时**，**不动 `normalize_scene`**）；② 治理理由写进文档；③ 修 §1.3 ③。

#### B.1 "不设全局上限"的**理由要改写**（评审 P1）

初版说"两个消费者都已有界"—— **错**。真实情况：

| 消费者 | 位置 | 有界？ |
|---|---|---|
| `governed_scenes()` | [service.py:350-366](file:///workspace/ai-coding/ai_coding/memory/service.py#L350-L366) | 有界（≥3） |
| `index_summary().top_scenes` | [service.py:334-337](file:///workspace/ai-coding/ai_coding/memory/service.py#L334-L337) | 有界（5） |
| **`index_summary().by_scene`** | [service.py:319-324](file:///workspace/ai-coding/ai_coding/memory/service.py#L319-L324) | **无界**，且**未命中时原样发给模型**（[memory_search_tool.py:228-229](file:///workspace/ai-coding/ai_coding/tools/memory_search_tool.py#L228-L229)） |
| `status()["by_scene"]` | [service.py:997-1008](file:///workspace/ai-coding/ai_coding/memory/service.py#L997-L1008) | 无界（`/memory` 渲染） |

**真正的上界**是 `max_index_entries=200`（[config/models.py:583](file:///workspace/ai-coding/ai_coding/config/models.py#L583)），
标签数 ≤ 笔记数 ≤ 200。**结论（不设全局上限）在默认配置下站得住**，但：

* 理由必须写成"`by_scene` 受 `max_index_entries` 约束"，不是"两个消费者都有界"；
* **调大 `max_index_entries` 时 `by_scene` 会线性膨胀并直接进入模型上下文** ——
  M-F3 的 1_000/10_000 压力档正是这样，**两者必须一起评估**。

#### B.2 折叠：**四处**应用点，一份词表（评审 2 个 P0）

**契约约束**（[data-contracts.md:868-869](file:///workspace/ai-coding/docs/data-contracts.md#L868-L869)）：
`normalize_scene` **不变**；折叠闸门在**读时与过滤时**；一次性归并是 `migrate_scenes.py`。

```python
# memory/scenes.py（新）
#: 同义拼写折叠表。**一份**：migrate_scenes.py 与读/过滤/写路径共用。
#: 与 scripts/migrate_scenes.py 的 SPELLING_MERGES 同源（该脚本改为导入本表）。
SCENE_SYNONYMS: Mapping[str, str] = {
    "记忆模块": "记忆", "项目概况": "项目", "工具约束": "工具",
    "执行约束": "执行", "发布流程": "发布", "构建流程": "构建",
    "依赖管理": "依赖", "单元测试": "测试", "配置文件": "配置",
    "数据库": "数据库", "db": "数据库", "api": "接口",
    ...
}

def fold_scene(value: str) -> str:
    """读/过滤/写时的同义折叠。`normalize_scene` 保持不变（契约）。

    **幂等不变量**：表的**值不得再是键**，否则二次折叠会过折。
    （`migrate_scenes.py` 的既有表满足这一条，[test_migrate_scenes.py:66-78](file:///workspace/ai-coding/tests/test_migrate_scenes.py#L66-L78) 钉死。）
    """
    normalized = normalize_scene(value)
    return SCENE_SYNONYMS.get(normalized, normalized)
```

**四个应用点**（初版只有两个，会静默失效）：

| 路径 | 位置 | 为什么必须 |
|---|---|---|
| **闸门之前** | [service.py:481](file:///workspace/ai-coding/ai_coding/memory/service.py#L481) 的 `scene not in self.governed_scenes()` | **评审 P0**：闸门用原始 `scene` 比较，折叠放晚了救不回来 |
| **过滤时** | [service.py:419-423](file:///workspace/ai-coding/ai_coding/memory/service.py#L419-L423) | 对**入参** `scene` 与 `note.scene` **都**套 `fold_scene` |
| **读时** | [store.py:65-88](file:///workspace/ai-coding/ai_coding/memory/store.py#L65-L88) `_load` | 磁盘上的旧拼写 |
| **写时** | [service.py:780](file:///workspace/ai-coding/ai_coding/memory/service.py#L780)、[service.py:859-869](file:///workspace/ai-coding/ai_coding/memory/service.py#L859-L869) | **评审 P1**：只折叠读路径会让同进程内新写的旧拼写与旧笔记并存，`governed_scenes()` 计成两个标签 |

* `scripts/migrate_scenes.py` 改为**导入 `SCENE_SYNONYMS`**（消除第二份词表）；
* **`tests/test_migrate_scenes.py` 同步改**（它 `from migrate_scenes import SPELLING_MERGES`）；
* **磁盘上的旧拼写**由 `migrate_scenes.py --apply` 负责（契约明写），本轮**不自动迁移**。

#### B.3 文档回写

* `features.md` 召回契约处补：**标签是过滤器**（已有）、**同义折叠表的位置与四处应用的分工**、
  **为什么不做全局上限**（`by_scene` 受 `max_index_entries` 约束）；
* 修 §1.3 ③ 的三处（含 [service.py:23-25](file:///workspace/ai-coding/ai_coding/memory/service.py#L23-L25) 的模块 docstring）。

### C. M-F3 · 召回预算实测

**范围**：建**召回延迟基准**，让 `recall_timeout_seconds` 有实测依据。**修正初版的三处错误**（评审 3 个 P0）。

#### C.1 基准脚本 `scripts/memory_latency_cases.py`

**① 三档，不是两档**（评审 P0-1）

| 档 | 触发条件 | 成本 |
|---|---|---|
| **命中** | 无过滤 + 有命中 | 1 次全库排序 **+ `1 + len(hits)` 次整文件写** |
| **普通未命中** | 无过滤 + 无命中 | 1 次全库排序（**不跑探针**） |
| **过滤未命中** | **设了任一过滤器** + 无命中 | 1 次全库排序 **+ 探针（再跑一次全库排序 + 全库语义打分）** |

**② 计时口径必须分开**（评审 P0-2）

| 量 | 含义 | 怎么测 |
|---|---|---|
| `elapsed_seconds`（服务内） | 只含排序（**不含写盘、不含 `index_summary`**） | 直接读 `RecallDiagnostics.elapsed_seconds` |
| 写盘 | `1 + len(hits)` 次整文件序列化 + 写 | **单独微基准** |
| `index_summary` | 每次召回都跑的多趟全库扫描 | **单独微基准** |

**③ 造库不能走 `add_note`**（评审 P0-3）：内存构造 `list[MemoryNote]`，**一次性**写 JSON，
`MemoryStore(path)` 加载。**不经过 harvest/consolidate**（load 也不触发 evict，已核实）。

**④ 档位设计（生产可达优先）**：默认 50 / 200 / 1_000；10_000 为**显式压力档**（抽样 + 少重复 + **小维度** + 临时 cache）。
**基准必须显式 `recall_timeout_seconds=0`**（否则超时会让写盘次数变 0，U12 假红）。

**⑤ 断言是结构性的，不是时间性的**（评审 P1-7）

| 断言 | 类型 |
|---|---|
| **过滤**未命中时 `rank_notes_detailed` 被调用 **2 次**；**普通**未命中 **1 次** | 结构性（计数桩） |
| 命中时写盘次数 **== `1 + len(hits)`** | 结构性（计数桩） |
| 命中档的墙钟 **>** 普通未命中档 | 结构性（用计数 + 一个宽松倍数，**不写"显著"**） |
| p50 / p95 表 | **只打印，不进断言** |

**⑥ U14 的作用域**：`by_scene` 的上界断言**限定在"经 harvest/consolidate 的库"**（此时 ≤ `max_index_entries`），
或断言 `len(by_scene) ≤ active notes` —— 1_000/10_000 压力档刻意绕过 evict，不能用它钉 200。

#### C.2 `retrieval_cases.py` 进 CI —— **移出 F**（评审 P1）

roadmap 把它列在 **M-G1**，且**词面门禁已由 pytest 覆盖**。**本轮不做。**

---

## 三、逐文件改动清单

| 文件 | 改动 | 属于 |
|---|---|---|
| `ai_coding/memory/service.py` | `_semantic_scores` → **三元组**（含 [service.py:501](file:///workspace/ai-coding/ai_coding/memory/service.py#L501) 的解包）；`RecallDiagnostics` 加 `semantic_status`（默认 `"off"`）；**闸门（:481）/过滤（:419）/写（:780、:859）四处**套 `fold_scene`；修 §1.3 ③ 的模块 docstring | A.1 / B.2 |
| `ai_coding/memory/scenes.py` | **新**：`SCENE_SYNONYMS` + `fold_scene()` | B.2 |
| `ai_coding/memory/store.py` | `_load` 对 `note.scene` 套 `fold_scene` | B.2 |
| `scripts/migrate_scenes.py` | 改为**导入** `SCENE_SYNONYMS` | B.2 |
| `tests/test_migrate_scenes.py` | 同步改（它 import `SPELLING_MERGES`） | B.2 |
| `ai_coding/tools/memory_search_tool.py` | 渲染 `semantic_status`（**`getattr`**） | A.1 |
| `scripts/calibrate_memory_threshold.py` | **新**：阈值标定（复用 `retrieval_cases` 的语料与服务构造） | A.2 |
| `scripts/retrieval_cases.py` | 分档；`_run_semantic_table` 的 `min_cosine` **与 transport** 参数化；**保留三条字面行** | A.3 |
| `scripts/memory_latency_cases.py` | **新**：三档 × 规模的延迟基准 + 写盘/`index_summary` 独立微基准 | C.1 |
| `docs/features.md` | 标签契约回写 + §1.3 ③ 的两处 + `recall_timeout_seconds` 的分位与"不含写盘" | B.3 / C.1 |
| `docs/roadmap-milestones.md` | M-F1~F3 标完成 | 文档 |

---

## 四、RIPR 用例

### Red（先写，必须变红）

| # | 用例 | 断言 |
|---|---|---|
| U1 | `enabled=True` + 端点 404 | `semantic_status == "endpoint_unsupported"` |
| U2 | `enabled=True` + 端点正常 | `semantic_status == "on"` |
| U3 | `enabled=False` | `semantic_status == "off"` |
| **U3b** | `enabled=True` + 端点**中途失败**（注入抛异常的 transport） | `semantic_status == "error"` |
| U4 | 工具输出含 `semantic_status` | 命中/未命中两条路径都能读到（**`getattr` 路径**） |
| U5 | 标定脚本（注入 transport） | 曲线 + 推荐阈值；**无 `relevant` 条目不入曲线**；`lexical_regression == 0`；报 FP@5 |
| U6 | `retrieval_cases.py` 默认路径 | 报 `lexical_hit` / `paraphrase_missed`；**不报** live 两档；**三条字面行原样保留** |
| U7 | `fold_scene("记忆模块")` / `("单元测试")` / `("db")` | 折叠成 `记忆` / `测试` / `数据库`（**真实词表**） |
| U8 | **闸门之前**：库里存 `记忆`（2 条），查询 `scene="记忆模块"`（2 条） | **命中**（初版放晚了会被闸门当"不是组"丢弃） |
| U9 | **过滤时**：库里存 `记忆`，查询 `scene="记忆模块"` | 命中 |
| U10 | 写时：`write_note(scene="记忆模块")` | 存进去的是 `记忆`（新旧拼写不并存） |
| U11 | 读时：磁盘上是 `记忆模块` | 加载后 `note.scene == "记忆"` |
| U12 | `migrate_scenes.py` | 导入 `SCENE_SYNONYMS`，无第二份词表 |
| U13 | 延迟基准：**过滤**未命中 / **普通**未命中 | 探针调用 **2 次** / **1 次** |
| U14 | 延迟基准：命中 k 条 | 写盘次数 **== `1 + k`** |
| U15 | 延迟基准：默认档 50/200/1_000 | 输出三档 p50/p95；**10k 默认不跑** |
| U16 | `by_scene` 上界（**经 harvest/consolidate 的库**） | 标签数 ≤ `max_index_entries` |

### Prove

* **U1-P**：真实 404 端点（或注入的 404 transport）——`semantic_status == "endpoint_unsupported"`，
  且**本进程只发一次 `/embeddings`**（计数断言）。
* **U13-P / U14-P**：计数桩（monkeypatch `rank_notes_detailed` 与 `store._save`）——**不依赖时间**。
* **突变测试**：① `semantic_status` 合并回静默 → U1 变红；② `SCENE_SYNONYMS` 清空 → U7 变红；
  ③ 折叠不套到**闸门**之前 → **U8 变红**；④ 折叠不套到**写路径** → **U10 变红**；
  ⑤ 探针计数桩删掉 → U13 变红；⑥ `1 + len(hits)` 改成 1 → U14 变红；
  ⑦ 无 `relevant` 条目混入曲线 → U5 变红。

### Refactor

* 折叠词表**只有一份**（`scenes.py`）；`migrate_scenes.py` 与四条路径共用。
* 标定脚本复用 `retrieval_cases.py` 的语料加载与 `_run_semantic_table` 的服务构造。

---

## 五、验收判据

* **M-F1**：`semantic_status` **四态**可观测（含 `error`）；"配了但端点不支持"不再静默；探测**每进程一次**；
  标定脚本可复跑并给出**分档 + FP@5 + 有效域**；离线路径只报离线两档且**保留三条字面行**。
* **M-F2**：`fold_scene` 在**闸门/过滤/读/写四处**都生效（U8–U11）；词表**一份**；
  `governed_scenes()` 的 ≥3 语义不变；文档写明"过滤器不是召回器"与"上界是 `max_index_entries`"。
* **M-F3**：延迟基准给出**三档 × 三规模**的 p50/p95 表 + **写盘与 `index_summary` 的独立微基准**；
  文档写明 `recall_timeout_seconds=2.0` 相对**哪个分位**、以及它**不含写盘**。
* 3.10 与 3.13 全量绿；真实 CI 绿（`ci.yml` 是一个 `quality` job 的 3.10/3.13 matrix）。

---

## 六、不做的事

* **不做 `enabled="auto"`** —— `enabled: bool` 经 pydantic 校验，`"auto"` **加载期即 `ValidationError`**；
  且 `True` 本来就会探测（[service.py:446](file:///workspace/ai-coding/ai_coding/memory/service.py#L446)），没有第三态可加。
* **有意偏离 roadmap M-F1 的"支持则默认开"** —— 默认开等于默认触网（探测本身就是一次网络调用），
  与 OWASP "excessive functionality" 相反。**保留默认关**，改为把"配了但不工作"做成**可观测**（A.1）。
* **不做标签语义聚类** —— 需要 embeddings，会引入不可解释的合并；只做**显式同义表**（§二 B.2 有理由）。
* **不设标签全局上限** —— 上界是 `max_index_entries=200`；但**调大 cap 时必须重新评估 `by_scene`**（§二 B.1）。
* **不给召回延迟设硬阈值** —— 阈值依赖部署环境；基准给分档数据 + 三个时钟口径，操作者按环境定
  （理由见 §二 C.1 ⑤：共享 runner 上绝对时间断言天然抖动）。
* **不把 `retrieval_cases.py` 接进 CI** —— 那是 M-G1，且词面门禁已由 pytest 覆盖（§二 C.2）。
* **不做 provider 抽象类** —— 本轮只需要可观测与标定路径（现状见 §1.2"无 provider 抽象"）。

---

## 七、评审记录

### 7.1 方案评审（子智能体，第 1 轮）

**3 个 P0（全部已修）**

| P0 | 内容 | 修法 |
|---|---|---|
| P0-1 | "未命中会再跑一遍全库"**只对"过滤未命中"成立**；普通未命中不跑探针；**命中路径因为写盘反而更慢** → U8 断言方向反了 | 改**三档**；断言改结构性 |
| P0-2 | 写盘成本是 **`1 + len(hits)` 次整文件写**，且**完全在 `elapsed_seconds` 之外** | 基准拆**三个时钟口径** |
| P0-3 | 用 `add_note` 造 10k notes 是 **O(N²)** → 基准不可用 | 一次性写 JSON 造库；10k 显式开启 + 小维度 |

**10 个 P1（全部已修）** —— 折叠表放 `normalize_scene` 违约（改 `scenes.py`）；消费者枚举错误（`by_scene` 无界）；
`lexical_regression` 挂离线自相矛盾（拆离线/live）；`auto` 空转（删）；只折叠存储值漏 `scene=` 入参（四处）；
U4 无 FP 约束；U8 不可机检；§C.2 属 M-G1 且冗余（移出）；10k 超生产上限（默认 50/200/1_000）。

### 7.2 方案评审（子智能体，第 2 轮）

**3 个 P0（全部已修）**

| P0 | 内容 | 修法 |
|---|---|---|
| P0-1 | `_semantic_scores` 2-tuple → 3-tuple，**调用点 [service.py:501](file:///workspace/ai-coding/ai_coding/memory/service.py#L501) 要改** | 清单与正文点出 |
| P0-2 | `fold_scene` 放在 `_candidates` **太晚** —— `governed_scenes()` 闸门（[service.py:481](file:///workspace/ai-coding/ai_coding/memory/service.py#L481)）先拦截 | 折叠提到**闸门之前**（U8） |
| P0-3 | U7 用**虚构标签**（认证/单测/db），真实表没有 → 永红 | 改用**真实中文词表** |

**11 个 P1（全部已修）**：工具侧需 `getattr`（`_EmptyDiagnostics`）；`lexical_hit` 计数 78→**68/124**；
`lexical_regression` 口径统一（注入 transport 可离线算）；保留 `test_retrieval_benchmark.py` 三条字面行；
**写路径也要折叠**（否则新旧拼写并存）；`test_migrate_scenes.py` 同步改；U14 限定作用域；U12 关超时；
`semantic_status` 默认 `"off"`；§1.3 只留第 ③ 处；roadmap"支持则默认开"显式声明有意偏离。

**4 个 P2**：§一↔§三 两处漏项（`service.py` docstring、timeout 文档化）；§六 三条禁令补论证；
`M-A6` 悬空引用删除；§八 4 条未用来源补引用；`fold_scene` 幂等不变量写明；按 `case.relevant` 过滤而非硬编码类名。

#### 7.2.1 两轮 findings 的处置

| Finding | 级别 | 处置 |
|---|---|---|
| 未命中档分档错误 | **P0** | 三档；U13 结构性计数 |
| 写盘次数与计时口径 | **P0** | `1 + len(hits)`；三个时钟口径 |
| `add_note` O(N²) | **P0** | 一次性写 JSON 造库 |
| 2-tuple→3-tuple 漏调用点 | **P0** | 正文 + 清单点出 |
| 折叠放太晚被闸门拦截 | **P0** | 提到闸门之前；U8 |
| U7 虚构标签 | **P0** | 真实中文词表 |
| 折叠表违约 / 消费者枚举错 / `lexical_regression` 口径 / `auto` 空转 / 只折叠存储值 / U4 无 FP / U8 不可机检 / §C.2 属 G / 10k 超上限 | P1 | 已修 |
| 工具侧 `getattr` / 68 vs 78 / 三条字面行 / 写路径折叠 / `test_migrate_scenes` / U14 作用域 / U12 关超时 / `semantic_status` 默认 / §1.3 范围 / roadmap 偏离声明 | P1 | 已修 |
| §一↔§三 漏项 / §六 论证 / `M-A6` 悬空 / §八 未用来源 / 幂等不变量 / 按 `case.relevant` 过滤 | P2 | 已修 |

---

## 八、调研来源

* Mem0 vs Zep（架构、多信号检索）—— https://mem0.ai/blog/zep-vs-mem0-which-ai-memory-layer-should-you-choose
* Mem0 vs Zep（生产对比）—— https://mem0.ai/library/agent-memory/mem0-vs-zep
* Mem0 / Letta / Zep / Cognee 四种下注 —— https://menuagentic.com/zh/blogs/mem0-vs-letta-vs-zep-vs-cognee/
* Mem0 vs Letta vs Zep（含基准口径分歧）—— https://iotdigitaltwinplm.com/mem0-vs-letta-vs-zep-agent-memory-frameworks-comparison-2026/
* Letta（MemGPT）三层记忆 —— https://aiwiki.ai/wiki/letta
* Letta / MemGPT 模式（Notebook）—— https://colab.research.google.com/github/NirDiamant/Agent_Memory_Techniques/blob/main/all_techniques/26_letta_memgpt_patterns/letta_memgpt_patterns.ipynb
* MemoryTax（记忆对强模型收益有限）—— https://arxiv.org/html/2610.29823
* KSE-Web（小库 BM25 胜过 dense）—— https://arxiv.org/abs/2608.21365
* Curse of Dense Low-Dimensional IR —— https://arxiv.org/html/2012.14210v1
* BM25 Wins at Scale —— https://www.alphaxiv.org/abs/2607.26497
* Supermemory：召回延迟预算（按 P95 定 buffer）—— https://supermemory.ai/blog/latency-budgets-memory-retrieval
* GenAI Mindmap：Timeout Budget（分阶段 + 部分结果）—— https://genai4a11.github.io/concepts/timeout-budget.html
* Agensi：降级与 fallback 设计（必须披露）—— https://www.agensi.io/skills/degraded-result-and-fallback-design
* Zylos：AI agent 系统的优雅降级模式 —— https://zylos.ai/research/2026-05-30-graceful-degradation-patterns-ai-agent-systems/
* AI Agent Memory Architecture Guide 2026（含延迟量级）—— https://baeseokjae.github.io/posts/agent-memory-architecture-guide-2026/
