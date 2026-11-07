# B2 调研方案：上下文工程 + 压缩遗留（14 项）

- 基线：`INVENTORY.md` 记录的 commit `fd5a929`
- 范围：`ai_coding/core/request_assembly.py`、`ai_coding/core/context_compression.py` 及其直接依赖
- 性质：**只读调研 + 方案**，本文件之外不修改任何代码
- 方法：逐项读源码、追调用图、核对现有测试；每个结论都带 `file:line`

## 0. 结论速览

| ID | 问题 | 推荐方案 | 是否有定案方向 | 主要风险 |
|---|---|---|---|---|
| CTX-01 | 裸路径/目录块无准入判定 | 进入 room 判定，超预算降级一行；目录条目按剩余预算缩放 | 是 | 目录条目数缩放导致既有断言变化 |
| CTX-02 | 裸指针整读文件数行 | `stat().st_size` 估算行数，标“约” | 是 | 行数由精确变近似 |
| CTX-03 | 显式 @ 先整读再截断 | 先 `stat`；>2MB/二进制后缀给指针；正常只读前缀 | 是 | 截断提示里的总行数只能估算 |
| CTX-04 | plan 文本三份 + 同名 skill 重复注入 | 推荐列表剔除已显式点名 skill；`PLAN_MODE_BLOCK` 去掉重复的 `PLAN_TEXT_RULE` | 部分（skill 无定案） | 完整单一来源需改白名单外测试 |
| CTX-05 | 单条长记忆超预算 | 渲染期按剩余预算截断 + harvest 期限长 | 否 | 记忆内容被截断 |
| CTX-06 | budget.py 死指标 | 先 grep 确认，删除仅测试使用的只读面 | 是 | 需改白名单外 `test_budget_ledger.py` |
| CTX-07a | closing 步按全量 schema 估算 | `assemble(offered_tools=False)` 传空 schema 文本 | 否 | 只影响报告/追踪，不影响真实请求 |
| CTX-07b | canonical 不含图像 | canonical 加 `images:n` 标记；事件估算补 `image_tokens` | 否 | canonical/digest 变化 |
| CTX-07c | history_chars 不含工具参数 | 把 `tool_calls[].arguments` 计入 | 否 | 仅指标变化 |
| CMP-01 | 重注入只活一轮 | 重注入收口到 `checkpoint_view` + `compress_if_needed` 后置挂载 | 是 | 改 `compress` 公共契约 |
| CMP-02 | checkpoint_view 每轮全量回扫 | 字符数判定 + 单槽 memo | 是 | memo 在“同长改写”下失效 |
| CMP-03 | protect_tokens 固定 40k | `clamp(window//3, 8k, 40k)` | 是 | 小窗口行为变化 |
| CMP-04 | 信封可能比正文大 | 构造后真实比较 `len(envelope) < len(body)` | 是 | 会留下一个孤儿 artifact |
| CMP-05 | provider 冻结读数蒙蔽 gate | 连续 3 次冻结且本地估算涨 >5% 时用 `working_history_tokens` | 是 | 误判会改变压缩时机 |

**最危险两项：CMP-05、CMP-01**（详见各项风险段）。

---

## 1. 逐项分析

### CTX-01 裸路径/目录引用块不扣预算

**现状（证据）**
- `request_assembly.py:765-791` 逐个处理引用：目录走 `771-772`，裸路径走 `773-775`，显式 `@` 才在 `776-789` 计算 `room`。
- `used += estimate_tokens(block)` 在 `790`，对三个分支都执行；但目录/裸路径分支是“先生成、后记账”，生成前不看 `budget - used`。
- 目录列表最多 `_DIR_ENTRIES = 200` 条（`request_assembly.py:77`），`_directory_listing` 在 `809-816` 无预算参数。
- 块预算是窗口的 15%，上限 10k token（`request_assembly.py:73-76`、`795-806`）。

**根因**
不是“完全没累加 `used`”（790 行有累加），而是**没有准入/降级**：一个 200 条目目录或一长串裸指针可以在零剩余预算时仍被整块塞进去，并且目录条目数不与剩余预算挂钩；`pending` 的 per-file floor（`758-764`）也只统计显式文件。

**候选与取舍**
- A. 仅在循环末尾截断整块：会切坏标签，且仍为整块付费。
- B. 每个分支生成后统一做 `cost` 与剩余预算比较，超限降级为一行指针；目录生成时直接按剩余预算缩放条目数。**（推荐）**
- C. 目录永远只给前 N 条：不随窗口变化，6k 窗口仍可能超。

**推荐方案（签名）**
```python
# request_assembly.py
def _directory_listing(
    rel: str, target: Path, *, max_entries: int = _DIR_ENTRIES
) -> str: ...

# 循环内（替换 765-791 的分支记账）
remaining = budget - used
if target.is_dir():
    block = self._directory_listing(
        rel, target,
        max_entries=max(1, min(_DIR_ENTRIES, remaining // _DIR_ENTRY_TOKENS)),
    )
elif rel not in explicit_paths:
    block = _bare_pointer(rel, target)
else:
    ...
cost = estimate_tokens(block)
if target.is_dir() or rel not in explicit_paths:
    if used + cost > budget:
        block = (
            f"- {rel}: [no room left in this request's file budget; "
            "read it with the read tool instead]"
        )
        cost = estimate_tokens(block)
used += cost
```
`_DIR_ENTRY_TOKENS` 取 8（一条名字约 0.5-1 行、含缩进/后缀），最小 1 条，避免 0 条目录。

**测试计划**
- `tests/test_file_mentions.py`：目录含 300 个文件 + 6k 窗口时，块 token 不超过 `_files_block_tokens()`；目录条目数随窗口缩小。
- 裸路径 200 条、预算耗尽时必须出现一行 `no room left ...`，且 `<referenced-files>` 仍闭合。
- 既有 `test_a_directory_mention_lists_its_entries` 保持通过。

**风险**
- 目录条目缩放会让“200 条全列”的旧行为变化；只影响显示，不影响文件可读性。
- 降级文案要与 784-787 的显式文件文案区分（目录用 `read`，目录其实更适合 `glob`，但保持 `read` 一致）。

---

### CTX-02 `_bare_pointer` 整读文件只为算行数

**现状（证据）**
- `request_assembly.py:202-222`：`_bare_pointer` 在 `215` 执行 `target.read_text(...)`，只为 `218` 的 `len(text.splitlines())`。
- 调用点在 `775`，即**每个裸路径每轮请求**都整读一次文件。

