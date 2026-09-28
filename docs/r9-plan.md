# R9 Implementation Plan（`depends_on` 硬门禁 / 观测三件）

> **For agentic workers:** REQUIRED SUB-SKILL：本项目按用户既有要求走 **Inline Execution**
> （「不准派子任务，自己实现」）。逐步执行时用 `- [ ]` 勾选跟踪；每个 Task 结束跑一次质量门禁并单独提交。

**Goal:** 让 `depends_on` 从装饰性字段变成一个有不变量、有门禁、有出路的约束；把已经攒好的轨迹数据接上
三个只读出口（跨会话总览、静态 HTML、OTLP/JSON）。

**Architecture:** R9a 在**领域层加纯查询**（`Blockers` / `blockers()` / `dependency_invariant()`），
把**策略留在工具层**（`todo_write` 的 add 校验与状态门禁），配置一个开关；R9b–d 在既有
`TraceStore` 上加一个「枚举会话」的读路径，再用两个纯模块把同一份数据渲染成表 / HTML / OTLP-JSON。
**轨迹写入格式与 `TaskState.update()` 的语义一个字都不改。**

**Tech Stack:** Python 3.13（mypy 按 3.10 下限）、dataclasses、`html.escape`、`hashlib`、
typer/rich、pytest（`-o addopts=""`）、ruff、mypy。**不新增任何依赖。**

**Spec:** `docs/r9-analysis.md`（提交 `c142d57`）。判据、实测数字与决策点都在那份文档里；
本计划只写「怎么做、怎么测」，不重复论证。

## Global Constraints

用户已全部拍板（「都同意你的建议」），实现时**不要改主意**：

- **D-R9-1**：门禁覆盖 `in_progress` 与 `completed` 两种转换；`pending/blocked/dropped` 一律放行。
- **D-R9-2**：`add` 的依赖必须**存在、非空、不自指、不重复**；违反 → `unknown_dependency` / `self_dependency`，清单不变。
- **D-R9-3**：门禁在**工具层**（`todo_write`），`TaskState` 保持宽松；开关 `ai.enforce_dependencies: bool = true`。
- **D-R9-4**：遗留环/幽灵依赖只靠**错误信息里的三条出路**（完成依赖 / `dropped` 掉作废依赖 / 环里丢一项），不加 `diagnose` 动作。
- **D-R9-5**：5.1 **零依赖**：导出 OTLP/JSON 文件，本轮**不发 HTTP**、不装 OTel SDK。
- **D-R9-6**：5.3 出**静态单文件 HTML**（内联 CSS、无外部资源、无 JS 依赖），不做 web 服务。
- **D-R9-7**：导出**默认不含**消息全文；`--full` 显式打开。
- **D-R9-8**：`--since` 与 prune 同口径——**目录 mtime**。
- **D-R9-9**：`summary.turns != 磁盘回合数` 时**只标注** `mismatch`，退出码仍为 0（不加 `--strict`）。

工程质量与流程约束（与前几轮一致）：

- 质量门禁：`pytest tests/ -o addopts="" -q`（当前 **1073 passed / 4 skipped**）、`ruff check .`、`mypy ai_coding`。
- **既有测试断言一条都不改**；只有确认测试本身写错才动，并在提交信息与回复里写明理由。
- TDD：先写红测试 → 跑红 → 最小实现 → 跑绿 → 提交。一个 Task 一个提交。
- 提交信息不含反引号；多行信息用 `git commit -F <file>`。
- 不改：轨迹写入格式（`v/ts/session/turn/span/parent/iter/evt/data`）、`TaskState.update()` 的宽松语义、
  `<task-state>` 的渲染形状、循环/策略/记忆/压缩/审批/steering。
- 新增公开接口（后续 Task 的名字/签名以此为准）：
  `domain.task.Blockers`、`TaskState.blockers(item_id)`、`TaskState.dependency_invariant()`、`TaskState.next_id()`；
  `TodoWriteTool(enforce_dependencies=True)`、错误码 `unknown_dependency` / `self_dependency` / `blocked_by_dependencies`；
  `AIConfig.enforce_dependencies`；
  `core.trace.SessionRef`、`TraceStore.sessions()`；
  `core.aggregate.SessionRow` / `AggregateReport` / `aggregate_sessions()` / `build_report()`；
  `core.export_html.SpanRow` / `TurnView` / `SessionView` / `read_session_view()` / `render_session_html()` / `render_index_html()`；
  `core.export_otlp.rows_to_otlp()`；
  `ui.trace_render.render_overview()`。

## File Structure

| 文件 | 责任 | Task |
|---|---|---|
| `ai_coding/domain/task.py` | 任务清单值对象：新增 `Blockers` 纯查询与不变量诊断 | 1 |
| `ai_coding/tools/todo_write_tool.py` | 清单的唯一写入口：add 校验 + 状态门禁 | 2 |
| `ai_coding/config/models.py`、`ai_coding/tools/builder.py` | 开关与接线 | 3 |
| `ai_coding/core/trace.py` | 轨迹存储：新增「枚举会话」，`prune` 改用同一口径 | 4 |
| `ai_coding/core/aggregate.py`（新） | 跨会话聚合（纯函数 + 一个读盘入口） | 5 |
| `ai_coding/ui/trace_render.py`、`ai_coding/cli.py` | `trace overview` 的渲染与命令 | 6 |
| `ai_coding/core/export_html.py`（新） | 单文件 HTML 导出（转义是第一条规矩） | 7, 8 |
| `ai_coding/core/export_otlp.py`（新） | 轨迹行 → OTLP/JSON | 9, 10 |
| `scripts/r9_cases.py`（新） | 离线验收脚本 | 11 |

---

### Task 1: 依赖变成一个有不变量、有诊断的查询

**Files:**
- Modify: `ai_coding/domain/task.py`
- Test: `tests/test_domain_task.py`（追加 6 条）

**Interfaces:**
- Consumes: `TodoItem.depends_on`、`TaskState.get()`、`TodoItem.is_terminal()`
- Produces: `Blockers`（冻结数据类，字段 `open/unknown/cycle` + 属性 `any`）；
  `TaskState.blockers(item_id) -> Blockers`；`TaskState.dependency_invariant() -> tuple[str, ...]`；
  `TaskState.next_id() -> str`（把原来的 `_next_id` 公开，Task 2 需要提前知道新 id）

- [x] **Step 1: 写红测试**

```python
# ---- R9a: the dependency query (docs/r9-analysis.md 3.1) ----


def test_r9_blockers_are_empty_when_dependencies_are_terminal():
    task = TaskState()
    task.add("dep")
    task.add("next", depends_on=["1"])
    task.update("1", status="completed")
    assert task.blockers("2") == Blockers()
    assert task.blockers("2").any is False


def test_r9_blockers_list_open_dependencies():
    task = TaskState()
    task.add("dep")
    task.add("next", depends_on=["1"])
    blockers = task.blockers("2")
    assert blockers.open == ("1",)
    assert blockers.unknown == () and blockers.cycle == ()
    assert blockers.any is True


def test_r9_blockers_report_unknown_ids():
    task = TaskState.from_dict(
        {"goal": "g", "items": [{"id": "1", "content": "a", "dependsOn": ["9"]}]}
    )
    assert task.blockers("1").unknown == ("9",)
    assert task.blockers("1").open == ()


def test_r9_blockers_detect_a_legacy_cycle():
    task = TaskState.from_dict(
        {
            "goal": "g",
            "items": [
                {"id": "1", "content": "a", "dependsOn": ["2"]},
                {"id": "2", "content": "b", "dependsOn": ["1"]},
            ],
        }
    )
    assert task.blockers("1").cycle == ("1", "2", "1")
    assert task.blockers("2").cycle == ("2", "1", "2")


def test_r9_dependency_invariant_reports_unknown_self_and_forward():
    task = TaskState.from_dict(
        {
            "goal": "g",
            "items": [
                {"id": "1", "content": "forward", "dependsOn": ["2"]},
                {"id": "2", "content": "self", "dependsOn": ["2"]},
                {"id": "3", "content": "ghost", "dependsOn": ["99"]},
                {"id": "4", "content": "clean"},
            ],
        }
    )
    assert task.dependency_invariant() == ("1", "2", "3")


def test_r9_render_and_focus_are_unchanged():
    """The domain keeps rendering and choosing exactly as before (R1 semantics)."""
    task = TaskState(goal="g")
    task.add("dep")
    task.add("needs dep", depends_on=["1"])
    assert task.focus() is not None and task.focus().id == "1"
    task.update("2", status="blocked")
    assert "[!] 2. needs dep (blocked by 1)" in task.render()
```

补充：`tests/test_domain_task.py` 顶部 import 需要 `Blockers`：

```python
from ai_coding.domain.task import Blockers, TaskState
```

- [x] **Step 2: 跑红**

Run: `.venv/bin/python -m pytest tests/test_domain_task.py -o addopts="" -q`
Expected: FAIL —— `ImportError: cannot import name 'Blockers' from 'ai_coding.domain.task'`

- [x] **Step 3: 最小实现**

在 `TodoItem` 之后、`TaskState` 之前加：

```python
@dataclass(frozen=True)
class Blockers:
    """Why an item cannot start or finish yet (R9a).  Empty = ready.

    ``open``    - dependencies that exist and are not terminal yet;
    ``unknown`` - dependencies whose target is gone (legacy or hand-edited data);
    ``cycle``   - a blocking chain that comes back to this item, when there is one.
    """

    open: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()
    cycle: tuple[str, ...] = ()

    @property
    def any(self) -> bool:
        return bool(self.open or self.unknown or self.cycle)
```

`TaskState` 里把 `_next_id` 改名为公开的 `next_id`（内部唯一调用点在 `add`），并在 `focus()` 之后加：

