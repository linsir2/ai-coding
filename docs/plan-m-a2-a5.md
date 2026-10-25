# M-A2 + M-A5 实施方案 · 契约漂移收敛 + lint/type 门禁清零

> 状态：**待评审**
> 关联：[roadmap-milestones.md](roadmap-milestones.md) §1 M-A2 / M-A5
> 依赖：M-A1（`httpx2` 已修）、M-A3（`restore_ok` 已修）
> 前置实测：[eval-01](eval-01-offline-baseline.md)

---

## 一、要实现的内容（两句话）

**M-A2**：`open_all()` 的返回值在文档里有两种说法（`None` vs `[]`/`list[BaseTool]`）——
统一为**代码的真实契约**，并清掉消费方的兜底补丁。

**M-A5**：`ruff check .` 6 条、`mypy ai_coding` 3 条 → **全零**。
这不是"清理卫生"——CI 已经把这两步作为硬门禁（[ci.yml:35-39](file:///workspace/ai-coding/.github/workflows/ci.yml#L35-L39)），
**所以 CI 现在就是红的**。

---

## 二、M-A2 调研结论

### 2.1 代码的真实契约

[mcp.py:1097-1117](file:///workspace/ai-coding/ai_coding/core/mcp.py#L1097-L1117)：

```python
async def open_all(self) -> None:
    """Start a supervisor per connectable server; do not wait for any of them.
    ...
    """
    if not self._config.enabled or self._supervisors or self._stop.is_set():
        return
    ...
        await self.start_supervisor(server)
```

**`-> None`，且是"起 supervisor 就返回"，不等任何连接。**

### 2.2 ⚠️ roadmap 的建议 ② 在物理上不可能

roadmap M-A2 写"**建议选 ②**：让 `open_all()` 返回工具列表（对齐 R1a-6 原始设计）"。

**这条建议基于过期设计，必须否决**：

- [test_mcp_lifecycle.py:5](file:///workspace/ai-coding/tests/test_mcp_lifecycle.py#L5)：
  `open_all` only starts the supervisors and returns: a row is `connecting`
- [data-contracts.md:121](file:///workspace/ai-coding/docs/data-contracts.md#L121)：
  `open_all` **非阻塞**——只给每个可连 server 起一个 supervisor 任务即返回
- [features.md:536](file:///workspace/ai-coding/docs/features.md#L536)：启动不阻塞，TUI 秒起，行显示 `Connecting …`

**返回时服务器还没连上，工具根本不存在** —— 返回工具列表只能是 `[]` 或撒谎。
R1a-6 那份文档是**对旧设计的分析**，不是 spec；M3 的非阻塞改造已经取代它。

→ **只能选 ①：改文档承认 `None`。**

### 2.3 消费方实测：**一个都没坏**

roadmap 写"两处消费方已坏：`sample_tasks.py:86`、`r7_cases.py:353/424`"。**实测全错**：

| 位置 | 实际写法 | 状态 |
|---|---|---|
| [sample_tasks.py:88](file:///workspace/ai-coding/scripts/sample_tasks.py#L88) | `tools = (await manager.open_all()) or []` | ✅ 有 `or []` 兜底，能跑（但是**补丁**，掩盖契约） |
| [r7_cases.py:353](file:///workspace/ai-coding/scripts/r7_cases.py#L353) | `await manager.open_all()` 后接 `manager.servers()` | ✅ **根本没用返回值** |
| [r7_cases.py:424](file:///workspace/ai-coding/scripts/r7_cases.py#L424) | `await mcp.open_all()` 后接 `mcp.available()` | ✅ **根本没用返回值** |
| [world.py:183](file:///workspace/ai-coding/ai_coding/server/world.py#L183) | `await mcp_manager.open_all()` | ✅ 不用返回值 |

→ **没有消费者依赖返回值**。M-A2 是**纯文档问题** + 一处可以清掉的补丁。

### 2.4 文档漂移点（逐条，含评审 P1-1/P2-3 补充）

| 文件:行 | 现文 | 判定 |
|---|---|---|
| [features.md:542](file:///workspace/ai-coding/docs/features.md#L542) | "`mcp.enabled=false`（默认）时 `open_all()` **立即返回空列表**" | ❌ **错**（返回 `None`） |
| [features.md:516](file:///workspace/ai-coding/docs/features.md#L516) | "`open_all()` 连接启用的 server、…" | 🟡 R1a-1 时代描述，需标"历史" |
| [data-contracts.md:121](file:///workspace/ai-coding/docs/data-contracts.md#L121) | "`open_all` 非阻塞——只给每个可连 server 起一个 supervisor 任务即返回" | ✅ **已正确**，未提返回值 |
| [r1a-6-analysis.md:156](file:///workspace/ai-coding/docs/r1a-6-analysis.md#L156) | `async def open_all(self) -> list[BaseTool]` | ❌ 旧设计 |
| [r1a-6-analysis.md:165](file:///workspace/ai-coding/docs/r1a-6-analysis.md#L165) | "`enabled` 为假直接返回 `[]`" | ❌ 旧设计 |
| [r1a-6-analysis.md:177](file:///workspace/ai-coding/docs/r1a-6-analysis.md#L177) | `for tool in await manager.open_all():` | ❌ 旧设计（该代码不存在于仓库） |
| [r1a-6-analysis.md:251](file:///workspace/ai-coding/docs/r1a-6-analysis.md#L251) | "`open_all()` 立即返回 `[]`" | ❌ 旧设计 |
| **[r1-analysis.md:818](file:///workspace/ai-coding/docs/r1-analysis.md#L818)** | `async def open_all(self) -> list[BaseTool]` | ❌ **评审 P1-1 补漏**，与 r1a-6 同性质 |
| **[r1-analysis.md:830](file:///workspace/ai-coding/docs/r1-analysis.md#L830)** | "把返回工具注册进注册表" | ❌ **评审 P1-1 补漏** |
| **[architecture.md:87](file:///workspace/ai-coding/docs/architecture.md#L87)** | `open_all() ─→ 远端工具注册进同一个 registry` | 🟡 **评审 P2-3**：与 M2「工具不再逐个注册」矛盾，需标历史 |
| [eval-03-live-features.md:120](file:///workspace/ai-coding/docs/eval-03-live-features.md#L120) | 把 `or []` 记为"最小修复" | ❌ **评审 P1-2**：步骤 3 删掉补丁后此表失真，必须同步 |

---

## 三、M-A5 调研结论

### 3.1 实测基线（roadmap 记的 4/6 已过期）

```
ruff check .          6 errors
mypy ai_coding        3 errors in 3 files (checked 152 source files)
```

### 3.2 ruff 6 条逐条

| # | 位置 | 规则 | 归属 |
|---|---|---|---|
| 1 | [eval_comparison.py:81](file:///workspace/ai-coding/scripts/eval_comparison.py#L81) | `E501` 行长 102 > 100 | **我的**（评估期新增） |
| 2 | [eval_longtask_mechanics.py:98](file:///workspace/ai-coding/scripts/eval_longtask_mechanics.py#L98) | `E501` 行长 112 > 100 | **我的** |
| 3 | [plan_lifecycle_live_check.py:63](file:///workspace/ai-coding/scripts/plan_lifecycle_live_check.py#L63) | `I001` import 未排序 | 既有 |
| 4 | [w2_live_check.py:21](file:///workspace/ai-coding/scripts/w2_live_check.py#L21) | `F401` `shutil` 未使用 | 既有 |
| 5 | [w2_live_check.py:31](file:///workspace/ai-coding/scripts/w2_live_check.py#L31) | `I001` import 未排序 | 既有 |
| 6 | [w2_live_check.py:147](file:///workspace/ai-coding/scripts/w2_live_check.py#L147) | `F841` `wire` 赋值未使用 | 既有 |

`ruff check . --fix` 能自动修 #3/#4/#5（`I001`×2 + `F401`），#6 需手工删赋值，#1/#2 需手工折行。

### 3.3 mypy 3 条逐条（含根因）

| # | 位置 | 报错 | 根因 | 修法 |
|---|---|---|---|---|
| 1 | [events.py:581](file:///workspace/ai-coding/ai_coding/core/events.py#L581) | `Redundant cast to "_T"` | `[cast(_T, e) for e in self.events if isinstance(e, event_type)]` —— `isinstance(e, event_type)` **已经**把 `e` 窄化为 `_T`，`cast` 多余 | 删掉 `cast`：`[e for e in self.events if isinstance(e, event_type)]` |
| 2 | [deadline.py:171](file:///workspace/ai-coding/ai_coding/infra/deadline.py#L171) | `Argument 2 to <set> has incompatible type "Task[bool]"; expected "Task[_T]"` | `waiter = {task} if watcher is None else {task, watcher}` —— 集合字面量从首元素 `task: Task[_T]` 推断出 `set[Task[_T]]`，`watcher: Task[bool]` 不兼容 | 显式标注：`waiter: set[asyncio.Task[Any]]` |
| 3 | [mcp.py:693](file:///workspace/ai-coding/ai_coding/core/mcp.py#L693) | `Incompatible return value type (got "object", expected "BaseException \| None")` | `_find_leaf(exc, cls: type)` 里 `isinstance(exc, cls)` —— `cls` 是**裸 `type`**，mypy 无法窄化，把 `exc` 窄成 `object` | 签名改 `cls: type[BaseException]`（调用方只传 `OAuthFlowError` / `OAuthLoginTimeout`，实测见 §3.4） |

### 3.4 `_find_leaf` 的调用方核验

```
mcp.py:695    _find_leaf(sub, cls)                      # 递归，同一 cls
mcp.py:712    _find_leaf(exc, OAuthFlowError)
mcp.py:713    _find_leaf(exc, OAuthLoginTimeout)
mcp.py:1309   _find_leaf(owner.error, OAuthLoginTimeout)
```

→ **全部是异常类**，`type[BaseException]` 安全。

### 3.5 CI 现状：已经是红的

[ci.yml:35-39](file:///workspace/ai-coding/.github/workflows/ci.yml#L35-L39)：

```yaml
      - name: Lint
        run: ruff check .
      - name: Type-check
        run: mypy ai_coding
```

→ **roadmap 的验收③"CI 增加这两步为硬门禁"已经满足**，不需要改 CI。
但正因为已是硬门禁，**6+3 条错误意味着 CI 一直红** —— 本里程碑的真实价值是**让 CI 重新变绿**。

---

## 四、执行范围（逐文件、逐步）

### M-A2

#### 步骤 1 · `docs/features.md:542`

```diff
-`mcp.enabled=false`（默认）时 `open_all()` 立即返回空列表，不连接任何服务器。
+`mcp.enabled=false`（默认）时 `open_all()` 立即返回（`-> None`，不连接任何服务器）；
+可调用工具一律从 `MCPManager.available()` 读，`open_all` 从不返回工具列表。
```

#### 步骤 2 · `docs/r1a-6-analysis.md` + `docs/r1-analysis.md` + `docs/architecture.md` —— 标注旧设计已被 M3 取代

在这三份**历史分析文档**的**每一个**漂移点就近插入同一警示块（评审 P2-1：不能只插一处，
读者跳读到 §251 时仍会看到无警示的 `[]` 断言）：

```markdown
> ⚠️ **已被 M3 取代**：M3 起 `open_all()` 改为非阻塞（只起 supervisor 即返回），
> 签名是 `async def open_all(self) -> None`，**不返回工具列表**。本节是旧设计分析，
> 保留作为历史记录。工具表面请读 `MCPManager.available()`。
```

覆盖点（共 6 处）：

| 文件 | 行 | 插入位置 |
|---|---|---|
| `r1a-6-analysis.md` | 156 | 签名代码块之后 |
| `r1a-6-analysis.md` | 165 | "返回 `[]`" 那句之后 |
| `r1a-6-analysis.md` | 177 | `for tool in await ...` 代码块之后 |
| `r1a-6-analysis.md` | 251 | "核验③" 段内 |
| `r1-analysis.md` | 818 | 类签名代码块之后 |
| `r1-analysis.md` | 830 | "把返回工具注册进注册表" 那句之后 |
| `architecture.md` | 87 | 架构图下方 |

> **为什么不逐行改正文**：这是**历史分析文档**，改写正文会让它假装自己当时就那么想。
> 加"已被取代"标注既保留历史，又不误导读者。

#### 步骤 2b · `docs/features.md:516` —— 标注历史描述

在"`open_all()` 连接启用的 server、…"句末加：（**M3 起非阻塞，只起 supervisor**）。

#### 步骤 2c · `docs/eval-03-live-features.md:120` —— 同步补丁的移除（评审 P1-2）

该表把 `tools = (await manager.open_all()) or []` 记为"最小修复"。步骤 3 删掉它后，
此表会与代码矛盾。改为：

```diff
-| `sample_tasks` 的 `mcp-off` 抛 `TypeError: object of type 'NoneType' has no len()` | `MCPManager.open_all()` 现返回 `None`（工具面改由 `available()` 提供），而脚本与 `docs/features.md:542` 仍假设返回 `[]` | `tools = (await manager.open_all()) or []`（`scripts/sample_tasks.py`） | `--only mcp-off` → **PASS**，全量 **8/8** |
+| `sample_tasks` 的 `mcp-off` 抛 `TypeError: object of type 'NoneType' has no len()` | `MCPManager.open_all()` 现返回 `None`（工具面改由 `available()` 提供），而脚本与 `docs/features.md:542` 仍假设返回 `[]` | 评估期打了 `or []` 补丁；**M-A2 已改为读 `manager.available()`**（补丁移除） | `--only mcp-off` → **PASS** |

#### 步骤 3 · `scripts/sample_tasks.py:88` —— 清掉 `or []` 补丁

```diff
-        # eval-03 minimal fix: open_all() returns None (the tool surface now
-        # comes from manager.available()), but this scenario asserts on a count.
-        tools = (await manager.open_all()) or []
+        await manager.open_all()
+        # The tool surface comes from available(), never from open_all's return
+        # value -- open_all is non-blocking and returns before any server is up.
+        tools = manager.available()
     finally:
         await manager.aclose()
-    return tools == [], f"enabled={app_cfg.mcp.enabled} tools={len(tools)}"
+    return tools == [], f"enabled={app_cfg.mcp.enabled} servers={len(tools)}"
```

> 这是**唯一真正需要动的消费方** —— 它原来的 `or []` 是评估期为了让断言过而打的补丁，
> 掩盖了契约。改用 `available()` 后断言的是真实契约（MCP 关 → 没有任何可用 server）。
>
> ⚠️ **评审 P2-2：语义迁移**。`available()` 返回 `list[tuple[str, list[handle]]]`，
> 所以 `len(tools)` 从"**工具数**"变成"**server 数**"。文案已随之从 `tools=` 改为 `servers=`，
> 并把这一迁移写进验收判据（§6.2 I 相）。

#### 步骤 4 · `docs/roadmap-milestones.md` §M-A2 —— 修正错误建议

把"决策（二选一，**建议选 ②**）"改为"**已选 ①**"并写明理由（非阻塞设计下 ② 不可能），
把"两处消费方已坏"改为实测结论（一个都没坏），标 ✅。

### M-A5

#### 步骤 5 · `ruff check . --fix`（自动修 3 条）

```bash
ruff check . --fix
```

预期消掉 #3（`plan_lifecycle_live_check` `I001`）、#4（`shutil` `F401`）、#5（`w2_live_check` `I001`）。

#### 步骤 6 · 手工修剩余 3 条

- #6 `w2_live_check.py:147` `F841`：`wire = ...` 未使用 → 删掉赋值（保留右侧调用的副作用，若有必要则改为裸调用）
- #1 `eval_comparison.py:81` `E501`：折行
- #2 `eval_longtask_mechanics.py:98` `E501`：折行

#### 步骤 7 · `events.py:581` 删冗余 cast（**含 import 清理**，评审 P1-4）

```diff
-        return [cast(_T, e) for e in self.events if isinstance(e, event_type)]
+        return [e for e in self.events if isinstance(e, event_type)]
```

**必须同时删 `cast` 的 import** —— 实跑确认 `cast` 在 `events.py` 中**仅此一处使用**，
只改 `return` 行会新增 `F401`，`ruff` 不为 0：

```diff
-from typing import cast
```

> 若 `typing` 还有其他名字在用，改为只删 `cast` 这一项（保持字母序）。

#### 步骤 8 · `deadline.py:171` 显式标注集合类型

```diff
-        waiter = {task} if watcher is None else {task, watcher}
+        waiter: set[asyncio.Task[Any]] = {task} if watcher is None else {task, watcher}
```

> 需确认 `Any` 已 import。

#### 步骤 9 · `mcp.py:683` 收紧 `cls` 类型

```diff
-def _find_leaf(exc: BaseException | None, cls: type) -> BaseException | None:
+def _find_leaf(exc: BaseException | None, cls: type[BaseException]) -> BaseException | None:
```

#### 步骤 10 · `docs/roadmap-milestones.md` §M-A5 —— 修正过期数字

`ruff 4 errors` → 实测 6；`mypy 6 errors` → 实测 3；明细表按 §3.2/§3.3 重写；标 ✅。

### 步骤 11 · 不做什么

- ❌ 不改 `open_all()` 的签名（`-> None` 是正确的）
- ❌ 不改 `r7_cases.py` / `world.py`（实测没坏）
- ❌ 不改 `ci.yml`（门禁已存在）
- ❌ 不逐行改写 `r1a-6-analysis.md` 正文（只加"已被取代"标注）
- ❌ 不碰 `data-contracts.md:121`（已正确）

---

## 五、数据流影响

### 5.1 M-A2

```
改前：docs 说 open_all() 返回 []/list[BaseTool]
      ↓
      读者/未来贡献者据此写 `for tool in await open_all():` → 运行时 TypeError
      （sample_tasks.py 就是被这么坑过，才打了 `or []` 补丁）

改后：docs 与代码一致（-> None），工具表面统一指向 available()
      ↓
      消费方按真实契约写；sample_tasks.py 的补丁被清掉，断言变成真实契约
```

**产品代码零改动**（`open_all()` 一行不动）。

### 5.2 M-A5

```
改前：ruff 6 + mypy 3 → CI 的 Lint / Type-check 两步红 → 整个 CI 红
改后：ruff 0 + mypy 0 → CI 绿
```

**运行时行为零改动**（全是类型标注/import 顺序/未用变量）。

---

## 六、TDD / RIPR

### 6.1 红相

M-A2 与 M-A5 都**不是"加功能"**，红相是**现状本身就是红的**：

```
$ ruff check .        → 6 errors        ← 红
$ mypy ai_coding      → 3 errors        ← 红
```

> ⚠️ **必须用 `.venv` 里的工具**（`/workspace/ai-coding/.venv/bin/`）。
> 用 pyenv 全局 python 会得到 **41 条 `import-not-found` 噪声**（依赖未装），基线不可复现。

M-A2 的"红"是文档与代码不一致 —— 用一条**可机检的判据**表达。

> ⚠️ **评审 P1-3：初稿的 grep 模式是假绿**。
> 原写 `grep "立即返回 \[\]"`，但实际文本是 `` 立即返回 `[]` ``（带反引号），**匹配不到** ——
> 只改 `features.md` 后该判据就报绿，而 `r1a-6-analysis.md` 仍有 3 处 `[]` 断言。
>
> **改用"带标注计数"判据**（能抓真实漂移）：

```bash
# 每个漂移点附近 5 行内必须出现"已被 M3 取代"
for f in docs/r1a-6-analysis.md docs/r1-analysis.md docs/architecture.md; do
  grep -c "已被 M3 取代" "$f"
done
# 期望：r1a-6 = 4，r1-analysis = 2，architecture = 1

# features.md:542 必须不再出现"返回空列表"
! grep -q "open_all()\` 立即返回空列表" docs/features.md
```

### 6.2 RIPR

| 相 | 动作 | 判据（可机检） |
|---|---|---|
| **R** | 记录基线 | `ruff` = **6**；`mypy` = **3**；`grep -c "已被 M3 取代"` = **0/0/0** |
| **I** | 步骤 5（`--fix`） | `ruff` 从 6 → **3** |
| **I** | 步骤 6-9 | `ruff` = **0**；`mypy` = **0** |
| **I** | 步骤 1-3 | 标注计数 = **4/2/1**；`features.md` 无"返回空列表"；`grep -c "or \[\]" scripts/sample_tasks.py` = **0** |
| **P** | 全量 `pytest` | **0 failed**，skipped 数不增（=6） |
| **P** | `python scripts/sample_tasks.py --only mcp-off` | **PASS** |
| **P** | **突变 A**：`_find_leaf` 的 `cls` 改回裸 `type` | `mypy` **重新报 `mcp.py:693`** |
| **P** | **突变 B**：`events.py` 把 `cast` 加回 | `mypy` **重新报 `events.py:581`** |
| **R** | `git status --porcelain` 白名单 | 见 §6.3 |

### 6.3 R 相白名单（评审 P1-1/P1-2 已补）

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

> ⚠️ `ai_coding/core/mcp.py` 只改 `_find_leaf` 的签名一行 —— 与 M-A1 改的 remote/OAuth 段不重叠。

---

## 七、修改成功后的效果

| 观测点 | 改前 | 改后 |
|---|---|---|
| `ruff check .` | **6 errors** | **0** |
| `mypy ai_coding` | **3 errors** | **0** |
| CI（Lint / Type-check 两步） | **红** | **绿** |
| `docs/features.md:542` | "立即返回空列表" | "立即返回（`-> None`），工具从 `available()` 读" |
| `docs/r1a-6-analysis.md` | 声称 `list[BaseTool]` | 加"已被 M3 取代"标注 |
| `sample_tasks.py` 的 `or []` | 有（补丁） | **无**（按真实契约写） |
| `open_all()` 行为 | `-> None` | **不变** |
| 运行时行为 | — | **零改动** |

---

## 八、自我核验

1. **推演**：改完步骤 1-10，`ruff` = 0、`mypy` = 0、全量 `pytest` 0 failed。
   依据：ruff 6 条全部有明确修法；mypy 3 条都是标注层面，不改运行时。
2. **反向检查**：`events.py` 删 `cast` 后 mypy 会不会改报别的？
   `isinstance(e, event_type)` 中 `event_type: type[_T]` 是**带参数**的 `type`，
   mypy 能窄化到 `_T`。若仍报错，则说明该版本 mypy 不支持，需回退到 `cast` 并改
   `# type: ignore[redundant-cast]` —— **这是备选，不是首选**。
3. **反向检查**：`deadline.py` 标注 `set[asyncio.Task[Any]]` 会不会让 `asyncio.wait` 报错？
   `asyncio.wait` 接受 `Iterable[Task[Any]]`，不会。
4. **反向检查**：`mcp.py` 的 `cls: type[BaseException]` 会不会让 `_find_leaf(sub, cls)`
   的递归调用报错？`sub` 是 `Any`（来自 `getattr`），`cls` 类型一致，不会。
5. **反向检查**：`sample_tasks.py` 改用 `available()` 后，`s0_mcp_off` 还成立吗？
   MCP 关闭 → `open_all()` 直接 return → 没有任何 supervisor → `available()` 返回 `[]`。
   **成立**。需实跑确认。
6. **反向检查**：`ruff --fix` 会不会误改别的文件？
   先跑 `--diff` 看变更集，确认只碰白名单内文件再落盘。

---

## 九、边界声明

| 项 | 归属 |
|---|---|
| **本方案改** | 3 份文档 + 4 个 `scripts/` + 3 个 `ai_coding/` 文件（各 1-2 行） |
| **不改** | `open_all()` 签名、`r7_cases.py`、`world.py`、`ci.yml`、`data-contracts.md:121` |
| **不在本方案** | M-A4 遗留的 N-1（`gitcmd` stderr 合并）、N-2（flaky 用例） |
| **不在本方案** | M-A6（memory_cases case7/case11 文档与实测不一致） |

---

## 十、待评审的关键问题

1. **M-A2 选 ① 还是 ②** —— 本方案选 ①（改文档），并论证 ② 在非阻塞设计下不可能。
   若评审认为应改设计（让 `open_all` 等连接完成再返回），那会**推翻 M3 的非阻塞改造**，
   属于另一个里程碑。
2. `r1a-6-analysis.md` 的处理 —— 本方案**加标注而非改正文**（保留历史）。
3. `sample_tasks.py` 改用 `available()` —— 会改变该脚本 `s0` 场景的语义
   （从"工具数为 0"变成"可用 server 数为 0"）。是否可接受？
4. `events.py` 删 `cast` 若 mypy 仍报错，是否接受 `# type: ignore` 兜底。

---

## 十一、评审记录

子智能体用 `git worktree` 建临时副本，**实跑全部 9 条修法** + 3 处文档/消费方改动后评审。

### 11.1 发现与处置

| # | 严重度 | 问题 | 处理 |
|---|---|---|---|
| **P1-1** | 🔴 | **漏了 `r1-analysis.md:818/830`** —— 与 r1a-6 同性质（声称 `list[BaseTool]` + "把返回工具注册进注册表"） | ✅ §2.4 补入；步骤 2 覆盖点从 4 处扩到 7 处；白名单补 |
| **P1-2** | 🔴 | **`eval-03-live-features.md:120` 白名单漏项** —— 它把 `or []` 记为"最小修复"，步骤 3 删掉补丁后此表与代码矛盾 | ✅ 新增步骤 2c 同步该表；白名单补 |
| **P1-3** | 🔴 | **RIPR grep 判据假绿** —— 模式 `立即返回 \[\]` 匹配不到实际文本 `` 立即返回 `[]` ``（带反引号）；只改 features.md 就报绿，而 r1a-6 仍有 3 处 | ✅ §6.1 改为"标注计数"判据（`grep -c "已被 M3 取代"` = 4/2/1）+ `! grep "返回空列表"` |
| **P1-4** | 🔴 | **步骤 7 diff 缺 `cast` import 删除** —— 照字面执行会新增 `F401`，`ruff` ≠ 0 | ✅ 步骤 7 补 import 删除 diff，并注明"实跑确认 `cast` 仅此一处" |
| **P2-1** | 🟠 | 步骤 2 警示块只插一处，§251 跳读时无警示 | ✅ 改为"每个漂移点就近插入"，共 7 处 |
| **P2-2** | 🟠 | 步骤 3 的语义迁移（工具数→server 数）未进验收判据 | ✅ 步骤 3 加注 + §6.2 I 相写入 |
| **P2-3** | 🟠 | `architecture.md:87`、`features.md:516` 未提及 | ✅ §2.4 补入；步骤 2/2b 覆盖 |

### 11.2 评审实跑确认的通过项

| 项 | 结论 |
|---|---|
| 基线数字（ruff 6 / mypy 3 / pytest 0 failed 6 skipped） | ✅ 实跑吻合 |
| `open_all` 非阻塞判断 | ✅ 成立（代码 + 3 份文档交叉印证） |
| ②（返回工具列表）不可能 | ✅ 论证成立，选 ① 正确 |
| 三个消费方"都没坏" | ✅ 实跑确认，推翻 roadmap 的"两处已坏" |
| 9 条修法全部清零 | ✅ 副本内实跑：`ruff 0` / `mypy 0` / `pytest 0 failed` / `sample_tasks --only mcp-off` PASS |
| 突变 A/B | ✅ 各自精确复现原报错 |
| CI 门禁已存在 | ✅ `ci.yml:35-39`，无需改 CI |
| `data-contracts.md:121` 已正确 | ✅ 不需改 |
| 步骤 5 `--fix` 只碰白名单内 3 文件 | ✅ 未破坏 `sys.path.insert` 时序 |
| §8 的 4 条反向检查 | ✅ 实跑全部成立，无需 `# type: ignore` |

### 11.3 评审的环境提醒（已采纳）

> 若用 pyenv 全局 python 跑 `mypy ai_coding`，会得到 **41 条 `import-not-found` 噪声**
> （openai/mcp/pydantic/typer/anyio/httpx 未装），与 3 条基线完全对不上。
> **必须用 `.venv`** —— 已写入 §6.1。
