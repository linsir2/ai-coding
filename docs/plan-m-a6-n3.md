# M-A6 + N-3 实施方案 · 文档/实测不一致收口 + MCP 夹具修复

> 状态：**待拍板**
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

| 块 | 长度 | 内容 |
|---|---|---|
| `<environment>` | 80 | 工作区 + **时间戳** |
| `<task-state>` | 78 | **目标 = 本轮用户消息** |
| `[budget]` | 57 | 步数 1/1 的收尾提示 |
| **合计** | **215** | |

请求结构：`[历史 2828][尾部 215]`，下一回合变成 `[历史 2828][a₁₆ u₁₇][尾部 215]`。
→ 字节前缀在**尾部处**断裂。这是**最优布局**：尾部必须跟在历史之后（若塞进系统提示，前缀会更早破）。

#### 92% 与 87% 的口径差异（关键）

```
len(公共前缀) / len(前一个请求) = 2828 / 3045 = 92.9%   ← 文档的 92%
len(公共前缀) / len(当前请求)   = 2828 / 3200 = 88.4%   ← 项目 prefix_shared 与用例的口径
```

- 项目自己的度量 [trace.py:118-134](file:///workspace/ai-coding/ai_coding/core/trace.py#L118-L134) `prefix_shared()` 用 **`index / len(current)`**
- [r3-analysis.md:881](file:///workspace/ai-coding/docs/r3-analysis.md#L881) 记的 **92%** 注明"**同一段落、同长度**"——即 `len(a) == len(b)`（delta=0）时两个口径**相等**
- 真实 loop 里历史在增长（delta = 155 字符），两个口径分离 → **87% 与 92% 不是同一个量**

#### 结论

| 说法 | 判定 |
|---|---|
| 产品让前缀掉到 87% | ❌ 不是缺陷。87-89% 是"易失尾部 215 字符 + 历史增长"的必然结果 |
| 用例阈值 `>= 90` | ⚠️ **过定**（over-specified）：把不可比的文档数字硬编进真实 loop |
| 用例的**相对**断言 | ✅ 成立且是真正的目的：`append-only(87%) >> per-turn-trim(15%)` |

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

### 6.2 `scripts/memory_cases.py` · case11（阈值 + 测量条件）

见 §6.5 的待拍板项 —— **三个选项**，我不替你选。

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

### 6.5 文档同步（4 处）

| 文件 | 改动 |
|---|---|
| `docs/eval-01-offline-baseline.md:85` | case7 行：改为"**用例断言已修正**"，写明合并是设计 |
| `docs/eval-01-offline-baseline.md:86` | case11 行：改为实测 87% + 口径说明 |
| `docs/eval-02-feature-matrix.md:96/97` | G8/G9 两行的 ❌ 改为 ✅（或按 §6.5 的选项） |
| `docs/roadmap-milestones.md` §M-A6 / §N-3 | 标 ✅ + 写明实测结论 |

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
| **I** | §6.2/6.5 按拍板改 | case11 转 **PASS**；文档与实测一致 |
| **P** | `.venv/bin/python scripts/memory_cases.py` | **11/11** |
| **P** | `AI_CODING_MCP_STDIO=1 pytest tests/test_mcp_stdio.py tests/test_mcp_end_to_end.py` | **5 passed** |
| **P** | 全量 `pytest` | **0 failed**，skipped ≤ 6 |
| **P** | `ruff check .` / `mypy ai_coding` | **0 / 0**（保持 M-A5 成果） |
| **P** | **突变 A**：`mcp_echo_server.py` 改回 `mcp.server.mcpserver` | stdio step **重新变红** |
| **P** | **突变 B**：`mcp_many_tools_server.py` 改回 | `r7_cases` 的 MCP 档**重新报错** |
| **P** | **突变 C**：把 case7 改回 `len(sizes) == 4` | case7 **重新变红**（证明新断言不是恒真） |
| **R** | `git status --porcelain` 白名单 | 见 §7.3 |

### 7.3 R 相白名单

```
M  scripts/memory_cases.py
M  scripts/mcp_echo_server.py
M  scripts/mcp_many_tools_server.py
M  docs/eval-01-offline-baseline.md
M  docs/eval-02-feature-matrix.md
M  docs/roadmap-milestones.md
?? docs/plan-m-a6-n3.md
```

即 **3 个脚本 + 3 份文档 + 1 份方案**。**不碰任何产品代码**（`ai_coding/` 零改动）。

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
   请求 N      [ 系统提示 355 ][ 历史 …2828 ][ 易失尾部 215 ]
   请求 N+1    [ 系统提示 355 ][ 历史 …2828 ][ a_N u_{N+1} ][ 易失尾部 215 ]
                                     └──────┬──────┘
                                           └─ 字节前缀在此断裂（尾部必须跟在历史后）

   len(公共前缀)/len(请求N)   = 92%   ← 文档的 92%（R3 同长度测量）
   len(公共前缀)/len(请求N+1) = 87%   ← 项目 prefix_shared 与用例的口径
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
| case11 的文档 | 92%（不可比） | 与实测口径一致 |

### 9.1 按此计划实施后的最终模样（我的推演）

- `python scripts/memory_cases.py` → `SUMMARY 11/11 passed`
- `AI_CODING_MCP_STDIO=1 pytest tests/test_mcp_stdio.py tests/test_mcp_end_to_end.py` → `5 passed`
- `ruff check .` → `All checks passed!`；`mypy ai_coding` → `Success: no issues found in 152 source files`
- 全量 `pytest` → `3812 collected = 3806 passed + 6 skipped / 0 failed`
- CI **7 个 step 全绿**
- `git diff --stat` 只有 3 个脚本 + 3 份文档 + 1 份方案

---

## 十、自我核验

1. **case7 是不是"把测试改松了"？** —— 不是。旧断言 `len(sizes)==4` 测的是**事件循环时序**（同一测试两次记录不同：`[3,4]` vs `[3,4,1]`）。新断言 `identity_chain` + `not whole_history` 直接对应产品契约，且**能打红旧实现**（旧实现批次是 `[2,4,6,8]`，最后一批 = 整段历史）。
2. **case11 的 87% 有没有可能是产品回归？** —— 已排除。尾部 215 字符、历史 2828、delta 155，三者相除即得 87-89%；且 5 次实跑**逐次相同**。产品布局（尾部跟在历史后）已是**最优**，塞进系统提示只会更早破前缀。
3. **N-3 的修法有没有更"省事"的？** —— 有：`from mcp.server.fastmcp.server import MCPServer` 也能 import 成功。**但那只是 lowlevel `Server` 的别名，没有 `.tool()`，`run()` 也不是 `transport=` 形式** → 改了还是跑不通。必须用 `FastMCP`。
4. **会不会漏了别的消费者？** —— `mcp_echo_server.py` 的消费者：`test_mcp_stdio.py`、`test_mcp_end_to_end.py`（已核）。`mcp_many_tools_server.py` 的消费者：`r7_cases.py:416`（已核，未被 CI 调用）。**无其它引用**。
5. **会不会碰坏产品代码？** —— 不会。`ai_coding/` **零改动**，白名单里没有它。
6. **`r7_cases.py` 要不要一起跑？** —— 它需要 `config.local.yaml` 且未被 CI 调用；本方案只修它依赖的夹具，不改它本身。

---

## 十一、待拍板

### 11.1 case11 的阈值怎么定（**三选一，请拍板**）

| 选项 | 做法 | 好处 | 代价 |
|---|---|---|---|
| **A（推荐）** | 阈值 `>= 90` → **`>= 85`**，并写明测量条件（真实 loop、尾部 215 字符、口径 `len(当前请求)`） | 保留现有口径（与项目 `prefix_shared` 一致）；改动最小；相对断言（87% ≫ 15%）不动 | 阈值余量只有 2 个点 |
| **B** | **改口径**：只测"稳定前缀"（把易失尾部排除），阈值保持 `>= 90` | 直接对应设计意图（"系统提示+历史"不变）；数字回到 90%+ | 与项目 `prefix_shared` 口径不一致；测试要自己识别尾部边界 |
| **C** | 阈值 `>= 90` → **`>= 80`** | 余量最大 | 余量过大，弱化了对回归的敏感度 |

> 我不替你选：A 改数字、B 改口径、C 改数字且更松。三者都能让 CI 转绿，但**语义不同**。

### 11.2 文档里的 92% 怎么处理

- ① 保留 92%，**加口径注释**（"R3 同长度测量；真实 loop 为 87%"）
- ② 直接把 92% 改成 87%

### 11.3 `mcp_many_tools_server.py` 是否一并修

- ① **一并修**（推荐）—— 同类缺陷，改了只要 2 行，`r7_cases` 的 MCP 档随之恢复
- ② 只修 `mcp_echo_server.py`，`many` 那个另立项
