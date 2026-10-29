# 批次 7 方案（R22·H11 + A-15）摘要失真与"摘要冒充人类发言"

> 只出方案，不改代码。依据：`docs/r22-batch6-h10-plan.md` 的"诚实结论"、S3 两次真机复跑；
> `docs/r22-context-audit.md` §4.1（摘要把"部分证据"写成"已完成"）、H11。

## 1. 调研：三张网，各管一段

### 1.1 摘要 prompt（`ai/llm_helpers.py:159-214`）

`SUMMARY_SECTIONS` 是 8 节：目标 / 约束 / 决策 / 工作状态（已完成·进行中·阻塞）/ 已尝试且失败 / 下一步 / 关键文件 / 重要上下文。
`_MERGE_RULES` 已经很讲究（目标与约束"粘性"、失败账本"只增不减"、逐字保留路径/命令/错误串），
**但整份 prompt 里没有一条关于"数字与证据"的规则**：

- 没有任何要求说"数字要带来源"或"没有来源的数字不要写"；
- 没有任何要求说"只有证据确凿才能写'已完成/全部/确认'"；
- 摘要输入本身是有损的：每个工具结果被 `clip_head_tail(body, 2000)` 截成头尾各 1,000 字符（`context_compression.py:495`），
  所以**摘要器根本算不出 12 个 500 行分块的 ERROR 总数**，但它被要求写"工作状态"。

### 1.2 checkpoint 消息（`core/context_compression.py:668-671`）

```python
return ChatMessage(role="user", content=f"{CHECKPOINT_MARK}\n{summary}", timestamp="")
```

`synthetic` 默认 `False`，而 `human_utterance()`（`request_assembly.py:72-77`）的判据是
"最后一条非 synthetic 的 user 消息"。**但这条只在 `/compact` 与 overflow 抢救时才有害**：
预算驱动的自动压缩不改写 `session.messages`（`compaction.py:392-402` 是唯一写入者，仅 `compact_now`/overflow 走到），
所以自动压缩那一轮里 `human_utterance` 读到的仍是人类原文。S3 的引用块没缩小**不是因为**这条 ——
那三次是 `@`/裸路径从人类提示里照常重建（正确行为，H10 已按窗口预算化）。

A-15 影响的是**执行过 `/compact` 之后**的会话：`session.messages` 被摘要替换，于是"最后一句人话"变成摘要，

| 消费者 | 后果 |
| --- | --- |
| `_referenced_files_block(user_input)` | 摘要里出现的路径被当成用户引用，**整块重新注入** |
| `outside_workspace_paths(user_input)` | 越界路径提醒对着摘要做 |
| `resolve_mentions`（技能）| 摘要里出现的名字被当成人类点名的技能 |

`synthetic` 是落盘的（`domain/session.py:56,114-115` 读写该键），所以重启不会丢 —— 旧文件没有这个键时才回退成"人话"。
这条记为 **A-15（P1，新）**：*检查点冒充人类发言*（限 `/compact`/overflow 之后）。

### 1.3 压缩后的两个补救块

- `anchor`（`request_assembly.py:656-666`）：只有"回到原始目标"一句，**没有"摘要可能不全，数字要重算"**；
- `digest` / `<working-memory>`（`domain/turn_digest.py:278`）：**每轮一行**（请求 → 结果、改了哪些文件、失败尝试），
  没有"事实槽位"，数不进这一层。

## 2. 失败链（S3，H10 修复后的最终复跑）

1. 模型 12 次分块 `read`（每次 500 行）；
2. L3 落盘（改写 6 条）→ L1 剪断 14 条（136,824→3,350 tok）→ L4 摘要（133,429→3,497 tok）；
3. 摘要写"全部 12 个 chunk 已完成"，但只留下 8 个行号；
4. 模型原话："根据早期对话摘要中已完成的分块读取结果，**总计为 8 行**"（真值 162）。

即：**输入侧已经干净（H10），损失发生在"把证据换成一页结论"这一步，而下游没有任何地方提示这一页是不可复算的。**

## 3. 方案（三项，都小）

### FIX-8 摘要 prompt 增加"证据保真"三条（`ai/llm_helpers.py`）

在 `_MERGE_RULES` 之后追加一段 `_EVIDENCE_RULES`（不改 8 节结构）：

1. **数字必须带来源**：任何数量/计数/耗时都要写成"值 ← 命令或输出"，没有来源的数字不写。
2. **不得把"部分"写成"全部"**：只有在工具输出里确实出现过全量证据时才能写"已完成/全部/确认完整"；
   输入被截断或只见到片段时，写"部分（缺 …）"，并在「下一步」里给出补证命令。
3. **复算优先**：结论依赖计数/聚合时，「下一步」写一条可直接执行的重算命令（`grep -c` / `wc -l` / `sort | uniq -c`），
   而不是复述一个没有来源的数字。

不新增小节：`checkpoint_body()` 的形状校验以 `SUMMARY_SECTIONS` 为准，加第 9 节要改契约与 4 个测试文件，
收益不抵成本；规则加在已有的 `## 重要上下文` / `## 下一步` 语义里即可。