```python
    def blockers(self, item_id: str) -> Blockers:
        """The unmet dependencies of one item, with a diagnostic for legacy data.

        Pure: it reads the list and nothing else, so the tool layer can decide
        what to refuse without the domain knowing anything about policies.
        """
        item = self.get(item_id)
        if item is None:
            return Blockers()
        open_ids: list[str] = []
        unknown: list[str] = []
        for dep in item.depends_on:
            target = self.get(dep)
            if target is None:
                unknown.append(dep)
            elif not target.is_terminal():
                open_ids.append(dep)
        cycle = self._cycle_from(item_id) if (open_ids or unknown) else ()
        return Blockers(open=tuple(open_ids), unknown=tuple(unknown), cycle=cycle)

    def _cycle_from(self, item_id: str) -> tuple[str, ...]:
        """A non-terminal dependency chain that comes back to ``item_id``.

        Only legacy/hand-edited lists can contain one: ``add`` refuses unknown
        ids (Task 2) and there is no way to rewrite ``depends_on`` afterwards,
        so fresh data is a DAG by construction.
        """
        visited: set[str] = set()
        stack: list[tuple[str, tuple[str, ...]]] = [(item_id, (item_id,))]
        while stack:
            current, path = stack.pop()
            node = self.get(current)
            if node is None or node.is_terminal():
                continue
            for dep in node.depends_on:
                if dep == item_id:
                    return (*path, item_id)
                if dep in visited:
                    continue
                visited.add(dep)
                stack.append((dep, (*path, dep)))
        return ()

    def dependency_invariant(self) -> tuple[str, ...]:
        """Ids whose ``depends_on`` is not an existing, earlier item id.

        This is the acceptance yardstick for "the list is well formed": the tool
        refuses to create violations, and this method makes the ones inherited
        from older sessions visible instead of silent.
        """
        order = {item.id: index for index, item in enumerate(self.items)}
        bad: list[str] = []
        for item in self.items:
            for dep in item.depends_on:
                if dep == item.id or dep not in order or order[dep] >= order[item.id]:
                    bad.append(item.id)
                    break
        return tuple(bad)
```

`__all__` 里加 `"Blockers"`。

- [x] **Step 4: 跑绿 + 门禁**

Run: `.venv/bin/python -m pytest tests/test_domain_task.py -o addopts="" -q`
Expected: **24 passed**（既有 18 + 新增 6），一条既有断言都没改

- [x] **Step 5: 提交**

```bash
git add ai_coding/domain/task.py tests/test_domain_task.py
git commit -m "R9a-1: dependencies become a query with an invariant"
```

---

### Task 2: 门禁落在工具层

**Files:**
- Modify: `ai_coding/tools/todo_write_tool.py`
- Test: `tests/test_tool_todo_write.py`（追加 9 条）

**Interfaces:**
- Consumes: `TaskState.blockers()`、`TaskState.next_id()`、`TaskState.get()`
- Produces: `TodoWriteTool(enforce_dependencies: bool = True)`；错误码
  `unknown_dependency` / `self_dependency` / `blocked_by_dependencies`

- [x] **Step 1: 写红测试**

```python
# ---- R9a: the gate (docs/r9-analysis.md 3.1) ----


def _gate_tool() -> TodoWriteTool:
    return TodoWriteTool(enforce_dependencies=True)


@pytest.mark.asyncio
async def test_r9_add_refuses_an_unknown_dependency(ctx):
    r = await _gate_tool().execute(
        {"action": "add", "content": "b", "depends_on": ["9"]}, ctx
    )
    assert r.success is False
    assert r.error == "unknown_dependency"
    assert ctx.task.items == []


@pytest.mark.asyncio
async def test_r9_add_refuses_a_self_dependency(ctx):
    r = await _gate_tool().execute(
        {"action": "add", "content": "a", "depends_on": ["1"]}, ctx
    )
    assert r.success is False
    assert r.error == "self_dependency"
    assert ctx.task.items == []


@pytest.mark.asyncio
async def test_r9_add_deduplicates_dependencies(ctx):
    await _gate_tool().execute({"action": "add", "content": "a"}, ctx)
    r = await _gate_tool().execute(
        {"action": "add", "content": "b", "depends_on": ["1", "1"]}, ctx
    )
    assert r.success is True
    assert ctx.task.get("2").depends_on == ["1"]


@pytest.mark.asyncio
async def test_r9_completing_a_blocked_item_is_refused(ctx):
    tool = _gate_tool()
    await tool.execute({"action": "add", "content": "dep"}, ctx)
    await tool.execute({"action": "add", "content": "next", "depends_on": ["1"]}, ctx)
    r = await tool.execute({"action": "update", "id": "2", "status": "completed"}, ctx)
    assert r.success is False
    assert r.error == "blocked_by_dependencies"
    assert r.data is not None and r.data["open"] == ["1"]
    assert ctx.task.get("2").status == "pending"  # state untouched


@pytest.mark.asyncio
async def test_r9_starting_a_blocked_item_is_refused_too(ctx):
    tool = _gate_tool()
    await tool.execute({"action": "add", "content": "dep"}, ctx)
    await tool.execute({"action": "add", "content": "next", "depends_on": ["1"]}, ctx)
    r = await tool.execute({"action": "update", "id": "2", "status": "in_progress"}, ctx)
    assert r.error == "blocked_by_dependencies"
    assert ctx.task.get("2").status == "pending"


@pytest.mark.asyncio
async def test_r9_after_the_dependency_completes_the_item_can_finish(ctx):
    tool = _gate_tool()
    await tool.execute({"action": "add", "content": "dep"}, ctx)
    await tool.execute({"action": "add", "content": "next", "depends_on": ["1"]}, ctx)
    await tool.execute({"action": "update", "id": "1", "status": "completed"}, ctx)
    r = await tool.execute({"action": "update", "id": "2", "status": "completed"}, ctx)
    assert r.success is True
    assert ctx.task.get("2").status == "completed"


@pytest.mark.asyncio
async def test_r9_dropping_the_dependency_unblocks(ctx):
    tool = _gate_tool()
    await tool.execute({"action": "add", "content": "dep"}, ctx)
    await tool.execute({"action": "add", "content": "next", "depends_on": ["1"]}, ctx)
    await tool.execute({"action": "update", "id": "1", "status": "dropped"}, ctx)
    r = await tool.execute({"action": "update", "id": "2", "status": "completed"}, ctx)
    assert r.success is True


@pytest.mark.asyncio
async def test_r9_pending_blocked_and_dropped_are_never_gated(ctx):
    tool = _gate_tool()
    await tool.execute({"action": "add", "content": "dep"}, ctx)
    await tool.execute({"action": "add", "content": "next", "depends_on": ["1"]}, ctx)
    for status in ("pending", "blocked", "dropped"):
        r = await tool.execute({"action": "update", "id": "2", "status": status}, ctx)
        assert r.success is True, status


@pytest.mark.asyncio
async def test_r9_the_gate_can_be_switched_off(ctx):
    tool = TodoWriteTool(enforce_dependencies=False)
    await tool.execute({"action": "add", "content": "dep"}, ctx)
    await tool.execute({"action": "add", "content": "next", "depends_on": ["1"]}, ctx)
    r = await tool.execute({"action": "update", "id": "2", "status": "completed"}, ctx)
    assert r.success is True  # today's behaviour
```

- [x] **Step 2: 跑红**

Run: `.venv/bin/python -m pytest tests/test_tool_todo_write.py -o addopts="" -q`
Expected: FAIL —— `AttributeError: 'TodoWriteTool' object has no attribute '_enforce'`（或 `unknown_dependency` 断言失败）

- [x] **Step 3: 最小实现**

```python
class TodoWriteTool(BaseTool):
    """Maintain the session task list (add / update / list / clear)."""

    parallel_safe = True

    def __init__(self, *, enforce_dependencies: bool = True) -> None:
        #: R9a: refuse to start/finish an item whose dependencies are unmet, and
        #: refuse to create dependencies that break the list's invariant.  The
        #: switch exists for legacy lists (docs/r9-analysis.md D-R9-3).
        self._enforce = bool(enforce_dependencies)
```

`_add` 在拿到 `deps` 之后、`task.add(...)` 之前插入校验（并且要预判新 id，才能认出自依赖）：

```python
        raw_deps = params.get("depends_on") or []
        deps = [str(d) for d in raw_deps] if isinstance(raw_deps, list) else []
        if self._enforce:
            new_id = task.next_id()
            cleaned: list[str] = []
            for dep in deps:
                target = dep.strip()
                if not target:
                    continue
                if target == new_id:
                    return ToolResult(
                        success=False,
                        output=(
                            f"error: item {new_id} cannot depend on itself; "
                            "drop it from depends_on or add it after the dependency exists"
                        ),
                        error="self_dependency",
                        data={"action": "add", "id": new_id, "depends_on": [target]},
                    )
                if task.get(target) is None:
                    return ToolResult(
                        success=False,
                        output=(
                            f"error: dependency '{target}' is not in the list; "
                            "add it first (ids are assigned in order), or drop it "
                            "from depends_on"
                        ),
                        error="unknown_dependency",
                        data={"action": "add", "depends_on": [target]},
                    )
                if target not in cleaned:
                    cleaned.append(target)
            deps = cleaned
        item = task.add(content, depends_on=deps)
```

`_update` 在真正 `task.update(...)` 之前插入门禁：

```python
        status = params.get("status")
        normalised = None if status is None else str(status).strip().lower()
        if self._enforce and normalised in {"in_progress", "completed"}:
            blockers = task.blockers(item_id)
            if blockers.any:
                return self._blocked_result(item_id, blockers)
```

以及拒绝结果本身（**错误信息必须能指导模型走出去**）：

```python
    @staticmethod
    def _blocked_result(item_id: str, blockers: Blockers) -> ToolResult:
        reasons: list[str] = []
        if blockers.open:
            reasons.append("未完成的依赖: " + ", ".join(blockers.open))
        if blockers.unknown:
            reasons.append("已不存在的依赖: " + ", ".join(blockers.unknown))
        if blockers.cycle:
            reasons.append("依赖成环: " + " → ".join(blockers.cycle))
        return ToolResult(
            success=False,
            output=(
                f"error: todo #{item_id} 还不能开始/完成（{'；'.join(reasons)}）。"
                "出路：先 update 那些依赖为 completed；若某个依赖已作废，"
                "update(id=<它>, status=dropped) 解锁；若链条成环，"
                "把环里任意一项 dropped。"
            ),
            error="blocked_by_dependencies",
            data={
                "id": item_id,
                "open": list(blockers.open),
                "unknown": list(blockers.unknown),
                "cycle": list(blockers.cycle),
            },
        )
```

import 需要加 `Blockers`：`from ai_coding.domain.task import VALID_STATUSES, Blockers, TaskState`。

> **注意**：`update` 里原有的 `status=None if status is None else str(status).strip().lower()`
> 逻辑要复用同一个 `normalised` 变量，避免两处各自处理（否则「大写 COMPLETED」会绕过门禁）。

