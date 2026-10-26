# R11 分析方案 — 二次压缩合并旧摘要（P0）

> 定位：`docs/compaction-debt.md` §1 的唯一 P0。**这是该清单里唯一会造成不可逆信息丢失的一条**：其余都是"不够好"，这条是"越用越瞎"。
> 依据：`ai_coding/core/context_compression.py`（503 行，全文核验）· `ai_coding/ai/llm_helpers.py`（205 行，全文核验）· `/home/linsir365/projects/opencode` @ `0a46301e3` `packages/core/src/session/compaction.ts`（948 行）· 实测脚本两轮压缩（§2.2）。
> 前置：`eb283df` / `ad466af` / `1053356` 已修的 7 条见 `docs/compaction-debt.md` §0，本文不重复。

---

## 1. 问题陈述

第 N 次压缩时，L4 只对**当前仍在 head 里的消息**做摘要。第 1 次摘要覆盖的内容在第 2 次压缩时早已不在 head 里。

**实测（§2.2）结论修正了我最初的假设**：旧摘要**确实**还在，而且**确实**被送进了摘要器。丢失不是因为管道断了，是因为**没有人告诉模型那是一份已有结论、应该合并而不是重写**。

---

## 2. 现状核验（实测，不是印象）

### 2.1 我们现在的 L4 管线

`context_compression.py::compress()`（`:399-411`）：

```
out = persist_large_outputs(out)          # L3
l4_head = self._l4_head_text(out)         # snip 前的真身（ad466af 引入）
snipped = snip_middle(out, 50, 3, 20)     # L1
dropped = len(snipped) < len(out)
if dropped: self._archive_dropped_history(out)   # ⑤
out = snipped
out = placeholder_old_results(out, ...)   # L2
should_summarise = self._l4_should_run(out) or dropped
if should_summarise:
    out = await self._run_l4(out, head_text=l4_head)   # L4
```

`_run_l4`（`:479-506`）：
```python
tail = messages[-self._l4_keep_tail:]
head_text = self._l4_head_text(messages) if head_text is None else head_text
summary = await self._summarizer(head_text)
...
note = ChatMessage(role="user", content=f"[以下为早期对话摘要]\n{summary}", timestamp="")
return [note, *tail]
```

> **历史快照**：`_l4_keep_tail` 这个配置项（条数）**已删除**，`_run_l4` 的尾部现在是
> token 预算（`ContextManager.split_tail`，`min(l4_tail_tokens, window//4)`，吸附到 user 边界）。
> 下面的代码块保留为当时的记录，不再代表现行实现。

`llm_helpers.py::build_summarizer`（`:95-120`）：
```python
async def summarize(text: str) -> str | None:
    prompt = f"请压缩以下对话历史：\n\n{text[:_MAX_SUMMARY_CHARS]}"   # 60k 字符硬截断
    for attempt in range(2):
        out = await _chat(model_config, _SUMMARY_SYSTEM_PROMPT,
                          prompt if attempt == 0 else prompt + _SUMMARY_NUDGE,
                          max_tokens=800, temperature=0.2)
        if summary_is_usable(out): return out
    return None
```

`_SUMMARY_SYSTEM_PROMPT`（`:64-69`）：
> 你是资深研发助手。请把一段很长的编程会话早期历史压缩成精炼的要点（中文），去掉寒暄与重复。只输出下面四个小节……

**契约**：`Callable[[str], Awaitable[str | None]]`，单参数。

### 2.2 实测：旧摘要到底丢没丢

两轮压缩，`max_context_tokens=3000, max_messages=12, l4_keep_tail=6`，摘要器返回含金丝雀的固定摘要：

```
第一次压缩后: 7 条
第二次压缩后: 7 条
摘要器调用次数: 2
=== 第二次送入摘要器的内容 ===
长度: 12594
旧摘要的关键事实 CANARY-OLD-1111 是否还在输入里: True      ← 在
第一批原始消息（第一批-5）是否还在: False                 ← 符合预期（已被摘要取代）

开头 260 字符:
[user] [以下为早期对话摘要]
## 目标
旧目标

## 已完成
关键事实 CANARY-OLD-1111
...
[user] 第一批-34 内容内容…
```

