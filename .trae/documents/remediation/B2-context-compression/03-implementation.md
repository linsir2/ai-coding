# B2 上下文工程 + 压缩遗留 —— 实施记录（S3）

- 依据：`01-research.md`、`02-design-review.md`（逐条裁决）
- 基线：`fd5a929`
- 范围：CTX-01/02/03/04(skill 去重)/05/07a/07b/07c、CMP-01/02/03/04/05；CTX-06 降级为 docstring
- 未 commit

## 1. 逐项改动

### CTX-01 裸路径/目录块的准入与缩放
`ai_coding/core/request_assembly.py`
- 新增 `_DIR_ENTRY_TOKENS = 8`。
- `_referenced_files_block`：目录块按剩余预算缩放条目数
  `max_entries = max(1, min(_DIR_ENTRIES, max(0, budget-used)//_DIR_ENTRY_TOKENS))`；
  目录/裸路径生成后做真实 `estimate_tokens` 判定，**超预算即追加一行 `no room left ...` 并停止继续添加**（`break`），不再等长替换。
- `_directory_listing(..., max_entries=_DIR_ENTRIES)`，`[+N more]` 随实际显示数计算。
- 新增模块级 `_no_room_line(rel)`，显式文件与低价值块共用同一文案。

### CTX-02 裸路径只 `stat`
`request_assembly.py`
- `_bare_pointer` 改为 `target.stat().st_size` + `_AVG_LINE_BYTES` 估算，输出“约 N 行”，不再 `read_text`。

### CTX-03 显式 @ 只读前缀
`request_assembly.py`
- 新增 `_MAX_INLINE_FILE_BYTES = 2_000_000`、`_BINARY_SUFFIXES`（图片后缀 + pdf/zip/tar/whl/exe/so/dll/class/jar）、`_AVG_LINE_BYTES = 40`、`_approx_lines()`。
- `_file_content`：先 `stat`；超限或二进制后缀直接给指针；否则只读 `room_tokens*4+1` 字符；截断时总行数/总字符数按前缀比例估算，文案加“约”；`read offset=N` 语义保持不变。

### CTX-04 只做 skill 去重
`request_assembly.py`
- `resolve_mentions` 提前到 router 之前计算；路由命中集合中剔除用户已显式点名的 id，避免同一 skill 同时出现在 `<recommended-skills>` 与 `<requested-skills>`。
- `PLAN_TEXT_RULE` / `ai/prompt.py` / `PLAN_MODE_BLOCK` 未改动。

### CTX-05 单条长记忆截断
`ai_coding/memory/service.py`
- 新增 `_MAX_NOTE_CHARS = 400`、`_clip_note_text(text, limit)`。
- `index_notes`：按 `budget - used - len(prefix) - 1` 逐条截断，并把截断后的文本放进返回的 note（`dataclasses.replace`），渲染不再二次膨胀。
- `render_index_block`：对直接传入的 note 再做 400 字上限保护。
- `_reconcile`：新写入/更新的候选文本截断到 400 字；旧数据不改。

### CTX-06 budget.py 死指标（仅注释）
`ai_coding/core/budget.py`
- `record_assembly`、`latency_ms`、`estimated_tokens`、`prefix_shared_ratio`、`calibration`、`cache_hit_ratio`、`by_model`、`cost` 补 docstring：“诊断用途、无生产消费者”。
- 未删除/新增任何字段，未改 `tests/test_budget_ledger.py`。

### CTX-07a closing 步不计工具 schema
`ai_coding/core/request_assembly.py`、`ai_coding/core/turn_report.py`、`ai_coding/core/agent_loop.py`（见偏离 1）
- `assemble(..., offered_tools: bool = True)`；为 False 时不向 reporter 传 `tools_text`（传 `None`，不覆盖缓存）。
- `TurnReporter.report_context(..., offered_tools: bool = True)`：False 时本次 canonical 用空 schema、事件 `tools_chars=0`，**不修改** `self._tools_text`。
- `_ask_for_closing` 传 `offered_tools=False`。