- [x] **Step 4: 跑绿 + 门禁**

Run: `.venv/bin/python -m pytest tests/test_tool_todo_write.py tests/test_domain_task.py tests/test_agent_loop_react.py -o addopts="" -q && .venv/bin/python -m ruff check . && .venv/bin/python -m mypy ai_coding`
Expected: 全绿（既有 `todo_write` 用例零改动）

- [x] **Step 5: 提交**

```bash
git add ai_coding/tools/todo_write_tool.py tests/test_tool_todo_write.py
git commit -m "R9a-2: the todo gate refuses to skip an unmet dependency"
```

---

### Task 3: 开关与接线

**Files:**
- Modify: `ai_coding/config/models.py`、`ai_coding/tools/builder.py`
- Test: `tests/test_builder.py`（追加 1 条）

**Interfaces:**
- Consumes: `TodoWriteTool(enforce_dependencies=...)`
- Produces: `AIConfig.enforce_dependencies: bool = True`；builder 按配置构造 `todo_write`

- [x] **Step 1: 写红测试**

```python
def test_r9_the_dependency_gate_is_on_by_default_and_configurable(tmp_path):
    on = _cfg(str(tmp_path))  # _cfg 里没有 enforce_dependencies → getattr 默认 True
    assert build_tool_registry(on, str(tmp_path), None).get("todo_write")._enforce is True

    off = _cfg(str(tmp_path))
    off.ai.enforce_dependencies = False
    assert build_tool_registry(off, str(tmp_path), None).get("todo_write")._enforce is False
```

- [x] **Step 2: 跑红**

Run: `.venv/bin/python -m pytest tests/test_builder.py -o addopts="" -q`
Expected: FAIL —— `AttributeError: 'TodoWriteTool' object has no attribute '_enforce'`（Task 2 已合入时是第二个断言失败）

- [x] **Step 3: 最小实现**

`config/models.py`，在 `max_todo_nudges` 旁边加：

```python
    # R9a: refuse to start/finish an item whose depends_on is unmet, and refuse
    # to create a dependency on a missing or self id.  Off = the R1 behaviour,
    # which exists for legacy lists only (docs/r9-analysis.md D-R9-3).
    enforce_dependencies: bool = True
```

`tools/builder.py` 第 129 行的 `registry.register(TodoWriteTool())` 改成：

```python
    # R9a: the dependency gate is on by default; legacy lists can switch it off.
    registry.register(
        TodoWriteTool(
            enforce_dependencies=bool(getattr(app_cfg.ai, "enforce_dependencies", True))
        )
    )
```

- [x] **Step 4: 跑绿 + 门禁**

Run: `.venv/bin/python -m pytest tests/test_builder.py tests/test_tool_todo_write.py tests/test_cli.py -o addopts="" -q && .venv/bin/python -m ruff check . && .venv/bin/python -m mypy ai_coding`
Expected: 全绿（`tests/test_builder.py` 的 `SimpleNamespace` 缺这个字段，靠 `getattr` 默认开）

- [x] **Step 5: 提交**

```bash
git add ai_coding/config/models.py ai_coding/tools/builder.py tests/test_builder.py
git commit -m "R9a-3: the dependency gate has a switch and reaches the tool stack"
```

---

### Task 4: 轨迹存储学会「列出所有会话」

**Files:**
- Modify: `ai_coding/core/trace.py`
- Test: `tests/test_trace_store.py`（追加 5 条）

**Interfaces:**
- Produces: `SessionRef(session, path, mtime, turn_files)`（属性 `turns`）；
  `TraceStore.sessions() -> tuple[SessionRef, ...]`（mtime 倒序、tie 按 id）；`prune()` 改用同一口径

- [x] **Step 1: 写红测试**

```python
# ---- R9b: listing sessions (docs/r9-analysis.md 3.2) ----


def test_r9_sessions_lists_directories_with_mtime_and_turns(tmp_path):
    store = _store(tmp_path)
    store.begin_turn("sess-1").write_line("{}")
    store.begin_turn("sess-1").write_line("{}")
    (store.root / "stray-file.txt").write_text("not a session", encoding="utf-8")

    refs = store.sessions()

    assert [ref.session for ref in refs] == ["sess-1"]
    assert refs[0].turns == 2
    assert refs[0].path == store.session_dir("sess-1")
    assert refs[0].mtime == store.session_dir("sess-1").stat().st_mtime


def test_r9_sessions_are_newest_first(tmp_path):
    store = _store(tmp_path)
    store.begin_turn("old").write_line("{}")
    os.utime(store.session_dir("old"), (1000, 1000))
    store.begin_turn("new").write_line("{}")
    os.utime(store.session_dir("new"), (2000, 2000))

    assert [ref.session for ref in store.sessions()] == ["new", "old"]


def test_r9_a_new_turn_file_refreshes_the_directory_mtime(tmp_path):
    """`--since` and `prune` share this basis, so it must hold."""
    store = _store(tmp_path)
    store.begin_turn("sess-1").write_line("{}")
    before = store.session_dir("sess-1").stat().st_mtime
    time.sleep(0.01)
    store.begin_turn("sess-1").write_line("{}")

    assert store.session_dir("sess-1").stat().st_mtime > before


def test_r9_prune_still_removes_only_expired_sessions(tmp_path):
    store = _store(tmp_path, retention_days=1)
    store.begin_turn("fresh").write_line("{}")
    store.begin_turn("stale").write_line("{}")
    old = time.time() - 3 * 86400
    os.utime(store.session_dir("stale"), (old, old))

    assert store.prune() == 1
    assert store.session_dir("fresh").is_dir()
    assert not store.session_dir("stale").exists()


def test_r9_sessions_on_a_missing_root_is_empty(tmp_path):
    assert TraceStore(tmp_path / "nope").sessions() == ()
```

（`tests/test_trace_store.py` 已有 `import os, time` 与 `_store` 帮助函数，直接复用。）

- [x] **Step 2: 跑红**

Run: `.venv/bin/python -m pytest tests/test_trace_store.py -o addopts="" -q`
Expected: FAIL —— `AttributeError: 'TraceStore' object has no attribute 'sessions'`

- [x] **Step 3: 最小实现**

在 `TurnWriter` 之前加：

```python
@dataclass(frozen=True)
class SessionRef:
    """One session directory on disk (R9b)."""

    session: str
    path: Path
    mtime: float
    turn_files: tuple[Path, ...]

    @property
    def turns(self) -> int:
        return len(self.turn_files)


def _turn_number(path: Path) -> int:
    """``turn-7.jsonl`` → 7；不认识的形状记 0（排最前）。"""
    stem = path.stem.removeprefix("turn-")
    return int(stem) if stem.isdigit() else 0
```

`__all__` 加 `"SessionRef"`；`TraceStore` 在 `next_turn_number` 之后加：

```python
    def sessions(self) -> tuple[SessionRef, ...]:
        """Every session directory, newest write first.  Never raises (R9b).

        Newest-first is the order ``trace overview`` wants; ties fall back to the
        session id, so the output is deterministic.
        """
        if not self.root.is_dir():
            return ()
        refs: list[SessionRef] = []
        for entry in sorted(self.root.iterdir()):
            if not entry.is_dir():
                continue
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            files = tuple(sorted(entry.glob("turn-*.jsonl"), key=_turn_number))
            refs.append(SessionRef(entry.name, entry, mtime, files))
        refs.sort(key=lambda ref: (-ref.mtime, ref.session))
        return tuple(refs)
```

`prune()` 的循环体换成同一口径（行为不变，只是不再自己 `iterdir`）：

```python
        for ref in self.sessions():
            if ref.mtime >= cutoff:
                continue
            try:
                for child in sorted(ref.path.rglob("*"), reverse=True):
                    if child.is_file():
                        child.unlink()
                    else:
                        child.rmdir()
                ref.path.rmdir()
            except OSError:
                continue
            removed += 1
```

- [x] **Step 4: 跑绿 + 门禁**

Run: `.venv/bin/python -m pytest tests/test_trace_store.py tests/test_trace_recorder.py tests/test_cli_trace.py -o addopts="" -q`
Expected: 全绿（既有 prune / numbering 用例零改动）

- [x] **Step 5: 提交**

```bash
git add ai_coding/core/trace.py tests/test_trace_store.py
git commit -m "R9b-1: the trace store can list its sessions"
```

---

### Task 5: 一份报告，跨全部会话

**Files:**
- Create: `ai_coding/core/aggregate.py`
- Test: `tests/test_trace_aggregate.py`（新建，5 条）

**Interfaces:**
- Consumes: `TraceStore.sessions()`、`TraceStore.read_summary()`
- Produces: `SessionRow`、`AggregateReport`、`row_from_summary(...)`、`aggregate_sessions(rows)`、
  `build_report(store, *, since_days=0, now=None)`

- [x] **Step 1: 写红测试**

```python
"""TDD (R9b): cross-session aggregation — pure maths, one disk entry point."""

import json
import os
import time
from pathlib import Path

from ai_coding.core.aggregate import SessionRow, aggregate_sessions, build_report
from ai_coding.core.trace import TraceStore


def _store(tmp_path: Path) -> TraceStore:
    return TraceStore(tmp_path / "traces")


def _record(store: TraceStore, session: str, *, turns: int, summary: dict | None) -> None:
    for _ in range(turns):
        store.begin_turn(session).write_line("{}")
    if summary is not None:
        (store.session_dir(session) / "summary.json").write_text(
            json.dumps(summary), encoding="utf-8"
        )


def test_r9_rows_sum_into_totals():
    rows = (
        SessionRow("a", turns=2, files=2, steps=3, prompt_tokens=100,
                   completion_tokens=10, cached_tokens=80),
        SessionRow("b", turns=1, files=1, steps=1, prompt_tokens=50,
                   completion_tokens=5, cached_tokens=0),
    )
    report = aggregate_sessions(rows)
    assert report.totals.turns == 3
    assert report.totals.steps == 4
    assert report.totals.prompt_tokens == 150
    assert report.totals.completion_tokens == 15
    assert report.totals.cached_tokens == 80
    assert [row.session for row in report.rows] == ["a", "b"]  # order preserved


def test_r9_cache_ratio_and_zero_prompt():
    report = aggregate_sessions((SessionRow("a", prompt_tokens=200, cached_tokens=50),))
    assert report.cache_ratio == 0.25
    empty = aggregate_sessions(())
    assert empty.cache_ratio == 0.0
    assert empty.totals.turns == 0


def test_r9_a_missing_summary_counts_zero_and_flags_mismatch(tmp_path):
    store = _store(tmp_path)
    _record(store, "sess-1", turns=2, summary=None)

    row = build_report(store).rows[0]

    assert row.turns == 0 and row.files == 2
    assert row.mismatch is True


def test_r9_a_corrupt_summary_is_not_fatal(tmp_path):
    store = _store(tmp_path)
    _record(store, "sess-1", turns=1, summary=None)
    (store.session_dir("sess-1") / "summary.json").write_text("not json", encoding="utf-8")

    row = build_report(store).rows[0]

    assert row.files == 1 and row.turns == 0


def test_r9_since_filters_by_directory_mtime_and_keeps_order(tmp_path):
    store = _store(tmp_path)
    _record(store, "old", turns=1, summary={"turns": 1})
    _record(store, "new", turns=1, summary={"turns": 1})
    stale = time.time() - 30 * 86400
    os.utime(store.session_dir("old"), (stale, stale))

    assert [row.session for row in build_report(store, since_days=7).rows] == ["new"]
    assert [row.session for row in build_report(store).rows] == ["new", "old"]
```

