# T2 根因追查：43 次调用 / 无法深入 / 答案幻觉

> 数据来源：`/workspace/eval-workspace/i2c-T2/`（真机 T2 复跑，43 次工具调用 / 32 个迭代 /
> 30 次压缩 / 301.5s）+ 该目录下的 `sessions/*.json`（79 条消息、checkpoint 2,464 字符）
> + trace 的 1,011 个事件。所有数字都是数出来的，不是估的。
> 日期：2026-10-28。对应方案：`docs/plan-i2-context-integrity.md`（已修的部分不再重复）。

---

## 0. 一句话

**不是"模型不会思考"，是三条链路在互相拆台**：
编排层在模型读到第 4 个文件时叫它别读了（而它每一次读都是新证据）；
记忆层只存结论、不存"我读过什么"，压缩一次就把读过的位置全忘了；
提示词要求"用 offset/limit 读片段"，于是模型把一个 441 行、5k token 的文件分 11 次读，
每次读完就被压缩吃掉。最后收尾压力一来，模型只能凭记忆编细节。

---

## 1. 问题一：43 次调用、无法深入

### 调用构成（数出来的）

| 工具 | 次数 | 说明 |
|---|---|---|
| `read` | **27** | 其中 **11 次是同一个文件** `tools/present_plan_tool.py`（441 行） |
| `bash` | 11 | 全是 `ruff` / `grep` / `find`，**没有一次是验证或修改** |
| `grep` | 4 | 正则扫 `[0]`、`.get()`、除法 |
| `glob` | 1 | 列目录 |

`present_plan_tool.py` 的 11 次读取，offset 依次是：
`195 / 300 / 195 / 140 / 55 / 25 / 195 / 195(失败) / 195 / 245 / 360 / 384`
——**重叠的 30–60 行窗口，反复回到 195**。

### RC-1（编排层，P0）`[search-gate]` 在"每次都是新证据"时仍然开火

trace 里的 `SearchStalled` 事件自己记着：

```json
{"iteration": 3, "level": 1, "calls": 4,  "streak": 4,  "novel": 4,  "repeated": 0, "stale_streak": 0}
{"iteration": 5, "level": 2, "calls": 6,  "streak": 6,  "novel": 6,  "repeated": 0, "stale_streak": 0}
{"iteration":13, "level": 2, "calls": 16, "streak": 10, "novel": 16, "repeated": 0, "stale_streak": 0}
```

`novel` 等于 `calls`（**一次重复都没有**），`stale_streak` 是 0 —— 判断"卡住"的那个信号
根本没触发，门却开了。因为 `interventions.py:172` 只看 `search_streak >= 4`：

```python
if snap.search_streak >= limits.search_streak_threshold:   # 默认 4
    return True
```

L1 的原文（`interventions.py:530`）：

> 「你已经连续 4 次只读检索……**继续读下去，收益会越来越小**」

L2 更直接（`:539`）：

> 「**不要再猜关键词了** —— 让静态检查替你回答：`ruff check .` / `mypy` / `grep -rn`」

**而 `bash` 会重置 streak**（`turn_books.py:89` `ACTION_TOOLS = {write, edit, bash, mcp_call, subAgent}`）。
于是形成一个闭环：**读 4 次 → 被骂 → 跑一次 `ruff`（streak 归零）→ 继续读 4 次 → 再被骂**。
trace 里 11 次 `bash` 全是这么来的（iter 6/7/14/15/16/21/25/27/31，其中 7 和 16 各有两次）。

代价：**32 个迭代里有 11 个花在 ruff/grep/mypy 上**（iter 4 的 3 次 grep + 10 个迭代的 11 次 bash），而模型自己的 checkpoint 已经写明
"ruff check . 未能暴露运行时逻辑缺陷""`[0]` 索引均受 len() 条件守卫"。它知道这条路不通，
但门一直把它推回去。

> 这不是"模型不会深入"，是**编排层在它最该深入的时候叫它别深入**，并给了它一条
> 能立刻"消除告警"的捷径（跑 bash）。这是典型的 reward hacking，只是奖励来自 harness。

### RC-2（记忆层，P0）没有"我读过什么"的账本，checkpoint 只存结论

checkpoint（2,464 字符，8 段）里写的是：

```
### 已完成
读取 ai_coding/tools/present_plan_tool.py 源码
...
## 关键文件
ai_coding/tools/present_plan_tool.py
```

——**没有行号、没有"读到了什么"、没有"哪几段还没读"**。所以压缩之后模型无法区分
"这个文件我读过"和"这个文件我没读过"，只能重读；而它又记不住读过哪些行，于是回到
同一个 offset 195。

现有代码里**已经有**能支撑这件事的数据，只是没接出去：