**根因**
指针只需要“文件存在 + 大概多大”，却付出了整文件 I/O 和解码成本；大文件（日志、CSV）在 wave seed 里会反复整读。

**候选与取舍**
- A. `stat().st_size` + 平均行长估算，标“约”。**（推荐，符合定案方向）**
- B. 流式数换行：仍是整读 I/O，只是内存低。
- C. 直接不给行数：信息量下降，模型无法判断规模。

**推荐方案（签名）**
```python
# request_assembly.py
_AVG_LINE_BYTES = 40  # 源码/文档的经验均值，只用于“约”

def _bare_pointer(rel: str, target: Path) -> str:
    try:
        size = target.stat().st_size
    except OSError as exc:
        return f"- {rel}: [unreadable: {exc}]"
    lines = max(1, (size + _AVG_LINE_BYTES - 1) // _AVG_LINE_BYTES)
    return (
        f"- {rel}（约 {lines} 行，{size} 字节）："
        "[not inlined this request; read it with the read tool]"
    )
```
不新增 import（用整数除法）。

**测试计划**
- `tests/test_file_mentions.py`：裸路径块出现“约”且不出现正文；用 monkeypatch 让 `Path.read_text` 抛错，证明不再调用（或断言只 `stat`）。
- 既有 `test_a_pasted_absolute_path_reaches_the_model_as_a_pointer` 继续通过（不依赖精确行数）。

**风险**
- 行数从精确变近似；文案必须保留“约”，否则会误导模型。
- `stat` 对目录也会成功，但目录走 CTX-01 分支，不进入此函数。

---

### CTX-03 显式 @ 先整读再截断

**现状（证据）**
- `request_assembly.py:155-199`：`_file_content` 在 `165` `read_text` 整个文件，`168` 才用 `chars_for_tokens` 算可容纳字符数，`169` 判断是否截断。
- `179` 的 `len(text.splitlines())` 依赖全文。

**根因**
截断信息（总行数）需要全文，但为此把整个文件读进内存；对 >2MB 文件或二进制后缀（虽然图片已在 `768-769` 跳过，但 pdf/zip 等未跳过）既慢又产生乱码。

**候选与取舍**
- A. 先 `stat`，超阈值/二进制后缀直接返回指针；否则只读前缀。**（推荐，符合定案方向）**
- B. 流式逐行读到预算：能得精确总行数但仍是整读 I/O。
- C. 只读前缀且不报总行数：信息损失最小，但截断提示弱。

**推荐方案（签名）**
```python
# request_assembly.py
_MAX_INLINE_FILE_BYTES = 2_000_000
_BINARY_SUFFIXES = frozenset(IMAGE_MIME_BY_SUFFIX) | frozenset(
    {".pdf", ".zip", ".gz", ".tar", ".whl", ".exe", ".so", ".dll", ".class", ".jar"}
)

def _file_content(rel: str, target: Path, *, room_tokens: int) -> str:
    try:
        size = target.stat().st_size
    except OSError as exc:
        return f'<file path="{rel}">[unreadable: {exc}]</file>'
    if size > _MAX_INLINE_FILE_BYTES or target.suffix.lower() in _BINARY_SUFFIXES:
        lines = max(1, (size + _AVG_LINE_BYTES - 1) // _AVG_LINE_BYTES)
        return (
            f"- {rel}（约 {lines} 行，{size} 字节）："
            "[too large or not text; read it with the read tool]"
        )
    probe_chars = max(1, room_tokens * 4)  # 最宽松的 4 chars/token
    with target.open("r", encoding="utf-8", errors="replace") as fh:
        probe = fh.read(probe_chars + 1)
    limit = chars_for_tokens(probe, room_tokens)
    cut = probe[:limit]
    truncated = len(probe) > limit
    boundary = cut.rfind("\n")
    if boundary >= 0:
        cut = cut[: boundary + 1]
    shown = len(cut.splitlines())
    # 被截断时总行数只能按“前缀行数 × 文件字节 / 前缀字节”估算
    total = shown if not truncated else max(shown, round(shown * size / max(1, len(probe.encode("utf-8")))))
    ...
```
保留现有 `[truncated: ...]` 文案与 `read offset={shown}` 语义；总行数在截断时加“约”。

**测试计划**
- `tests/test_file_mentions.py`：3MB 文件返回指针且不含正文；`.pdf` 后缀同样；100KB 文本仍截断且含 `read offset=`。
- 用 monkeypatch 统计 `open().read` 的最大字符数 ≤ `room_tokens*4+1`，证明不再整读。

**风险**
- 前缀读取后“总行数”只能估算；不要让提示语从“lines total”退化成错误断言，统一加“约”。
- 若 `room_tokens <= 0`，`probe_chars=1`、`limit=0`，要保证不除零、仍返回一行说明。

---

### CTX-04 plan 文本三份重复 + 同一 skill 既推荐又全量注入

**现状（证据）**
- `PLAN_TEXT_RULE` 定义在 `domain/agents.py:57-62`，被拼进至少 4 处：
  - 系统提示：`ai/prompt.py:154-186`，规则在 `170`；
  - 请求尾部 `<plan-mode>`：`request_assembly.py:82-91`，规则在 `86`，注入于 `577-580`；
  - 历史提醒：`plan_reminders.py:46-68`，规则在 `58`，由 `request_assembly.py:436-446` 的 `reconcile` 每轮补回；
  - 工具描述：`tools/finish_research_tool.py:49`、`168`（plan 模式下工具 schema 也随请求发送）。
- 同一请求里系统提示 + 尾部块 + 历史提醒 = 3 份规则文本（finish_research 是第 4 份）。
- skill：`request_assembly.py:528-535` 路由并 `format_block` 推荐；`548-550` 对显式点名 skill 追加 `<requested-skills>` 全量正文；`818-846` 渲染正文。若用户点名 A 且路由器也匹配 A，A 会同时出现在“推荐列表”和“全量正文”。

**根因**
plan 姿态没有单一事实源（`plan_reminders.py:1-10` 的 docstring 自己承认是“第三种形状”，但旧的两种没删）；skill 的“推荐”和“已加载”是两条独立管线，没有去重。

**候选与取舍**
- 方案 A（完整单一来源）：只保留 `PLAN_ENTER_REMINDER`（`reconcile` 每轮保证存在），删 `prompt.py` 的 plan 段和尾部 `PLAN_MODE_BLOCK`。
  - 收益最大；但 `tests/test_prompt.py:83-138`、`tests/test_plan_codex_path.py:245-255` 会失败，二者**不在本批白名单**。
