# 压缩机制审查 — 待办清单（R11）

> 来源：2026-10-13 真机验证（`/tmp/opencode/tui-verify/shots/`）+ `/home/linsir365/projects/opencode` @ `0a46301e3`（v2.0.22）源码对照。
> 定位：这是**审查产出**，不是方案。每条都注明证据与影响面，实施另写 `docs/r11-analysis.md`。
> 状态：`✅` 已修 · `⬜` 未动。

---

## 0. 已修（记录在此以免重复踩）

| ✅ | 问题 | 提交 | 根因一句话 |
|---|---|---|---|
| M11-0 | `/compact` 只产出 `[snipped N messages]` | `eb283df` | L1 snip 先于 L4，被删内容在摘要时已不存在 |
| M11-0b | 摘要器收到的是占位符而非真实历史 | `ad466af` | `_run_l4(out)` 的 `out` 已是 snip 后的列表 |
| M11-0c | 边界一次都没写过 | `ad466af` | `session.turn_number` 字段不存在，真名 `turn_count` |
| M11-0d | 压缩后 meter 不刷新 | `ad466af` | 只在 `turn.finished` 刷新，`/compact` 无信号 |
| M11-0e | 闸门读字符估算，偏差 4 倍 | `ad466af` | `estimate_tokens` 对 ASCII 密集内容失准 |
| M11-0f | 边界从不落盘 | `ad466af` | `to_dict` 白名单漏了 `last_compacted_turn` |
| M11-0g | **压缩后立刻再压一次** | `1053356` | 闸门读 `usages[-1]`，未按边界过滤 |

> M11-0g 的教训：同一条规则（"哪些 usage 仍描述当前上下文"）被写了两遍，两边漂移。
> 现在收敛到 `SessionData.live_prompt_tokens()`，两个读者共用。

---

## 1. 🔄 P0 — 二次压缩不合并旧摘要

> **2026-10-13：已实施。** 方案见 `docs/r11-analysis.md`（含拍板决策 §12）。提交 `（见 git log）`。

**现象**：第 N 次压缩时，L4 只对**当前仍在 head 里的消息**做摘要。第 1 次摘要覆盖的内容早已不在历史里，被静默丢弃。

**为什么是 P0**：这是清单里**唯一会造成不可逆信息丢失**的。其余都是"不够好"，这个是"越用越瞎"。

**OpenCode 的做法**：`compaction.ts:266` `previousCompaction(messages)` 取出上一次 compaction 消息；`buildPrompt` 走 `update` 分支（`:142-173`）——
> "Update the existing checkpoint in the conversation above into one consolidated summary." + "Return only the updated Markdown sections."

即：把**旧摘要 + 新增内容**一起送进去，产出**一份合并后的 checkpoint**，而不是再叠一层摘要。

**我们的现状**：`context_compression.py::_run_l4` 只调 `self._summarizer(head_text)`，无状态、无前次摘要概念。

**实施结果**：`_run_l4` 用 `split_checkpoint()` 把旧 note 从 head 摘出，拼成
`<previous-checkpoint>` / `<conversation-since-checkpoint>` 两段；提示词无条件描述两段含义
并给出五条合并规则；模板换成 OpenCode 七节；输出加增长判据（带下限）。
真机两轮压缩验证：旧 checkpoint 的两条关键事实在第 2 轮合并后存活，上下文里始终只有一份 checkpoint。

**实施中真机抓到、测试没抓到的缺陷**：模板里把三个子标题排在七个顶级节**之后**，
模型据此把它们追加到了最后一个节（`## 重要上下文`）底下。测试只断言"节存在"没断言位置。
已修并补 `test_the_work_state_subheadings_sit_under_their_parent_in_the_prompt`。

---

## 2. ✅ P1 — 同一问题被答两遍（原描述"不校验模板"已过时）

> **2026-10-13 已修。** 校验早就存在（`summary_is_usable` + NUDGE + 两轮重试）。真实缺陷是**接受与识别两个答案不一致**：`summary_is_usable` 用子串匹配（宽），`split_checkpoint` 用前缀匹配（窄）。实测三种形态通过前者却被后者拒绝，最严重的是**带前言的摘要**——被装成 checkpoint，下一轮认不出，R11 的合并功能对该 session 静默失效。
>
> 修法：一个 `checkpoint_body()`，两个查询方。整行匹配（非子串），返回首个节标题起的正文（前言剥离而非拒绝）。`summary_is_usable` 删除，不并存。
> 详见 `docs/r11-analysis.md`。

