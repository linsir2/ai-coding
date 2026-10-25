# M-A2 + M-A5 实施方案 · 契约漂移收敛 + lint/type 门禁清零

> 状态：**✅ 已完成（2026-10-24）** —— 实施记录见 §十四
> 关联：[roadmap-milestones.md](roadmap-milestones.md) §1 M-A2 / M-A5
> 依赖：M-A1（`httpx2`）、M-A3（`restore_ok`）
> 评审：子智能体 `git worktree` 实跑评审（见 §十二），4 条阻塞项已修订

---

## 一、先回答质疑：工具列表**没有**被删，也不会被删

你的担心是"工具列表不能没有吧"。**它确实在，而且有真实生产消费者。**

### 1.1 工具列表的**唯一来源**是 `available()`，不是 `open_all()` 的返回值

```
[mcp.py:1419]  def available(self) -> list[tuple[str, list[MCPToolHandle]]]
```

### 1.2 生产链路（`server/world.py`，共 3 处，环环相扣）

```python
world.py:183    await mcp_manager.open_all()                              # ① 只起 supervisor
world.py:195    registry.register(MCPCallTool(mcp_manager))               # ② 注册唯一的 MCP 工具
world.py:204    catalog_builder=lambda: build_mcp_catalog(
                    mcp_manager.available())                              # ③ 目录每步从 available() 重建
```

