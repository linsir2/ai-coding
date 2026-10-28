# R20 方案 v2：证据表 / 检索门 / read 分页 / 引用校验

> 输入：`docs/r20-t2-rootcause.md`（RC-1 ~ RC-7，全部有 trace 证据）。
> v1 经过两个子智能体评审（A/B 两块分开审），**B 块的核心公式被推翻**，
> 另有 4 处事实错误。本文件是 v2，逐条标注了评审结论。
> 状态：**待拍板**。v1 的 4 个决策 + v2 的 3 个新决策（5/6/7）已全部拍板，见 §0。

---

## §0 决策

### v1 已拍板（不变）

| # | 决策 | 结论 |
|---|---|---|
| 1 | 证据表放哪 | **会话级 volatile `<read-ledger>` 块**（§1 给出三家调研依据） |
| 2 | 引用校验出问题怎么办 | **退回重写一次** |
| 3 | read 一次最多返回 | **500 行** |
| 4 | search-gate 触发条件 | **改成「无新发现才开火」** |

### v2 新增（评审后需要拍板）

| # | 决策 | 选项 | 我的推荐 |
|---|---|---|---|
| 5 | 引用校验的**触发面** | (a) 窄；(b) 宽：再加"文件从未被任何工具读过"也退回 | **已拍板：(b) 宽**（§4.11 给出三档规则） |
| 6 | L2 文案要不要改 | (a) 只改触发条件；(b) 触发条件 + L2 文案都改 | **已拍板：(b)**（要改 1 条既有测试，§4.8 说明理由） |
| 7 | 引用校验的**退回时机** | (a) `_verify_acceptance` 之前；(b) 之后、`decide_after_step` 之前 | **已拍板：(b)**（§4.11） |

> 决策 5 选 (b) 带来一个必须一起解决的问题：**`seen_paths` 不能从
> `session.messages` 现算**。压缩会把早期工具结果摘要掉（rootcause RC-4：
> 工具结果在摘要里只剩 2,000 字符），现算会漏掉早期 grep/glob 看到的路径 →
> 把合法引用误判成"从未见过"。所以 `seen_paths` 要**落盘 + 增量累加**（§4.2/§4.6）。

---

## §1 先回答那个问题：compact 之后，"我读过"还有效吗

**内容不在了，指针还在。三个参照实现里有两个明确按这个分工设计。**

### opencode（`packages/core/src/session/compaction.ts:171-181`）

摘要模板里只有一节 `## Relevant Files`：

> List the files and directories … **that another agent would need to open to continue
> this work. Include at most 15, most important first. Do not list every file that was
> read or changed.**

配套规则（`SUMMARY_RULES`）：

> **It is fine to leave out information the next agent can recover from the code or the
> files listed above.**

即：**opencode 故意不保留文件内容，只留"去哪再读一次"的指针**，并明说"下一个 agent
可以从代码里重新拿到"。

### codex（`prompts/templates/compact/prompt.md`）

4 条：进度与决策 / 重要上下文与约束 / 剩余工作 / 继续所需的关键数据。
**连文件清单都没有**，完全依赖重读。

### Claude Code（社区逆向，9 段摘要）

唯一**把内容带过去**的：`Files and Code Sections (with full snippets)`。
代价是摘要体积大；收益是压缩后不必重读。

### 结论（决定设计）

1. **compact 之后文件内容一定没了**（三家里只有 Claude Code 例外）。
2. 所以证据表**不能假装内容还在**，它必须记录"读过哪些行 + 内容已不在上下文"，
   并明确告诉模型：**要内容就按缺口重读**。
3. 证据表必须放在**每轮重建的 volatile 块**里——它不在 history 里，
   **压缩根本碰不到它**。

> 一句话：**我们记的不是内容，是"我查过哪里、哪里还没查"。**

---

## §2 评审结论：v1 被推翻 / 修正的地方

两个子智能体分别审 A+B 与 C+D，全部结论我都逐条在代码里复核过。**下面每一条都是实测的。**