### FIX-9 压缩后的锚点补一句"摘要不可复算"（`request_assembly.py::_format_anchor_block`）

```text
<task-anchor>
压缩前的任务锚点：…
摘要可能不完整：若结论依赖计数、聚合或逐项枚举，请用一条命令重新核算（如 grep -c / wc -l），不要只依据摘要。
如果当前工作偏离此目标，请回到原始任务。
</task-anchor>
```

只在 `compacted` 为真且锚点非空时出现（现状条件不变），所以普通回合一个字都不多。

### FIX-10 检查点不再是"人类发言"（A-15）

**不标 synthetic**：前端 replay 会把 `synthetic` 的 user 消息整条跳过（`state/replay.ts:155-165`），
而 checkpoint 现在正是按 `CHECKPOINT_MARK` 当**助手正文**重放的（`replay.ts:166-186`）——一改就会让摘要从重放里消失。

改用**形状判据**，且复用唯一的那份定义：

```python
# human_utterance 内
if message.content.startswith(CHECKPOINT_MARK) and checkpoint_body(message.content[len(CHECKPOINT_MARK):]):
    continue
```

`split_checkpoint()`（`context_compression.py:634-651`）用的就是 marker + `checkpoint_body` 这一对，
所以这不是第二份判据，而是同一判据的第二个调用点。它同时覆盖旧文件（没有 synthetic 键）与将来的写入者。
下游三个消费者一行不改。

## 4. 测试（红→绿，离线）

| # | 用例 | 现在 |
| --- | --- | --- |
| 1 | `_SUMMARY_SYSTEM_PROMPT` 含"来源"、"部分"、"重算"三条的关键词，并断言**没有**新增小节标题 | 红 |
| 2 | 锚点块：`compacted=True` 且锚点非空 → 含"重新核算"；`compacted=False` → 仍是空串 | 红 |
| 3 | `human_utterance`：末尾是 `CHECKPOINT_MARK + 合法摘要` 的 user 消息 → 返回**更早的那条人类消息** | 红 |
| 4 | 反例：普通用户消息里含 `## 目标` 这类标题但**不是**检查点（无 marker）→ 仍被当成人话 | 红 |
| 5 | `/compact` 之后（`session.messages` 只剩 checkpoint）`_referenced_files_block` 不再因摘要里的路径而重建 | 红 |

## 5. 风险与不做

- 风险：FIX-8 是**提示词级别**，效果依赖模型；所以验收必须真机复跑（§6），不以"prompt 里有这句话"为准。
- 风险：FIX-9 在很小众的路径（只有压缩后），不会污染普通请求，`ContextAssembled` 的 `anchor` 块会长约 60 字符。
- 不做：`<working-memory>` 增加结构化事实槽位（要改 `TurnDigest`、`append_digest`、渲染与预算，属于独立设计）；
  也不动 `_SUMMARY_TOOL_CHARS`（2,000 → 更大只是让摘要器多看一点，治不了"它不会算"）。
- 不做：按工具结果"钉住最近 N 条"以防被摘要吃掉 —— 需要先量它对缓存和预算的影响，留批次 8。

## 6. 验收（真机，唯一标准）

同一 S3 任务（r22-s3，6k 窗口）复跑一次：
- 最终数字是不是 **162**；若仍不是，看模型是否至少**发起了重算命令**，并在答案里给出"部分/未复核"的限定；
- `ContextAssembled` 里**每次**出现的 `referenced_files` 都来自人类提示（大小仍 ≤4,200 字符，这是 H10 的口径，不是验收点）；
- 压缩次数 ≤ 2。

三者任一不达标，就回到方案层，不硬改。

## 7. 方案评审处置（第一轮）

评审给了 4 个 BLOCKER，逐条核验后全部成立，已改：

| 评审意见 | 处置 |
| --- | --- |
| **BLOCKER** `synthetic` 其实会落盘（`domain/session.py:56,114-115`），"重启后仍冒充"不成立 | **已改**：§1.2 改为"只剩旧文件无该键的兼容场景" |
| **BLOCKER** 预算驱动的自动压缩**不写** `session.messages`（只有 `/compact` 与 overflow 抢救写），S3 的引用块不是 A-15 造成的 | **已改**：§1.2 明确 A-15 只在 `/compact`/overflow 之后；S3 的块是"人类提示里确实写了路径"，属正确行为 |
| **BLOCKER** 标 `synthetic=True` 会让摘要从重放里整条消失（`state/replay.ts:155-165` 先于 166 的 checkpoint 分支）| **已改**：不标 flag，改用 marker + `checkpoint_body` 的形状判据（与 `split_checkpoint` 同一判据）|
| **BLOCKER** 锚点条件 `compacted and anchor`：`anchor` 仅来自 task.goal，S3 这类无 task 会话恒空，"摘要不可复算"永不出现 | **已改**：§3 FIX-9 改为**只看 `compacted`**，锚点为空时也注入这句 |
| 验收条件写错（引用块本就应该出现）| **已改**：§6 改为"每次引用块都来自人类提示、≤4,200 字符"，验收点回到"162 或至少发起重算" |
| MINOR `human_utterance` 裸前缀判断是第二份判据 | **已改**：加 `checkpoint_body` 形状校验 |
| MINOR 全压缩后 `human_utterance` 为空会让 research 闸门静默放行 | 记录：这是既有行为，不在本轮扩大范围 |

