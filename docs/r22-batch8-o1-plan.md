# 批次 8-7 方案（R22·O-1 重复调用守卫挡住"重复读同一段来累加"）

> 只出方案，不改代码。依据：`docs/r22-batch8-queue.md` 第 7 项、`docs/r4-analysis.md` §5.2/§8、
> `/workspace/eval-workspace/r22-s3/.ai-coding/traces/8910e125…/turn-1.jsonl`。

## 1. 调研：把"模型自述"还原成轨迹里发生的事

S3 收尾原话："由于 `read` 工具的重复调用守卫（`duplicate_call`）阻止了对同一参数的多次调用，
我无法按 500 行一 chunk 的方式逐块累加统计。"轨迹里逐条对得上，但因果不是它说的那样：

| 迭代 | 调用 | 结果 |
| --- | --- | --- |
| 1 | `read data/big.txt offset=0/500/…/5500`（12 个不同区间，一批） | 12 次全部成功 |
| 2 | `read offset=0 limit=500` | 成功（该指纹第 2 次） |
| 3 | 同上 | **`guard_denied`**："already succeeded 2 time(s)… reuse the earlier result"，指向 iter1 的 call |
| 4 | `bash grep -c 'ERROR' …` | 成功，**得到正确总数 162** |
| 5/6/8/10 | 同上 `read offset=0` | 继续 `guard_denied` |
| 收尾 | — | `MAX_ITERATIONS`；答案里的 162 正确，"逐 chunk 明细"没给出 |

三个事实：

1. **守卫没有挡"逐块读"**：12 个不同 offset 的指纹各不相同（参数一变指纹就变，
   `r4-analysis.md:532`），iter1 全部放行。被挡的只有"完全相同参数"的重复。
2. **正确路径没被挡**：iter4 的 `grep -c` 拿到了 162，"换个工具"一直可用。
3. **真正被挡的是"同一段再读一次"，而 iter3 时上下文里已经没有那次的结果**：
   - compaction #1（iter2 前）：`L3 rewritten=12`——每个 500 行块约 40 KB，超过
     `per_result_persist_bytes=30000`，12 条结果全被换成 `<persisted-output>` 标记 + 2000 字预览；
   - compaction #2（iter3 前）：`L3 rewritten=1`、**`L4 removed=13`**，`history_messages` 只剩 4 条
     （checkpoint + 两条 assistant + iter2 那次读的 `<persisted-output>` 标记，3081 字符）——
     iter1 的 12 个 call id 已完全不在 history，iter2 的结果只剩 2000 字预览，**都不足以复用**。

   也就是说 iter3 的拒绝理由"reuse the earlier result"指向一个**上下文里已不可复用**的结果。

### 为什么不能简单放开

`read` 若整体豁免，模型可以无限重读同一区间（R20 之前同一文件读 11 次的真实病）。
守卫要防的正是这个循环，所以只能**在"没有可复用结果"时放行，并保留总量上限**。

### 为什么"逐 chunk 累加"本来也不成立

单块 500 行 ≈ 40 KB > `per_result_persist_bytes`，L3 当场把它换成 2000 字预览；12 块更放不进
48k 窗口，L4 会把它们全部摘要掉。要逐块明细，正确做法是"读一块就把计数写进 assistant 文本"
（一次一块），或用 `grep -c`/`awk`。这是上下文预算的问题，不是守卫能解决的（见 §5）。

## 2. 方案

**FIX-O1-1：守卫按"结果还在不在这次请求里"计数（不动 TurnView）。**

`ToolContext.messages` 已经是**本次请求装配后**的 history（`request_builder.py:117`
`messages=tuple(history)`，`tools/base.py:73-78` 明确写着"the part the model can see"），
守卫是同步纯函数，读它不引入 IO。在 `core/guard.py` 加一个模块内 helper：

```python
#: 正文仍可复用的 role=tool 消息 id。L2 信封、L3 持久化标记都不算；
#: L4 摘要删掉的消息不在 history 里，自然也不算。
def _visible_tool_ids(history: Sequence[ChatMessage]) -> frozenset[str]:
    # 跳过 tool_call_id 为 None 的消息（ChatMessage 允许 assistant/user 不带 id）；
    # 判定用 content.startswith(("[Previous: used", "<persisted-output"))，不要用 in。
    ...

#: 可以"结果丢了就重读"的工具：正文即结果、重执行无副作用。
_REREADABLE = frozenset({"read"})
#: 同一指纹的总成功次数上限 = threshold 的倍数。
_UNVISIBLE_REREAD_FACTOR = 2
```

**FIX-O1-2：`DuplicateCallGuard` 的判据与状态。**

- 状态由 `Counter[fingerprint]` 改为 `dict[fingerprint, list[call_id]]`（成功按序追加；
  失败仍整条删除，保持"失败不占预算"的既有契约）。
- `check` 分两种：
  - **非 `read`**：行为不变，总成功次数达到阈值即拒绝（副作用工具不能因"输出被裁剪"重跑）。
  - **`read`**：
    - `visible` = 该指纹的成功 call id 中，仍出现在 `ctx.messages` 可见结果里的那些；
    - `len(visible) >= threshold` → 拒绝（这时"复用"确实成立，沿用原文案并指向最早可见的 call）；
    - `total >= threshold * _UNVISIBLE_REREAD_FACTOR` → 拒绝，**换一句真话**：
      "同一读取已成功 N 次，先前输出已不在上下文里；换个行段读，或搜索/计数用 grep"；
    - 其余放行（结果不可复用了，重读是恢复内容的唯一方式）。