| # | v1 的说法 | 评审结论 | 实测证据 | v2 怎么改 |
|---|---|---|---|---|
| 1 | 新增 `novel_in_streak`，`looks = streak - min(streak, novel)` | **FAIL：重复造轮子 + 公式错** | `stale_streak` 已存在且语义就是"连续无新发现"（[turn_books.py:534](file:///workspace/ai-coding/ai_coding/core/turn_books.py#L534)、[:541](file:///workspace/ai-coding/ai_coding/core/turn_books.py#L541)）；`stale_l1/l2/l3` 已接线（[interventions.py:124-129](file:///workspace/ai-coding/ai_coding/core/interventions.py#L124-L129)、[:331-334](file:///workspace/ai-coding/ai_coding/core/interventions.py#L331-L334)）。反例 `N,S,N,S` → `looks=2`，真实连续无新发现=1 | **删掉 `novel_in_streak`**，改用 `novel_findings == 0` 门控（§4.8） |
| 2 | 既有 16 条 `search_streak=N` 用例不用改 | **PASS（我复核后确认）** | [test_interventions.py](file:///workspace/ai-coding/tests/test_interventions.py) 16 处 `search_streak=`，**无一处设 `novel_findings`**（`_snap` 默认 0）→ 门控后行为逐字不变 | 保持 |
| 3 | `ContextAssembled.tail_blocks` 含该块 | **FAIL：字段不存在** | [request_assembly.py:116-125](file:///workspace/ai-coding/ai_coding/core/request_assembly.py#L116-L125) 只有 `history/instructions/request/previous_request`；`tail_blocks` 在 `reporter.calls`（[:362-368](file:///workspace/ai-coding/ai_coding/core/request_assembly.py#L362-L368)） | A5 断言改读 `reporter.calls[-1]["tail_blocks"]` |
| 4 | 尾块放 `memory` 之后 `task` 之前 | **存疑：漏了 `mcp_catalog`** | 实际顺序是 `environment, memory, mcp_catalog, task, …`（[:346-360](file:///workspace/ai-coding/ai_coding/core/request_assembly.py#L346-L360)） | 明确写"紧跟 `memory` 之后" |
| 5 | 放 tail 会破坏前缀缓存 | **PASS：担心是多余的** | tail 本来就在缓存前缀之外（[test_context_compression_cache_friendliness.py:13-15](file:///workspace/ai-coding/tests/test_context_compression_cache_friendliness.py#L13-L15)）；`environment` 块每轮带时间戳，本来就变 | 保持 |
| 6 | `TAIL_BLOCK_NAMES` 不用动 | **FAIL** | [test_context_compression_cache_friendliness.py:37-49](file:///workspace/ai-coding/tests/test_context_compression_cache_friendliness.py#L37-L49) 是白名单，:206 断言 `names <= set(TAIL_BLOCK_NAMES)` | 把 `read_ledger` 加进去（纯文档性，不改断言） |
| 7 | `METHODS_SECTION` 897 → 827 | **数字错** | 实测 `len(METHODS_SECTION) == 897`；删的那行 70 字符 + `"- "` + 换行 = **73**；**897 − 73 = 824** | 改成 897 → 824 |
| 8 | `note_read(path_str, …)` 记原始路径 | **FAIL：致命** | 实测 T2 的 read 调用**混用绝对与相对路径**：`/workspace/ai-coding/ai_coding/domain/plan_delivery.py` 与 `ai_coding/core/agent_loop.py` 并存；而答案引用的是相对路径 `ai_coding/domain/plan_delivery.py`，中间消息里还出现 basename `plan_delivery.py:81`。不归一化 → **7 条引用全部判未覆盖** | `ReadMark.path` 存 workspace 相对路径，`covers()` 接受相对/绝对/basename（§4.1） |
| 9 | `page_end = min(end, offset + max_lines)` | **FAIL：`end` 未 clamp** | [read_tool.py:123](file:///workspace/ai-coding/ai_coding/tools/read_tool.py#L123) `end = offset + limit`，`offset=100, limit=50, len=117` → `end=150`；`note_read(101,150,117)` 会记下 `end > total` | 三重 clamp（§4.4） |
| 10 | "传 limit ≤500 行为完全不变" | **FAIL** | 旧分支 [:129](file:///workspace/ai-coding/ai_coding/tools/read_tool.py#L129) 在 `offset=0, limit=50, len=100` 时会打印 `[showing lines 1-50 of 100]`；v1 的两个分支都不进 → 后缀消失 | 提示分支补全（§4.4） |
| 11 | 引用抽取"last-path-wins 无距离限制" | **FAIL：假阴性 + 假阳性** | (a) 路径写在行号之后 → 绑不上；(b) 一段里两个文件 → 都绑到最后一个，可能误退；(c) `path:N` 与 `path`+`第 N 行` 未定义是否重复计数；(d) 只认 `第 N 行`，漏 `line 81`/`L81`/`#L81`/裸 `81-85` | 单遍扫描 + 多写法 + 歧义保护（§4.11） |
| 12 | 校验放在 `_verify_acceptance` **之前** | **FAIL** | 会跳过验收；且 `continue` 后若预算已尽可能直接 FINISH，退回白费——**而 T2 的幻觉恰好发生在预算末尾**（iter 32 出 budget，iter 35 交答案） | 放在 `_verify_acceptance` **之后**（§4.11） |
| 13 | `budget_pressure` 加一句 | **PASS** | [turn_policy.py:598-603](file:///workspace/ai-coding/ai_coding/core/turn_policy.py#L598-L603) 是 `"\n".join([...])`，无长度上限，4 处既有断言只查子串 | 保持；明确写成**第三个列表元素** |
| 14 | §7 判据"覆盖率 100%" | **FAIL：不可测** | `t2-result.json` 只有 `seconds/messages/tool_calls/tool_seq/answer/session_id/checkpoint_*`，**没有 ledger、没有抽取结果**；`scripts/` 下也没有产出它的脚本 | §7 给出可计算定义 + 产出方式 |
| 15 | （决策 5 选"宽"之后新增） | **新发现：`seen_paths` 现算会被压缩吃掉** | 压缩后工具结果在摘要里只剩 2,000 字符（`_SUMMARY_TOOL_CHARS`），早期 grep/glob 看到的路径消失 → 合法引用被误判"从未见过" | 落盘 + 在 `_run_tools` 每批累加（§4.2/§4.6） |

**结论**：A 块（证据表）方向正确、可落地；**B 块（检索门）的判据必须重写**——
不是"新增一个计数器"，而是"把既有计数档位门控到『本 streak 无新证据』"。

---

## §3 原有设计（复用什么，不造什么）

| 已有 | 位置 | 本方案怎么用 |
|---|---|---|
| **`stale_streak`**（连续无新发现计数） | [turn_books.py:534,541](file:///workspace/ai-coding/ai_coding/core/turn_books.py#L534) | **B 块的核心**。不新造计数器 |
| **`novel_findings`**（本 streak 的新证据数） | [turn_books.py:539](file:///workspace/ai-coding/ai_coding/core/turn_books.py#L539) | B 块的门控信号：`== 0` 才算"白读" |
| **`stale_l1/l2/l3`** 档位 | [interventions.py:124-129](file:///workspace/ai-coding/ai_coding/core/interventions.py#L124-L129) | 不动。它们已经是"无新发现才升档" |
| `ToolContext` 的"循环回调"模式（`note_checkpoint`/`note_approval`/`note_plan_approved`） | [base.py:117-129](file:///workspace/ai_coding/tools/base.py#L117-L129) | 新增 `note_read`，**同一种模式** |
| `tool_context(...)` 单一工厂 | [request_builder.py:81](file:///workspace/ai-coding/core/request_builder.py#L81) | 加一个参数 |
| `volatile_block` / `build_tail` | [request_builder.py:43,62](file:///workspace/ai-coding/core/request_builder.py#L43) | `<read-ledger>` 直接走它 |
| `thinking_retries` 的一次性退回模式 | [agent_loop.py:1141-1172](file:///workspace/ai-coding/ai_coding/core/agent_loop.py#L1141-L1172) | 引用校验退回**照抄这个形状** |
| `compaction_failures` 的"每 turn 重置" | [agent_loop.py:1533](file:///workspace/ai-coding/ai_coding/core/agent_loop.py#L1533) | `claim_retries` 照抄（`thinking_retries` **不**每 turn 重置，不要学它） |
| `SessionData` 可选键序列化（`if x: data["x"]=…`） | [session.py:578-658](file:///workspace/ai-coding/ai_coding/domain/session.py#L578-L658) | `readLedger` 同一规则 |
| `FileStateTracker` | `security/file_state_tracker.py` | **不动**。它管"写之前有没有读过" |
| `TurnDigest` | `domain/turn_digest.py` | **不动**。回合结束后的摘要，解决不了 T2 |

**明确不造的东西**：不新建 `EvidenceStore`、不加 `novel_in_streak`、不做第二套工具上下文、
不在 `split_tail` 里加"超大消息切窗"（read 封顶 500 行后单条消息 ≤ 约 20k 字符 ≈ 5k token，
tail 预算 12k token 装得下，RC-4 自然消失）。

---

## §4 逐文件改动

### 4.1 新文件 `ai_coding/domain/read_ledger.py`（约 110 行）

```python
@dataclass(frozen=True)
class ReadMark:
    path: str        # workspace 相对路径（唯一键）
    start: int       # 1-based, inclusive
    end: int         # 1-based, inclusive
    total: int = 0   # 文件总行数，0 = 未知

def normalize(path: str, workspace: str) -> str
    """绝对/相对/带 ./ 的路径 → workspace 相对路径。

    实测：同一个会话里 read 会收到 `/workspace/ai-coding/ai_coding/domain/plan_delivery.py`
    和 `ai_coding/core/agent_loop.py` 两种形式，而答案引用的是相对路径。
    不归一化的话 covers() 对每条引用都返回 False。
    """

def same_file(a: str, b: str) -> bool
    """两个路径是否同一个文件：完全相等 / basename 相同 / 一方是另一方的后缀。

    实测三种写法并存：ledger 存 `ai_coding/domain/plan_delivery.py`，
    答案写 `plan_delivery.py:81`，grep 输出 `ai_coding/domain/plan_delivery.py:81: ...`。
    basename 匹配会把同名不同目录的文件并成一个——那是**安全方向**
    （漏判一次，不是误退一次）。
    """

#: 我们关心的文件后缀。答案里的引用几乎都是源码文件；放开成任意 token 会让
#: 一段普通散文里的 `foo.bar` 也被当成"看过的文件"。
_SOURCE_SUFFIXES = (
    ".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".json", ".md", ".txt",
    ".toml", ".yaml", ".yml", ".cfg", ".ini", ".sh", ".go", ".rs",
    ".java", ".rb", ".c", ".h", ".cpp", ".hpp",
)

def paths_in(text: str, workspace: str = "") -> tuple[str, ...]
    """文本里出现的源码文件路径（workspace 相对形式）。

    给 grep/glob/bash 的**输出**用：它们的输出就是路径清单。
    """

def merge_mark(marks, mark, *, max_marks=40) -> tuple[ReadMark, ...]
def render(marks, *, max_chars=900, max_files=8) -> str      # "" 表示没有可说的
def covers(marks, path, start, end) -> bool
def missing_ranges(marks, path) -> list[tuple[int, int]]      # 只对已知 total 的文件
```

- `merge_mark`：同 path 且区间相邻/重叠 → 合并；超过 `max_marks` 丢最旧。
- `render`：按"最近读的在前"，每文件一行：`path 共 N 行：1-60,140-245（缺 61-139）`；
  全文读完写 `完整`。超出 `max_chars` 截断并写 `…还有 K 个文件`。
- `covers`：区间包含判断（合并后区间是排好序的）。
- 纯函数、无 IO、无 session 依赖 → 单测直接覆盖。

**为什么单独一个模块**：值对象 + 纯函数，`session.py` 只持有它；渲染与归一化都要单独测。

### 4.2 `ai_coding/domain/session.py`

```python
read_ledger: tuple[ReadMark, ...] = ()      # 持久化范围，不持久化内容
seen_paths: tuple[str, ...] = ()             # 任何工具结果里出现过的路径（决策 5(b)）
claim_retries: int = 0                       # 内存态，每 turn 重置
```

序列化（[:578-658](file:///workspace/ai-coding/ai_coding/domain/session.py#L578-L658) 的白名单尾部）：
```python
if self.read_ledger:
    data["readLedger"] = [asdict(m) for m in self.read_ledger]
if self.seen_paths:
    data["seenPaths"] = list(self.seen_paths)
```
反序列化（[:720-750](file:///workspace/ai-coding/ai_coding/domain/session.py#L720-L750)）：
```python
raw_ledger = data.get("readLedger")
rows = raw_ledger if isinstance(raw_ledger, list) else []
raw_seen = data.get("seenPaths")
seen_rows = raw_seen if isinstance(raw_seen, list) else []
...
read_ledger=tuple(
    ReadMark(**row) for row in rows
    if isinstance(row, dict) and {"path", "start", "end"} <= set(row)
),
seen_paths=tuple(
    str(row) for row in seen_rows if isinstance(row, str) and row
),
```
**旧会话缺键 = 空表**，与 `compactionCheckpoint` 同一规则。
`claim_retries` 与 `thinking_retries` 一样**不进白名单**（不持久化）。

> **为什么 `seen_paths` 必须落盘**：如果从 `session.messages` 现算，压缩会把早期
> 工具结果摘要掉（工具结果在摘要里只剩 2,000 字符），早期 grep/glob 看到的路径
> 就消失了 → 合法引用被误判成"从未见过"。落盘 + 每批累加才跨压缩存活。

```python
def note_seen(self, paths: Iterable[str]) -> None:
    """累加本批工具结果里出现过的路径（去重，最多 200 条）。"""
    fresh = [p for p in dict.fromkeys(paths) if p and p not in self.seen_paths]
    if fresh:
        self.seen_paths = (self.seen_paths + tuple(fresh))[-200:]
```

### 4.3 `ai_coding/tools/base.py`

```python
#: 读工具报告"我读了哪几行"。与 note_checkpoint 同一种模式：循环给，工具用。
note_read: Callable[[str, int, int, int], None] | None = None
```

### 4.4 `ai_coding/tools/read_tool.py`

1. 构造参数加 `max_lines: int = _MAX_LINES`（模块常量 500）。
2. `execute` 里（**先修既有缺陷，再加分页**）：

```python
lines = text.splitlines(keepends=False)
end = min(offset + limit, len(lines)) if limit is not None else len(lines)
end = max(end, offset)                      # offset 越界时不要 end < offset
page_end = min(end, offset + self._max_lines)
view = lines[offset:page_end]
...
if page_end < len(lines):
    output += (
        f"\n[已返回第 {offset + 1}-{page_end} 行（共 {len(lines)} 行），"
        f"用 offset={page_end} 继续]"
    )
elif offset > 0 or (limit is not None and end < len(lines)):
    output += f"\n[showing lines {offset + 1}-{page_end} of {len(lines)}]"
```

> **为什么要补全提示分支**：`offset=0, limit=50, len=100` 时旧代码打印
> `[showing lines 1-50 of 100]`；v1 的分支设计会让这条后缀静默消失。
> 评审实测确认：`tests/` 里**没有**任何测试断言这条后缀，所以补全它是安全的。
> **但不补全是行为倒退**，所以补。

3. `note_read`：
```python
note_read = resolve_ctx(ctx).note_read
if note_read is not None:
    note_read(normalize(path_str, str(workspace)), offset + 1, page_end, len(lines))
```
- 用**归一化后的相对路径**（见 §4.1，这是评审发现的最严重缺陷）。
- 错误路径（sandbox 拒绝、非文件、超 10MB、读失败）都在算 `view` **之前** return
  → **不会误记 ledger**（评审已确认）。
- `workspace` 从 `getattr(self._sandbox, "workspace", None)` 取（[:91](file:///workspace/ai-coding/ai_coding/tools/read_tool.py#L91) 已有同样写法）。

4. `description` 改成："Returns at most 500 lines per call; the result says where to
   continue. Files that fit are returned whole."

**行为变化**：不传 `limit` 时不再整读任意大小文件。传了 `limit` 且 ≤500 时除
"`offset=0` 不再打印 showing 后缀"外完全不变。

### 4.5 `ai_coding/core/request_builder.py`

`tool_context(..., note_read=None)` → 透传进 `ToolContext`。

### 4.6 `ai_coding/core/agent_loop.py`

1. `_run_tools` 的 `tool_context(...)`（[:2113](file:///workspace/ai-coding/ai_coding/core/agent_loop.py#L2113)）加
   `note_read=session.note_read`；
2. **在 `_run_tools` 里累加 `seen_paths`**（[:2166](file:///workspace/ai-coding/ai_coding/core/agent_loop.py#L2166) `dispatch_batch` 之后）：

```python
outcomes = await self.dispatcher.dispatch_batch(...)
# 决策 5(b): 本批工具结果里出现过的路径 = 模型"见过"的文件。
# 在循环里做而不是在每个工具里做：read/grep/glob/bash 一处覆盖，
# 且落盘后跨压缩存活（现算会被摘要吃掉）。
session.note_seen(
    p
    for item in outcomes
    if item.result.success
    for p in paths_in(item.result.output or "", self.workspace)
)
```

> **为什么不在 grep/glob/bash 三个工具里各自接线**：那要改 4 个文件、加一个
> `ToolContext` 字段，而循环里这一处就能覆盖全部工具，且语义更准
> ——"模型在工具输出里看见过这个路径"，正是我们要的。
> `read` 的路径由 `note_read` 单独记（它还要记行号），但 `paths_in` 也会从
> read 的输出里捞到同一路径，两者不冲突。

3. `_begin_turn`（[:1533](file:///workspace/ai-coding/ai_coding/core/agent_loop.py#L1533)）加 `session.claim_retries = 0`；
4. 答案校验（§4.11）。

### 4.7 `ai_coding/core/request_assembly.py`

在 `build_tail([...])`（[:346-360](file:///workspace/ai-coding/ai_coding/core/request_assembly.py#L346-L360)）里，
**紧跟 `memory` 之后**插一项：

```python
volatile_block("read_ledger", render_read_ledger(session.read_ledger)),
```

内容形如：

```
<read-ledger>
本会话已读（行号；内容不在上下文里，需要时按缺口重读）：
- ai_coding/tools/present_plan_tool.py 共 441 行：1-60,140-245,300-434（缺 61-139,246-299）
- ai_coding/core/agent_loop.py 共 3081 行：1-500（缺 501-3081）
未读完的只读缺的段；已完整的不要重读。
</read-ledger>
```

**为什么放 tail 而不是 system prompt**：评审确认 tail 在缓存前缀之外，且 `environment`
块本来就每轮变 → 放这里不破坏前缀缓存。

同步把 `"read_ledger"` 加进
[test_context_compression_cache_friendliness.py:37-49](file:///workspace/ai-coding/tests/test_context_compression_cache_friendliness.py#L37-L49)
的 `TAIL_BLOCK_NAMES`（纯文档性白名单，**不改断言**）。

### 4.8 `ai_coding/core/interventions.py`（B 块，**重写**）

**核心认识**：`stale_streak` 已经是"连续无新发现"的计数，`stale_l1/l2/l3` 已经接线。
v1 想加的 `novel_in_streak` 是它的错误重写。真正的问题是**纯计数档位**
（`search_streak >= 4`）在"每次都读到新证据"时也开火。

改动只有一处判据 + 一处文案：

```python
# _triggered，:168-181 整段替换
if snap.mode == "plan" or snap.readonly:
    return False
# I1/R-7: the hard stop is a *count* rung on purpose -- a turn of only looks
# must close even when every look was new (see L3's docstring).
if limits.search_streak_l3 and snap.search_streak >= limits.search_streak_l3:
    return True
if not limits.search_streak_threshold:
    return False
# RC-1: the count rung only fires when the streak produced no new evidence.
# Measured on T2: streak=4, novel=4, stale_streak=0 still fired, and the model
# answered every nudge with `ruff` -- the rung told it to stop doing the one
# thing that was working.
if snap.search_streak >= limits.search_streak_threshold and not snap.novel_findings:
    return True
# I1/R-7: repeated evidence earns the rungs earlier than the streak can.
return bool(limits.stale_l1 and snap.stale_streak >= limits.stale_l1)
```

> **这个写法是试出来的，不是推出来的。** 我第一版写成
> `if snap.search_streak >= threshold: if snap.novel_findings: return False; return True`，
> **5 条既有测试立刻红了**。两个原因：
> 1. 提前 `return False` **跳过了下面的 stale 分支** →
>    `test_a_look_only_turn_gets_the_static_check_output`（9 次同参数读，
>    `novel_findings=1`、`stale_streak` 一路涨）再也发不出任何 `SearchStalled`；
> 2. **L3 也走 `_triggered`** → 门控把硬停档一起关掉了 →
>    `test_a_turn_of_only_looks_ends_itself`（16 次全新证据的只读回合）不再收尾。
>
> 所以 L3 必须放在门控**之前**单独判。改对后：
> **`pytest -q` 全绿（exit 0），128 条相关用例 + 全量套件零回归。**
> 这是我在这版方案里唯一"先改代码验证再写进方案"的地方。

- `_search_due`（[:310-349](file:///workspace/ai-coding/ai_coding/core/interventions.py#L310-L349)）的档位计算**不用改**：
  它只在 `_triggered` 返回 True 后才会被调用。
- `L3`（`search_streak_l3`）**继续用 `search_streak`**，且**不受 `novel_findings` 门控**——
  [test_interventions_loop.py:429](file:///workspace/ai-coding/tests/test_interventions_loop.py#L429)
  的 docstring 写得很清楚："Every look here returns *new* evidence … so this is the
  streak rung alone -- the evidence fingerprint cannot help."
- `_search_text`（[:525](file:///workspace/ai-coding/ai_coding/core/interventions.py#L525)）的措辞从
  "你已经连续 N 次只读检索" 改成 "你已经连续 N 次检索，但没有带来新证据"——
  现在它只在这种情况下开火，文案必须说真话。

**为什么既有 16 条用例不用改**（评审复核 + 我逐条推演 + 实跑验证）：它们全部不设
`novel_findings`（默认 0）→ 门控放行 → 行为逐字不变。`test_interventions_loop.py` 的
五条端到端用例（:253/:321/:429/:464/:498）两条走 stale、三条走 L3，都不受门控影响。

**净效果**：T2 的三次触发（iter 3/5/13，`novel == calls`）全部消失；
连续 4 次读同一段（`stale_streak` 到 3 就触发）才开火。

#### 决策 6：L2 文案

现在 L2（[:537-544](file:///workspace/ai-coding/ai_coding/core/interventions.py#L537-L544)）：

> 「**不要再猜关键词了** —— 让静态检查替你回答：`ruff check .` / `mypy` / `grep -rn`」

T2 的 11 次 `bash` **全是** `ruff`/`grep`/`find`，而模型自己的 checkpoint 已经写明
"ruff check . 未能暴露运行时逻辑缺陷"。**它知道这条路不通，但门一直把它推回去。**

- **选项 (a)** 不动文案：触发条件修好后，门不再推它 → 11 次 bash 应该消失。0 条测试改动。
- **选项 (b)** 把 L2 改成"停止重读，直接给结论"：更准确，但
  [test_interventions.py:218-231](file:///workspace/ai-coding/tests/test_interventions.py#L218-L231)
  断言 `"ruff check" in d.text` 且 `"mypy" in d.text`，
  且其 docstring 写着 "the second rung must name the static check -- that is its whole job"。

**我的推荐是 (b)**，理由：修完触发条件后 L2 的含义变成"你已经 3 次重复读同一处"，
这时建议跑 `ruff` 仍然是文不对题（重复读不是语法问题）。**但这条测试的意图是真实的**
（B1 设计时就是要它指向静态检查），所以改它属于"测试用例表达的是旧设计"，
符合"除非是测试用例出错，不然不准修改测试用例"的例外。
**如果选 (a)，我会把 §7 的成功判据里"静态检查类调用 ≤ 3"保留为观察项而非硬判据。**

### 4.9 `ai_coding/ai/prompt.py`

**实测数字**（我逐字符数过，评审的 896/825 也不对）：

```
len(METHODS_SECTION) = 897
删除那一行（含 "- " 与换行）后 = 826     # 实测，≤ 900 ✓
# [test_prompt.py:234](file:///workspace/ai-coding/tests/test_prompt.py#L234) 是硬断言
```

**两处改动，互不冲突**：

1. **删** `TOOL_GUIDANCE["read"]` 第二条（[:61](file:///workspace/ai-coding/ai_coding/ai/prompt.py#L61)）
   ——分页由工具自己做（§4.4），这条只会让模型把 441 行的文件切成 11 段。
   `METHODS_SECTION` 897 → **824**。

2. **引用规则加进 `BASE_INSTRUCTIONS`**（[:16-19](file:///workspace/ai-coding/ai_coding/ai/prompt.py#L16-L19)，
   实测 134 字符、**不被 `METHODS_SECTION` 的 900 上限约束**）：

```
Ground every claim about the code in something you actually read this session: cite
`path:line`. If you have not read it, say "unconfirmed" instead of describing it.
```

> **为什么不加进 `GENERAL_METHODS`**：它是 `METHODS_SECTION` 的一部分。
> 897 − 73 = 824，再加这条（约 150 字符）= 974 > 900，会红。
> 要加进 `GENERAL_METHODS` 就必须再删一条既有内容（比如把 read 的第一条
> "Use read — not shell commands like cat…" 移进工具的 `description`），
> 代价是 `test_prompt.py:184-195` 会红。
> **`BASE_INSTRUCTIONS` 没有这个代价**，且它本来就是"放之四海"的通用准则
> ——引用归因正是这种准则，不是"跨工具习惯"。所以选它。

**净效果**：`METHODS_SECTION` 897 → 824（减 73），引用规则进 base（+150，不计入预算）。
**既有 4 处 `test_prompt.py` 断言全部不受影响**（它们断言的是 read 的第一条和
`subAgent` 那条，我们只删第二条）。

### 4.10 `ai_coding/core/turn_policy.py::budget_pressure`

[:598-603](file:///workspace/ai-coding/ai_coding/core/turn_policy.py#L598-L603) 是两元素列表。
**新句作为第三个元素**（不要拼到上一句尾部）：

```python
return "\n".join(
    [
        f"{BUDGET_PRESSURE_LEAD}（{'，'.join(parts)}）。",
        "请用这一轮收尾：把已完成的和还差的讲清楚，不要再开新的工作。",
        "证据不足的部分直接写「未确认」并说明缺什么，不要补全。",
    ]
)
```

评审确认：无长度上限、无精确文本断言，4 处既有断言只查子串 → 全绿。

### 4.11 答案校验（`core/agent_loop.py` + `domain/read_ledger.py`）

#### 抽取器（放 `domain/read_ledger.py`，与归一化共用）

```python
def extract_claims(text: str) -> list[tuple[str, int, int]]
```

单遍扫描，同时认这几种写法（实测都出现在真实答案里）：

| 写法 | 例子 |
|---|---|
| `path:line` / `path:line-line` | `plan_delivery.py:81` |
| `path` 在前 + `第 N 行` / `第 N-M 行` | `` `plan_delivery.py`，第 81 行 `` |
| `path` 在前 + 裸 `N-M 行` | `第 73-85 行` |
| `path` 在前 + `line N` / `L N` / `#L N` | `line 81` / `L81` |
| 中文"第 N 行"无空格 | `第81行` |

绑定规则：**last-path-wins**（评审确认 v1 的"无距离限制"在真实答案上能绑对，
因为路径恰好写在最前面）。

#### 歧义保护（评审指出的假阳性来源）

- 段里出现两个文件 → 都绑到最后一个 → 可能误退。
- 路径写在行号之后 → 绑不上 → 假阴性。

**处理方式**：抽取时同时返回 `paths: set[str]`（答案里提到的不重复文件）。
校验时：
- 若 `len(paths) <= 1` → 用唯一路径绑定，正常判定；
- 若 `len(paths) >= 2` → **跳过**该校验（宁可漏判，不可误退）。

> 真实 T2 答案只提到一个文件 → 正常判定 → 抓住 `18-46`。

#### 三档规则（决策 5(b)）

| 档 | 条件 | 判定 | 为什么 |
|---|---|---|---|
| 1 | 路径**从未**出现在任何工具结果里（不在 `seen_paths`） | **退回** | 引用了一个本会话根本没碰过的文件 = 凭空捏造，最强信号 |
| 2 | 路径出现过，但**不在 `read_ledger`**（只被 grep/glob/bash 看到） | **放行** | 行号无法核验——grep 命中不代表读过那一行 |
| 3 | 路径在 `read_ledger`，但引用区间**完全落在已读区间之外** | **退回** | T2 这处幻觉的形状 |

#### 校验函数

```python
def _reject_unsupported_claims(session, text) -> bool:
    if session.claim_retries >= 1:
        return False
    claims = extract_claims(text)
    if not claims or len({c[0] for c in claims}) >= 2:
        return False                      # 歧义保护：答案提到 ≥2 个文件就跳过
    path = claims[0][0]
    marks = [m for m in session.read_ledger if same_file(m.path, path)]
    if marks:
        bad = [(s, e) for _, s, e in claims if not covers(marks, path, s, e)]
    elif any(same_file(p, path) for p in session.seen_paths):
        bad = []                          # 档 2：只被 grep/glob/bash 见过 → 放行
    else:
        bad = [(s, e) for _, s, e in claims]   # 档 1：从未见过 → 全退
    if not bad:
        return False
    session.claim_retries += 1
    return True
```

> **档 1 的误退风险**：`seen_paths` 是**累加并落盘**的（§4.2/§4.6），所以压缩不会
> 让它变小。唯一的漏网是"路径出现在被截断的工具输出里"——grep 截断的是**命中文本**
> （`_MAX_LINE_CHARS = 300`），不是路径；glob 截断的是**结果条数**（250），
> 但截断提示 `... [showing N of M results]` 里没有路径。所以档 1 的误退面很小。

#### 放置点（决策 7）

`agent_loop.py` [:1229-1251](file:///workspace/ai-coding/ai_coding/core/agent_loop.py#L1229-L1251) 之间，
**`_verify_acceptance` 之后、`decide_after_step` 之前**：

```python
if not step.tool_calls:
    await self._verify_acceptance(session, books)          # 不动
    if self._reject_unsupported_claims(session, step.text):
        books.note_strategy_nudge()
        self.reporter.report_degraded(
            "model_step", "the answer cited unread code", "asked it once to correct"
        )
        session.add_message(
            user_message(
                "你的答案引用了本会话没有读过的位置：\n"
                f"- {path}:{start}-{end}\n"
                "请核对：要么先读这些位置再确认，要么把结论改成「未确认」并说明缺什么。\n"
                "不要凭记忆描述没有读过的代码。",
                now_utc(), synthetic=True,
            )
        )
        self.closer.checkpoint(session, index)
        continue
    snapshot = books.snapshot(...)
```

**为什么放在 `_verify_acceptance` 之后**（评审的实测理由）：
v1 放在它之前会**跳过验收**；且 `continue` 回到循环顶部后若预算已尽可能直接 FINISH，
退回就白费了——而 **T2 的幻觉恰好发生在预算末尾**（rootcause §2：iter 32 出 budget，
iter 35 交答案）。放在验收之后，退回后模型至少能看到验收结果再改一次。

**为什么不会死循环**：`claim_retries >= 1` 直接放行；且它每 turn 重置（§4.6）。
`thinking_retries` **不**每 turn 重置（[:309](file:///workspace/ai-coding/ai_coding/domain/session.py#L309)
全文只有 [:1154,1156](file:///workspace/ai-coding/ai_coding/core/agent_loop.py#L1154) 两处读写），
它的语义是"进程内只问一次"；`claim_retries` 的语义是"每回合的答案有一次改正机会"，
所以学 `compaction_failures`（[:1533](file:///workspace/ai-coding/ai_coding/core/agent_loop.py#L1533)）。

#### 在本次 T2 的真实答案上跑过

抽出 7 条引用（不是 v1 写的 6 条——"修复方式"里的"第 81 行"被 v1 漏了）：

```
81, 73-85, 81, 88-110, 18-46, 81, 81
```

本次会话读过 `plan_delivery.py` 第 61-110 行（trace 里的 `offset=60, limit=50`）
→ 该路径在 `read_ledger` **且**在 `seen_paths` → **档 3** → `18-46` 未覆盖 → **会触发退回**。
即这条校验确实能抓到本次那处幻觉，而不是只在设计上成立。

---

## §5 数据流（改后）

```
read(path) ──► ReadTool.execute
                 │  ① end/page_end 三重 clamp 到 len(lines)
                 │  ② 截断到 500 行 + "offset=N 继续"
                 │  ③ ctx.note_read(normalize(path), start, end, total)
                 ▼
           session.read_ledger  (合并区间，最多 40 段)
                 ▲
工具结果 ─────────┤  ④ 每批工具结果里的路径（read/grep/glob/bash 一处覆盖）
                 ▼
           session.seen_paths  (累加，落盘，跨压缩存活)

     ┌───────────┴────────────┐
     ▼                        ▼
RequestAssembler          AgentLoop（模型交答案时）
  <read-ledger> 尾块         抽取 path:line（单遍、多写法）
  （每轮重建，压缩碰不到）      └─ 三档规则：从未见过 → 退；
     │                             grep-only → 放行；读过但区间外 → 退
     ▼
  模型：知道读过什么、缺什么 → 只读缺口（不再把 441 行读 11 次）

检索工具成功 ──► TurnBooks._note_finding
                 │  novel → stale_streak=0, novel_findings+=1
                 │  重复  → stale_streak+=1
                 ▼
              TurnSnapshot ──► interventions._triggered
                                ├─ L3 硬停：search_streak >= search_streak_l3（不动，默认关）
                                ├─ 纯计数档位：novel_findings==0 才开火（RC-1 修复）
                                └─ stale 档位：stale_streak >= stale_l1/l2/l3（不动）
```

---

## §6 TDD / RIPR 用例（红相先行）

**R（红）**：每条先写测试，在**未改代码**时必须是红的。

| # | 用例 | 断言 | 红相 |
|---|---|---|---|
| A1 | `test_a_read_records_the_range_it_returned` | `session.read_ledger == (ReadMark("a.py",1,60,441),)` | `note_read` 不存在 |
| A2 | `test_overlapping_reads_merge_into_one_range` | 读 1-60 再读 50-100 → 单段 1-100 | 无合并 |
| A3 | `test_the_ledger_names_the_missing_ranges` | 441 行读了 1-60/300-434 → 渲染含 `缺 61-299` | 无渲染 |
| A4 | `test_a_fully_read_file_says_complete` | 1-441 → `完整` | — |
| A5 | `test_the_ledger_block_rides_the_request` | `reporter.calls[-1]["tail_blocks"]` 含 `("read_ledger", n>0)` | 块不存在 |
| A6 | `test_a_read_after_compaction_still_shows_in_the_ledger` | 压缩后下一轮请求仍含该 path 的行号 | 块若写进 history 会被压掉（防回归） |
| A7 | `test_absolute_and_relative_reads_share_one_file` | 绝对路径读 + 相对路径读 → 同一个 key | **评审发现，不修则 7 条引用全判未覆盖** |
| A8 | `test_a_basename_citation_matches_the_ledger` | 答案写 `plan_delivery.py:81`，ledger 存 `ai_coding/domain/plan_delivery.py` → `covers` True | 同上 |
| B1 | `test_a_novel_read_streak_does_not_nag` | 4 次读、每次新内容（`novel_findings=4`）→ 无 nudge | 现在会 nudge |
| B2 | `test_four_looks_without_any_new_evidence_still_nag` | `novel_findings=0, search_streak=4` → L1 nudge | — |
| B3 | `test_the_count_rung_is_off_while_every_look_is_new` | `novel_findings=4, stale_streak=0, streak=4` → None | **RC-1 的直接回归** |
| B4 | `test_the_stale_rungs_are_untouched` | `stale_streak=3` 且 `novel_findings>0` → L1（既有档位仍生效） | — |
| C1 | `test_a_long_file_is_paged_with_a_continue_hint` | 3081 行文件不传 limit → 500 行 + `offset=500` | 现在返回全文 |
| C2 | `test_a_file_under_the_cap_is_returned_whole` | 441 行 → 441 行，无分页标记 | — |
| C3 | `test_an_explicit_limit_above_the_cap_is_clamped` | limit=900 → 500 + 提示 | — |
| C4 | `test_offset_plus_limit_past_the_end_does_not_record_a_bad_range` | `offset=100, limit=50, len=117` → 17 行，ledger 记 `101-117` | **评审发现：现在会记 101-150** |
| C5 | `test_an_explicit_limit_below_the_end_keeps_its_suffix` | `offset=0, limit=50, len=100` → 含 `showing lines 1-50 of 100` | v1 会静默丢掉 |
| D1 | `test_an_answer_citing_an_unread_range_is_sent_back_once` | 只读 61-110、答 `plan_delivery.py:18-46` → 插消息、`claim_retries==1` | 现在直接收尾 |
| D2 | `test_the_same_answer_is_accepted_the_second_time` | 第二次不再插消息 | 防死循环 |
| D3 | `test_a_citation_inside_a_read_range_is_accepted` | 读过 18-46 后引用 → 直接收尾 | — |
| D4 | `test_a_bare_line_number_without_a_path_is_ignored` | 只有 `第 81 行` 无路径 → 不触发 | 防误判 |
| D5 | `test_two_files_in_one_answer_skips_the_check` | 答案提到 2 个文件 → 不退回 | **评审发现：防误绑** |
| D6 | `test_a_citation_to_a_never_touched_file_is_sent_back` | 路径既不在 ledger 也不在 `seen_paths` → 退回 | **决策 5(b) 档 1** |
| D7 | `test_a_citation_to_a_grep_only_file_is_accepted` | 路径在 `seen_paths`、不在 ledger → 放行 | **决策 5(b) 档 2（防误伤）** |
| D8 | `test_seen_paths_survive_a_compaction` | 压缩后 `seen_paths` 不减 | **评审发现：现算会被摘要吃掉** |
| D9 | `test_every_supported_citation_style_is_extracted` | `path:N` / `第 N 行` / `第 N-M 行` / `line N` / `L N` / `#L N` 各抽一条 | **评审发现：v1 只认两种写法** |

**I（绿）**：实现后上表全绿。
**P（证明）**：每条用例配一条变异（例如 `note_read` 改成空实现 → A1 红；
`covers` 恒 True → D1 红；`_triggered` 去掉 `novel_findings` 门控 → B3 红）。
**P 阶段要求变异全杀**（沿用 i2 阶段的做法，用变异脚本 + 锚点）。
**R（重构）**：全量套件 + ruff + mypy。

---

## §7 成功后的样子（可验证）

### 可计算的指标

| 判据 | 基线（i2c） | 目标 | 从哪算 |
|---|---|---|---|
| `tool_calls` | 43 | 先修根因，再看数字 | `t2-result.json:tool_calls` |
| 同一文件重复读取 | 11（`present_plan_tool.py`，重叠窗口） | 不再有"同一窗口反复读" | 从 `sessions/*.json` 的 read 参数统计 |
| 静态检查类调用 | 11 `bash` | 门不再推它 | `t2-result.json:tool_seq` 里数 `bash` |
| **单次 read 的最大字符数** | **174,113**（43k token，占窗口 89%） | ≤ 约 20,000 | 会话里 `role=tool, toolName=read` 的 `content` 长度 |
| **引用覆盖率** | 未测（已知 1 处幻觉） | **100%** | `scripts/eval_claims.py`，见下 |

> 基线的 `tool_calls` 目标（≤30）是方案里我自己提的，**不是实测判据**。
> 根因报告 §6 写得很清楚："不为 T2 这种开放式任务硬凑判据——先修根因，再看数字"。
> 所以下面给的是**结构性判据**（同一窗口反复读、单次 read 的上限、引用覆盖率），
> 而不是一个会被模型方差左右的总数。

### 真机复跑结果（两次，同一题同一模型）

| 判据 | 基线 i2c | r20 第 1 次 | r20 第 2 次 |
|---|---|---|---|
| `tool_calls` | 43 | 57 | **26** |
| `read` 次数 | 27 | 43 | **4** |
| `bash` 次数 | 11 | 0 | 0 |
| 同一文件最多重复读 | **11**（`present_plan_tool.py`，重叠窗口） | 12（`permission_gate.py`，**不同 offset 覆盖全文**） | **2** |
| `[search-gate]` nudge | **3 次，全部 `novel == calls`** | **0 次** | 1 次（`stale_streak=3`，文案准确） |
| 单次 read 最大字符 | **174,113** | **30,492** | **2,159** |
| 引用覆盖率 | 未测（1 处幻觉） | **100%（15/15）** | **100%（1/1）** |
| 答案 | 1 处幻觉（`plan_delivery.py:18-46`） | 真实缺陷（`llm_helpers.py:89`） | 真实缺陷（`mcp.py:624`） |

**两次的 `tool_calls` 差一倍（57 vs 26），是模型方差**：两次找到的是不同的缺陷，
探索路径自然不同。结构性判据才是可比的：

- **RC-1**：基线 3 次 nudge 全部落在"每次都是新证据"上；两次复跑里，第 1 次 0 次，
  第 2 次 1 次且落在真实的 3 连重复上（文案如实说"最近 3 次检索都在重复已经看过的内容"）。
- **RC-2**：基线把一个 441 行文件按重叠窗口读了 11 次；两次复跑都没有"同一窗口反复读"
  （第 1 次的 12 次读分布在 1093 行的不同 offset 上，合并后的 ledger 显示文件被完整覆盖）。
- **RC-3 / RC-4**：174,113 字符的单次读**消失了**，最大值降到 30,492 / 2,159。
  基线那次读占掉 48k 窗口的 89%，然后被摘要成 2,000 字符——内容没了。
- **RC-5/6/7**：两次答案的引用覆盖率都是 **100%**，且两处引用的都是真实缺陷。

**`<read-ledger>` 确实到达了模型**（trace 实测，第 2 次复跑）：

```
iter 1-4  无块（还没读过东西）
iter 5-9  [environment, read_ledger(140), task]
iter 10-12 [environment, read_ledger(219), task]
iter 13   [environment, read_ledger(278), task]
```

复跑脚本：`scripts/eval_t2_r20.py`（同题、同模型、同 40 步预算、同 I2 压缩档位）。

### 引用覆盖率怎么算（评审指出 v1 没给方法）

1. **ledger 必须落盘**：`read_ledger` 已经在 `SessionData.to_dict` 里（§4.2），
   而 T2 的会话文件**已经是** `SessionData` 的 JSON → 无需额外落盘，
   只要读 `sessions/*.json` 的 `readLedger` 键即可（T2 那份是旧文件，没有这个键，
   所以**本次复跑必须用新代码跑**，这也正是我们要做的）。
2. 抽取器就是 §4.11 的 `extract_claims`。复算脚本：
   ```
   python -m ai_coding.scripts.eval_claims \
       --session <sessions/xxx.json> --answer <t2-result.json>
   # 输出：claims=7 covered=6 uncovered=1 coverage=0.857
   ```
3. **分子分母都要说清**：分母只包含"答案里出现 ≤1 个不同文件"的答案
   （歧义保护），所以"100%"的含义是"**在可判定的答案上，被引用的位置都已被会话覆盖**"。
   这里的"覆盖"按三档规则算：档 1（从未见过）和档 3（读过但区间外）算未覆盖，
   档 2（只被 grep/glob/bash 见过）算覆盖。
   另加一个下限判据：**抽出的引用数 ≥ 3**，否则模型可以靠"不写引用"拿到 100%。
4. **落盘是前提**：`read_ledger` 和 `seen_paths` 都在 `SessionData.to_dict` 里（§4.2），
   复算脚本直接读 `sessions/*.json` 的 `readLedger` / `seenPaths` 两个键。

### 复跑方式

T2 的题面在 `sessions/5ce279bf356f436fa744c2a542f92c60.json` 的 `turnDigests[0].request`，
同一题、同一模型（`qwen3.7-flash`）、同一 48k 窗口、`max_tool_iterations=40`。
产出 `t2-result.json` 的 harness 不在仓库里（评审确认），所以复跑沿用 `eval-workspace`
里的同一套脚本。

### 中途自检（用来判断实现细节对不对）

- 跑到第 10 个迭代时，`<read-ledger>` 里应该已经列出 3-5 个文件及缺口——
  如果它是空的，说明 `note_read` 没接上；
- 如果 ledger 里的 path 是绝对路径，说明 `normalize` 没生效（A7 应该已经红了）；
- 如果模型仍在读已读过的行，说明渲染里的"缺 X-Y"没起作用，先看渲染文本；
- 如果 `[search-gate]` 还在 `novel == calls` 时开火，B3 应该已经红了。

---

## §8 不做的事

1. 不在 checkpoint 里抄代码片段（Claude Code 路线）：摘要会涨 3-5 倍，而我们的窗口只有 48k；
2. 不改 `split_tail`（read 封顶后单条消息不再超预算）；
3. 不动 `FileStateTracker`；
4. 不引入第二个模型做校验（MARCH 路线）；
5. **不加 `novel_in_streak`**：`stale_streak` 已经在了；
6. **不改 `search_streak_l3`**：它是独立的硬停档位，默认关闭，有明确的设计依据
   （eval-08 实测它会截断一个完整的答案）；
7. 不动 `METHODS_SECTION` 的 900 字符预算（我们是**减** 73 字符）。

---

## §9 拍板结果

| # | 决策 | 结论 |
|---|---|---|
| 5 | 引用校验触发面 | **(b) 宽**：三档规则（§4.11）——从未见过 → 退；只被 grep/glob/bash 见过 → 放行；读过但区间外 → 退 |
| 6 | L2 文案 | **(b) 改**：触发条件 + L2 文案都改。要动 1 条既有测试 `test_interventions.py:218-231`（理由见 §4.8） |
| 7 | 退回时机 | **(b)**：`_verify_acceptance` 之后、`decide_after_step` 之前 |

**决策 5 选 (b) 的代价与对策**：

- 代价：要新增 `session.seen_paths`（落盘 + 每批累加）+ `read_ledger.paths_in()`。
- 对策：**在 `_run_tools` 一处累加**，不改 grep/glob/bash 任何工具；
  落盘所以跨压缩存活（现算会被摘要吃掉，那是评审发现的最隐蔽的一处）。
- 误伤面：只剩"路径出现在被截断的工具输出里"——grep 截断的是命中文本（300 字符），
  不是路径；glob 截断的是条数（250），截断提示里没有路径。

**决策 6 的代价**：1 条既有测试的断言要改
（`test_interventions.py:218-231` 断言 `"ruff check" in d.text`）。
我在 §4.8 写清了为什么这条测试表达的是旧设计，属于
"除非是测试用例出错，不然不准修改测试用例"的例外。

---

## §10 实现阶段修正（评审实现后发现，已改）

实现完成后又跑了两个子智能体评审**实现本身**，找出 8 处真问题。全部已修，
并各配一条变异测试（`scripts/mutate_r20.py`，20/20 全杀）。

| # | 问题 | 修法 |
|---|---|---|
| 1 | `read_tool` 在 `limit=0` / `offset` 越界时打印 `showing lines 1-0 of 100`（起点 > 终点 > 总数），模型只能靠猜 | 加空页守卫，改说"没有返回任何行：请求第 N 行起共 M 行，文件共 K 行" |
| 2 | 模型读 `.env` 后，凭证**文件名**进 ledger、落盘、每轮回灌 | `note_read` 前判 `is_credential_path`，是则不记 |
| 3 | `seen_paths` 从 **read 的输出**里捞路径 —— read 输出是文件正文，正文里提到的每个模块都被当成"见过"，档 1 被静默关掉 | 累加时排除 `read`（read 自己的路径由 ledger 负责） |
| 4 | `_search_text` 说"没有带来任何新证据"，但 stale 档位在 `novel_findings>0` 时也会开火 —— 文案在说谎 | 按 `stale_streak` 分流：重复 → "最近 K 次检索都在重复已经看过的内容"；计数 → "没有带来任何新证据" |
| 5 | L2 直接点名 `<read-ledger>`，但该块只在**有读记录**时存在；L2 可以在只有 grep 时开火 | 改成"请求里有 `<read-ledger>` 块时，它列出的缺口才是没读过的部分" |
| 6 | 退回若发生在**最后一个允许的迭代**上，`continue` 后预算已尽 → 回合以 handoff 收尾，更正消息永远没人读 | 加 `_has_a_round_left(index)` 守卫 |
| 7 | `_reject_unsupported_claims` 里调了 `books.note_strategy_nudge()`，把引用校验退回记进了策略 nudge 计数（语义污染） | 删掉；`report_degraded` + `session.claim_retries` 已经够了 |
| 8 | `same_file` 的 basename 折叠是**双向**的：`a/foo.py` 与 `b/foo.py` 被并成一个，会让档 1 永不触发 | 只允许"裸 basename → 带目录路径"单向折叠 |

**评审提出但决定不改的**：

- `covers` 只判交集（读 1-60、引 50-200 会放行）。这是 §4.11 里已拍板的规则
  （"部分重叠不触发"），且退回有上限、答案最终仍会交付。改成"完全包含"更强，
  但那是**改一个已拍板的决策**，留给用户决定。
- 档 2 完全不看行号（grep 过 `a.py` 后引 `a.py:999` 会放行）。档 2 的定义就是
  "行号无法核验"，且该路径此时**不在 ledger 里**，所以拿不到 `total`。已在
  §7 写明覆盖率不是行号级准确率。
- `Node.js` 这类"产品名恰好以后缀结尾"的 token 仍会被当成路径。
  `core/file_mentions.py` 靠**存在性检查**避开它，而引用校验不能这么做
  （答案可能引用工作区外的文件）。影响是"多一次退回"，有上限。
- `paths_in` 与 `core/file_mentions.py` 是两套"什么像路径"的答案，且已分叉。
  `domain/` 全仓**不 import `core/`**，为一段正则新建这条边是更差的交易；
  已把 `file_mentions` 的左右边界与 URL 守卫搬过来（那是正确性问题，不是维护问题）。

**实测数字修正**：`METHODS_SECTION` 实际是 **897 → 826**（不是 §4.9 说的 824）。

**新增脚本**：
- `scripts/mutate_r20.py` —— RIPR 的 P 阶段，20 条变异全杀
- `scripts/eval_claims.py` —— §7 的覆盖率复算，`python scripts/eval_claims.py --session <sessions/x.json> --answer <t2-result.json>`
