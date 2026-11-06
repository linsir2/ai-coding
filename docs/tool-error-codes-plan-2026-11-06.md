# 工具错误码统一 + 轨迹可观测性方案（2026-11-06）

关联：[code-review-2026-11-06.md](code-review-2026-11-06.md) §6.4（F3/F4/错误码三项未处理）。
本文先记录方案，后作为 **E1–E4 的落地记录**；§1–§2 是改造前的量化与参照，
§3 之后是最终口径与实施结果。

---

## 0. 结论（2026-11-06 定稿）

1. **统一口径：`ToolResult.error` 只承载 16 个 gRPC status family。**
   旧的 100+ snake_case 叶子码不再出现在 `error`；它们只在
   `ToolResult.detail`（诊断/策略/UI 的次行）和事件 `ToolFinished.detail`
   里保留。这样 `error` 是稳定、可枚举、带明确重试语义的契约。
2. **借鉴的是 gRPC 的「族」与 Stripe 的「type + code」形状**：gRPC 提供
   重试语义（`UNAVAILABLE` 可重试、`FAILED_PRECONDITION` 不可），Stripe
   证明细码应放在次字段而不是主字段。JSON-RPC/MCP 的 `-326xx`、HTTP 4xx/5xx、
   errno 同理——**主码负责策略，细码负责检索**。
3. **兼容通过单点归一化实现**：旧 session/trace/第三方工具仍可写叶子码，
   `ToolResult.__post_init__` 调用 `normalize()` 把它变成
   `(family, leaf)`；无码失败变成 `INTERNAL`；未知字符串变成
   `(UNKNOWN, 原串)`。消费者只读 family，不再比较叶子。
4. **三处断点已闭合**：46 个无码失败点全部补码；`ToolOutput.error/detail`
   与 `<tool_output ... error="FAMILY" code="leaf">` 已进入模型消息；
   策略层改读注册表的 `severity_of` / `is_neutral`。
5. **F3/F4 是下一批（未实施）：事件的「分母」不完整。** 补一个
   `NudgeInjected` 事件和 `SearchStalled.trigger` 字段即可，不动策略语义。
6. **F6（重复读）不做硬上限**：先统一参数指纹并观测。
   （详见 §7；本文件记录方案，E1–E4 已落地。）

---

## 1. 现状盘点（量化）

### 1.1 生产者

