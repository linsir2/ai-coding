# I2 方案：上下文完整性（压缩必须真的"看到全部"）

> 触发：eval-08 阶段 E（T2 42 次调用 / 554s）与阶段 F（90 分钟未完成）的根因追查。
> 结论先行：**不是模型笨，也不是记忆库召回不灵**。是压缩管线有三处结构性缺陷
> （无状态重算 / 头截断 / merge 死代码），加两处保留量过紧、一处编排副作用，
> 使模型看到的上下文**从第 5 个迭代起就冻结在会话开头**。
> 环境：`/workspace/ai-coding`，Python 3.14，`dashscope qwen3.7-flash`，窗口 48k。
> 竞品源码：`/workspace/_vendors/opencode`（TS）、`/workspace/_vendors/codex`（Rust）。
> v2 已修入两轮独立评审的 **8 个 P0**（见 §9）。

---

## §0 一句话

**我们的"压缩"是每次请求从零重算的无状态纯函数，摘要输入又只取拍平文本的前 60,000 字符，
且"上一次摘要"从未进入 prompt**——于是每次摘要都在总结"会话最开头那一段"，
checkpoint 永远停在第 4 个迭代，而模型被告知"对话原文被截断"。

修法：checkpoint 变成**会话级持久状态**；只摘要**新增部分**（按消息身份切分，绝不按长度猜）；
摘要输入**保留工具结构 + 分块滚动合并**（`<previous-checkpoint>` 真正进 prompt）；
并在"摘要不该跑"的那些轮次把 delta **原样带进请求**——三条分支都不丢消息。

---

## §1 事实底稿（实测，非推断）

### R1 · 压缩无状态 → 每次都在重摘要"同一段开头"（P0）

`RequestAssembler.assemble` 每次请求都拿**全量** `session.messages` 重算：

```python
# core/request_assembly.py:208-215
history = await self._compactor.compress(
    session.messages, deadline=deadline, on_summary=books.summaries.append,
    pinned=pinned, **self._compactor_extras(session),
)
```

而 `_run_l4` 产出的 checkpoint 只存在于**返回值**里，从不写回 `session.messages`
（唯一写回点是 `/compact` 的 `compaction.py:362`）：

```python
# core/context_compression.py:999-1007
note = ChatMessage(role="user", content=f"[以下为早期对话摘要]\n{summary}", timestamp="")
return [note, *tail]
```

所以下一次压缩时 `split_checkpoint(head)` 在 `session.messages` 里**找不到旧摘要**，
`_l4_head_text` 退回"整段历史拍平"，再被 `_clip_for_summary` 取前 60k。

**证据 1（同一会话两次摘要几乎逐字相同）**——T2 trace，iter 4 与 iter 25：

```
iter 4  (8→3 msgs):  进行中 逐行阅读 ai_coding/core/agent_loop.py
                     阻塞   对话原文截断。尚未完成完整代码审查与缺陷定位
iter 25 (52→21 msgs): 进行中 读取 ai_coding/core/agent_loop.py 源码
                     阻塞   原始对话在代码阅读阶段被截断
```

中间隔了 21 个迭代、30 次工具调用，摘要一个字没往前走。

**证据 2（每次都从全量重算）**——`CompactionEnded.before_tokens` 单调增长：

```
iter 4: 55459   iter 16: 81982   iter 28: 98486
iter 8: 61391   iter 20: 94117   iter 32: 102518
iter 12: 72611  iter 25: 97690   iter 40: 105512
```

**证据 3（成本）**：37 个迭代里 **36 次 compaction、17 次 L4 摘要调用**，
每次摘要输入恒为 18,050 token（= `_clip_for_summary` 的 60k 字符）：
17 × 18,050 = **306,850**，占该回合 prompt 总量 **446,289 的 68.8%**
（`traces/*/summary.json`）。**三分之二的钱花在重复摘要同一段开头**。

**证据 4（模型被告知"被截断"）**：iter25 摘要的"阻塞"一栏写着
"原始对话在代码阅读阶段被截断"——模型在复述我们塞进 prompt 的
`[历史过长，此处截断]`（`llm_helpers.py:37`）。它因此认为"之前的工作丢了"，
于是重新开始探索（it4 "之前的对话已经尝试分析 agent_loop.py 但没有完成"、
it30 "我来系统地扫描…先从主控制流开始"）。

### R2 · 摘要输入是"拍平文本 + 头截断"（P0）

```python
# ai/llm_helpers.py:28, 232-242
_MAX_SUMMARY_CHARS = 60_000
def _clip_for_summary(text, cap=_MAX_SUMMARY_CHARS):
    if len(text) <= cap: return text
    return text[:cap] + _TRUNCATION_NOTE      # ← 只留最旧，丢最新
```

历史先被 `_joined_history_text` **拍平成纯文本**（`context_compression.py:416-424`，
只拼 `role + content`，**丢掉 tool_call 的参数与结构**），再取**前** 60k。

### R3 · "合并模式"是双死代码（P0，评审修正）

不只是 `previous` 恒为 `None`（`context_compression.py:951/956/960`）——
**即使传了，它也不会进 prompt**：