- 方案 B（低风险）：尾部 `PLAN_MODE_BLOCK` 去掉 `f"{PLAN_TEXT_RULE} "`，保留 `<plan-mode>` 标签与其余句子；系统提示与提醒暂不动。
  - 每请求少约 50-60 字符，且 `<plan-mode>` 仍在 request 里，`test_plan_codex_path` 继续通过。
- skill 去重：无争议，直接做。

**推荐方案（签名）**
```python
# request_assembly.py assemble()
requested = list(resolve_mentions(user_input, self._skills)) if self._skills else []
...
matches = self._router.route(...)
if requested:
    claimed = set(requested)
    matches = [m for m in matches if m.name not in claimed]
recommended_skills_block = SOPRouter.format_block(matches)
...
if requested:
    recommended_skills_block += "\n\n" + self._requested_skills_block(requested)
```
同时把 `PLAN_MODE_BLOCK` 第 86 行的 `f"{PLAN_TEXT_RULE} "` 删除（方案 B）。完整方案 A 记为 DEFERRED，待白名单扩展到 `tests/test_prompt.py`、`tests/test_plan_codex_path.py`。

**测试计划**
- `tests/test_request_assembly.py`：用户点名 skill 且 router 命中同一 skill 时，`<recommended-skills>` 不含该 name，`<requested-skills>` 含一次正文；未点名时行为不变。
- `tests/test_file_mentions.py:158-169`（`@tidy`）继续通过。
- plan：断言尾部块不再出现 `PLAN_TEXT_RULE`，但仍有 `<plan-mode>`。

**风险**
- 过滤推荐会让“点名 skill”失去路由器给出的 `When:/Why:` 提示；这是有意取舍（正文更强）。
- 方案 A 不做，plan 仍有两份规则；必须在批次结论里明确这是 DEFERRED，不是遗漏。

---

### CTX-05 单条长记忆超预算

**现状（证据）**
- `memory/service.py:684-687`：预算单位是**字符**（`index_max_chars` 默认 600，扣 `_INDEX_OVERHEAD`）。
- `712-722`：`if used + len(line) > budget and chosen: break`——`chosen` 为空时无条件收下，单条 3000 字记忆会把块撑到 3000+。
- `724-730` `render_index_block` 也不截断。
- `891-976` `_reconcile` 落库时只做 `is_plausible_note_text`（`memory/note.py:244-252`，仅检查首字符），没有长度上限。
- 该块每轮进入请求尾部：`request_assembly.py:470`、`565`。

**根因**
“预算”只在选择循环里生效，且对第一条豁免；长度限制既不在写入侧也不在渲染侧。

**候选与取舍**
- A. 写入侧限长：影响存储，需迁移/兼容旧数据。
- B. 渲染侧按剩余预算截断，并在 `_reconcile` 对新候选限长。**（推荐，非破坏性）**
- C. 直接跳过超长 note：会丢信息，不如截断可读。

**推荐方案（签名）**
```python
# memory/service.py
_MAX_NOTE_CHARS = 400

def _clip_note_text(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"

# index_notes（替换 712-722）
for note in selected:
    prefix = f"- [{note.kind.value}#{note.id[:8]}] "
    room = budget - used
    if room <= len(prefix) + 1:
        break
    text = _clip_note_text(note.text, room - len(prefix) - 1)
    line = prefix + text
    chosen.append(note)
    used += len(line) + 1
    if used >= budget:
        break

# _reconcile（907 之后）
text = _clip_note_text(text, _MAX_NOTE_CHARS)
```
`render_index_block` 用同一个 `_clip_note_text`，保证 `index_notes` 与最终渲染不漂移。

**测试计划**
- 新增/扩展现有 memory 测试（需白名单扩展）：600 预算下一条 3000 字 note 的块长度 ≤ 600，且保留开头；两条短 note 仍完整。
- `_reconcile` 落库文本 ≤ `_MAX_NOTE_CHARS`。
- 若不便扩白名单，至少在 `tests/test_request_assembly.py` 用 fake memory 断言块长度不被信任——但这不能覆盖 `index_notes`，建议扩白名单。

**风险**
- 截断会丢失长记忆尾部；用“…”明示，且只影响注入块，不改存储原文（B 方案）。
- 与 `render_index_block` 的重复实现有关：两处必须共用 helper，否则会漂移。

---

### CTX-06 budget.py 多个指标无生产消费者

**现状（证据，已 grep 确认）**
- `core/budget.py:136-139` `record_assembly` 仅由 `core/turn_report.py:159` 调用；写入的 `_estimated_tokens`、`_prefix_ratio` **没有任何生产读者**。
- `budget.py:152-154` `latency_ms`、`156-158` `estimated_tokens`、`160-162` `prefix_shared_ratio`、`164-169` `calibration`、`171-176` `cache_hit_ratio`、`178-183` `by_model`、`185-196` `cost`：全仓非测试调用点为 0（`rg` 只命中 `tests/test_budget_ledger.py`）。
- 生产真正使用：`turn_report.py:108`（`totals.total_tokens`）、`186-191`（`record_step`）、`205`（`usage_dict`）。
- `Pricing` 仍被 `cli.py:341-348`、`app/trace_text.py:460` 使用（**不能删**）。

**根因**
账本曾经同时服务“窗口占用”和“R4 校准/成本”两类读者；后者只在测试和脚本里使用，随重构失去生产入口，字段成为死重。

**候选与取舍**
- A. 全删上述只读面 + `record_assembly` + `_by_model` 桶 + `turn_report.py:159` 调用。**（推荐，符合定案方向）**
- B. 保留但标 deprecated：仍维护无用状态，违背“只留确认有用的字段”。
- C. 只删其中几个：留下半套 API 更易误用。

**推荐方案（签名）**
删除：
- `record_assembly`（136-139）、`latency_ms`（152-154）、`estimated_tokens`（156-158）、`prefix_shared_ratio`（160-162）、`calibration`（164-169）、`cache_hit_ratio`（171-176）、`by_model`（178-183）、`cost`（185-196）。
- 私有字段 `_latency_ms`（80）、`_estimated_tokens`（81）、`_prefix_ratio`（82）、`_by_model`（71）及 `record_step` 里的 by-model 桶（106-116）。
- `turn_report.py:159` 的 `self._ledger.record_assembly(...)`。
保留：`Pricing`、`UsageTotals`、`totals`、`record_step`（`model` 参数保留以兼容调用方，可 `del model`）、`usage_dict`、`_context`。
`ContextAssembled` 事件本身仍带 `estimated_tokens`/`prefix_shared_ratio`，它们是事件消费者（trace/前端），不属于账本只读面。

