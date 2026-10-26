# 领域 E · 上下文工程（M-E1 ~ M-E4）

> 调研：一轮仓库侦察 + 一轮联网（50 引用源）。方案与评审记录见 §八。
>
> **一句话**：领域 E 不是独立子系统，而是**横切在 ReAct 循环请求组装路径上的一层上下文塑形**，
> 核心实现集中在 `core/context_compression.py`。侦察发现一个共同模式 ——
> **观测层缺失**（事件不含每层预算、`/trace` 不渲染压缩、cache_probe 不在 CI）
> 叠加**测试载体错配**（pytest 版用非默认 `max_messages` 掩盖真实失败）。

---

## 一、调研结论

### 1.1 联网调研主线

**四项彼此耦合**：压缩的"**层序**"同时决定**计数是否稳定**与**前缀是否可缓存**；
而**计数与预算的可观测性**是判断这两点是否成立的唯一手段。

**① 语义分层**：共识形态是"类 CPU 存储层级"——**工作层（常驻，占窗口 10–15%）→ 压缩会话记忆 → 归档**。
关键设计是**把两类内容分开**：**可淘汰的原始历史** vs **硬保留的显式记忆层**（结构化实体槽位，
即使滑动窗口把历史压到只剩几轮也**永不丢失**）。硬保留层必须**钉在可变区之外的固定位置**。

**② 计数稳定性**：系统里同时存在**三个不同步的"计数"** —— 原始消息数 / 可见消息数 / 有效 token 数。
压缩层按"是否改变条数"分两类：**改条数**（整段摘要、滑动窗口）与**只改 token**（掩码、microcompact）。
* **Hermes 的真实 bug**：压缩把 token 从 ~288K 降到 ~183K（省 36%），但**消息条数 220→220 未变**，
  而循环用 `len(messages) >= orig_len` 判定"无法再压" → 报"上下文耗尽"并**自动重置会话**。
  修法：**按 token 计，而非按行数计**。
* **原子性**：把 assistant 的 `tool_call` 与对应 `tool_result` 拆开淘汰会产生**非法请求** →
  压缩必须以 **MessageGroup 原子单元**为单位。
* **边界安全**：截断若切在工具调用/审批中间会破坏协议结构 → trim 边界应**吸附到 user message / 段边界**。
* **投影优先于破坏**：Claude Code 的 Context Collapse 是**读时投影**（原始消息从不修改，
  另有一份折叠决策日志在每次读取时重放）→ **不使 prompt 缓存前缀失效**，且**零 API 成本**。

**③ 压缩预算可观测**：预算公式 `effectiveMax = window − outputReserve − safetyBuffer`，
**先给前面的账户设上限，再让历史吃剩下的**（反过来做必然导致系统指令被裁）。
可观测性需要**每次调用级账本**与**结构化压缩事件**（`compact:start`/`compact:exec` 携带
**压缩前后 token 数、压缩比、摘要文本**）。
* **估算粗糙**是高频失效：用 `text.length / 3` 估算 token，中英混合误差 **30–50%**。
* **二次方成本**：N 步 agent 的输入成本是 **O(N²)** —— 10 步不是 10×，而是 **25–50×**。
* **压缩策略与缓存 TTL 脱钩**是"一阶成本问题"（每次压缩都摧毁已缓存前缀）。

**④ 前缀缓存友好度**：命中前提是**从第 0 个 token 起的精确前缀匹配** ——
不是"内容相似"，**任何一个 token 不同都会破坏匹配**，其后全部重算。
* **装配顺序由"这一块多常变化"决定**：稳定在前、易变在后。
  动态值（时间戳、request id、session id、工作目录、光标状态、最新检索结果）**一律放末尾**。
* **时间戳陷阱**：system prompt 顶部放精确到秒的时间戳 → **开了缓存反而比不开贵 25%**
  （每轮付 1.25× 缓存写费却一次没读到）。
* **cache_write 单价是 cache_read 的 12.5 倍**：一个 4 轮会话烧 $77.3，其中 **83% 全是 cache_write**。
* **mask 而非 remove**：掩码保留结构、可恢复，且**不重写整段前缀**。

### 1.2 四项的耦合

| | 语义分层 | 计数稳定性 | 预算可观测 | 前缀缓存 |
|---|---|---|---|---|
| **核心问题** | 信息按什么生命周期存在 | 压缩后条数/token 是否收敛 | 压缩是否真生效 | 压缩是否破坏可复用前缀 |
| **判定信号** | 分层命中率、遗忘策略 | **token 下降**（非条数） | 填充率、命中率、成本/任务 | 缓存命中率 |
| **代表实现** | GA L1–L4 / MEMTIER | Hermes bug / Context Collapse | SemaClaw `compact:*` | llmbuffer / Claude Code 五段管线 |