**关键发现**：旧摘要躺在 head 里，被 `_joined_history_text` 渲染成 `[user] [以下为早期对话摘要]\n## 目标…`，**除了一行标记之外，和任何一条用户消息没有区别**。系统提示词要求"把一段很长的编程会话早期历史压缩成要点"——模型据此把这份摘要**当成对话的一部分**来总结，而不是当成**已有的结论**来合并。

结果不是"数据丢了"，是"模型在一个它并不知道其角色的输入上做无条件重写"。每一轮都是一次无保护的摘要套摘要。

### 2.3 顺带核出的三个静默损失点

| 位置 | 行为 | 后果 |
|---|---|---|
| `llm_helpers.py:26` `_MAX_SUMMARY_CHARS = 60_000` | 输入硬截断 `text[:60000]` | 第 3/4 次压缩时"旧摘要 + 新内容"超 60k，尾部静默丢弃，且**无任何记录** |
| `llm_helpers.py:112` `max_tokens=800` | 输出硬上限 | 合并后的 checkpoint 比首次摘要更丰富，800 可能截断，且无校验 |
| `context_compression.py:506` | 标记是**内联字面量** | 同一文件 `:25` 的 `SNIPPED_HEADER` 已按惯例提为常量并导出；这个没提，是因为**没人需要识别它**。要做识别就必须提。 |

---

## 3. OpenCode 怎么做的（`packages/core/src/session/compaction.ts`）

### 3.1 旧摘要怎么进入摘要请求

**没有专门的管道。** `previousCompaction`（`:669-673`）取第一条消息：

```ts
/** History loads from the latest completed compaction, so a previous one is always the first message. */
const previousCompaction = (messages) => {
  const [first] = messages
  return first?.type === "compaction" && first.status === "completed" ? first : undefined
}
```

链路：旧 checkpoint 消息 → `splitConversation` 的 `messageToText` 对 `compaction` 类型返回空串 → 被排除出 `recent` → 留在 `older` → `transcript()` → `to-llm-message.ts:303-322` 把它渲染成一条普通 user 消息（`<conversation-checkpoint>` 包裹）→ 作为**普通 transcript 消息**发给摘要模型。

**结构上和我们一样**：旧摘要就是 transcript 里的第一条消息。

### 3.2 差别只在提示词

`buildPrompt(update, legacy)`（`:142-173`）：

```ts
if (!update) {
  return ["You MUST summarize the conversation above into a structured summary …", ...shared].join("\n\n")
}
return [
  "Update the existing checkpoint in the conversation above into one consolidated summary.",
  "Newer history always takes precedence over the existing checkpoint. Preserve previous information unless newer history clearly contradicts, supersedes, resolves, or makes it stale. If something is no longer relevant to continuing the work, you may remove it.",
  "Incorporate newer requirements, decisions, progress, and context. Reconcile Work State and Next Move: move completed work out of Active, remove resolved blockers and answered questions, and preserve unresolved or pending work.",
  "Return only the updated Markdown sections. Do not reproduce the `<conversation-checkpoint>`, `<summary>`, or `<recent-context>` wrapper tags from the previous checkpoint.",
  ...shared,
].join("\n\n")
```

**四条合并指令，逐条对应我们缺的能力：**
1. 识别这是"更新已有 checkpoint"，不是"从零摘要"
2. 新内容优先，但旧信息默认保留，除非被明确推翻
3. 显式要求对账：把已完成移出 Active、删掉已解决的阻塞、保留未解决的
4. 不要把包装标签原样吐回来

`update` 的判定就是 `previous !== undefined`（`:266`）。

### 3.3 它的模板（7 节，我们 4 节）

`## Objective` · `## Requirements` · `## Decisions` · `## Work State`（含 `### Completed/Active/Blocked`）· `## Next Move` · `## Relevant Files` · `## Important Context`

