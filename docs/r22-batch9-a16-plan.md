# 批次 9-1 方案（R22·A-16 子代理 span 与父循环 span 同名并组）

> 只出方案，不改代码。依据：`docs/r22-feature-matrix.md` §8 A-16、
> `docs/data-contracts.md:620-629`、`core/trace.py:688-706 / 778-807`、
> 真实轨迹 `/workspace/eval-workspace/r22-s5/.ai-coding/traces/dc6b6368…/turn-1.jsonl`。

## 1. 调研：合并发生在哪、证据是什么

### 1.1 轨迹层的现状（已存在，不动）

`TraceRecorder._describe`（`core/trace.py:778-807`）为子代理事件返回：
`span` 取自子循环内部（`i1`/`step1`/`call_x`/`turn`），`parent` 取自 `ScopedEvent.parent`
（= 父循环里 `subAgent` 那次调用的 call id），`scope = "subagent:<name>:<call_id>"`。
真实行示例（s5 轨迹）：

```
IterationStarted span=i1 parent=call_fd99… scope=subagent:scout:call_fd99…
ModelStepFinished span=step1 parent=call_fd99… scope=subagent:scout:call_fd99…
TurnFinished     span=turn parent=call_fd99… scope=subagent:scout:call_fd99…
ToolStarted      span=call_fd99… parent=i2   scope=""            ← 父循环里创建子代理的那次调用
```

### 1.2 导出层的现状（缺陷点）

`core/export_html.py::_read_spans` 只读 `ts/span/parent/iter/evt/data`，**把 `scope` 丢掉了**；
`export_otlp.py:80-82` 再按 `row.span` 单键分组。于是：
- 父、子同名 span（`i1`/`i2`/`step1`/`turn`…）被并成一组；
- `parentSpanId` 取组内**先出现**那行的 `parent`（父循环的），子 span 全部失去自己的身份；
- 一方的 `error` 会把整组标成 `status.code=2`；
- `start` 取两者最小、`duration` 取最大，父子的时间被揉在一起。

**实测（s5 真实轨迹 47 行）**：按 `span` 分组是 **14** 个 span；按 `(scope, span)` 是 **21** 个。
被并掉的 7 个键：`i1 i2 i3 step1 step2 step3 turn`，每个都同时出现在 `""` 与
`subagent:scout:call_fd99…` 两个 scope 里。

### 1.3 为什么"只改导出"不够（已拍板：连内部树一起还原）

轨迹文件带 `scope`（`core/trace.py:700-701`），但**父循环与子循环的内部层级在写入时就被拍平了**：
`_describe` 对 `ScopedEvent` 直接返回 `event.parent`（= `subAgent` 的 call id），把递归算出来的
`inner_parent`（`step1→i1`、`i1→turn`）丢掉了（`core/trace.py:786` 的 `_parent` 未使用）。
所以子代理内部永远是一层星形结构；导出层再怎么分组也还原不出 `step1→i1`。

拍板结论：**一起还原内部树**（轨迹语义变更，但格式不变——只是 `parent` 的值更准确），
导出层同时兼容两类历史轨迹：
- 旧轨迹：所有子行 `parent = subAgent call id`（星形）；
- 新轨迹：子循环内部 `i1→turn`、`step1→i1`、`call_x→i{iter}`，只有子 `turn` 的父是 call id。

## 2. 现有设计与契约（复用什么）

| 既有契约 | 位置 | 本轮怎么用 |
| --- | --- | --- |
| 轨迹行 `scope` | `core/trace.py:700-701` | 原样读取，不新增字段 |
| `_describe` 的递归 `inner_parent` | `core/trace.py:778-787` | 子循环根挂 call id，其余挂内部父 |
| `SpanRow` / `_read_spans` | `core/export_html.py:231-298` | 加一个 `scope` 字段并填充 |
| `rows_to_otlp` | `core/export_otlp.py:72-147` | 只改分组键、spanId 派生与父解析 |
| 确定性 id | `_span_id` = `sha256(session\|turn\|span)[:16]` | **无 scope 时保持原串**，历史导出字节不变 |
| 属性契约 | `ai-coding.turn/span/events`、`instant`、`data` 展平 | 追加 `ai-coding.scope`（仅 scope 非空） |
| `status` 语义 | 任一行 `error` → `code=2` | 改为"组内任一行"，分组修好后自然隔离 |
| OTel 语义 | 一个 turn 一个 trace，span 通过 `parentSpanId` 连树 | 子代理 span 的父指回父循环的 `subAgent` call span |