---

## 二、仓库侦察（子智能体 + 我逐条核实）

### 2.0 共同底座

**管线顺序是硬不变量**，写死在模块 docstring 与 `ContextManager.compress`：

```
L3 persist_large_outputs → L1 snip_middle → L2 placeholder_old_results
  → L4 _run_l4(LLM 摘要) → hard_trim → sanitize_pairs
```

**预算计量契约**：`estimate_tokens`（CJK≈½、其他≈¼）、`_history_tokens`（含 `image_tokens` 固定 1600/张）、
`compaction_ceiling`（对齐 OC）；`should_compact` 取 `max(provider observed, estimate)`。

**观测链**：`events.py`（`CompactionStarted/Delta/Ended/Failed/Skipped` + `ContextCompacted`）
→ `trace.py` JSONL → `app/trace_text.py`（`/trace` 摘要）→ 前端压缩卡片。

### 2.1 M-E1 · L3 语义显式化 —— **语义无测试锁定**

* **实现**：`persist_large_outputs` 的 `while start > 0 and messages[start-1].role == "tool"`
  —— **只取末尾连续 `role=="tool"` 段**。触发条件：该段总字节 > `max_context_tokens/2`
  且**单条** > `per_result_persist_bytes`（默认 30000）。
* **语义**：它是**回合中段护栏**（工具刚返回、模型还没答的瞬间，防 400）。
  回合结束时 `session.messages` 末条是 assistant → `batch` 为空 → 直接返回，
  **落盘会话 JSON 永远观测不到**。
* **可验证缺口证据（我复现）**：

  ```
  M-E1 非尾部(后有 assistant): 落盘=0 未变=True   ← 大输出被静默留在历史里
  M-E1 尾部连续 tool:          落盘=1 未变=False
  ```
* **测试载体**：`test_context_compression.py` 的 3 个 `test_persist_*` —— **全部构造尾部输入**；
  `eval-04-longtask-mechanics.md` 记录"10 轮真实对话后 `persisted=0`，只有真实函数+合成输入才触发"。
* **缺口**：**没有任何一条测试断言"非尾部大输出不被持久化"（文档化路径），
  也没有一条断言"非尾部也会持久化"（扩展路径）** —— 语义是**隐式**的。

### 2.2 M-E2 · L2/L4 顺序导致的计数不稳定 —— **顺序无不变量**

* **实现**：`compress()` 中 L2（`placeholder_old_results`）**先于** L4（`_run_l4`）。
  L4 会把中段（含 L2 刚生成的 `[Previous:...]` 信封）**摘要掉**。
* **可验证缺口证据（我复现）**：8 轮工具重历史 + 摘要器 →
  `压缩后信封数=0 剩余条数=2` —— **L4 摘要把 L2 的信封一并吃掉**。
* **缺口**：全仓**没有**断言管线顺序的单测；`test_context_compression_l2_window.py`
  **绕开了端到端顺序**（直接测 `placeholder_old_results`）；
  `eval-04` 的端到端计数断言**已被改成"直接验证真实 `elide_and_persist`"** ——
  即**承认了计数不可断言**，但没有把这个承认**固化成不变量**。

### 2.3 M-E3 · 压缩预算可观测 —— **观测层缺失 + 测试载体错配**

* **实现**：`/trace` 的 `summarize_turn` **完全不处理压缩事件**；`render_turn` 只输出
  token/spans/model steps/tool calls/degraded/cache%。
* **契约**：`CompactionEnded` 只带 `before_messages/after_messages` + 摘要自身 usage，
  **没有每层压缩前后的 token 估算与命中数**。前端压缩卡片也只显示 `before→after 条 · in/out · 压缩耗`。
* **可验证缺口证据（我复现）**：

  ```
  $ python scripts/r7_cases.py
  FAIL  books_feed_the_policy: action=NUDGE reason='repeated tool failures'
  FAIL  l2_long_history_stays_under_budget: est tokens=7058 envelopes=67 of 280 messages
  ```

  而 pytest 版 `test_a_long_history_stays_well_under_the_context_budget` **通过** ——
  差异是它用 `_Settings.max_messages=50`（触发 L1 snip），而 r7 用 `AIConfig()` 默认
  `max_messages=400`（280 条不触发 snip，只靠 L2 信封化，7058 > 24000 目标）。
* **缺口**：`r7_cases.py` **不在 CI**（ci.yml 只跑 redline/memory/trace cases），
  且 pytest 版用**非默认 `max_messages`** 绕过了真实失败 ——
  **该缺口既无自动化守护，也被测试默认值掩盖**。