| 已有 | 位置 | 缺什么 |
|---|---|---|
| `FileStateTracker` 记了每个读过的文件（mtime/size） | `security/file_state_tracker.py` | 只有文件、没有区间；且从不出现在请求里 |
| `TurnDigest.files` | `domain/turn_digest.py` | 只记**改过**的文件（artifacts），不记**读过**的 |
| `TurnDigest.attempts`（D6） | 同上 | 只记失败，不记读取 |

**这就是用户已同意的第 2 项（`files_seen`）要解决的问题**，但根因比"加一个字段"更深：
checkpoint 模板（`SUMMARY_SECTIONS`）是**叙事**结构（目标/决策/工作状态/下一步），
没有"证据"结构。任何只存在于叙事里的东西，压缩一次就退化成形容词。

### RC-3（工具 + 提示词，P0）`read` 无上限，而提示词要求"只读片段"

系统提示词（`ai/prompt.py:58-62`）：

> - Use read — not shell commands like cat… to inspect text files.
> - **Read a line range with offset/limit instead of dumping a whole file.**

工具描述里也是 "Use offset and limit for large files."。

这条建议对 3,081 行的 `agent_loop.py` 是对的（一次整读 = **174,113 字符 ≈ 43k token**，
占 48k 窗口的 89%，第 2 个迭代就把窗口撑爆）。但对 441 行 / 20,648 字符（≈5k token）的
`present_plan_tool.py` 是错的——**它本来一次就能读完，而且 5k token 完全放得进 12k 的 tail 预算**。

结果：模型对"大文件"和"小文件"用了同一套 30 行窗口策略，小文件被切成 11 段，
每段都在下一次压缩里消失。**提示词没有"文件多大才该分段"的判据，`read` 也没有输出上限。**

### RC-4（上下文，P1）tail 以"整条消息"为粒度，装不下一条 43k token 的消息

`split_tail` 的预算 12k token，而一次整文件读是 43k token 的**一条**消息。
它要么整条进 tail（超预算），要么整条进摘要（内容没了）。实际是后者：

```
iter 2  读取 agent_loop.py → 单条 174,113 字符
iter 4  CompactionEnded: 62,035 tokens → 318 tokens
```

摘要对工具结果的渲染上限是 `_SUMMARY_TOOL_CHARS = 2,000` 字符（`context_compression.py`），
所以 174k 字符的文件进摘要时只剩 2k 字符，模型再写 200 token 的结论——**原文没了，
只剩"我读过 agent_loop.py"这句话**。

---

## 2. 问题三：答案幻觉

### 事实

模型最终答案是 `domain/plan_delivery.py:81`：

```python
"turns": [int(self.turns[0]), int(self.turns[1])],
```

这一行确实存在。但它同时写了：

> 构造函数签名（第 18-46 行的 `__init__`）：`turns: tuple[int, str] | None = None`
> ……默认值是空元组 `()`：当不传 `turns` 参数时，`self.turns = ()`，此时 `to_dict()`
> 调用 `self.turns[0]` → 直接 `IndexError`

文件里（`domain/plan_delivery.py:15-30`）实际是：

```python
@dataclass
class StepDelivery:
    ...
    turns: tuple[int, int] = (0, 0)
```

**没有 `__init__`，没有 `None`，默认值是 `(0, 0)` 不是 `()`。**
trace 里它只在 iter 34 读了一次 `offset=60, limit=50`（第 61–110 行），
**第 1–59 行它从未读过**。它编了没看过的那一半。

### RC-5（编排层，P0）收尾压力 + 没有"证据不足就说不知道"的出口

`turn_policy.budget_pressure`（`:601`）在迭代到 80% 时注入：

```
[budget]（步数 32/40）。
请用这一轮收尾：把已完成的和还差的讲清楚，不要再开新的工作。
```

trace 里这个 block 从 iter 32 开始出现，iter 34 还在读，iter 35 就交了答案。
**"不要再开新的工作" + 任务本身要求"只报一个，但要讲透"**，最省力的满足方式就是：
拿手上已有的半截证据，把缺的部分补圆。

### RC-6（提示词，P0）系统提示词里没有任何引用/归因要求

`ai/prompt.py` 的全部约束（`GENERAL_METHODS`，4 条）：

```
- Read before write: never edit a file you have not read this turn.
- Declare parallel-safe tool calls truthfully; independent reads may batch.
- When blocked after two attempts, ask the user instead of repeating the same call.
- Verify the change (run the relevant test/command) before reporting it done.
```

**四条里没有一条关于"结论要有证据"**。最后一条是"改动要验证"，不是"判断要有出处"。
所以"我没看过这段，但我知道它长什么样"在提示词层面是完全合法的。

### RC-7（缺校验，P1）答案没有任何落地校验

`path:line` 存在、且那一行确实含所引代码——§8 的判据全部通过。
但它引用的**上下文**（构造函数）不存在，没有任何机制发现这件事。

---

## 3. 共同根因