**测试计划**
- 删除 `tests/test_budget_ledger.py` 中对应 8 个测试（33、42-58、61-69、81-84、89-92、157-170、184-225）。
- 保留 `test_totals_accumulate...`、`usage_dict` 系列。
- 校验 `tests/test_turn_reporter.py` 通过（不再依赖 ledged 的 `record_assembly`）。

**风险（白名单冲突，见 §4）**
- `tests/test_budget_ledger.py` **不在本批白名单**，但删除字段必然要改它。需要把该文件加入白名单，或在白名单外做一次性同步（本批禁止）。

---

### CTX-07a closing 步 `tools=[]` 仍按全量 schema 估算

**现状（证据）**
- `agent_loop.py:2915-2974` `_ask_for_closing`：`2938-2948` 调 `assembler.assemble(...)`，`2960` 给引擎 `tools=[]`。
- `assemble` 在 `596-608` 调 `reporter.report_context(tools_text=self._tools_text_provider(session))`，**不知道引擎实际没带工具**。
- `turn_report.py:142-144` 用该文本算 `tools_chars`，`154` 算 `estimated_tokens`。

**根因**
“请求要发送的 schema”由 provider 决定，但 closing 步在 assemble 之后才被调用方改成空列表；估算层没有入口表达“这一步不提供工具”。

**候选与取舍**
- A. 给 `assemble` 增加 `offered_tools: bool = True`，false 时传空文本。**（推荐）**
- B. 在 `ReportContext` 里按 `index == max_iterations+1` 推断：把循环策略泄漏进报告层，脆弱。
- C. 不改：只影响 `ContextAssembled`/trace 的估算，不影响真实请求，但会持续误导排查。

**推荐方案（签名）**
```python
# request_assembly.py
async def assemble(
    self, *, session, task, books, turn_number, hook_context, deadline,
    index, previous_request, pinned=(), offered_tools: bool = True,
) -> Assembled:
    ...
    canonical = self._reporter.report_context(
        ...,
        tools_text=(
            ""
            if not offered_tools
            else (self._tools_text_provider(session)
                  if self._tools_text_provider is not None else None)
        ),
    )

# agent_loop.py:2938
assembled = await self.assembler.assemble(..., pinned=..., offered_tools=False)
```

**测试计划**
- `tests/test_request_assembly.py`：`_Reporter` 记录 kwargs，`offered_tools=False` 时 `tools_text == ""`；默认时仍为 provider 文本。
- `tests/test_turn_reporter.py`：`tools_text=""` 时 `tools_chars == 0`、`estimated_tokens` 明显小于全量 schema。

**风险**
- 低。`report_context` 用 `""` 而非 `None` 才能清空（`None` 表示“沿用上次文本”，见 `turn_report.py:142-144`），签名必须明确这一点。

---

### CTX-07b canonical 不含图像 token

**现状（证据）**
- `core/trace.py:152-182` `canonical_request`：`175-181` 只拼 `role/tool_call_id/content/tool_calls`，**完全没有 `message.images`**。
- 对比 `context_compression.py:581-592` 的 `_history_tokens`：显式 `+ image_tokens(messages)`（`757-766`，每张 1600 token，`754`）。
- `ChatMessage.images` 在 `domain/message.py:220-222`，`ImageRef.bytes` 在 `189-198`。

**根因**
canonical 诞生于图像能力之前；估算函数升级了，canonical 没跟上，导致两套测量对图像不一致，且图像变化不会改变 canonical/digest。

**候选与取舍**
- A. canonical 每条消息追加 `|images:{n}`，只表达身份；事件估算另加 `image_tokens(request)`。**（推荐）**
- B. 把 1600×n 个填充字符写进 canonical：能让 `estimate_tokens` 自动对上，但污染 prefix 比较且浪费内存。
- C. 只改事件估算：prefix 仍看不出图像变化，不完整。

**推荐方案（签名）**
```python
# trace.py: canonical_request
for message in messages:
    ...
    images = getattr(message, "images", ()) or ()
    image_mark = ""
    if images:
        measured = sum(1 for ref in images if int(getattr(ref, "bytes", 0) or 0) > 0)
        image_mark = f"|images:{measured}"
    parts.append(f"{role}:{call_id}:{content}:{calls}{image_mark}")

# turn_report.py: report_context
from ai_coding.core.context_compression import image_tokens
...
estimated_tokens=estimate_tokens(canonical) + image_tokens(list(request)),
```
`canonical` 返回值只用于前缀比对，不含成本；`estimated_tokens` 只进事件/追踪。

**测试计划**
- 新建 `tests/test_trace.py`（白名单允许；现有最接近的是 `tests/test_trace_recorder.py`）：同一文本 + 无图 vs 有图，canonical 不同；两张 `bytes>0` 的图使 `estimate_tokens` 增加约 3200。
- `tests/test_turn_reporter.py`：带图请求的 `estimated_tokens` 比无图高约 `1600*n`。

**风险**
- canonical 变化会让跨版本 prefix 比较第一次全部 miss（一次性的，可接受）。
- `ImageRef.bytes == 0` 的旧会话不计费（与 `image_tokens` 一致），canonical 也只标 0——保持同一口径。

---

### CTX-07c `history_chars` 不含工具参数

**现状（证据）**
- `core/turn_report.py:152`：`history_chars=sum(len(message.content or "") for message in history)`。
- 对比同函数 `154` 的 `estimated_tokens` 基于 canonical，而 canonical `trace.py:176-178` **包含** `tool_calls[].arguments`。
- `context_compression._history_tokens`（`581-592`）也把 `_tool_args_text` 计入。

**根因**
`history_chars` 是早期“内容长度”指标，工具调用参数迁入 `tool_calls` 后没有同步。

**候选与取舍**
- A. 把 `arguments` 加入求和。**（推荐）**
- B. 改名为 `content_chars` 并新增字段：破坏事件消费者。
- C. 不改：指标长期低估工具密集请求。

**推荐方案（签名）**
```python
# turn_report.py:152
history_chars=sum(
    len(message.content or "")
    + sum(len(call.arguments or "") for call in (message.tool_calls or ()))
    for message in history
),
```
可提取 `_history_chars(messages)` 供本文件内复用。