- [x] **Step 2: 跑红**

Run: `.venv/bin/python -m pytest tests/test_trace_aggregate.py -o addopts="" -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'ai_coding.core.aggregate'`

- [x] **Step 3: 最小实现**

```python
"""Cross-session aggregation for ``trace overview`` (R9b).

Pure wherever it can be: the maths takes rows and returns a report, so the table,
the JSON and the HTML index all share one set of numbers and the tests need no
filesystem.  ``build_report`` is the single disk-touching entry point, and it
degrades to zeros instead of raising - a broken trace must not break the report.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ai_coding.core.trace import TraceStore

__all__ = [
    "AggregateReport",
    "SessionRow",
    "aggregate_sessions",
    "build_report",
    "row_from_summary",
]


@dataclass(frozen=True)
class SessionRow:
    """One session's numbers, as read (or as summed for the totals row)."""

    session: str
    turns: int = 0
    files: int = 0
    steps: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    age_days: float = 0.0

    @property
    def mismatch(self) -> bool:
        """``summary.turns`` disagrees with the turn files on disk."""
        return self.turns != self.files

    @property
    def cache_ratio(self) -> float:
        return self.cached_tokens / self.prompt_tokens if self.prompt_tokens > 0 else 0.0


@dataclass(frozen=True)
class AggregateReport:
    rows: tuple[SessionRow, ...] = ()
    totals: SessionRow = field(default_factory=lambda: SessionRow(session="(all)"))

    @property
    def cache_ratio(self) -> float:
        return self.totals.cache_ratio


def _int(value: Any) -> int:
    """Tolerant int: anything unusable counts as zero (never raises)."""
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def row_from_summary(
    session: str,
    summary: Any,
    *,
    files: int,
    mtime: float,
    now: float,
) -> SessionRow:
    """One reading row; a missing/broken summary is "zeros", not an exception."""
    data: dict[str, Any] = summary if isinstance(summary, dict) else {}
    age_days = max(0.0, (now - mtime) / 86_400) if mtime else 0.0
    return SessionRow(
        session=session,
        turns=_int(data.get("turns")),
        files=int(files),
        steps=_int(data.get("steps")),
        prompt_tokens=_int(data.get("prompt_tokens")),
        completion_tokens=_int(data.get("completion_tokens")),
        cached_tokens=_int(data.get("cached_tokens")),
        age_days=age_days,
    )


def aggregate_sessions(rows: Sequence[SessionRow]) -> AggregateReport:
    """Sum the rows into one report, preserving their order.  Pure."""
    totals = SessionRow(
        session="(all)",
        turns=sum(row.turns for row in rows),
        files=sum(row.files for row in rows),
        steps=sum(row.steps for row in rows),
        prompt_tokens=sum(row.prompt_tokens for row in rows),
        completion_tokens=sum(row.completion_tokens for row in rows),
        cached_tokens=sum(row.cached_tokens for row in rows),
    )
    return AggregateReport(rows=tuple(rows), totals=totals)


def build_report(
    store: TraceStore, *, since_days: int = 0, now: float | None = None
) -> AggregateReport:
    """Read every session on disk and fold it into one report (R9b).

    ``since_days`` filters on the **directory mtime**, exactly like ``prune``, so
    "what prune would delete" and "what counts as old" can never disagree.
    """
    moment = time.time() if now is None else now
    cutoff = moment - since_days * 86_400 if since_days > 0 else None
    rows: list[SessionRow] = []
    for ref in store.sessions():
        if cutoff is not None and ref.mtime < cutoff:
            continue
        rows.append(
            row_from_summary(
                ref.session,
                store.read_summary(ref.session),
                files=ref.turns,
                mtime=ref.mtime,
                now=moment,
            )
        )
    return aggregate_sessions(rows)
```

- [x] **Step 4: 跑绿 + 门禁**

Run: `.venv/bin/python -m pytest tests/test_trace_aggregate.py -o addopts="" -q && .venv/bin/python -m ruff check . && .venv/bin/python -m mypy ai_coding`
Expected: 5 passed / clean

- [x] **Step 5: 提交**

```bash
git add ai_coding/core/aggregate.py tests/test_trace_aggregate.py
git commit -m "R9b-2: one report, folded across every recorded session"
```

---

### Task 6: `trace overview`

**Files:**
- Modify: `ai_coding/ui/trace_render.py`、`ai_coding/cli.py`
- Test: `tests/test_cli_trace.py`（追加 5 条）

**Interfaces:**
- Consumes: `core.aggregate.build_report`、`AggregateReport`
- Produces: `ui.trace_render.render_overview(report) -> str`；CLI `trace overview`（新选项 `--since` / `--format`）

- [x] **Step 1: 写红测试**

```python
# ---- R9b: trace overview (docs/r9-analysis.md 3.2) ----


def _session_with(tmp_path, session: str, *, turns: int, summary: dict) -> Path:
    session_dir = tmp_path / ".ai-coding" / "traces" / session
    session_dir.mkdir(parents=True)
    for index in range(1, turns + 1):
        (session_dir / f"turn-{index}.jsonl").write_text("{}\n", encoding="utf-8")
    (session_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    return session_dir


def test_r9_overview_renders_sessions_and_a_total(tmp_path):
    _session_with(tmp_path, "sess-1", turns=2,
                  summary={"turns": 2, "steps": 3, "prompt_tokens": 200,
                           "completion_tokens": 20, "cached_tokens": 150})
    _session_with(tmp_path, "sess-2", turns=1,
                  summary={"turns": 1, "steps": 1, "prompt_tokens": 100,
                           "completion_tokens": 10, "cached_tokens": 0})

    result = runner.invoke(app, ["trace", "overview", "--workspace", str(tmp_path)])

    assert result.exit_code == 0
    assert "sess-1" in result.stdout and "sess-2" in result.stdout
    assert "TOTAL" in result.stdout
    assert "300" in result.stdout        # prompt tokens summed
    assert "50.0%" in result.stdout      # 150/300


def test_r9_overview_json_is_machine_readable(tmp_path):
    _session_with(tmp_path, "sess-1", turns=1,
                  summary={"turns": 1, "steps": 2, "prompt_tokens": 10,
                           "completion_tokens": 1, "cached_tokens": 5})

    result = runner.invoke(
        app, ["trace", "overview", "--workspace", str(tmp_path), "--format", "json"]
    )

    payload = json.loads(result.stdout)
    assert payload["totals"]["session"] == "(all)"
    assert payload["totals"]["turns"] == 1
    assert payload["rows"][0]["session"] == "sess-1"
    assert payload["rows"][0]["cache_ratio"] == 0.5


def test_r9_overview_since_filters_old_sessions(tmp_path):
    old = _session_with(tmp_path, "old", turns=1, summary={"turns": 1})
    _session_with(tmp_path, "new", turns=1, summary={"turns": 1})
    stale = time.time() - 30 * 86400
    os.utime(old, (stale, stale))

    result = runner.invoke(
        app, ["trace", "overview", "--workspace", str(tmp_path), "--since", "7"]
    )

    assert "new" in result.stdout
    assert "old" not in result.stdout


def test_r9_overview_on_an_empty_workspace(tmp_path):
    result = runner.invoke(app, ["trace", "overview", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "no traces recorded" in result.stdout


def test_r9_overview_flags_a_summary_mismatch(tmp_path):
    _session_with(tmp_path, "sess-1", turns=1, summary={"turns": 9})
    result = runner.invoke(app, ["trace", "overview", "--workspace", str(tmp_path)])
    assert "mismatch" in result.stdout.lower()
```

（`tests/test_cli_trace.py` 需要补 `import os, time` 与 `from pathlib import Path`。）

- [x] **Step 2: 跑红**

Run: `.venv/bin/python -m pytest tests/test_cli_trace.py -o addopts="" -q`
Expected: FAIL —— typer 不认 `overview` / `--since` / `--format`（`exit_code == 2`）

- [x] **Step 3: 最小实现**

`ui/trace_render.py`（import 处补 `from ai_coding.core.aggregate import AggregateReport`）：

```python
def render_overview(report: AggregateReport) -> str:
    """One table for every session plus a totals row (R9b).  Pure."""
    header = (
        f"{'SESSION':<20} {'TURNS':>5} {'FILES':>5} {'STEPS':>5} {'PROMPT':>9} "
        f"{'COMPL':>7} {'CACHED':>8} {'HIT%':>6} {'AGE':>6}"
    )
    lines = [header, "-" * len(header)]
    for row in report.rows:
        flag = "  !mismatch" if row.mismatch else ""
        lines.append(
            f"{row.session[:20]:<20} {row.turns:>5} {row.files:>5} {row.steps:>5} "
            f"{_thousands(row.prompt_tokens):>9} {_thousands(row.completion_tokens):>7} "
            f"{_thousands(row.cached_tokens):>8} {row.cache_ratio * 100:>5.1f}% "
            f"{row.age_days:>5.1f}d{flag}"
        )
    totals = report.totals
    lines.append("-" * len(header))
    lines.append(
        f"{'TOTAL':<20} {totals.turns:>5} {totals.files:>5} {totals.steps:>5} "
        f"{_thousands(totals.prompt_tokens):>9} {_thousands(totals.completion_tokens):>7} "
        f"{_thousands(totals.cached_tokens):>8} {totals.cache_ratio * 100:>5.1f}%"
    )
    bad = [row.session for row in report.rows if row.mismatch]
    if bad:
        lines.append(f"! summary/disk mismatch in {len(bad)} session(s): {', '.join(bad)}")
    return "\n".join(lines)
```

