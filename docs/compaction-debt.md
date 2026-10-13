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

## 2. ⬜ P1 — 摘要不校验模板

`compaction.ts:268-269` 只要**任意一行**等于模板里的二级标题就算通过；不合格时 `:280-282` 追加一次 NUDGE 重试，仍不合格才失败。

我们 `_run_l4:457-461` 只判 `if not summary: return messages`，模型返回一段散文照样接受。

**代价**：上下文里会出现一段没有固定结构的自由文本，下游（尤其是将来的二次合并）无从下手。

---

## 3. ⬜ P2 — 摘要请求不会因"太大"而收缩

`compaction.ts:420-427`：`context-overflow` / `payload-too-large` 时按 `SHRINK_STEPS = [0.7, 0.5, 0.35]` 逐级收缩重发，四次后放弃。

我们只有 `hard_trim` 这一个降级，且是在摘要**之后**作用于历史，不作用于摘要**请求**本身。

**触发条件**：历史本身能塞进窗口，但"旧摘要 + 待摘内容"装不下。第 N 次压缩时才会真正需要这条 —— 与 P0 强相关。

---

## 4. ⬜ P2 — overflow 兜底不检查是否已开始输出

`step.ts:108-115` 要求 `!publisher.record().outputStarted` 才允许压缩；已开始输出则直接放弃。

我们 `agent_loop.py` 的 `_CONTEXT_OVERFLOW` 兜底没有这个前置判断。

---

## 5. ⬜ P3 — headroom 无下限

我们：`soft = max_context_tokens * compress_soft_ratio`（默认 0.8，即留 20%）。

OpenCode `compaction.ts:895`：`window - max(window * 0.1, window >= 32k ? 16_000 : 0)` —— 留 10%，但**有 16k 保底**。

小窗口模型上我们留得更多（安全），大窗口模型上留得一样多比例但绝对值大得多（够用）。目前无实际问题，仅记录口径差异。

> 反向记录 OpenCode 的一个 footgun：`calculateCeiling` 不校验 `window - buffer >= 0`，设大了会退化成"每步压一次且压不动"。我们没有这个坑。

---

## 6. ⬜ P3 — 压缩事件粒度

我们只发 `ContextCompacted`（含 before/after 消息数与字符数）。

OpenCode 发四个：`started`(durable) / `delta`(ephemeral) / `ended`(durable，含 cost+tokens) / `failed`(durable)。

**注意**：OpenCode 的 `ended.tokens` 注释明写是**压缩请求自身的消耗**，"not the size of the resulting context" —— 不能直接拿来喂 meter。

---

## 7. 我们比 OpenCode 好的地方（勿在实施中"对齐"掉）

| | OpenCode | 我们 |
|---|---|---|
| 摘要失败 | `llm.ts:218` 整个 step 失败 | 降级为原始历史，不中断 turn |
| L4 连续失败 | **无断路器**（全仓无 `circuit`） | `_L4_BREAKER_THRESHOLD` |
| 压缩后 UI | 等下一次 assistant 回复才变 | 服务端算好，命令返回即刷新 |
| tail 重复发送 | `compaction.ts:71` TODO 承认发两遍 | 只发一次 |