**测试计划**
- `tests/test_turn_reporter.py`：消息含一个 `arguments='{"a":1}'` 的 tool call 时，`history_chars` 等于 content+arguments 长度；既有 `== len("hello")+len("hi")` 不受影响。

**风险**
- 仅指标口径变化；任何依赖旧值的外部看板会看到数值变大，这是修正而非回归。

---

### CMP-01 文件重注入只活一轮请求

**现状（证据）**
- `_reinject_recent_files`：`context_compression.py:1728-1792`，预算 `_REINJECT_FILE_TOKENS = 8_000`（`73`）、最多 5 个文件（`74`）。
- 只在 `compress` 的两个“摘要成功”分支调用：
  - `1319-1323`：已有 checkpoint、摘要成功；
  - `1439-1443`：首次压缩、摘要成功。
- `checkpoint_view`（`1584-1658`）**不调用**重注入；它只返回 `[_checkpoint_note(previous), *tail]`（`1635`）或纯 L0 结果（`1648-1653`）。
- 生产调用链：`assemble` → `Compactor.compress`（`request_assembly.py:386`）→ `Compactor._compress_once`（`compaction.py:146-175`）→ 若 gate 关闭返回 `view`，否则 → `ContextManager.compress_if_needed`（`compaction.py:367-399`）→ 再 gate，开则 `ContextManager.compress`（`context_compression.py:1695-1703`）。

**根因**
重注入被当成“摘要成功的副作用”，而不是“checkpoint 视图的组成部分”。摘要只发生一次，快照就只存在那一轮；下一轮 `checkpoint_view` 重建视图时没有它。

**重复风险验证（重点）**
逐条走现在的调用图：

1. gate 关闭（`compaction.py:171-175`）：直接返回 `view`，**不会**调用 `compress` → 只有一份。
2. gate 打开：`ContextManager.compress_if_needed` 内部再算一次 `view`（`1684`）仅用于 gate；gate 开则调用 `compress(messages)`（`1695`），并用 `compress` 的返回值作为最终请求 → 只有 `compress` 里的那一份。
3. `compress_if_needed` 返回前**不会**把 `compress` 的结果再喂给 `checkpoint_view`（`1704-1726` 只做 hard trim / pinned）→ 不存在“view + compress 各一份”。
4. `Compactor._compress_once` 也不会把 `view` 与 `_compress` 结果拼接（`188-198` 二选一）。

**结论：在当前调用图下，同时保留 `compress` 注入 + 在 `checkpoint_view` 注入，不会在同一请求出现两份。** 方向里的“避免重复”是结构性预防（防止未来某个调用方把两者组合），不是当前已存在的 bug。

**但**：如果按方向“`compress` 不再单独追加”，**首次压缩成功的那一轮会丢快照**——因为 `compress_if_needed` 拿到 `compress` 的 `out` 后不会自动应用 view。必须同时把挂载动作放到 `compress_if_needed`，否则是回归。

**推荐方案（签名）**
```python
# context_compression.py
_REINJECT_HEADER = "[压缩后重新载入的文件快照]"

def _attach_reinjection(
    self, messages: list[ChatMessage], session: Any
) -> list[ChatMessage]:
    """在 checkpoint note 之后插入一份文件快照；已有则替换，绝不重复。"""
    snapshot = self._reinject_recent_files(session)
    if not snapshot:
        return messages
    body = [m for m in messages if not (m.content or "").startswith(_REINJECT_HEADER)]
    if body and _is_checkpoint_note(body[0]):
        body = [body[0], *snapshot, *body[1:]]
    else:
        body = [*body, *snapshot]  # 无 checkpoint note 时放尾部，保持结构
    return body

# checkpoint_view：先判“L0 结果是否更小”，再挂快照
core = ...                       # 现有 1635 或 1648-1653
if _history_chars(core) >= _history_chars(list(messages)):
    return None
core = self._attach_reinjection(core, session)
out = _ensure_pinned(core, pinned)
out = sanitize_pairs(out)
return out

# compress：删除 1321、1441 的 *self._reinject_recent_files(session)

# compress_if_needed：compress 返回后挂载
out = await self.compress(...)
out = self._attach_reinjection(out, session)
...  # 再 hard trim / _ensure_pinned
```
`bank` 变量仅为可读性，实际直接过滤即可；关键点是**按 header 前缀去重**，即使未来两个入口都调用，也只会留一份。

**测试计划**
- 修改 `tests/test_microcompact_and_refresh.py:244-254`：改为 `await manager.compress_if_needed(..., force=True)`，断言仍有快照（覆盖新挂载点）。
- 新增：`compress(messages)` 原始输出**不含**快照；`compress_if_needed` 输出含且仅含一份。
- 新增：连续两次 `checkpoint_view` 都含快照，且拼接文本里 `_REINJECT_HEADER` 只出现一次（验证不重复、内容稳定）。
- 保留 `test_the_refresh_is_capped`、`test_a_missing_file_is_not_reinjected`、`test_an_oversized_file_is_truncated_with_a_notice`。

**风险**
- `ContextManager.compress` 是公共 API（测试/脚本直接调用，见 `scripts/r7_cases.py:264`），契约从“含快照”变为“不含”，必须在 docstring 与 changelog 里写明。
- 快照进入 `checkpoint_view` 后，如果它让 view 不再“更小”，必须按上面顺序**先判定再挂载**，否则会把整个 view 判成 `None`，连带 L0 一起失效。

---

### CMP-02 `checkpoint_view` 每轮全量回扫

**现状（证据）**
- `context_compression.py:1584-1658`：每次调用都对 `messages[used:]`（`1629-1634`）或整个 `messages`（`1648-1653`）跑 `microcompact_old_results`，再 `1656-1657` 用 `_history_tokens(out) >= _history_tokens(list(messages))` 判断。
- 每个请求至少调用两次：`compaction.py:160` 一次、`context_compression.py:1684` 一次（gate 打开时）。
- `microcompact_old_results`（`369-400`）内部还要逐条 `estimate_tokens`。

**根因**
视图是纯函数但没缓存；判定“更小”又用了比内容更贵的 token 估算（包含工具参数和图像）。

**候选与取舍**
- A. 字符数判定 + 单槽 memo（key：`used/anchor/len/last.id`）。**（推荐，符合定案方向）**
- B. 只做 memo 不改判定：仍要跑两次 token 估算。
- C. 进程外缓存到磁盘：过度设计。

