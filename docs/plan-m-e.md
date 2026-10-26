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

### 2.3 M-E3 · 压缩预算可观测 —— **观测层缺失**（但"预算缺口"是误诊）

* **实现**：`/trace` 的 `summarize_turn` **完全不处理压缩事件**；`render_turn` 只输出
  token/spans/model steps/tool calls/degraded/cache%。
* **契约**：`CompactionEnded` 只带 `before_messages/after_messages` + 摘要自身 usage，
  **没有每层压缩前后的 token 估算与命中数**。前端压缩卡片也只显示 `before→after 条 · in/out · 压缩耗`。
* **`/trace` 不渲染压缩** —— ✅ 属实。
* **`r7_cases` 的 `l2_long_history_stays_under_budget` FAIL —— 但根因是测试 bug，不是预算缺口**
  （**第 1 轮评审 P0-1 更正**）。实测：

  ```
  AIConfig().max_context_tokens           = 0        ← 未解析
  r7 用例阈值 = max_context_tokens * 0.5   = 0.0      ← 用例实际用的
  resolve_context_window(cfg)             = 48000    ← 真窗口
  resolve_context_window(cfg) * 0.5       = 24000.0  ← 正确阈值
  tokens=7058 <= 24000  →  True                              ← 本应 PASS
  tokens=7058 <= 0      →  False  ← r7 的判据，所以 FAIL
  ```

  **7058 是 24000 的 29%** —— **没有预算缺口**。失败是因为 r7 用例读了**未解析的**
  `AIConfig().max_context_tokens`（默认 0），而 `ContextManager` 靠内部的 `or 48_000` 兜底才拿到真窗口。
* **连带更正**：
  * **不存在"调 L2 窗口 / 调水位"的拍板项** —— 修法是**改判据载体**（`resolve_context_window(cfg)`）。
  * `books_feed_the_policy` 的 FAIL 与压缩**毫无关系**（它只构造 `TurnBooks`+`TurnPolicy`，
    失败是策略阶梯先命中 L1 而非期望的 L2）—— 是 **domain B 的过期期望**，应单独归案，**移出 M-E**。
  * "r7 与 pytest 的差异是 `max_messages` 造成" **也不成立**（正确阈值下两者都通过）。
* **缺口（真实的）**：`r7_cases.py` **不在 CI**，且**断言载体是错的** ——
  一个错误的断言既不能守护预算，又会**制造"有缺口"的错觉**。

### 2.4 M-E4 · 压缩前缀缓存友好度 —— **缺 CI 里的离线门禁**

* **实现**：`request_builder.build_tail` 把 memory/task/hook/digest 作为**易变尾部追加在请求末尾**，
  system prompt 与历史保持字节一致；`compress_if_needed` 低于水位**完全不动历史**（`out is history`），
  从而保持 append-only。
* **契约**：`ContextAssembled.prefix_shared_ratio` 是我们自己的**估算**；
  `ModelStepFinished.cached_tokens` 是 provider **真实值**。
* **实测**：`features.md` 记录 —— 记忆放系统提示前缀命中 **7%**、放尾部 **94%**；
  每轮压缩历史中段仅 **26%**（改按预算分页后 **92%**）。
* **已有守护（第 1 轮评审 P1-3 更正 —— 原写"未被任何阈值测试守护"不准确）**：
  * `test_agent_loop_r4_wiring.py:148`：`> 0.5`（离线、**在 CI**）
  * `test_budget_ledger.py:58/91`：精确 0.9 / 0.95
  * `trace_cases.py:429`：`>= 0.9`（**仅 `--real`**）
  * → 准确说法：**缺 CI 里的离线 0.9 门禁**。
* **缺口**：`cache_probe.py` 是**手工脚本**（需 `config.local.yaml`、产生真实调用、**不在 CI**），
  且只覆盖 system-prompt/尾部两类通道；**没有**"所有注入通道都按预算分页"的**统一断言**。