外加 `SUMMARY_RULES`（`:134-140`）六条规则，其中三条直接管合并：
- "Preserve exact file paths, symbols, commands, error strings, URLs, and identifiers."
- "Carry forward only user questions or requests that remain unanswered… Preserve exact wording when carrying one forward."
- "Preserve consequential workflow state, including whether changes are uncommitted, committed, pushed, under review, or merged."

### 3.4 它的模板校验（`:268-269, 280-282`）

```ts
const headings = SUMMARY_TEMPLATE.split("\n").filter((line) => line.startsWith("##"))
const filled = (text) => text.split("\n").some((line) => headings.includes(line.trim()))
```
任意一行等于某个二级标题即通过；不合格追加一次 NUDGE，仍不合格才失败。

**我们已经有等价物**：`summary_is_usable`（`llm_helpers.py:80-92`）+ `_SUMMARY_NUDGE`（`:73-77`）。**这是 P1，不在本文范围，但它说明我们的 L4 骨架本来就是照着这个形状建的。**

### 3.5 它踩过的坑（我们要避开）

`compaction.ts:158-163` 注释：2026-09 前（#48058）的 update prompt 是"preserve-first"，每次更新都把细节原样带下去，summary 无限膨胀。修法是：旧 checkpoint 若含 `## Additional Context`，用一次性的重写指令把它降到当前粒度，后续更新跳过。

**含义**：合并若无界，会退化成"摘要只增不减"，比现在的丢失更糟（占满窗口）。**合并必须有界**。

### 3.6 它没有的（我们有的，别对齐掉）

全仓无 `circuit` —— 没有 L4 断路器。我们有 `_L4_BREAKER_THRESHOLD`。

---

## 4. 影响面 / 执行范围

| 文件 | 改动 | 量 |
|---|---|---|
| `ai_coding/core/context_compression.py` | 提 `_CHECKPOINT_MARK` 常量；新增 `_split_checkpoint()`；`_l4_head_text` 调用它产出两段式 | ~25 行 |
| `ai_coding/ai/llm_helpers.py` | 系统提示词增加"输入结构 + 合并规则"；合并模式放宽 `max_tokens` | ~15 行 |
| `tests/test_context_compression_l4.py` / 新测试文件 | RIPR 四层 | ~120 行 |

**不改**：`Compactor` / `SessionData` / `agent_loop` / `request_assembly` / 前端 / 配置 / 持久化格式。
**不动契约**：`summarizer: Callable[[str], Awaitable[str|None]]` 保持单参数（见 §5 方案对比）。

---

## 5. 契约设计：三个候选

### 方案 A — 纯提示词（零契约变更）
只改 `_SUMMARY_SYSTEM_PROMPT`，加一句"若输入中含 `[以下为早期对话摘要]` 开头的段落，说明这是上一次压缩的结论，请合并……"。

- 成本：1 个文件，改 1 个字符串
- 检测发生在**模型**内部，不在我们的代码里
- 缺陷：**无法在不调真模型的情况下测试**（RIPR 的 R 层只能测字符串）；检测靠模型认字符串

### 方案 B — 显式 `previous` 参数
`summarize(text, previous: str | None = None)`。`_run_l4` 拆出旧摘要单独传。

- 优点：判定在我们的代码里，**可用 fake 断言 `previous is not None`**；提示词确定性构造
- 成本：契约变更。`context_compression.py:336` 签名、`llm_helpers.py:104` 闭包、**全部测试 fake**（`test_context_compression_l4.py` 至少 5 处、`test_agent_loop_memory.py:243`、`test_compaction_policy.py` 的 `_collector`）需接受新参
- 风险：本仓库对注入的可调用对象有明确容忍约定（`compress_if_needed` 优先、`_compactor_extras` 按签名过滤），需要同样处理，否则旧注入器炸