`compaction.ts:268-269` 只要**任意一行**等于模板里的二级标题就算通过；不合格时 `:280-282` 追加一次 NUDGE 重试，仍不合格才失败。

我们 `_run_l4:457-461` 只判 `if not summary: return messages`，模型返回一段散文照样接受。

**代价**：上下文里会出现一段没有固定结构的自由文本，下游（尤其是将来的二次合并）无从下手。

---

## 3. ✅ P2 — 摘要请求不会因"太大"而收缩

> **2026-10-13 已修（反应式，不预测）。** 方案见 `docs/r15-summary-shrink.md`。
>
> `llm_helpers.py`：`_SHRINK_STEPS = (0.7, 0.5, 0.35)`，锚定**首次实际发送大小**
> （`len(_clip_for_summary(text))`），四次耗尽后放弃。触发是**反应式**——复用
> `openai_engine._looks_like_context_overflow` 判拒因（与主循环同一条分类规则），
> 不做预测式收缩（`estimate_tokens` 在 `core/`，ai/ 引用即违反单向依赖）。
> 非 overflow 异常 / 形状失败 / 增长超限——全部行为不变（不收缩、不加重试）。
> 验证：`tests/test_summary_shrink.py` 8 用例，其中 R2 让真 `AsyncOpenAI` 对本地真
> HTTP 服务器发请求（首 POST 回 400 `context_length_exceeded`），断言第二次
> wire payload 更小。

---

## 4. ✅ P2 — overflow 兜底不检查是否已开始输出（**前提不成立，已关闭**）

> **2026-10-13 核验：保护早就存在，只是在上面一层没看出来。**
>
> `openai_engine.py:428-439`：`_stream` 记录每个已投递的 chunk（`text_parts` / `acc`），
> 之后任何失败都抛 **无 kind** 的 `EngineError`——
> `if text_parts or acc: raise EngineError(f"stream failed after {len(text_parts)} text chunk(s)…")`。
> 无 kind 的失败永远匹配不上 `exc.kind == "context_overflow"`，所以**流已输出后被拒，本来就不会重试**。
>
> 唯一两个 engine 实现是 `OpenAIChatEngine`（有上述保护）和 `FallbackEngine`（纯委派）。
> 脚本化的假 engine 可以造出"先 on_delta 再抛 typed overflow"，但没有真实 engine 能产生它。
>
> **结论**：债务清单这条是从 `agent_loop.py:528` 单点读出来的，漏了下游的既有多温护。
> 未加代码（加了就是同一规则的第二份拷贝，且没有调用方走那条路）；
> 改为补回归锚点 `tests/test_overflow_safety.py`，其中驱动**真 engine** 端到端验证。

---

## 5. ✅ P3 — headroom 无下限（2026-10-14 已按 OC 对齐）

~~我们：`soft = max_context_tokens * compress_soft_ratio`（默认 0.8，即留 20%）。~~

现在：`compaction_ceiling(window) = window - max(⌊10%⌋, window≥32k ? 16k : 0)`
（`core/context_compression.py`），与 OpenCode `compaction.ts:895` 同式；
`compress_soft_ratio` 配置项已删除。32k 模型 25.6k→16k 触发（更勤），
200k/1M 模型 160k/800k→180k/900k（更晚）。`max(observed, estimate)` 度量保留。

> 反向记录 OpenCode 的一个 footgun：`calculateCeiling` 不校验 `window - buffer >= 0`，设大了会退化成"每步压一次且压不动"。我们没有这个坑。
> 另保留一点不同：未知窗口 OC 取 INF（auto 永不触发），我们仍取 48k 保守默认。

## 6. ✅ P3 — 压缩事件粒度（2026-10-14 四事件已落地）

~~我们只发 `ContextCompacted`（含 before/after 消息数与字符数）。~~