`__all__` 加 `"render_overview"`。

`cli.py::trace_command` 加两个选项与一个分支：

```python
    since: int = typer.Option(
        0, "--since", help="Only sessions touched in the last N days (0 = all)."
    ),
    fmt: str = typer.Option("table", "--format", help="table | json"),
```

```python
    if action == "overview":
        from dataclasses import asdict

        from ai_coding.core.aggregate import build_report
        from ai_coding.core.trace import TraceStore
        from ai_coding.ui.trace_render import render_overview

        report = build_report(TraceStore(root), since_days=max(0, int(since)))
        if not report.rows:
            typer.echo("(no traces recorded yet)")
            return
        if fmt == "json":
            payload = {
                "rows": [
                    {**asdict(row), "mismatch": row.mismatch, "cache_ratio": row.cache_ratio}
                    for row in report.rows
                ],
                "totals": {
                    **asdict(report.totals),
                    "mismatch": report.totals.mismatch,
                    "cache_ratio": report.cache_ratio,
                },
            }
            typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
            return
        typer.echo(render_overview(report))
        return
```

（放在 `if not session:` 之前——`overview` 不需要会话参数。）

- [x] **Step 4: 跑绿 + 门禁**

Run: `.venv/bin/python -m pytest tests/test_cli_trace.py tests/test_trace_render.py -o addopts="" -q && .venv/bin/python -m ruff check . && .venv/bin/python -m mypy ai_coding`
Expected: 全绿

- [x] **Step 5: 提交**

```bash
git add ai_coding/ui/trace_render.py ai_coding/cli.py tests/test_cli_trace.py
git commit -m "R9b-3: trace overview shows every session in one table"
```

---

### Task 7: 一个会话 → 一个 HTML 文件

**Files:**
- Create: `ai_coding/core/export_html.py`
- Test: `tests/test_trace_export_html.py`（新建，8 条）

**Interfaces:**
- Consumes: `TraceStore`、`ui.trace_render.summarize_turn`
- Produces: `SpanRow`、`TurnView`、`SessionView`、`read_session_view(store, session, *, full=False)`、
  `render_session_html(view)`、`render_index_html(report, views)`

- [x] **Step 1: 写红测试**

```python
"""TDD (R9c): one self-contained HTML page per session — escaped, offline, stable."""

import json
from pathlib import Path

from ai_coding.core.aggregate import SessionRow, aggregate_sessions
from ai_coding.core.export_html import read_session_view, render_index_html, render_session_html
from ai_coding.core.trace import TraceStore


def _row(**over) -> dict:
    base = {
        "v": 1, "ts": "2026-09-28T00:00:00.000Z", "session": "sess-1", "turn": 1,
        "span": "i1", "parent": "turn", "iter": 1, "evt": "IterationStarted",
        "data": {},
    }
    base.update(over)
    return base


def _session(tmp_path: Path, *, session: str = "sess-1", rows: list[dict] | None = None) -> TraceStore:
    store = TraceStore(tmp_path / "traces")
    writer = store.begin_turn(session)
    for row in rows if rows is not None else [_row()]:
        writer.write_line(json.dumps(row))
    (store.session_dir(session) / "summary.json").write_text(
        json.dumps({"turns": 1, "steps": 1, "prompt_tokens": 10,
                    "completion_tokens": 1, "cached_tokens": 5}),
        encoding="utf-8",
    )
    return store


def test_r9_script_in_a_tool_output_is_escaped(tmp_path):
    store = _session(tmp_path, rows=[
        _row(evt="ToolStarted", span="c1", parent="i1",
             data={"tool_name": "bash", "params": {"command": "echo '<script>alert(1)</script>'"}}),
    ])
    html = render_session_html(read_session_view(store, "sess-1"))
    assert "&lt;script&gt;" in html
    assert "<script>" not in html


def test_r9_session_id_and_tool_names_are_escaped(tmp_path):
    store = _session(tmp_path, session="s<b>1", rows=[_row(evt="ToolStarted", span="c1", data={"tool_name": "<b>"})])
    html = render_session_html(read_session_view(store, "s<b>1"))
    assert "&lt;b&gt;" in html


def test_r9_the_page_has_no_external_resources(tmp_path):
    store = _session(tmp_path)
    html = render_session_html(read_session_view(store, "sess-1"))
    assert "http://" not in html and "https://" not in html
    assert "<script" not in html and "<link" not in html


def test_r9_full_payload_is_opt_in(tmp_path):
    store = _session(tmp_path)
    (store.session_dir("sess-1") / "turn-1.full.json").write_text(
        json.dumps({"messages": [{"role": "user", "content": "SECRET-TEXT"}]}),
        encoding="utf-8",
    )
    assert "SECRET-TEXT" not in render_session_html(read_session_view(store, "sess-1"))
    assert "SECRET-TEXT" in render_session_html(
        read_session_view(store, "sess-1", full=True)
    )


def test_r9_every_turn_becomes_a_details_block_with_its_summary(tmp_path):
    store = _session(tmp_path)
    view = read_session_view(store, "sess-1")
    html = render_session_html(view)
    assert html.count("<details>") == len(view.turns) == 1
    assert "turn 1" in html


def test_r9_rendering_is_deterministic(tmp_path):
    store = _session(tmp_path)
    view = read_session_view(store, "sess-1")
    assert render_session_html(view) == render_session_html(view)


def test_r9_the_index_page_carries_the_totals(tmp_path):
    store = _session(tmp_path, session="sess-1")
    views = [read_session_view(store, "sess-1")]
    report = aggregate_sessions((SessionRow("sess-1", turns=1, files=1, prompt_tokens=10),))
    html = render_index_html(report, views)
    assert "sess-1" in html and "TOTAL" in html.upper()


def test_r9_a_fifty_turn_session_stays_under_a_megabyte(tmp_path):
    store = TraceStore(tmp_path / "traces")
    for turn in range(1, 51):
        writer = store.begin_turn("big")
        for index in range(20):
            writer.write_line(json.dumps(_row(turn=turn, span=f"c{index}",
                                              data={"tool_name": "bash", "output_preview": "x" * 200})))
    html = render_session_html(read_session_view(store, "big"))
    assert len(html.encode("utf-8")) < 1_000_000
```

- [x] **Step 2: 跑红**

Run: `.venv/bin/python -m pytest tests/test_trace_export_html.py -o addopts="" -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'ai_coding.core.export_html'`

- [x] **Step 3: 最小实现**

```python
"""Static HTML export of a recorded session (R9c).

One file, no external resources, no JavaScript: the trace is already on disk as
JSONL, and what it needs is a *view*.  Everything a trace can hold - tool
parameters, command output, error text - is attacker-adjacent (a tool can be told
to ``echo '<script>…'``), so every dynamic string goes through ``html.escape``:
the page must show code, never run it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any

from ai_coding.core.aggregate import AggregateReport
from ai_coding.core.trace import TraceStore
from ai_coding.ui.trace_render import summarize_turn

__all__ = [
    "SessionView",
    "SpanRow",
    "TurnView",
    "read_session_view",
    "render_index_html",
    "render_session_html",
]

_CSS = """
body { font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; margin: 2rem; color: #222; }
h1 { font-size: 1.3rem; } .kpi { display: inline-block; margin-right: 1.5rem; }
.kpi b { display: block; font-size: 1.1rem; }
details { margin: .6rem 0; border: 1px solid #ddd; border-radius: 4px; padding: .4rem .6rem; }
summary { cursor: pointer; }
table { border-collapse: collapse; width: 100%; margin-top: .4rem; }
th, td { border-bottom: 1px solid #eee; text-align: left; padding: .15rem .4rem; vertical-align: top; }
td.data { white-space: pre-wrap; word-break: break-word; max-width: 60ch; }
.muted { color: #777; }
"""


@dataclass(frozen=True)
class SpanRow:
    ts: str
    span: str
    parent: str
    iter: int
    evt: str
    data: dict[str, Any]


@dataclass(frozen=True)
class TurnView:
    turn: int
    path: Path
    spans: tuple[SpanRow, ...]
    summary: dict[str, Any]
    full: dict[str, Any] | None = None


@dataclass(frozen=True)
class SessionView:
    session: str
    turns: tuple[TurnView, ...]
    summary: dict[str, Any]
    full: bool = False


def _int(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _turn_number(path: Path) -> int:
    stem = path.stem.removeprefix("turn-")
    return int(stem) if stem.isdigit() else 0


def _read_spans(path: Path) -> tuple[SpanRow, ...]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()
    spans: list[SpanRow] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        data = row.get("data")
        spans.append(
            SpanRow(
                ts=str(row.get("ts", "")),
                span=str(row.get("span", "")),
                parent=str(row.get("parent", "")),
                iter=_int(row.get("iter")),
                evt=str(row.get("evt", "")),
                data=dict(data) if isinstance(data, dict) else {},
            )
        )
    return tuple(spans)


def _read_full(path: Path) -> dict[str, Any] | None:
    candidate = path.with_suffix(".full.json")
    if not candidate.is_file():
        return None
    try:
        loaded = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def read_session_view(
    store: TraceStore, session: str, *, full: bool = False
) -> SessionView:
    """Read one session into a view object (no rendering, no policy)."""
    turns: list[TurnView] = []
    for path in sorted(store.session_dir(session).glob("turn-*.jsonl"), key=_turn_number):
        turns.append(
            TurnView(
                turn=_turn_number(path),
                path=path,
                spans=_read_spans(path),
                summary=summarize_turn(path),
                full=_read_full(path) if full else None,
            )
        )
    return SessionView(
        session=session,
        turns=tuple(turns),
        summary=store.read_summary(session),
        full=full,
    )


def _kv_row(key: str, value: Any) -> str:
    body = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    return f"<tr><th>{escape(key)}</th><td class='data'>{escape(body)}</td></tr>"


def render_session_html(view: SessionView) -> str:
    """One self-contained page for one session (R9c).  Pure."""
    summary = view.summary if isinstance(view.summary, dict) else {}
    kpis = "".join(
        f"<span class='kpi'>{escape(label)}<b>{escape(str(_int(summary.get(key, 0))))}</b></span>"
        for key, label in (
            ("turns", "turns"),
            ("steps", "steps"),
            ("prompt_tokens", "prompt"),
            ("completion_tokens", "completion"),
            ("cached_tokens", "cached"),
        )
    )
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        f"<title>trace {escape(view.session)}</title><style>{_CSS}</style></head><body>",
        f"<h1>session {escape(view.session)}"
        f"{' · <span class=\"muted\">includes message payloads</span>' if view.full else ''}</h1>",
        f"<p>{kpis}</p>",
    ]
    for turn in view.turns:
        stats = turn.summary
        head = (
            f"turn {turn.turn} · {escape(str(_int(stats.get('spans', 0))))} spans · "
            f"{escape(str(_int(stats.get('model_steps', 0))))} steps · "
            f"{escape(str(_int(stats.get('tool_calls', 0))))} tools · "
            f"{escape(str(_int(stats.get('model_ms', 0)) + _int(stats.get('tool_ms', 0))))} ms"
        )
        parts.append(f"<details><summary>{head}</summary><table>")
        parts.append("<tr><th>ts</th><th>span</th><th>event</th><th>data</th></tr>")
        for row in turn.spans:
            parts.append(
                "<tr>"
                f"<td>{escape(row.ts)}</td><td>{escape(row.span)}</td>"
                f"<td>{escape(row.evt)}</td><td class='data'>{escape(json.dumps(row.data, ensure_ascii=False))}</td>"
                "</tr>"
            )
        parts.append("</table>")
        if turn.full is not None:
            parts.append(f"<details><summary>message payload</summary><pre>"
                         f"{escape(json.dumps(turn.full, ensure_ascii=False, indent=2))}</pre></details>")
        parts.append("</details>")
    parts.append("</body></html>")
    return "\n".join(parts)


def render_index_html(report: AggregateReport, views: list[SessionView]) -> str:
    """All sessions in one page: totals on top, each session's page inline (R9c)."""
    totals = report.totals
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<title>ai-coding traces</title>",
        f"<style>{_CSS}</style></head><body><h1>ai-coding traces</h1>",
        "<p>"
        f"<span class='kpi'>sessions<b>{len(report.rows)}</b></span>"
        f"<span class='kpi'>TOTAL turns<b>{totals.turns}</b></span>"
        f"<span class='kpi'>steps<b>{totals.steps}</b></span>"
        f"<span class='kpi'>prompt<b>{totals.prompt_tokens}</b></span>"
        f"<span class='kpi'>cached<b>{totals.cached_tokens}</b></span>"
        f"<span class='kpi'>hit%<b>{report.cache_ratio * 100:.1f}%</b></span>"
        "</p>",
    ]
    for view in views:
        parts.append(f"<h2>session {escape(view.session)}</h2>")
        parts.append(render_session_html(view))
    parts.append("</body></html>")
    return "\n".join(parts)
```