### 方案 C — `_run_l4` 拆帧 + 提示词无条件描述两段式 ★推荐
`_run_l4` 用 `_split_checkpoint(messages)` 把旧 note 从 head 里**摘出来**，再以确定的两段式拼回去：

```
<previous-checkpoint>
{旧摘要正文}
</previous-checkpoint>

<conversation-since-checkpoint>
{新增对话}
</conversation-since-checkpoint>
```

系统提示词**无条件**说明这两段各自的含义（没有旧 checkpoint 时第一段为空）。检测仍在模型侧，但**输入结构由代码保证**，模型不需要"认出"标记，只需要"按标签读"。

- 优点：**零契约变更**（fake 一行不动）；拆帧是纯函数，`_split_checkpoint` 可单测；RIPR 的 R 层能真正验证"旧摘要被送进了摘要器，且是与新增内容分离的一段"
- 成本：`context_compression.py` 一个新私有函数 + `_l4_head_text` 改几行
- 对比 OpenCode：结构上更弱（我们是**文本分段**，它是**消息类型**），但我们没有消息类型可用——`ChatMessage` 只有 `role/content`，`role="user"` 的 checkpoint note 无法与用户消息在类型层区分，强行加类型是改领域模型，代价远大于收益。

**推荐 C。** 理由：契约零变更 + 可测 + 与本仓库既有约定一致（`_joined_history_text` 已是文本层渲染器，再加一个分段渲染器不是新轮子）。

---

## 6. 逐文件实施步骤（方案 C）

### 6.1 `context_compression.py`

**步骤 1**：把内联字面量提为常量，紧邻既有的 `SNIPPED_HEADER`（`:25`）
```python
#: 上一轮 L4 产出的结论。提为常量是因为现在需要**识别**它，而不只是写出它。
CHECKPOINT_MARK = "[以下为早期对话摘要]"
```

**步骤 2**：新增纯函数
```python
def split_checkpoint(messages: Sequence[ChatMessage]) -> tuple[str, list[ChatMessage]]:
    """把上一轮压缩的结论与它之后的对话分开。

    只有一条：`_run_l4` 产出的 note 永远是 head 的第 0 条（它返回 `[note, *tail]`）。
    没有它就返回 ("", list(messages)) —— 首次压缩的路径逐位不变。
    """
```
契约：入参任意长度消息列表；出参 `(checkpoint 正文, 其余消息)`。**纯函数，无 I/O，可单测。**

**步骤 3**：`_l4_head_text` 改用它
```python
def _l4_head_text(self, messages):
    head = messages if self._l4_keep_tail >= len(messages) else messages[: -self._l4_keep_tail]
    checkpoint, rest = split_checkpoint(head)
    body = _joined_history_text(rest)
    if not checkpoint:
        return body                      # 首次压缩：逐位不变
    return (
        f"<previous-checkpoint>\n{checkpoint}\n</previous-checkpoint>\n\n"
        f"<conversation-since-checkpoint>\n{body}\n</conversation-since-checkpoint>"
    )
```

**步骤 4**：`_run_l4:506` 用常量替换字面量
```python
note = ChatMessage(role="user", content=f"{CHECKPOINT_MARK}\n{summary}", timestamp="")
```
并把 `CHECKPOINT_MARK` 加进 `__all__`（与 `SNIPPED_HEADER` 同列）。

**不变**：管道顺序、`_L4_BREAKER_THRESHOLD`、`hard_trim`、`sanitize_pairs`、`_ensure_pinned`、签名。

### 6.2 `llm_helpers.py`

**步骤 1**：系统提示词追加输入结构说明 + 合并规则（中文，贴现有风格）
```
输入分两部分，用 XML 标签隔开：
<previous-checkpoint> —— 上一次压缩的结论。若为空标签则表示这是首次压缩，没有已有结论。
<conversation-since-checkpoint> —— 上一次压缩之后新发生的对话。

若<previous-checkpoint> 非空，这是**更新**而非重写：
- 新内容优先，但默认保留旧结论中仍然有效的信息；
- 仅当新内容明确推翻、取代、解决或使其失效时才移除；
- 保持各小节结构不变，只更新内容，不要新增或删除小节；
- 逐字保留文件路径、命令、标识符、错误串。
总结的长度不得超过 <previous-checkpoint> 的两倍。
```

