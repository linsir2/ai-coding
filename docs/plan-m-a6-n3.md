# M-A6 + N-3 实施方案 · 文档/实测不一致收口 + MCP 夹具修复

> 状态：**✅ 已完成（2026-10-25）** —— 实施记录见 §十三
> 关联：[roadmap-milestones.md](roadmap-milestones.md) §1 M-A6 / §十三 N-3
> 依赖：M-A1（`mcp>=1.24,<2`）、M-A5（lint/type 清零）
> 调研日期：2026-10-25（全部结论均有实跑证据）

---

## 一、先回答：这两件事分别是什么

| | **M-A6** | **N-3** |
|---|---|---|
| 现象 | `scripts/memory_cases.py` **9/11**，`case7-delta-only` 与 `case11-cache-prefix` **FAIL** | CI 的 `Real MCP stdio round trip` 与 `Real MCP call, whole path` **两个 step 全红** |
| 性质 | **文档/用例的期望与实测不一致**（产品代码是对的） | **产品夹具缺陷**（`mcp_echo_server.py` 用了 mcp 2.x 的 API） |
| 影响 | CI 的 `Memory acceptance cases` step 红 | CI 两个 MCP step 红；`r7_cases.py` 的 MCP 档也跑不通 |
| 修法 | 改用例断言 + 改文档 | 改 2 个脚本的 import（2 行） |

**两件事的共性**：都是"**期望没跟上实现**"，不是产品逻辑坏了。

---

## 二、M-A6 调研结论

### 2.1 case7 · `batch_sizes=[3, 4]` —— **用例断言的是调度产物，不是产品契约**

#### 实跑证据（4 回合 → 3 个批次，合计 8 条 = 全部消息）

```
after turn 0: harvested_messages=0 total_msgs=2
after turn 1: harvested_messages=0 total_msgs=4
after turn 2: harvested_messages=3 total_msgs=6
after turn 3: harvested_messages=3 total_msgs=8
seen batches: [3, 4, 1]
  batch0: ['user','assistant','user']                 -> 第0轮 / 好的 / 第1轮
  batch1: ['assistant','user','assistant','user']     -> 好的 / 第2轮 / 好的 / 第3轮
  batch2: ['assistant']                               -> 好的
```

#### 根因（产品代码，**是对的**）

```
agent_loop.py:1491  _schedule_harvest()  ← 收割是"调度"，不是 await
agent_loop.py:1505  "schedule, don't await"
memory_turn.py:174  delta = session.messages[start:]   ← "游标之后的全部"
```

回合跑得比提取器快时，**多个回合的消息会合并进同一个 delta**（coalescing）。
`agent_loop.py:1509-1514` 明确写了这是设计：

> The chain also coalesces deltas when turns outrun extraction -- **by design**:
> the cursor only advances over what the extractor was shown, so nothing is ever
> skipped, and a retry after a failure naturally includes everything since the
> last success.

#### 用例错在哪

```python
sizes = [len(batch) for batch in extractor.seen]
repeated = sum(1 for size in sizes if size > 3)
ok = len(sizes) == 4 and repeated == 0     # ← 断言"恰好 4 批"
```

`len(sizes) == 4` 要求**每个回合都单独成批**，这只在"收割同步完成"时成立。
而收割是异步的 → 批次数量**随事件循环时序漂移**：

| 来源 | 记的批次 |
|---|---|
| `docs/eval-01-offline-baseline.md:85` | `[3, 4]`（2 批） |
| 本次实跑（5 次稳定） | `[3, 4, 1]`（3 批） |

**同一个测试，两次记录不同 → 它测的是调度，不是契约。**

#### 真正的契约（5 次实跑全部成立）

```
run0..run4: sizes=[3,4,1] total=8 identity_chain=True whole_history=False
```

- `identity_chain=True`：提取器看到的片段**按顺序拼起来恰好是会话的全部消息**（同一批对象，无丢失、无重复）
- `whole_history=False`：**没有任何一批等于整段历史**（这正是用例最初要防的回归）

### 2.2 case11 · `87% < 90%` —— **阈值来自不可比的测量口径**

#### 实跑证据（5 次完全一致，**不是抖动**）

```
FAIL  prefix_hit append-only=87% per-turn-trim=15%   （× 5 次，逐次相同）
```

#### 根因：易失尾部打断了字节前缀（产品设计如此）

分歧点实测（`requests[-2]` vs `requests[-1]`）：

```
prefix=88%  len(a)=3045 len(b)=3200 diverge_at=2828
   A[2828:] = '<environment>\n工作区: ...\n时间: ...\n</environment>'
   B[2828:] = '好的，这个模块负责装配与转发，...'
```

