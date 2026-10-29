# 批次 8-2 方案（R22·A-13 OTLP span 名）

> 只出方案，不改代码。依据：`docs/r22-batch8-queue.md` 第 2 项、`docs/r22-feature-matrix.md` §5 A-13、
> 以及已有导出物 `/workspace/eval-workspace/r22-s1/.ai-coding/exports/traces-20261029-121135.json`。

## 1. 调研

### 1.1 现状

`core/export_otlp.py:83-121`：一个 trace span（`row.span` 分组，如 `turn`/`i1`/`step1`/`<call_id>`）
对应一个 OTLP span，但 `name` 用的是**该 span 下所有事件名的拼接**：

```python
"name": "|".join(events),
```

导出物实测（41 个 span）：

| 长度 | name 前缀 |
| --- | --- |
| 160 | `IterationStarted\|ContextAssembled\|AssistantMessage\|ToolApprovalRequested\|JobChanged\|JobCha…` |
| 17 | `ModelStepFinished` |
| 24 | `ToolStarted\|ToolFinished` |
| 50 | `IterationStarted\|ContextAssembled\|AssistantMessage` |

同一 span 里的事件越多，名字越长；同一个 span 类型（`i1`）在不同回合的名字还不一样。

### 1.2 已有的两个正确落点

- `attributes` 里已有 `ai-coding.span`（span id，如 `i1`）与 `ai-coding.events`（`"|".join(events)`）——
  事件日志并没有丢，只是被同时塞进了 `name`；
- `spanId`/`parentSpanId` 都由 `session|turn|span` 派生，已经是按 span 组织的。

所以 `name` 没有承担"唯一信息"的职责，纯粹是显示层字段。

### 1.3 OTLP 语义

OTel 的 span `name` 是**操作名**（低基数、可聚合），事件/属性才承载细节；把整段事件日志放进
`name` 会让后端按名聚合时每个回合都变成一个独立的操作，看板上全是超长字符串。

## 2. 方案（一处代码 + 契约文档）

**FIX-A13-1：`name` 取该 span 的**首个事件名**（操作名），不再拼接整串。**

```python
start = min(_nano(row.ts) for row in rows)
first_event = min(rows, key=lambda row: _nano(row.ts)).evt   # tie 保持写入序
...
"name": first_event,
```

（评审补：取"ts 最早"而不是"写盘首行"——`start` 已经这么算，名字要和它同锚点；
时钟回拨、断点续写、并发追加都不会把名字挂到后写的事件上。）

理由（评审后从"用 span id"改过来）：

- `docs/r9-analysis.md:325` 的原始映射就是 `evt → name`（"事件名就是 span 名"），实现当年在
  分组后改成了拼接；回到"一个事件名"是对既有契约的回归，不是新定义；
- 实测 51 个 span 的首事件只有 4 类：`turn`→`TurnFinished`、`iN`→`IterationStarted`、
  `stepN`→`ModelStepFinished`、`call_*`→`ToolStarted`——低基数、可读、跨回合稳定；
- 用 span id 的话，工具 span 的名字是 `call_`+27 hex（29 字符）且每个调用都不同，高基数又不读；
- 事件全表已在 `ai-coding.events` 属性里，一个字段都不丢；
- 既有测试按 `"TurnFinished"` / `"IterationStarted"` / `"ModelStepFinished"` 取 span，**不用改**。

### 契约文档同步（评审阻塞项）

| 文件 | 行 | 改法 |
| --- | --- | --- |
| `docs/r9-analysis.md` | 325 | `evt → name` 补一句：按 span 分组后取**首个**事件名；事件全表在 `ai-coding.events` |
| `docs/data-contracts.md` | 611-616 | OTLP 映射补一行：`name` = 该 span 的首个事件名（低基数操作名），全量事件见属性 |

### 影响面

只有 `/export otlp` 的产物与新增的一条测试；`spanId` 派生、排序、clip 都不动，
导出仍然字节确定（`test_r9_the_export_is_deterministic` 不受影响）。

## 3. 测试（先红后绿）

| 测试 | 断言 |
| --- | --- |
| 新增 `test_a13_a_span_is_named_by_its_first_event` | 三个事件同属 `i1` → `name == "IterationStarted"`；`"|" not in name`；`ai-coding.events` 仍是完整 `"IterationStarted\|ContextAssembled\|AssistantMessage"` |
| 新增（同例后半） | `ToolStarted` + `ToolFinished` 同属 `call_x` → `name == "ToolStarted"` |
| 新增 `test_a13_the_name_is_the_earliest_event_not_the_first_written_line` | 先写较晚的 `ToolFinished`、再写较早的 `ToolStarted` → `name == "ToolStarted"` |
| 既有 7 例 | 原样通过（父链/时间戳/instant/属性类型/错误状态/确定性/信封）——`evt → name` 的契约没有变 |

### 评审发现但本轮不修

`export_otlp.py:81-82` 的分组键只有 `row.span`，**忽略 `scope`**。子代理事件与父循环共用
`i1`/`step1` 这类 id（`core/trace.py:688-706` 写入 `scope`），于是父、子同名 span 被并成一个：
`parentSpanId` 取谁的、`error` 互相传染、`name` 取两者最早的事件。根治要把分组键改成
`(scope, span)` 并派生新的 `spanId`，属于契约级改动，记入矩阵 **A-16**，留独立一轮。

## 4. 验收

1. 用现有 trace 重新导出（离线 `read_session_view` + `rows_to_otlp`，不起 TUI）：
   全部 resourceSpans 里的 `name` ∈ {`TurnFinished`,`IterationStarted`,`ModelStepFinished`,`ToolStarted`}，
   无一含 `|`（改前最长 160 字符）；
2. `ai-coding.events` 与改前逐字一致；
3. `pytest tests/test_trace_export_otlp.py`、`ruff`、`mypy` 干净。