**步骤 2**：合并模式的 `max_tokens` 放宽
首次 800；合并给 1200（合并后的 checkpoint 更丰富，800 会截断）。**并在超限时做长度校验**——超过 `len(checkpoint) * 2` 的视为不合格，触发已有的 NUDGE 重试。

**步骤 3**：`_MAX_SUMMARY_CHARS` 截断改为**不静默**
```python
text = text if len(text) <= _MAX_SUMMARY_CHARS else text[:_MAX_SUMMARY_CHARS] + "\n[历史过长，此处截断]"
```
一行，消除"静默丢尾巴"。（§2.3 顺带项，不属于 P0 核心，但同一处代码。）

### 6.3 不做的事（防止范围蔓延）

- 不加 `summarizer` 参数（方案 A/B 都否决）
- 不改 `ChatMessage` 领域模型
- 不引入"摘要版本链"或 `summary_of_summary` 元数据
- 不改模板小节数（除非你拍板，见问卷 Q2）
- 不碰 `/compact` 命令、meter、归档

---

## 7. 数据流（改后）

```
                      ┌─ 首次压缩 ─────────────────────────────┐
messages(74) → L3 → head(全部74) → split_checkpoint → checkpoint=""
                 → snip → L2 → L4(head_text = 裸对话)
                 → summary1 → note("[以下为早期对话摘要]\n" + summary1)
                 → out = [note, *tail6]

                      └─ 二次压缩 ─────────────────────────────┐
[note, *tail6, 新增30条]
   → head = 除最后6条外全部
   → split_checkpoint(head)
        ├─ checkpoint = summary1 正文      ← 从 head 中摘出
        └─ rest      = note之外的 33 条    ← note 不再混在里面
   → head_text = "<previous-checkpoint>\n{summary1}\n</previous-checkpoint>\n\n
                  <conversation-since-checkpoint>\n{33条}\n</conversation-since-checkpoint>"
   → L4 → 模型看到两段 → 合并 → summary2（一份，不是两份）
   → out = [note2, *tail6]                ← 只有一份 checkpoint
```

**与改前的差别只有一处**：note 不再以 `[user]` 的身份混在对话里，而是被提到一个**有标签、与对话分离**的位置。

---

## 8. RIPR 用例

### R（失败用例 — 改前必挂）

| # | 用例 | 断言 | 改前 |
|---|---|---|---|
| R1 | 二次压缩时，旧摘要不与对话混为一体 | 送入摘要器的文本含 `<previous-checkpoint>` 且**不含** `[user] [以下为早期对话摘要]` | 🔴 挂 |
| R2 | 旧 checkpoint 正文确实被送进去了 | 送入文本含第一次摘要里的金丝雀 | 🟢 过（但 R1 挂） |
| R3 | 二次压缩后上下文里**只有一份** checkpoint | 输出里 `以下为早期对话摘要` 出现次数 == 1 | 🟢 过 |
| R4 | 合并输入超出截断上限时留下痕迹 | 文本尾部含 `[历史过长，此处截断]` | 🔴 AttributeError（无此行为） |

### I（忽略用例 — 改后行为必须不变）

| # | 用例 | 断言 |
|---|---|---|
| I1 | **首次压缩**的 head_text 与改前**逐位相同** | 无 checkpoint 标签时输出 == `_joined_history_text(messages[:-6])` |
| I2 | 无 checkpoint 的历史走 `split_checkpoint` | 返回 `("", 原列表)` |
| I3 | 历史短于 `l4_keep_tail` | 不切片，不崩 |
| I4 | 用户消息里含 `<previous-checkpoint>` 字面量 | **不被误当 checkpoint**（只有 `_run_l4` 的 note 才是） |