```python
# ai/llm_helpers.py:284-293
max_tokens = _MERGE_TOKENS if previous else _CREATE_TOKENS
prompt = f"请{('更新' if previous else '压缩')}以下对话：\n{_clip_for_summary(text, cap)}"
```

`previous` 只影响 `max_tokens` 与一个中文动词；system prompt 里
`<previous-checkpoint>` / `<conversation-since-checkpoint>` 的说明（`:164-178`）
**永远收不到对应输入**。于是 `_MERGE_TOKENS`、`summary_within_growth_bound`、
"更新而不是重写"全部落空。

### R4 · 活 turn 内 write-ahead 重放，且跳过护栏计数（P0）

```python
# core/dispatch.py:516-524
reused = log.claim(call)
if reused is not None:
    return reused, False, False      # ← 不执行、不跑 guards.after、不跑 TOOL_AFTER
```

`WriteAheadLog` 跨批次存活（`session.write_ahead` 由 `_persist` 写回，
`__init__` 再读回，`core/write_ahead.py:41-49`），而 `bash` 的 `mutates_world`
默认为 `True`（`tools/base.py:171`，`bash` 未覆盖）。

**证据（F 阶段）**：`cat -n models.py` 连续 **10 次**（消息 351–381，8 分 18 秒），
输出字节完全相同（2,726 字符）、`duration_seconds` 全部 = **0.018** —— 同一份记录被重放。
模型自己在 reasoning 里写：

> "I did call `edit` to remove the `task_tags` block… But then subsequent `bash` calls
> still show the same content… This suggests either: 1. The edit didn't actually apply
> (maybe I had a stale read) 2. Or the file was reverted somehow."

三层后果：① 改完代码再跑同一条命令，拿到**改之前**的输出；② 重放跳过 `guards.after`
→ `duplicate_call` 计数不增长 → **护栏永远拦不住**（对照：`read` 是
`mutates_world=False`，走真实执行，第 3 次被拒，session 里 5 条 deny 全是 read）；
③ 重放仍被记为 `success=True`，`ACTION_TOOLS` 因此重置 `search_streak` 与证据集
（`turn_books.py:469-479`）→ **假进展**。

### R5 · 保留量过紧：窗口 48k，模型只看到 2–3k（P0，评审新增）

`split_tail` 的尾部预算本是 `min(15000, 48000//4)=12,000` token
（`context_compression.py:846`），但 L1（`keep_tail=20`）与 L2
（`keep_recent_tool_results=3` + 每条信封 200 字符）在它之前把历史切碎。
实测压缩后历史只剩 **2.3–3.6k token**：

```
i14: 74058 → 3601      i25: 24759 → 3345      i40: 6217 → 2203
```

T2 的 i40 是 105,512 → 2,297，压缩比 **46:1**；而 48k 窗口的 ceiling 是 32k。
**窗口不是被撑爆，是被浪费。** 直接后果是重读：`agent_loop.py` 读 4 次、
`plan_runner.py` 读 4 次、`ruff check` 跑 4 次、`grep '\[0\]'` 跑 4 次；
42 次调用里至少 20 次重复或高度重叠。

### R6 · 工具输出单侧截断，两边都残缺（P1）

| 位置 | 默认 | 方向 | 后果 |
|---|---|---|---|
| `tools/bash_tool.py:36, 532-535` | 50,000 字符 | **只留头** | pytest 的 `FAILED` 汇总在尾部 → 丢 |
| `infra/subprocess_runner.py:58-61` | 50,000/流 | **只留头** | stdout 大时 stderr 的报错被挤掉 |
| `scripts/eval_fullstack_task.py:102,113` | 1,200 字符 | **只留尾** | 首个 traceback 在头部 → 丢 |
| `domain/write_ahead.py:25, 42-44` | 4,000 字符 | **只留头** | 重放时再截一次 |

实测 F 阶段喂给模型的 4 条反馈**长度全部恰好 1,276 字符**，其中约 500 字符是
`asyncio_default_fixture_loop_scope` 弃用警告；18 个失败里只能看到最后 1 个。

### R7 · 没有"试过什么"的账本（P1）

`TurnDigest` 只有 8 个字段（`domain/turn_digest.py:104-118`），**没有 attempts/failures**。
F 的 `turnDigests[3]` 只有 `outcome/files/openItems`。同一根因表述至少 14 次
（消息 216, 218, 220, 222, 224, 230, 233, 244, 263, 351, 357, 365, 367, 381），
`models.py` 被 `write` 5 次、`edit` 4 次、`cat` 约 20 次。

### R8 · 三个小口径错误（P2）

- `_history_tokens` 只统计 `role + content`（`:381-413`），**不计 `tool_calls[].arguments`**；
- `_l4_should_run` 用 `max_context_tokens` 而非 `compaction_ceiling`（`:830-833`）；
- `_l4_failures` 是 `ContextManager` 实例字段（`:595`），而 ContextManager 进程级共享
  （`cli.py:186`）→ 一个会话摘要失败 3 次会让**所有**会话停止摘要。

---

## §2 竞品对照（源码级）