- [x] **Step 4: 跑绿 + 门禁**

Run: `.venv/bin/python -m pytest tests/test_trace_export_html.py -o addopts="" -q && .venv/bin/python -m ruff check . && .venv/bin/python -m mypy ai_coding`
Expected: 8 passed / clean

- [x] **Step 5: 提交**

```bash
git add ai_coding/core/export_html.py tests/test_trace_export_html.py
git commit -m "R9c-1: a session renders to one escaped, offline HTML page"
```

---

### Task 8: 从 CLI 导出 HTML

**Files:**
- Modify: `ai_coding/cli.py`
- Test: `tests/test_cli_trace.py`（追加 2 条）

**Interfaces:**
- Consumes: `read_session_view`、`render_session_html`、`render_index_html`、`build_report`
- Produces: CLI `trace export --format html [--session ID | --all] [--out FILE] [--full]`

- [x] **Step 1: 写红测试**

```python
def test_r9_export_html_writes_a_file(tmp_path):
    _session_with(tmp_path, "sess-1", turns=1, summary={"turns": 1})
    out = tmp_path / "trace.html"

    result = runner.invoke(
        app,
        ["trace", "export", "--format", "html", "--session", "sess-1",
         "--workspace", str(tmp_path), "--out", str(out)],
    )

    assert result.exit_code == 0
    body = out.read_text(encoding="utf-8")
    assert "<!doctype html>" in body and "sess-1" in body


def test_r9_export_an_unknown_session_reports_instead_of_crashing(tmp_path):
    result = runner.invoke(
        app,
        ["trace", "export", "--format", "html", "--session", "nope",
         "--workspace", str(tmp_path)],
    )
    assert result.exit_code == 1
    assert "no traces" in result.stdout.lower() or "not found" in result.stdout.lower()
```

- [x] **Step 2: 跑红**

Run: `.venv/bin/python -m pytest tests/test_cli_trace.py -k r9_export -o addopts="" -q`
Expected: FAIL（typer 不认 `export`）

- [x] **Step 3: 最小实现**

`cli.py::trace_command` 在 `overview` 分支之后、`if not session:` 之前加：

```python
    if action == "export":
        from ai_coding.core.aggregate import build_report
        from ai_coding.core.export_html import (
            read_session_view,
            render_index_html,
            render_session_html,
        )

        store = TraceStore(root)
        if all_sessions:
            views = [read_session_view(store, ref.session, full=full)
                     for ref in store.sessions()]
            if not views:
                typer.echo("(no traces recorded yet)")
                return
            body = render_index_html(build_report(store), views)
        else:
            directory = store.session_dir(session)
            if not session or not directory.is_dir():
                typer.echo(f"error: no traces for session '{session or '(none)'}'")
                raise typer.Exit(code=1)
            body = render_session_html(read_session_view(store, session, full=full))
        if out:
            Path(out).write_text(body, encoding="utf-8")
            typer.echo(f"wrote {out}")
            return
        typer.echo(body)
        return
```

新增的三个选项：

```python
    session_id: str = typer.Option(
        "", "--session", "-s", help="Session to export (defaults to the positional one)."
    ),
    all_sessions: bool = typer.Option(False, "--all", help="Export every session."),
    out: str = typer.Option("", "--out", "-o", help="Write to this file instead of stdout."),
    full: bool = typer.Option(False, "--full", help="Include the recorded message payloads."),
```

> `session` 已经是**位置参数**（`trace show <session>`），typer 不允许同一个名字再做成选项，
> 所以新增的是 `--session/-s`（变量名 `session_id`）。`export` 分支开头先做一次归一化：

```python
        session = session_id or session
```

（`--format otlp-json` 在 Task 10 接同一分支；Task 10 会把它写成 `fmt == "otlp-json"` 的判断。）

- [x] **Step 4: 跑绿 + 门禁**

Run: `.venv/bin/python -m pytest tests/test_cli_trace.py -o addopts="" -q`

- [x] **Step 5: 提交**

```bash
git add ai_coding/cli.py tests/test_cli_trace.py
git commit -m "R9c-2: trace export writes the HTML page"
```

---

### Task 9: 轨迹行 → OTLP/JSON

**Files:**
- Create: `ai_coding/core/export_otlp.py`
- Test: `tests/test_trace_export_otlp.py`（新建，7 条）

**Interfaces:**
- Consumes: `SessionView` / `TurnView` / `SpanRow`（Task 7）
- Produces: `rows_to_otlp(views, *, service_name="ai-coding") -> dict[str, Any]`

- [x] **Step 1: 写红测试**

```python
"""TDD (R9d): recorded rows become OTLP/JSON spans — deterministic, no network."""

import json
from pathlib import Path

from ai_coding.core.export_html import SpanRow, SessionView, TurnView, read_session_view
from ai_coding.core.export_otlp import rows_to_otlp
from ai_coding.core.trace import TraceStore


def _row(**over) -> dict:
    base = {
        "v": 1, "ts": "2026-09-28T00:00:00.000Z", "session": "s", "turn": 1,
        "span": "i1", "parent": "turn", "iter": 1, "evt": "IterationStarted", "data": {},
    }
    base.update(over)
    return base


def _view(tmp_path: Path, rows: list[dict]) -> SessionView:
    store = TraceStore(tmp_path / "traces")
    writer = store.begin_turn("s")
    for row in rows:
        writer.write_line(json.dumps(row))
    return read_session_view(store, "s")


def _spans(payload: dict) -> list[dict]:
    return payload["resourceSpans"][0]["scopeSpans"][0]["spans"]


def test_r9_envelope_carries_the_service_and_scope(tmp_path):
    payload = rows_to_otlp([_view(tmp_path, [_row()])])
    resource = payload["resourceSpans"][0]["resource"]
    keys = {attr["key"] for attr in resource["attributes"]}
    assert "service.name" in keys and "ai-coding.session" in keys
    scope = payload["resourceSpans"][0]["scopeSpans"][0]["scope"]
    assert scope["name"] == "ai-coding.trace"


def test_r9_parent_ids_resolve_to_the_parent_span(tmp_path):
    view = _view(tmp_path, [
        _row(span="turn", parent="", evt="TurnFinished"),
        _row(span="i1", parent="turn", evt="IterationStarted"),
        _row(span="step1", parent="i1", evt="ModelStepFinished",
             data={"latency_ms": 120, "usage": {"prompt_tokens": 5}}),
    ])
    spans = {span["name"]: span for span in _spans(rows_to_otlp([view]))}
    turn = spans["TurnFinished"]
    iteration = spans["IterationStarted"]
    step = spans["ModelStepFinished"]
    assert turn["parentSpanId"] == ""
    assert iteration["parentSpanId"] == turn["spanId"]
    assert step["parentSpanId"] == iteration["spanId"]


def test_r9_timestamps_use_nanos_and_latency_sets_the_end(tmp_path):
    view = _view(tmp_path, [_row(evt="ModelStepFinished", data={"latency_ms": 250})])
    span = _spans(rows_to_otlp([view]))[0]
    start = int(span["startTimeUnixNano"])
    assert start > 0
    assert int(span["endTimeUnixNano"]) == start + 250 * 1_000_000


def test_r9_instant_events_are_marked_and_never_zero_length(tmp_path):
    view = _view(tmp_path, [_row()])
    span = _spans(rows_to_otlp([view]))[0]
    attrs = {attr["key"]: attr["value"] for attr in span["attributes"]}
    assert attrs["ai-coding.instant"]["boolValue"] is True
    assert int(span["endTimeUnixNano"]) == int(span["startTimeUnixNano"]) + 1


def test_r9_attribute_types_are_mapped(tmp_path):
    view = _view(tmp_path, [
        _row(evt="ToolFinished", span="c1",
             data={"tool_name": "bash", "success": True, "duration_ms": 42,
                   "params": {"command": "ls"}}),
    ])
    span = _spans(rows_to_otlp([view]))[0]
    attrs = {attr["key"]: attr["value"] for attr in span["attributes"]}
    assert attrs["tool_name"] == {"stringValue": "bash"}
    assert attrs["success"] == {"boolValue": True}
    assert attrs["duration_ms"] == {"intValue": "42"}
    assert attrs["params"] == {"stringValue": '{"command": "ls"}'}  # nested → JSON text


def test_r9_a_tool_error_becomes_an_error_status(tmp_path):
    view = _view(tmp_path, [_row(evt="ToolFinished", span="c1", data={"error": "nonzero_exit"})])
    span = _spans(rows_to_otlp([view]))[0]
    assert span["status"]["code"] == 2


def test_r9_the_export_is_deterministic(tmp_path):
    view = _view(tmp_path, [_row(), _row(span="c1", parent="i1", evt="ToolStarted")])
    first = json.dumps(rows_to_otlp([view]), sort_keys=True)
    second = json.dumps(rows_to_otlp([view]), sort_keys=True)
    assert first == second
```