现在发五个：`ContextCompacted`（保留：它是"历史被重写"的事实，books/测试依赖）
+ `CompactionStarted(reason)` / `CompactionDelta`（ephemeral，桥 150ms 节流）
/ `CompactionEnded(reason, counts, 自身 usage, 全文）` / `CompactionFailed(reason, error, usage)`。
漏斗以内容相等判改写：无变化返回原对象 + Failed，不再有 N→N 的 phantom Ended；
异常路径的重复 Failed 已去重（对象同一性区分）。

**注意**：OpenCode 的 `ended.tokens` 注释明写是**压缩请求自身的消耗**，"not the size of the resulting context" —— 不能直接拿来喂 meter。
我们同样独立字段，meter 仍读 `live_prompt_tokens`（压缩后归 0，下 turn 实测恢复），
Ended 行内写消耗（双数都显；`session.ts` 压缩行）。

## 7. ✅ P3-2 重构附带（2026-10-14）

- force 直达 L4：`force` 同时 bypass L4 内门（`_l4_should_run`），手动/溢出必出摘要；
  空输入守卫（`_l4_since_empty`）防第二次 `/compact` 烧两次调用。
- token 预算 tail：`split_tail`（`min(l4_tail_tokens=15k, window//4)` + user 边界回退 +
  至少 1 条；全 fit 只留最新 exchange，其余全总结——OC `split` 同规则）。
  `l4_keep_tail`（条数）配置项已删除。
- L4 输入排除 verbatim tail（对象同一性排除）+ 摘要成功时去 snip marker
  （失败/无摘要时 marker 保留，M11-0 诚实性不变）。
- `/compact` 成功时控制台 `[compact] N→M` 消音（转录行已有全文+计数）；无变化时保留诊断行。

## 8. ✅ P3-4（2026-10-14）：累积转录 + 触发对齐 + 失败去重

**触发机制与 OpenCode 的最终差额（全部收口）**：

- `auto_compact: bool = True`（`ai.` 段，OC `settings.auto`）：False 只关主动路径
  （`request_assembly` 一处 gate，无调用无事件）；`/compact` 与 overflow 救援不受影响
  —— 比 OC 更安全，OC 连 overflow 一起关。
- 并发去重（OC inbox merge 的等价物）：`Compactor._lock` 一把锁覆盖两个入口
  （turn assembly 与 command）；`compact_now` 在**锁内取快照**，第二个 `/compact`
  等待后基于压完的历史发现无事可做—— 一次模型调用，两份诚实报告。
- **skip ≠ failure**：漏斗以"是否真的调过摘要器"（`last_summary_attempted`）区分：
  没事可做 → `CompactionSkipped`（muted `○ 无可压缩内容`，tmux 实证）；
  摘要失败 → `CompactionFailed`（红）。console `[compact] N→N` 诊断行保留。
- 溢出首枪 estimate×0.7：不做（我们的摘要器自带反应式 0.7/0.5/0.35 收缩，更强）；
  native provider compaction：不做（deepseek/qwen 不支持）。
- 单 exchange `/compact`：按 B 案不烧调用（OC 是全总结——已评估并放弃：
  摘要成本 > 原文，且刚发生的对话不应被有损替换）。

**UI 缺陷修复（tmux 实测驱动）**：

- failed 双报：一次失败曾出现两条同文案红行（failed 卡 + 冗余 notice）——notice 砍掉。
- 会话预览污染：`_first_user_preview` 跳过 checkpoint 笔记（`CHECKPOINT_MARK`
  上移 `domain/message.py`，与压缩器/前端共用一处定义）并截 80 字符；
  tmux 实证 header 现显示 tail 中真用户消息。

---

## 7. 我们比 OpenCode 好的地方（勿在实施中"对齐"掉）

| | OpenCode | 我们 |
|---|---|---|
| 摘要失败 | `llm.ts:218` 整个 step 失败 | 降级为原始历史，不中断 turn |
| L4 连续失败 | **无断路器**（全仓无 `circuit`） | `_L4_BREAKER_THRESHOLD` |
| 压缩后 UI | 等下一次 assistant 回复才变 | 服务端算好，命令返回即刷新 |
| tail 重复发送 | `compaction.ts:71` TODO 承认发两遍 | 只发一次 |