- ② 的 `MCPCallTool` 是 LLM 唯一能看到的 MCP 工具（`mcp_call(server, tool, input)`）
- ③ 的 `catalog_builder` 把 `available()` 渲染成有预算的目录，挂进 volatile tail
- 派发时 [mcp_call.py:92](file:///workspace/ai-coding/ai_coding/tools/mcp_call.py#L92) 也是 `rows = self._manager.available()` 校验目标

### 1.3 实测证据（本次会话实跑）

| 证据 | 结果 |
|---|---|
| `await manager.open_all()` 的返回值 | **`None`**（真机实跑，非推断） |
| `tests/test_mcp_lifecycle.py`（`available()` 随连接填充） | **40 passed** |
| `tests/test_mcp_call_tool.py`（`MCPCallTool` 按 `available()` 派发） | 同上 |
| `tests/test_mcp_catalog.py`（`build_mcp_catalog` 渲染） | 同上 |

### 1.4 本方案**完全不碰**这三处

`available()` / `build_mcp_catalog` / `MCPCallTool` / `world.py` —— **一行都不改**。

> 所以"工具列表没了"不会发生。真正错的只是**几份旧文档**说它从 `open_all()` 返回。

---

## 二、要实现的内容（两句话）

**M-A2**：`open_all()` 的返回值在**旧文档**里有两种说法（`[]` / `list[BaseTool]`）——
统一为代码的真实契约 `-> None`，并修掉一个**因此变成空转的评估用例**。

**M-A5**：`ruff check .` 6 条、`mypy ai_coding` 3 条 → **全零**。
CI 已把这两步作为硬门禁（[ci.yml:35-39](file:///workspace/ai-coding/.github/workflows/ci.yml#L35-L39)），
**所以 CI 现在是红的**。

---

## 三、原设计 vs 目前设计（为什么会有两种说法）

| 阶段 | `open_all()` 的契约 | 工具面来源 | 文档 |
|---|---|---|---|
| **R1a-6 / R1**（旧） | `-> list[BaseTool]`，**阻塞**连完再返回 | 返回值 | `r1a-6-analysis.md`、`r1-analysis.md`、`architecture.md` |
| **M2** | 同上 | 改为 `available()`（工具不再逐个注册为 schema） | `features.md:535` |
| **M3 起**（现在） | **`-> None`，非阻塞**，只起 supervisor | `available()` | `data-contracts.md:121` ✅、`features.md:536` ✅ |

**M3 的改造把 `open_all` 从"阻塞 + 返回工具"改成"非阻塞 + 不返回"**，理由是
启动不能让 TUI 卡住（[features.md:536](file:///workspace/ai-coding/docs/features.md#L536)、
[specs/2026-10-10-external-mcp.md:185](file:///workspace/ai-coding/docs/specs/2026-10-10-external-mcp.md#L185)）。

旧文档没跟上 —— 这就是 M-A2 的全部内容。

### 3.1 为什么不能选"让 `open_all()` 返回工具列表"（roadmap 的建议 ②）

**物理上不可能**：非阻塞意味着返回时 server 还没连上，工具不存在。
若强行返回，只能返回 `[]` —— 那就是**撒谎**（正是 M-A2 要消灭的那种谎）。

> roadmap 的建议 ② 引用了 R1a-6 的设计，但 R1a-6 是**旧设计分析**，不是 spec。

---

## 四、复用的方法契约（一个都不新建）

| 契约 | 位置 | 本方案怎么用 |
|---|---|---|
| `MCPManager.available() -> list[tuple[str, list[MCPToolHandle]]]` | [mcp.py:1419](file:///workspace/ai-coding/ai_coding/core/mcp.py#L1419) | **复用**：`sample_tasks.py` 改读它 |
| `MCPManager.open_all() -> None` | [mcp.py:1097](file:///workspace/ai-coding/ai_coding/core/mcp.py#L1097) | **不改**，文档对齐它 |
| `build_mcp_catalog(rows) -> str` | [mcp.py:320](file:///workspace/ai-coding/ai_coding/core/mcp.py#L320) | **不改** |
| `_find_leaf(exc, cls) -> BaseException \| None` | [mcp.py:683](file:///workspace/ai-coding/ai_coding/core/mcp.py#L683) | **只收紧 `cls` 标注** |

**没有新增任何函数、类、字段。** 唯一的"新增"是 `sample_tasks.py` 里把
`(await open_all()) or []` 换成 `manager.available()` —— 这是**换数据源**，不是造轮子。

---

## 五、对代码的影响 + 执行范围

### 5.1 M-A2 的影响：**产品代码零改动**

| 文件 | 改动 | 影响 |
|---|---|---|
| `docs/features.md:542` | 一句话改措辞 | 无 |
| `docs/features.md:516` | 加"（M3 起非阻塞）" | 无 |
| `docs/r1a-6-analysis.md` ×4 处 | 加"已被 M3 取代"标注 | 无 |
| `docs/r1-analysis.md` ×2 处 | 同上 | 无 |
| `docs/architecture.md` ×1 处 | 同上 | 无 |
| `docs/eval-03-live-features.md:120` | 同步补丁已移除 | 无 |
| `scripts/sample_tasks.py` | 换数据源 | **让空转用例变成真用例**（见 §7.3） |

### 5.2 M-A5 的影响：**运行时行为零改动**

9 条全是类型标注 / import 顺序 / 未用变量 / 行长。

---

## 六、数据流（含架构图）

### 6.1 工具面的真实数据流（**本方案不改**）

```
        ┌─────────────────────────────────────────────────────────┐
        │ server/world.py                                         │
        │                                                         │
  :183  │  await mcp_manager.open_all()   ──┐                     │
        │     只起 supervisor，返回 None     │                     │
        │                                   ↓                     │
        │                        ┌──────────────────────┐          │
        │                        │ 每个 server 一个      │          │
        │                        │ supervisor 任务       │          │
        │                        │ (连接/重连/持有)      │          │
        │                        └──────────┬───────────┘          │
        │                                   ↓ _owners              │
  :204  │  catalog_builder() ──→ mcp_manager.available()          │
        │                          └→ build_mcp_catalog()          │
        │                               └→ volatile tail（每步重建）│
  :195  │  registry.register(MCPCallTool(mcp_manager))             │
        │      └→ LLM 只见 mcp_call(server, tool, input)           │
        └─────────────────────────────────────────────────────────┘
                                     ↓ 调用时
                        ┌────────────────────────────┐
                        │ mcp_call.py:92             │
                        │ rows = manager.available() │ ← 校验目标在不在
                        │ handle = manager.lookup()  │
                        └────────────────────────────┘
```

**工具列表在这条链上全程存在。`open_all()` 的返回值不参与。**

### 6.2 M-A2 改的只是文档层

```
改前：docs 说 open_all() 返回 []/list[BaseTool]
      ↓
      未来贡献者照文档写 `for tool in await open_all():` → TypeError
      （sample_tasks.py 当年就是这么被坑的，才打了 or [] 补丁）

改后：docs 与代码一致（-> None），工具面统一指向 available()
      ↓
      照文档写 `manager.available()` → 正确
```

### 6.3 M-A5 改的是门禁

```
改前：ruff 6 + mypy 3 → CI 的 Lint / Type-check 红 → 整个 CI 红
改后：ruff 0 + mypy 0 → CI 绿
```

---

## 七、逐文件实现（每一步）

### 7.1 `docs/features.md:542`

```diff
-`mcp.enabled=false`（默认）时 `open_all()` 立即返回空列表，不连接任何服务器。
+`mcp.enabled=false`（默认）时 `open_all()` 立即返回（`-> None`，不连接任何服务器）；
+可调用工具一律从 `MCPManager.available()` 读，`open_all` 从不返回工具列表。
```

### 7.2 `docs/features.md:516`

```diff
-`MCPManager` 用官方 `mcp` 包**直接连 stdio 服务器**：`open_all()` 连接启用的 server、
+`MCPManager` 用官方 `mcp` 包**直接连 stdio 服务器**：`open_all()` 起 supervisor（M3 起非阻塞）、
```

### 7.3 `scripts/sample_tasks.py` —— 唯一需要动的消费方

**现状（这是空转用例，必须说清楚）**：

```python
tools = (await manager.open_all()) or []      # open_all() 恒返回 None → tools 恒为 []
return tools == [], f"enabled={app_cfg.mcp.enabled} tools={len(tools)}"
```

**逻辑证明**：`open_all()` 恒返回 `None`（已实跑证实）→ `None or []` 恒为 `[]`
→ `tools == []` **恒为 True**。

**也就是说：即使 MCP 开着、连上了 10 个服务器、有 200 个工具，这个场景照样 PASS。**
它什么都没验证。

**改后**：

```diff
-        # eval-03 minimal fix: open_all() returns None (the tool surface now
-        # comes from manager.available()), but this scenario asserts on a count.
-        tools = (await manager.open_all()) or []
+        await manager.open_all()
+        # The tool surface comes from available(), never from open_all's return
+        # value: open_all is non-blocking and returns before any server is up.
+        rows = manager.available()
     finally:
         await manager.aclose()
-    return tools == [], f"enabled={app_cfg.mcp.enabled} tools={len(tools)}"
+    return rows == [], f"enabled={app_cfg.mcp.enabled} servers={len(rows)}"
```

**为什么这是改进而不是删除**：断言从"一个恒真的东西"变成"`available()` 为空"。
MCP 关 → 没有任何 owner → `available()` 返回 `[]` → 断言成立且**有意义**。

> ⚠️ 语义迁移：`available()` 返回 `list[tuple[server, handles]]`，`len(rows)` 是
> **server 数**而非工具数，故文案 `tools=` 改 `servers=`。

### 7.4 三份历史分析文档 —— 加标注，不改正文

**为什么只加标注**：这些是**历史分析文档**。改写正文会让它假装自己当时就那么想。
标注既保留历史，又不误导读者。

统一插入（**每个漂移点就近插**，共 7 处 —— 评审 P2-1：只插一处的话，
读者跳读到 §251 仍会看到无警示的 `[]` 断言）：

```markdown
> ⚠️ **已被 M3 取代**：M3 起 `open_all()` 改为非阻塞（只起 supervisor 即返回），
> 签名是 `async def open_all(self) -> None`，**不返回工具列表**。本节是旧设计分析，
> 保留作为历史记录。工具表面请读 `MCPManager.available()`。
```

| 文件 | 行 | 位置 |
|---|---|---|
| `r1a-6-analysis.md` | 156 | 签名代码块后 |
| `r1a-6-analysis.md` | 165 | "返回 `[]`" 那句后 |
| `r1a-6-analysis.md` | 177 | `for tool in await ...` 代码块后 |
| `r1a-6-analysis.md` | 251 | "核验③"段内 |
| `r1-analysis.md` | 818 | 类签名代码块后 |
| `r1-analysis.md` | 830 | "把返回工具注册进注册表"那句后 |
| `architecture.md` | 87 | 架构图下方 |

### 7.5 `docs/eval-03-live-features.md:120`

该表把 `or []` 记为"最小修复"。7.3 删掉补丁后此表会与代码矛盾，改为：

```diff
-| ... | `tools = (await manager.open_all()) or []`（`scripts/sample_tasks.py`） | `--only mcp-off` → **PASS**，全量 **8/8** |
+| ... | 评估期打了 `or []` 补丁；**M-A2 已改为读 `manager.available()`**（补丁移除） | `--only mcp-off` → **PASS** |
```

### 7.6 M-A5 · `ruff check . --fix`（自动修 3 条）

预期消掉：`plan_lifecycle_live_check.py:63` `I001`、`w2_live_check.py:21` `F401`、`w2_live_check.py:31` `I001`。

> 先跑 `ruff check . --fix --diff` 看变更集，确认只碰白名单内文件再落盘。

### 7.7 M-A5 · 手工修剩余 3 条 ruff

| 位置 | 规则 | 修法 |
|---|---|---|
| `w2_live_check.py:147` | `F841` `wire` 未用 | 删赋值（保留右侧调用的副作用，改为裸调用） |
| `eval_comparison.py:81` | `E501` 102>100 | 折行 |
| `eval_longtask_mechanics.py:98` | `E501` 112>100 | 折行 |

### 7.8 M-A5 · `events.py:581` 删冗余 cast（**含 import 清理**）

```diff
-        return [cast(_T, e) for e in self.events if isinstance(e, event_type)]
+        return [e for e in self.events if isinstance(e, event_type)]
```

**必须同时删 import** —— 实跑确认 `cast` 在 `events.py` 中**仅此一处使用**：

```diff
-from typing import cast
```

> 只改 `return` 行会新增 `F401`，`ruff` 不为 0。评审 P1-4 实测。

### 7.9 M-A5 · `deadline.py:171` 显式标注集合类型

```diff
-        waiter = {task} if watcher is None else {task, watcher}
+        waiter: set[asyncio.Task[Any]] = {task} if watcher is None else {task, watcher}
```

> 根因：集合字面量从首元素 `task: Task[_T]` 推断出 `set[Task[_T]]`，
> `watcher: Task[bool]` 不兼容。需确认 `Any` 已 import。

### 7.10 M-A5 · `mcp.py:683` 收紧 `cls` 类型

```diff
-def _find_leaf(exc: BaseException | None, cls: type) -> BaseException | None:
+def _find_leaf(exc: BaseException | None, cls: type[BaseException]) -> BaseException | None:
```

> 根因：`cls: type` 是**裸 type**，`isinstance(exc, cls)` 无法窄化 → `exc` 变 `object`
> → `return exc` 与 `BaseException | None` 不符。
> 调用方实测只传 `OAuthFlowError` / `OAuthLoginTimeout`（`mcp.py:712/713/1309`），安全。

### 7.11 `docs/roadmap-milestones.md` 修正过期记录

- §M-A2：把"建议选 ②"改为"**已选 ①**"并写理由；把"两处消费方已坏"改为实测结论；标 ✅
- §M-A5：`ruff 4 errors` → 实测 **6**；`mypy 6 errors` → 实测 **3**；明细按实际重写；标 ✅

### 7.12 不做什么

- ❌ 不改 `open_all()` 签名（`-> None` 正确）
- ❌ 不改 `available()` / `build_mcp_catalog` / `MCPCallTool` / `world.py`
- ❌ 不改 `r7_cases.py`（实测不使用返回值）
- ❌ 不改 `ci.yml`（门禁已存在）
- ❌ 不逐行改写历史分析文档正文（只加标注）
- ❌ 不碰 `data-contracts.md:121`（已正确）

---

## 八、TDD / RIPR

### 8.1 红相（三条，都可机检）

```bash
# ① lint / type 门禁本来就是红的
ruff check .            # 6 errors
mypy ai_coding          # 3 errors

# ② 文档漂移（标注计数为 0）
grep -c "已被 M3 取代" docs/r1a-6-analysis.md    # 0
grep -c "已被 M3 取代" docs/r1-analysis.md       # 0
grep -c "已被 M3 取代" docs/architecture.md      # 0
grep -n "立即返回空列表" docs/features.md         # 命中

# ③ 空转用例：证明 s0_mcp_off 恒真
python -c "print((None or []) == [])"            # True —— 恒真
```

> ⚠️ **评审 P1-3**：初稿用的 grep 是 `立即返回 \[\]`，但实际文本是
> `` 立即返回 `[]` ``（带反引号），**匹配不到** → 只改 features.md 就报绿，
> 而 r1a-6 仍有 3 处。已改为上面的"标注计数"判据。

### 8.2 RIPR（缺一不可）

| 相 | 动作 | 判据（可机检） |
|---|---|---|
| **R** | 记录基线 | `ruff`=**6**、`mypy`=**3**、标注计数=**0/0/0**、`grep "立即返回空列表"`=**命中** |
| **I** | 7.6 `--fix` | `ruff` 6 → **3** |
| **I** | 7.7-7.10 | `ruff`=**0**、`mypy`=**0** |
| **I** | 7.1-7.5 | 标注计数=**4/2/1**；`grep -c "立即返回空列表"`=**0**；`grep -c "or \[\]" scripts/sample_tasks.py`=**0** |
| **P** | 全量 `pytest` | **0 failed**，skipped 数不增（**=6**） |
| **P** | `python scripts/sample_tasks.py --only mcp-off` | **PASS**，且输出含 `servers=0` |
| **P** | **突变 A**：`cls` 改回裸 `type` | `mypy` 重新报 `mcp.py:693` |
| **P** | **突变 B**：`cast` 加回 | `mypy` 重新报 `events.py:581` |
| **P** | **突变 C**：把 `sample_tasks` 改回 `(await open_all()) or []` | 该场景**仍 PASS**（证明原写法空转） |
| **R** | `git status --porcelain` 白名单 | 见 §8.3 |

> **突变 C 是本次的关键判据** —— 它直接证明"原用例恒真、新用例有实质"。

### 8.3 R 相白名单

```
M  ai_coding/core/events.py
M  ai_coding/infra/deadline.py
M  ai_coding/core/mcp.py
M  scripts/sample_tasks.py
M  scripts/eval_comparison.py
M  scripts/eval_longtask_mechanics.py
M  scripts/plan_lifecycle_live_check.py
M  scripts/w2_live_check.py
M  docs/features.md
M  docs/r1a-6-analysis.md
M  docs/r1-analysis.md
M  docs/architecture.md
M  docs/eval-03-live-features.md
M  docs/roadmap-milestones.md
?? docs/plan-m-a2-a5.md
```

即 **7 个代码文件 + 7 份文档 + 1 份方案**。

### 8.4 具体用例清单

| # | 用例 | 类型 | 期望 |
|---|---|---|---|
| U1 | `tests/test_mcp_lifecycle.py`（40 条） | 回归 | 全绿（证明工具面未受影响） |
| U2 | `tests/test_mcp_call_tool.py` | 回归 | 全绿 |
| U3 | `tests/test_mcp_catalog.py` | 回归 | 全绿 |
| U4 | `tests/integration/test_r17_*.py` | 回归 | 全绿 |
| U5 | `scripts/sample_tasks.py --only mcp-off` | 真实业务 | PASS + `servers=0` |

> 注：本方案**不新增测试文件** —— 因为没有新功能，只有"把空转断言改成真断言"
> 与"类型标注修正"。新增测试反而是冗余。

---

## 九、修改成功后的效果

| 观测点 | 改前 | 改后 |
|---|---|---|
| `ruff check .` | **6 errors** | **0** |
| `mypy ai_coding` | **3 errors** | **0** |
| CI（Lint / Type-check） | **红** | **绿** |
| 工具列表 | 存在（`available()`） | **存在，不变** |
| `open_all()` 行为 | `-> None` | **不变** |
| `s0_mcp_off` 的有效性 | **恒真（空转）** | **真实校验 `available() == []`** |
| 文档与代码一致性 | 3 份旧文档说返回列表 | 全部标注"已被 M3 取代" |

### 9.1 按此计划实施后的最终模样（我的推演）

- 打开 `docs/features.md:542`，看到的是"`-> None` + 从 `available()` 读"——与代码一致
- 打开 `r1a-6-analysis.md`，旧分析还在，但每个 `list[BaseTool]` 旁边都有醒目警示块
- 跑 `ruff check .` → `All checks passed!`
- 跑 `mypy ai_coding` → `Success: no issues found in 152 source files`
- 跑 `pytest` → `3812 passed, 6 skipped`
- 跑 `sample_tasks.py --only mcp-off` → `PASS enabled=False servers=0`
- `git log` 里每个里程碑一个提交，提交信息说明"为什么"而不只是"改了什么"

---

## 十、自我核验

1. **推演**：改完 7.1-7.11，`ruff`=0、`mypy`=0、全量 `pytest` 0 failed。
   依据：6 条 ruff 与 3 条 mypy 都有明确修法，且评审已在 worktree 副本实跑验证。
2. **反向检查**：删 `cast` 后 mypy 会不会换报错？
   `isinstance(e, event_type)` 中 `event_type: type[_T]` 是**带参数**的 type，
   mypy 能窄化到 `_T`。评审实跑确认无新报错。
3. **反向检查**：`set[asyncio.Task[Any]]` 会不会让 `asyncio.wait` 报错？
   `asyncio.wait` 接受 `Iterable[Task[Any]]`。不会。
4. **反向检查**：`cls: type[BaseException]` 会不会让递归调用报错？
   `sub` 来自 `getattr`（`Any`），`cls` 类型一致。不会。
5. **反向检查**：`sample_tasks` 改用 `available()` 后 `s0` 还成立吗？
   MCP 关 → `open_all()` 早退 → 无 owner → `available()` 返回 `[]` → 成立。需实跑确认。
6. **反向检查**：工具列表会不会因此丢失？
   **不会** —— §1 已证：`available()` / `build_mcp_catalog` / `MCPCallTool` / `world.py`
   全部不动，且 40 条相关测试作回归网。
7. **反向检查**：有没有"为了修 lint 而改坏行为"？
   9 条全是标注/import/未用变量/行长。`F841` 那条要确认右侧调用是否有副作用。

---

## 十一、边界声明

| 项 | 归属 |
|---|---|
| **本方案改** | 7 个代码文件（其中 3 个只改 1-2 行标注）+ 7 份文档 |
| **不改** | `open_all()` / `available()` / `build_mcp_catalog` / `MCPCallTool` / `world.py` / `r7_cases.py` / `ci.yml` / `data-contracts.md` |
| **不在本方案** | M-A4 遗留 N-1（`gitcmd` stderr 合并）、N-2（flaky 用例） |
| **不在本方案** | M-A6（memory_cases case7/case11） |
| **新发现的独立缺陷** | `scripts/mcp_echo_server.py` 在当前 mcp 版本下 `ModuleNotFoundError: mcp.server.mcpserver` —— 与 M-A2 无关，建议单独立项（见 §十二.3） |

---

## 十二、评审记录

子智能体用 `git worktree` 建临时副本，**实跑全部 9 条修法**后评审。

### 12.1 4 条阻塞项（已全部修订）

| # | 问题 | 处理 |
|---|---|---|
| **P1-1** | 漏了 `r1-analysis.md:818/830` 同类漂移 | ✅ §7.4 覆盖点 4 → **7** |
| **P1-2** | 漏了 `eval-03-live-features.md:120`（把 `or []` 记为"最小修复"） | ✅ §7.5 |
| **P1-3** | **RIPR grep 判据假绿**（反引号不匹配） | ✅ §8.1 改"标注计数" |
| **P1-4** | 步骤 7 diff 缺 `cast` import 删除 | ✅ §7.8 补 import diff |

### 12.2 评审实跑确认的通过项

- 基线 ruff 6 / mypy 3 / pytest 0 failed 6 skipped —— 实跑吻合
- `open_all` 非阻塞判断成立（代码 + 3 份文档交叉印证）
- ②（返回工具列表）不可能 —— 论证成立
- 三个消费方"都没坏" —— 实跑确认，推翻 roadmap 的"两处已坏"
- 9 条修法在副本内**全部清零**（`ruff 0` / `mypy 0` / `pytest 0 failed` / `sample_tasks` PASS）
- 突变 A/B 各自精确复现原报错
- CI 门禁已存在，无需改 CI
- `data-contracts.md:121` 已正确，不需改

### 12.3 本轮新增发现（我实跑时撞到）

`scripts/mcp_echo_server.py:15` → `from mcp.server.mcpserver import MCPServer` →
**`ModuleNotFoundError: No module named 'mcp.server.mcpserver'`**

当前 mcp 版本（M-A1 收紧为 `>=1.24,<2`）没有这个模块。影响：
- `tests/test_mcp_end_to_end.py` 与 `tests/test_mcp_stdio.py` 由 `AI_CODING_MCP_STDIO=1` 门控，
  平时 skip（所以没暴露）
- **CI 里那两个 step 会红**（`ci.yml:45/51` 设了该变量）

→ 这是**独立缺陷**，不在 M-A2/M-A5 范围（M-A5 只清 `ruff`/`mypy`，不含运行时）。
建议记入 roadmap §十三 作为 N-3。**本次不修**。

---

## 十三、拍板结论（已按推荐项执行）

1. **M-A2 选 ① 改文档** —— ✅ 已执行。② 物理上不可能（非阻塞 ⇒ 返回时工具不存在）。
2. `sample_tasks.py` 改用 `available()` 后，`s0` 的语义从"工具数 0"变成"**server 数 0**" —— ✅ 接受。
   理由：原写法是**恒真空转**（突变 C 实测：3 个工具在场上仍 PASS），改后才有实质。
3. 历史分析文档只加标注不改正文 —— ✅ 已执行（7 处标注）。
4. `mcp_echo_server.py` 的 `mcp.server.mcpserver` 缺陷 —— ✅ 只记录不修，已立 **N-3**（见 roadmap §十三）。

---

## 十四、实施记录（2026-10-24）

### 14.1 对方案的两处修正（实施中发现）

| # | 方案原文 | 实测结论 | 处理 |
|---|---|---|---|
| **C-1** | §7.7 说 `w2_live_check.py:145` "删赋值、保留裸调用" | `result["entries"]` 来自第 95 行 `json.loads`（**已是可序列化对象**），`json.dumps` 结果被丢弃 → **纯无操作** | **整行删除**（保留裸调用就是"假实现"） |
| **C-2** | §7.11 说 `ruff 4 errors` / `mypy 6 errors` | 实测 **6 / 3**（M-A1 已修掉 3 条 mypy） | roadmap 已按实测改写 |

### 14.2 提交

| commit | 内容 |
|---|---|
| `53e6eb2` | 方案重写（含工具面数据流实证与空转用例分析） |
| `e709d4f` | M-A5：ruff 6→0、mypy 3→0 |
| 本提交 | M-A2：文档对齐 + `sample_tasks` 换数据源 + roadmap 收口 |

### 14.3 判据复核（全部实跑）

| 判据 | 期望 | 实测 |
|---|---|---|
| `ruff check .` | 0 | **All checks passed!** |
| `mypy ai_coding` | 0 | **Success: no issues found in 152 source files** |
| 标注计数 `r1a-6` / `r1` / `architecture` | 4 / 2 / 1 | **4 / 2 / 1** |
| `grep -c "立即返回空列表" docs/features.md` | 0 | **0** |
| `grep -c "or \[\]" scripts/sample_tasks.py` | 0 | **0** |
| `sample_tasks.py --only mcp-off` | PASS + `servers=0` | **PASS enabled=False servers=0** |
| 全量 `pytest` | 0 failed，skipped ≤ 6 | **3812 collected = 3806 passed + 6 skipped / 0 failed** |
| **突变 A**（`cls` 改回裸 `type`） | mypy 复现 `mcp.py:693` | **复现** |
| **突变 B**（`cast` 加回） | mypy 复现 `events.py:581` | **复现** |
| **突变 C**（改回 `(await open_all()) or []`） | 证明原写法空转 | **证明**：3 工具在场上仍 PASS |

### 14.4 突变 C 的原始输出（本次关键证据）

```
MCP enabled=True, 1 server declared
  open_all() returned          : None
  available() after connect    : [('fs', 3)]
  OLD assertion  tools == []   : True   <- 空转，完全无视这 3 个工具
  NEW assertion  rows  == []   : False
```

### 14.5 代码评审（子智能体，`git worktree` 独立实跑）

评审结论：**两项提交均通过，无阻塞项**。评审在 `/tmp/review-ma2a5` 副本内独立复现了
ruff=0、mypy=0、突变 A/B/C、`sample_tasks --only mcp-off`，并逐条核对 9 条改动确为
"标注/import/行长/纯无操作"，确认工具面 3 个消费点（`world.py:195/204`、`mcp_call.py:92`）
与 `r7_cases.py` 都不用 `open_all()` 的返回值。

评审提出 2 条文档层问题，处理如下：

| # | 问题 | 处理 |
|---|---|---|
| **P2** | 提交信息写"3806 passed"，评审误读为与 roadmap 的"3812 tests"矛盾 | **口径澄清**：`3812 collected = 3806 passed + 6 skipped`（实测 `--collect-only` = 3812）。两者一致，非缺陷 |
| **P3** | 两份 superpowers 计划稿仍有 `for tool in await mcp_manager.open_all():` 未标注 | ✅ **已补标注**（`2026-09-28-textual-ui.md:1633`、`2026-09-28-terminal-interaction-redesign.md:1526`），漂移点归零 |

> 漂移收敛后，`grep -rn "for tool in await.*open_all()"` 的 3 处命中全部位于
> **已加警示块的代码块内**（`r1a-6-analysis.md:185` 的警示块在其后第 9 行）。