* **`prefix_shared_ratio` 不能当硬阈值用（第 1 轮评审 P0-2）**：
  `features.md:418-423`（M-A6，2026-10-25）已明确 —— **整请求的前缀比率不是定值**，
  实测随工作区路径长度漂移（8/32/66/442 字符 → 87%/87%/86%/**77%**），
  `memory_cases` 的 case11 已改用**路径无关的布尔不变量**并**不再使用百分比阈值**。
  机理：`prefix_shared` 的分母 `len(current)` **含易失尾部**，尾部越大比率越低。
  → **本轮不得重新引入百分比阈值**（会推翻 M-A6 的决策，并在深路径工作区下 flaky）。

### 2.5 附带发现（文档漂移）

`data-contracts.md:107` 仍列 `l4_keep_tail = 6`（L4 摘要保留尾部条数），
但代码实际是 `l4_tail_tokens = 15_000`（`models.py:357`），
且 `compaction-debt.md:132` 明确"`l4_keep_tail`（条数）配置项已删除"。**同一批文档里已存在的事实漂移。**

---

## 三、方案：逐项实现

### A. M-E1 · L3 语义显式化

**问题**：L3 的语义（"只处理末尾连续 tool 段"）是**隐式**的 —— 无文档、无测试锁定。

**修法**：**A1（文档化 + 测试锁定现状）** —— 用户已拍板（A2 的真实风险见评审 P1-1：
它会去掉"最后手段"判据、未定义范围/顺序/预算、并缩小 L2 覆盖；若要保留须**先补规格**）。

**具体动作**：

| 动作 | 内容 |
|---|---|
| 文档 | 在 `features.md` / `data-contracts.md` 写明"L3 是**回合中段护栏**，只处理**末尾连续 tool 段**，且要求批总量 > 窗口一半 + 单条 > 30000" |
| 测试 | 加一条断言"**非尾部大输出不被持久化**"（把现状钉住） |
| 可观测 | `CompactionEnded`（或新事件）带上 **L3 实际持久化了几条 / 跳过几条 / 为什么** —— 让"语义"从注释变成**可观测事实**（见 M-E3） |

* **为什么这条重要**：`eval-04` 记录"10 轮真实对话后 `persisted=0`" ——
  **真实使用里 L3 几乎从不触发**。若语义是"压缩层"，这是缺口；若是"回合中段护栏"，这是设计。
  **必须显式化，否则下一个人会按错误的心智模型去改它。**

### B. M-E2 · 计数稳定性

**问题（第 1 轮评审 P0-3 更正）**：原方案说"压缩成功的判据没有写死，一律用 token"——
**前提不存在**。实测：

* `should_compact`（`context_compression.py:618-635`）**本来就是 token 判据**
  （`max(observed, _history_tokens) >= ceiling`）。
* "压缩成功"判据在 `compaction.py:152-193` 是**内容真值** `result != history`，
  且 `test_compaction_oc_parity.py:10` 已把它写成契约。
* **全仓没有任何 `len(messages)` 版的成功判据可替换** —— 实测"token 大降、条数不变"
  的场景**已被判为成功**（`Ended=1 Failed=0`）。

→ **Hermes 教训在本仓已经落实**。原方案的 W2（断言"按 token 判定"）**恒绿**，违反 RIPR。

**修法（重新定位）**：

1. **文档化现有判据**：把"触发按 token（`should_compact`）、成功按内容真值（`result != history`）、
   **不按条数**"写进 `features.md` / `data-contracts.md`，并说明**为什么**（Hermes 的教训：
   按条数判会把"省了 36% token 但条数没变"误判为失败 → 假性耗尽 → 会话重置）。
2. **加契约测试锁住内容真值判据**：断言 `Compactor` 的成功判据**是** `result != history`
   （不是条数比较）—— 把 `test_compaction_oc_parity.py` 的契约**扩展到显式断言"条数不变也算成功"**。
   * **构造方式（评审 P3-⑥）**：用真实管线造"条数不变"**条件苛刻**
     （`force=True` 时 L4 必跑 → 条数必变）。**更稳的做法**：仿
     `test_compaction_oc_parity.py:192-202` 的 `_SilentContext`，写一个返回
     「**同条数、异内容**」副本的 fake delegate，直接断言 `CompactionEnded`（而非 `Skipped/Failed`）。
3. **加管线顺序不变量单测**：断言 `compress()` 中 L2 的调用**先于** L4。
   * **观测手段（评审 P2-⑤）**：两者注入方式**不同** —— L2 是**模块级函数**
     `placeholder_old_results(...)`（`monkeypatch.setattr(context_compression, ...)`），
     L4 是**绑定方法** `ContextManager._run_l4`（patch `ContextManager._run_l4`）。
   * **具体形状**：monkeypatch 两个目标，各自 append 一个 tag 到共享列表，断言 `tags == ["L2", "L4"]`。
   * **以文档为主、单测作回归锁**（评审 P2-1）—— 别把单测当主要交付。
4. **把"elided 计数不可断言"固化**：把 `eval-04` 里那句"直接验证真实 `elide_and_persist`"
   的承认**写进测试注释与文档**。
   * **先核实再写（评审 P3-⑦）**：现存断言（`test_context_compression_l2_window.py:221-224`、
     `r7_cases.py:234-240`）**都是 token 判据**，**没找到**"以压缩后条数为目标"的断言 ——
     所以这条指令**可能是空操作**，实现期先确认再决定是否保留。
5. **原子组（修订）**：`sanitize_pairs`（`context_compression.py:322-359`）**已结构性保证**
   "无孤儿 result、无无 result 的 call"，且**所有管线出口**都过它。
   → 原方案的"修法（吸附到 user message）"**非必需**（管线里不会产生非法请求）；
   **只保留管线级回归锁**（现有 `test_sanitize_*` 只到 stage 级）。

### C. M-E3 · 压缩预算可观测

**问题**：`CompactionEnded` 只有条数 + 摘要自身 usage；`/trace` 不渲染压缩；r7 的失败被非默认配置掩盖。

**修法**：

1. **扩展 `CompactionEnded`**（新增**可选**字段；**但见 §4.1 —— 前端要拿到就必须改 `to_wire`**）：

   ```python
   @dataclass(frozen=True)
   class LayerStat:          # 定义在 events.py（与 CompactionEnded 同处，asdict/to_wire 只认一种形状）
       name: str             # "L1" | "L2" | "L3" | "L4"
       removed: int          # 该层移除的消息条数（L1 snip / L2 信封化 / L3 持久化）
       before_tokens: int    # 进入该层时的估算 token
       after_tokens: int     # 离开该层时的估算 token
   ```

   * **字段口径（评审 P2-①）**：用 `removed`（条数）与 `before/after_tokens` **分列**，
     **不混用 `hits`** —— L4 是"摘要"不是"命中"，`hits` 对它无意义。
   * **顺序写死 = 管线顺序**（评审 P2-①）：`L3 → L1 → L2 → L4`（**不是**数值序）。
   * **不变量**：`Σ(after − before) == estimated_after − estimated_before`（避免两处口径漂移）。
   * **不带 `ratio`**（可由 `estimated_before/after` 推出）—— 不重复携带。
   * `estimated_before` / `estimated_after` 是**整次压缩**的首尾估算。
   * **plumbing（评审 P2-4）**：`Compactor._compress_once` 只从
     `self._context.last_summary_usage` 取数 —— 要填 `layers` / `estimated_before/after`，
     `ContextManager` 需**新增** `last_layer_stats` 之类的暴露，`Compactor` 再读它。**这一步必须写进实现清单。**
2. **`/trace` 渲染压缩**：`summarize_turn` 折叠 `CompactionStarted/Delta/Ended`，
   `render_turn` 增加一行"压缩：L1 −N 条 / L2 −M 条 / L3 −K 条 / L4 摘要 X→Y token（估）"。
3. **修测试载体（评审 P0-1 / P2-3）**：
   * **根因是测试读了未解析的配置**：r7 用例用 `AIConfig().max_context_tokens * 0.5`（= `0.0`），
     而真窗口是 `resolve_context_window(cfg)` = **48000**（阈值 **24000**）。
     **7058 是 24000 的 29%，没有预算缺口。**
   * **修法**：把判据载体换成 `resolve_context_window(cfg)`（或 `ContextManager` 解析后的窗口）。
   * **pytest 版必须改用 `AIConfig()` + `resolve_context_window`** —— 它现在用手搭
     `_Settings(max_context_tokens=48000)`，**并不复现**这个 bug；不换的话"搬进 pytest"
     只是把 bug 一起搬（评审 P2-3）。
   * `r7_cases.py` 的预算用例**接入 CI**（或等价地，把该断言搬进 pytest）。
   * **删掉"调 L2 窗口 / 调水位"的拍板项**（不存在缺口，无需调参）。
   * **`books_feed_the_policy` 移出 M-E** —— 它与压缩无关，是 domain B 的过期期望。
4. **前端压缩卡片**：显示每层命中数（数据来自 #1）。

### D. M-E4 · 前缀缓存友好度

**问题**：缺 **CI 里的离线门禁**；无"所有注入通道都按预算分页"的统一断言；探针不在 CI。
**且 `prefix_shared_ratio` 不能当硬阈值**（评审 P0-2 —— M-A6 已弃用百分比阈值）。

**修法**：

1. **统一分页断言**（新，`tests/test_context_compression_cache_friendliness.py`）：
   **枚举必须对齐 `build_tail` 的真实块**（评审 P1-b —— 原枚举与代码不符）：

   * **tail 通道（11 个，`TailBlock.name` 天然可枚举）**：
     `environment` / `memory` / `mcp_catalog` / `task` / `recommended_skills` /
     `referenced_files` / `error_recovery` / `anchor` / `hook` / `digest` / `budget`。
     * **删掉"检索片段"** —— 代码里**没有**这个通道（`referenced_files` 是文件内容、memory 是约束块）。
   * **system prompt 通道**：`_build_instructions`（project/agent instructions + methods + plan-mode + skills catalog）
     + `inject_task(task_injection="system")`。
   * **history 通道**：plan reminder。

   断言方式（评审 P2-④）：**对每个 tail 块断言"该块内容不出现在 stable 前缀里"**，
   或更省事的**"stable 前缀的字节在相邻两轮间不变"**（天然覆盖所有通道，不需要 per-channel 提取）。
   * **措辞更正（评审 P2-③）**：是"注入落在**可变区内**，从而**稳定前缀不被改写**" ——
     原写"注入位置在可变区之外"与括号内意图**正好相反**。
2. **用 M-A6 的布尔不变量，不用百分比阈值**（评审 P0-2）：
   目标形态是 `stable_b.startswith(stable_a)`（**路径无关**，不会在深路径工作区下 flaky）。
   * **但不要直接复用 case11 的 `stable()`**（评审 P1-c）—— 它的 volatile marker 只覆盖
     **5/11 个 tail 块**（`<environment>/<task-state>/<referenced-files>/<requested-skills>/[budget]`），
     而 `stable()` 是**从尾部 while 循环剥离**：第一个没 marker 的块就让它**停下**，
     于是它**前面**所有未标记的 tail 块（`<memory-constraints>`、`<working-memory>`、`<task-anchor>`、
     `<error-recovery>`、hook、`mcp_catalog`…）**全被留在 "stable" 里**。
     只要场景里 memory/hook/digest 非空，`startswith` 就会在**合法的易变内容**上失败 → **假红**。
   * **改用仓库已有的 `canonical_request`**（`core/trace.py:137-167`）—— 它正是
     `previous_request` 的口径，**不含 timestamp**、**确定性**。
3. **前缀稳定断言必须分口径**（评审 P1-4 / P2-⑩）：
   * **口径统一到 `canonical_request`（字节级、确定性）** —— case11 的 `stable()` 操作的是
     engine 记录的 `[message.content ...]`（**内容投影**），与"请求字节"**不是一回事**。
   * **`prefix_shared_ratio` 只能度量"同回合内相邻模型步"** —— 因为
     `previous_request` **每回合重置为 `None`**（`agent_loop.py:783`），它**测不了"连续两轮"**。
   * **跨回合**的稳定走 `canonical_request`，**两者不能混用**。
   * **三类误报触发点必须作为 skip 条件**：
     ① `task_injection="system"`（`todo_write` 会改系统提示）；
     ② **回合内触发压缩**（`compress_if_needed` 重写历史 → 前缀**合法**断裂）；
     ③ **instructions 变化** —— 实现为**"两轮 assembled instructions 字符串不同即 skip"**
     （一次字符串比较覆盖全部，评审 P2-⑧：窄枚举会漏掉 `mode` 与 `tools` 变化）。
   * **必须加"至少执行一次"的护栏**（评审 P2-⑨）：断言**非 skip 场景数 ≥ 1** ——
     否则三类 skip 可能让断言**vacuous pass**，没有任何信号。
4. **`cache_probe.py` 接入 CI**（或做一个**离线等价物**）：
   * 离线版：用 `canonical_request` 断言"同一会话连续两轮请求的**前缀完全相同**"
     （tail 整段排除）—— 不需要真实 provider。
   * 保留 `cache_probe.py` 作为**真实 provider 验证**的手工工具。
5. **时间戳陷阱扫描**（范围要精确，评审 P2-5）：
   断言**系统提示与工具定义**里没有秒级时间戳。
   **注意**：`now_local()` 出现在 environment **尾部块**（`request_assembly.py:393`）
   与 plan reminder 的 `timestamp` 字段（`canonical_request` **不渲染** timestamp）——
   **尾部块放时间戳是正确设计**，扫描**不能误伤**。

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
| 4b | **`server/bridge.py`** | `to_wire` 增 `layers` / `estimatedBefore` / `estimatedAfter`（**手写映射，不是 `asdict`** —— 不加就进不了前端） | M-E3（评审 P1-a） |
| 4c | **`frontend/src/state/session.ts`** | reducer + `Part` 类型增 `layers` | M-E3（评审 P1-a） |
| 4d | **`tests/test_compaction_events.py`** | `compaction.ended` 的 wire **整字典相等断言**会因新增键而红 → 放宽为子集断言 | M-E3（评审 P1-a） |
| 4e | `core/context_compression.py` | 新增 `last_layer_stats` 暴露（`Compactor` 读它） | M-E3（评审 P2-4） |
| 5 | `tests/test_context_compression.py` | M-E1 语义锁定（A1）或扩展断言（A2） | M-E1 |
| 6 | `tests/test_context_compression_l2_window.py` | **改用默认 `max_messages`**，让真实失败可见 | M-E3 |
| 7 | `tests/test_context_compression_cache_friendliness.py`（新） | 统一分页断言 + `prefix_shared_ratio` 阈值 + 前缀字节稳定 + 时间戳陷阱扫描 | M-E4 |
| 8 | `tests/test_context_compression.py` | 管线顺序不变量 + **按 token 判定** + 原子组 | M-E2 |
| 9 | `scripts/cache_probe.py` | 保留为手工工具（离线等价物进 #7） | M-E4 |
| 10 | `docs/features.md` / `docs/data-contracts.md` | M-E1 语义 + 修 `l4_keep_tail` 漂移 | 文档 |
| 11 | `docs/eval-04-longtask-mechanics.md` | 把"计数不可断言"的承认**固化** | M-E2 |

### 4.1 语义等价性

* **`CompactionEnded` 新增字段全部可选** → **后端消费方**（trace JSONL）不受影响。
  * **但前端卡片要拿到 `layers` 就必须改 `to_wire` + `state/session.ts`**（评审 P1-a）：
    `bridge.py` 的 `to_wire` 是**手写键映射**（不是 `asdict`），不加键就进不了前端；
    且 `test_compaction_events.py` 对 wire 做**整字典相等断言**，加键必红 → 要放宽为子集断言。
    → **"向后兼容"与"卡片显示每层"不可兼得**，必须同步改三处（见 §四 #4b/#4c/#4d）。
  * **trace JSONL 会多字段**（`trace.py` 用 `dataclasses.asdict`）—— 是**会变的**，只是不变坏。
* **M-E2 的判据口径**：`should_compact` **已按 token**（不改）；**成功判据保持内容真值 `result != history`**
  —— **本轮明确不改用 token 判成功**（那正是 Hermes bug 的形态：省了 token 但条数没变被判失败 → 假性耗尽 → 会话重置）。
* **M-E1 A1**：纯文档 + 测试，**零行为变更**。**A2**：行为变更（更多大输出被持久化）。
* **M-E4**：纯新增断言，**不改装配逻辑**（除非发现真的不达标）。

---

## 五、RIPR 用例

### Red（先写，必须变红）

| 编号 | 用例 | 期望红相（今天必须失败） |
|---|---|---|
| W1 | `test_l3_semantics_are_locked` | 断言"非尾部大输出**不**被持久化"（今天：无此断言） |
| W2 | `test_a_compaction_that_saves_tokens_without_changing_the_count_is_a_success` | 断言 `Compactor` 的成功判据是 `result != history`（**条数不变也算成功**）（今天：契约只隐含，无显式断言） |
| W3 | `test_the_pipeline_order_is_l3_l1_l2_l4` | 顺序不变量（今天：无断言） |
| W4 | `test_no_orphan_tool_result_survives_a_full_pipeline_compress` | **管线级**原子组（今天：只有 stage 级） |
| W5 | `test_compaction_ended_reports_per_layer_stats` | `layers` / `estimated_before/after`（今天：无字段） |
| W6 | `test_the_trace_renders_the_compaction_layers` | `/trace` 输出每层统计（今天：不渲染压缩） |
| W7 | `test_every_injection_channel_leaves_the_stable_prefix_alone` | 每通道**布尔不变量**（今天：无统一断言） |
| W8 | `test_a_turns_prefix_is_byte_stable_when_nothing_volatile_changed` | 跨回合前缀字节稳定（**skip 条件**：task_injection=system / 回合内压缩 / instructions 变化）（今天：无断言） |
| W9 | `test_the_system_prompt_carries_no_sub_second_timestamp` | 时间戳陷阱扫描（**范围排除尾部块**）（今天：无断言） |
| W10 | `test_the_r7_budget_case_uses_the_resolved_window` | 断言判据载体是 `resolve_context_window(cfg)`（今天：用未解析的 `AIConfig().max_context_tokens` → **恒 FAIL**） |

> **W2 的口径（评审 P0-3）**：**不是**"断言按 token 判定"（那恒绿），而是
> **显式断言成功判据是内容真值 `result != history`**，并把"条数不变也算成功"钉住。

### Prove

1. `.venv/bin/python -m pytest -q`
2. `.venv/bin/ruff check .` / `.venv/bin/mypy ai_coding`
3. **突变测试**：
   * 把 L4 挪到 L2 之前 → W3 变红
   * 把成功判据改回 `len(messages) 减少` → W2 变红
   * 让 `sanitize_pairs` 放行孤儿 result → W4 变红
   * 去掉 `layers` 字段 → W5/W6 变红
   * 把 memory 注入移到 system prompt 前缀 → W7/W8 变红
   * 把 W10 的判据换回 `AIConfig().max_context_tokens * 0.5` → W10 变红
4. **3.10 venv 跑 CI 同构七步**
5. **端到端**：`/trace` 看到压缩层统计

---

## 六、验收判据

* **M-E1**：L3 的语义**被文档化且被测试锁定**（A1）或**按扩展后的语义实现并锁定**（A2）。
* **M-E2**：压缩**触发**按 token、**成功**按**内容真值**（`result != history`），**不按条数**；
  管线顺序**有不变量单测**；tool_call/result **原子淘汰**（管线级回归锁）。
  * **本轮不改用 token 判成功** —— 那正是 Hermes bug 的形态（省了 token 但条数没变被判失败）。
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

**3 个 P0 · 4 个 P1 · 5 个 P2 · 3 个 P3（全部已修，见 §8.1.1）**

**已实测验证**（评审/我逐条复核）：

| 断言 | 方式 | 结果 |
|---|---|---|
| L3 只取末尾连续 `tool` 段 | 实跑 | ✅ 属实（非尾部落盘=0） |
| L2 信封被 L4 吃掉 | 实跑 `compress(force=True)` | ✅ 属实（压缩后信封数=0） |
| **`r7` 的 7058 不是预算缺口** | 实跑 | ✅ `AIConfig().max_context_tokens=0` → 阈值 `0.0`；真窗口 48000 → 阈值 24000；`7058 <= 24000` = **True** |
| **"按 token 判定"前提不存在** | 实跑 | ✅ `should_compact` 本就是 token；成功判据是 `result != history`；"条数不变"已判成功 |
| **`prefix_shared_ratio` 不能当硬阈值** | 对照 M-A6 | ✅ 分母含易失尾部 → 深路径下 77%；M-A6 已弃用百分比阈值 |

#### 8.1.1 本轮 findings 的处置（全部已修）

| 级别 | 问题 | 修法 |
|---|---|---|
| **P0-1** | **M-E3 根因诊断错误** —— 7058 **并不** > 24000。r7 用例读了**未解析的** `AIConfig().max_context_tokens`（默认 0）→ 阈值 `0.0` → FAIL。真窗口 48000，7058 是 **29%** | §2.3 / §C.3 / W10 全部重写：**修法是改判据载体**（`resolve_context_window`），**删掉"调 L2 窗口/水位"的拍板项**；`books_feed_the_policy` **移出 M-E**（与压缩无关，是 domain B 过期期望） |
| **P0-2** | **M-E4 的 `prefix_shared_ratio ≥ 0.9` 与 M-A6 直接冲突且不可达** —— 分母含易失尾部，深路径下实测 **77%**；M-A6 已**明确弃用百分比阈值** | §D.2 改为复用 `memory_cases` case11 的**布尔不变量**（`stable()` + `startswith`）；§2.4 措辞改为"缺 CI 里的离线门禁" |
| **P0-3** | **M-E2"按 token 判定"的前提不存在** —— `should_compact` 本就是 token 判据；成功判据是内容真值 `result != history`；全仓**没有** `len(messages)` 版判据可替换 → 原 W2 **恒绿** | §B 重写：改为**文档化现有判据 + 契约测试锁住内容真值**；W2 重写为"断言成功判据是 `result != history`（条数不变也算成功）" |
| P1-1 | M-E1 的 A1 站得住；A2 的真实风险不是"破坏护栏"，而是**去掉"最后手段"判据**（`total > window/2`）→ 变成无差别 30KB 阈值 + **未定义范围/顺序/预算** + **缩小 L2 覆盖** | §A 改为**只推 A1**；A2 若要保留须**先补规格**（范围/顺序/预算/L2 交互），不与 A1 并列 |
| P1-2 | 把无关的 `books_feed_the_policy` 失败算作 M-E 证据 | 移出（见 P0-1） |
| P1-3 | "`prefix_shared_ratio` 未被任何阈值测试守护"**不准确** | §2.4 更正：已有 `>0.5`（CI）、0.9/0.95（测试）、`>=0.9`（`--real`）；缺的是**CI 离线 0.9 门禁** |
| P1-4 | **W8 前缀字节稳定断言会因正常轮转误报**（`task_injection="system"`、回合内压缩、`_build_instructions`）；且 `previous_request` **每回合重置** → `prefix_shared_ratio` **测不了"连续两轮"** | §D.3 重写：**分口径**（同回合相邻步 vs 跨回合确定性序列化）+ **三类误报触发点作为 skip 条件** |
| P2-1 | M-E2 W3 锁的是实现细节；roadmap 原意是"明确顺序是刻意的" | §B.3：**以文档为主、单测作回归锁** |
| P2-2 | M-E2 W4"原子组"**已被 `sanitize_pairs` 结构性保证**；"修法（吸附到 user message）"是误判 | §B.5：去掉修法，**只保留管线级回归锁** |
| P2-3 | M-E3"把 r7 断言搬进 pytest"**必须用 `AIConfig()`**，否则把 bug 一起搬 | §C.3 写明 |
| P2-4 | `CompactionEnded` 加 `layers` **缺 plumbing**（`Compactor` 只读 `last_summary_usage`） | §C.1 补：`ContextManager` 新增 `last_layer_stats` 暴露 |
| P2-5 | M-E4 W9 时间戳扫描**范围要精确**（尾部块的 `now_local()` 是正确设计，不能误伤） | §D.5 补范围说明 |
| P3-1 | 文档漂移已确认，修法正确 | §E 保留 |
| P3-2 | M-E1 证据链成立（`eval-04`：`persisted=0`） | ✅ |
| P3-3 | M-E3"`/trace` 不渲染压缩"属实 | ✅ |

### 8.2 方案评审（子智能体，第 2 轮）

**1 个 P0 · 4 个 P1 · 10 个 P2/P3（全部已修，见 §8.2.1）**

#### 8.2.1 本轮 findings 的处置（全部已修）

| 级别 | 问题 | 修法 |
|---|---|---|
| **P0** | **§4.1 + §六 仍写"成功判据按 token"，与 §B/W2 的"内容真值"直接矛盾** —— P0-3 的修正**没同步**。若实现者按 §六 验收，会把成功判据改成"token 下降"，**那正是 Hermes bug 的形态** | §4.1 / §六 都改为"**触发按 token、成功按内容真值 `result != history`、不按条数**" |
| P1-a | **`layers` 到前端卡片缺 plumbing**：`to_wire` 是**手写键映射**、`state/session.ts` 要改 reducer、`test_compaction_events.py` 的**整字典相等断言**必红 → **"向后兼容"与"卡片显示每层"不可兼得** | §四 补 **#4b/#4c/#4d**；§4.1 措辞改正（并说明 trace JSONL 会多字段） |
| P1-b | **M-E4 通道枚举与 `build_tail` 的 11 块不符**（漏 7 个块 + system/history 通道；"检索片段"**无对应通道**） | §D.1 改为**对齐 `TailBlock.name` 的 11 个块** + system prompt 通道 + history 通道；删掉"检索片段" |
| P1-c | **`stable()` 的 marker 只覆盖 5/11 块** —— 这才是 W8 误报的**真正来源**（`stable()` 从尾部 while 剥离，遇第一个无 marker 的块就停，前面的未标记块全被留在 "stable" 里） | §D.2 改为**用 `canonical_request`**，不复用 case11 的 `stable()` |
| P2-① | `LayerStat` 把"条数/token"混在一个字段集；L4 的 `hits` 无意义；`layers` 顺序未定义 | §C.1：`removed` + `before/after_tokens` 分列；顺序写死 = 管线顺序 `L3→L1→L2→L4`；补 Σ 不变量 |
| P2-② | `LayerStat` 定义位置未指定，依赖方向未进清单 | §C.1：明确放 `events.py` |
| P2-③ | "注入位置在可变区之外"与括号内意图**相反** | §D.1：改为"注入落在**可变区内**，从而稳定前缀不被改写" |
| P2-④ | 缺"如何判定通道在可变区内"的可执行定义 | §D.1：给出两种（per-channel 提取 / **整段 stable 前缀字节不变**） |
| P2-⑤ | W3 顺序不变量的观测手段未定义（L2 是模块函数、L4 是绑定方法，注入方式不同） | §B.3：给出 monkeypatch recorder 具体形状 |
| P2-⑧ | W8 的 skip ③ 是窄枚举，漏 `mode` / `tools` 变化 | §D.3：实现为**"两轮 instructions 字符串不同即 skip"** |
| P2-⑨ | 缺"必须执行"护栏 → **vacuous pass** 风险 | §D.3：断言**非 skip 场景数 ≥ 1** |
| P2-⑩ | §D.2 是 content 级、§D.4/W8 是字节级，口径不一致 | §D.3：**统一到 `canonical_request`** |
| P3-⑥ | W2 用真实管线造"条数不变"条件苛刻 | §B.2：用 fake delegate（`_SilentContext` 变体） |
| P3-⑦ | §B.4"删掉条数断言"可能**空操作**（现存断言都是 token 判据） | 先核实再写 |
| P3-① | 未带 `ratio`；`estimated_before/after` 与 layers 口径重叠 | §C.1：不带 ratio（可推）；补 Σ 不变量 |

### 8.3 实现评审（子智能体）

_待填。_