### 2.4 M-E4 · 压缩前缀缓存友好度 —— **无统一断言 + 探针不在 CI**

* **实现**：`request_builder.build_tail` 把 memory/task/hook/digest 作为**易变尾部追加在请求末尾**，
  system prompt 与历史保持字节一致；`compress_if_needed` 低于水位**完全不动历史**（`out is history`），
  从而保持 append-only。
* **契约**：`ContextAssembled.prefix_shared_ratio` 是我们自己的**估算**；
  `ModelStepFinished.cached_tokens` 是 provider **真实值**。
* **可验证缺口证据**：`features.md` 记录实测 —— 记忆放系统提示前缀命中 **7%**、放尾部 **94%**；
  每轮压缩历史中段仅 **26%**（改按预算分页后 **92%**）。目标 ≥90%。
* **缺口**：`cache_probe.py` 是**手工脚本**（需 `config.local.yaml`、产生真实调用、**不在 CI**），
  且只覆盖 system-prompt/尾部两类通道；**没有**"所有注入通道都按预算分页"的**统一断言**，
  `prefix_shared_ratio` 也**未被任何阈值测试守护**。

### 2.5 附带发现（文档漂移）

`data-contracts.md:107` 仍列 `l4_keep_tail = 6`（L4 摘要保留尾部条数），
但代码实际是 `l4_tail_tokens = 15_000`（`models.py:357`），
且 `compaction-debt.md:132` 明确"`l4_keep_tail`（条数）配置项已删除"。**同一批文档里已存在的事实漂移。**

---

## 三、方案：逐项实现

### A. M-E1 · L3 语义显式化

**问题**：L3 的语义（"只处理末尾连续 tool 段"）是**隐式**的 —— 无文档、无测试锁定。

**修法**：**把语义写成可执行的契约**，二选一由用户拍板（见 §五）：

| 方案 | 内容 |
|---|---|
| **A1（保守）** | **文档化 + 测试锁定现状**：在 `features.md` / `data-contracts.md` 写明"L3 是**回合中段护栏**，只处理末尾连续 tool 段"；加一条测试断言"**非尾部大输出不被持久化**"（把现状钉住） |
| **A2（扩展）** | **扩展语义**：`persist_large_outputs` 也处理**非尾部**的单条超大工具结果（> 阈值即持久化），并在文档写明新的语义边界 |

* **共同部分**：`CompactionEnded`（或新事件）带上 **L3 实际持久化了几条 / 跳过几条 / 为什么** ——
  让"语义"从注释变成**可观测事实**（这是 M-E3 的一部分）。
* **为什么这条重要**：`eval-04` 记录"10 轮真实对话后 `persisted=0`" ——
  **真实使用里 L3 几乎从不触发**。若语义是"压缩层"，这是缺口；若是"回合中段护栏"，这是设计。
  **必须显式化，否则下一个人会按错误的心智模型去改它。**

### B. M-E2 · 计数稳定性

**问题**：压缩成功的判据**没有写死**，且"压缩后条数"被当成断言目标（而它不稳定）。

**修法**：

1. **写死"按 token 判定"**（调研的 Hermes 教训）：
   `should_compact` / 压缩成功判据**一律用 token**，**不用 `len(messages)`**。
   加一条单测：**构造"token 大降但条数不变"的场景，断言压缩被判定为成功**。
2. **加管线顺序不变量单测**：断言 `compress()` 中 L2 的调用**先于** L4
   （用可注入的 recorder 或 monkeypatch 计数），把 docstring 里的"硬不变量"**变成可执行断言**。
3. **把"elided 计数不可断言"固化**：把 `eval-04` 里那句"直接验证真实 `elide_and_persist`"
   的承认**写进测试注释与文档**，并**删掉/改写**任何仍以"压缩后条数"为目标的断言。
4. **原子组**：`hard_trim` / L1 的截断边界**吸附到 user message**（调研：切在 tool_call/tool_result
   中间会产生非法请求）。加一条测试：构造 tool_call 与 result 相邻的历史，断言压缩后**两者同生共死**。

### C. M-E3 · 压缩预算可观测

**问题**：`CompactionEnded` 只有条数 + 摘要自身 usage；`/trace` 不渲染压缩；r7 的失败被非默认配置掩盖。

**修法**：