> **记忆层存的是"叙事"，不是"证据"。**

- 对问题一：叙事里没有"读过哪些行"，所以只能重读（RC-2）。
- 对问题三：叙事里没有"哪些事实是我亲眼看到的"，所以缺的部分由语言模型补齐（RC-6 无约束 + RC-7 无校验）。

两个问题在数据上是同一个：**上下文里没有一个"可引用的、带位置的、压缩后仍存在的证据表"**。

---

## 4. 别人怎么克制的（调研）

| 来源 | 做法 | 对我们的含义 |
|---|---|---|
| **Codex `review/rubric.md`** | "It is **not enough to speculate** that a change may disrupt another part of the codebase; one must **identify the other parts that are provably affected**"；"**Do not fabricate citations**"；"The bug does not rely on **unstated assumptions**" | 把"可证明"写成硬条件，而不是"讲透" |
| **代码评审类 agent skill**（社区通行写法） | "**Cite `file:line` for every claim.** A finding with no citation is not a finding."；"**`unknown` is a real answer.** Anything you cannot determine from the code stays unknown; it never becomes a pass." | 两条规则直接针对本次幻觉 |
| **CiteGuard**（ACL 2026） | 检索增强的引用校验：把答案里的每条引用拿回原文核对，不一致就改写 | 事后校验，成本是二次模型调用 |
| **MARCH**（arXiv 2603.24579） | Solver / Proposer / Checker 三角色，把答案拆成**原子断言**逐条核 | 重，但可以只对"结论段"做 |
| **urlhealth**（arXiv 2604.03173） | 对引用做"存活/失效/幻觉"三分类，agent 自纠后不落地引用降到 <1% | 思路：**引用必须能被机器验证** |
| **Perplexity Sonar** | 输出结构化 citation 指针，而不是让模型自由写引用 | 把引用变成数据，不是文本 |

共同点：**不是让模型"更小心"，而是让"没有出处"在结构上不成立**。

---

## 5. 建议的修复（按根因，不按文件）

### 对 RC-2（记忆层）—— 用户已同意的第 2 项，但要做成"证据表"而不是"文件清单"

- `TurnDigest` 加 `reads: tuple[(path, start, end, note)]`，`FileStateTracker` 提供数据源；
- checkpoint 模板加第 9 段 `## 已读证据`（path:start-end → 一句结论），**只追加不重写**；
- 渲染进 `<working-memory>` 时按文件聚合、压缩成一行一条。

这一条同时修 RC-2 和（部分）RC-6：模型能看见"我读过 A:1-60，结论是 X"，就不会编 A:18-46。

### 对 RC-1（编排层）—— 门要看"证据是否增长"，不看"读了几次"

`SearchStalled` 已经记了 `novel` / `repeated` / `stale_streak`，只是没用：

- 触发条件从 `streak >= 4` 改为 `novel == 0 and stale_streak >= 3`（或 `novel_ratio < 0.3`）；
- L2 的"让静态检查替你回答"改成"先确认你的假设"（静态检查找不到语义缺陷，这是模型自己
  都写进 checkpoint 的结论）；
- `bash` 不再无条件重置 streak：只有 `bash` 的**输出改变了证据**（新增文件/新失败）才重置。

### 对 RC-3（工具 + 提示词）—— 按大小决定要不要分段

- 提示词改成："整文件 ≤ N 行（或 ≤ M 字符）就一次读完；超过再分段"；
- `read` 加输出上限（比如 40k 字符）并明确说"已截断，用 offset 继续"；
- 配合 RC-2 的证据表，分段读取有地方记。

### 对 RC-4（上下文）—— tail 里给超大消息一个窗口，而不是整条丢进摘要

- `split_tail` 遇到单条超过预算的消息时，用 L2 的信封（头+尾窗口 + 落盘路径）把它放进 tail，
  而不是整条排除。

### 对 RC-5 / RC-6 / RC-7（幻觉）—— 三条一起才有效

1. **提示词**（RC-6）加两条，抄 Codex/社区写法：
   - "每条结论必须给出 `path:line`；给不出的，写 `未确认`，不要补全。"
   - "只引用你**本轮实际读到**的内容；没读到的部分一律 `unknown`。"
2. **收尾块**（RC-5）改成有出口的版本：
   "如果证据不足以支撑结论，明确写出'未确认'和缺什么，不要编。"
3. **落地校验**（RC-7）：对答案里每个 `path:line` 做机器校验——行存在、且该行内容与
   答案引用的片段一致；不一致就把答案退回重写一次（只退回一次，不循环）。

---

## 6. 不做的事

- 不引入第二个模型做 Checker（MARCH 那套）——成本翻倍，先用规则+一次校验；
- 不改 `max_tool_iterations`（40 是评测设定，不是产品默认）；
- 不为 T2 这种开放式任务硬凑"≤25 次调用"的判据——先修根因，再看数字。