### CTX-07b canonical 含图像、事件估算含图像成本
`ai_coding/core/trace.py`、`ai_coding/core/turn_report.py`
- `canonical_request` 每条消息追加 `|images:n`（仅统计 `bytes > 0` 的引用，与 `image_tokens` 同口径）。
- `report_context` 的 `estimated_tokens = estimate_tokens(canonical) + image_tokens(list(request))`。

### CTX-07c history_chars 含工具参数
`ai_coding/core/turn_report.py`
- `history_chars` 求和加入 `tool_calls[].arguments`。

### CMP-01 重注入收口到请求视图
`ai_coding/core/context_compression.py`
- 新增 `_REINJECT_HEADER`、`_same_history()`、`ContextManager._attach_reinjection()`（按 header 去重，插在 checkpoint note 之后或尾部）。
- `compress` 的两处 `_reinject_recent_files` 删除；docstring 明确“compress 返回值不含快照”。
- `checkpoint_view`：先判定 L0 结果是否更小（`_history_chars`），再挂快照，最后 `_ensure_pinned` / `sanitize_pairs`。
- `compress_if_needed`：在 `hard_trim` 与 `_announce_omissions` **之后**、`_ensure_pinned` **之前**挂载；仅当 `not _same_history(out, messages)` 时挂载。

### CMP-02 checkpoint_view 记忆化 + 字符判定
`context_compression.py`
- 新增 `_history_chars()`。
- `ContextManager.__init__` 增加 `_view_memo`。
- 新增 `ContextManager._l0_view(messages, used, *, anchor)`：memo key 含
  `used / anchor(摘要) / len(source) / 末条 id / l2_window_chars / l2_window_budget / protect_tokens`。
- `checkpoint_view` 改用 `_l0_view`，最终“是否更小”改用 `_history_chars`。

### CMP-03 protect 随窗口
`context_compression.py`
- 新增 `_microcompact_protect_tokens()`：`max(8_000, min(40_000, window//3))`。
- `compress` 的 L0、`checkpoint_view` 的 L0 均显式传入；`microcompact_old_results` 默认值保持 40k（兼容外部调用）。
- 注释与本文档声明：**仅 <120k 窗口行为改变**；≥120k 时 `window//3 ≥ 40k`，与旧值一致；8k 下限只影响 <24k 窗口。

### CMP-04 信封真实比较
`context_compression.py`
- `_elide_tool_results`：先构造 `candidate`，`len(candidate) >= len(body)` 则保留正文（artifact 已写、内容寻址可复用），不再只靠 `_NOTE_ALLOWANCE` 预估。

### CMP-05 冻结读数降级
`ai_coding/core/request_assembly.py`
- 新增 `_LiveReadingWatch`：状态以 `(session_id, turn_count)` 为键，turn 变化即清零；连续 3 次 `live` 相同且本地估算涨 >5% 时返回本地估算。
- `_observed_prompt_tokens(..., *, watch=None)`；`RequestAssembler.__init__` 建 `_live_watch`，`_compactor_extras` 传入。

## 2. 测试与证据

新增/更新测试：
- `tests/test_file_mentions.py`：裸路径不整读、约行数、目录条目随窗口缩放、裸路径超预算停止、>2MB/二进制指针、截断总行数改为近似断言（CTX-03 裁决 6）。
- `tests/test_request_assembly.py`：skill 去重（点名不推荐、未点名仍推荐）、`offered_tools`。
- `tests/test_microcompact_and_refresh.py`：CMP-01 新契约（compress 不含快照、compress_if_needed 含且仅一份、checkpoint_view 跨请求稳定、/compact 无变化不挂载）、CMP-02 单次扫描/窗口维度/20 轮性能、CMP-03 参数化。
- `tests/test_context_compression_l2_window.py`：长 artifact 路径下信封不替换正文。
- `tests/test_compaction_gate_working_view.py`：冻结读数第 4 次降级、读数变化复位、跨 turn 复位、`_observed_prompt_tokens` 降级。
- `tests/test_turn_reporter.py`：history_chars 含工具参数、图像计费、`offered_tools=False` 不污染缓存。
- `tests/test_trace_canonical.py`（新建）：canonical 图像标记。
- `tests/test_memory_index_invariants.py`：单条 3000 字记忆被截断到 600 预算、`_reconcile` 落库 ≤400。
- `tests/golden/loop_events/exhausted.json`：按新指标重录（见偏离 2）。

