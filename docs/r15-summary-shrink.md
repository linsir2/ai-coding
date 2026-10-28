# R15 — 摘要请求收缩（SHRINK_STEPS，反应式）

> 定位：`docs/compaction-debt.md` §3。实施记录，不是方案。
> 依据：`ai_coding/ai/llm_helpers.py`（`build_summarizer`/`_clip_for_summary`/`_SHRINK_STEPS`）· `/home/linsir365/projects/opencode` `compaction.ts:92,421-426`。

## 1. 问题陈述

L4 摘要是一个与主循环**共用窗口**的 chat 请求。`_clip_for_summary` 的 60k 字符上限在纯
CJK 历史上约 30k token，加上七节系统提示词——32k 窗口模型上，摘要请求本身即可超窗。

旧 `summarize` 对**任何**异常返回 `None`，随后主请求带着"完整的 trimmed 历史"出战——
而那正是刚被拒绝的大小。turn 死亡，且不是"降级"，是结构性卡死。

## 2. 对照参考：OpenCode 怎么做

`compaction.ts:421-426`：当 `isContextOverflowFailure(err) || isPayloadTooLarge(err)`，按
`SHRINK_STEPS = [0.85, 0.7, 0.5, 0.35]` 逐级收缩重发，最后一次拒绝后放弃。（I1/R-6 补上了 0.85 首档：首枪更温和，`0.7` 会为了换一次重试扔掉 30% 对话。）

`rejected ??= estimateRequest(request)`（`:421`）——锚定的是**首次被拒的请求大小**，
不是原始历史；收缩沿"消息数量/token 预算"两轴同时收（`:425-426`）。

## 3. 我们的三条路线与取舍

| 路线 | 做法 | 否决原因 |
|---|---|---|
| 预测式 | 发前 `estimate_tokens`，超窗先裁 | `estimate_tokens` 在 `core/`，ai/ 引用即违反 core→ai 单向依赖；CJK/ASCII 混排时估算本就失准（M11-0e） |
| 对齐式 | 照抄 OpenCode 全部逻辑（含 payload-too-large） | 我们没有"消息数"轴（摘要请求是拼好的文本），409/413 在 `_looks_like_context_overflow` 里已被 mine 掉——抄第二轴是死逻辑 |
| **反应式（采用）** | 发→被拒→判因→收缩→重发 | 触发与主循环**同一条**分类规则；零新判据、新引入类别只有 cap 阶梯 |

拍板记录：触发**反应式**（OpenCode 对齐）；增长判据失败**不收缩**（仍走 NUDGE 轴，
输入缩小修不好输出跑偏）；R2 用**本地真 HTTP 服务器**验证。

## 4. 实现

`ai_coding/ai/llm_helpers.py`：

- `_SHRINK_STEPS = (0.85, 0.7, 0.5, 0.35)` —— 只此一个新常量。
- `_clip_for_summary(text, cap=_MAX_SUMMARY_CHARS)` —— `cap` 参数带默认值，向后兼容
  （`tests/test_compaction_checkpoint_merge.py:258` 的单参调用不动）。
- `summarize` 改为**外层 cap 阶梯 × 内层 NUDGE 双轴**：
  `caps = [len(base), len(base)×0.7, ×0.5, ×0.35]`，`base = len(_clip_for_summary(text))`
  即首次实际发送大小。
  - 异常且 `_looks_like_context_overflow(exc)` → `break` 到下一级（太小重试）。
  - 异常且非 overflow → 直接 `return None`（大小没问题，收缩帮不上忙；
    限流/网络错误走原来的 one-shot 语义，I2 锁定）。
  - 形状失败 / 增长超限——两轮 NUDGE 耗尽后 `return None`，**不进入**下一级
    （I3 锁定；`calls[1].startswith(calls[0])` 断言历史前缀未被动过）。
  - 末级仍 overflow → `None`；保持"降级为原始历史、不中断 turn"（I1 锁定 5 次调用）。
  - `max_tokens`（`_CREATE_TOKENS`/`_MERGE_TOKENS`）按 `previous` 选定后**在整个阶梯内不变**
    ——收的是输入预算，不动输出预算（P1 锁定）。

## 5. 测试（`tests/test_summary_shrink.py`，8 用例）

| # | 用例 | 断言的失败含义 |
|---|---|---|
| R1 | 首次 overflow→收缩→成功 | 缺陷本身：老代码返回 `None` |
| R2 | 真 `AsyncOpenAI` → 本地真 HTTP（首 400 `context_length_exceeded`） | wire 上第二次请求字节数更小；防"mock 自证" |
| I1 | 五级全拒 | 5 次调用、严格递减、`None` |
| I2 | 非 overflow（RateLimit）异常 | `None`、只 1 次调用（分类错误会多烧 3 次） |
| I3 | 形状失败 ×2 | `None`、恰 2 次调用、历史前缀不变（误收缩会多烧） |
| I4 | 首调成功 | 1 次调用 |
| I5 | `_clip_for_summary` cap 参数 | 兼容性契约 |
| P1 | merge 路径 overflow + 收缩 | 收缩且 `max_tokens` 两次都是 `_MERGE_TOKENS` |

RED 基线（实施前跑全文件）：R1/R2/I1/I5/P1 挂，I2/I3/I4 绿（引脚）。
实施后 8/8 绿；全量 `pytest` 仅剩 `test_loop_digest.py` 两个预存失败（基线已知）；
`ruff check ai_coding/` + `mypy ai_coding/` 干净。

## 6. 观察到但未修（超范围）

`context_compression.py:559`：`_run_l4` 调 `self._summarizer(head_text)` **不传 `previous`**，
故 `_MERGE_TOKENS(1600)` 与"请**更新**"分支在生产侧从不触发——merge 由内嵌
`<previous-checkpoint>` 块驱动。提示词语气与预算错配，但行为正确，不在此修。