**推荐方案（签名）**
```python
# context_compression.py
def _history_chars(messages: Sequence[ChatMessage]) -> int:
    return sum(len(m.content or "") for m in messages)

# ContextManager.__init__
self._view_memo: tuple[tuple[Any, ...], list[ChatMessage]] | None = None

def _l0_view(
    self, messages: list[ChatMessage], used: int, anchor: str
) -> list[ChatMessage]:
    last = messages[-1].id if messages else ""
    key = (used, anchor, len(messages), last)
    if self._view_memo is not None and self._view_memo[0] == key:
        return list(self._view_memo[1])
    out = microcompact_old_results(
        list(messages[used:] if 0 < used <= len(messages) else messages),
        protect_tokens=self._microcompact_protect_tokens(),
        elide_root=self._root,
        window_chars=self._l2_window_chars,
        window_budget=self._l2_window_budget,
    )
    self._view_memo = (key, out)
    return list(out)
```
`checkpoint_view` 用它替换两处 `microcompact_old_results`；`1656` 改成 `if _history_chars(out) >= _history_chars(list(messages)): return None`。

**测试计划**
- `tests/test_microcompact_and_refresh.py:170-187`（可重复性）继续通过。
- 新增：monkeypatch `microcompact_old_results` 计数，同一 `messages` 连调两次 `checkpoint_view` 只执行一次；追加一条消息后再次执行。
- 新增：仅修改某条消息内容、不改变长度与尾 id 时 memo 会命中（把该限制写成测试，明确是已知边界）。

**风险**
- key 不含消息内容哈希：**同长度原地改写**（非 append-only 路径，如 undo/rewrite）会命中旧视图。当前 `session.messages` 是 append-only（`context_compression.py:1170` 复制、注释 `docs/features.md:240`），风险可控；需在 docstring 写明。
- memo 返回浅拷贝列表，元素仍是原 `ChatMessage`；`sanitize_pairs`/`_ensure_pinned` 不原地改消息内容，安全。

---

### CMP-03 `_MICROCOMPACT_PROTECT_TOKENS = 40_000` 不随窗口

**现状（证据）**
- `context_compression.py:366` 固定 40k。
- 三处调用都用默认值：`compress` 的 L0（`1180-1185`）、`checkpoint_view` 有 checkpoint（`1629-1634`）、无 checkpoint（`1648-1653`）。
- 函数默认参数在 `369-375`。

**根因**
40k 是“Claude Code 同量级”的经验值（注释 `359-366`），但 6k/48k 窗口下它可能比整个窗口还大，等于 L0 永不生效；大窗口下又可能太保守。

**候选与取舍**
- A. `max(8_000, min(40_000, window // 3))`。**（推荐，已定案）**
- B. 直接 `window // 3`：小窗口会保护过少，丢掉模型正在用的结果。
- C. 只按窗口百分比、无上下限：极端窗口不可控。

**推荐方案（签名）**
```python
# context_compression.py: ContextManager
def _microcompact_protect_tokens(self) -> int:
    return max(8_000, min(_MICROCOMPACT_PROTECT_TOKENS, self._max_context_tokens // 3))
```
三处调用加 `protect_tokens=self._microcompact_protect_tokens()`；`microcompact_old_results` 的默认参数保持 40k（外部/测试兼容）。

**测试计划**
- `tests/test_microcompact_and_refresh.py` 新增参数化：window=6_000 → 8_000；48_000 → 16_000；131_072 → 40_000；393_216 → 40_000。
- 既有显式传 `protect_tokens=` 的测试（`71-116`）不受影响。

**风险**
- 小窗口（`_manager` 用 3_000）从“几乎不清”变为“清到只剩 8k 保护”，可能让 `test_a_compaction_reinjects...` 的 L0 行为变化；断言只检查快照存在，预计通过，但要在实施时跑全量。
- 8k 下限对小窗口仍是“大于窗口”的保护；这是方向明确要求的下限，不在本批推翻。

---

### CMP-04 信封可能比正文更大

**现状（证据）**
- `_NOTE_ALLOWANCE = 160`（`context_compression.py:56`）。
- `_elide_tool_results` 在 `343-345` 只做 `len(body) <= 2*window + 160` 的**预估**跳过；判定假设信封 ≤ 2×window+160，但真实信封是 `note + 2×window + 3`（`265-269`），`note` 含 artifact 绝对路径（`259-262`、`232-249`），路径可远超 160。
- 规则 1 的注释在 `282`，现有测试 `tests/test_context_compression_l2_window.py:68-76` 只覆盖短路径。

**根因**
用常数 allowance 代替真实长度比较；artifact 路径长度不可控（长 `tmp_path`/深工作区）。

**候选与取舍**
- A. 构造候选后真实比较 `len(candidate) < len(body)`，不满足则保持正文。**（推荐，已定案）**
- B. 按 `len(path)+固定前缀` 预估后再决定是否写 artifact：能避免孤儿文件，但要把 note 构造逻辑前置，复杂。
- C. 截断路径：路径是唯一找回原文的线索，不能截。

**推荐方案（签名）**
```python
# context_compression.py: _elide_tool_results
note = previous_result_note(m.tool_name or "tool", len(body), path)
candidate = _elided_body(body, note, window)
if len(candidate) >= len(body):
    continue  # 信封不比正文小：保留正文；artifact 已写，内容寻址、可复用
out[i] = _tool_copy(m, candidate)
budget -= window
```
`343-345` 的预估可保留为快速跳过，真实比较作为最后闸门。

**测试计划**
- `tests/test_context_compression_l2_window.py` 新增：`elide_root=tmp_path/("x"*120)`，构造 `len(body)=161`（`window_chars=0`），断言 `out[0].content == body`。
- 既有 `test_the_envelope_is_never_bigger_than_the_body`（`68-76`）继续通过。

**风险**
- 会留下一个不再被引用的 artifact（`transcripts/elided/<hash>`）；内容寻址且幂等，可接受，但要在注释里说明。
- 如果 `path is None`，现有 `347-352` 已跳过，不进入比较。

---

### CMP-05 provider 冻结读数蒙蔽 gate

**现状（证据）**
- `request_assembly.py:248-250`：只要 `session.live_context_tokens > 0` 就无条件返回它，**没有“是否变化”的判断**。
- 写入点：`agent_loop.py:3064-3069`，每个 step 用 `context_tokens or prompt_tokens` 覆盖；新 turn 清零在 `1679`。
- 实测背景（任务给出）：会话 `dc35e24b` 的 prompt 连续冻结在 **4978**，历史在涨，gate 仍读到 4978 → 永不触发压缩。
- `working_history_tokens` 已实现 checkpoint-aware 估算（`context_compression.py:595-614`）。