命令与结果（实际运行）：
```
.venv/bin/python -m pytest tests/test_file_mentions.py tests/test_request_assembly.py \
  tests/test_microcompact_and_refresh.py tests/test_context_compression_l2_window.py \
  tests/test_compaction_gate_working_view.py tests/test_turn_reporter.py \
  tests/test_context_compression.py tests/test_compaction_checkpoint_view.py
# 165 passed
.venv/bin/ruff check ai_coding tests/test_file_mentions.py tests/test_request_assembly.py tests/test_microcompact_and_refresh.py
# All checks passed!
.venv/bin/mypy ai_coding
# 仅 1 个与本次改动无关的错误：ai_coding/core/scheduler.py:697 (Cannot infer type of lambda)
.venv/bin/mypy <本次改动的 6 个模块>
# Success: no issues found
```
补充：含新增测试的扩展集合（+ `test_trace_canonical.py` / `test_memory_index_invariants.py` / `test_loop_refactor_equivalence.py`）174 passed。

## 3. 偏离项

1. **修改了白名单外的 `ai_coding/core/agent_loop.py`**：`_ask_for_closing` 增加 `offered_tools=False`。CTX-07a 只有调用方传入该参数才生效，`01-research.md` §5 也将该文件列为 CTX-07a 的生产文件；若不改，参数是死代码。
2. **更新了白名单外的 `tests/golden/loop_events/exhausted.json`**：CTX-07a/07c 有意改变了 golden 中的 `tools_chars`（closing 步 116→0）与 `history_chars`（48→50、94→98），用该测试自带的 `AI_CODING_RECORD_GOLDEN=1` 重录，未放宽断言。
3. CTX-06 按裁决 7 只加 docstring，未删除任何字段。
4. CTX-04 按裁决 8 只做 skill 去重，plan 文本三份问题保持现状（DEFERRED）。
5. 工作区存在其他批次并发修改（如 `ai/prompt.py`、`core/scheduler.py`、MCP/subagent 相关），本报告的全量 pytest 因并发编辑中断；上述针对性测试均在当前树上通过。

## 4. 已知限制

- CMP-02 memo key 不含内容哈希：同长度原地改写（undo/rewrite）可能命中旧视图；append-only 路径不受影响，`_l0_view` docstring 已写明。
- CMP-04 被跳过的信封会留下一个内容寻址的孤儿 artifact（可复用，不丢正确性）。
- CTX-05 只在渲染与新写入侧限长，不迁移旧存储的长 note；单条渲染硬上限 400 字。
- CTX-03 的总行数/总字符数变为“约”值，依赖精确总数的读者需接受近似。
- CTX-01 的 `no room left` 行本身可能让块略微超过预算（仅一条短行），但此后不再添加任何条目。
- CMP-03 的 8k 下限对 <24k 窗口仍可能大于窗口；这是定案要求的下限。
- CMP-05 是启发式（5% + 连续 3 次），误判只会改变压缩时机的早晚，不改变请求内容。

## 5. 评审整改（REQUEST-CHANGES）

仅改 `ai_coding/core/context_compression.py`、`ai_coding/core/request_assembly.py` 及对应测试；未改默认窗口、未放宽已有断言、未新增依赖、未 commit。