## 3. 方案（四处，按依赖顺序）

### 3.0 轨迹写入：还原子循环内部层级（`core/trace.py`）

```python
if isinstance(event, ScopedEvent):
    scope = event.scope
    name, payload, span, inner_parent, iteration, _ = self._describe(event.event)
    # 子循环内部有自己的层级（step1→i1、i1→turn）；只有子循环的根（TurnFinished，
    # inner_parent 为空）才挂到产生它的 subAgent call 上。
    return name, payload, span, inner_parent or event.parent, iteration, scope
```

只改 `_describe` 一处，格式不变（`span/parent/scope` 三个字段都在），历史轨迹仍可读。
`ScopedEvent` 本身不动（内部父可由递归得出，不新增字段）。

### 3.1 `SpanRow` 带上 `scope`

```python
@dataclass(frozen=True)
class SpanRow:
    ts: str
    span: str
    parent: str
    iter: int
    evt: str
    data: dict[str, Any]
    scope: str = ""          # 末位默认值：现有构造点只有 _read_spans
```

`_read_spans` 增加 `scope=str(row.get("scope", ""))`（缺字段的历史轨迹兼容）。

### 3.2 `rows_to_otlp` 按 `(scope, span)` 分组

```python
grouped: dict[tuple[str, str], list[SpanRow]] = {}
for row in turn.spans:
    grouped.setdefault((row.scope, row.span), []).append(row)
```

每个 turn 先建一次 `scopes_by_span: dict[str, set[str]]`（`span` 名 → 出现过的 scope），
用于父解析。

### 3.3 `spanId` 派生与父解析

```python
def _span_id(session: str, turn: int, span: str, scope: str = "") -> str:
    # 无 scope 时与旧串逐字一致（历史导出不变）；有 scope 时把作用域编进哈希。
    key = f"{session}|{turn}|{span}" if not scope else f"{session}|{turn}|{scope}|{span}"
    return _hash(key, _SPAN_ID_CHARS)
```

父解析规则（纯函数）：

```python
#: 子循环自己的 span 名。带 scope 的行若父名是这些，就留在同 scope——
#: 即使父行被截断丢失，也不能挂到根循环同名 span 上。
_INNER_PARENT = re.compile(r"^(?:turn|i\d+|step\d+)$")


def _parent_scope(name: str, own_scope: str, scopes_by_span) -> str:
    if not name or not own_scope:          # 根 scope 的父永远在同层
        return ""
    # 新轨迹：子循环的 i1/step1 的父就在同一 scope 里。
    if own_scope in scopes_by_span.get(name, set()):
        return own_scope
    if _INNER_PARENT.match(name):
        return own_scope                 # 父行被截断：悬空但不串到根树
    candidates = scopes_by_span.get(name, set()) - {own_scope}
    if not candidates:
        return own_scope
    if "" in candidates:
        return ""                        # 子代理的根 / 旧星形父：subAgent call 在根 scope
    return sorted(candidates)[0]         # 嵌套子代理：父在上层子 scope
```

`parentSpanId`：

```python
parent = rows[0].parent
if not parent or (parent == "turn" and span_name == "turn" and not scope):
    parent_span_id = ""
else:
    parent_span_id = _span_id(session, turn, parent, _parent_scope(parent, scope, scopes_by_span))
```

- 根 `turn`：`parent=""` → 空（与今天一致）。
- 根 `i1`：`parent="turn"`，同 scope 命中 → 根 `turn` id（与今天一致）。
- 子 `turn`（新）：`parent=call_fd99…`，子 scope 里没有该名字 → 根 scope 的 call span id。
- 子 `i1`（新）：`parent="turn"`，同 scope 命中 → 子 `turn` id。
- 子 `step1`（新）：`parent="i1"`，同 scope命中 → 子 `i1` id。
- 旧轨迹子行：`parent=call_fd99…` → 根 scope，与今天一致（兼容）。