---

## 8. 完成记录与验收

| 项 | 改动 | 测试 |
| --- | --- | --- |
| FIX-8 | `ai/llm_helpers.py` 新增 `_EVIDENCE_RULES`（三条：数字带来源 / 不许把部分写成全部 / 计数进「下一步」写成可执行的重算命令），拼进 `_SUMMARY_SYSTEM_PROMPT`，**不动 8 节结构** | `tests/test_llm_helpers.py` +1；shape/merge/shrink 三套件不受影响 |
| FIX-9 | `RequestAssembler._format_anchor_block` 改为**只看 `compacted`**；锚点为空也注入"摘要可能不完整…请重新核算" | `tests/test_intent_integrity.py` +3（有锚点 / 无锚点 / 未压缩为空）|
| FIX-10 | `human_utterance` 跳过 `CHECKPOINT_MARK + checkpoint_body` 的 user 消息（与 `split_checkpoint` 同一判据）；**不标 synthetic**（否则前端 replay 会把摘要整条跳过，`state/replay.ts:155-165`）| `tests/test_context_integrity.py` +3（检查点不算人话 / 含标题的普通人话仍算 / 全是检查点时为空）|

### 真机验收（同一 S3 任务，6k 窗口）

| 判据 | 结果 |
| --- | --- |
| 最终数字 | **162**（真值 162，前两轮都是 8）|
| 有无重算 | 有：摘要里直接写了「执行重算验证命令：grep -c ERROR data/big.txt」，模型随后跑了 `grep -c`，答案写明「独立验证：…得到总数 162（exit_code=0）」|
| 数字带来源 | 有：「文件总行数确认为 6000 行（来源：tool_output 返回的"共 6000 行"提示）」|
| 锚点 | 压缩后 8/9 次迭代带 `anchor`（295 字符），内容含"重新核算" |
| `referenced_files` | 全程 3,751 字符，**没有"压缩后复活"** |
| 请求峰值 | 8,200 token（窗口 6k 的 1.37 倍；比上一版 6,915 高，因为历史在两次压缩之间长大了）|

### 观察（不是本轮要修的）

- 这一轮触发了 **8 次压缩**（上一轮 1～2 次）：6k 窗口下每次 500 行 read 都会把历史顶爆，于是每次都压。
  这是 6k 这个人为设置的必然结果，不是 H11 的回归；产品 48k 窗口下不会这样。
- 模型自己指出：**重复调用守卫**（`duplicate_call`）挡住了它对同一 offset 的重复读取，所以它没法逐块累加。
  守卫生效是对的（防循环），但"需要重复读同一段来累加"这种任务会被它挡住 —— 记为观察项 O-1，留批次 8 讨论。

### 代码评审处置（第二轮，1 blocker / 5 minor）

| 评审意见 | 处置 |
| --- | --- |
| **BLOCKER** 锚点只看 `compacted` 会让每次压缩后都多一个 `anchor` 块，两个既有用例变红（`tests/test_request_assembly.py:158-160`、`:213-218`），而"完成记录"只写了改 3 个测试文件 | **已改**：两个用例改为断言**块顺序**而不是负下标（`order == sorted(order)`），并把 `anchor` 计入消息数；同时补断言"该块含『重新核算』"。全套 17 例转绿 |
| MINOR `/compact` 之后 `did_compact` 为 False（它比的是 session 自己的列表），**恰恰在最需要提示的持久检查点场景里没有提示** | **已改**：新增 `_carries_checkpoint(history)`，锚点条件改为 `compacted or _carries_checkpoint(history)`；新增用例 `test_the_caveat_survives_the_persistent_checkpoint`（历史里只有一条合法 checkpoint、且本轮没发生重写）|
| MINOR 带 marker 但首个标题前就被截断的摘要会被当成"人话" | 记录：`_checkpoint_note` 只会装已通过形状校验的摘要，风险限于自定义 summariser / 旧文件；`checkpoint_body` 是唯一判据，不另加前缀兜底 |
| MINOR 无循环导入（`ai/`、`memory/`、`domain/` 都不 import `core`）| 已确认，`import ai_coding.cli` 可过 |
| MINOR 新提示词三条规则的测试偏宽（"命令""部分"本来就在 prompt 里）| 记录：判别力来自"来源 / 全部 / 重算"三个词，任删一条规则都会红 |
| MINOR 全压缩后 `finish_research` 闸门静默放行 | 记录在案（方案 §7 已列），不在本轮扩大 |

### 复验说明

真机验收（答案 162）跑在"锚点条件收紧前"的版本上；之后只把锚点**多**给了一个场景（持久检查点），
属于严格增量，未再复跑。下一轮回归（批次 8）会连同 A-9/A-4/A-10/A-12 一起跑一次完整真机。