1. **扩展 `CompactionEnded`**（向后兼容，新增可选字段）：

   ```python
   layers: tuple[LayerStat, ...]      # 每层：name / before_tokens / after_tokens / hits
   #: L1 snip 掉几条、L2 信封化几条、L3 持久化几条、L4 摘要前后 token
   estimated_before: int
   estimated_after: int
   ```

   * `LayerStat` 是**新增 dataclass**，不破坏既有字段。
   * 与调研的 `compact:start`/`compact:exec`（携带压缩前后 token、压缩比、摘要文本）对齐。
2. **`/trace` 渲染压缩**：`summarize_turn` 折叠 `CompactionStarted/Delta/Ended`，
   `render_turn` 增加一行"压缩：L1 −N 条 / L2 −M 条 / L3 −K 条 / L4 摘要 X→Y token（估）"。
3. **修测试载体错配**：
   * pytest 版的 `test_a_long_history_stays_well_under_the_context_budget` **改用默认 `max_messages`**，
     让那个**真实的 7058 > 24000 失败**在 CI 里可见；
   * **先诊断再修**：7058 token / 280 条 / 67 信封 —— 目标 24000，**超了 3 倍**。
     这是**真实的预算缺口**（不是测试问题），需要决定：① 调 L2 窗口；② 调水位；③ 承认并记录。
     **由用户拍板**（见 §五）。
   * `r7_cases.py` 的预算用例**接入 CI**（或等价地，把该断言搬进 pytest）。
4. **前端压缩卡片**：显示每层命中数（数据来自 #1）。

### D. M-E4 · 前缀缓存友好度

**问题**：`prefix_shared_ratio` 无阈值守护；无"所有注入通道都按预算分页"的统一断言；探针不在 CI。

**修法**：

1. **统一分页断言**（新，`tests/test_context_compression_cache_friendliness.py`）：
   对**每一条**注入通道（memory / task / hook / digest / skill 索引 / 检索片段）断言
   **注入后前缀共享率 ≥ 阈值**，且**注入位置在可变区之后**。
   —— 把 `features.md` 里那句"改按预算分页后 92%"从**一次性实测**变成**永久回归锁**。
2. **`prefix_shared_ratio` 阈值测试**：直接断言 `ContextAssembled.prefix_shared_ratio ≥ 0.9`
   （在注入 memory 的场景下）。
3. **`cache_probe.py` 接入 CI**（或做一个**离线等价物**）：
   * 离线版：用**确定性序列化**断言"同一会话连续两轮请求的**前缀字节完全相同**"
     （除了末尾的易变区）—— 不需要真实 provider。
   * 保留 `cache_probe.py` 作为**真实 provider 验证**的手工工具。
4. **时间戳陷阱扫描**：加一条测试/扫描，断言**系统提示与工具定义里没有秒级时间戳**
   （调研：这是"开了缓存反而更贵 25%"的根因）。

### E. 附带 · 修文档漂移

* `data-contracts.md:107` 的 `l4_keep_tail = 6` → 改为 `l4_tail_tokens = 15000`（与 `models.py:357` 一致）。
* `r11-analysis.md:38` 的 `messages[-self._l4_keep_tail:]` → 标注为**历史快照**（该配置已删除）。

---

## 四、逐文件改动清单

| # | 文件 | 改动 | 为什么 |
|---|---|---|---|
| 1 | `core/context_compression.py` | `persist_large_outputs`（A1 文档化 / A2 扩展）+ `LayerStat` 收集 | M-E1 / M-E3 |
| 2 | `core/events.py` | `CompactionEnded` 增 `layers` / `estimated_before` / `estimated_after`（**向后兼容**） | M-E3 |
| 3 | `app/trace_text.py` | `summarize_turn` 折叠压缩事件；`render_turn` 输出每层统计 | M-E3 |
| 4 | `frontend/src/component/message.tsx` + `util/*` | 压缩卡片显示每层命中数 | M-E3 |
| 5 | `tests/test_context_compression.py` | M-E1 语义锁定（A1）或扩展断言（A2） | M-E1 |
| 6 | `tests/test_context_compression_l2_window.py` | **改用默认 `max_messages`**，让真实失败可见 | M-E3 |
| 7 | `tests/test_context_compression_cache_friendliness.py`（新） | 统一分页断言 + `prefix_shared_ratio` 阈值 + 前缀字节稳定 + 时间戳陷阱扫描 | M-E4 |
| 8 | `tests/test_context_compression.py` | 管线顺序不变量 + **按 token 判定** + 原子组 | M-E2 |
| 9 | `scripts/cache_probe.py` | 保留为手工工具（离线等价物进 #7） | M-E4 |
| 10 | `docs/features.md` / `docs/data-contracts.md` | M-E1 语义 + 修 `l4_keep_tail` 漂移 | 文档 |
| 11 | `docs/eval-04-longtask-mechanics.md` | 把"计数不可断言"的承认**固化** | M-E2 |