### 3.4 属性

```python
if scope:
    attributes.append({"key": "ai-coding.scope", "value": {"stringValue": scope}})
```

保留 `ai-coding.span` 为内部 span 名（`i1` 等）。OTel 允许同名 span，两个子代理/父子 span
靠 `spanId` 与 `ai-coding.scope` 区分。整条 `scope` 字符串原样保留（`subagent:<name>:<call>`），
不拆字段——拆了就多一个解析契约，没人消费。

## 4. 数据流

```
turn-N.jsonl
   │  _read_spans（新增 scope 透传）
   ▼
SpanRow(ts, span, parent, scope, evt, data)
   │  scopes_by_span = span -> {scope…}           （每个 turn 一次）
   ▼
group by (scope, span)
   │  spanId  = sha256(session|turn|[scope|]span)
   │  parent  = rows[0].parent
   │  parentScope = _parent_scope(parent, scope, scopes_by_span)
   │  parentSpanId = sha256(session|turn|[parentScope|]parent)
   ▼
resourceSpans[0].scopeSpans[0].spans
```

影响面：
- **轨迹写入**：新跑出来的子代理行 `parent` 由"统一 call id"改为内部层级；字段与格式不变，
  历史轨迹按旧形状原样可读；前端 trace 面板读事件流、不读 JSONL，不受影响。
- **OTLP 导出**：span 数、id、父子关系变化；
- HTML 导出不变（`SpanRow` 新字段不参与渲染，`fold_turn` 只读 evt/data）；
- 无子代理的会话：spanId 与输出**逐字节不变**（回归测试锁死）。

## 5. RIPR（Red → Implement → Prove → Refactor，缺一不可）

### Red

写入层（`tests/test_trace_recorder.py`）：

| 用例 | 断言（期望行为） |
| --- | --- |
| `test_a16_scoped_rows_keep_their_inner_hierarchy` | 子 scope 的 `(span,parent)`：`("i1","turn")`、`("step1","i1")`、`("child-1","i1")`、`("turn","c1")`（今天全是 `c1`） |

导出层（`tests/test_trace_export_otlp.py`）：

| 用例 | 断言（期望行为） |
| --- | --- |
| `test_a16_a_subagent_span_is_not_merged_into_its_parent` | 根 `i1` + 子 scope `i1` → 2 个 span；同名；`spanId` 不同；子 span 带 `ai-coding.scope` |
| `test_a16_the_subagent_root_points_at_the_spawning_call` | 根 `call_fd` + 子 `turn`（parent=`call_fd`）→ 子 `turn.parentSpanId == 根 call_fd.spanId`，≠ 根 `turn.spanId` |
| `test_a16_the_subagent_inner_tree_survives_export` | 新轨迹形状（子 `i1→turn`、`step1→i1`、`call→i1`）→ 导出后三条 `parentSpanId` 分别指向子 `turn`/子 `i1`/子 `i1` |
| `test_a16_old_flat_subagent_rows_still_export` | 旧形状（子行全挂 `call_fd`，含子 `turn`）→ 均可导出，父指向根 `call_fd`（历史轨迹兼容） |
| `test_a16_a_truncated_child_parent_stays_in_its_own_scope` | 子 `turn` 行缺失时，子 `i1` 的父 id 保持在本 scope，不落到根 `turn` |
| `test_a16_each_scope_keeps_its_own_status` | 根 `i1` 无 error、子 `i1` 有 `error` → 根 `status.code == 0`、子 `== 2` |
| `test_a16_unscoped_span_ids_are_unchanged` | 无 scope 行 `spanId` 等于旧公式 `sha256("s|1|i1")[:16]` |
| `test_a16_two_scopes_with_the_same_name_get_distinct_ids` | 两个不同 `subagent:*` 的 `i1` → 3 个不同 id（含根） |

`scripts/trace_cases.py::c10` 的期望要同步改（它现在断言 `parents == {"c1"}`，正是被拍板的旧行为）：
改为断言四条内部父子关系；这是"测试用例写的是旧设计"，不是为通过而放宽。

### Implement

按 §3.0 → §3.1 → §3.2/3.3/3.4 顺序；一次只改一个文件，跑一次目标测试。