> I4 是这套方案最容易出的 bug：标签字符串是公开的，用户完全可能在贴日志时写进去。`split_checkpoint` **必须**只认 `role == "user"` 且 `content.startswith(CHECKPOINT_MARK)` 且**位于 head 第 0 条**。

### P（通过用例）

| # | 用例 | 断言 |
|---|---|---|
| P1 | 首次压缩仍产出摘要 + 无占位符 | 回归护栏 |
| P2 | 无摘要器时降级为占位符 | 回归护栏 |
| P3 | 连续三次压缩，每次仍只有一份 checkpoint | 不累积 |
| P4 | `summary_is_usable` 行为不变 | 已有测试覆盖 |

---

## 9. 架构图

```
改前                                    改后
─────────────────────────────         ─────────────────────────────
messages[74]                           messages[ note1, tail6, 新增30 ]
   ↓ head = 前 68 条                      ↓ head = 除最后6条
   ↓                                       ↓ split_checkpoint(head)
   ↓ _joined_history_text                  ├─ checkpoint = summary1   ← 摘出
   ↓   "[user] [以下为早期对话摘要]…"      └─ rest      = 33条
   ↓   "[user] 第一批-34 …"                       （note 不在其中）
   ↓                                       ↓ 拼装
   ↓                                       "<previous-checkpoint>
   ↓                                        {summary1}
   ↓                                       </previous-checkpoint>
   ↓                                       <conversation-since-checkpoint>
   ↓                                        {33条}
   ↓                                       </conversation-since-checkpoint>"
   ↓ summarizer                                      ↓
   ↓ 系统提示："压缩这段历史"（无合并语义）           ↓ summarizer
   ↓                                       系统提示："更新已有 checkpoint"
   ↓                                            ├─ 旧信息默认保留
   ↓                                            └─ 新内容优先
   ▼                                       ▼
summary1（首轮）                          summary2（合并，非叠加）
```

---

## 10. 终局推演（实施后跑一遍，检查中途细节是否成立）

1. **第 1 次压缩**：head_text 无任何标签 = 纯对话 → summary1 → `note1`。行为与今天**完全一致**（I1 保证）。
2. **第 2 次压缩**：`split_checkpoint` 在 head 第 0 条认出 `note1` → summary1 被摘出并加标签 → 模型收到"更新"指令 → summary2 是**一份**合并结论，含 summary1 里仍然有效的条目 + 第 31-74 条的新增要点。
3. **第 3 次压缩**：`note2` 同样被认出 → summary3 合并 summary2 + 新增。**始终只有一份**，不叠加。
4. **用户在第 2 次压缩后手动删掉 `note1`**：下次 `split_checkpoint` 返回 `("", ...)` → 退回首次压缩语义。这是可接受的降级（用户主动删的东西不该被找回）。
5. **摘要器返回 None**：`_l4_failures += 1`，`return messages`（未截断的 snip 结果）→ 断路器照旧。**行为不变**。
6. **用户消息里写了 `[以下为早期对话摘要]`**：I4 保证只有 head 第 0 条且 `startswith` 才算 checkpoint → 不会被误认。
7. **合并后摘要超过 2×旧长度**：校验判不合格 → 追加 NUDGE 重试一次 → 仍不合格返回 None → 降级。不静默接受膨胀（§3.5 的坑）。

**第 7 条是这套方案唯一的新失败模式**（多一次模型调用），换掉的是"无界膨胀"。

---

## 11. 验收口径

- 全量测试绿（当前基线 `2353 passed, 2 failed`——两个 `test_loop_digest` 预存失败，不在范围内）
- `ruff check ai_coding/` + `mypy ai_coding/` 干净
- 真机：造一个两轮压缩的长历史，第 2 轮摘要里能认出第 1 轮的金丝雀，且上下文里只有一份 checkpoint
- 不引入对 `summarizer` 签名的改动（`inspect.signature` 检查点数量不变）
---

## 12. 拍板决策（2026-10-13）