- [x] **Step 2: 跑红**

Run: `.venv/bin/python -m pytest tests/test_trace_export_otlp.py -o addopts="" -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'ai_coding.core.export_otlp'`

- [x] **Step 3: 最小实现**

```python
"""OTLP/JSON export of recorded spans (R9d).

One OTLP span per *trace span* (not per event): a turn's rows are grouped by
``span``, so ``i1`` becomes one span with its events as attributes and its child
``step1``/``<call_id>`` pointing at it through ``parentSpanId``.  Ids are derived
from ``session|turn|span`` with sha256, so exporting the same trace twice is
byte-identical - real OTel SDKs generate random ids; this is a deliberate
difference (see docs/r9-analysis.md 2.2).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from ai_coding.core.export_html import SessionView, SpanRow

__all__ = ["rows_to_otlp"]

_SPAN_ID_CHARS = 16
_TRACE_ID_CHARS = 32


def _int(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _hash(value: str, length: int) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _nano(ts: str) -> int:
    try:
        moment = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return int(moment.timestamp() * 1_000_000_000)


def _value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    return {"stringValue": json.dumps(value, ensure_ascii=False)}


def _attributes(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten one row's ``data``; ``None`` is dropped, nesting becomes JSON text."""
    return [
        {"key": str(key), "value": _value(value)}
        for key, value in sorted(payload.items())
        if value is not None
    ]


def _span_id(session: str, turn: int, span: str) -> str:
    return _hash(f"{session}|{turn}|{span}", _SPAN_ID_CHARS)


def rows_to_otlp(
    views: Sequence[SessionView], *, service_name: str = "ai-coding"
) -> dict[str, Any]:
    """Render sessions as OTLP/JSON.  Pure; no network (D-R9-5)."""
    resources: list[dict[str, Any]] = []
    for view in views:
        spans: list[dict[str, Any]] = []
        for turn in view.turns:
            grouped: dict[str, list[SpanRow]] = {}
            for row in turn.spans:
                grouped.setdefault(row.span, []).append(row)
            for span_name, rows in grouped.items():
                events = [row.evt for row in rows]
                start = min(_nano(row.ts) for row in rows)
                duration_ms = max(
                    max(_int(row.data.get("latency_ms")), _int(row.data.get("duration_ms")))
                    for row in rows
                )
                instant = duration_ms <= 0
                failed = any(row.data.get("error") for row in rows)
                parent = rows[0].parent
                attributes: list[dict[str, Any]] = [
                    {"key": "ai-coding.turn", "value": {"intValue": str(turn.turn)}},
                    {"key": "ai-coding.span", "value": {"stringValue": span_name}},
                    {"key": "ai-coding.events", "value": {"stringValue": "|".join(events)}},
                ]
                if instant:
                    attributes.append(
                        {"key": "ai-coding.instant", "value": {"boolValue": True}}
                    )
                for row in rows:
                    attributes.extend(_attributes(row.data))
                spans.append(
                    {
                        "traceId": _hash(f"{view.session}|{turn.turn}", _TRACE_ID_CHARS),
                        "spanId": _span_id(view.session, turn.turn, span_name),
                        "parentSpanId": (
                            ""
                            if not parent or parent == "turn" and span_name == "turn"
                            else _span_id(view.session, turn.turn, parent)
                        ),
                        "name": "|".join(events),
                        "kind": 1,  # SPAN_KIND_INTERNAL
                        "startTimeUnixNano": str(start),
                        "endTimeUnixNano": str(
                            start + duration_ms * 1_000_000 if duration_ms else start + 1
                        ),
                        "attributes": attributes,
                        "status": {"code": 2 if failed else 0},
                    }
                )
        resources.append(
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": service_name}},
                        {
                            "key": "ai-coding.session",
                            "value": {"stringValue": view.session},
                        },
                    ]
                },
                "scopeSpans": [
                    {"scope": {"name": "ai-coding.trace", "version": "1"}, "spans": spans}
                ],
            }
        )
    return {"resourceSpans": resources}
```

- [x] **Step 4: 跑绿 + 门禁**

Run: `.venv/bin/python -m pytest tests/test_trace_export_otlp.py -o addopts="" -q && .venv/bin/python -m ruff check . && .venv/bin/python -m mypy ai_coding`
Expected: 7 passed / clean

- [x] **Step 5: 提交**

```bash
git add ai_coding/core/export_otlp.py tests/test_trace_export_otlp.py
git commit -m "R9d-1: rows become OTLP/JSON spans, deterministically"
```

---

### Task 10: 从 CLI 导出 OTLP/JSON

**Files:**
- Modify: `ai_coding/cli.py`
- Test: `tests/test_cli_trace.py`（追加 1 条）

**Interfaces:**
- Consumes: `rows_to_otlp`、`read_session_view`
- Produces: `trace export --format otlp-json`（与 `html` 共用 `--session/--all/--out`）

- [x] **Step 1: 写红测试**

```python
def test_r9_export_otlp_json_is_stable(tmp_path):
    _session_with(tmp_path, "sess-1", turns=1, summary={"turns": 1})
    args = ["trace", "export", "--format", "otlp-json", "--session", "sess-1",
            "--workspace", str(tmp_path)]

    first = runner.invoke(app, args)
    second = runner.invoke(app, args)

    assert first.exit_code == 0
    payload = json.loads(first.stdout)
    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert spans and all("spanId" in span for span in spans)
    assert first.stdout == second.stdout  # deterministic ids
```

- [x] **Step 2: 跑红**

Run: `.venv/bin/python -m pytest tests/test_cli_trace.py -k r9_export_otlp -o addopts="" -q`
Expected: FAIL（`otlp-json` 还没接）

- [x] **Step 3: 最小实现**

Task 8 的 `export` 分支里，把「取 `body`」的那一段改成按 `fmt` 分派：

```python
        from ai_coding.core.export_otlp import rows_to_otlp

        if fmt == "otlp-json":
            if all_sessions:
                views = [read_session_view(store, ref.session) for ref in store.sessions()]
            else:
                views = [read_session_view(store, session)] if store.session_dir(session).is_dir() else []
            if not views:
                typer.echo(f"error: no traces for session '{session or '(none)'}'")
                raise typer.Exit(code=1)
            body = json.dumps(rows_to_otlp(views), ensure_ascii=False, indent=2)
        elif all_sessions:
            ...
```

> 实现提示：`export` 分支里「html」与「otlp-json」共用读盘与写文件，只有**渲染函数**不同；
> 把渲染抽成一个局部函数比复制两遍 if 更清楚。

- [x] **Step 4: 跑绿 + 门禁**

Run: `.venv/bin/python -m pytest tests/test_cli_trace.py -o addopts="" -q && .venv/bin/python -m mypy ai_coding`

- [x] **Step 5: 提交**

```bash
git add ai_coding/cli.py tests/test_cli_trace.py
git commit -m "R9d-2: trace export speaks OTLP/JSON"
```

---

### Task 11: 验收脚本 + 六份文档

**Files:**
- Create: `scripts/r9_cases.py`
- Modify: `docs/features.md`、`docs/data-contracts.md`、`docs/architecture.md`、`docs/how-to-run.md`、`docs/integration_testing.md`、`docs/r7-backlog.md`（并给本文件加「执行状态」）

**Interfaces:**
- Produces: `scripts/r9_cases.py`（离线 12 + `--real` 1），文档与 backlog 同步

- [x] **Step 1: 写验收脚本**

沿用 `scripts/r8_cases.py` 的形状（一个 case 一个 async 函数，返回 `(ok, detail)`）。离线 12 条：