### Prove

1. `pytest tests/test_trace_recorder.py tests/test_trace_export_otlp.py tests/test_trace_export_html.py -q` 全绿；
2. `pytest tests/test_trace_cases_smoke.py -q`（c10 新期望）；
3. 离线真数据：用 s5 的旧形状 `turn-1.jsonl` 跑 `read_session_view` + `rows_to_otlp`，
   断言 **21 个 span**、无 `(scope,span)` 重复、子 `turn.parentSpanId == 根 call_fd spanId`；
4. 对照无子代理会话（r22-s1 任一轨迹）导出，spanId 与改前一致（脚本对比）；
5. `ruff` / `mypy` 干净。

### Refactor

`_parent_scope` 与 `_span_id` 都是纯函数、无状态；`rows_to_otlp` 内只保留一次分组与一次
`scopes_by_span` 构建，不引入类/缓存/新模块。若评审发现 helper 只有一处调用且读不出价值，
直接内联（最小化）。

## 6. 边界与不修

- **嵌套子代理**：`_parent_scope` 在父 span 名只出现在一个上层 scope 时能选对；同名跨层
  且父名恰好是 `i1` 的极端情形不处理（现实中 `parent` 是唯一 call id）。
- **HTML/trace 面板**：不展示 scope，保持现状。
- **`scopeSpans` 不按 scope 拆分**：`scope` 在 OTel 里是 instrumentation scope，不是运行作用域；
  子代理用属性表达，避免把所有消费方（含未来 OTLP 后端）引到错误语义。
- **旧轨迹不再回填空缺的内部父**：历史文件已经写死星形；导出按旧形状兼容，不重写文件。

## 7. 验收（含真机）

1. 单测 + 离线真数据（§5 Prove）；
2. 真机（新轨迹）：在 `r22-s5` 工作区起 TUI，让模型用 `subAgent` 跑一个小任务，随后 `/export otlp`，
   检查导出文件：子行 `i1/step1/call` 的 `parentSpanId` 分别指向子 `turn/i1/i1`、
   子 `turn` 指向父 `subAgent` call、`spanId` 无重复；
3. 真机（旧轨迹兼容）：同一工作区直接 `/export otlp`（历史文件），21 个 span、无并组；
4. 全量 `pytest` / `ruff` / `mypy`；
5. 文档：`data-contracts.md:622` 改写、`r9-analysis.md` span 分组说明同步、矩阵 A-16 划掉、
   队列进度更新。

## 8. 完成记录（实现 → 评审 → 修复 → 复审 → 真机）

**实现**（4 个源文件 + 测试）：
- `core/trace.py::_describe`：`inner_parent or event.parent`，子循环内部树恢复；
- `core/export_html.py`：`SpanRow.scope`（末位默认值）+ `_read_spans` 透传；
- `core/export_otlp.py`：`(scope, span)` 分组、`_span_id` 带 scope（无 scope 旧串不变）、
  `_parent_scope`、`ai-coding.scope` 属性；
- `scripts/trace_cases.py::c10`：星形旧断言改为六对内部关系精确集合。

**评审两轮**：第一轮指出 c10 用子集断言会掩盖缺陷、文档未同步；已改为精确集合并同步
`data-contracts.md` / `features.md` / `r9-analysis.md` / 矩阵。第二轮追加 `_INNER_PARENT`
（子循环内部父名留同 scope，父行截断时不串到根树）及对应测试，复审通过。

**测试**：trace 相关 7 个文件 96 例绿；ruff / mypy 干净。

**真机**（r22-s5，TUI）：
- 旧轨迹 `/export otlp`：24 span / 24 unique id / 8 个 scoped，子 `turn` 指向 spawn call；
- 让模型真跑一次 `subAgent`（call_eddfc…）后 `/export otlp`：14 span / 14 unique id，
  子 `turn→call`、子 `i1→turn`、`step1→i1`、子 `call→i2`、根 `turn` 父为空；
- 原始轨迹行（`f20ac384…/turn-1.jsonl`）的 16 条 scoped 行呈现完整内部树。
证据：`/tmp/r22-shots/158-a16-real.txt`、导出物 `traces-20261029-1724/1725*.json`。