| 问 | 决策 |
|---|---|
| Q1 契约形态 | **方案 C** —— `_run_l4` 拆帧 + 两段式提示词，`summarizer` 签名不动 |
| Q2 摘要模板 | **采纳 OpenCode 7 节全量** |
| Q3 增长控制 | **加长度校验** |

### 12.1 七节映射（中文，沿用本仓库提示词风格）

| OpenCode | 本项目 | 子节 |
|---|---|---|
| Objective | `## 目标` | |
| Requirements | `## 约束与要求` | |
| Decisions | `## 决策` | |
| Work State | `## 工作状态` | `### 已完成` / `### 进行中` / `### 阻塞` |
| Next Move | `## 下一步` | |
| Relevant Files | `## 关键文件` | |
| Important Context | `## 重要上下文` | |

`## 已完成` 与 `## 待办` 消失 —— 前者下沉为 `### 已完成`，后者成为 `## 下一步`。旧 checkpoint 在下一次合并时自动归一到新形状。

### 12.2 一处工程判断（对 Q3 决策的执行细化，已向用户明示）

**字面 2.0 倍会误杀合法的首次合并。** 七节模板比原四节信息量大：旧 checkpoint 可能只有 200 字，一份合格的七节合并摘要约 500 字，`2 × 200 = 400` 会判它不合格 → 追加 NUDGE 重试 → 大概率仍不合格 → 返回 `None` → 降级为 snip 结果，**摘要反而丢了**。

因此判据取 `max(2 × 旧长度, _MIN_SUMMARY_CHARS)`，`_MIN_SUMMARY_CHARS = 1200`（约一份完整七节的容量）。

- 仍能抓住真正的问题：模型无视指令、把整段历史倒进摘要，越滚越大（OpenCode #48058 的成因）
- 不会因模板变胖而误杀正常结果
- 最坏情况的代价是有界的：多一次付费模型调用，然后走既有的 `None` → 降级路径

### 12.3 派生：`max_tokens`

七节 + 合并承接旧结论，比四节 + 首次摘要更长。

| 模式 | max_tokens | 依据 |
|---|---|---|
| 首次 | 1200 | 7 节 × ~3 行 + 3 个子标题，约 30 行 |
| 合并 | 1600 | 多承接一份旧结论 |

### 13.3 Q3 决策的反向修正（实现时的发现）

问卷把 Q3 的选项 B 描述为「判据下沉到 `_run_l4`」。实施时发现该描述低估了自己的代价，因此**执行时反向**：

**1. 不存在「拆模板」问题** — `core` 本来就从 `ai` import `SUMMARY_SECTIONS`（P0 建的依赖方向），`_run_l4` 调用同一个谓词不新增依赖、不拆模板。这部分我当时判断错了。

**2. 但「两个真相源」的代价是真的** — 我在 Q1 里论证过：不给 `_run_l4` 加形状守卫，因为 adapter 已过滤，是冗余。把增长判据也下沉，就是同一个理由下自相矛盾的两套门。

**3. 更具体：下沉会丢掉重试。** 增长违规是模型无视指令、把整段历史倒了出来。正确反应是 NUDGE 重问一次（`build_summarizer:205-217` 的两轮循环）。这个循环在 adapter。下沉到 `_run_l4` → 增长失败直接 `None` 降级，**重试没了**。

**取舍**：为「换一个 summarizer 实现时也能限长」这个**不存在的第二个实现**（今天只有 `build_summarizer` 一个真实实现，其余全是测试 fake），放弃一个**已观测到的真实失败模式**的恢复手段。前者是为假想场景写代码，后者是实打实的鲁棒性。

**执行**：增长判据**保持只在 `build_summarizer`**，不 `_run_l4`。

这与用户「不要过多真相源，要统一好」的指示一致 —— 真相源是 `summary_within_growth_bound` 这一个函数，执行点也只有一处。两个读者（接受/识别）调用的是**查询式**的 `checkpoint_body`，那不是双份真相。