| 维度 | opencode | codex | 我们（现状） |
|---|---|---|---|
| 触发 | `estimateContext >= ceiling`，ceiling = 窗口 − max(15%·窗口, 16k)（`compaction.ts:1104-1112`） | `active_tokens >= 窗口×90%`（`openai_models.rs:534-545`） | ceiling 有，但 L4 另用 `max_context_tokens`，且 `dropped` 每轮触发 |
| 摘要输入 | **保留完整消息结构**（含 tool call/result），放不下才拍平，拍平时**丢最旧**（`compaction.ts:461-464, 660-728`） | `for_prompt` 保留 `ResponseItem` 结构（`compact.rs:300-305`） | 拍平成纯文本，**丢最新** |
| 头尾 | 尾部 `keep=15k tokens` **原样保留**，其余进摘要（`compaction.ts:954-996`） | 只留最新 user 消息累计到 20k token（`compact.rs:710-814`） | 尾部预算 12k，实际只剩 2–3k |
| 增量合并 | **有**：`previousCompaction()` + "Update the existing checkpoint…Newer history always takes precedence"（`compaction.ts:936-939, 214-230`） | 无（每次重写），但摘要旁保留最近 user 消息 | **无**（`previous` 恒 None，且从不进 prompt） |
| 模板 | 七段：Objective / Requirements / Decisions / Work State(Completed/Active/Blocked) / Next Move / Relevant Files / Important Context（`compaction.ts:159-190`） | 5 条要点，无固定章节 | 七段，≤1200 token |
| 工具输出 | 进历史 head-only 2000 行/50KiB + 全文落盘 + marker（`tool-output.ts:13-14, 80-92`）；transcript 截断 **head+tail** 2000（`compaction.ts:1043-1050`） | 10k token，head 留 tail 丢（`output-truncation/src/lib.rs:164-252`） | bash head-only 50k；驱动 tail-only 1.2k |
| 重复读抑制 | 无 | 无 | 有 `duplicate_call`，但被 R4 绕过 |
| 预计算 | 默认 **false**，门槛 64k（`compaction.ts:255, 146-157`） | 无 | 无（I1 已论证不做） |

**业界共识（联网，带来源）**：