### P1-1 快照不再让视图变大
`ai_coding/core/context_compression.py`
- `checkpoint_view`：只有 `read_checkpoint` 返回**有效 checkpoint**（`has_checkpoint`）时才调用 `_attach_reinjection`；无 checkpoint 的 L0 视图不再挂 8k 快照。挂载后仍要求 `_history_chars(mounted) <= _history_chars(messages)`，否则保留不含快照的 L0 视图。
- 返回前补最后一道不变量：`_history_chars(out) > _history_chars(messages)` 时返回 `None`（调用方使用原始历史）。`_ensure_pinned` / `sanitize_pairs` 之后统一判定。
- `compress_if_needed`：同样仅在 `read_checkpoint(session, out)` 能读到有效 checkpoint 时挂载，并保留“不新于输入历史”的字符上限。
- 新增 `tests/test_microcompact_and_refresh.py::test_a_checkpointless_view_never_mounts_a_snapshot`：无 checkpoint + 大文件快照场景，断言视图无 `[压缩后重新载入的文件快照]` 且 `_history_chars(view) <= _history_chars(messages)`。旧实现会挂快照并超过输入。

### P2-1 live 读数不再每迭代扫全历史
`ai_coding/core/request_assembly.py`
- 新增 `_LiveReadingWatch.observe_lazy(session, local_fn)`：只在读数**重复**时才取一次本地估算作基线，达到 `_REPEATS` 阈值时再取一次；读数持续变化时一次扫描都不做。`observe()` 保留原行为，供原有单测。
- `_observed_prompt_tokens`：`live > 0` 时通过 `observe_lazy` 判断冻结，否则直接返回 `live`；不再无条件调用 `working_history_tokens(session)`。
- CMP-05 的“连续 3 次 + 本地涨 >5%”语义保持不变（`test_observed_prompt_tokens_falls_back_to_the_working_view_when_frozen` 仍通过）。
- 新增 `tests/test_compaction_gate_working_view.py::test_a_live_reading_does_not_scan_the_whole_history`：monkeypatch `working_history_tokens` 计数，live>0 首轮调用应为 0 次。旧实现为 1 次。

### P2-2 降级行计入预算并停止添加
`ai_coding/core/request_assembly.py`
- 目录 listing 超预算、裸路径超预算、显式 `@` 在 `room <= 0` 三处：`_no_room_line` 一律 `used += estimate_tokens(line)`，随后 `break`（显式 `@` 由 `continue` 改为 `break`）。
- 新增 `tests/test_file_mentions.py::test_explicit_mentions_stop_once_the_budget_is_spent`：400 个显式 `@` + 6k 窗口，断言 `no room left` 仅 1 行且整块 `estimate_tokens <= 900 + 5000`。旧实现输出 400 行未计数降级行。

### P2-3 memo key 补 session_id
`ai_coding/core/context_compression.py`
- `_l0_view(..., session_id="")`，memo key 首维加入 `session_id`；`checkpoint_view` 从 `session.session_id` 读取并传入。
- 新增 `tests/test_microcompact_and_refresh.py::test_the_memo_key_covers_the_session`：同一 manager、同一批消息对象、两个不同 session，断言 L0 扫描 2 次。旧实现命中 memo 只扫 1 次。

### 验证（实际运行）
```
.venv/bin/python -m pytest tests/test_microcompact_and_refresh.py tests/test_request_assembly.py \
  tests/test_file_mentions.py tests/test_compaction_gate_working_view.py \
  tests/test_context_compression_l2_window.py tests/test_context_compression.py \
  tests/test_compaction_checkpoint_view.py
# 150 passed in 2.93s
.venv/bin/ruff check ai_coding tests/test_microcompact_and_refresh.py tests/test_request_assembly.py
# All checks passed!
.venv/bin/mypy ai_coding
# Success: no issues found in 186 source files
```

### 未解决项
无。CMP-02 的枚举键仍不含内容哈希（同长度原地改写可能命中旧视图），该限制在 §4 已声明，本批次未扩大范围处理。