**根因**
`live_context_tokens` 被当作“永远可信的最新读数”，但某些 provider/兼容模式会返回不随请求增长的缓存值；没有陈旧检测。

**候选与取舍**
- A. 在 assembler 内维护“上一次读数 + 连续相同次数 + 本地估算涨幅”，连续 3 次后降级到 `working_history_tokens(session)`。**（推荐，已定案）**
- B. 每次都用 max(live, local)：会长期高估，gate 过早压缩。
- C. 按时间判定：与 turn/step 节奏无关，不稳定。

**推荐方案（签名）**
```python
# request_assembly.py
class _LiveReadingWatch:
    """Detect a provider count that stopped moving while the history grows."""
    def __init__(self) -> None:
        self._session = ""
        self._live = 0
        self._local = 0
        self._repeats = 0

    def observe(self, session: SessionData, local_tokens: int) -> int | None:
        session_id = str(getattr(session, "session_id", "") or "")
        if session_id != self._session:
            self._session, self._live, self._local, self._repeats = session_id, 0, 0, 0
        live = int(getattr(session, "live_context_tokens", 0) or 0)
        if live <= 0:
            self._live, self._local, self._repeats = 0, local_tokens, 0
            return None
        grew = self._local > 0 and local_tokens > self._local * 1.05
        self._repeats = self._repeats + 1 if (live == self._live and grew) else 0
        self._live, self._local = live, local_tokens
        return local_tokens if self._repeats >= 3 else None

def _observed_prompt_tokens(
    session, view=None, *, watch: _LiveReadingWatch | None = None
) -> int:
    live = int(getattr(session, "live_context_tokens", 0) or 0)
    if live > 0:
        if watch is not None:
            override = watch.observe(session, working_history_tokens(session))
            if override is not None:
                return override
        return live
    ...  # 现有逻辑不变
```
`RequestAssembler.__init__` 建 `self._live_watch = _LiveReadingWatch()`；`_compactor_extras`（`932-969`）调用时传 `watch=self._live_watch`。

**测试计划**
- `tests/test_compaction_gate_working_view.py` 新增：
  - `live` 恒为 4978、`working_history_tokens` 每次涨 6%：前两次返回 4978，第三次仍返回 4978，第四次返回本地值；
  - `live` 变化时 `_repeats` 归零；
  - `live=0` 时行为与现有 `test_observed_prompt_tokens_falls_back_to_the_working_view` 完全一致。
- 既有 `282-326` 全部保持通过（默认 `watch=None` 不改变行为）。

**风险（最危险）**
- 阈值/次数是启发式：如果 provider 的 prompt 计数在“同一请求重试”时合法地保持不变，而本地估算因 L0/图像误差涨了 5%，可能误降级，导致 gate 提前开或不开。
- `working_history_tokens` 是字符估算，可能高估/低估；它只作为 gate 的**降级**输入，不直接决定窗口，但会改变压缩时机。
- 该状态在 assembler 实例上：若一个 loop 复用多个 session，必须按 `session_id` 隔离（上面已做）；若同一 session 跨 turn，`live=0` 时会重置（`agent_loop.py:1679`），符合“每 turn 重新观察”。

---

## 2. 批次内顺序

分 5 波，后一波依赖前一波的测试与语义稳定。

1. **Wave 0（零行为风险，先清噪音）**
   - CTX-07c（一行求和）→ CTX-07b（canonical 标记 + 事件估算）→ CTX-07a（`offered_tools`）→ CTX-06（删死指标）。
   - 理由：都不改压缩/预算决策，可先让指标与报告变准，后续对比有可信基线。
2. **Wave 1（请求装配，互不依赖）**
   - CTX-02 → CTX-03 → CTX-01 → CTX-04（skill 去重 + 删 `PLAN_TEXT_RULE` 重复）。
   - 理由：都在 `_referenced_files_block` 附近，按“先省 I/O、再加预算、最后去重”的顺序减少 diff 冲突。
3. **Wave 2（记忆）**
   - CTX-05。
   - 理由：独立文件，放中间便于单独回滚。
4. **Wave 3（压缩局部）**
   - CMP-04 → CMP-03。
   - 理由：CMP-04 是 L2/L0 共用信封逻辑，先修正确性；CMP-03 改变 L0 的选择输入，后做便于定位。
5. **Wave 4（压缩结构性，风险最高）**
   - CMP-02（memo + 字符判定）→ CMP-01（重注入收口，依赖 memo 的 key 已含“尾消息 id”）→ CMP-05（gate 降级，最后做，便于用前四项后的真实请求验证）。
   - 理由：CMP-01 的重注入会进入 `checkpoint_view`，必须先有 memo 才能保证“内容稳定 + 每轮只算一次”；CMP-05 改变 gate 输入，放最后避免掩盖前三项的回归。

---

## 3. 明确不做

1. 不改默认窗口：不动 `DEFAULT_CONTEXT_TOKENS`、`config/models.py` 的默认值、`ContextManager` 的 48k 兜底（`context_compression.py:1011-1013`）。
2. 不重写 `session.messages`：保持 append-only，不把压缩结果写回会话（`context_compression.py:1170`、`docs/features.md:240`）。
3. 不改 L3→L1→L2→L4→hard trim 的层序（`context_compression.py:7-9`）。
4. 不改 `closing` 步的 `tools=[]` 行为（`agent_loop.py:2956-2961`），只修估算。
5. 不删除 `Pricing`（`cli.py:341-348`、`app/trace_text.py:460` 在用）、`totals`、`record_step`、`usage_dict`。
6. 不引入新依赖（不做精确 tokenizer，不做 tiktoken）。
7. 不联网、不 commit、不 push、不修改本文件之外的白名单文件。
8. 不弱化已有断言；删除的测试必须对应删除的 API（CTX-06），不做“改成恒真”。
9. 不迁移 memory 存储格式（CTX-05 只做渲染/新写入截断，旧 note 原样保留）。
10. 不实现 CTX-04 的完整单一来源（方案 A）；仅做 skill 去重与 `PLAN_MODE_BLOCK` 内规则去重，完整方案 DEFERRED 并记录所需白名单扩展。
11. 不为 CMP-02 引入跨请求/磁盘缓存。

---

## 4. 与既定方向冲突 / 需要决策的发现