```python
async def gate_refuses_completion(tmp: Path) -> tuple[bool, str]:
    task = TaskState(goal="g")
    tool = TodoWriteTool()
    ctx = ToolContext(task=task)
    await tool.execute({"action": "add", "content": "dep"}, ctx)
    await tool.execute({"action": "add", "content": "next", "depends_on": ["1"]}, ctx)
    result = await tool.execute({"action": "update", "id": "2", "status": "completed"}, ctx)
    ok = result.error == "blocked_by_dependencies" and task.get("2").status == "pending"
    return ok, f"error={result.error} status={task.get('2').status}"


async def gate_refuses_start(tmp: Path) -> tuple[bool, str]:
    ... same shape with status="in_progress" ...


async def gate_unblocks_after_completion(tmp: Path) -> tuple[bool, str]:
    ... complete dep, then the item completes ...


async def gate_allows_after_drop(tmp: Path) -> tuple[bool, str]:
    ... update dep to dropped, then the item completes ...


async def add_refuses_unknown_and_self(tmp: Path) -> tuple[bool, str]:
    ... "9" → unknown_dependency; "1" on an empty list → self_dependency ...


async def the_forward_reference_cycle_is_gone(tmp: Path) -> tuple[bool, str]:
    """The R9 analysis measured that this used to succeed."""
    task = TaskState(goal="g")
    tool, ctx = TodoWriteTool(), ToolContext(task=task)
    first = await tool.execute({"action": "add", "content": "a", "depends_on": ["2"]}, ctx)
    ok = first.error == "unknown_dependency" and task.items == []
    return ok, f"error={first.error} items={len(task.items)}"


def _fake_store(tmp: Path) -> TraceStore:
    store = TraceStore(tmp / "traces")
    writer = store.begin_turn("sess-1")
    writer.write_line(json.dumps({...one ToolStarted row with a <script> payload...}))
    (store.session_dir("sess-1") / "summary.json").write_text(
        json.dumps({"turns": 1, "steps": 1, "prompt_tokens": 10,
                    "completion_tokens": 2, "cached_tokens": 5}), encoding="utf-8")
    return store


async def overview_totals(tmp: Path) -> tuple[bool, str]:
    report = build_report(_fake_store(tmp))
    ok = report.totals.turns == 1 and abs(report.cache_ratio - 0.5) < 1e-9
    return ok, f"turns={report.totals.turns} hit={report.cache_ratio:.2f}"


async def html_escapes_tool_output(tmp: Path) -> tuple[bool, str]:
    store = _fake_store(tmp)
    body = render_session_html(read_session_view(store, "sess-1"))
    ok = "&lt;script&gt;" in body and "<script>" not in body
    return ok, f"{len(body)} chars"


async def html_has_no_external_resources(tmp: Path) -> tuple[bool, str]:
    body = render_session_html(read_session_view(_fake_store(tmp), "sess-1"))
    ok = not any(token in body for token in ("http://", "https://", "<script", "<link"))
    return ok, "clean" if ok else "external resource found"


async def otlp_is_deterministic(tmp: Path) -> tuple[bool, str]:
    view = read_session_view(_fake_store(tmp), "sess-1")
    first = json.dumps(rows_to_otlp([view]), sort_keys=True)
    second = json.dumps(rows_to_otlp([view]), sort_keys=True)
    return first == second, f"{len(first)} chars"


async def prune_and_overview_agree(tmp: Path) -> tuple[bool, str]:
    """Both must use the directory mtime (D-R9-8)."""
    store = _fake_store(tmp)
    stale = time.time() - 30 * 86400
    os.utime(store.session_dir("sess-1"), (stale, stale))
    old = build_report(store, since_days=7).rows == ()
    return old and store.prune() == 1, "consistent"
```

外加 `--real`：跑一个真模型回合（复用 `scripts/r8_cases.py::_real_loop` 的搭法），
断言轨迹目录里至少有一个会话、且 `build_report` 把它算进去。

- [x] **Step 2: 跑验收**

Run: `.venv/bin/python scripts/r9_cases.py`
Run: `.venv/bin/python scripts/r9_cases.py --real --config config.local.yaml`
Expected: 离线 12/12；真实 1/1

- [x] **Step 3: 更新文档**

按 `docs/r9-analysis.md` §12 的清单逐条改：`features.md`（依赖门禁 + 三个观测出口）、
`data-contracts.md`（§5 那些新契约）、`architecture.md`（「一个数据源、三个出口」+ 策略/领域边界）、
`how-to-run.md`（新命令与 `ai.enforce_dependencies`）、`integration_testing.md`（脚本跑法）、
`r7-backlog.md`（3.5 与 5.1/5.2/5.3 划掉并指向本文）。

- [x] **Step 4: 全量门禁**

Run: `.venv/bin/python -m pytest tests/ -o addopts="" -q && .venv/bin/python -m ruff check . && .venv/bin/python -m mypy ai_coding`
Expected: 全绿（用例数从 1073 起只增不减）

- [x] **Step 5: 提交**

```bash
git add -A
git commit -m "R9: the acceptance script, and the docs the round changed"
```

---

## Self-Review

**1. Spec coverage**（`docs/r9-analysis.md` → 本计划）

| Spec | Task |
|---|---|
| §3.1 R9a 契约（`Blockers` / `blockers()` / `dependency_invariant()`） | 1 |
| §3.1 工具层三条规则（add 校验、多状态门禁、错误信息三出路） | 2 |
| §3.1 开关与接线（`ai.enforce_dependencies`） | 3 |
| §3.2 `TraceStore.sessions()` + prune 同口径 | 4 |
| §3.2 纯函数聚合 + `mismatch` + `cache_ratio` + `--since` | 5, 6 |
| §3.3 静态 HTML（转义 / 无外链 / 默认不含全文 / 体积） | 7, 8 |
| §3.4 OTLP/JSON（三层信封 / 确定性 id / 时间与状态映射） | 9, 10 |
| §6.2 不变量（观测只读、格式冻结、对账优先） | 4–10（用例钉住） |
| §7 的 41 条用例 | 1–10（见下） |
| §11 D-R9-1…D-R9-9 | Global Constraints |
| §12 文档同步 | 11 |

**用例数**：本计划实排 **49 条**（Task 1: 6、Task 2: 9、Task 3: 1、Task 4: 5、Task 5: 5、
Task 6: 5、Task 7: 8、Task 8: 2、Task 9: 7、Task 10: 1）。比分析文档估的 41 多 8 条：
**接线 1**（Task 3）、**会话与 CLI 边界 6**（最新优先、根目录不存在、表格、`--since`、
空工作区、`mismatch` 标注）、**OTLP 确定性 1**——都是写用例时才发现必须单独钉住的边界。

**2. Placeholder scan**：无 TBD/TODO；每个 Step 都有可执行的测试代码、实现代码或命令。
Task 8/10 的 CLI 分支给了完整代码；Task 11 的脚本骨架里用 `...` 省略的四处都是同一形状的重复体
（与紧邻的完整例子行数相同），实现时照抄即可。

**3. Type consistency**：`Blockers`、`TaskState.next_id()`、`TodoWriteTool(enforce_dependencies=)`、
`AIConfig.enforce_dependencies`、`SessionRef`、`TraceStore.sessions()`、`SessionRow`、
`AggregateReport`、`aggregate_sessions`、`build_report`、`SpanRow`、`TurnView`、`SessionView`、
`read_session_view`、`render_session_html`、`render_index_html`、`rows_to_otlp`、`render_overview`
在 Task 1–11 中的引用与首次定义完全一致；错误码字符串 `unknown_dependency` / `self_dependency` /
`blocked_by_dependencies` 只在这三处出现。

## Execution Handoff

用户已明确选择 **Inline Execution**（「不准派子任务，自己实现，或者就用主模型」），
并按 Task 1 → 11 顺序执行，每个 Task 先跑红、再实现、跑绿、单独提交。

---

## 执行状态（2026-09-28，本轮已完成）

**11 个 Task 全部落地，每一步都先跑红再实现，一个 Task 一个提交。既有测试断言零改动。**

| Task | 提交 | 红 → 绿 | 结果 |
|---:|---|---|---|
| 1 | `3aefeda` | `ImportError: Blockers` → **24 passed** | `Blockers` / `blockers()` / `dependency_invariant()` / `next_id()` |
| 2 | `eac9c4f` | 9 failed（`TypeError: unexpected keyword`）→ **20 passed** | add 校验 + 状态门禁 + 三条出路 |
| 3 | `026b70a` | 第二个断言 failed（`_enforce` 仍是 `True`）→ **8 passed** | `ai.enforce_dependencies` + builder 接线 |
| 4 | `ac5c178` | `AttributeError: sessions` → **17 passed** | `SessionRef` / `TraceStore.sessions()` / `prune` 同口径 |
| 5 | `303ee30` | `ModuleNotFoundError: aggregate` → **5 passed** | `SessionRow` / `AggregateReport` / `build_report` |
| 6 | `8da0334` | `exit_code == 2` → **16 passed** | `render_overview` + `trace overview --since/--format` |
| 7 | `00be25f` | `ModuleNotFoundError: export_html` → **8 passed** | 单文件 HTML（全转义、无外链、`--full` 才含全文） |
| 8 | `660f5ab` | `exit_code == 2` → **13 passed** | `trace export --format html --session/--all/--out/--full` |
| 9 | `c2dd61a` | `ModuleNotFoundError: export_otlp` → **7 passed** | `rows_to_otlp`（确定性 id、span 树、状态映射） |
| 10 | `65f6ed4` | `JSONDecodeError`（还是 HTML）→ **14 passed** | `trace export --format otlp-json` |
| 11 | 见下 | — | `scripts/r9_cases.py` + 六份文档 |

**全量门禁（收尾实测）**：`pytest tests/ -o addopts="" -q` = **1122 passed / 4 skipped**
（1073 → 1122，正好 +49，与 Self-Review 的用例数一致）；`ruff check .` = 0；`mypy ai_coding` = 0（98 文件）。
`scripts/r9_cases.py` = **12/12**；`--real --config config.local.yaml` = **13/13**
（真模型那一条：`answer=106 chars, sessions=1, turns=1, prompt=11723`）。

### 与计划的偏差（全部记录在案）

1. **HTML 体积判据被证实**：计划里「50 回合 < 1MB」是估的，实测 **389,771 字节**（50 回合 × 20 span），
   余量约 2.5×。用法例 + 一次真跑各钉一遍。
2. **`ruff` 的 `target-version = py310` 不接受 PEP 701 的 f-string**：计划里
   `f"{' · <span class=\"muted\">…' if view.full else ''}"` 在 3.10 语法下是非法的
   （`Cannot use an escape sequence in f-strings on Python 3.10`）。改成先算 `note` 变量再插值，
   输出逐字节不变。
3. **`_kv_row` 删掉**：计划里那个私有辅助函数最终没有任何调用点（表格渲染走的是逐行拼
   `<td class='data'>`），留着就是死代码，实现时移除；不影响任何用法例。
4. **`SessionRef` 用 `@dataclasses.dataclass`**：`core/trace.py` 是 `import dataclasses`（不是
   `from dataclasses import dataclass`），照抄计划片段会 `NameError`（已实测）。语义不变。
5. **`tests/test_trace_export_html.py` 三处换行**：计划里的测试片段有 3 行 >100 字符（`ruff` E501）。
   只换了行，**断言一条没动**。
6. **验收脚本的第 12 条**：计划列举了 11 个具名的离线用例，但目标写的是 12 条；补上的那条是
   `gate_can_be_switched_off`（D-R9-3 的开关），与门禁的其余 6 条同源。
7. **Task 8 / Task 10 的 CLI 分支**：计划提示「把渲染抽成一个局部函数」，实现时用的是
   `fmt == "otlp-json"` 先行、其余走 HTML 的 `if/elif` 分派；行为与计划一致（同一个 `store`、
   同一套 `--session/--all/--out/`）。