- 阈值 0 关停、per-turn reset、`error="guard_denied"`、前端展示全部不动。

为什么这仍是"防循环"的：模型若只重读同一段，第 `2×threshold` 次后必被拒；它若在读别的区间
（指纹不同）则本来就不受这条守卫约束。

**FIX-O1-3：契约文档同步。**

`docs/r4-analysis.md` §5.2 的守卫表与 §8 的"误伤"条目补一句：
"`read` 的重复只在**上次结果仍在本请求上下文里**时才算重复；结果被 L3/L4 移走后按总次数上限
（2×阈值）兜底，拒绝文案不再要求复用不存在的结果。"

## 3. 测试（先红后绿）

| 测试 | 断言 |
| --- | --- |
| `tests/test_guard.py::test_duplicate_guard_allows_a_reread_whose_output_left_the_context` | `read` 两次成功（id `a`/`b`），`ToolContext(messages=())` → 第 3 次 ALLOW（今天 DENY） |
| 同文件 `..._still_denies_while_the_earlier_result_is_visible` | `messages` 含 `b` 的普通 tool 消息 → DENY，理由指向 `b`，且含"reuse the earlier result" |
| 同文件 `..._caps_rereads_whose_output_is_gone` | threshold=2，4 次成功后第 5 次 DENY，理由含"已不在上下文"且**不含**"reuse the earlier result" |
| 同文件 `..._cap_scales_with_the_threshold` | threshold=1：2 次成功后第 3 次 DENY（上限=2×1） |
| 同文件 `..._does_not_excuse_a_trimmed_bash` | `bash` 两次成功、`messages=()` → 仍 DENY（副作用工具不放行，行为不变） |
| 同文件 `test_duplicate_guard_treats_an_envelope_or_marker_as_gone` | `messages` 里是 `[Previous: used read …` / `<persisted-output …` → 视为不可复用，ALLOW |
| 同文件 `..._a_guard_denied_call_is_not_counted_as_a_failure` | 2 次成功（可见）+ 连续 3 次 DENY：每次都 DENY（DENY 不走 `after`，预算不被清空） |

**必须同步调整的既有用例**（它们用空 `ToolContext()` 表达"可见"，契约改为"可见才算"后要补
可见 tool 消息 fixture）：

| 行号 | 用例 | 调整 |
| --- | --- | --- |
| 80-102 | `test_duplicate_guard_allows_a_retry_then_refuses` | `ctx` 带 `c1` 的可见结果；断言不变 |
| 105-116 | `test_duplicate_guard_resets_between_turns` | 同上（reset 前的拒绝依赖可见） |
| 166-177 | `test_duplicate_guard_still_refuses_a_third_successful_repeat` | 同上 |
| 180-197 | `test_duplicate_guard_names_no_call_when_every_attempt_failed` | 同上，保持 `"(call " in reason` |
| 277-282 | `test_a_repeated_read_still_says_reuse_the_result` | 同上，保持文案断言 |

`tests/test_guard.py` 的 `_call()` 需要默认 `id` 参数（现在写死 `"c1"`），以便构造 `a`/`b` 两条；
再加一个 `_visible(*ids)` helper 生成 `ToolContext(messages=(...))`。

## 4. 验收

1. `pytest tests/test_guard.py -q` 绿；
2. 全量 `pytest`、`ruff`、`mypy` 干净；
3. 离线复核：用 s3 `turn-1.jsonl` 的 `CompactionEnded` 层统计说明 iter3 的 history 里
   role=tool 只剩 iter2 的 `<persisted-output>` 标记（2000 字预览，`history_messages=4`），
   iter1 的 12 个 id 全不在——该次拒绝在新判据下会放行；第 5 次同参读取起仍被拒；
   已用轨迹里的真实 call id 重放（`/tmp/r22-shots/157-o1-offline.txt`）：

   ```
   iter3 same-range read -> ALLOW (trace: guard_denied)
   attempt 4 -> ALLOW
   attempt 5 -> DENY | 'read' already succeeded 4 time(s) this turn … earlier output is no longer fully in the context …
   ```
4. 前端不受影响（不改 `toolmeta`/卡片）。

## 5. 明确不修的部分（写进矩阵）

**"12 块逐块累加出明细表"本身不可行**：单块 40 KB 会被 L3 立即换成 2000 字预览，L4 随后摘要掉
全部 tool 消息；要逐块明细应"每读一块就把计数写进 assistant 文本"或用 `grep -c`/`awk`。
本轮只修"结果已经不在上下文、却仍被要求复用"这一块，并把拒绝文案改成可执行的真话。

**已知边界（不修）**：同一批并行提交的同一指纹调用不共享本批计数——`dispatch` 先对整批
`check`、执行后才 `after`，所以一批 4 个相同 `read` 会全部放行（阈值 2 时上限本应 4，行为
仍在 2×阈值内；阈值 1 时可能多放 1–3 次）。这是改动前就存在的行为，修它要给守卫加"批次内
预占"，超出本轮范围，记入 `docs/r4-analysis.md` §8 风险表。