请求的最后一截是**易失尾部**（每轮重渲染）：

| 块 | 实测长度（58 字符路径） | 内容 |
|---|---|---|
| `<environment>` | ≈121 | 工作区路径 + **时间戳** |
| `<task-state>` | ≈50 | **目标 = 本轮用户消息** |
| `[budget]` | 57 | 步数 1/1 的收尾提示 |
| 静态分隔句 | ≈28 | "以下是你自己的任务清单状态（系统注入…）" |
| **合计** | **≈256** | **随工作区路径长度线性增长** |

> ⚠️ 方案的初稿把尾部记为"215 字符"，那是**特定路径下**的值。评审指出后实测：
> 路径每长 1 字符，尾部就 +1 字符 → **尾部不是常数**。

请求结构：`[历史 H][尾部 T]`，下一回合变成 `[历史 H][a₁₆ u₁₇][尾部 T']`。
→ 字节前缀在**尾部处**断裂。这是**最优布局**：尾部必须跟在历史之后（若塞进系统提示，前缀会更早破）。

#### 92% 与 87% 的口径差异（关键）

```
len(公共前缀) / len(前一个请求) = 2828 / 3045 = 92.9%   ← 文档的 92%
len(公共前缀) / len(当前请求)   = 2828 / 3200 = 88.4%   ← 项目 prefix_shared 与用例的口径
```