| 类别 | 位置 | 数量 |
|---|---|---|
| 工具内的显式码 | `ai_coding/tools/*.py` | 98 个不同字面量 |
| 分发器拒绝/超时 | [dispatch.py](../ai_coding/core/dispatch.py#L982-L1252) | `tool_error` / `unknown_tool` / `readonly_denied` / `denied` / `guard_denied` / `hook_blocked` / `not_approved` / `approval_timeout` / `stopped` / `skipped` / `write_conflict` / `tool_timeout`，另有 `invalid_arguments` / `truncated_arguments` |
| 完成门禁 | [completion.py](../ai_coding/core/completion.py#L127-L226) | `completion_unverified` / `verify_failed` / `directory_unsupported` / `legacy_session_unverified` / `override_unavailable` / `override_denied` / `missing_override_reason` 等，经由 `code=` 返回 |
| 合并/调度 | [scheduler.py](../ai_coding/core/scheduler.py#L452-L535) | `not_found` / `ambiguous` / `not_settled` / `commit_failed` / `nothing_to_merge` / `branch_moved` / `head_moved` / `dirty` / `conflict` |

**关键缺口（AST 统计 `ai_coding/tools`）**：

```
失败结果 184 个：带 error 码 138（75%），不带 46（25%）
不带码的分布：
  edit_tool 9   websearch 8   grep 7   webfetch 6   skill 5
  glob 4        read 3        write 2  tool_search 1 bash 1
```

`edit` 的 `old_string not found`、`file not found`、`appears N times`，
`read` 的 `file not found` / `file too large`，`write` 的 `write error`
全部是 `error=None`。这就是 trace 里 `('edit', None)` 的来源。

### 1.2 数据流：8 跳，3 处断点

```
① 工具构造 ToolResult(success, output, data, error)
        │
② ToolDispatcher._execute 兜底：异常→tool_error，超时→tool_timeout
        │
③ ToolFinished 事件 ──────────────► trace jsonl / bridge / 前端卡片
        │                              （断点 A：仅 75% 有码；64 个码 UI 无标签）
        │
④ TurnCloser.write_back
     ToolOutput(call_id, tool_name, output, success, data, artifacts)
        │                              （断点 B：ToolOutput 没有 error 字段）
        ▼
⑤ wrap_tool_output → 模型看到的 <tool_output ok="false"> 散文
        │                              （断点 C：码不可见，模型无法按码分支）
        ▼
⑥ SessionData.messages → 落盘 / 重放 / 压缩
        │
⑦ TurnBooks.note_outcomes
     _NOT_EVIDENCE_ERRORS / failures / _severity_of(关键词匹配)
        │
⑧ TurnPolicy → interventions → ledger(U-err-{tool}-{error})
```

三处断点的具体后果：

- **A**：`toolmeta.ts` 只有 54 个标签，后端 109 个码里有 64 个落到
  `未知原因（code）`；`error=None` 时卡片连原因都没有。
- **B/C**：模型下一轮看到的是
  `<tool_output tool="edit" ok="false">old_string not found ...</tool_output>`，
  没有 `code`。做后验分析时只能从散文猜。
- **D（策略）**：[turn_books.py:739](../ai_coding/core/turn_books.py#L739-L745)
  的 `_severity_of` 用关键词判断严重度；`error="failed to start: ..."` 这类
  散文会误命中 `failed` → severity 2，而它其实是 `unavailable`（5）。

### 1.3 消费者清单

| 消费者 | 读到什么 | 需要什么 |
|---|---|---|
| `TurnBooks.note_outcomes` | `error` 字符串 | 稳定叶子码 + family/severity |
| `TurnBooks._NOT_EVIDENCE_ERRORS` | 2 个豁免码 | 改为注册表 `neutral` 字段（语义不变） |
| `domain/ledger.py` | `(tool, error)` 作 id | 码稳定后 id 才稳定（现在是 `U-err-edit-failed`） |
| `core/sop_router.py:542` | 跨回合同码比较 | `error=None` 时永远不重复，改后才会生效 |
| `core/trace.py` | `ToolFinished.error` 原样落盘 | 建议加 `error_family`（可选） |
| `server/bridge.py:141` | `error` → 前端 | family 可选，前端只需标签 |
| `frontend/src/util/toolmeta.ts` | `FAILURES[code]` | 覆盖全部码，缺省不吞 |
| `app/trace_text.py` | 打印 error | 无改动 |
| `docs/data-contracts.md:209-237` | 手工维护的码表 | 改为从注册表生成/校验 |

---

## 2. 业界参照与取舍

| 体系 | 结构 | 我们借什么 | 我们不借什么 |
|---|---|---|---|
| **Stripe API errors** | `type`（粗族）+ `code`（细码）+ `param` + `message` | 双层的形状；`param` 对应我们的 `data` | 支付领域词表 |
| **gRPC status** | 16 个固定枚举，带明确重试语义 | 族名与 `retryable` 语义（`UNAVAILABLE` 可重试、`FAILED_PRECONDITION` 不可） | 用 16 个枚举替换叶子码 |
| **JSON-RPC 2.0 / MCP** | `-32602 invalid params`、`-32601 method not found`、`-32603 internal`；MCP 明确区分「协议错误」与「工具执行错误（`isError`）」 | 「工具失败走结果，不走协议错误」——我们已经是这个形状；`unknown_tool` 对应 method not found | 数字错误码（不可 grep，且会与 HTTP 状态混淆） |
| **HTTP** | 400/401/403/404/409/422/429/500/503 | 族的可读语义、UI 配色 | 把状态码当业务码 |
| **Linux errno** | `ENOENT`/`EACCES`/`EEXIST`/`ENOSPC` | 文件类叶子码的命名直觉（`file_not_found` 等） | 数字/宏风格 |
| **Anthropic / OpenAI tools** | 只有 `is_error` 布尔 | 反例：布尔不足以驱动重试、报表与 UI | — |

结论：**叶子码保持 snake_case 字符串，新增 `family` 作为机器可读的粗粒度。**
这是"借鉴"而不是"照抄"，也是唯一不会破坏已有 session/trace 的做法。

---

## 3. 目标模型（最终采用：只用 gRPC 族码）

### 3.1 契约

`ai_coding/domain/error_codes.py` 是唯一事实源：

```python
class ToolError(str, Enum):
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    NOT_FOUND = "NOT_FOUND"
    ALREADY_EXISTS = "ALREADY_EXISTS"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"
    FAILED_PRECONDITION = "FAILED_PRECONDITION"
    ABORTED = "ABORTED"
    OUT_OF_RANGE = "OUT_OF_RANGE"
    UNIMPLEMENTED = "UNIMPLEMENTED"
    INTERNAL = "INTERNAL"
    UNAVAILABLE = "UNAVAILABLE"
    DATA_LOSS = "DATA_LOSS"
    UNAUTHENTICATED = "UNAUTHENTICATED"
```

对外函数：

```python
def is_family(value: str | None) -> bool
def normalize(error: str | None, *, success: bool) -> tuple[str | None, str | None]
def family_of(error: str | None) -> str
def severity_of(error: str | None, detail: str | None = None) -> int
def is_neutral(error: str | None, detail: str | None = None) -> bool
def is_retryable(error: str | None) -> bool
def tone_of(error: str | None) -> Literal["denied", "failed"]
```

`ToolResult` 在 `__post_init__` 单点归一化：

| 输入 | `error` | `detail` |
|---|---|---|
| 成功，`error=None` | `None` | `None` |
| 失败，`error=None` | `INTERNAL` | `None` |
| 旧叶子 `stale_read` | `FAILED_PRECONDITION` | `stale_read` |
| 直接写 family `PERMISSION_DENIED` | `PERMISSION_DENIED` | `None` |
| 未注册字符串 `brand_new_code` | `UNKNOWN` | `brand_new_code` |

**叶子码没有消失，只是降级为 `detail`。** 它仍进模型消息
（`<tool_output ... error="FAMILY" code="leaf">`）、trace（`ToolFinished.detail`）
和 bridge（`detailCode`），但没有任何策略再比较它。

### 3.2 族的默认元数据

| family | tone | retryable | severity | 说明 |
|---|---|---|---|---|
| `INVALID_ARGUMENT` | failed | false | 2 | 参数/格式错，改参数才能过 |
| `NOT_FOUND` | failed | false | 2 | 路径/对象不存在 |
| `ALREADY_EXISTS` | denied | false | 2 | 状态已到位（已批准、已收敛） |
| `PERMISSION_DENIED` | denied | false | 3 | 规则/审批/边界拒绝 |
| `FAILED_PRECONDITION` | denied | false | 2 | 顺序或状态不对（先读再写） |
| `RESOURCE_EXHAUSTED` | failed | true | 4 | 尺寸/配额 |
| `DEADLINE_EXCEEDED` | failed | true | 5 | 超时 |
| `CANCELLED` | denied | false | 1 | 用户/系统取消 |
| `ABORTED` | denied | true | 2 | 并发冲突，重读/重排后可再试 |
| `OUT_OF_RANGE` | failed | false | 2 | 越界（索引/偏移） |
| `UNIMPLEMENTED` | failed | false | 2 | 工具不存在或未实现 |
| `UNAVAILABLE` | failed | true | 5 | 依赖不可用（MCP/沙箱/子代理） |
| `INTERNAL` | failed | true | 5 | 未预期异常 |
| `DATA_LOSS` | failed | true | 5 | 读写损坏 |
| `UNAUTHENTICATED` | denied | false | 3 | 凭据缺失/失效 |
| `UNKNOWN` | failed | false | 1 | 未分类（只在无法归族时） |

**severity 双轨**：直接发 family 时用上表；带 `detail` 时沿用旧关键词函数
（作用于 detail），保证疲劳度阶梯与历史测量一致。`neutral` 恰为
`cancelled_by_user` / `invalid_plan` 两个 detail。

### 3.3 为什么保留 detail（而不是只留 16 个族）

`edit` 的 `old_string_not_found` / `old_string_not_unique` / `file_not_found`
修复动作不同；压成 `FAILED_PRECONDITION` 后模型仍需要那句话。因此：
**family 负责机器策略（重试/配色/中性），detail 负责可检索与可行动。**
与早期"叶子码 + family"方案的区别只在字段命名：本文档的最终形态是
`error`（family）+ `detail`（leaf），而不是两个并列的 family 字段。

---

## 4. 码表（按族）

**权威码表在代码里，不在文档里**：`ai_coding/domain/error_codes.py` 的
`LEGACY_TO_FAMILY` 是 152 个旧叶子的唯一归属，CI 的
`scripts/check_error_codes.py` 保证生产者不会写出未登记的码。
下面是分布（自动统计，改表时同步）：

| family | 旧叶子数 |
|---|---|
| `INVALID_ARGUMENT` | 39 |
| `FAILED_PRECONDITION` | 23 |
| `PERMISSION_DENIED` | 21 |
| `UNAVAILABLE` | 20 |
| `NOT_FOUND` | 11 |
| `ABORTED` | 9 |
| `INTERNAL` | 8 |
| `RESOURCE_EXHAUSTED` | 7 |
| `CANCELLED` | 5 |
| `DEADLINE_EXCEEDED` | 5 |
| `ALREADY_EXISTS` | 3 |
| `UNIMPLEMENTED` | 1 |
| `OUT_OF_RANGE` / `DATA_LOSS` / `UNAUTHENTICATED` | 0（保留给未来生产者） |

> 与早期草案的差异：`unknown_tool` 归 `UNIMPLEMENTED`（不是 NOT_FOUND），
> `unknown_action` 归 `INVALID_ARGUMENT`，`invalid_plan` / `not_in_plan` /
> `directory_unsupported` 归 `FAILED_PRECONDITION`，`already_*` 归
> `ALREADY_EXISTS`。以代码表为准。

---

## 5. 改造清单（文件级）

### 5.1 新增

| 文件 | 内容 |
|---|---|
| `ai_coding/domain/error_codes.py` | `ToolError`（16 族）、`LEGACY_TO_FAMILY`、查询/归一化函数 |
| `tests/test_error_codes.py` | 族集合、归一化、severity/neutral/retryable/tone、TS 标签对齐 |
| `scripts/check_error_codes.py` | CI 用：AST 扫描无码失败 + 未登记码 + 前后端对齐 |

### 5.2 数据流改造

| 层 | 文件 / 行 | 改动 |
|---|---|---|
| 归一化 | [message.py:93-140](../ai_coding/domain/message.py#L93-L140) | `ToolResult` 增加 `detail`；`__post_init__` 调 `normalize()` |
| 信封 | [run.py:57-100](../ai_coding/domain/run.py#L57-L100) | `wrap_tool_output(..., error=None, detail=None)`；失败时 `<tool_output ... ok="false" error="FAMILY" code="leaf">`；**成功时逐字节不变** |
| 持久化 | [message.py:150-175](../ai_coding/domain/message.py#L150-L175) | `ToolOutput` 增加 `error` / `detail` |
| 回写 | [turn_close.py:177-195](../ai_coding/core/turn_close.py#L177-L195) | 构造 `ToolOutput` 时传 `error=item.result.error, detail=item.result.detail` |
| 事件 | [events.py:101-122](../ai_coding/core/events.py#L101-L122) | `ToolFinished` 增加 `detail: str = ""`；`error` 是 family |
| 分发 | [dispatch.py](../ai_coding/core/dispatch.py) | 叶子码比较改为 `result.detail`（`credential_approval_required` / `unknown_tool` / `sandbox_violation`） |
| 策略 | [turn_books.py](../ai_coding/core/turn_books.py) | `_NOT_EVIDENCE_ERRORS` / `_severity_of` → `is_neutral` / `severity_of` |
| 跨回合 | [sop_router.py](../ai_coding/core/sop_router.py) | `ToolObservation.detail`；重复判定同时比较 family + detail |
| 桥接 | [bridge.py:141-155](../ai_coding/server/bridge.py#L141-L155) | `error` 保持 family；`detailCode` 仅非空时写，旧 payload 不变 |
| UI | [toolmeta.ts:118-140](../ai_coding/ai_coding/frontend/src/util/toolmeta.ts#L118-L140) | `FAILURES` 改为 16 个 family 标签；保留 `未知原因（code）` 兜底 |

### 5.3 补齐 46 个无码失败点

按 §4 的新增码逐点补（不改文案，只加 `error=`）：

| 文件 | 新增码 |
|---|---|
| `edit_tool.py`（9 处） | `missing_old_string`、`file_not_found`、`read_error`、`write_error`、`old_string_not_found`、`old_string_not_unique` |
| `read_tool.py`（3 处） | `file_not_found`、`file_too_large`、`read_error` |
| `write_tool.py`（2 处） | `write_error` |
| `grep_tool.py`（7 处） | `missing_pattern`、`workspace_escape`、`sandbox_violation`、`invalid_pattern`（正则编译失败）、`timeout`、`grep_error`×2 |
| `glob_tool.py`（4 处） | `missing_pattern`、`workspace_escape`、`sandbox_violation`、`glob_error` |
| `webfetch_tool.py`（6 处） | `missing_url`、`url_not_allowed`、`invalid_format`、`sandbox_violation`、`fetch_failed`、`fetch_timeout` |
| `websearch_tool.py`（8 处） | `missing_query`、`not_configured`、`websearch_error`×6 |
| `skill_tool.py`（5 处） | `unknown_action`、`missing_name`、`invalid_skill_name`、`unknown_skill`、`invalid_skill`（frontmatter 解析失败） |
| `tool_search_tool.py`（1 处） | `empty_query` |
| `bash_tool.py`（1 处） | `missing_command` |

> 命名复核：webfetch 与 websearch 是两条独立链路，分别用 `fetch_*` /
> `websearch_error`；`grep` 的正则编译失败是参数错误（`invalid_pattern`），
> 不是 `invalid_plan`。最终以注册表为准。

---

## 6. 分五批实施

### 批次 E1：注册表 + 强制校验（已完成）
- 新增 `error_codes.py`（16 族 + 152 个旧叶子映射）、`tests/test_error_codes.py`、
  `scripts/check_error_codes.py`；CI quality job 已接入脚本。
- 测试覆盖族集合、归一化、severity/neutral/retryable/tone、TS 标签对齐。

### 批次 E2：补齐 46 个无码失败点（已完成）
- 只加 `error=`，不改文案、不改 `success`；AST 扫描确认全包
  `success=False` 且无码的构造为 0。
- 涉及 webfetch / websearch / grep / glob / read / write / edit / bash /
  skill / tool_search 十个工具。
- 真机复跑待办（与 14 轮压力任务同口径）。

### 批次 E3：让码进入模型与事件（已完成）
- `ToolResult.detail` + 单点归一化；`ToolOutput.error/detail`；
  `wrap_tool_output(error=..., detail=...)`；`ToolFinished.detail`；
  bridge 的 `detailCode`；策略层/跨回合改读注册表。
- 测试：成功信封逐字节不变；失败信封含 family + code；17 个测试文件
  的叶子断言迁移到 `.detail`；旧 session 重放仍可解析。

### 批次 E4：UI 补齐（已完成）
- `toolmeta.ts` 的 `FAILURES` 改为 16 个 family；`bun test` 33 项通过。
- 前端不再需要 100+ 叶子标签：叶子只作为 `detailCode` 用于 trace/诊断。

### 批次 E5：文档与清理（部分完成）
- 本文档已改为最终口径；`docs/data-contracts.md` 的码表待同步。
- 旧关键词函数保留在 `severity_of` 内部（作用于 detail），
  等旧 trace 不再需要逐码等价时删除。

---

## 7. F3 / F4 / F6

### 7.1 F3：nudge 消耗的迭代在 trace 里不可见

**事实**：`turn-1` 的 `IterationStarted` 是 `i1…i20, i22, i23, i24, i26, i28,
i29`；缺的 21/25/27 在
[agent_loop.py:1153-1210](../ai_coding/ai_coding/core/agent_loop.py#L1153-L1210)
的 NUDGE 分支：`index += 1` 后 `continue`，不 emit、不 `note_iteration`。
`books.total_iterations` 统计的是模型步（这是对的），而 `index` 是预算槽位
（两者本就应该不同）；问题是**预算槽位没有任何事件**。

**方案**

1. 新事件 `NudgeInjected`：
   ```python
   @dataclass(frozen=True)
   class NudgeInjected:
       iteration: int        # 被消耗的预算槽位
       gate: str             # strategy / fatigue / search / task / continuation
       level: int
       action: str           # "nudge" | "continue"
       reason: str
       chars: int            # 注入文本长度（成本可见）
       restarted: bool = False
   ```
   发在 `session.add_message(...)` 之后、`continue` 之前。
2. `trace._describe` 增加显式分支：span `n{index}`（批次前缀复用 `_span`），
   parent `"turn"`，iteration=index。于是时间线变成
   `i1…i20, n21, i22…`，与预算槽位一一对应。
3. `TurnBooks` 增加 `nudge_steps` 与 `note_nudge_step()`；
   `TurnSnapshot.budget_steps` = `total_iterations + nudge_steps`。
4. `TurnOutcome` 增加 `nudge_steps: int = 0`，在 `_close_turn` 一处
   `replace(outcome, nudge_steps=books.nudge_steps)`，不碰 10 个构造点。
5. `render_handoff` 在 `nudge_steps > 0` 时补一句"迭代 N，其中提醒 M"。

**不做**：不给 nudge 发 `IterationStarted`。那会让"模型步"与"预算槽位"
重新混在一起，`total_iterations` 的语义会被破坏。

**测试**：trace case（`n2` span + parent turn）；books 单测；
`test_interventions_loop` 断言每次注入都有一条 `NudgeInjected`，且
`i` 序列 + `n` 序列 = 连续整数。

### 7.2 F4：`SearchStalled` 的 reason 与计数矛盾

**事实**：`turn-2` 第 9 步 `calls=5, streak=0, repeated=0, novel=2,
reason="look-only streak"`，实际由
[interventions.py:339-344](../ai_coding/ai_coding/core/interventions.py#L339-L344)
的 `repeat_streak >= 1` 分支触发。零调用的那条已经由 F7（`d152095`）修掉，
剩下的问题是**归因缺失**。

**方案**

1. `_search_due` 返回 `(level, kind, trigger)`，trigger 取值：
   `repeat_output` / `look_streak` / `look_streak_l2` / `search_streak_l3` /
   `stale_evidence` / `stale_evidence_l2` / `stale_evidence_l3`。
2. `_due` 对其他 gate 返回 `trigger=""`；`InterventionDecision` 增加
   `trigger: str = ""`；`_build` 透传。
3. `SearchStalled` 增加 `trigger: str = ""` 与 `repeat_streak: int = 0`
   （默认值保证旧 trace 可读）。
4. nudge 的 `reason` 改成与触发源一致（`"repeated identical output"` /
   `"repeated evidence"` / `"look-only streak"`）。**不动** L3 的
   `SEARCH_UNCONVERGED`——`agent_loop` 用它决定要不要请求收尾。
5. `interventions.record()` 把 `trigger` 写进 ledger entry（additive），
   让"上一次为什么说话"在下次判定时可读。

**测试**：三条单测分别构造 repeat/stale/look 三种快照，断言
`trigger` 与 `reason`；trace case 断言字段落盘。

### 7.3 F6：同一文件重复读

**先给结论**：不建议在 E 批次里加"硬上限"。依据：

- `ws8 turn-5` 的 19 次 `App.tsx` 读取中，多数紧跟在 `edit`/`sed -i` 之后；
  [guard.py:405](../ai_coding/ai_coding/core/guard.py#L405) 的
  `_mutated_after_read` 逃生门是**故意**放行这种"改完再读"的。
- 一刀切会重现 2026-10-31 的死锁（write 被拒 → 重读被拒，75 迭代）。

**只做两件低风险的事**：

1. **指纹归一**：`DuplicateCallGuard` 对 `read` 单独规范化参数——
   路径 `normpath`；`offset` 缺省/0 等价；`limit` 缺省/null 等价。
   这能抓住 `{}` 与 `{offset:0}` 被当成两个调用的问题，不影响"内容变了"的读。
2. **观测先行**：`TurnBooks` 已有 `stale_streak`/`repeated_nonempty`；
   在压力任务脚本里加一列"每轮被抑制的读次数/重复内容读次数"，
   连续两轮都有 >10 次且内容相同再考虑上限。

---

## 8. 测试与验收

### 8.1 自动测试

| 测试 | 断言 |
|---|---|
| `test_error_codes.py::test_every_emitted_code_is_registered` | AST 扫描 `ai_coding/**/*.py` 的 `error="..."`/`code="..."` 全在注册表 |
| `::test_every_registered_code_has_a_ui_label` | 解析 `toolmeta.ts`，双向对齐 |
| `::test_neutral_matches_the_old_exemption_set` | 恰为 `{cancelled_by_user, invalid_plan}` |
| `::test_severity_matches_the_legacy_keyword_function` | 对旧码逐码相等 |
| `::test_failed_envelope_carries_the_code` | `<tool_output ... ok="false" code="stale_read">` |
| `::test_successful_envelope_is_byte_identical` | 防止 prompt 前缀缓存回归 |
| `test_trace_recorder::test_a_nudge_gets_its_own_span` | `n21` + parent `turn` |
| `test_interventions::test_search_trigger_names_the_real_cause` | repeat/stale/look 三种 |

### 8.2 CI

- `quality` 增加 `python scripts/check_error_codes.py`。
- `frontend` 的 `bun test` 增加一条"FAILURES 覆盖注册表"的用例
  （由脚本生成一份 `error-codes.json` 供 TS 读取，或直接解析 TS）。

### 8.3 真机验收（与 14 轮压力任务同口径）

| 指标 | 现状 | 目标 |
|---|---|---|
| trace 里 `error=None` 的失败 | 约 25% | 0 |
| UI `未知原因（code）` | 64 个码可触发 | 0 |
| `U-err-*` 账本 id 中的 `failed` | 出现 | 只剩 `internal` |
| 每轮 `n{index}` 事件数 | 0 | = 被消耗的提醒槽位 |
| `SearchStalled.trigger` | 无 | 每条都有，且与计数一致 |

---

## 9. 风险与兼容

| 风险 | 影响 | 缓解 |
|---|---|---|
| 失败信封加 `code=` 改变 prompt | 该回合前缀缓存失效一次 | 只改失败结果；成功路径字节不变；`test_successful_envelope_is_byte_identical` |
| 旧 session 无 code | 重放时 `error=None` | `severity_of(None)=1`；`neutral=False`；账本用 `internal` 兜底 |
| 64 个新 UI 标签 | `toolmeta.ts` 变长 | 只加数据不加逻辑；脚本保证不漏 |
| `is_cross_turn_duplicate` 开始生效 | 可能多注入 skill 提示 | 真机对比一次；必要时加 `audience="operator"` 过滤 |
| severity 表与关键词不一致 | 疲劳度/阶梯提前或延后 | §3.2 的逐码等价测试 |
| 注册表与生产者漂移 | 新码绕过校验 | CI 的 AST 扫描 + `unknown code` 测试 |

---

## 10. 明确不做

- 不把 `error` 改成数字码；不与 HTTP 状态混用。
- 不把 `ToolResult.success` 去掉（`success + error` 是 MCP 的 `isError` 同构）。
- 不在本方案里改 ladder 阈值、疲劳公式或 C3 语义。
- 不给重复读加硬上限（见 §7.3）。
- 不把 `SubagentResult.status` 混进 `ToolResult.error` 词表的核心部分
  （只登记、只标注 `audience="operator"`）。