- Claude Code 的压缩 prompt 有 **9 段**，第 ④ 段就是"遇到的错误与修复"；
  压缩后文件最多重读 5 个、按最近修改排序（[context-window](https://code.claude.com/docs/en/context-window)）。
- Cursor 自训练摘要约 1,000 token，固定含 "Errors / fixes" 与 "What's still broken"
  （[self-summarization](https://cursor.com/blog/self-summarization)）。
- Roo Code 明确要求 **"Keep track of all attempted solutions and their outcomes"**
  （[intelligent-context-condensing](https://roocodeinc.github.io/Roo-Code/features/intelligent-context-condensing/)）。
- Manus "**Keep the Wrong Stuff In**"：保留失败转弯显著降低重复同一错误的概率
  （[context-engineering](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus)）。
- 递归摘要漂移：只基于旧摘要推进会累积误差，新摘要必须回看原始消息
  （[arxiv 2308.15022](https://arxiv.org/html/2308.15022v3)）。
- 工具输出 head+tail + "省略多少、全文在哪"优于单侧截断
  （[trimhook](https://www.npmjs.com/package/trimhook)）；Claude 对 >100k 字符输出**落盘 + 只回预览与路径**
  （[release-notes](https://platform.claude.com/docs/en/release-notes/overview#may-29-2026)）。

**结论：我们同时犯了竞品都避开的两个错**——摘要丢最新、没有增量合并。
opencode 的 `flattenAndDropOldest` 名字本身就是答案：**要丢就丢最旧的**。

---

## §3 设计

### 3.0 三条不丢消息的不变量（本方案的正确性定义）

> 对任意一次 `compress(messages)`，返回的请求历史中，每条 `messages` 里的消息
> **要么原样出现在 tail 里，要么被某次摘要覆盖（索引 < consumed）**。
> 不允许第三种结局。

三条分支各自满足它：

| 分支 | 条件 | 返回 |
|---|---|---|
| A 首次压缩 | 无有效 checkpoint，且 `dropped` 或超 ceiling | `[note(new)] + tail`，`consumed = len(head)` |
| B 增量压缩 | 有 checkpoint，且 delta ≥ 阈值（或 force） | `[note(new)] + tail`，`consumed = len(head)` |
| C 攒着不摘 | 有 checkpoint，delta < 阈值 | `[note(prev)] + delta + tail` ← **delta 原样携带** |
| D 摘要失败 | 任何分支的摘要返回 None | 返回**未安装 note 的 `out`**（降级为不压缩，绝不丢 delta） |

### D1 · checkpoint 变成会话级持久状态 + 按身份切分

**状态**：`SessionData` 新增三个可选键（沿用"加可选键、不 bump 版本"约定，
只在非空时序列化，参照 `session.py:594` 的 `lastCompactedTurn`）：

```python
compaction_checkpoint: str = ""   # 摘要正文（不含标记行）
compaction_consumed: int = 0      # 覆盖 messages[:consumed]
compaction_anchor: str = ""       # messages[consumed-1].id
```

**取值（三级回退，解决 `/compact` 与旧会话）**：

```python
def _valid_checkpoint(session, messages) -> tuple[str, int]:
    prev, used, anchor = (session.compaction_checkpoint,
                          session.compaction_consumed,
                          session.compaction_anchor)
    if prev and 0 < used <= len(messages) and messages[used - 1].id == anchor:
        return prev, used
    # 回退 1：/compact 会把 session.messages 改写成 [note, *tail]
    #         （compaction.py:362），此时 note 就在 0 号位
    head, _ = split_checkpoint(messages[:1])
    if head:
        return checkpoint_body(head) or "", 1
    return "", 0
```

**为什么不能按长度算切点（评审 P0-A1）**：L1 会在中间插入
`[snipped N messages]` 占位符（`context_compression.py:110-116`），L2 会把旧结果
换成**新对象**（`_tool_copy`，`:320-328`）。所以"tail 是 `l4_source` 的后缀"不成立，
`cut = len(l4_source) - len(tail)` 会把边界上的消息（如 m39）**同时排除在 delta 与 tail 之外**。

改为**按对象身份**求切点，且在有 checkpoint 时**直接从 `l4_source` 切 tail**（不经 L1）：

```python
head, tail = split_tail(l4_source)      # 纯函数、真后缀
cut = len(l4_source) - len(tail)
delta = head[used:]                     # used 已由 _valid_checkpoint 校验
```

**流程**（`ContextManager.compress` 新增 `session` 参数；`_run_l4` 保留，供测试
monkeypatch，签名扩展为 `_run_l4(previous, delta, tail)`）：

```
out = L3(messages)
l4_source = out
prev, used = _valid_checkpoint(session, messages)

if prev:
    head, tail = split_tail(l4_source)
    delta = head[used:]
    tail = L2(tail)                       # 只在 tail 内做信封化
    if delta and (force or tokens(delta) >= summary_delta_tokens):
        new = await self._run_l4(prev, delta, tail)
        if new and head:
            install(session, new, consumed=len(head), anchor=messages[len(head)-1].id)
            out = [note(new)] + tail
        else:
            out = l4_source               # 分支 D：不装 note，不丢 delta
    else:
        out = [note(prev)] + delta + tail  # 分支 C：delta 原样带
else:
    out = L1(out); out = L2(out)           # 既有路径（首次压缩 / 无 checkpoint）
    head, tail = split_tail(out)
    delta = _delta_by_identity(l4_source, tail)   # 身份切分，见上
    if delta and (dropped or _l4_should_run(l4_source) or force):
        new = await self._run_l4("", delta, tail)
        if new: install(...); out = [note(new)] + tail
        else: out = 原 out                 # 分支 D
out = _ensure_pinned(out, pinned)          # 保持在最后（评审 P1-B3）
```

**关键性质**：

- **分支 C 消灭抖动**：delta 小于阈值时一次 LLM 都不调，且 delta 原样在请求里；
- **`dropped` 不再单独触发摘要**：有 checkpoint 时 L1 根本不跑（note 已经界定了 head），
  没有 checkpoint 时才走老路 → 直接消灭"每迭代一次摘要"（36 次 → 预计 ≤6 次）；
- **覆盖性**：`consumed` 单调推进；任何消息要么在 tail，要么在某次 delta 里被摘要过。

**与 `/compact`、overflow 救援**：`compact_now` 写回 `session.messages` 后，
`_valid_checkpoint` 的**回退 1** 会从 `messages[0]` 的 note 认出 checkpoint，
`consumed=1`，增量语义继续（评审 P0-B1/B6）。

**失败语义**：`_run_l4` 返回 None → **不推进 `consumed`、不安装 note**，
返回未压缩的 `out`（等价于现有"降级为不摘要"，`context_compression.py:992-993`）。

### D2 · 摘要输入：结构化渲染 + 分块滚动合并 + previous 真正进 prompt

**2.1 结构化渲染**（新 `render_for_summary`，替代 `_joined_history_text`）：

```
[user] 请在当前工作区从零构建…
[assistant] 我先搭后端。 → bash({"command": "mkdir -p backend/app"})
[tool:bash] ok | <输出前 1200 + 后 800 字符，中间标"省略 N 字符 · 全文 <path>">
[assistant] 缺陷很明显：…（无工具调用）
```

- 保留 `tool_calls` 的**工具名与参数**（每个参数值截 200 字符）；
- 复用既有 `_is_snip_placeholder` / `_is_background_notice` 过滤（评审 P0-D-10）；
- 工具结果截断**必须**带省略量与原文路径（`persisted-output` / `elided` 的 path 都在消息里）。

**2.2 分块滚动合并**（`build_summarizer`）：

```python
chunks = split_by_messages(delta, cap=_MAX_SUMMARY_CHARS)   # 单条超长则按行再切
acc = previous
for chunk in chunks:
    acc = await one_call(render_for_summary(chunk), previous=acc)
return acc
```

- **永不 `text[:cap]`**；放不下就分块，每块都进 LLM；
- **不设块数上限**（评审 P0-A6：上限=静默丢弃）。防御性上界改为
  `_MAX_SUMMARY_INPUT_CHARS = 2_000_000`，超过才降级为 head+tail **并显式报 Degraded**；
- 单块内保留 `_SHRINK_STEPS`（provider overflow 时降档重试），降档只作用于当前块。

**2.3 `previous` 必须进 prompt**（评审 P0-A3）：

```python
prompt = (
    f"<previous-checkpoint>\n{previous}\n</previous-checkpoint>\n\n"
    f"<conversation-since-checkpoint>\n{text}\n</conversation-since-checkpoint>"
    if previous else f"请压缩以下对话：\n\n{text}"
)
```

**2.4 合并规则补三条**（写进 `_MERGE_RULES`）：

- **目标与约束是粘性的**：只有更新的 user 消息才能改写 `## 目标` / `## 约束与要求`；
- **`## 已尝试且失败` 只增不减**：后续成功只能把条目移到"已完成"并注明修复方式；
- **新块与旧决策冲突时保留两条并标注反转原因**，不得静默覆盖。

**2.5 预算与新增章节**：

- `_CREATE_TOKENS` 1200 → **2400**，`_MERGE_TOKENS` 1600 → **2800**
  （依据：T2 压缩比 46:1，1,200 token 只够 10–15 条事实，而任务涉及 20+ 文件）；
- `SUMMARY_SECTIONS` 末尾新增 **`## 已尝试且失败`**（八段）。

### D3 · 摘要质量门（在 `checkpoint_body` 之后判定）

现状只要**任意一行**等于某个标题就整体接受（`llm_helpers.py:205-229`）。
改为：对 `checkpoint_body()` 的**返回值**（已剥前言）计算标题索引序列，要求

1. 首个标题 == `SUMMARY_SECTIONS[0]`；
2. 出现 ≥ 5 个标题（8 段允许缺 3 段，模型可以写「无」而不是删段）；
3. 索引严格递增。

不满足 → 用现有 `_SUMMARY_NUDGE` 重试一次；仍不满足 → 返回 None（降级）。
**在 `checkpoint_body` 之后判定**，以免与既有"前言应被剥掉而不是拒收"
（`test_compaction_shape.py:85`）冲突。

### D4 · write-ahead 只在"恢复被崩溃的回合"时重放

```python
# core/write_ahead.py
def claim(self, call, *, allow_replay: bool = False):
    ...
    if record is not None and record.status == "ok" and allow_replay:
        return <replayed result，data 里加 "replayed": True>
```

- `AgentLoop._run_turn(..., resumed: bool)` → `_run_tools(..., resumed=)` →
  `dispatch_batch(..., allow_replay=)`；只有 `resume_turn`（`agent_loop.py:883`）传 True；
- 活 turn 内 `claim` **只记录 intent**，真实执行 → `guards.after` 正常计数；
- 恢复 turn 内重放**也调用** `guards.after`（那次调用崩溃前确实发生过），
  并在 `data["replayed"]=True` 标明；
- `FailedRepeatGuard` 只按失败计数，重放成功记录只会 pop 失败计数，不会误计（评审 C4）。

**为什么不是删掉 write-ahead**：它是 M-C1 的崩溃恢复能力，只把它**收回恢复路径**。

### D5 · 工具输出改 head+tail

新 `infra/text_clip.py::clip_head_tail(text, limit, *, head_ratio=0.6, note="")`：

```
<head>…\n[省略 N 字符{ · 全文见 <path>}]\n<tail>
```

应用：`tools/bash_tool.py`（50k → head 30k + tail 20k）、
`infra/subprocess_runner.py`（每流分开裁，不再互相挤占）、
`domain/write_ahead.py::clip_output`（4k → head 2.4k + tail 1.6k）。
`grep`/`glob` 保持现状（截断维度是"条数"，且已告知）。

### D6 · 尝试账本（缓解，不是解药）

`TurnDigest` 新增 `attempts: tuple[str, ...]`（最多 3 条，每条 ≤100 字符，
形如 `backend/app/models.py → nonzero_exit(18 failed)`）：

- 来源：本回合 `books.failures` 去重后的 `(tool, reason)` + 本回合改动过的文件；
- 渲染进 `<working-memory>`（1,200 字符预算内，从最新往前）。

**诚实标注**：评审指出它的数据源（`books.failures` 只有 `(tool, error, excerpt)`，
没有文件、没有真实异常签名）不足以让 F 那种"诊断错了错误"的循环停下来。
真正需要的"同一错误在 2 次编辑后仍出现 → 停止编辑、运行最小复现"是**新立项**，
本方案不做（见 §7）。

### D7 · 驱动脚本反馈改结构化

`scripts/eval_fullstack_task.py` 新增 `_pytest_digest(text, limit=4000)`：

1. 抽出全部 `^FAILED .*` 行（模型最需要的索引）；
2. 抽第一个 `=== FAILURES ===` 到下一个 `===` 的块（首个 traceback）；
3. 过滤 `PytestDeprecationWarning` / `warnings summary`（实测占 500/1276 字符）；
4. 末尾附 `N failed` 统计。

### D8 · 三个口径修正

- `_history_tokens` 计入 `tool_calls[].arguments`；
- `_l4_should_run` 用 `compaction_ceiling(self._max_context_tokens)`；
- `_l4_failures` 移进 `SessionData`（或按 `session_id` 键控），修跨会话熔断。

### D9 · L4 消费掉的消息也要归档（评审新增）

现状只有 L1 剪枝会 `archive_transcript`（`context_compression.py:705-709`），
L4 替换掉的 head **不归档**——而环境块却承诺"需要原文用 `history_search` 取回"
（`request_assembly.py:413-417`），i4 那种 `L1 removed=0, L4 removed=6` 的消息根本搜不到。

改：L4 安装 note 之前 `self.archive_transcript(head)`（已有方法，零新代码）。

### D10 · 保留量调整（依据 R5）

| 配置 | 现值 | 新值 | 理由 |
|---|---|---|---|
| `ai.keep_recent_tool_results` | 3 | **8** | 3 条只覆盖 1–2 个迭代；48k 窗口能放 8–12 |
| `ai.l2_window_chars` | 200 | **1200** | 200 字符等于"内容没了"，模型只能重读 |
| `ai.l2_window_budget` | 4000 | **12000** | 配合上面两项 |
| `ai.snip_keep_tail` | 20 | **30** | 只影响无 checkpoint 的首压路径 |
| `ai.summary_delta_tokens`（新） | — | **4000** | 分支 C 的阈值 |

---

## §4 逐文件改动

| 文件 | 改动 |
|---|---|
| `domain/session.py` | +3 可选字段 + 序列化（非空才写）+ `l4_failures` |
| `core/context_compression.py` | `compress`/`compress_if_needed` 收 `session`；D1 三分支；D9 归档；D8 口径；D10 默认 |
| `core/compaction.py` | `_compress` 把 `session` 透传给 `compress_if_needed`（探测式） |
| `ai/llm_helpers.py` | `render_for_summary`、`split_by_messages`、分块合并、previous 进 prompt、D3 质量门、第 8 段、预算 |
| `core/dispatch.py` | `claim(allow_replay=)`；重放时跑 `guards.after` 并标 `replayed` |
| `core/write_ahead.py` | `claim` 加 `allow_replay` |
| `core/agent_loop.py` | `_run_turn(resumed=)` → `_run_tools` → `dispatch_batch`；`resume_turn` 传 True |
| `infra/text_clip.py`（新） | `clip_head_tail` |
| `tools/bash_tool.py`、`infra/subprocess_runner.py`、`domain/write_ahead.py` | 改用 `clip_head_tail` |
| `domain/turn_digest.py` | `TurnDigest.attempts` + 渲染 + `to_dict/from_dict` |
| `config/models.py` | D10 五个默认值 |
| `scripts/eval_fullstack_task.py` | `_pytest_digest` |
| `docs/features.md`、`docs/data-contracts.md`、`docs/r15-summary-shrink.md` | 同步契约 |

---

## §5 RIPR 用例（红相先行）

| # | 用例 | 断言（红相必须失败） |
|---|---|---|
| 1 | `test_a_second_compaction_summarizes_only_the_delta` | 首压后新增 3 条 → 第二次摘要输入**只含这 3 条**，不含首条 user 消息 |
| 2 | `test_the_previous_checkpoint_reaches_the_prompt` | 第二次调用的 user prompt 含 `<previous-checkpoint>` 与上次摘要正文 |
| 3 | `test_a_small_delta_is_carried_verbatim_not_summarized` | delta < 阈值 → **零次摘要调用**，且 delta 的三条消息都出现在请求里 |
| 4 | `test_no_message_disappears_when_l1_inserts_a_placeholder` | 60 条消息 + `max_messages=50` → 请求 ∪ 已摘要 == 全部 60 条（**覆盖性断言**） |
| 5 | `test_a_rewritten_history_invalidates_the_checkpoint` | 删一条消息 → anchor 失配 → 重新全量摘要 |
| 6 | `test_a_compact_note_is_picked_up_as_the_checkpoint` | `session.messages = [note, *tail]` → `consumed=1`、增量继续、不重摘旧内容 |
| 7 | `test_an_oversized_delta_is_summarized_in_chunks_not_truncated` | delta 150k → ≥3 次调用；所有消息都出现在某次输入里 |
| 8 | `test_the_summary_input_keeps_tool_structure` | 输入含 `[tool:bash]` 与 `bash({"command": ...})` |
| 9 | `test_a_summary_missing_sections_is_rejected` | 只回 `## 目标` → 拒绝 |
| 10 | `test_a_preamble_before_the_first_section_is_still_accepted` | 前言 + 完整八段 → 接受（防与既有契约冲突） |
| 11 | `test_a_live_turn_never_replays_a_write_ahead_record` | 同 turn 同一 bash 两次 → 第二次真实执行、计数 +1 |
| 12 | `test_a_resumed_turn_replays_and_still_counts` | `resumed=True` → 重放、`data["replayed"] is True`、计数 +1 |
| 13 | `test_bash_output_keeps_the_tail` | 60k 输出 → 含尾部最后一行 + 省略标记 |
| 14 | `test_pytest_feedback_carries_the_failed_list` | 18 失败 → ≥18 条 `FAILED` 行，无弃用警告 |
| 15 | `test_the_digest_records_what_was_attempted` | 失败 2 次 → `attempts` 2 条并渲染进 working memory |
| 16 | `test_history_tokens_counts_tool_arguments` | 10k 参数 → 估算显著上升 |
| 17 | `test_a_failed_summary_does_not_drop_the_delta` | 摘要返回 None → 返回未压缩历史，delta 仍在 |
| 18 | `test_the_l4_consumed_head_is_archived_for_history_search` | L4 替换 6 条 → 归档文件含这 6 条 |

**变异（每条必须被某个用例打红）**：

1. 切点退回 `len(l4_source) - len(tail)`（长度算术）→ #4 红
2. delta 退回 `l4_source[0:cut]`（全量重摘要）→ #1 红
3. `previous` 不渲染进 prompt → #2 红
4. 小 delta 也强制摘要 → #3 红
5. `_valid_checkpoint` 去掉 note 回退 → #6 红
6. `claim` 忽略 `allow_replay` → #11 红
7. 重放不跑 `guards.after` → #12 红
8. 分块退回 `text[:cap]` → #7 红
9. 质量门退回"任一行等于标题" → #9 红
10. `clip_head_tail` 退回 head-only → #13 红
11. `_pytest_digest` 退回 `[-1200:]` → #14 红
12. `attempts` 恒空 → #15 红
13. `_history_tokens` 不计 arguments → #16 红
14. 摘要失败仍安装 note → #17 红
15. L4 不归档 head → #18 红

---

## §6 契约变更与兼容

| 变更 | 影响 | 处理 |
|---|---|---|
| `SessionData` +3 可选键 | 旧会话缺键 → 等同"无 checkpoint" | `data.get(..., default)` |
| `ContextManager.compress`/`compress_if_needed` + `session` | 旧 fake 不声明该参数 | **探测式传参**（`_compactor_extras` 已如此） |
| 活 turn 内不再重放 write-ahead | **语义修正**；`tests/test_write_ahead.py` 3 条用例（`:204/:225/:290`）锁定的是旧语义 → 需改写，理由：旧语义正是 R4 缺陷 | 改测试并在文档记录 |
| 摘要新增第 8 段 | 摘要变长 | 更新 `r15-summary-shrink.md` |
| 有 checkpoint 时 L1 不运行 | `dropped` 不再是 L4 触发项 | 更新 `features.md` 的层序说明；`test_the_pipeline_order_is_l3_l1_l2_l4` 走无 checkpoint 路径，仍绿 |
| bash 输出 head+tail | 相同预算下信息更多 | 无 |

**不改**：`search_streak_l3`（默认仍 0）、`turn_policy` 决策表、`_SHRINK_STEPS` 数值、
预计算（不做）、`mutates_world` 语义（`webfetch` 在 `/readonly` 被拒是既有契约）。

---

## §7 本方案不做的事

1. **不做预计算**（I1 已论证；opencode 默认关）。
2. **不改 `mutates_world` 语义**、**不给 MCP/read 加输出上限**（P1，另立项）。
3. **不做"同一错误 2 次编辑后强制最小复现"规则**——F 的真正解药，需要新的策略门，另立项。
4. **不做植入 bug 的基准任务**（评审建议的 ground truth），本轮只把判据改成可证伪（§8）。
5. **不改驱动脚本的轮数/自检逻辑**（只改反馈截取方式）。

---

## §8 验收

**离线**：18 条新用例全绿；`ruff`/`mypy` 0；全量不回归；15 条变异全部被杀。

**真机（复跑 eval-08 阶段 E 的 T2，同题同模型）**：

| 判据 | 修前 | 目标 | 防作弊说明 |
|---|---|---|---|
| 摘要调用次数 | 17 | ≤ 6 | 与"答案质量"合取，不以少做工作换取 |
| 摘要 token 占比 | 68.8% | ≤ 20% | 分母仍是总 prompt |
| 最后一次摘要是否含"截断"字样 | 是 | 否 | — |
| `tool_calls` | 42 | ≤ 25 | **且**答案含一个真实存在的 `path:line`（脚本校验该行存在且含所引代码） |
| 唯一调用率 `unique/total` | 0.93 | ≥ 0.8 | 防止"少读"刷低 total |
| 同一 `(tool, params)` 成功执行次数 | 10 | 1 | 由 trace 的 `ToolStarted` 计数 |
| 必须引用的文件数 | — | ≥ 3 | 每个文件须有非空 read 结果 |

**真机（阶段 F 前 4 轮）**：反馈含 ≥18 条 `FAILED` 行、不含弃用警告；
同一 `bash` 不被重放（`executed_calls` 与 `tool_calls` 分别统计）。

**诚实声明**：T2 是开放式"找一个真实 bug"，没有 ground truth；上述判据能证明
"上下文不再冻结、重复执行消失"，**不能**证明"模型一定找到 bug"。
后者需要植入 bug 的固定任务（§7 第 4 条，另立项）。

---

## §9 修订记录

| 版本 | 变化 |
|---|---|
| v1 | 首版：R1–R8 事实底稿 + 竞品对照 + D1–D8 + 14 条用例与 10 条变异 |
| v2 | 两轮独立评审修入 **8 个 P0**：① 切点改**按对象身份**（长度算术会在 L1 占位符处丢消息，且 L2 会换对象）；② `previous` **必须渲染进 prompt**（v1 以为传了就够，实际 `llm_helpers.py:289-293` 只用于选 max_tokens）；③ `/compact` 与 overflow 救援会改写 `session.messages` → 增加"从 `messages[0]` 的 note 回退"；④ 摘要失败不推进 consumed、不装 note；⑤ `dropped` 不再每轮触发 L4（否则 ≤6 次摘要不可能），新增分支 C"delta 原样携带"；⑥ 分块去掉块数上限（上限=静默丢弃）；⑦ 新增 D9（L4 消费的 head 也要归档，否则 `history_search` 承诺是假的）、D10（L2 保留量 3→8、窗口 200→1200，依据 R5：窗口 48k 却只给模型 2–3k）、D8（`_l4_failures` 跨会话共享）；⑧ 评测补 4 条防作弊判据并声明"不能证明找到 bug"。另更正 token 数字：446,289（非 635,067），摘要占 68.8%。 |
| v3 | **实现期发现并修掉两个 v2 没看见的问题**：<br/>① **anchor 用 `ChatMessage.id` 是空转**——循环创建的消息 id 一律为 `""`，`"" == ""` 恒真，等于没有失效判据。改成**覆盖前缀的内容指纹** `_anchor_of(messages[:consumed])`：跨存盘稳定，前缀里任何一条被改写/删除/回退都会失配。只指纹"边界那一条"也不够（改前面某条检测不到），所以是整段前缀。<br/>② 新增 `test_context_integrity.py` 15 条用例；**15/15 变异全部被杀**（原计划的变异 #1「长度算术切点」经实测是**等价变异**——在 L1 插入占位符后，`len(source)-len(tail)` 与按身份得到的切点数值相同，因为 tail 始终是 `l4_source` 的真后缀；已替换为「tail 丢掉第一条」这一真正会丢消息的变异）。<br/>③ `_run_l4` 的 chunk 数不设上限由 `_split_text` 的行边界切分保证，已用 150k 输入的用例锁定。 |
| v4 | **独立实现评审（只读、逐行）发现 2 个 P0 + 4 个 P1，全部修入**：<br/>**P0-a** `split_tail` 回溯到"只剩一条 tool"时，声明它的 assistant 在 head 里，`sanitize_pairs` 会把这条最新工具结果删掉——它既不在请求里也不在 checkpoint 覆盖内。修：回溯后若 `tail[0].role == "tool"`，向前把载体一并纳入 tail（`split_tail`）。这是"工具密集回合的末尾"，不是边角。<br/>**P0-b** 分支 D 原样返回全量历史后，`compress_if_needed` 的 paging 用 `hard_trim` 丢掉未覆盖前缀，且 note 根本不在请求里。修：分支 D 改为 `[note(previous), *delta, *tail]`（与分支 C 同形——旧 checkpoint 仍然描述 `messages[:used]`），并让 `hard_trim` **保留 checkpoint note**。<br/>**P1-a** checkpoint 路径完全没接 L4 熔断器（`_l4_should_run` 只在首压路径被调用）→ 摘要持续失败时每个请求都白打 2 次。修：`wants_summary` 加 `not breaker_open`（`force` 仍可绕过，手动 `/compact` 要能重试）。<br/>**P1-b** `split_tail` 兜底分支在"整段都放得下"时返回 `head=[]`，分支 C 会把已被 checkpoint 覆盖的整段历史再发一遍。修：`head` 为空且有 checkpoint 时，以 `used` 为界重切。<br/>**P1-c** 首压路径 `cut = len(l4_source) - len(surviving_tail)` 把 snip 占位符当真实消息，占位符落在 tail 首部时 `cut` 少 1 → 该条被摘要却记为"未覆盖"。修：新增 `_cut_index`（按身份从尾部扫）。<br/>**P1-d** 恢复回合内**新 settle** 的记录也能被重放（R4 只是缩小了窗口）。修：`resume_turn` 开始时快照 sidecar 的 digest 集合，`WriteAheadLog(replay_scope=…)` 只对快照内的记录重放。<br/>**P2**：删掉死代码 `_l4_head_text*` 与 `_run_l4(tail=)`；`compaction_failures` 落盘；L1 的 `LayerStat.removed` 在 checkpoint 路径改为 `skipped`（head 是被 note 代表，不是被删）；`_anchor_of` 纳入 `tool_calls` 参数；`summary_delta_tokens` 改 `gt=0`（0 会静默关掉分支 C）。<br/>**变异 21/21 全杀**（新增 6 条对应上面的修复）。 |