### 4.1 语义等价性

* **`CompactionEnded` 新增字段全部可选** → 既有消费方（trace JSONL、前端）**不受影响**。
* **M-E2 的"按 token 判定"**：`should_compact` 已取 `max(provider observed, estimate)` ——
  本轮**把成功判据也统一到 token**，不改触发条件。
* **M-E1 A1**：纯文档 + 测试，**零行为变更**。**A2**：行为变更（更多大输出被持久化）。
* **M-E4**：纯新增断言，**不改装配逻辑**（除非发现真的不达标）。

---

## 五、RIPR 用例

### Red（先写，必须变红）

| 编号 | 用例 | 期望红相（今天必须失败） |
|---|---|---|
| W1 | `test_l3_semantics_are_locked`（A1） | 断言"非尾部大输出**不**被持久化"（今天：无此断言） |
| W2 | `test_a_token_reduction_with_an_unchanged_message_count_counts_as_success` | token 大降、条数不变 → 压缩判为**成功**（今天：无此判据） |
| W3 | `test_the_pipeline_order_is_l3_l1_l2_l4` | 顺序不变量（今天：无断言） |
| W4 | `test_a_tool_call_and_its_result_are_eliminated_together` | 原子组（今天：无断言） |
| W5 | `test_compaction_ended_reports_per_layer_stats` | `layers` / `estimated_before/after`（今天：无字段） |
| W6 | `test_the_trace_renders_the_compaction_layers` | `/trace` 输出每层统计（今天：不渲染压缩） |
| W7 | `test_every_injection_channel_keeps_the_prefix_shared` | 每通道前缀共享率 ≥ 阈值（今天：无统一断言） |
| W8 | `test_the_context_prefix_is_byte_stable_across_turns` | 连续两轮请求前缀字节相同（除末尾易变区）（今天：无断言） |
| W9 | `test_the_system_prompt_carries_no_sub_second_timestamp` | 时间戳陷阱扫描（今天：无断言） |
| W10 | `test_the_default_max_messages_still_stays_under_budget` | **用默认配置**跑长历史 → 预算内（今天：**失败**，7058 > 24000） |

### Prove

1. `.venv/bin/python -m pytest -q`
2. `.venv/bin/ruff check .` / `.venv/bin/mypy ai_coding`
3. **突变测试**：
   * 把 L4 挪到 L2 之前 → W3 变红
   * 把成功判据改回 `len(messages)` → W2 变红
   * 去掉原子组保护 → W4 变红
   * 去掉 `layers` 字段 → W5/W6 变红
   * 把 memory 注入移到 system prompt 前缀 → W7/W8 变红
4. **3.10 venv 跑 CI 同构七步**
5. **端到端**：`/trace` 看到压缩层统计

---

## 六、验收判据

* **M-E1**：L3 的语义**被文档化且被测试锁定**（A1）或**按扩展后的语义实现并锁定**（A2）。
* **M-E2**：压缩成功判据**按 token**；管线顺序**有不变量单测**；tool_call/result **原子淘汰**。
* **M-E3**：`CompactionEnded` 带**每层统计**；`/trace` **渲染压缩**；**默认配置**下的预算用例
  在 CI 里可见（且通过，或按拍板结论记录）。
* **M-E4**：**每条注入通道**都有前缀共享断言；`prefix_shared_ratio` **有阈值**；
  连续两轮请求**前缀字节稳定**；系统提示**无秒级时间戳**。
* 3.10 与 3.13 全量绿；真实 CI 双 job 绿。

---

## 七、不做的事

* **不做 provider 真实 tokenizer 替换** —— 调研建议"用供应商 tokenizer"，但引入依赖
  （`tiktoken` / 各家 tokenizer）是独立工程；本轮**先让估算可观测**（M-E3），
  替换留待需要时。
* **不做 Context Collapse（读时投影）** —— 本轮**先让计数判据正确**（M-E2）；
  投影是更大的架构变更，且会动到前端。
* **不做 IP/CIDR 级出网管控**（那是领域 D 的残余风险）。
* **不改 `estimate_tokens` 的字符估算** —— 只让它**可观测**，不换算法（避免一轮改动里混两件事）。
* **不做缓存预热** —— 调研说"要算账"（`p_reuse × 冷启动增量 − 刷新成本`），
  在没有真实 `cached_tokens` 数据前无法算。

---

## 八、评审记录

### 8.1 方案评审（子智能体，第 1 轮）

_待填。_

### 8.2 方案评审（子智能体，第 2 轮）

_待填。_

### 8.3 实现评审（子智能体）

_待填。_