1. **CTX-01 的描述与代码不完全一致**：`request_assembly.py:790` 已经对目录/裸路径执行 `used += estimate_tokens(block)`，所以不是“不扣预算”，而是“无准入、无上限、目录条目不缩放”。方案按“补准入 + 缩放”落实，不推翻方向，但根因表述需要修正。
2. **CMP-01 的重复风险在当前调用图下不存在**：`Compactor._compress_once` 在 view 与 compress 之间二选一（`compaction.py:160-198`），`compress_if_needed` 也不会把两者拼接（`context_compression.py:1695-1726`）。但**若按方向把 `compress` 的注入删掉，首次压缩成功的那一轮会丢快照**，因为 `compress_if_needed` 不会自动应用 view。方案因此增加 `_attach_reinjection` 并在 `compress_if_needed` 后置挂载；这是对方向的必要补全，不是推翻。
3. **白名单不足以完成 CTX-06 / CTX-05 / CTX-04 方案 A**：
   - CTX-06 必删字段的测试全在 `tests/test_budget_ledger.py`（`33,42-58,61-69,81-84,89-92,157-170,184-225`），该文件不在白名单。
   - CTX-05 的记忆测试不在白名单（现有 memory 测试文件均未列入）。
   - CTX-04 方案 A 需要改 `tests/test_prompt.py`、`tests/test_plan_codex_path.py`，均不在白名单。
   - 建议：要么扩展白名单，要么本批只执行不触碰这些测试的部分（CTX-06/CTX-05 将无法满足“同步改测试”的约束）。
4. **`tests/test_trace.py` 不存在**：白名单列出的该文件需要新建（现有相近文件是 `tests/test_trace_recorder.py`/`test_trace_text.py`）。CTX-07b 建议新建 `tests/test_trace.py`，与白名单一致。
5. **CMP-05 缺少“上一步读数”的会话字段**：`domain/session.py:336-341` 只有 `live_context_tokens` / `context_measure_invalid`，且 `domain/session.py` 不在白名单。方案把观察状态放在 `RequestAssembler` 实例（`request_assembly.py` 内），按 `session_id` 隔离；这是在不改会话模型前提下的唯一可行落点。
6. **CMP-02 的 memo key 无法覆盖“同长度原地改写”**：方向给出的 key（消息数/尾消息 id/anchor）在 append-only 下成立，但 undo/rewrite 路径可能同长度改内容。方案保留该 key 并把限制写成测试/注释；若要求绝对正确，需把内容哈希加入 key（成本接近重扫，收益消失）。
7. **CMP-04 会在跳过时留下孤儿 artifact**：方向要求“实际比较”，而真实路径长度只有写盘后才知道；若先写后比，被跳过的那次会留下内容寻址文件。方案接受该成本并在注释说明；若不可接受，需改为“先构造成本上界再决定是否写盘”（与本批方向不同）。

---

## 5. 文件清单

### 生产文件（均在白名单）

| 文件 | 涉及项 | 改动要点 |
|---|---|---|
| `ai_coding/core/request_assembly.py` | CTX-01/02/03/04/07a/CMP-05 | 文件块预算与目录缩放；裸指针/显式文件 `stat`；plan 规则去重；skill 去重；`offered_tools`；`_LiveReadingWatch` |
| `ai_coding/core/context_compression.py` | CMP-01/02/03/04 | `_history_chars`、view memo、`_microcompact_protect_tokens`、`_attach_reinjection`、信封真实比较 |
| `ai_coding/core/budget.py` | CTX-06 | 删除无生产读者的只读面与 `record_assembly`；保留 `Pricing`/`totals`/`usage_dict` |
| `ai_coding/core/turn_report.py` | CTX-06/07b/07c | 去掉 `record_assembly` 调用；`history_chars` 计工具参数；`estimated_tokens` 补图像成本 |
| `ai_coding/core/trace.py` | CTX-07b | `canonical_request` 追加 `|images:n` |
| `ai_coding/core/agent_loop.py` | CTX-07a | `_ask_for_closing` 传 `offered_tools=False` |
| `ai_coding/memory/service.py` | CTX-05 | `_clip_note_text`；`index_notes`/`render_index_block`/`_reconcile` 限长 |

### 测试文件（白名单内）

| 文件 | 涉及项 |
|---|---|
| `tests/test_file_mentions.py` | CTX-01/02/03/04（skill 与文件块） |
| `tests/test_request_assembly.py` | CTX-04/07a；CMP-05 的 assembler 行为 |
| `tests/test_microcompact_and_refresh.py` | CMP-01/02/03、CTX-07b 相关估算 |
| `tests/test_context_compression_l2_window.py` | CMP-04、CMP-03 |
| `tests/test_compaction_gate_working_view.py` | CMP-05、CTX-07b |
| `tests/test_turn_reporter.py` | CTX-07b/07c、CTX-06（不再依赖 `record_assembly`） |
| `tests/test_trace.py`（需新建） | CTX-07b canonical 图像标记 |

### 不在白名单、但按方案必须触碰（需决策）

| 文件 | 原因 |
|---|---|
| `tests/test_budget_ledger.py` | CTX-06 删除字段后必须同步删除/改写测试 |
| `tests/test_memory_*.py` | CTX-05 需要在 `index_notes`/`_reconcile` 层验证截断 |
| `tests/test_prompt.py`、`tests/test_plan_codex_path.py` | CTX-04 完整方案 A 才需要；本批选方案 B，预计不需要 |
| `ai_coding/domain/session.py` | 仅当 CMP-05 要持久化“上一步读数”时才需要；本方案不改 |

### 明确不改（白名单内但无改动）

| 文件 | 原因 |
|---|---|
| `ai_coding/core/file_mentions.py` | 解析层无需变更；CTX-01/02/03 的改动都在 `request_assembly.py` 的渲染/预算侧 |

---

## 6. 验收口径（供后续实施批使用）

- 全量 `pytest` 通过；`ruff`/`mypy` 不新增错误。
- 新增测试必须能在**旧实现**上复现问题（红 → 绿），尤其是：CTX-01 超预算目录、CTX-02 不再 `read_text`、CTX-03 不整读大文件、CMP-01 连续两请求各一份快照、CMP-02 同一请求只扫一次、CMP-04 长路径信封不替换正文、CMP-05 冻结读数第 4 次降级。
- 不修改默认窗口；不改变 `/compact` 的 force 语义；不改变 `closing` 的 `tools=[]`。
