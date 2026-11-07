# B2 上下文工程 + 压缩 实现验证（S4）

## 结果
- 定向 8 文件（file_mentions / request_assembly / microcompact / l2_window / gate_working_view / turn_reporter / context_compression / checkpoint_view）→ 165 passed
- 扩展集合（含 trace / memory / compaction）→ 174 passed
- `ruff` 全绿；`mypy ai_coding` 186 文件 0 错误（波次末复核）

## 与 01/02 的一致性
- CMP-03 公式 `max(8_000, min(40_000, window//3))`，注释声明"仅 <120k 窗口行为改变"。
- CMP-01 挂载点在 hard_trim 之后，仅 `out` 实际变化时挂载；`compress` 不再注入，`compress_if_needed` 补位。
- CMP-05 以 `(session_id, turn_count)` 为键，turn 变化清零。
- CMP-02 memo key 含 window/protect/anchor。
- CTX-01 超预算即停止添加；目录条目按剩余预算缩放。
- CTX-02 用 `stat` 估算并标"约"；CTX-03 `stat` + 前缀读取，非文本/超大给指针。
- CTX-04 只做 skill 去重，plan 文本未动。
- CTX-06 仅加注释，未删字段。
- CTX-07a/b/c 指标口径修正。

## 偏离（已接受，但需评审重点看）
1. **改了白名单外的 `core/agent_loop.py` 一处**：closing 步传 `offered_tools=False`。原因：不改调用点则 CTX-07a 无法生效（传空串会永久覆盖 reporter 的 tools_text）。
2. **重录 `tests/golden/loop_events/exhausted.json`**：仅 3 行变化——`history_chars` 48→50、94→98（计入工具参数），closing 步 `tools_chars` 116→0。与设计一致，未放宽断言，断言文件本身未改。

## 已知限制
- CMP-05 慢增长下可能不触发；5%+连续 3 次为折中。
- CMP-01 每请求重读 ≤5 文件；文件集合变化会使前缀变化（内容本身稳定）。
- 同长度原地改写不会命中 memo（append-only 成立；undo/rewrite 是已知边界）。

## 波次门禁
W1 全量 pytest：**exit 0**。