- 项目自己的度量 [trace.py:118-134](file:///workspace/ai-coding/ai_coding/core/trace.py#L118-L134) `prefix_shared()` 用 **`index / len(current)`**
- [r3-analysis.md:881](file:///workspace/ai-coding/docs/r3-analysis.md#L881) 记的 **92%** 注明"**同一段落、同长度**"——即 `len(a) == len(b)`（delta=0）时两个口径**相等**
- 真实 loop 里历史在增长（delta = 155 字符），两个口径分离 → **87% 与 92% 不是同一个量**

#### 🔴 评审 P0：任何固定阈值都**随工作区路径漂移**

评审发现（我独立复现）：**尾部含 `<environment>工作区: <path>`，路径每长 1 字符，`len(当前请求)` 就 +1，而公共前缀不变 → 比值被稀释。**

| 工作区路径长度 | append-only 前缀（`len(当前请求)` 口径） |
|---|---|
| 8 字符 | **87%** |
| 32 字符 | **87%** |
| 66 字符 | **86%** |
| 442 字符 | **77%** |

```
TMPDIR=/tmp/p0/level1-…/level12-…（442 字符）
→ FAIL  prefix_hit append-only=77% per-turn-trim=13%
```

**所以方案 §11.1 的选项 A（`>= 85`）与 C（`>= 80`）都不可靠** —— 它们只在路径短时成立。

#### 结论

| 说法 | 判定 |
|---|---|
| 产品让前缀掉到 87% | ❌ 不是缺陷。87-89% 是"易失尾部 + 历史增长"的必然结果 |
| 用例阈值 `>= 90` | ❌ **过定 + 环境相关**（见上表） |
| 用例的**相对**断言 | ⚠️ 能区分两臂，但同样受路径稀释 |
| **真正的契约** | ✅ **剥离易失尾部后，请求 N 的稳定部分应当是请求 N+1 的精确前缀** |

#### ✅ 修正方案：布尔不变量（无阈值、路径无关）

```
[append-only]    stable_a=2827  stable_b=2982  b.startswith(a)=True   ← 路径 16 / 454 都一样
[per-turn-trim]  stable_a=3399  stable_b=3404  b.startswith(a)=False  ← 历史被重写
```

- **路径无关**：`stable_a` 恒为 **2827**（路径只进尾部，而尾部被剥离）
- **无魔数阈值**：不再是"≥某个百分比"，而是"前缀是否完好"
- **两臂对比**：append-only 保住前缀；per-turn-trim 的历史被压缩重写 → 不是前缀
- 副产物：稳定口径下的比率是 **95%**，与项目"纯追加回合前缀不被破坏"的设计意图吻合

---

## 三、N-3 调研结论

### 3.1 现象与影响

```
scripts/mcp_echo_server.py:15
  from mcp.server.mcpserver import MCPServer
  → ModuleNotFoundError: No module named 'mcp.server.mcpserver'
```

| 消费者 | 状态 |
|---|---|
| `tests/test_mcp_stdio.py` | **1 failed**（`assert handle is not None`） |
| `tests/test_mcp_end_to_end.py` | **3 failed, 1 passed** |
| CI `Real MCP stdio round trip` / `Real MCP call, whole path` | **两个 step 全红**（`ci.yml:45/51`） |
| `scripts/mcp_many_tools_server.py` | **同样的 bug**（`r7_cases.py:416` 消费，但 r7_cases 未被 CI 调用） |

### 3.2 根因：这是 mcp **2.x** 的 API

实跑版本探测：

| 模块 | mcp 1.30.0 |
|---|---|
| `mcp.server.mcpserver` | ❌ `ModuleNotFoundError` |
| `mcp.server.fastmcp.server.MCPServer` | ✅ 存在，但**只是** `mcp.server.lowlevel.server.Server` 的别名 |
| `mcp.server.fastmcp.FastMCP` | ✅ 有 `.tool()` + `.run(transport=...)` |

关键：`MCPServer` 别名指向的 lowlevel `Server` ——

```
has .tool : False
run sig  : (self, read_stream, write_stream, initialization_options, ...)
```

**没有 `.tool()` 装饰器，`run()` 也不是 `transport=` 形式。** 原脚本是照 mcp 2.x 写的
（[r2-analysis.md:225](file:///workspace/ai-coding/docs/r2-analysis.md#L225) 也写着"真 `MCPServer`（mcp 2.x）"）。

### 3.3 修法：改用官方 `FastMCP`（2 行）

```python
from mcp.server.fastmcp import FastMCP
server = FastMCP("echo")
```

**实测已验证**（在副本内改完跑过）：

```
AI_CODING_MCP_STDIO=1 pytest tests/test_mcp_stdio.py tests/test_mcp_end_to_end.py
→ 5 passed          （改前：1 failed + 3 failed）
```

`mcp_many_tools_server.py` 同样修法，实测：

```
tool count: 13
first 3: ['tool_0', 'tool_1', 'tool_2']
call tool_0: ['tool0:a:False:1']
```

---

## 四、原设计 vs 目前设计

### 4.1 收割批次（case7）

| 阶段 | 批次语义 | 文档 |
|---|---|---|
| R3 及以前 | 每轮一个 delta（收割同步） | `r3-analysis.md:877` "每轮 2 条" |
| **2026-10-17 起** | delta = "游标之后的全部"，**可合并多轮**（收割异步） | `agent_loop.py:1509-1514` ✅ |
| 用例 `case7` | 仍按 R3 的"每轮一批"断言 | ❌ 未跟上 |

### 4.2 缓存前缀（case11）

| 项 | 值 |
|---|---|
| 项目度量 | `prefix_shared(prev, cur) = index / len(cur)`（[trace.py:134](file:///workspace/ai-coding/ai_coding/core/trace.py#L134)） |
| R3 记录 | `92%`（"同一段落、同长度"，两口径相等） |
| 真实 loop | `87%`（`len(cur)` 口径） |
| 用例阈值 | `>= 90`（取自 R3 数字） |

---

## 五、复用的方法契约（**一个都不新建**）

| 契约 | 位置 | 本方案怎么用 |
|---|---|---|
| `AgentLoop.flush_harvest() -> None` | [agent_loop.py:1544](file:///workspace/ai-coding/ai_coding/core/agent_loop.py#L1544) | **复用**：case7 用它排空收割链（docstring 明写"tests; aclose bounds it"） |
| `prefix_shared(previous, current) -> (int, float)` | [trace.py:118](file:///workspace/ai-coding/ai_coding/core/trace.py#L118) | **不改**，只用它的口径说明 |
| `mcp.server.fastmcp.FastMCP` | 官方 SDK | **复用**：N-3 的修法 |
| `session.messages` / `session.harvested_messages` | 既有字段 | **复用**：case7 的不变量判据 |

**没有新增任何函数、类、字段。**

---

## 六、逐文件实现（每一步）

### 6.1 `scripts/memory_cases.py` · case7（唯一改用例的地方）

```diff
 async def case_7_delta_only(root: Path) -> tuple[bool, str]:
-    """Every turn used to re-extract the whole history (2, 4, 6, ... messages)."""
+    """The extractor is never handed the whole history, and never sees it twice.
+
+    The harvest is *scheduled*, not awaited (``_schedule_harvest``), so a turn can
+    finish before the extractor runs and the delta -- "everything since the
+    cursor" -- coalesces the turns that outran extraction.  That is by design
+    (``agent_loop.py:1509-1514``).  What must hold is the invariant, not the batch
+    count: nothing is lost, nothing is shown twice, and no batch is the whole
+    history.
+    """
     extractor = _Extractor("事实")
     root_ws = root / "ws"
     root_ws.mkdir()
     memory = MemoryService(
         MemoryStore(root / "m.json"), extractor=extractor, config=MemoryConfig()
     )
     loop = _loop(_Engine(), root_ws, memory=memory)
     session = loop.sessions.create()
     for index in range(4):
         await loop.process_input(session, f"第{index}轮：聊点别的")
+    # The harvest chain is background work; drain it so the measurement is complete.
+    await loop.flush_harvest()
     sizes = [len(batch) for batch in extractor.seen]
-    repeated = sum(
-        1 for size in sizes if size > 3
-    )
-    ok = len(sizes) == 4 and repeated == 0
-    return ok, f"batch_sizes={sizes}（每轮 ≤3：用户+回答，不含历史）"
+    flat = [message for batch in extractor.seen for message in batch]
+    total = len(session.messages)
+    identity_chain = len(flat) == total and all(
+        left is right for left, right in zip(flat, session.messages, strict=False)
+    )
+    whole_history = any(len(batch) == total for batch in extractor.seen)
+    ok = identity_chain and not whole_history
+    return ok, (
+        f"batch_sizes={sizes} total={total} "
+        f"identity_chain={identity_chain} whole_history={whole_history}"
+    )
```

**为什么这是修正而不是削弱**：

| 旧断言 | 新断言 |
|---|---|
| `len(sizes) == 4` —— 调度产物，随事件循环漂移 | `identity_chain` —— 产品契约（不丢不重） |
| `repeated == 0`（每批 ≤3）—— 与异步设计矛盾 | `not whole_history` —— **正是最初要防的回归**（旧行为是 2/4/6/8 的整段历史） |

> 旧行为的批次是 `[2, 4, 6, 8]`（每批都是"到当前为止的全部历史"）→ `whole_history` 会命中最后一批 → **新断言依然打红旧实现**。
> 判据见 §7 的**突变 C**。

### 6.2 `scripts/memory_cases.py` · case11（改用布尔不变量）

```diff
 async def case_11_cache_prefix(root: Path) -> tuple[bool, str]:
-    """Cost side: append-only turns keep the prompt-cache prefix intact.
-
-    The "per-turn-trim" arm reproduces the M3 policy on purpose (a context object
-    that compacts on *every* turn), which is what the old message-count threshold
-    did from turn 26 onwards.
-    """
+    """Cost side: append-only turns keep the prompt-cache prefix intact.
+
+    The volatile tail (``<environment>`` / ``<task-state>`` / ``[budget]`` …) is
+    rebuilt every turn and sits *after* the history, so a byte-level ratio over the
+    whole request is diluted by it -- and by the workspace path inside it.  That
+    makes a fixed percentage threshold both wrong and machine-dependent (87% on a
+    short path, 77% at 442 characters).
+
+    What must hold is the invariant, not a percentage: with the tail set aside,
+    request N's stable part is an exact prefix of request N+1's.
+
+    The "per-turn-trim" arm reproduces the M3 policy on purpose (a context object
+    that compacts on *every* turn), which rewrites the history -- so its stable part
+    is *not* a prefix.  That contrast is what the case exists to show.
+    """
+
+    #: The tail blocks are recognisable by these markers; the history never is.
+    _VOLATILE_MARKERS = (
+        "<environment>",
+        "<task-state>",
+        "<referenced-files>",
+        "<requested-skills>",
+        "[budget]",
+    )
+
+    def _stable(request: dict) -> str:
+        """A request's instructions + history, with the volatile tail set aside."""
+
+        messages = list(request["messages"])
+        while messages and any(
+            marker in messages[-1] for marker in _VOLATILE_MARKERS
+        ):
+            messages.pop()
+        return request["instructions"] + "\n".join(messages)
+
     class _AlwaysCompacts:
         """M3 behaviour: run the pipeline every request, no budget check."""
```

Then, in the loop:

```diff
-        results[label] = prefix
+        results[label] = {
+            "stable_share": round(100 * len(stable_a) / max(len(stable_b), 1)),
+            "prefix_intact": stable_b.startswith(stable_a),
+        }
```

And the assertion:

```diff
     ok = (
-        results["append-only"] >= 90
-        and results["per-turn-trim"] < 60
-        and results["append-only"] > results["per-turn-trim"]
+        # append-only preserves the prefix; per-turn-trim rewrites the history.
+        results["append-only"]["prefix_intact"]
+        and not results["per-turn-trim"]["prefix_intact"]
     )
     return ok, (
-        f"prefix_hit append-only={results['append-only']}% "
-        f"per-turn-trim={results['per-turn-trim']}%"
+        f"prefix_intact append-only={results['append-only']['prefix_intact']} "
+        f"per-turn-trim={results['per-turn-trim']['prefix_intact']} "
+        f"(stable_share {results['append-only']['stable_share']}% vs "
+        f"{results['per-turn-trim']['stable_share']}%)"
     )
```

> 断掉历史重写以外的判据（比率、阈值）不是因为"改松"，而是因为**比率本身是坏的度量**：
> 实测 per-turn-trim 臂的稳定比率同样是 ~100%（`stable_a=3399 / stable_b=3404`），
> 但它的 `prefix_intact=False` —— 历史被压缩重写。**比率会说"很好"，不变量会说实话。**

### 6.3 `scripts/mcp_echo_server.py`（N-3）

```diff
-from mcp.server.mcpserver import MCPServer
+from mcp.server.fastmcp import FastMCP

-server = MCPServer("echo")
+server = FastMCP("echo")
```

### 6.4 `scripts/mcp_many_tools_server.py`（N-3 同类）

```diff
-from mcp.server.mcpserver import MCPServer
+from mcp.server.fastmcp import FastMCP

-server = MCPServer("many")
+server = FastMCP("many")
```

### 6.5 文档同步（**5 份**）

> 评审 P1：初稿漏了 `docs/features.md`，而 roadmap 的 M-A6 验收判据**点名**要求它
> （"「`docs/features.md` 的数字与实测一致，且注明测量日期与环境」"）。

| 文件 | 行 | 改动 |
|---|---|---|
| `docs/features.md` | 408 | `92%` → 改为"稳定口径 95%（R3 同长度 92%）"+ 注明测量日期 |
| `docs/features.md` | 595 | `纯追加回合实测 0.95` → 与实测一致（0.95 恰好吻合，补口径注释） |
| `docs/eval-01-offline-baseline.md` | 85 | case7 行：改为"**用例断言已修正**"，写明合并是设计 |
| `docs/eval-01-offline-baseline.md` | 86 | case11 行：改为"口径改为布尔不变量；比率仅参考" |
| `docs/eval-02-feature-matrix.md` | 96/97 | G8/G9 的 ❌ 改为 ✅ |
| `docs/roadmap-milestones.md` | §M-A6 / §N-3 | 标 ✅ + 写明实测结论 |

> `r4-analysis.md:653`、`hotel-fix-backlog.md:156`、`roadmap-milestones.md:241/243`
> 也含同一数字，但都是**历史分析稿**，不追溯标注（沿用 M-A2 的既定处理）。

---

## 七、TDD / RIPR

### 7.1 红相（三条，都可机检）

```bash
# ① case7 / case11 本来就是红的
.venv/bin/python scripts/memory_cases.py | tail -3
#   FAIL  case7-delta-only
#   FAIL  case11-cache-prefix
#   SUMMARY 9/11 passed

# ② N-3：CI 的两个 step 本来就是红的
AI_CODING_MCP_STDIO=1 .venv/bin/python -m pytest tests/test_mcp_stdio.py -q -o addopts=""
#   1 failed
AI_CODING_MCP_STDIO=1 .venv/bin/python -m pytest tests/test_mcp_end_to_end.py -q -o addopts=""
#   3 failed, 1 passed

# ③ case7 旧断言确实随调度漂移（证明它测的不是契约）
grep -c "len(sizes) == 4" scripts/memory_cases.py     # 1
```

### 7.2 RIPR（缺一不可）

| 相 | 动作 | 判据（可机检） |
|---|---|---|
| **R** | 记录基线 | `memory_cases` = **9/11**；MCP stdio **1 failed**；end_to_end **3 failed** |
| **I** | §6.1 case7 改用例 | case7 转 **PASS**，输出含 `identity_chain=True` |
| **I** | §6.3/6.4 改 2 个脚本 | 两个 CI step **全绿** |
| **I** | §6.2 case11 改布尔不变量 | case11 转 **PASS**，输出含 `prefix_intact append-only=True per-turn-trim=False` |
| **I** | §6.5 文档同步（5 份） | 文档数字与实测一致 |
| **P** | `.venv/bin/python scripts/memory_cases.py` | **11/11** |
| **P** | `AI_CODING_MCP_STDIO=1 pytest tests/test_mcp_stdio.py tests/test_mcp_end_to_end.py` | **5 passed** |
| **P** | 全量 `pytest` | **0 failed**，skipped ≤ 6 |
| **P** | `ruff check .` / `mypy ai_coding` | **0 / 0**（保持 M-A5 成果） |
| **P** | **突变 A**：`mcp_echo_server.py` 改回 `mcp.server.mcpserver` | stdio step **重新变红** |
| **P** | **突变 B**：`mcp_many_tools_server.py` 改回 | `r7_cases` 的 MCP 档**重新报错** |
| **P** | **突变 C**：把 case7 改回 `len(sizes) == 4` | case7 **重新变红**（证明新断言不是恒真） |
| **P** | **突变 D**：把 case11 改回比率阈值 `>= 90` | **深路径（442 字符）下重新变红**；`prefix_intact` 版仍为绿 → 证明路径无关 |
| **R** | `git status --porcelain` 白名单 | 见 §7.3 |

### 7.3 R 相白名单

```
M  scripts/memory_cases.py
M  scripts/mcp_echo_server.py
M  scripts/mcp_many_tools_server.py
M  docs/features.md
M  docs/eval-01-offline-baseline.md
M  docs/eval-02-feature-matrix.md
M  docs/roadmap-milestones.md
?? docs/plan-m-a6-n3.md
```

即 **3 个脚本 + 5 份文档 + 1 份方案**。**不碰任何产品代码**（`ai_coding/` 零改动）。

### 7.4 具体用例清单

| # | 用例 | 类型 | 期望 |
|---|---|---|---|
| U1 | `scripts/memory_cases.py`（11 例） | 真实业务 | **11/11** |
| U2 | `tests/test_mcp_stdio.py`（门控） | 真实 stdio 往返 | 1 passed |
| U3 | `tests/test_mcp_end_to_end.py`（门控） | 真实全链路 | 4 passed |
| U4 | `scripts/r7_cases.py` 的 MCP 档 | 真实 stdio | 不再报 `ModuleNotFoundError` |
| U5 | 全量 `pytest` | 回归 | 0 failed |

> 本方案**不新增测试文件** —— 没有新功能，只有"把错期望改对"和"把坏夹具修好"。

---

## 八、架构图

### 8.1 case7 的数据流（**本方案不改产品代码**）

```
   process_input()  ──►  追加 [user, assistant] 到 session.messages
        │
        └─► _schedule_harvest()  ──► asyncio.create_task  ← 不 await（2026-10-17 起）
                                            │
                                            ▼
                               harvest()  delta = messages[游标:]
                                            │
                                            ├─ 提取器看到：[b0..bk]（可含多轮）
                                            └─ 游标 += len(delta)   ← 只推进"已示出的"

   不变量：Σ(批次) == 全部消息，且按顺序、不重复、任一批 ≠ 整段历史
```

### 8.2 case11 的请求布局（**本方案不改产品代码**）

```
   请求 N      [ 系统提示 355 ][ 历史 H …2827 ][ 易失尾部 T …256 ]
   请求 N+1    [ 系统提示 355 ][ 历史 H …2827 ][ a_N u_{N+1} ][ 易失尾部 T' … ]
                                     └──────┬──────┘
                                           └─ 字节前缀在此断裂（尾部必须跟在历史后）

   比率口径（路径敏感 ❌）：len(公共前缀) / len(请求N+1) = 87%（442 字符路径下 77%）
   不变量口径（路径无关 ✅）：strip(请求N+1).startswith(strip(请求N))  → True
                              per-turn-trim 臂同法测量 → False（历史被重写）
```

### 8.3 N-3 的修复

```
   改前：mcp.server.mcpserver.MCPServer   ← mcp 2.x，1.30.0 里不存在
   改后：mcp.server.fastmcp.FastMCP       ← 官方高层 API，有 .tool() / .run(transport=)
```

---

## 九、修改成功后的效果

| 观测点 | 改前 | 改后 |
|---|---|---|
| `scripts/memory_cases.py` | **9/11** | **11/11** |
| CI `Memory acceptance cases` | 🔴 红 | 🟢 绿 |
| CI `Real MCP stdio round trip` | 🔴 1 failed | 🟢 1 passed |
| CI `Real MCP call, whole path` | 🔴 3 failed | 🟢 4 passed |
| `ruff` / `mypy` | 0 / 0 | **0 / 0**（保持） |
| `ai_coding/` 产品代码 | — | **零改动** |
| case7 测的东西 | 调度产物（会漂移） | **产品契约**（不丢不重） |
| case11 测的东西 | 比率阈值（路径敏感） | **布尔不变量**（路径无关） |
| case11 的文档 | 92%（不可比） | 与实测口径一致 |

### 9.1 按此计划实施后的最终模样（我的推演）

- `python scripts/memory_cases.py` → `SUMMARY 11/11 passed`
- `AI_CODING_MCP_STDIO=1 pytest tests/test_mcp_stdio.py tests/test_mcp_end_to_end.py` → `5 passed`
- `ruff check .` → `All checks passed!`；`mypy ai_coding` → `Success: no issues found in 152 source files`
- 全量 `pytest` → `3812 collected = 3806 passed + 6 skipped / 0 failed`
- **深路径（442 字符）下 case11 仍为绿** ← 这是旧方案做不到的
- CI **7 个 step 全绿**
- `git diff --stat` 只有 3 个脚本 + 5 份文档 + 1 份方案

---

## 十、自我核验

1. **case7 是不是"把测试改松了"？** —— 不是。旧断言 `len(sizes)==4` 测的是**事件循环时序**（同一测试两次记录不同：`[3,4]` vs `[3,4,1]`）。新断言 `identity_chain` + `not whole_history` 直接对应产品契约，且**能打红旧实现**（旧实现批次是 `[2,4,6,8]`，最后一批 = 整段历史）。
2. **case11 的 87% 有没有可能是产品回归？** —— 已排除。易失尾部 ≈256 字符、历史 ≈2827、delta ≈155，三者相除即得 87-89%。产品布局（尾部跟在历史后）已是**最优**，塞进系统提示只会更早破前缀（`cache_probe.py` 的 `churn` 模式实测掉到 17%）。
3. **固定阈值可不可靠？** —— ❌ **不可靠**（评审 P0，我独立复现）：尾部含工作区路径，442 字符路径下掉到 **77%**。已改用**路径无关的布尔不变量**。
4. **改用布尔不变量是不是把测试改松了？** —— 不是，反而**更严**：它要求"请求 N 的稳定部分是请求 N+1 的**精确前缀**"。而比率是**更弱**的判据 —— 实测 per-turn-trim 臂的稳定比率同样 ~100%，但它的 `prefix_intact=False`（历史被重写）。**比率会说"很好"，不变量会说实话。**
5. **case7 是不是"把测试改松了"？** —— 不是。旧断言 `len(sizes)==4` 测的是**事件循环时序**（同一测试两次记录不同：`[3,4]` vs `[3,4,1]`）。新断言 `identity_chain` + `not whole_history` 直接对应产品契约，且**能打红旧实现**（旧实现批次是 `[2,4,6,8]`，最后一批 = 整段历史）。
6. **N-3 的修法有没有更"省事"的？** —— 有：`from mcp.server.fastmcp.server import MCPServer` 也能 import 成功。**但那只是 lowlevel `Server` 的别名，没有 `.tool()`，`run()` 也不是 `transport=` 形式** → 改了还是跑不通。必须用 `FastMCP`。
7. **会不会漏了别的消费者？** —— `mcp_echo_server.py` 的消费者：`test_mcp_stdio.py`、`test_mcp_end_to_end.py`（已核）。`mcp_many_tools_server.py` 的消费者：`r7_cases.py:416`（已核，未被 CI 调用）。**无其它引用**。
8. **会不会碰坏产品代码？** —— 不会。`ai_coding/` **零改动**，白名单里没有它。
9. **`r7_cases.py` 要不要一起跑？** —— 它需要 `config.local.yaml` 且未被 CI 调用；本方案只修它依赖的夹具，不改它本身。

---

## 十一、拍板结论（已按批准执行）

| # | 问题 | 你的决定 | 执行 |
|---|---|---|---|
| 11.1 | case11 修法 | ✅ **改用布尔不变量** | ✅ 已实施 |
| 11.2 | 文档里的 92% | ✅ **保留 92% + 加口径注释** | ✅ 已实施（`features.md:408`、`:595`） |
| 11.3 | `mcp_many_tools_server.py` | ✅ **一并修** | ✅ 已实施 |

### 11.1 为什么不是固定阈值

原 §11.1 的选项 A（`>= 85`）/ C（`>= 80`）**已作废** —— 评审 P0 发现阈值随工作区路径漂移，
442 字符路径下 87% → **77%**，任何固定阈值都不可靠。

**最终方案：布尔不变量** —— `strip(请求N+1).startswith(strip(请求N))`，
两臂分别为 `True` / `False`，**无阈值、路径无关、5 次实跑稳定**。

> 比率是**坏度量**：实测 per-turn-trim 臂的稳定比率同样 ~100%，但 `prefix_intact=False`
> （历史被重写）。**比率会说好，不变量会说实话。**

### 11.2 文档里的 92% 怎么处理

- ① 保留 92%，**加口径注释**（"R3 同长度测量；真实 loop 稳定口径 95%"）—— ✅ 已选

### 11.3 `mcp_many_tools_server.py` 是否一并修

- ① **一并修**（推荐）—— 同类缺陷，改了只要 2 行，`r7_cases` 的 MCP 档随之恢复
- ② 只修 `mcp_echo_server.py`，`many` 那个另立项

---

## 十二、评审记录

子智能体在 `/workspace/review-m-a6-n3`（detached HEAD `6265451`）**独立重跑全部结论**：

- **技术判断全部成立**：版本事实、根因、基线红相、修法、回归数字逐条复现，无一虚假
- 方案的核心主张（新断言"能打红旧实现"）经反证成立：模拟旧实现 `[2,4,6,8]` → `ok=False`
- 消费者清单、白名单、`ruff`/`mypy`、全量 `pytest`（3812）均核实

### 12.1 评审发现（已处理）

| # | 问题 | 处理 |
|---|---|---|
| 🔴 **P0** | **case11 阈值随工作区路径漂移**：尾部含路径，442 字符下 87%→77%，任何固定阈值都不可靠 | ✅ **改用布尔不变量**（§2.2、§6.2）；我自己独立复现了 77% |
| 🟠 **P1** | **漏了 `docs/features.md`** —— roadmap 的 M-A6 验收判据点名要求它，且 `features.md:408` 含 92%、`:595` 含 0.95 | ✅ 已补进 §6.5 与白名单（**5 份文档**） |
| 🟡 **P2** | §2.2 的尾部数字 215 与实跑不符（未含路径） | ✅ 已改为 ≈256 并标注"随路径浮动" |
| 🟡 **P2** | §7.4 U4（`r7_cases`）在副本内不可机检（缺 `config.local.yaml`） | ✅ 已改为"直接 spawn 夹具验证"，并说明原因 |

### 12.2 我的复核

- 77% 那条我自己跑了 4 档路径（8 / 32 / 66 / 442 字符）→ 87 / 87 / 86 / **77**
- 布尔不变量跑了 5 次 + 2 档路径（16 / 454 字符）→ 两臂均逐次稳定
- 方案初稿的 `stable_a=2827` 路径无关性，独立复现为真

---

## 十三、实施记录（2026-10-25）

### 13.1 提交

| commit | 内容 |
|---|---|
| `6265451` | 方案（含实跑证据与 RIPR 判据） |
| `b65cc3d` | 方案按评审修订（P0 → 布尔不变量） |
| `a755022` | **M-A6**：case7/case11 改用例断言（9/11 → 11/11） |
| `8ece1cd` | **N-3**：两个 stdio 夹具改用官方 FastMCP |
| `2c565a1` | 同步 5 份文档与 roadmap |
| 本提交 | 实施记录 |

### 13.2 判据复核（全部实跑）

| 判据 | 期望 | 实测 |
|---|---|---|
| `.venv/bin/python scripts/memory_cases.py` | 11/11 | **11/11 passed** |
| **深路径（442 字符）下** | 11/11 | **11/11 passed**（旧判据在此会红：77%） |
| `AI_CODING_MCP_STDIO=1 pytest` 两个 MCP 文件 | 5 passed | **5 passed** |
| `ruff check .` | 0 | **All checks passed!** |
| `mypy ai_coding` | 0 | **Success: no issues found in 152 source files** |
| 全量 `pytest` | 0 failed，skipped ≤ 6 | **3806 passed / 6 skipped / 0 failed** |
| **突变 A**（echo 改回旧 API） | stdio 重新变红 | **1 failed** ✅ |
| **突变 B**（many 改回旧 API） | 夹具起不来 | **`McpError: Connection closed`** ✅ |
| **突变 C**（case7 改回 `len(sizes)==4`） | case7 重新变红 | **FAIL** ✅ |
| **突变 D**（case11 改回整请求比率 `>=90`） | 深路径重新变红 | **FAIL（87% / 77%）** ✅ |

### 13.3 白名单核验

```
M  scripts/memory_cases.py
M  scripts/mcp_echo_server.py
M  scripts/mcp_many_tools_server.py
M  docs/features.md
M  docs/eval-01-offline-baseline.md
M  docs/eval-02-feature-matrix.md
M  docs/roadmap-milestones.md
?? docs/plan-m-a6-n3.md
```

**3 个脚本 + 5 份文档 + 1 份方案**；`ai_coding/` **零改动**（实测 `git diff --name-only | grep -c '^ai_coding/'` = **0**）。

### 13.4 实施中发现并修正的两点

| # | 问题 | 处理 |
|---|---|---|
| **I-1** | 方案 §7.4 的 U4 写"跑 `r7_cases` 验证 MCP 档"，但 `real_mcp_tool_surface`（唯一用 `many` 脚本的用例）在 **LIVE** 档（`r7_cases.py:487`），离线运行不覆盖 | 改为**直接 spawn 夹具**验证（`13.2` 突变 B），并注明原因 |
| **I-2** | 突变 D 首次实现有缺陷（`return` 仍引用 `intact` → `KeyError`），不是有效的突变 | 修正为同时替换断言与 `return`，复现出真实的 87% / 77% |
